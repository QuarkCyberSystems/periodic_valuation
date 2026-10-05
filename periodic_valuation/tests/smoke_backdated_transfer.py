"""Backdated warehouse transfer - bench --site <site> execute periodic_valuation.tests.smoke_backdated_transfer.run

MAP-Warehouse-001: a warehouse-scope transfer dated in the previous open month
books in that month and carries both legs into the open month, so each
warehouse's opening + carryover equals its previous closing, and the Bin shows
the open month's balance. Scenario A is the client's case (10 in at 500,
5 moved in the open month, then 2 out and 1 back dated in the previous month);
scenario B moves stock at the previous month's MAP while the open month already
holds stock at another price. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate

from periodic_valuation.shared.period_close import assert_continuity
from periodic_valuation.tests.smoke_edges import check, CHECKS, ipb_period, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters


def transfer(item, qty, src, dst, posting_date):
	se = frappe.get_doc({
		"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Transfer",
		"posting_date": posting_date, "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "s_warehouse": src, "t_warehouse": dst}],
	})
	se.insert(ignore_permissions=True)
	se.submit()
	return se


def qv(row):
	return (flt(row.closing_qty), flt(row.closing_value, 2)) if row else None


def carry(row):
	return (flt(row.opening_qty) + flt(row.carryover_qty),
		flt(flt(row.opening_value) + flt(row.carryover_value), 2)) if row else None


def bin_of(item, wh):
	b = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh},
		["actual_qty", "stock_value"], as_dict=True)
	return (flt(b.actual_qty), flt(b.stock_value, 2)) if b else None


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
	transfer(it, 2, wh, wh2, str(add_days(prior, 9)))
	transfer(it, 1, wh2, wh, str(add_days(prior, 11)))

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

	# ============ the period-close carry check passes for the open month
	open_period = frappe.get_doc("Inventory Period", {"company": COMPANY, "period_year": cy, "period_month": cm})
	cont = assert_continuity(open_period)
	bad = [x for x in cont["detail"] if x.startswith("_SMK-BTRF")]
	check("continuity gate: no transfer item breaks the carry chain", not bad, str(bad))

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("backdated transfer smoke failures: " + "; ".join(x[0] for x in failed))
