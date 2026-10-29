"""Backdated issue cancellation - bench --site <site> execute periodic_valuation.tests.smoke_backdated_issue_cancellation.run

DR-75 (Vivek, 29/10/2026; TSK-2026-01005): the Cancellation of a Material
Issue may be dated in the previous open month, on or after the issue - so a
capitalization or repair reversed in that month gets its stock back as the
issue's reversal. Scenario E: an issue of the previous month cancelled there
- the previous month nets its Out bucket, the event is a cancellation of the
issue's, the open month re-derives and GL stays equal to stock; F: a
cancellation dated before the issue is refused; G: a receipt's Cancellation
still posts only in the current month (DR-71); H: an issue that was itself
backdated (a day-1 leg in the open month) is cancelled only in the open
month. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate

from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
from periodic_valuation.shared.accounts import get_inventory_account
from periodic_valuation.tests.smoke_backdated_transfer import carry, gl_vs_stock, qv
from periodic_valuation.tests.smoke_edges import CHECKS, check, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters


def _issue(item, wh, qty, posting_date=None):
	doc = frappe.get_doc({
		"doctype": "Stock Entry", "stock_entry_type": "Material Issue", "company": COMPANY,
		"posting_date": posting_date or nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "s_warehouse": wh}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def _cancel(doctype, name, posting_date):
	doc = frappe.get_doc(doctype, make_cancellation(doctype, name))
	doc.posting_date = posting_date
	doc.set_posting_time = 1
	doc.save(ignore_permissions=True)
	doc.submit()
	return doc


def _refused(fn, text):
	"""A refused document leaves nothing behind - as a request's rollback does."""
	frappe.db.savepoint("bic_refused")
	try:
		fn()
		return False, "posted"
	except frappe.ValidationError as exc:
		frappe.db.rollback(save_point="bic_refused")
		return text in str(exc), str(exc)[:200]


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
		# ============ E: an issue of the previous month, cancelled there
		it = make_item("_SMK-BIC-E")
		make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		issue = _issue(it, wh, 4, str(add_days(prior, 4)))            # previous: 6/600
		make_pr(it, wh, 10, 200)                                       # open: 16/2600, MAP 162.5
		back = _cancel("Stock Entry", issue.name, str(add_days(prior, 6)))
		p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
		check("E previous month: the cancellation nets the issue there (10/1000, Out back to 0)",
			qv(p) == (10, 1000) and flt(p.issue_qty) == 0, f"{qv(p)} out {p.issue_qty}")
		check("E open month carries the previous closing: 20/3000, MAP re-derives to 150",
			carry(c) == qv(p) and qv(c) == (20, 3000) and flt(c.moving_avg_price, 4) == 150, f"{qv(c)} {c.moving_avg_price}")
		ive = frappe.db.get_value("Inventory Valuation Event", {"source_docname": back.name, "is_cancelled": 0},
			["posting_date", "reason_code", "reversal_of"], as_dict=True)
		issue_ive = frappe.db.get_value("Inventory Valuation Event", {"source_docname": issue.name, "is_cancelled": 0}, "name")
		check("E its event is a cancellation of the issue's, dated in the previous month",
			ive and ive.reason_code == "cancellation" and ive.reversal_of == issue_ive and str(ive.posting_date) < str(cur),
			str(ive))
		stock, gl = gl_vs_stock(it, {get_inventory_account(COMPANY, it, wh)})
		check("E GL inventory equals stock value", stock == gl == 3000, f"stock {stock} gl {gl}")

		# ============ F: dated before the issue
		it = make_item("_SMK-BIC-F")
		make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		issue = _issue(it, wh, 4, str(add_days(prior, 6)))
		ok, msg = _refused(lambda: _cancel("Stock Entry", issue.name, str(add_days(prior, 5))), "cannot be dated before")
		check("F a cancellation dated before the issue is refused (the Cancellation's own date rule)", ok, msg)

		# ============ G: a receipt's Cancellation is still never backdated
		it = make_item("_SMK-BIC-G")
		pr = make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		ok, msg = _refused(lambda: _cancel("Purchase Receipt", pr.name, str(add_days(prior, 5))),
			"Backdated cancellations into a previous period are not supported")
		check("G a receipt's backdated Cancellation is refused (DR-71)", ok, msg)

		# ============ H: an issue that was itself backdated into a negative open month
		it = make_item("_SMK-BIC-H")
		make_pr(it, wh, 10, 100, posting_date=str(add_days(prior, 3)))
		make_pr(it, wh, 10, 200)                                       # open: 20/3000, MAP 150
		make_dn(it, wh, 20)                                            # open: 0/0
		issue = _issue(it, wh, 4, str(add_days(prior, 5)))            # carried: open -4, day-1 leg
		day1 = frappe.db.count("Inventory Valuation Event", {"caused_by_event_id": frappe.db.get_value(
			"Inventory Valuation Event", {"source_docname": issue.name, "reason_code": "issue"}, "name"), "is_cancelled": 0})
		check("H the backdated issue left a day-1 leg in the open month", day1 >= 1, str(day1))
		ok, msg = _refused(lambda: _cancel("Stock Entry", issue.name, str(add_days(prior, 6))), "posts in the current period")
		check("H its backdated cancellation is refused - it posts in the current month", ok, msg)
		_cancel("Stock Entry", issue.name, nowdate())
		stock, gl = gl_vs_stock(it, {get_inventory_account(COMPANY, it, wh)})
		check("H cancelled in the open month instead: GL = stock", stock == gl, f"stock {stock} gl {gl}")
	finally:
		if commit:
			frappe.db.commit()
		else:
			frappe.db.rollback()
	failed = [label for label, ok in CHECKS if not ok]
	print(f"\nbackdated issue cancellation smoke: {len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
	if failed:
		raise AssertionError(f"backdated issue cancellation: {failed}")
