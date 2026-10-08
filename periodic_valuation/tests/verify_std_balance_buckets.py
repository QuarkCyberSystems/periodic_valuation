"""Standard-cost period balance buckets follow the transaction, and the
opening stays fixed (client tickets STD-013 reopened and STD-015,
08/10/2026; DR-31 as MAP). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_balance_buckets.run

  1  a purchase return reduces Receipts (100 in, 20 returned -> Receipts 80,
     Issues 0)
  2  a sales return reduces Issues (30 out, 10 back -> Issues 20)
  3  cancelling a delivery reduces Issues; cancelling a receipt reduces
     Receipts
  4  a backdated receipt into the previous month: the current month's
     Opening is unchanged, Carryover takes it, Closing moves (MTD and YTD)
  5  the repair patch rebuilds rows written the old way (buckets by sign,
     backdated postings in Opening) without changing any Closing
  6  the YTD section reads the netted buckets

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
F = ["opening_qty", "carryover_qty", "receipt_qty", "receipt_value", "issue_qty", "issue_value",
	"closing_qty", "closing_value", "ytd_receipt_qty", "ytd_issue_qty"]


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
		raise Exception("STD balance bucket failures: " + "; ".join(c[0] for c in failed))


def _row(item, d):
	d = getdate(d)
	r = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["name", "creation"] + F)
	return frappe._dict({k: (flt(v, 6) if k in F else v) for k, v in r[0].items()}) if r else None


def _return(doctype, name, qty):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	ret = make_return_doc(doctype, name)
	ret.items[0].qty = -qty
	ret.insert(ignore_permissions=True)
	ret.submit()
	return ret


def _cancel(doc):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cx = frappe.get_doc(doc.doctype, make_cancellation(doc.doctype, doc.name))
	cx.posting_date, cx.posting_time = nowdate(), "23:00:00"
	cx.save()
	cx.submit()
	return cx


def _run():
	today = getdate(nowdate())
	if today.day < 3:
		print("SKIP: needs two days of the current month before today")
		return
	day1 = get_first_day(today)
	o2 = add_days(day1, 1)
	prev = add_months(day1, -1)
	frappe.db.savepoint("std_buckets")
	try:
		wh, _ = pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)

		# ---- 1-3 ----------------------------------------------------------------
		it = pack.std_item("_STD-BUCKETS")
		pack.scv_release(it, today.year, today.month, 10)
		rec = pack.make_pr(it, wh, 100, 10, posting_date=str(day1))
		_return("Purchase Receipt", rec.name, 20)
		r = _row(it, today)
		check("1: a purchase return reduces Receipts (100 - 20 = 80), Issues stay 0",
			(r.receipt_qty, r.receipt_value, r.issue_qty, r.closing_qty) == (80, 800, 0, 80), str(r))
		dn = pack.make_dn(it, wh, 30, posting_date=str(o2))
		_return("Delivery Note", dn.name, 10)
		r = _row(it, today)
		check("2: a sales return reduces Issues (30 - 10 = 20)",
			(r.issue_qty, r.issue_value, r.receipt_qty, r.closing_qty) == (20, 200, 80, 60), str(r))
		dn2 = pack.make_dn(it, wh, 5, posting_date=str(o2))
		_cancel(dn2)
		r = _row(it, today)
		check("3: cancelling a delivery reduces Issues (back to 20), Receipts untouched",
			(r.issue_qty, r.receipt_qty, r.closing_qty) == (20, 80, 60), str(r))
		rec2 = pack.make_pr(it, wh, 7, 10, posting_date=str(o2))
		_cancel(rec2)
		r = _row(it, today)
		check("3: cancelling a receipt reduces Receipts (back to 80), Issues untouched",
			(r.receipt_qty, r.issue_qty, r.closing_qty) == (80, 20, 60), str(r))

		# ---- 4: backdated receipt -> carryover -------------------------------------
		for view in ("MTD", "YTD"):
			b = pack.std_item(f"_STD-CARRY-{view}", view=view)
			pack.scv_release(b, prev.year, prev.month, 10)
			pack.make_pr(b, wh, 50, 10, posting_date=str(add_days(prev, 2)))
			pack.make_pr(b, wh, 5, 10, posting_date=str(o2))     # creates the current month's row
			before = _row(b, today)
			pack.make_pr(b, wh, 12, 10, posting_date=str(add_days(prev, 5)))
			after = _row(b, today)
			check(f"4 {view}: a backdated receipt leaves the current Opening fixed and adds to Carryover",
				after.opening_qty == before.opening_qty == 50 and after.carryover_qty == 12
				and after.closing_qty == 67, f"before {before} after {after}")

		# ---- 6: YTD section reads the netted buckets --------------------------------
		y = pack.std_item("_STD-BUCKETS-YTD", view="YTD")
		pack.scv_release(y, today.year, today.month, 10)
		yr = pack.make_pr(y, wh, 40, 10, posting_date=str(day1))
		_return("Purchase Receipt", yr.name, 15)
		r = _row(y, today)
		check("6: YTD section - Receipts 25 after the return, Issues 0",
			(r.ytd_receipt_qty, r.ytd_issue_qty) == (25, 0), str(r))

		# ---- 5: the repair patch ---------------------------------------------------
		from periodic_valuation.patches.v1_0 import std_balance_buckets_and_carryover as patch

		good = {n: _row(n, today) for n in (it, "_STD-CARRY-MTD", "_STD-CARRY-YTD")}
		# write them back the old way: returns/cancellations by sign, carry in opening
		frappe.db.set_value("Inventory Period Balance", good[it].name, {
			"receipt_qty": 122, "receipt_value": 1220, "issue_qty": 62, "issue_value": 620}, update_modified=False)
		for n in ("_STD-CARRY-MTD", "_STD-CARRY-YTD"):
			frappe.db.set_value("Inventory Period Balance", good[n].name, {
				"opening_qty": 62, "opening_value": 620, "carryover_qty": 0, "carryover_value": 0},
				update_modified=False)
		patch.execute()
		rebuilt = {n: _row(n, today) for n in good}
		keys = ("opening_qty", "carryover_qty", "receipt_qty", "issue_qty", "closing_qty", "closing_value")
		check("5: the patch rebuilds buckets and carryover exactly; no closing changes",
			all(all(rebuilt[n][k] == good[n][k] for k in keys) for n in good),
			f"{ {n: {k: rebuilt[n][k] for k in keys} for n in good} }")
	finally:
		frappe.db.rollback(save_point="std_buckets")
