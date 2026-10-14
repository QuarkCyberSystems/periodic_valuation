"""A posting into the previous open month reaches the current month's balance
even when the item has no row there yet (client ticket STD-015, 13/10/2026,
MH S44: "why the October not opened"; client design V2-1 section 4.3). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_later_month_rows.run

  1  a new item received only in the previous month: the current month's
     balance exists - Opening 0, Carryover 100, Closing 100 at standard
  2  the same when the two months' standards differ: the current month
     closes at its own standard, the bridge counted once (Opening 0,
     Carryover 100)
  3  the patch opens the missing row for an item left without one

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
		raise Exception("STD later-month row failures: " + "; ".join(c[0] for c in failed))


def _row(item, d):
	d = getdate(d)
	r = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month},
		fields=["name", "opening_qty", "carryover_qty", "closing_qty", "closing_value"])
	return (flt(r[0].opening_qty), flt(r[0].carryover_qty), flt(r[0].closing_qty), flt(r[0].closing_value, 2)) if r else None


def _run():
	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	frappe.db.savepoint("std_later_rows")
	try:
		wh, _ = pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur = pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_value("Inventory Period", cur, "creation", f"{day1} 00:00:01", update_modified=False)

		a = pack.std_item("_STD-LATER-ROW")
		pack.scv_release(a, prev.year, prev.month, 400)
		pack.make_pr(a, wh, 100, 400, posting_date=str(add_days(prev, 1)))
		check("1: the current month's balance exists - Opening 0, Carryover 100, Closing 100 x 400",
			_row(a, today) == (0.0, 100.0, 100.0, 40000.0), str(_row(a, today)))

		b = pack.std_item("_STD-LATER-ROW-DIFF")
		pack.scv_release(b, prev.year, prev.month, 400)
		frappe.db.delete("Inventory Period Balance", {"item_code": b})
		pack.scv_release(b, today.year, today.month, 500)
		frappe.db.delete("Inventory Period Balance", {"item_code": b, "period_month": today.month})
		pack.make_pr(b, wh, 100, 400, posting_date=str(add_days(prev, 1)))
		check("2: standards differ - the current month closes 100 x 500, Opening 0, Carryover 100, counted once",
			_row(b, today) == (0.0, 100.0, 100.0, 50000.0) and _row(b, prev) == (0.0, 0.0, 100.0, 40000.0),
			f"current {_row(b, today)} previous {_row(b, prev)}")

		frappe.db.delete("Inventory Period Balance", {"item_code": a, "period_month": today.month,
			"period_year": today.year})
		from periodic_valuation.patches.v1_0 import open_later_std_rows

		open_later_std_rows.execute()
		check("3: the patch opens the missing row with the same figures",
			_row(a, today) == (0.0, 100.0, 100.0, 40000.0), str(_row(a, today)))
	finally:
		frappe.db.rollback(save_point="std_later_rows")
