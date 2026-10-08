"""Backdated standard cost change = a correction of that month only (DR-57;
client 06/10/2026: "the first reversal should exist", tickets STD-003 /
STD-004). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_backdated_change.run

A change valid from the previous month (still open, not settled), released
in the current month; the previous month received 1,000 at 300 and issued
200, so 800 are on hand; 300 -> 400:

  A  MTD, "First day of the period": the previous month's triplet on its
     day 1 (REV In 100,000 / REV out -20,000); Rev Reverse -80,000 on day 1
     of the current month; nothing else. The previous month closes at
     800 x 400, the current month at 800 x 300 and keeps pricing at 300;
     the consumption adjustment stays in the previous month; the earlier
     version stays RELEASED; GL = valuation events in both months
  B  MTD, "Latest day of the period": the same, the triplet dated the
     previous month's last day and the reversal the release day (DR-59)
  C  YTD, "First day": the previous month's YTD triplet, then the stock
     reversed on day 1 - consumption adjusted once (20,000)
  D  previous month already settled for the item: nothing posts into it;
     the current month revalues forward as before (design §8.A)
  E  nothing on hand and no movement: one zero-value event, dated as the
     revaluation would be; no reversal
  F  a change for the current month is unchanged
  G  a late receipt into the corrected month is valued at 400 and bridged
     back to the current month's 300 (DR-09 companion), dated day 1 under
     "First day of the period" (DR-59)
  H  a new version for the current month measures its delta from the
     current month's standard (300), not from the correction; the 300
     version then prices nothing and is SUPERSEDED (DR-60)
  I  a second correction of the same month supersedes the first

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, flt, get_first_day, get_last_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
SCV = "Item Standard Cost Version"


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


def _gl(item, account):
	return flt(frappe.db.sql(
		"""select coalesce(sum(g.debit - g.credit), 0) from `tabGL Entry` g
		join `tabInventory Valuation Event` e on e.name = g.valuation_event_id
		where e.item_code = %s and g.account = %s and g.is_cancelled = 0""", (item, account))[0][0], 2)


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD backdated correction failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.shared.period_close import assert_event_gl_identity

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_days(day1, -1)
	p1, p2, p3, plast = get_first_day(prev), add_days(get_first_day(prev), 1), add_days(get_first_day(prev), 2), get_last_day(prev)
	frappe.db.savepoint("std_bd_corr")
	try:
		wh, _wh2 = pack.ensure_company()
		prev_period = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")
		acc = pack.acct()

		def setup(code, view="MTD"):
			it = pack.std_item(code, view=view)
			v0 = pack.scv_release(it, prev.year, prev.month, 300)
			pack.make_pr(it, wh, 1000, 300, posting_date=str(p2))
			pack.make_dn(it, wh, 200, posting_date=str(p3))
			return it, v0

		def identity(label):
			ok = all(assert_event_gl_identity(frappe.get_doc("Inventory Period", n))["ok"]
				for n in (prev_period, cur_period))
			check(f"{label}: inventory GL equals the valuation events in both months", ok)

		# ---- A: MTD, first day ---------------------------------------------
		_set("First day of the period")
		a, a0 = setup("_STD-BDCOR-A")
		va = pack.scv_release(a, prev.year, prev.month, 400)
		check("A: the correction applies to the previous month only",
			str(frappe.db.get_value(SCV, va.name, "effective_to")) == str(plast))
		check("A: previous month revalued on its day 1, reversed on day 1 of the current month, nothing else",
			_events(va.name) == sorted([("REV In", 100000.0, str(p1)), ("REV out", -20000.0, str(p1)),
				("Rev Reverse", -80000.0, str(day1))]), str(_events(va.name)))
		check("A: previous month closes at 800 x 400; the current month at 800 x 300",
			_closing(a, prev) == (800.0, 320000.0) and _closing(a, today) == (800.0, 240000.0),
			f"{_closing(a, prev)} {_closing(a, today)}")
		check("A: the previous month prices at 400, the current month keeps 300",
			_sc(a, p3) == 400 and _sc(a, day1) == 300 and _sc(a, today) == 300)
		check("A: the consumption adjustment stays in the previous month (20,000)",
			_gl(a, acc.cogs_adj) == 20000, str(_gl(a, acc.cogs_adj)))
		check("A: the earlier version stays RELEASED and now starts from the current month (DR-60)",
			frappe.db.get_value(SCV, a0.name, "status") == "RELEASED"
			and str(frappe.db.get_value(SCV, a0.name, "effective_from")) == str(day1))
		identity("A")

		# ---- G: late receipt into the corrected month -------------------------
		late = pack.make_pr(a, wh, 10, 300, posting_date=str(p3))
		check("G: a late receipt into the corrected month is at 400, bridged back to 300 on day 1 (First day, DR-59)",
			_events(late.name) == sorted([("REC (BD)", 4000.0, str(p3)), ("REC (BD) - Rev", -1000.0, str(day1))])
			and _closing(a, today) == (810.0, 243000.0), f"{_events(late.name)} {_closing(a, today)}")

		# ---- H: a new version for the current month ------------------------------
		vh = pack.scv_release(a, today.year, today.month, 350)
		check("H: a current-month version measures its delta from 300 (Rev Beg 810 x 50)",
			("Rev Beg", 40500.0, str(day1)) in _events(vh.name)
			and frappe.db.get_value(SCV, vh.name, "supersedes_version") == a0.name, str(_events(vh.name)))
		check("H: the 300 version, which no date resolves to any more, is SUPERSEDED; one RELEASED per month (DR-60)",
			frappe.db.get_value(SCV, a0.name, "status") == "SUPERSEDED"
			and sorted(frappe.get_all(SCV, filters={"item_code": a, "status": "RELEASED"}, pluck="name")) == sorted([va.name, vh.name]))

		# ---- I: a second correction of the same month retires the first ---------
		vi = pack.scv_release(a, prev.year, prev.month, 450)
		check("I: a second correction of the previous month retires the first (DR-60)",
			frappe.db.get_value(SCV, va.name, "status") == "SUPERSEDED"
			and frappe.db.get_value(SCV, vi.name, "status") == "RELEASED" and _sc(a, p3) == 450 and _sc(a, today) == 350)

		# ---- B: MTD, latest day --------------------------------------------
		_set("Latest day of the period")
		b, _b0 = setup("_STD-BDCOR-B")
		vb = pack.scv_release(b, prev.year, prev.month, 400)
		check("B: previous month revalued on its last day, reversed on the release day (Latest day, DR-59)",
			_events(vb.name) == sorted([("REV In", 100000.0, str(plast)), ("REV out", -20000.0, str(plast)),
				("Rev Reverse", -80000.0, str(today))]), str(_events(vb.name)))
		check("B: previous month 800 x 400, current month 800 x 300",
			_closing(b, prev) == (800.0, 320000.0) and _closing(b, today) == (800.0, 240000.0),
			f"{_closing(b, prev)} {_closing(b, today)}")
		identity("B")

		# ---- C: YTD, first day ---------------------------------------------
		_set("First day of the period")
		if prev.year == today.year:
			c, _c0 = setup("_STD-BDCOR-C", view="YTD")
			vc = pack.scv_release(c, prev.year, prev.month, 400)
			check("C: YTD - the previous month's triplet, the stock reversed on day 1",
				_events(vc.name) == sorted([("REV In", 100000.0, str(p1)), ("REV out", -20000.0, str(p1)),
					("Rev Reverse", -80000.0, str(day1))]), str(_events(vc.name)))
			check("C: consumption adjusted once (20,000); the current month at 800 x 300",
				_gl(c, acc.cogs_adj) == 20000 and _closing(c, today) == (800.0, 240000.0),
				f"cogs {_gl(c, acc.cogs_adj)} closing {_closing(c, today)}")
			identity("C")
		else:
			print("SKIP C: the previous month is in the prior fiscal year")

		# ---- D: previous month settled for the item ------------------------
		d, _d0 = setup("_STD-BDCOR-D")
		StdEngine(pack.COMPANY, d).close_period(year=prev.year, month=prev.month, sc=300,
			source=("Inventory Period", prev_period))
		vd = pack.scv_release(d, prev.year, prev.month, 400)
		check("D: settled previous month - nothing posts into it, no reversal; the current month revalues forward",
			_events(vd.name) and all(x[2] == str(day1) for x in _events(vd.name))
			and not [x for x in _events(vd.name) if x[0] == "Rev Reverse"]
			and not frappe.db.get_value(SCV, vd.name, "effective_to"), str(_events(vd.name)))

		# ---- E: nothing on hand, no movement ---------------------------------
		e = pack.std_item("_STD-BDCOR-E")
		pack.scv_release(e, prev.year, prev.month, 300)
		ve = pack.scv_release(e, prev.year, prev.month, 400)
		check("E: one zero-value event dated as the revaluation would be, no reversal",
			_events(ve.name) == [("Rev Beg", 0.0, str(p1))], str(_events(ve.name)))

		# ---- F: current-month change ------------------------------------------
		f, _f0 = setup("_STD-BDCOR-F")
		vf = pack.scv_release(f, today.year, today.month, 400)
		check("F: a change for the current month posts nothing in the previous month, no reversal",
			_events(vf.name) and all(x[2] >= str(day1) for x in _events(vf.name))
			and not [x for x in _events(vf.name) if x[0] == "Rev Reverse"], str(_events(vf.name)))
	finally:
		frappe.db.rollback(save_point="std_bd_corr")
