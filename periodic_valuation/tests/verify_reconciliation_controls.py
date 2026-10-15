"""DR-69: the controls behind the reconciliation gate, and the repair of the
three UAT conditions (badiav16, 15/10/2026). Run:
bench --site <site> execute periodic_valuation.tests.verify_reconciliation_controls.run

  1  an inventory account cannot change while an item it values holds stock
     (Item; Company); it can while the item holds none
  2  repair, condition A: a stock leg posted to another asset account moves to
     the item's inventory account (kernel-tagged, reason account_transfer)
  3  repair, condition B: a settlement of the OPEN month is reversed
  4  repair, condition C: a balance that drifted from its events is rebuilt
  5  after the repair the gate reconciles every open month

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("Reconciliation control failures: " + "; ".join(c[0] for c in failed))


def _refusal(fn):
	frappe.db.savepoint("rc_refusal")
	try:
		fn()
		return ""
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="rc_refusal")


def _gates():
	from periodic_valuation.shared.period_close import run_reconciliation_gate

	return {p: flt(run_reconciliation_gate(frappe.get_doc("Inventory Period", p))["discrepancy"], 2)
		for p in frappe.get_all("Inventory Period", filters={"company": pack.COMPANY,
			"status": ("in", ("OPEN", "PREV_OPEN_UNSETTLED"))}, pluck="name")}


def _run():
	from periodic_valuation.overrides.account_guard import guard_inventory_account
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	frappe.db.savepoint("recon_controls")
	try:
		wh, _ = pack.ensure_company()
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cp = pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		a = pack.acct()
		other = pack.make_acc("_DR69 Fixtures", "Asset")

		# ---- 1: account lock ------------------------------------------------------
		held = pack.std_item("_DR69-HELD")
		pack.scv_release(held, prev.year, prev.month, 10)
		pack.make_pr(held, wh, 5, 10, posting_date=str(add_days(prev, 1)))
		empty = pack.std_item("_DR69-EMPTY")

		def set_item_account(item, account):
			# the guard as a save of this change would run it
			doc = frappe.get_doc("Item", item)
			doc._doc_before_save = frappe.get_doc("Item", item)
			rows = [r for r in doc.item_defaults if r.company == pack.COMPANY]
			if rows:
				rows[0].default_inventory_account = account
			else:
				doc.append("item_defaults", {"company": pack.COMPANY, "default_inventory_account": account})
			guard_inventory_account(doc)

		msg = _refusal(lambda: set_item_account(held, other))
		check("1: an item's inventory account cannot change while it holds stock", "cannot change" in msg, msg)
		check("1: it can change while the item holds none", not _refusal(lambda: set_item_account(empty, other)))
		def company_change():
			c = frappe.get_doc("Company", pack.COMPANY)
			c.default_inventory_account = other
			c.save(ignore_permissions=True)
		msg = _refusal(company_change)
		check("1: the company's default inventory account cannot change while an item holds stock",
			"cannot change" in msg, msg)

		# ---- 2-4: the three conditions -----------------------------------------------
		base = _gates()
		# A: a stock leg on another asset account (the account setup changed while stocked)
		ma = pack.std_item("_DR69-MISROUTED")
		pack.scv_release(ma, prev.year, prev.month, 20)
		pra = pack.make_pr(ma, wh, 10, 20, posting_date=str(add_days(prev, 2)))
		frappe.db.sql("""UPDATE `tabGL Entry` SET account = %s WHERE voucher_no = %s AND account = %s""",
			(other, pra.name, a.stock))
		# B: a settlement of the OPEN month (made before DR-69: carry dated into a month with no period)
		mb = pack.std_item("_DR69-OPENSETT")
		pack.scv_release(mb, today.year, today.month, 10)
		pack.make_pr(mb, wh, 10, 12, posting_date=str(day1))
		frappe.db.set_value("Inventory Period", cp, "status", "PREV_OPEN_UNSETTLED", update_modified=False)
		frappe.db.set_value("Inventory Period", pp, "status", "SETTLED_FROZEN", update_modified=False)
		sb = StdEngine(pack.COMPANY, mb).close_period(year=today.year, month=today.month, sc=10,
			source=("Inventory Period", cp))
		frappe.db.set_value("Inventory Period", cp, "status", "OPEN", update_modified=False)
		frappe.db.set_value("Inventory Period", pp, "status", "PREV_OPEN_UNSETTLED", update_modified=False)
		# C: a balance that drifted from its events
		mc = pack.std_item("_DR69-DRIFT")
		pack.scv_release(mc, prev.year, prev.month, 30)
		pack.make_pr(mc, wh, 4, 30, posting_date=str(add_days(prev, 3)))
		crow = frappe.db.get_value("Inventory Period Balance", {"company": pack.COMPANY, "item_code": mc,
			"period_year": today.year, "period_month": today.month})
		frappe.db.set_value("Inventory Period Balance", crow, {"closing_value": 999, "carryover_value": 999 - 120},
			update_modified=False)
		broken = _gates()

		from periodic_valuation.patches.v1_0 import dr69_reconcile_open_periods

		dr69_reconcile_open_periods.execute()
		after = _gates()
		moved = frappe.get_all("Inventory Valuation Event", filters={"item_code": ma, "reason_code": "account_transfer"},
			pluck="name")
		gl_moved = frappe.get_all("GL Entry", filters={"valuation_event_id": ("in", moved or [""]), "is_cancelled": 0},
			fields=["account", "debit", "credit"])
		check("2: the misrouted stock leg moves to the inventory account (kernel-tagged transfer)",
			len(moved) == 1 and sorted((g.account, flt(g.debit) - flt(g.credit)) for g in gl_moved)
			== sorted([(a.stock, 200.0), (other, -200.0)]), str(gl_moved))
		check("3: the OPEN-month settlement is reversed",
			frappe.db.get_value("Inventory Period Settlement", sb.name, "cancelled") == 1)
		check("4: the drifted balance is rebuilt from its events (4 x 30)",
			flt(frappe.db.get_value("Inventory Period Balance", crow, "closing_value"), 2) == 120.0)
		check("5: the gate reconciles every open month after the repair",
			broken != base and all(abs(v - base[k]) < 0.01 for k, v in after.items()),
			f"base {base} broken {broken} after {after}")
	finally:
		frappe.db.rollback(save_point="recon_controls")
