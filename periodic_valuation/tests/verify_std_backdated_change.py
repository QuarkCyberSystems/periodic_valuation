"""Backdated standard cost change: the valid-from month is revalued and that
value carries into the current month; the current month revalues only its
own movements - no reversal (DR-56; client design §8.A / §4.3; Vivek
06/10/2026, replacing DR-54's day-1 reversal). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_backdated_change.run

A change valid from the previous month (still open, not settled), released
in the current month; the previous month received 1,000 at 300 and issued
200, so 800 are on hand; 300 -> 400:

  A  MTD, "First day of the period": the previous month's triplet on its
     day 1 (REV In 100,000 / REV out -20,000); nothing reverses; the
     current month (no movements of its own) posts nothing; both months
     close at 800 x 400; the consumption adjustment stays in the previous
     month; GL = valuation events in both months
  B  MTD, "Latest day of the period": the same, dated the previous month's
     last day
  C  YTD, "First day": the previous month's YTD triplet only - consumption
     adjusted once (20,000)
  D  previous month already settled for the item: nothing posts into it;
     the current month revalues forward as before (design §8.A)
  E  nothing on hand and no movement: one zero-value event in the current
     month, nothing in the previous one
  F  a change for the current month is unchanged (no previous-month entry)

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


def _events(version):
	return [(e.std_trans, flt(e.total_sc, 2), str(e.posting_date))
		for e in frappe.get_all("Inventory Valuation Event",
			filters={"source_docname": version, "is_cancelled": 0},
			fields=["std_trans", "total_sc", "posting_date"], order_by="creation")]


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
		raise Exception("STD backdated change failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.shared.period_close import assert_event_gl_identity

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_days(day1, -1)
	p1, p2, p3, plast = get_first_day(prev), add_days(get_first_day(prev), 1), add_days(get_first_day(prev), 2), get_last_day(prev)
	frappe.db.savepoint("std_bd_chg")
	try:
		wh, _wh2 = pack.ensure_company()
		prev_period = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")
		acc = pack.acct()

		def setup(code, view="MTD"):
			it = pack.std_item(code, view=view)
			pack.scv_release(it, prev.year, prev.month, 300)
			pack.make_pr(it, wh, 1000, 300, posting_date=str(p2))
			pack.make_dn(it, wh, 200, posting_date=str(p3))
			return it

		def identity(label):
			ok = all(assert_event_gl_identity(frappe.get_doc("Inventory Period", n))["ok"]
				for n in (prev_period, cur_period))
			check(f"{label}: inventory GL equals the valuation events in both months", ok)

		# ---- A: MTD, first day ---------------------------------------------
		_set("First day of the period")
		a = setup("_STD-BDREV-A")
		va = pack.scv_release(a, prev.year, prev.month, 400)
		ev = _events(va.name)
		check("A: previous month revalued on its day 1 (REV In 100,000 / REV out -20,000), nothing else",
			sorted(ev) == sorted([("REV In", 100000.0, str(p1)), ("REV out", -20000.0, str(p1))]), str(ev))
		check("A: previous month closes at 800 x 400 = 320,000; the current month too",
			_closing(a, prev) == (800.0, 320000.0) and _closing(a, today) == (800.0, 320000.0),
			f"{_closing(a, prev)} {_closing(a, today)}")
		check("A: the consumption adjustment stays in the previous month (COGS Adjustment 20,000 once)",
			_gl(a, acc.cogs_adj) == 20000, str(_gl(a, acc.cogs_adj)))
		check("A: the previous month prices at 400 from its day 1", _sc(a, p1) == 400 and _sc(a, today) == 400)
		identity("A")

		# ---- B: MTD, latest day --------------------------------------------
		_set("Latest day of the period")
		b = setup("_STD-BDREV-B")
		vb = pack.scv_release(b, prev.year, prev.month, 400)
		ev = _events(vb.name)
		check("B: previous month revalued on its last day, nothing else",
			sorted(ev) == sorted([("REV In", 100000.0, str(plast)), ("REV out", -20000.0, str(plast))]), str(ev))
		check("B: both months close at 800 x 400; the previous month prices at 400",
			_closing(b, prev) == (800.0, 320000.0) and _closing(b, today) == (800.0, 320000.0)
			and _sc(b, p3) == 400, f"{_closing(b, prev)} {_closing(b, today)}")
		identity("B")

		# ---- C: YTD, first day ---------------------------------------------
		_set("First day of the period")
		if prev.year == today.year:
			c = setup("_STD-BDREV-C", view="YTD")
			vc = pack.scv_release(c, prev.year, prev.month, 400)
			ev = _events(vc.name)
			check("C: YTD - the previous month's triplet only, no reversal",
				sorted(ev) == sorted([("REV In", 100000.0, str(p1)), ("REV out", -20000.0, str(p1))]), str(ev))
			check("C: COGS adjustment counted once (20,000) and stock at 800 x 400",
				_gl(c, acc.cogs_adj) == 20000 and _closing(c, today) == (800.0, 320000.0),
				f"cogs {_gl(c, acc.cogs_adj)} closing {_closing(c, today)}")
			identity("C")
		else:
			print("SKIP C: the previous month is in the prior fiscal year")

		# ---- D: previous month settled for the item ------------------------
		d = setup("_STD-BDREV-D")
		StdEngine(pack.COMPANY, d).close_period(year=prev.year, month=prev.month, sc=300,
			source=("Inventory Period", prev_period))
		vd = pack.scv_release(d, prev.year, prev.month, 400)
		ev = _events(vd.name)
		check("D: settled previous month - nothing posts into it; the current month revalues forward",
			ev and all(x[2] == str(day1) for x in ev) and not [x for x in ev if x[0] == "Rev Reverse"], str(ev))

		# ---- E: nothing on hand, no movement ---------------------------------
		e = pack.std_item("_STD-BDREV-E")
		pack.scv_release(e, prev.year, prev.month, 300)
		ve = pack.scv_release(e, prev.year, prev.month, 400)
		ev = _events(ve.name)
		check("E: one zero-value event in the current month, nothing in the previous one",
			ev == [("Rev Beg", 0.0, str(day1))], str(ev))

		# ---- F: current-month change ------------------------------------------
		f = setup("_STD-BDREV-F")
		vf = pack.scv_release(f, today.year, today.month, 400)
		check("F: a change for the current month posts nothing in the previous month",
			_events(vf.name) and all(x[2] >= str(day1) for x in _events(vf.name)), str(_events(vf.name)))
	finally:
		frappe.db.rollback(save_point="std_bd_chg")
