"""Backdated warehouse transfer - bench --site <site> execute periodic_valuation.tests.smoke_backdated_transfer.run

MAP-Warehouse-001: a warehouse-scope transfer dated in the previous open month
books in that month and carries both legs into the open month, so each
warehouse's opening + carryover equals its previous closing, and the Bin shows
the open month's balance. Scenario A is the client's case (10 in at 500,
5 moved in the open month, then 2 out and 1 back dated in the previous month);
scenario B moves stock at the previous month's MAP while the open month already
holds stock at another price; scenario C lands the source's open month below
zero, so the carry takes its day-1 price-difference leg; scenario D cancels
backdated transfers in both directions (the destination warehouse sorting
before and after the source) and the C transfer with its day-1 leg;
scenario F moves stock into a NEGATIVE destination at a price other than its
frozen MAP: the in-leg posts its price difference like any receipt into
negative stock (DR-60), so GL inventory equals stock value, on one shared
inventory account and on two, current and backdated, and after cancellation.
Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate

from periodic_valuation.shared.period_close import assert_continuity
from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
from periodic_valuation.tests.smoke_edges import (
	CHECKS, check, ipb_period, make_dn, make_item, make_pr, make_transfer as transfer,
)
from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters


def cancel(se):
	name = make_cancellation("Stock Entry", se.name)
	frappe.get_doc("Stock Entry", name).submit()
	return name


def qv(row):
	return (flt(row.closing_qty), flt(row.closing_value, 2)) if row else None


def carry(row):
	return (flt(row.opening_qty) + flt(row.carryover_qty),
		flt(flt(row.opening_value) + flt(row.carryover_value), 2)) if row else None


def bin_of(item, wh):
	b = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh},
		["actual_qty", "stock_value"], as_dict=True)
	return (flt(b.actual_qty), flt(b.stock_value, 2)) if b else None


def gl_vs_stock(item, accounts):
	"""(stock value from the ledger, GL on the inventory accounts) for an item."""
	sles = frappe.get_all("Stock Ledger Entry", filters={"item_code": item, "is_cancelled": 0},
		fields=["voucher_no", "stock_value_difference"])
	vouchers = list({x.voucher_no for x in sles})
	gl = frappe.get_all("GL Entry", filters={"voucher_no": ["in", vouchers], "is_cancelled": 0,
		"account": ["in", list(accounts)]}, fields=["debit", "credit"])
	return (flt(sum(flt(x.stock_value_difference) for x in sles), 2),
		flt(sum(flt(x.debit) - flt(x.credit) for x in gl), 2))


def gl_on(voucher, account):
	return flt(sum(flt(x.debit) - flt(x.credit) for x in frappe.get_all("GL Entry",
		filters={"voucher_no": voucher, "account": account, "is_cancelled": 0}, fields=["debit", "credit"])), 2)


def run(commit=False):
	del CHECKS[:]
	wh = ensure_masters()
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	wh2 = f"_SMK Stores 2 - {abbr}"
	if not frappe.db.exists("Warehouse", wh2):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_SMK Stores 2",
			"company": COMPANY}).insert(ignore_permissions=True)
	prior = get_first_day(add_months(nowdate(), -1))
	cur = get_first_day(nowdate())
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	py, pm, cy, cm = prior.year, prior.month, cur.year, cur.month

	# ============ A: the client's case, same quantities and price
	it = make_item("_SMK-BTRF-A", include_warehouse=1)
	make_pr(it, wh, 10, 500, posting_date=str(add_days(prior, 7)))
	transfer(it, 5, wh, wh2, nowdate())
	a_out = transfer(it, 2, wh, wh2, str(add_days(prior, 9)))
	a_back = transfer(it, 1, wh2, wh, str(add_days(prior, 11)))
	item_a = it

	p1, p2 = ipb_period(it, py, pm, wh), ipb_period(it, py, pm, wh2)
	c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
	check("A previous month: source 9/4500, destination 1/500",
		qv(p1) == (9, 4500) and qv(p2) == (1, 500), f"{qv(p1)} {qv(p2)}")
	check("A open month carries the previous closing per warehouse",
		carry(c1) == qv(p1) and carry(c2) == qv(p2), f"{carry(c1)} {carry(c2)}")
	check("A open month: source 4/2000, destination 6/3000",
		qv(c1) == (4, 2000) and qv(c2) == (6, 3000), f"{qv(c1)} {qv(c2)}")
	check("A Bin shows the open month's balance",
		bin_of(it, wh) == (4, 2000) and bin_of(it, wh2) == (6, 3000), f"{bin_of(it, wh)} {bin_of(it, wh2)}")

	# ============ B: open month at another price; the move is at the previous MAP
	it = make_item("_SMK-BTRF-B", include_warehouse=1)
	make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
	make_pr(it, wh, 10, 200)                                   # open: 20/3000, MAP 150
	transfer(it, 4, wh, wh2, str(add_days(prior, 5)))          # at previous MAP 100
	p1, p2 = ipb_period(it, py, pm, wh), ipb_period(it, py, pm, wh2)
	c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
	check("B previous month: source 6/600, destination 4/400",
		qv(p1) == (6, 600) and qv(p2) == (4, 400), f"{qv(p1)} {qv(p2)}")
	check("B open month: source 16/2600 MAP 162.5, destination 4/400 MAP 100",
		qv(c1) == (16, 2600) and flt(c1.moving_avg_price, 4) == 162.5
		and qv(c2) == (4, 400) and flt(c2.moving_avg_price, 4) == 100,
		f"{qv(c1)} {c1.moving_avg_price} {qv(c2)} {c2.moving_avg_price}")
	check("B Bin shows the open month's balance",
		bin_of(it, wh) == (16, 2600) and bin_of(it, wh2) == (4, 400), f"{bin_of(it, wh)} {bin_of(it, wh2)}")

	# ============ C: the carry takes the source's open month below zero
	it = make_item("_SMK-BTRF-C", include_warehouse=1)
	make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
	make_pr(it, wh, 10, 200)                                   # open: 20/3000, MAP 150
	make_dn(it, wh, 20)                                        # open: 0/0, MAP kept 150
	c_trf = transfer(it, 4, wh, wh2, str(add_days(prior, 5)))  # at previous MAP 100
	p1 = ipb_period(it, py, pm, wh)
	c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
	check("C previous month: source 6/600", qv(p1) == (6, 600), str(qv(p1)))
	check("C open month: source -4 re-priced at frozen MAP 150 (-600), destination 4/400",
		qv(c1) == (-4, -600) and flt(c1.frozen_map) == 150 and qv(c2) == (4, 400),
		f"{qv(c1)} frozen {c1.frozen_map} {qv(c2)}")
	out_ive = frappe.db.get_value("Inventory Valuation Event", {"source_docname": c_trf.name,
		"warehouse": wh, "reason_code": "transfer"}, "name")
	day1 = frappe.get_all("Inventory Valuation Event", filters={"caused_by_event_id": out_ive,
		"reason_code": "prd_split"}, fields=["value_delta", "posting_date"])
	check("C day-1 price-difference leg of -200 on the 1st of the open month",
		len(day1) == 1 and flt(day1[0].value_delta, 2) == -200 and str(day1[0].posting_date) == str(cur),
		str(day1))

	# ============ D: cancel backdated transfers in the open month, both directions
	cancel(a_out)                                              # source sorts before destination
	cancel(a_back)                                             # destination sorts before source
	c1, c2 = ipb_period(item_a, cy, cm, wh), ipb_period(item_a, cy, cm, wh2)
	check("D A after both cancellations: 5/2500 in each warehouse",
		qv(c1) == (5, 2500) and qv(c2) == (5, 2500), f"{qv(c1)} {qv(c2)}")
	check("D A Bin follows", bin_of(item_a, wh) == (5, 2500) and bin_of(item_a, wh2) == (5, 2500),
		f"{bin_of(item_a, wh)} {bin_of(item_a, wh2)}")
	cancel(c_trf)
	c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
	check("D C cancellation unwinds the move and its day-1 leg: both warehouses 0/0",
		qv(c1) == (0, 0) and qv(c2) == (0, 0), f"{qv(c1)} {qv(c2)}")

	# ============ the period-close carry check passes for the open month
	open_period = frappe.get_doc("Inventory Period", {"company": COMPANY, "period_year": cy, "period_month": cm})
	cont = assert_continuity(open_period)
	bad = [x for x in cont["detail"] if x.startswith("_SMK-BTRF")]
	check("continuity gate: no transfer item breaks the carry chain", not bad, str(bad))

	# ============ E: a month's missing row seeds from the closing before it,
	# even when a LATER month's row already exists (MH #46 on UAT: August 2,
	# October 2, September loaded at 0)
	from periodic_valuation.periodic_moving_average.kernel import ScopeState
	from periodic_valuation.shared.immutable import KERNEL_FLAG

	it_e = make_item("_SMK-BTRF-E", include_warehouse=1)
	py, pm = (cy - 1, 12) if cm == 1 else (cy, cm - 1)
	ey, em = (py - 1, 12) if pm == 1 else (py, pm - 1)
	frappe.flags[KERNEL_FLAG] = True
	try:
		for (y, m, q) in ((ey, em, 2), (cy, cm, 2)):
			frappe.get_doc({"doctype": "Inventory Period Balance", "company": COMPANY, "item_code": it_e,
				"warehouse": wh2, "period_year": y, "period_month": m, "opening_qty": q,
				"opening_value": 7, "closing_qty": q, "closing_value": 7, "moving_avg_price": 3.5,
				}).insert(ignore_permissions=True)
	finally:
		frappe.flags[KERNEL_FLAG] = False
	seeded = ScopeState(COMPANY, it_e, wh2).load(frappe._dict(period_year=py, period_month=pm))
	check("E a missing previous-month row seeds from the month before it (2 / 7), not zero",
		(flt(seeded.opening_qty), flt(seeded.opening_value, 2)) == (2, 7)
		and flt(seeded.moving_avg_price) == 3.5, f"{seeded.opening_qty} {seeded.opening_value}")


	# ============ F: transfer into a NEGATIVE destination (DR-60 on the in-leg)
	from periodic_valuation.shared.accounts import get_inventory_account, get_offset_account

	py, pm = prior.year, prior.month
	abbr_inv = get_inventory_account(COMPANY, make_item("_SMK-BTRF-F1", include_warehouse=1), wh)
	if not frappe.db.exists("Account", f"_SMK Stores 2 Inventory - {abbr}"):
		frappe.get_doc({"doctype": "Account", "account_name": "_SMK Stores 2 Inventory", "company": COMPANY,
			"parent_account": frappe.db.get_value("Account", abbr_inv, "parent_account"),
			"account_type": "Stock"}).insert(ignore_permissions=True)
	split_acct = f"_SMK Stores 2 Inventory - {abbr}"
	for code, split in (("_SMK-BTRF-F1", False), ("_SMK-BTRF-F2", True)):
		it = make_item(code, include_warehouse=1)
		if split:
			doc = frappe.get_doc("Item", it)
			doc.append("item_default_warehouse_accounts", {"company": COMPANY, "warehouse": wh2,
				"default_inventory_account": split_acct})
			doc.save(ignore_permissions=True)
		a1, a2 = get_inventory_account(COMPANY, it, wh), get_inventory_account(COMPANY, it, wh2)
		prd = get_offset_account(COMPANY, it, wh2, "prd")
		label = "two accounts" if split else "one account"
		make_pr(it, wh, 10, 20)                                # W1 10 @ 20
		make_pr(it, wh2, 2, 30)                                # W2 2 @ 30
		make_dn(it, wh2, 7)                                    # W2 -5, frozen 30
		f_trf = transfer(it, 5, wh, wh2)                       # 5 @ 20 into frozen 30
		c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
		check(f"F {label}: source 5/100, destination lands on 0/0",
			qv(c1) == (5, 100) and qv(c2) == (0, 0), f"{qv(c1)} {qv(c2)}")
		check(f"F {label}: price difference 50 credits PRD on the transfer",
			gl_on(f_trf.name, prd) == -50, str(gl_on(f_trf.name, prd)))
		check(f"F {label}: inventory {'150 in / 100 out' if split else 'nets +50'}",
			(gl_on(f_trf.name, a2), gl_on(f_trf.name, a1)) == (150, -100) if split
			else gl_on(f_trf.name, a1) == 50,
			f"{gl_on(f_trf.name, a2)} {gl_on(f_trf.name, a1)}")
		sv, gv = gl_vs_stock(it, {a1, a2})
		check(f"F {label}: GL inventory equals stock value", sv == gv, f"stock {sv} GL {gv}")
		f_cx = cancel(f_trf)
		c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
		sv, gv = gl_vs_stock(it, {a1, a2})
		check(f"F {label}: cancellation restores 10/200 and -5/-150, PRD nets to 0, GL equals stock",
			qv(c1) == (10, 200) and qv(c2) == (-5, -150) and gl_on(f_cx, prd) == 50
			and sv == gv, f"{qv(c1)} {qv(c2)} stock {sv} GL {gv}")

	# backdated: the destination was negative in the previous month
	it = make_item("_SMK-BTRF-F3", include_warehouse=1)
	a1, a2 = get_inventory_account(COMPANY, it, wh), get_inventory_account(COMPANY, it, wh2)
	make_pr(it, wh, 10, 20, posting_date=str(add_days(prior, 2)))
	make_pr(it, wh2, 2, 30, posting_date=str(add_days(prior, 2)))
	make_dn(it, wh2, 7, posting_date=str(add_days(prior, 3)))  # W2 previous -5, frozen 30
	transfer(it, 5, wh, wh2, str(add_days(prior, 5)))
	p2 = ipb_period(it, py, pm, wh2)
	c1, c2 = ipb_period(it, cy, cm, wh), ipb_period(it, cy, cm, wh2)
	check("F backdated: destination previous month 0/0, open month carries 0/0, source 5/100",
		qv(p2) == (0, 0) and qv(c2) == (0, 0) and qv(c1) == (5, 100), f"{qv(p2)} {qv(c2)} {qv(c1)}")
	sv, gv = gl_vs_stock(it, {a1, a2})
	check("F backdated: GL inventory equals stock value", sv == gv, f"stock {sv} GL {gv}")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("backdated transfer smoke failures: " + "; ".join(x[0] for x in failed))
