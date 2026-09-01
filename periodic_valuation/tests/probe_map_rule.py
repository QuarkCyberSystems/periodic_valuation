# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Probe: the client's MAP Rule (31 Aug 2026) cases not covered by the signed
C1/C2 anchors - bench --site <site> execute periodic_valuation.tests.probe_map_rule.run

Special Case #1: backdated receipt into a POSITIVE prior period while the
  current period is NEGATIVE and stays negative after the carry.
  Client: no PRD in the prior period; PRD in the current period =
  (rate - current frozen MAP) x qty; the carried inventory = qty x frozen MAP.
Special Case #2: same, but the carry turns the current period positive.
  Client: PRD in current = (rate - current frozen MAP) x |current deficit|;
  inventory = deficit x frozen + excess x rate; MAP resets to the rate.
Issue case: backdated issue that drives the prior period negative (client
  rows "Bef+ & Aft-" for an issue) - report what the kernel does.

Prints the kernel's actual period balances and GL next to the client's
expected numbers. Rolled back always."""

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from periodic_valuation.tests.smoke_edges import COMPANY, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters

OUT = []


def say(label, ok, detail):
	OUT.append((label, ok))
	print(("PASS " if ok else "FAIL ") + label + f" - {detail}")


def gl_for(voucher):
	rows = frappe.get_all("GL Entry", filters={"voucher_no": voucher, "is_cancelled": 0},
		fields=["account", "debit", "credit", "posting_date"])
	out = {}
	for g in rows:
		k = (g.account.split(" - ")[0], str(g.posting_date))
		out[k] = flt(out.get(k, 0) + flt(g.debit) - flt(g.credit), 2)
	return {k: v for k, v in out.items() if v}


def state(item, y, m):
	b = ipb_period(item, y, m)
	return f"qty {flt(b.closing_qty)} / value {flt(b.closing_value, 2)} / MAP {flt(b.moving_avg_price, 4)} / neg {b.is_negative} frozen {flt(b.frozen_map)} / prd {flt(b.prd_value, 2)} / adjust {flt(b.adjust_value, 2)} / carry {flt(b.carryover_qty)} {flt(b.carryover_value, 2)}"


def run(commit=False):
	wh = ensure_masters()
	prior = get_first_day(add_months(nowdate(), -1))
	cur = get_first_day(nowdate())
	# the period machine may not have rolled to this month yet (e.g. on the 1st):
	# advance it inside this rolled-back transaction - older PREV_OPEN -> FROZEN,
	# OPEN -> PREV_OPEN, current month OPEN
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_year": cur.year,
			"period_month": cur.month, "status": "OPEN"}):
		for p in frappe.get_all("Inventory Period", filters={"company": COMPANY,
				"status": "PREV_OPEN_UNSETTLED"}, pluck="name"):
			frappe.db.set_value("Inventory Period", p, "status", "SETTLED_FROZEN", update_modified=False)
		for p in frappe.get_all("Inventory Period", filters={"company": COMPANY, "status": "OPEN"}, pluck="name"):
			frappe.db.set_value("Inventory Period", p, "status", "PREV_OPEN_UNSETTLED", update_modified=False)
		for d, status in ((prior, "PREV_OPEN_UNSETTLED"), (cur, "OPEN")):
			name = frappe.db.get_value("Inventory Period", {"company": COMPANY,
				"period_year": d.year, "period_month": d.month})
			if name:
				frappe.db.set_value("Inventory Period", name, "status", status, update_modified=False)
			else:
				doc = frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
					"start_date": d, "status": status})
				doc.flags.ignore_validate = True
				doc.insert(ignore_permissions=True)
				frappe.db.set_value("Inventory Period", doc.name, "status", status, update_modified=False)
	py, pm, cy, cm = prior.year, prior.month, cur.year, cur.month
	prior_day = str(prior.replace(day=10))

	# ---------------- Special Case #1: prior +, current - and stays -
	it = make_item("_PRB-SC1")
	make_pr(it, wh, 10, 15, posting_date=prior_day)   # prior 10 / 150
	make_dn(it, wh, 15)                                # current: 10 -> -5, frozen 15
	print("\n[SC#1] before backdate: prior", state(it, py, pm)); print("        current", state(it, cy, cm))
	bd = make_pr(it, wh, 3, 43, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	print("[SC#1] after backdate : prior", state(it, py, pm)); print("        current", state(it, cy, cm))
	print("        GL of backdated PR:", gl_for(bd.name))
	# client expectation
	say("SC#1 prior: plain blend 13 / 279 (no PRD in prior)",
		flt(p.closing_qty) == 13 and flt(p.closing_value, 2) == 279 and flt(p.prd_value, 2) == 0,
		f"got {flt(p.closing_qty)} / {flt(p.closing_value, 2)} prd {flt(p.prd_value, 2)}")
	say("SC#1 current: carry at frozen 15 -> -2 / -30, PRD 84 = (43-15)x3 booked in current",
		flt(c.closing_qty) == -2 and flt(c.closing_value, 2) == -30,
		f"got {flt(c.closing_qty)} / {flt(c.closing_value, 2)} (client: -2 / -30, PRD 84)")

	# ---------------- Special Case #2: prior +, current - and turns +
	it = make_item("_PRB-SC2")
	make_pr(it, wh, 10, 15, posting_date=prior_day)   # prior 10 / 150
	make_dn(it, wh, 15)                                # current -5 / -75 frozen 15
	bd = make_pr(it, wh, 8, 43, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	print("\n[SC#2] after backdate : prior", state(it, py, pm)); print("        current", state(it, cy, cm))
	print("        GL of backdated PR:", gl_for(bd.name))
	say("SC#2 prior: plain blend 18 / 494 (no PRD in prior)",
		flt(p.closing_qty) == 18 and flt(p.closing_value, 2) == 494 and flt(p.prd_value, 2) == 0,
		f"got {flt(p.closing_qty)} / {flt(p.closing_value, 2)} prd {flt(p.prd_value, 2)}")
	say("SC#2 current: deficit 5 cleared at frozen 15, excess 3 @ 43 -> 3 / 129, MAP 43, PRD 140 in current",
		flt(c.closing_qty) == 3 and flt(c.closing_value, 2) == 129 and flt(c.moving_avg_price, 2) == 43,
		f"got {flt(c.closing_qty)} / {flt(c.closing_value, 2)} MAP {flt(c.moving_avg_price, 4)} (client: 3 / 129 / 43, PRD 140)")

	# ---------------- Backdated ISSUE driving the prior period negative
	it = make_item("_PRB-ISS")
	make_pr(it, wh, 10, 15, posting_date=prior_day)   # prior 10 / 150
	make_pr(it, wh, 5, 20)                             # current 15 / 250, MAP 16.6667
	print("\n[ISS] before backdate: prior", state(it, py, pm)); print("       current", state(it, cy, cm))
	bd = make_dn(it, wh, 12, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	print("[ISS] after backdate : prior", state(it, py, pm)); print("       current", state(it, cy, cm))
	print("       GL of backdated DN:", gl_for(bd.name))
	say("ISS prior: issue at MAP 15 -> -2 / -30, frozen 15, no PRD",
		flt(p.closing_qty) == -2 and flt(p.closing_value, 2) == -30 and p.is_negative and flt(p.frozen_map) == 15,
		f"got {state(it, py, pm)}")
	say("ISS current: carry -12 @ 15 -> 3 / 70 (MAP 23.33) - reported, client rule silent on issues at MAP",
		flt(c.closing_qty) == 3 and flt(c.closing_value, 2) == 70, f"got {state(it, cy, cm)}")

	failed = [x for x in OUT if not x[1]]
	print(f"\n{len(OUT) - len(failed)}/{len(OUT)} client expectations met")
	frappe.db.rollback()
