"""Months settle in order: an item's month cannot be settled while the month
before is still open and unsettled for it (client ticket STD-016,
08/10/2026: "Settlement was executed for October 2026, but September 2026
were not settled"). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_settle_in_order.run

  1  MTD and YTD: with stock in the previous month, settling the current
     month is refused, naming the previous month
  2  once the previous month is settled, the current month settles
  3  an item with nothing to settle in the previous month settles the
     current month at once
  4  a Settlement Run for the current month settles the items that can be
     settled and lists the others in Remarks

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, get_first_day, getdate, nowdate

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
		raise Exception("STD settle-in-order failures: " + "; ".join(c[0] for c in failed))


def _settle(item, d, period):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	return StdEngine(pack.COMPANY, item).close_period(year=d.year, month=d.month, sc=10,
		source=("Inventory Period", period))


def _refusal(fn):
	frappe.db.savepoint("sio_refusal")
	try:
		fn()
		return ""
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="sio_refusal")


def _run():
	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	frappe.db.savepoint("std_settle_order")
	try:
		wh, _ = pack.ensure_company()
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cp = pack.make_period(today.year, today.month, "OPEN")
		if prev.year != today.year:
			print("SKIP YTD: the previous month is in the prior fiscal year")
		for view in ("MTD", "YTD"):
			if view == "YTD" and prev.year != today.year:
				continue
			it = pack.std_item(f"_STD-SIO-{view}", view=view)
			pack.scv_release(it, prev.year, prev.month, 10)
			pack.make_pr(it, wh, 100, 12, posting_date=str(add_days(prev, 2)))
			pack.make_pr(it, wh, 10, 11, posting_date=str(day1))
			msg = _refusal(lambda: _settle(it, today, cp))
			check(f"1 {view}: settling the current month is refused while the previous month is unsettled",
				f"settle {prev.year}-{prev.month:02d} first" in msg, msg)
			_settle(it, prev, pp)
			check(f"2 {view}: after the previous month settles, the current month settles",
				not _refusal(lambda: _settle(it, today, cp)))

		quiet = pack.std_item("_STD-SIO-NEW")
		pack.scv_release(quiet, today.year, today.month, 10)
		pack.make_pr(quiet, wh, 5, 12, posting_date=str(day1))
		check("3: an item with nothing in the previous month settles the current month at once",
			not _refusal(lambda: _settle(quiet, today, cp)))

		late = pack.std_item("_STD-SIO-RUN-LATE")
		pack.scv_release(late, prev.year, prev.month, 10)
		pack.make_pr(late, wh, 40, 12, posting_date=str(add_days(prev, 2)))
		ready = pack.std_item("_STD-SIO-RUN-READY")
		pack.scv_release(ready, today.year, today.month, 10)
		pack.make_pr(ready, wh, 7, 12, posting_date=str(day1))
		run_doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": pack.COMPANY,
			"period_year": today.year, "period_month": today.month, "run_type": "INITIAL_CLOSE"})
		run_doc.insert(ignore_permissions=True)
		run_doc.submit()
		run_doc.reload()
		settled = lambda i: frappe.db.exists("Inventory Period Settlement", {"item_code": i, "cancelled": 0,
			"period_year": today.year, "period_month": today.month})
		check("4: the run settles the ready item and lists the late one in Remarks",
			settled(ready) and not settled(late) and late in (run_doc.remarks or "")
			and "first" in (run_doc.remarks or ""), run_doc.remarks)
	finally:
		frappe.db.rollback(save_point="std_settle_order")
