# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client MAP Rule (31 Aug 2026) and Cost Adjustment tree (23 Aug 2026), rulings
confirmed 1 Sep 2026 (DR-33..DR-36) - bench --site <site> execute periodic_valuation.tests.smoke_map_rule.run

Special Case #1: backdated receipt into a POSITIVE prior period while the
  current period is negative and stays negative: no PRD in the prior period;
  current period books the carry at its frozen MAP and PRD = (rate - frozen) x qty.
Special Case #2: same, the carry turns the current period positive: PRD on the
  deficit, excess at the receipt rate, MAP resets to the rate.
C1 convention: the current-period leg of the signed C1 case now offsets the
  PRD account (not Inventory Variance), so PRD nets to zero across periods.
Backdated issue: the current period keeps its MAP; the carry's rate difference
  goes to the issue's expense account as a revaluation leg.
Cost Adjustment floor: a late cost may take inventory to zero, the excess is PRD.
Rolled back unless commit=True."""

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from periodic_valuation.periodic_moving_average.kernel import get_offset_account
from periodic_valuation.tests.smoke_edges import COMPANY, ipb, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def gl_net(voucher, account, posting_date=None):
	filters = {"voucher_no": voucher, "account": account, "is_cancelled": 0}
	if posting_date:
		filters["posting_date"] = posting_date
	rows = frappe.get_all("GL Entry", filters=filters, fields=["debit", "credit"])
	return flt(sum(flt(g.debit) - flt(g.credit) for g in rows), 2)


def run(commit=False):
	wh = ensure_masters()
	prior = get_first_day(add_months(nowdate(), -1))
	cur = get_first_day(nowdate())
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	py, pm, cy, cm = prior.year, prior.month, cur.year, cur.month
	first_of_cur = str(cur)
	from periodic_valuation.shared.accounts import get_inventory_account

	# ---------------- Special Case #1
	it = make_item("_MR-SC1")
	stock = get_inventory_account(COMPANY, it, wh)
	prd = get_offset_account(COMPANY, it, wh, "prd")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))   # prior 10 / 150
	make_dn(it, wh, 15)                                                  # current -5 / -75, frozen 15
	bd = make_pr(it, wh, 3, 43, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("SC#1 prior: plain blend 13 / 279, no PRD",
		flt(p.closing_qty) == 13 and flt(p.closing_value, 2) == 279 and flt(p.prd_value, 2) == 0,
		f"{p.closing_qty}/{p.closing_value}/{p.prd_value}")
	check("SC#1 current: carried at frozen 15 -> -2 / -30, still frozen",
		flt(c.closing_qty) == -2 and flt(c.closing_value, 2) == -30 and c.is_negative and flt(c.frozen_map) == 15,
		f"{c.closing_qty}/{c.closing_value} neg {c.is_negative} frozen {c.frozen_map}")
	check("SC#1 GL: Dr PRD 84 / Cr Inventory 84 on day 1 of the current period",
		gl_net(bd.name, prd, first_of_cur) == 84 and gl_net(bd.name, stock, first_of_cur) == -84,
		f"prd {gl_net(bd.name, prd, first_of_cur)} stock {gl_net(bd.name, stock, first_of_cur)}")
	check("SC#1 GL: prior-period receipt leg is the plain 129",
		gl_net(bd.name, stock, str(prior.replace(day=20))) == 129, str(gl_net(bd.name, stock, str(prior.replace(day=20)))))

	# ---------------- Special Case #2
	it = make_item("_MR-SC2")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))
	make_dn(it, wh, 15)
	bd = make_pr(it, wh, 8, 43, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("SC#2 prior: plain blend 18 / 494, no PRD",
		flt(p.closing_qty) == 18 and flt(p.closing_value, 2) == 494 and flt(p.prd_value, 2) == 0,
		f"{p.closing_qty}/{p.closing_value}/{p.prd_value}")
	check("SC#2 current: deficit 5 @ 15 + excess 3 @ 43 -> 3 / 129, MAP 43, unfrozen",
		flt(c.closing_qty) == 3 and flt(c.closing_value, 2) == 129 and flt(c.moving_avg_price, 2) == 43
		and not c.is_negative, f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price} neg {c.is_negative}")
	check("SC#2 GL: Dr PRD 140 / Cr Inventory 140 on day 1 of the current period",
		gl_net(bd.name, prd, first_of_cur) == 140 and gl_net(bd.name, stock, first_of_cur) == -140,
		f"prd {gl_net(bd.name, prd, first_of_cur)}")
	check("SC#2 since-zero counter restarts at the excess (3)",
		flt(c.total_received_since_zero) == 3, str(c.total_received_since_zero))

	# ---------------- C1 under the PRD convention (signed anchors 252 / 587 / 30.8947)
	it = make_item("_MR-C1")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))
	make_dn(it, wh, 20, posting_date=str(prior.replace(day=12)))      # prior -10 / -150 frozen 15
	make_pr(it, wh, 20, 17.5)                                          # current crosses to +10 / 175
	rv = frappe.get_doc({"doctype": "Stock Revaluation", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh, "new_valuation_rate": 20}]})
	rv.insert(ignore_permissions=True)
	rv.submit()                                                         # current 10 / 200
	bd = make_pr(it, wh, 9, 43, posting_date=str(prior.replace(day=20)))
	c = ipb_period(it, cy, cm)
	check("C1 anchors hold: current 19 / 587, MAP 30.8947",
		flt(c.closing_qty) == 19 and flt(c.closing_value, 2) == 587 and flt(c.moving_avg_price, 4) == 30.8947,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	check("C1 GL: prior PRD 252 (Dr) reversed by Dr Inventory 252 / Cr PRD 252 on day 1 - PRD nets to zero",
		gl_net(bd.name, prd, str(prior.replace(day=20))) == 252 and gl_net(bd.name, prd, first_of_cur) == -252
		and gl_net(bd.name, stock, first_of_cur) == 252 and gl_net(bd.name, prd) == 0,
		f"prior prd {gl_net(bd.name, prd, str(prior.replace(day=20)))} cur prd {gl_net(bd.name, prd, first_of_cur)} net {gl_net(bd.name, prd)}")

	# ---------------- backdated issue keeps the current period's MAP
	it = make_item("_MR-ISS")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))   # prior 10 / 150
	make_pr(it, wh, 5, 20)                                              # current 15 / 250, MAP 16.6667
	dn = make_dn(it, wh, 12, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	expense = frappe.get_all("GL Entry", filters={"voucher_no": dn.name, "is_cancelled": 0,
		"posting_date": str(prior.replace(day=20)), "debit": (">", 0)}, pluck="account")[0]
	check("ISS prior: issue at MAP 15 -> -2 / -30, frozen 15",
		flt(p.closing_qty) == -2 and flt(p.closing_value, 2) == -30 and p.is_negative and flt(p.frozen_map) == 15,
		f"{p.closing_qty}/{p.closing_value} frozen {p.frozen_map}")
	check("ISS current: 3 units at the kept MAP 16.6667 -> value 50",
		flt(c.closing_qty) == 3 and flt(c.closing_value, 2) == 50 and flt(c.moving_avg_price, 4) == 16.6667,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	check("ISS GL: -(15 - 16.67) x 12 = 20 -> Dr Expense 20 / Cr Inventory 20 on day 1",
		gl_net(dn.name, stock, first_of_cur) == -20 and gl_net(dn.name, expense, first_of_cur) == 20,
		f"stock {gl_net(dn.name, stock, first_of_cur)} expense {gl_net(dn.name, expense, first_of_cur)}")
	sle = flt(frappe.db.get_value("Stock Ledger Entry", {"voucher_no": dn.name, "is_cancelled": 0}, "stock_value_difference"), 2)
	check("ISS stock ledger shows the current-period effect (-200 = 12 x 16.67)", sle == -200, str(sle))

	# ---------------- Cost Adjustment floor: inventory to zero, excess to PRD
	from periodic_valuation.periodic_moving_average.kernel import post_value_event
	it = make_item("_MR-FLOOR")
	pr = make_pr(it, wh, 100, 10); make_dn(it, wh, 90)                  # 10 @ 100
	srbnb = frappe.get_cached_value("Company", COMPANY, "stock_received_but_not_billed")
	post_value_event(COMPANY, it, wh, source=("Purchase Receipt", pr.name, pr.items[0].name),
		posting_date=nowdate(), reason="invoice_diff", value_delta=-300, offset_account=srbnb)
	ev = frappe.get_all("Inventory Valuation Event", filters={"source_docname": pr.name, "reason_code": "invoice_diff"},
		fields=["value_delta", "expense_portion"])[0]
	c = ipb(it)
	check("floor: -300 adjustment -> inventory -100 (lands on zero), price difference -200",
		flt(ev.value_delta, 2) == -100 and flt(ev.expense_portion, 2) == -200 and flt(c.closing_value, 2) == 0,
		f"{ev.value_delta}/{ev.expense_portion} closing {c.closing_value}")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
