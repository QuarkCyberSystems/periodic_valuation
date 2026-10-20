"""Backdated returns - bench --site <site> execute periodic_valuation.tests.smoke_backdated_returns.run

DR-71 (client meeting 19/10/2026: "Backdated returns are permitted as long as
the relevant accounting period is not closed"; TSK-2026-00944): a return
dated in the previous open month books there as a return of that month is and
carries into the open month. Scenario A: a purchase return of a previous-month
receipt while the open month holds stock at another price - booked at the
previous cost, carried like a backdated issue, the open MAP re-derives;
scenario B: a sales return of a previous-month delivery, carried like a
backdated receipt; scenario C: the carry takes the open month below zero, so
it is re-priced at the frozen MAP with a day-1 price-difference leg (DR-46);
scenario D: the Cancellation document of a backdated return posts in the open
month and unwinds it with its day-1 leg; every scenario keeps GL inventory
equal to stock value. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate

from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
from periodic_valuation.shared.accounts import get_inventory_account
from periodic_valuation.tests.smoke_backdated_transfer import carry, gl_vs_stock, qv
from periodic_valuation.tests.smoke_edges import CHECKS, check, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters


def _return(doctype, source, qty, posting_date):
	if doctype == "Purchase Receipt":
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return as maker
	else:
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return as maker
	ret = maker(source.name)
	ret.posting_date = posting_date
	ret.set_posting_time = 1
	ret.items[0].qty = -qty
	ret.insert(ignore_permissions=True)
	ret.submit()
	return ret


def run(commit=False):
	del CHECKS[:]
	wh = ensure_masters()
	prior = get_first_day(add_months(nowdate(), -1))
	cur = get_first_day(nowdate())
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	py, pm, cy, cm = prior.year, prior.month, cur.year, cur.month
	try:
		# ============ A: purchase return of a previous-month receipt
		it = make_item("_SMK-BRET-A")
		pr = make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		make_pr(it, wh, 10, 200)                                    # open: 20/3000, MAP 150
		ret_a = _return("Purchase Receipt", pr, 4, str(add_days(prior, 5)))
		p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
		check("A previous month: the return books there at the receipt's cost (6/600)", qv(p) == (6, 600), str(qv(p)))
		check("A open month carries the previous closing", carry(c) == qv(p), f"{carry(c)} {qv(p)}")
		check("A open month: 16/2600, MAP re-derives to 162.5",
			qv(c) == (16, 2600) and flt(c.moving_avg_price, 4) == 162.5, f"{qv(c)} {c.moving_avg_price}")
		inv = get_inventory_account(COMPANY, it, wh)
		stock, gl = gl_vs_stock(it, {inv})
		check("A GL inventory equals stock value", stock == gl == 2600, f"stock {stock} gl {gl}")
		ive = frappe.db.get_value("Inventory Valuation Event", {"source_docname": ret_a.name}, ["posting_date", "reason_code"],
			as_dict=True)
		check("A the return's own event is dated in the previous month", ive and str(ive.posting_date) < str(cur), str(ive))

		# ============ B: sales return of a previous-month delivery
		it = make_item("_SMK-BRET-B")
		make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		dn = make_dn(it, wh, 5, posting_date=str(add_days(prior, 4)))  # previous: 5/500
		make_pr(it, wh, 10, 200)                                      # open: 15/2500
		_return("Delivery Note", dn, 2, str(add_days(prior, 6)))
		p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
		check("B previous month: the sales return books there at the issue cost (7/700)", qv(p) == (7, 700), str(qv(p)))
		check("B open month: 17/2700, carried like a backdated receipt", qv(c) == (17, 2700) and carry(c) == qv(p),
			f"{qv(c)} {carry(c)}")
		stock, gl = gl_vs_stock(it, {get_inventory_account(COMPANY, it, wh)})
		check("B GL inventory equals stock value", stock == gl == 2700, f"stock {stock} gl {gl}")

		# ============ C: the carry takes the open month below zero
		it = make_item("_SMK-BRET-C")
		pr = make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		make_pr(it, wh, 10, 200)                                    # open: 20/3000, MAP 150
		make_dn(it, wh, 20)                                         # open: 0/0, MAP kept 150
		ret_c = _return("Purchase Receipt", pr, 4, str(add_days(prior, 5)))
		p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
		check("C previous month: 6/600", qv(p) == (6, 600), str(qv(p)))
		check("C open month: -4 re-priced at the frozen MAP 150 (-600)",
			qv(c) == (-4, -600) and flt(c.frozen_map) == 150, f"{qv(c)} frozen {c.frozen_map}")
		own = frappe.db.get_value("Inventory Valuation Event", {"source_docname": ret_c.name,
			"reason_code": ("!=", "prd_split")}, "name")
		day1 = frappe.get_all("Inventory Valuation Event", filters={"caused_by_event_id": own, "reason_code": "prd_split"},
			fields=["value_delta", "posting_date"])
		check("C day-1 price-difference leg of -200 on the 1st of the open month",
			len(day1) == 1 and flt(day1[0].value_delta, 2) == -200 and str(day1[0].posting_date) == str(cur), str(day1))
		stock, gl = gl_vs_stock(it, {get_inventory_account(COMPANY, it, wh)})
		check("C GL inventory equals stock value", stock == gl == -600, f"stock {stock} gl {gl}")

		# ============ D: the Cancellation document of a backdated return
		for label, ret, it_code, back in (("A", ret_a, "_SMK-BRET-A", (20, 3000)), ("C", ret_c, "_SMK-BRET-C", (0, 0))):
			frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", ret.name)).submit()
			c = ipb_period(it_code, cy, cm)
			stock, gl = gl_vs_stock(it_code, {get_inventory_account(COMPANY, it_code, wh)})
			check(f"D {label} cancelled in the open month: back to {back}, GL = stock", qv(c) == back and stock == gl,
				f"{qv(c)} stock {stock} gl {gl}")
	finally:
		if commit:
			frappe.db.commit()
		else:
			frappe.db.rollback()
	failed = [label for label, ok in CHECKS if not ok]
	print(f"\nbackdated returns smoke: {len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
	if failed:
		raise AssertionError(f"backdated returns: {failed}")
