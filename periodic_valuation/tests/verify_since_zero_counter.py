"""Total Received Since Zero restarts when the quantity passes through zero on
its way down (DR-70; client tickets STD-022 / TSK-2026-00936 and MAP
TSK-2026-00981, ABC Item - MAP Test - MH #63). Run:
bench --site <site> execute periodic_valuation.tests.verify_since_zero_counter.run

MH #63's own movements, entered as she entered them - the current month
first, the previous month afterwards (backdated, carried into the current
month against its stock as it stands: the signed MAP rule, 31 Aug 2026):
  previous month: +100, -120, +10, +170, +5, -30   -> closing 135
  current month:  -650 (day 3), +500 (day 13)      -> closing -15
  1  previous month's counter 165 (the -120 went through zero; +10 while
     negative; +170 crossed back, excess 160; +5)
  2  current month's counter 0: the last movement carried in (-30) took it
     from 15 through zero, and nothing has been received since - not the 15
     the counter kept before
  3  entered in date order: the -650 takes 135 through zero, the +500 is
     received while negative - 500
  4  the DR-70 patch re-derives a negative row's counter (15 -> 0) and
     leaves a row at or above zero as it is
  5  Special Case #2 is unchanged: a backdated receipt crossing the current
     month's deficit restarts the counter at its excess

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate

from periodic_valuation.tests.smoke_edges import make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters

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
		raise Exception("Since-zero counter failures: " + "; ".join(c[0] for c in failed))


def _row(item, d):
	r = frappe.get_all("Inventory Period Balance", filters={"company": COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["name", "closing_qty", "total_received_since_zero"])
	return r[0] if r else frappe._dict()


def _run():
	wh = ensure_masters()
	cur = get_first_day(nowdate())
	prev = add_months(cur, -1)
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prev.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY, "start_date": prev,
			"status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.db.savepoint("since_zero")
	try:
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		previous = [(+100, 75, 2), (-120, None, 4), (+10, 150, 5), (+170, 160, 9), (+5, 170, 15), (-30, None, 17)]
		current = [(+500, 100, 12), (-650, None, 2)]  # entered: the +500 first, then the -650 dated day 3

		def post(item, moves, base):
			for qty, rate, day in moves:
				d = str(add_days(base, day))
				if qty > 0:
					make_pr(item, wh, qty, rate, posting_date=d)
				else:
					make_dn(item, wh, -qty, posting_date=d)

		it = make_item("_SZC-MH63")
		post(it, current, cur)
		post(it, previous, prev)
		p, c = _row(it, prev), _row(it, cur)
		check("1: previous month - closing 135, counter 165", flt(p.closing_qty) == 135
			and flt(p.total_received_since_zero) == 165, f"{p.closing_qty} / {p.total_received_since_zero}")
		check("2: current month - closing -15, counter 0 (passed through zero, nothing received since)",
			flt(c.closing_qty) == -15 and flt(c.total_received_since_zero) == 0,
			f"{c.closing_qty} / {c.total_received_since_zero}")

		it2 = make_item("_SZC-INORDER")
		post(it2, previous, prev)
		post(it2, sorted(current, key=lambda m: m[2]), cur)
		c2 = _row(it2, cur)
		check("3: entered in date order - the -650 goes through zero, the +500 comes while negative: 500",
			flt(c2.closing_qty) == -15 and flt(c2.total_received_since_zero) == 500, f"{c2.total_received_since_zero}")

		frappe.db.set_value("Inventory Period Balance", c.name, "total_received_since_zero", 15, update_modified=False)
		frappe.db.set_value("Inventory Period Balance", p.name, "total_received_since_zero", 999, update_modified=False)
		from periodic_valuation.patches.v1_0 import dr70_since_zero_counter

		dr70_since_zero_counter.execute()
		check("4: the patch re-derives the negative row (15 -> 0)",
			flt(_row(it, cur).total_received_since_zero) == 0, str(_row(it, cur).total_received_since_zero))
		check("4: ...and leaves a row at or above zero as it is", flt(_row(it, prev).total_received_since_zero) == 999)

		it3 = make_item("_SZC-SC2")
		make_pr(it3, wh, 10, 15, posting_date=str(add_days(prev, 9)))
		make_dn(it3, wh, 15)
		make_pr(it3, wh, 8, 43, posting_date=str(add_days(prev, 19)))
		check("5: Special Case #2 - the backdated receipt crossing the current deficit restarts at its excess (3)",
			flt(_row(it3, cur).total_received_since_zero) == 3, str(_row(it3, cur).total_received_since_zero))
	finally:
		frappe.db.rollback(save_point="since_zero")
