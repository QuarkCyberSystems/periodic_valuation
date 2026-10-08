"""Standard cost windows: every RELEASED version has Effective From and
Effective To, and no two overlap (client design §3 / §4.1 / §4.5; DR-63).
Run:
bench --site <site> execute periodic_valuation.tests.verify_std_cost_windows.run

  A  a chain: the earlier version closes the day before the next one starts
  B  a correction of the previous month when the version in force started in
     that month: the correction holds the month, that version moves to the
     current month
  C  a correction of the previous month when the version in force started
     earlier: it closes before the corrected month and a continuation at the
     same cost carries on from the current month (nothing to revalue)
  D  a later current-month version supersedes the continuation
  E  two RELEASED versions whose windows overlap are refused
  F  the cost every day resolves to is the same before and after windowing

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback
from datetime import timedelta

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
SCV = "Item Standard Cost Version"


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def _windows(item):
	return [(r.standard_cost, str(r.effective_from), str(r.effective_to) if r.effective_to else None, r.source_type)
		for r in frappe.get_all(SCV, filters={"item_code": item, "company": pack.COMPANY, "status": "RELEASED"},
			fields=["standard_cost", "effective_from", "effective_to", "source_type"], order_by="effective_from")]


def _sc(item, day):
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	return flt(get_active_standard_cost(pack.COMPANY, item, None, day).standard_cost)


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD cost window failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		assert_no_overlap,
		normalize_cost_windows,
	)

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_days(day1, -1)
	p1, plast = get_first_day(prev), get_last_day(prev)
	pp = add_months(day1, -2)
	pplast = get_last_day(pp)
	frappe.db.savepoint("std_windows")
	try:
		pack.ensure_company()
		pack.make_period(pp.year, pp.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
			"revaluation_posting_date", "First day of the period")

		# ---- A: a chain ------------------------------------------------------
		a = pack.std_item("_STD-WIN-A")
		pack.scv_release(a, prev.year, prev.month, 100)
		pack.scv_release(a, today.year, today.month, 110)
		check("A: the earlier version closes the day before the next starts",
			_windows(a) == [(100.0, str(p1), str(plast), "MANUAL_OVERRIDE"), (110.0, str(day1), None, "MANUAL_OVERRIDE")],
			str(_windows(a)))

		# ---- B: correction, the version in force started in the corrected month --
		b = pack.std_item("_STD-WIN-B")
		pack.scv_release(b, prev.year, prev.month, 300)
		vb = pack.scv_release(b, prev.year, prev.month, 400)
		check("B: the correction holds the month; the earlier version moves to the current month",
			_windows(b) == [(400.0, str(p1), str(plast), "MANUAL_OVERRIDE"), (300.0, str(day1), None, "MANUAL_OVERRIDE")]
			and frappe.db.get_value(SCV, vb.name, "is_correction") == 1, str(_windows(b)))

		# ---- C: correction, the version in force started earlier -----------------
		c = pack.std_item("_STD-WIN-C")
		pack.scv_release(c, pp.year, pp.month, 300)
		pack.make_period(pp.year, pp.month, "SETTLED_FROZEN")       # two months back is closed
		pack.scv_release(c, prev.year, prev.month, 400)
		check("C: the earlier version closes before the corrected month; a continuation carries on at 300",
			_windows(c) == [(300.0, str(pp), str(pplast), "MANUAL_OVERRIDE"),
				(400.0, str(p1), str(plast), "MANUAL_OVERRIDE"),
				(300.0, str(day1), None, "CORRECTION_CONTINUATION")], str(_windows(c)))
		check("C: costs: 300 two months back, 400 in the corrected month, 300 now",
			_sc(c, add_days(pp, 10)) == 300 and _sc(c, add_days(p1, 10)) == 400 and _sc(c, today) == 300)
		cont = frappe.get_all(SCV, filters={"item_code": c, "source_type": "CORRECTION_CONTINUATION"},
			fields=["name", "revaluation_posted"])
		check("C: the continuation revalues nothing",
			cont and cont[0].revaluation_posted == 1
			and not frappe.db.exists("Inventory Valuation Event", {"source_docname": cont[0].name}), str(cont))

		# ---- D: a later current-month version supersedes the continuation ---------
		pack.scv_release(c, today.year, today.month, 350)
		check("D: a current-month version supersedes the continuation",
			_windows(c)[-1][:2] == (350.0, str(day1)) and len(_windows(c)) == 3
			and frappe.db.get_value(SCV, cont[0].name, "status") == "SUPERSEDED", str(_windows(c)))

		# ---- E: overlapping windows are refused ------------------------------------
		first = frappe.get_all(SCV, filters={"item_code": a, "status": "RELEASED"}, order_by="effective_from", pluck="name")[0]
		frappe.db.set_value(SCV, first, "effective_to", None, update_modified=False)
		refused = False
		try:
			assert_no_overlap(pack.COMPANY, a, None)
		except frappe.ValidationError:
			frappe.clear_last_message()
			refused = True
		check("E: two RELEASED versions whose windows overlap are refused", refused)
		frappe.db.set_value(SCV, first, "effective_to", plast, update_modified=False)

		# ---- F: windowing never changes the cost a date resolves to --------------
		f = pack.std_item("_STD-WIN-F")
		pack.scv_release(f, prev.year, prev.month, 10)
		pack.scv_release(f, today.year, today.month, 12)
		# make the scope messy as data written before DR-63: open windows everywhere
		for n in frappe.get_all(SCV, filters={"item_code": f}, pluck="name"):
			frappe.db.set_value(SCV, n, "effective_to", None, update_modified=False)
		days = [p1 + timedelta(days=i) for i in range((today - p1).days + 40)]
		before = [_sc(f, d) for d in days]
		normalize_cost_windows(pack.COMPANY, f, None)
		check("F: every day resolves to the same cost after windowing",
			before == [_sc(f, d) for d in days] and _windows(f)[0][2] == str(get_last_day(add_days(day1, -1))),
			str(_windows(f)))
	finally:
		frappe.db.rollback(save_point="std_windows")
