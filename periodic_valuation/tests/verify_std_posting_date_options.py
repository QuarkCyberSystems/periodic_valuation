"""The two Revaluation Posting Date options, case by case, as the client
answered them on 06/10/2026 (DR-55). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_posting_date_options.run

Both options price from the valid-from month and revalue with the day-1
triplet over the month to date; the option chooses only the dates. Each case
runs under "First day of the period" and "Latest day of the period":

  1  current-month change 300 -> 400 (opening 800, October in 100 / out
     50): Rev Beg 80,000 / REV In 10,000 / REV out -5,000, dated day 1 or the
     release day; the whole month prices at 400
  2  backdated (previous month open) = a correction of that month only
     (DR-57): it revalues (REV In 100,000 / REV out -20,000) on its day 1 or
     last day, Rev Reverse -80,000 on day 1 of the current month, which keeps
     its own standard (300)
  3  previous month settled: nothing posts in it; the current month revalues
  4  valid-from month frozen: refused
  5  future month: refused
  6  the item's first cost: one zero-value event, no GL
  7  no stock and no movement: one zero-value event, no GL
  8  late entry dated in the current month before the release: 400
  9  late entry dated in the previous month after the release: 400, bridged
     to the current month's 300
  10 the month's settlement is the same under both options
  L  a version released under the 05/10 switch-at-release rule keeps pricing
     from its switch and bridges a late entry; a new version released after
     it takes over and measures its delta from it

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
SCV = "Item Standard Cost Version"
MODES = (("First", "First day of the period"), ("Latest", "Latest day of the period"))


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def _set(value):
	frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY}, "revaluation_posting_date", value)


def _events(source):
	return sorted((e.std_trans, flt(e.total_sc, 2), str(e.posting_date))
		for e in frappe.get_all("Inventory Valuation Event",
			filters={"source_docname": source, "is_cancelled": 0},
			fields=["std_trans", "total_sc", "posting_date"]))


def _sc(item, day):
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	return flt(get_active_standard_cost(pack.COMPANY, item, None, day).standard_cost)


def _closing(item, d):
	d = getdate(d)
	row = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["closing_qty", "closing_value"])
	return (flt(row[0].closing_qty), flt(row[0].closing_value, 2)) if row else (0.0, 0.0)


def _release(item, when, sc):
	"""A real release (pack.scv_release stores a future version as pending)."""
	doc = frappe.get_doc({"doctype": SCV, "company": pack.COMPANY, "item_code": item,
		"valid_from_year": when.year, "valid_from_month": when.month, "standard_cost": sc,
		"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
	doc.release()


def _refused(fn):
	frappe.db.savepoint("pdo_refusal")
	try:
		fn()
		return False
	except frappe.ValidationError:
		return True
	finally:
		frappe.db.rollback(save_point="pdo_refusal")


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD posting date option failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.shared.period_close import assert_event_gl_identity

	today = getdate(nowdate())
	if today.day < 3:
		print("SKIP: needs two days of the current month before today")
		return
	day1 = get_first_day(today)
	o2 = add_days(day1, 1)
	prev = add_days(day1, -1)
	p1, p2, p3, plast = get_first_day(prev), add_days(get_first_day(prev), 1), add_days(get_first_day(prev), 2), get_last_day(prev)
	frappe.db.savepoint("std_pdo")
	try:
		wh, _wh2 = pack.ensure_company()
		prev_period = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")

		def setup(code, current_moves=True):
			it = pack.std_item(code)
			pack.scv_release(it, prev.year, prev.month, 300)
			pack.make_pr(it, wh, 1000, 300, posting_date=str(p2))
			pack.make_dn(it, wh, 200, posting_date=str(p3))
			if current_moves:
				pack.make_pr(it, wh, 100, 300, posting_date=str(day1))
				pack.make_dn(it, wh, 50, posting_date=str(o2))
			return it

		def identity():
			return all(assert_event_gl_identity(frappe.get_doc("Inventory Period", n))["ok"]
				for n in (prev_period, cur_period))

		for tag, mode in MODES:
			_set(mode)
			rd = str(today) if tag == "Latest" else str(day1)        # current-month revaluation date
			pd = str(plast) if tag == "Latest" else str(p1)           # previous-month revaluation date

			# 1 current month
			it = setup(f"_STD-PDO1-{tag}")
			v = pack.scv_release(it, today.year, today.month, 400)
			check(f"{tag} 1: Rev Beg 80,000 / REV In 10,000 / REV out -5,000 dated {rd}",
				_events(v.name) == sorted([("Rev Beg", 80000.0, rd), ("REV In", 10000.0, rd), ("REV out", -5000.0, rd)]),
				str(_events(v.name)))
			check(f"{tag} 1: the whole month prices at 400; it closes at 850 x 400",
				_sc(it, day1) == 400 and _closing(it, today) == (850.0, 340000.0), str(_closing(it, today)))
			check(f"{tag} 1: GL equals the valuation events", identity())
			# 8 late entry dated before the release
			late = pack.make_pr(it, wh, 20, 300, posting_date=str(o2))
			check(f"{tag} 8: a late receipt dated {o2} is valued at 400, no extra entry",
				[(x[0], x[1]) for x in _events(late.name)] == [("Rec", 8000.0)], str(_events(late.name)))
			# 10 settlement - the same under both options (proportional)
			sett = StdEngine(pack.COMPANY, it).close_period(year=today.year, month=today.month, sc=400,
				source=("Inventory Period", cur_period))
			expected = flt(sett.rev_pool) * flt(sett.es_qty) / (flt(sett.beg_qty) + flt(sett.in_qty))
			check(f"{tag} 10: the revaluation settles over the whole month (ending / (beg + in))",
				abs(flt(sett.rev_es) - expected) < 0.01, f"{sett.rev_es} vs {expected}")

			# 2 backdated
			it = setup(f"_STD-PDO2-{tag}")
			v = pack.scv_release(it, prev.year, prev.month, 400)
			check(f"{tag} 2: previous month revalued on {pd}, reversed on {day1}; the current month keeps its standard",
				_events(v.name) == sorted([("REV In", 100000.0, pd), ("REV out", -20000.0, pd),
					("Rev Reverse", -80000.0, str(day1))]),
				str(_events(v.name)))
			check(f"{tag} 2: previous month 800 x 400 and prices at 400; current 850 x 300 and keeps 300",
				_closing(it, prev) == (800.0, 320000.0) and _closing(it, today) == (850.0, 255000.0)
				and _sc(it, p3) == 400 and _sc(it, day1) == 300, f"{_closing(it, prev)} {_closing(it, today)}")
			check(f"{tag} 2: GL equals the valuation events", identity())
			# 9 late entry into the previous month
			late = pack.make_pr(it, wh, 10, 300, posting_date=str(p3))
			ev = _events(late.name)
			check(f"{tag} 9: a late receipt into the previous month is valued at 400, bridged to the current 300",
				("REC (BD)", 4000.0, str(p3)) in ev and ("REC (BD) - Rev", -1000.0, str(today)) in ev, str(ev))

			# 3 previous month settled
			it = setup(f"_STD-PDO3-{tag}")
			StdEngine(pack.COMPANY, it).close_period(year=prev.year, month=prev.month, sc=300,
				source=("Inventory Period", prev_period))
			v = pack.scv_release(it, prev.year, prev.month, 400)
			check(f"{tag} 3: settled previous month - nothing in it, the current month revalues on {rd}",
				_events(v.name) and all(x[2] == rd for x in _events(v.name))
				and not [x for x in _events(v.name) if x[0] == "Rev Reverse"], str(_events(v.name)))

			# 4 frozen valid-from month, 5 future month
			it = pack.std_item(f"_STD-PDO45-{tag}")
			pack.scv_release(it, today.year, today.month, 300)
			pp = add_months(day1, -2)
			pack.make_period(pp.year, pp.month, "SETTLED_FROZEN")
			check(f"{tag} 4: a version for a frozen month is refused",
				_refused(lambda: _release(it, pp, 350)))
			nxt = add_months(day1, 1)
			check(f"{tag} 5: a version for a future month is refused",
				_refused(lambda: _release(it, nxt, 350)))

			# 6 first cost, 7 no stock and no movement
			it = pack.std_item(f"_STD-PDO67-{tag}")
			v6 = pack.scv_release(it, today.year, today.month, 300)
			check(f"{tag} 6: the first cost records one zero-value event on {rd}, no GL",
				_events(v6.name) == [("Rev Beg", 0.0, rd)]
				and not frappe.db.exists("GL Entry", {"voucher_no": v6.name}), str(_events(v6.name)))
			v7 = pack.scv_release(it, today.year, today.month, 320)
			check(f"{tag} 7: no stock, no movement - one zero-value event on {rd}, no GL",
				_events(v7.name) == [("Rev Beg", 0.0, rd)]
				and not frappe.db.exists("GL Entry", {"voucher_no": v7.name}), str(_events(v7.name)))

		# ---- L: a version released under the 05/10 switch-at-release rule -------
		_set("First day of the period")
		it = pack.std_item("_STD-PDO-LEGACY")
		v0 = pack.scv_release(it, prev.year, prev.month, 30)
		pack.make_pr(it, wh, 10, 30, posting_date=str(p2))
		legacy = frappe.get_doc({"doctype": SCV, "company": pack.COMPANY, "item_code": it,
			"valid_from_year": prev.year, "valid_from_month": prev.month, "standard_cost": 35,
			"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
		frappe.db.set_value(SCV, legacy.name, {
			"status": "RELEASED", "switch_on_release": 1, "revaluation_posted": 1,
			"price_from_year": today.year, "price_from_month": today.month,
			"effective_from": str(o2), "revaluation_date": str(o2),
			"released_on": f"{o2} 09:00:00", "supersedes_version": v0.name,
		}, update_modified=False)
		check("L: a switch-at-release version prices from its switch (30 before, 35 from it)",
			_sc(it, day1) == 30 and _sc(it, o2) == 35)
		late = pack.make_pr(it, wh, 4, 30, posting_date=str(day1))
		check("L: a late entry dated before its switch still bridges (4 x 5 = 20 as Rev Rel)",
			("Rev Rel", 20.0) in [(x[0], x[1]) for x in _events(late.name)], str(_events(late.name)))
		v40 = pack.scv_release(it, today.year, today.month, 40)
		check("L: a new version released after it takes over and measures its delta from 35",
			_sc(it, today) == 40 and frappe.db.get_value(SCV, v40.name, "supersedes_version") == legacy.name
			and ("Rev Beg", 50.0, str(day1)) in _events(v40.name), str(_events(v40.name)))
	finally:
		frappe.db.rollback(save_point="std_pdo")
