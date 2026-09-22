# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-47 demo pack - per-item settlement and the settlement gate on the
month freeze, entered the way a user enters them in the STD UAT company so
the client can open every document and finish the last step themselves.

bench --site <site> execute periodic_valuation.tests.uat_dr47_pack.run --kwargs "{'commit': True}"

  D47-A  receipt at a price variance; settled by a Settlement Run filtered
         to THIS ITEM only (the run document carries the filter)
  D47-B / D47-C  two items in item group "DR-47 Demo Group"; settled by ONE
         Settlement Run filtered to the group
  D47-D  receipt at a price variance, deliberately LEFT UNSETTLED: its
         Inventory Period Balance shows "Settle This Item"
  D47-E  receipt dated in the PREVIOUS month (still previous-open): that
         month now has an unsettled standard-cost scope, so its Inventory
         Period Close is REFUSED naming D47-E until it is settled. The pack
         proves the full path (refuse -> settle -> close passes) inside a
         savepoint and rolls it back, so the client performs it: Settle This
         Item on the previous-month balance row, then Close again.

Runs in "STD UAT Co" (created by uat_std_pack if missing). Rolled back
unless commit=True; the scenario -> document index prints between INDEX
markers."""

import frappe
from frappe.utils import flt, getdate, nowdate

from periodic_valuation.tests.uat_std_pack import COMPANY, ensure_company, make_pr, scv_release, std_item

CHECKS, INDEX = [], []
GROUP = "DR-47 Demo Group"


def check(ref, label, ok, detail=""):
	CHECKS.append((f"{ref} {label}", bool(ok)))
	print(("PASS " if ok else "FAIL ") + f"{ref} {label}" + (f" - {detail}" if detail and not ok else ""))


def idx(ref, scenario, docs, expected, observed):
	INDEX.append((ref, scenario, docs, expected, observed))


def demo_item(code, name, group=None):
	std_item(code)
	frappe.db.set_value("Item", code, {"item_name": name, **({"item_group": group} if group else {})},
		update_modified=False)
	frappe.clear_document_cache("Item", code)
	return code


def run_settlement(year, month, **scope):
	run = frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": COMPANY,
		"period_year": year, "period_month": month, "run_type": "INITIAL_CLOSE", **scope})
	run.insert(ignore_permissions=True)
	run.submit()
	run.reload()
	return run


def settled(item, year, month):
	return frappe.db.get_value("Inventory Period Settlement", {"company": COMPANY, "item_code": item,
		"period_year": year, "period_month": month, "cancelled": 0})


def balance(item, year, month):
	return frappe.get_doc("Inventory Period Balance", {"company": COMPANY, "item_code": item,
		"period_year": year, "period_month": month})


def run(commit=False):
	from periodic_valuation.shared.period_close import assert_std_scopes_settled

	wh, _ = ensure_company()
	today = getdate(nowdate())
	cy, cm = today.year, today.month
	py, pm = (cy - 1, 12) if cm == 1 else (cy, cm - 1)
	prev = frappe.get_doc("Inventory Period", {"company": COMPANY, "period_year": py, "period_month": pm})
	if prev.status != "PREV_OPEN_UNSETTLED":
		frappe.throw(f"{prev.name} is {prev.status}; the pack needs the previous month previous-open.")
	if frappe.db.exists("Item Group", GROUP) or frappe.db.exists("Item", {"item_code": ("like", "D47-%")}):
		frappe.throw("The DR-47 demo pack has already been applied on this site (D47-* items or the demo group exist).")
	if not frappe.db.exists("Item Group", GROUP):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": GROUP,
			"parent_item_group": frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}),
		}).insert(ignore_permissions=True)

	# ---- D47-A: settle ONE item by a filtered run
	a = demo_item("D47-A Single Item Run", "D47-A - settled by an item-filtered run")
	scv_release(a, cy, cm, 10)
	pr_a = make_pr(a, wh, 100, 12)                       # PPV 200
	run_a = run_settlement(cy, cm, item_code=a)
	sa = settled(a, cy, cm)
	check("D47-A", "item-filtered run settles exactly this item", run_a.scopes_settled == 1 and sa,
		f"settled {run_a.scopes_settled} sett {sa}")
	idx("D47-A", "Settlement Run filtered to one item",
		[("Purchase Receipt", pr_a.name), ("Inventory Period Settlement Run", run_a.name),
			("Inventory Period Settlement", sa)],
		"1 scope settled; PPV 200 split by the MTD rule (all on hand -> 200 to stock)",
		f"scopes_settled {run_a.scopes_settled}, es_var {flt(frappe.db.get_value('Inventory Period Settlement', sa, 'es_var'), 2)}")

	# ---- D47-B / D47-C: settle an ITEM GROUP by one run
	b = demo_item("D47-B Group Run", "D47-B - settled by the item-group run", GROUP)
	c = demo_item("D47-C Group Run", "D47-C - settled by the item-group run", GROUP)
	for it, rate in ((b, 11), (c, 13)):
		scv_release(it, cy, cm, 10)
	pr_b = make_pr(b, wh, 50, 11)                        # PPV 50
	pr_c = make_pr(c, wh, 20, 13)                        # PPV 60
	run_g = run_settlement(cy, cm, item_group=GROUP)
	sb, sc_ = settled(b, cy, cm), settled(c, cy, cm)
	check("D47-BC", "item-group run settles both group members and nothing else",
		run_g.scopes_settled == 2 and sb and sc_, f"settled {run_g.scopes_settled} B={sb} C={sc_}")
	idx("D47-B/C", f"Settlement Run filtered to item group '{GROUP}'",
		[("Purchase Receipt", pr_b.name), ("Purchase Receipt", pr_c.name),
			("Inventory Period Settlement Run", run_g.name),
			("Inventory Period Settlement", sb), ("Inventory Period Settlement", sc_)],
		"2 scopes settled in one run (D47-B PPV 50, D47-C PPV 60)",
		f"scopes_settled {run_g.scopes_settled}")

	# ---- D47-D: left unsettled -> Settle This Item offered on its balance row
	d = demo_item("D47-D Unsettled", "D47-D - left unsettled: Settle This Item on the balance row")
	scv_release(d, cy, cm, 10)
	pr_d = make_pr(d, wh, 30, 14)                        # PPV 120
	bal_d = balance(d, cy, cm)
	st = bal_d.settlement_state()
	check("D47-D", "balance row offers Settle This Item (STD, month postable, no live settlement)",
		st["is_std"] and st["postable"] and not st["settled"], str(st))
	idx("D47-D", "unsettled item in the open month - the Settle This Item button",
		[("Purchase Receipt", pr_d.name), ("Inventory Period Balance", bal_d.name)],
		"button visible; clicking it opens a Settlement Run pre-filled with the item",
		f"settlement_state {st}")

	# ---- D47-E: previous-month receipt makes the previous month unfreezable until settled
	e = demo_item(f"D47-E Blocks {prev.period_name} Close",
		f"D47-E - receipt dated {prev.period_name}: Close of {prev.period_name} refused until settled")
	scv_release(e, py, pm, 10)
	pr_e = make_pr(e, wh, 10, 12, posting_date=f"{py}-{pm:02d}-20")   # (BD) receipt, PPV 20 in the previous month
	gate = assert_std_scopes_settled(prev)
	names = {u["item_code"] for u in gate["unsettled"]}
	check("D47-E", "settlement gate names the previous-month scope", e in names and not gate["ok"], str(sorted(names)))
	def try_close():
		ipc = frappe.get_doc({"doctype": "Inventory Period Close", "company": COMPANY,
			"inventory_period": prev.name, "posting_date": nowdate()})
		ipc.insert(ignore_permissions=True)
		ipc.submit()
		return ipc

	# probe the whole client path inside a savepoint, then roll it back so
	# the client performs it live: refuse -> Settle This Item -> Close passes
	frappe.db.savepoint("d47_close_probe")
	refused, passed_after = "", None
	try:
		try_close()
	except frappe.ValidationError as ex:
		refused = frappe.utils.strip_html(str(ex))
	frappe.db.rollback(save_point="d47_close_probe")
	check("D47-E", "Inventory Period Close of the previous month is refused and names D47-E",
		e in refused and "Settlement Run" in refused, refused[:200])
	frappe.db.savepoint("d47_close_after")
	try:
		run_e = run_settlement(py, pm, item_code=e)          # what Settle This Item opens
		ipc_ok = try_close()
		passed_after = (run_e.scopes_settled, frappe.db.get_value("Inventory Period", prev.name, "status"),
			ipc_ok.std_scopes_settled)
	except frappe.ValidationError as ex:
		passed_after = ("ERR", frappe.utils.strip_html(str(ex))[:160])
	frappe.db.rollback(save_point="d47_close_after")
	check("D47-E", "after settling D47-E the same Close passes and freezes the month (probed, rolled back for the client)",
		passed_after == (1, "SETTLED_FROZEN", 1), str(passed_after))
	check("D47-E", "probe left the previous month previous-open and D47-E unsettled for the client",
		frappe.db.get_value("Inventory Period", prev.name, "status") == "PREV_OPEN_UNSETTLED" and not settled(e, py, pm))
	bal_e = balance(e, py, pm)
	idx("D47-E", f"previous month {prev.period_name}: Close refused until this scope is settled (client step)",
		[("Purchase Receipt", pr_e.name), ("Inventory Period Balance", bal_e.name), ("Inventory Period", prev.name)],
		"Close -> 'Standard-cost scopes not yet settled ... D47-E'; then Settle This Item on the balance row; Close again -> frozen",
		f"gate names {sorted(names)}; refusal: {refused[:160]}")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	print("\n===== INDEX =====")
	for ref, scenario, docs, expected, observed in INDEX:
		print(f"{ref}: {scenario}")
		for dt, dn in docs:
			print(f"    {dt}: {dn}")
		print(f"    expected: {expected}")
		print(f"    observed: {observed}")
	print("===== END INDEX =====")
	if commit and not failed:
		frappe.db.commit()
		print("COMMITTED")
	else:
		frappe.db.rollback()
		print("ROLLED BACK")
