# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client MAP Rule (31 Aug 2026) and Cost Adjustment tree (23 Aug 2026), rulings
confirmed 1 Sep 2026 (DR-33..DR-36) and MAP-001 (DR-46) - bench --site <site> execute periodic_valuation.tests.smoke_map_rule.run

Special Case #1: backdated receipt into a POSITIVE prior period while the
  current period is negative and stays negative: no PRD in the prior period;
  current period books the carry at its frozen MAP and PRD = (rate - frozen) x qty.
Special Case #2: same, the carry turns the current period positive: PRD on the
  deficit, excess at the receipt rate, MAP resets to the rate.
C1 convention: the current-period leg of the signed C1 case now offsets the
  PRD account (not Inventory Variance), so PRD nets to zero across periods.
Backdated issue (DR-46, client MAP-001 10 Sep 2026): the carried value flows
  into the current period and its MAP re-derives; a current period that is or
  becomes negative re-prices the deficit at its frozen MAP, difference to PRD.
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

	# ---------------- backdated issue: the carry flows, the current MAP re-derives
	# (DR-46, client MAP-001 10 Sep 2026; supersedes DR-36's correcting leg)
	it = make_item("_MR-ISS")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))   # prior 10 / 150
	make_pr(it, wh, 5, 20)                                              # current 15 / 250, MAP 16.6667
	dn = make_dn(it, wh, 12, posting_date=str(prior.replace(day=20)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("ISS prior: issue at MAP 15 -> -2 / -30, frozen 15",
		flt(p.closing_qty) == -2 and flt(p.closing_value, 2) == -30 and p.is_negative and flt(p.frozen_map) == 15,
		f"{p.closing_qty}/{p.closing_value} frozen {p.frozen_map}")
	check("ISS current: 3 units carrying 250 - 180 = 70 -> MAP 23.3333",
		flt(c.closing_qty) == 3 and flt(c.closing_value, 2) == 70 and flt(c.moving_avg_price, 4) == 23.3333,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	check("ISS GL: nothing posted in the current period",
		gl_net(dn.name, stock, first_of_cur) == 0 and gl_net(dn.name, stock) == -180,
		f"day-1 stock {gl_net(dn.name, stock, first_of_cur)} total stock {gl_net(dn.name, stock)}")
	sle = frappe.db.get_value("Stock Ledger Entry", {"voucher_no": dn.name, "is_cancelled": 0},
		["stock_value_difference", "valuation_rate"], as_dict=True)
	check("ISS stock ledger shows the prior period's value (-180) at the re-derived rate",
		flt(sle.stock_value_difference, 2) == -180 and flt(sle.valuation_rate, 4) == 23.3333,
		f"{sle.stock_value_difference} @ {sle.valuation_rate}")

	# ---------------- backdated issue into a FROZEN current period: deficit at the
	# current frozen MAP, difference to PRD (the negative-stock convention)
	it = make_item("_MR-ISSNEG")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))   # prior 10 / 150
	make_pr(it, wh, 10, 25)                                             # current 20 / 400, MAP 20
	make_dn(it, wh, 22)                                                 # current -2 / -40, frozen 20
	dn = make_dn(it, wh, 5, posting_date=str(prior.replace(day=20)))   # prior 5 / 75; carry -5 / -75
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("ISSNEG prior: 5 / 75 at MAP 15",
		flt(p.closing_qty) == 5 and flt(p.closing_value, 2) == 75, f"{p.closing_qty}/{p.closing_value}")
	check("ISSNEG current: deficit 7 at frozen 20 -> -7 / -140, still frozen 20",
		flt(c.closing_qty) == -7 and flt(c.closing_value, 2) == -140 and c.is_negative and flt(c.frozen_map) == 20,
		f"{c.closing_qty}/{c.closing_value} frozen {c.frozen_map}")
	check("ISSNEG GL: (15 - 20) x 5 = -25 -> Dr PRD 25 / Cr Inventory 25 on day 1",
		gl_net(dn.name, stock, first_of_cur) == -25 and gl_net(dn.name, prd, first_of_cur) == 25,
		f"stock {gl_net(dn.name, stock, first_of_cur)} prd {gl_net(dn.name, prd, first_of_cur)}")

	# ---------------- backdated issue that takes the current period THROUGH zero:
	# freezes at the MAP before the carry, deficit at that price, difference to PRD
	it = make_item("_MR-ISSX")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))   # prior 10 / 150
	make_pr(it, wh, 5, 20)                                              # current 15 / 250, MAP 16.6667
	dn = make_dn(it, wh, 20, posting_date=str(prior.replace(day=20)))  # prior -10 / -150; carry -20 / -300
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("ISSX prior: -10 / -150 frozen 15",
		flt(p.closing_qty) == -10 and flt(p.closing_value, 2) == -150 and flt(p.frozen_map) == 15,
		f"{p.closing_qty}/{p.closing_value} frozen {p.frozen_map}")
	check("ISSX current: -5 at the crossing MAP 16.6667 -> -83.33, frozen 16.6667",
		flt(c.closing_qty) == -5 and flt(c.closing_value, 2) == -83.33 and c.is_negative
		and flt(c.frozen_map, 4) == 16.6667, f"{c.closing_qty}/{c.closing_value} frozen {c.frozen_map}")
	check("ISSX GL: -83.33 - (250 - 300) = -33.33 -> Dr PRD 33.33 / Cr Inventory 33.33 on day 1",
		gl_net(dn.name, stock, first_of_cur) == -33.33 and gl_net(dn.name, prd, first_of_cur) == 33.33,
		f"stock {gl_net(dn.name, stock, first_of_cur)} prd {gl_net(dn.name, prd, first_of_cur)}")

	# ---------------- MAP-001: the client's own sequence (Ruba, 10 Sep 2026)
	# prior = her August, current = her September; issues are backdated where hers were
	it = make_item("_MR-MAP001")
	make_dn(it, wh, 100, posting_date=str(prior.replace(day=2)))
	make_dn(it, wh, 10, posting_date=str(cur.replace(day=2)))
	make_pr(it, wh, 120, 50, posting_date=str(cur.replace(day=3)))
	make_dn(it, wh, 20, posting_date=str(prior.replace(day=3)))
	make_pr(it, wh, 100, 40, posting_date=str(prior.replace(day=4)))
	make_pr(it, wh, 30, 50, posting_date=str(prior.replace(day=5)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("MAP-001 before: prior 10 / 500 @ 50, current 120 / 5,100 @ 42.50 (her Before sheet)",
		flt(p.closing_qty) == 10 and flt(p.closing_value, 2) == 500 and flt(p.moving_avg_price, 2) == 50
		and flt(c.closing_qty) == 120 and flt(c.closing_value, 2) == 5100 and flt(c.moving_avg_price, 2) == 42.5,
		f"prior {p.closing_qty}/{p.closing_value}/{p.moving_avg_price} current {c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	dn = make_dn(it, wh, 20, posting_date=str(prior.replace(day=6)))
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("MAP-001 after: prior -10 / -500 frozen 50",
		flt(p.closing_qty) == -10 and flt(p.closing_value, 2) == -500 and flt(p.frozen_map) == 50,
		f"{p.closing_qty}/{p.closing_value} frozen {p.frozen_map}")
	check("MAP-001 after: current 100 / 4,100 -> MAP 41.00 (her expected figure)",
		flt(c.closing_qty) == 100 and flt(c.closing_value, 2) == 4100 and flt(c.moving_avg_price, 2) == 41,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	check("MAP-001 GL: one leg only, 1,000 out of inventory in the prior period",
		gl_net(dn.name, stock) == -1000 and gl_net(dn.name, stock, first_of_cur) == 0,
		f"total {gl_net(dn.name, stock)} day-1 {gl_net(dn.name, stock, first_of_cur)}")

	# ---------------- the carry takes a POSITIVE current period's value below zero:
	# floored at zero (DR-34), the rest to PRD - positive stock never carries
	# negative value
	it = make_item("_MR-ISSFLOOR")
	make_pr(it, wh, 10, 50, posting_date=str(prior.replace(day=10)))   # prior 10 / 500
	make_pr(it, wh, 100, 45)                                            # current 110 / 5,000
	dn = make_dn(it, wh, 105, posting_date=str(prior.replace(day=20))) # prior -95 / -4,750; carry -105 / -5,250
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("ISSFLOOR prior: -95 / -4,750 frozen 50",
		flt(p.closing_qty) == -95 and flt(p.closing_value, 2) == -4750 and flt(p.frozen_map) == 50,
		f"{p.closing_qty}/{p.closing_value} frozen {p.frozen_map}")
	check("ISSFLOOR current: 5 units floored at value 0.00, not -250",
		flt(c.closing_qty) == 5 and flt(c.closing_value, 2) == 0 and not c.is_negative,
		f"{c.closing_qty}/{c.closing_value} neg {c.is_negative}")
	check("ISSFLOOR GL: the 250 inventory could not give up credits PRD (DR-34 convention) on day 1",
		gl_net(dn.name, stock, first_of_cur) == 250 and gl_net(dn.name, prd, first_of_cur) == -250,
		f"stock {gl_net(dn.name, stock, first_of_cur)} prd {gl_net(dn.name, prd, first_of_cur)}")

	# ---------------- the carry lands the current period on exactly zero:
	# the residual is floored to PRD, the MAP is retained
	it = make_item("_MR-ISSZERO")
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))   # prior 10 / 150
	make_pr(it, wh, 5, 20)                                              # current 15 / 250, MAP 16.6667
	dn = make_dn(it, wh, 15, posting_date=str(prior.replace(day=20)))  # carry -15 / -225 -> 0 / 25
	c = ipb_period(it, cy, cm)
	check("ISSZERO current: 0 / 0.00, MAP retained at 16.6667",
		flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0 and flt(c.moving_avg_price, 4) == 16.6667,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	check("ISSZERO GL: residual 25 -> Dr PRD 25 / Cr Inventory 25 on day 1",
		gl_net(dn.name, stock, first_of_cur) == -25 and gl_net(dn.name, prd, first_of_cur) == 25,
		f"stock {gl_net(dn.name, stock, first_of_cur)} prd {gl_net(dn.name, prd, first_of_cur)}")

	# ---------------- cancelling a backdated document unwinds its day-1 leg too
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	it = make_item("_MR-ISSNEGCX")                                      # the ISSNEG shape again
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))
	make_pr(it, wh, 10, 25)
	make_dn(it, wh, 22)                                                 # current -2 / -40 frozen 20
	dn = make_dn(it, wh, 5, posting_date=str(prior.replace(day=20)))   # current -7 / -140, prd_split -25
	cx = frappe.get_doc("Delivery Note", make_cancellation("Delivery Note", dn.name))
	cx.submit()
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("CANCEL backdated issue: prior period untouched at 5 / 75 (a cancellation is current-dated, CD-01)",
		flt(p.closing_qty) == 5 and flt(p.closing_value, 2) == 75, f"{p.closing_qty}/{p.closing_value}")
	check("CANCEL backdated issue: current back to -2 / -40, frozen 20 (day-1 leg unwound)",
		flt(c.closing_qty) == -2 and flt(c.closing_value, 2) == -40 and flt(c.frozen_map) == 20,
		f"{c.closing_qty}/{c.closing_value} frozen {c.frozen_map}")
	check("CANCEL backdated issue: PRD nets to zero across original + cancellation",
		gl_net(dn.name, prd) + gl_net(cx.name, prd) == 0 and gl_net(dn.name, stock) + gl_net(cx.name, stock) == 0,
		f"prd {gl_net(dn.name, prd)}+{gl_net(cx.name, prd)} stock {gl_net(dn.name, stock)}+{gl_net(cx.name, stock)}")
	mirrors = frappe.get_all("Inventory Valuation Event", filters={"source_docname": cx.name, "is_cancelled": 0},
		fields=["reversal_of"])
	check("CANCEL backdated issue: two mirror events, one per original event",
		len(mirrors) == 2 and all(m.reversal_of for m in mirrors), str(mirrors))

	it = make_item("_MR-SC1CX")                                         # the SC#1 shape again
	make_pr(it, wh, 10, 15, posting_date=str(prior.replace(day=10)))
	make_dn(it, wh, 15)                                                 # current -5 / -75 frozen 15
	bd = make_pr(it, wh, 3, 43, posting_date=str(prior.replace(day=20)))  # current -2 / -30, prd_split -84
	cx = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", bd.name))
	cx.submit()
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	check("CANCEL backdated receipt: prior period untouched at 13 / 279 (a cancellation is current-dated, CD-01)",
		flt(p.closing_qty) == 13 and flt(p.closing_value, 2) == 279, f"{p.closing_qty}/{p.closing_value}")
	check("CANCEL backdated receipt: current back to -5 / -75, frozen 15 (day-1 leg unwound)",
		flt(c.closing_qty) == -5 and flt(c.closing_value, 2) == -75 and flt(c.frozen_map) == 15,
		f"{c.closing_qty}/{c.closing_value} frozen {c.frozen_map}")
	check("CANCEL backdated receipt: PRD and inventory net to zero across original + cancellation",
		gl_net(bd.name, prd) + gl_net(cx.name, prd) == 0 and gl_net(bd.name, stock) + gl_net(cx.name, stock) == 0,
		f"prd {gl_net(bd.name, prd)}+{gl_net(cx.name, prd)} stock {gl_net(bd.name, stock)}+{gl_net(cx.name, stock)}")

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

	# ---------------- reversing a backdated landed cost undoes the carry too (MH #14)
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	it = make_item("_MR-LCVREV")
	pr = make_pr(it, wh, 100, 10, posting_date=str(prior.replace(day=5)))   # prior 100 / 1000, carried
	exp_acct = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0, "root_type": "Expense"},
		limit=1, pluck="name")[0]
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": str(prior.replace(day=7)), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name,
			"supplier": "_SMK Supplier", "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp_acct, "description": "freight", "amount": 200}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	c = ipb_period(it, cy, cm)
	check("LCV into the previous month carries +200 into the current month (1,200)",
		flt(c.carryover_value, 2) == 1200 and flt(c.closing_value, 2) == 1200, f"{c.carryover_value}/{c.closing_value}")
	cx = frappe.get_doc("Landed Cost Voucher", make_cancellation("Landed Cost Voucher", lcv.name))
	cx.submit()
	p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
	sle_val = flt(frappe.db.sql("SELECT COALESCE(SUM(stock_value_difference),0) FROM `tabStock Ledger Entry` WHERE item_code=%s AND is_cancelled=0", it)[0][0], 2)
	evs = frappe.get_all("Inventory Valuation Event", filters={"item_code": it, "is_cancelled": 0},
		fields=["reason_code", "period_month", "value_delta", "source_docname"], order_by="creation")
	# the cancellation is dated today: the prior month's history stays (1,200) and
	# the current month takes the -200 as a revaluation against its 1,200 carry
	check("current-dated LCV reversal: prior month untouched (1,200), current 1,200 carry - 200 = 1,000, stock ledger 1,000",
		flt(p.closing_value, 2) == 1200 and flt(c.carryover_value, 2) == 1200 and flt(c.reval_value, 2) == -200
		and flt(c.closing_value, 2) == 1000 and sle_val == 1000,
		f"prior {p.closing_value} cur carry {c.carryover_value} reval {c.reval_value} adjust {c.adjust_value} closing {c.closing_value} sle {sle_val} events {[(e.reason_code, e.period_month, flt(e.value_delta)) for e in evs]}")

	# ================ DR-44: reversal value floor (cancel after consumption)
	# a) the client's Cancel Netting Demo replayed: exact mirror would strand
	#    -65,625 at zero qty; the floor caps inventory at the covered value
	#    and credits PRD with the shortfall
	it = make_item("_MR-DR44A")
	stock44 = get_inventory_account(COMPANY, it, wh)
	prd44 = get_offset_account(COMPANY, it, wh, "prd")
	pr1 = make_pr(it, wh, 1500, 1175)
	make_pr(it, wh, 500, 1000)                    # MAP 1131.25
	make_dn(it, wh, 500)                          # -565,625 at blended MAP
	cx = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr1.name))
	cx.submit()
	c = ipb(it)
	check("DR-44a cancel-after-issue lands on 0 qty / 0 value (no stranded -65,625)",
		flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0,
		f"{c.closing_qty}/{c.closing_value}")
	ev = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cx.name, "reason_code": "cancellation"},
		fields=["value_delta", "prd_amount"])[0]
	check("DR-44a IVE: inventory leg floored to -1,696,875, PRD -65,625",
		flt(ev.value_delta, 2) == -1696875 and flt(ev.prd_amount, 2) == -65625,
		f"{ev.value_delta}/{ev.prd_amount}")
	check("DR-44a GL: Cr Inventory 1,696,875 / Cr PRD 65,625",
		gl_net(cx.name, stock44) == -1696875 and gl_net(cx.name, prd44) == -65625,
		f"stock {gl_net(cx.name, stock44)} prd {gl_net(cx.name, prd44)}")
	sle44 = flt(frappe.db.sql("SELECT COALESCE(SUM(stock_value_difference),0) FROM `tabStock Ledger Entry` WHERE item_code=%s AND is_cancelled=0", it)[0][0], 2)
	check("DR-44a stock ledger agrees (0)", sle44 == 0, str(sle44))

	# b) floor can trigger with quantity still on hand: the leftover unit
	#    carries zero value, never negative
	it = make_item("_MR-DR44B")
	pr1 = make_pr(it, wh, 15, 10)
	make_pr(it, wh, 5, 4)                         # 20 qty / 170, MAP 8.5
	make_dn(it, wh, 4)                            # 16 qty / 136
	cx = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr1.name))
	cx.submit()
	c = ipb(it)
	ev = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cx.name, "reason_code": "cancellation"},
		fields=["value_delta", "prd_amount"])[0]
	check("DR-44b floor at positive qty: 1 unit left at 0 value, PRD -14",
		flt(c.closing_qty) == 1 and flt(c.closing_value, 2) == 0
		and flt(ev.value_delta, 2) == -136 and flt(ev.prd_amount, 2) == -14,
		f"{c.closing_qty}/{c.closing_value} ev {ev.value_delta}/{ev.prd_amount}")

	# c) an unconsumed cancellation stays exact - no PRD leg
	it = make_item("_MR-DR44C")
	prd44c = get_offset_account(COMPANY, it, wh, "prd")
	pr1 = make_pr(it, wh, 10, 10)
	cx = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr1.name))
	cx.submit()
	c = ipb(it)
	ev = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cx.name, "reason_code": "cancellation"},
		fields=["value_delta", "prd_amount"])[0]
	check("DR-44c unconsumed cancel is exact: 0/0, value -100, no PRD",
		flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0
		and flt(ev.value_delta, 2) == -100 and flt(ev.prd_amount, 2) == 0
		and gl_net(cx.name, prd44c) == 0,
		f"{c.closing_qty}/{c.closing_value} ev {ev.value_delta}/{ev.prd_amount}")

	# d) LCV reversal floors the same way
	it = make_item("_MR-DR44D")
	stock44d = get_inventory_account(COMPANY, it, wh)
	prd44d = get_offset_account(COMPANY, it, wh, "prd")
	pr = make_pr(it, wh, 10, 10)
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": nowdate(), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name,
			"supplier": "_SMK Supplier", "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp_acct, "description": "freight", "amount": 50}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()                                   # 10 qty / 150, MAP 15
	make_dn(it, wh, 9)                             # 1 qty / 15
	cxl = frappe.get_doc("Landed Cost Voucher", make_cancellation("Landed Cost Voucher", lcv.name))
	cxl.submit()
	c = ipb(it)
	ev = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cxl.name, "reason_code": "cancellation"},
		fields=["value_delta", "prd_amount"])[0]
	check("DR-44d LCV reversal floored: 1 qty / 0 value, inventory -15, PRD -35",
		flt(c.closing_qty) == 1 and flt(c.closing_value, 2) == 0
		and flt(ev.value_delta, 2) == -15 and flt(ev.prd_amount, 2) == -35
		and gl_net(cxl.name, stock44d) == -15 and gl_net(cxl.name, prd44d) == -35,
		f"{c.closing_qty}/{c.closing_value} ev {ev.value_delta}/{ev.prd_amount} "
		f"gl stock {gl_net(cxl.name, stock44d)} prd {gl_net(cxl.name, prd44d)}")

	# e) PI debit-note reversal floors the same way
	it = make_item("_MR-DR44E")
	stock44e = get_inventory_account(COMPANY, it, wh)
	prd44e = get_offset_account(COMPANY, it, wh, "prd")
	pr = make_pr(it, wh, 10, 10)
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": COMPANY,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": it, "qty": 10, "rate": 15, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()                                    # +50 diff, 10 qty / 150
	make_dn(it, wh, 9)                             # 1 qty / 15
	cxp = frappe.get_doc("Purchase Invoice", make_cancellation("Purchase Invoice", pi.name))
	cxp.submit()
	c = ipb(it)
	ev = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cxp.name, "reason_code": "cancellation"},
		fields=["value_delta", "prd_amount"])[0]
	check("DR-44e PI reversal floored: 1 qty / 0 value, inventory -15, PRD -35",
		flt(c.closing_qty) == 1 and flt(c.closing_value, 2) == 0
		and flt(ev.value_delta, 2) == -15 and flt(ev.prd_amount, 2) == -35
		and gl_net(cxp.name, stock44e) == -15 and gl_net(cxp.name, prd44e) == -35,
		f"{c.closing_qty}/{c.closing_value} ev {ev.value_delta}/{ev.prd_amount} "
		f"gl stock {gl_net(cxp.name, stock44e)} prd {gl_net(cxp.name, prd44e)}")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
