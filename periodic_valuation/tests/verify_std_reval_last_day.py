"""Standard cost change on the last day of the period (DR-50; client ticket
STD-003, option b). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_reval_last_day.run

Periodic Standard Cost Settings > Revaluation Posting Date = "Last day of
the period", after the client design §4.4 / §5.7 (only the stock on hand at
the switch point is revalued; posted movements are not repriced):

  A  a change valid from the current month: the month stays at the old cost
     (70); nothing posts at release; after the month a single Rev End
     revalues the closing stock (120 x 20 = 2,400) dated the month's last
     day - GL and stock ledger the same day; the month's settlement gives
     it wholly to ending stock (rev_es -2,400, rev_cons 0) and the GL /
     event identity holds; the new cost (90) prices from the next month
  B  a backdated change (valid from the previous, still-open month) switches
     at the end of the current period
  C  an item's first cost prices its own month at once
  D  the overnight job posts the Rev End once the month has ended, and the
     settlement does not post it twice
  E  back on "First day of the period" the day-1 rule is unchanged

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


class _Today:
	def __init__(self, day):
		self.day = str(day)

	def __enter__(self):
		import frappe.utils
		self._orig = frappe.utils.nowdate
		frappe.utils.nowdate = lambda: self.day

	def __exit__(self, *exc):
		import frappe.utils
		frappe.utils.nowdate = self._orig


def _set(value):
	frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY}, "revaluation_posting_date", value)


def _events(version, trans=None):
	f = {"source_docname": version, "is_cancelled": 0}
	if trans:
		f["std_trans"] = trans
	return frappe.get_all("Inventory Valuation Event", filters=f,
		fields=["std_trans", "total_sc", "posting_date", "period_year", "period_month"])


def _gl_dates(version):
	return set(frappe.get_all("GL Entry", filters={"voucher_no": version, "is_cancelled": 0}, pluck="posting_date"))


def _sc(item, day):
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	return flt(get_active_standard_cost(pack.COMPANY, item, None, day).standard_cost)


def _settle(item, year, month):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	engine = StdEngine(pack.COMPANY, item)
	period = frappe.db.get_value("Inventory Period",
		{"company": pack.COMPANY, "period_year": year, "period_month": month})
	return engine.close_period(year=year, month=month, sc=_sc(item, f"{year}-{month:02d}-01"),
		source=("Inventory Period", period))


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _run():
	from periodic_valuation.shared.period_close import assert_event_gl_identity

	today = getdate(nowdate())
	day1, last = get_first_day(today), get_last_day(today)
	prev = add_days(day1, -1)
	nxt = add_days(last, 1)
	frappe.db.savepoint("std_reval_last")
	try:
		wh, _wh2 = pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")
		_set("Last day of the period")

		# ---- A: current month, 70 -> 90 ---------------------------------
		a = pack.std_item("_STD-LASTDAY-A")
		pack.scv_release(a, prev.year, prev.month, 70)
		pack.make_pr(a, wh, 150, 70, posting_date=str(today))
		pack.make_dn(a, wh, 40, posting_date=str(today))
		v90 = pack.scv_release(a, today.year, today.month, 90)
		v90.reload()
		check("A: the version switches at period end, prices from next month, revalues on the last day",
			v90.switch_at_period_end and (v90.price_from_year, v90.price_from_month) == (nxt.year, nxt.month)
			and getdate(v90.revaluation_date) == last, f"{v90.price_from_year}-{v90.price_from_month} {v90.revaluation_date}")
		check("A: nothing posts at release", not _events(v90.name) and not v90.revaluation_posted)
		check("A: the current month stays at the old cost (70); next month resolves 90",
			_sc(a, today) == 70 and _sc(a, nxt) == 90)
		check("A: resolving next month before this one ends keeps the revaluation pending",
			not frappe.db.get_value(SCV, v90.name, "revaluation_posted"))
		pr = pack.make_pr(a, wh, 10, 70, posting_date=str(today))
		ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": pr.name, "is_cancelled": 0},
			fields=["standard_cost"])
		check("A: a receipt after the release is still valued at the old cost",
			ive and all(flt(e.standard_cost) == 70 for e in ive), str(ive))

		sett = _settle(a, today.year, today.month)
		rev_end = _events(v90.name, "Rev End")
		check("A: settling the month first posts one Rev End = closing 120 x 20 = 2,400 on the last day",
			len(rev_end) == 1 and flt(rev_end[0].total_sc) == 2400 and getdate(rev_end[0].posting_date) == last,
			str(rev_end))
		check("A: no day-1 triplet", not [e for e in _events(v90.name) if e.std_trans != "Rev End"])
		check("A: its GL and stock-ledger row are dated the last day",
			_gl_dates(v90.name) == {last} and set(frappe.get_all("Stock Ledger Entry",
				filters={"voucher_no": v90.name, "is_cancelled": 0}, pluck="posting_date")) == {last})
		check("A: the settlement gives the revaluation wholly to ending stock",
			flt(sett.rev_es) == -2400 and flt(sett.rev_cons) == 0, f"rev_es {sett.rev_es} rev_cons {sett.rev_cons}")
		check("A: inventory GL equals the valuation events for the month",
			assert_event_gl_identity(frappe.get_doc("Inventory Period", cur_period))["ok"])

		# ---- B: backdated, valid from the previous month ------------------
		b = pack.std_item("_STD-LASTDAY-B")
		pack.scv_release(b, prev.year, prev.month, 10)
		pack.make_pr(b, wh, 61, 10, posting_date=str(prev))
		v12 = pack.scv_release(b, prev.year, prev.month, 12)
		v12.reload()
		check("B: a backdated re-price switches at the end of the current period; the earlier cost keeps pricing",
			getdate(v12.revaluation_date) == last and (v12.price_from_year, v12.price_from_month) == (nxt.year, nxt.month)
			and _sc(b, prev) == 10 and _sc(b, today) == 10, str(v12.revaluation_date))

		# ---- C: first cost ----------------------------------------------
		c = pack.std_item("_STD-LASTDAY-C")
		vc = pack.scv_release(c, today.year, today.month, 40)
		vc.reload()
		check("C: an item's first cost prices its own month at once",
			not vc.switch_at_period_end and vc.revaluation_posted and _sc(c, today) == 40)

		# ---- D: the overnight job, once the month has ended --------------
		from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
			materialize_pending_revaluations,
		)

		d = pack.std_item("_STD-LASTDAY-D")
		pack.scv_release(d, prev.year, prev.month, 50)
		pack.make_pr(d, wh, 8, 50, posting_date=str(today))
		v55 = pack.scv_release(d, today.year, today.month, 55)
		materialize_pending_revaluations()
		check("D: before the month ends the overnight job leaves it pending",
			not _events(v55.name, "Rev End"))
		with _Today(nxt):
			materialize_pending_revaluations()
		rev_end = _events(v55.name, "Rev End")
		check("D: after the month ends it posts Rev End 8 x 5 = 40 on the last day",
			len(rev_end) == 1 and flt(rev_end[0].total_sc) == 40 and getdate(rev_end[0].posting_date) == last,
			str(rev_end))
		_settle(d, today.year, today.month)
		check("D: the settlement does not post it twice", len(_events(v55.name, "Rev End")) == 1)

		# ---- E: back on the first day ------------------------------------
		_set("First day of the period")
		e = pack.std_item("_STD-LASTDAY-E")
		pack.scv_release(e, prev.year, prev.month, 20)
		pack.make_pr(e, wh, 5, 20, posting_date=str(today))
		v25 = pack.scv_release(e, today.year, today.month, 25)
		v25.reload()
		t = _events(v25.name)
		check("E: the first-day rule is unchanged - day-1 triplet, new cost this month",
			t and {getdate(x.posting_date) for x in t} == {day1} and _sc(e, today) == 25
			and not v25.switch_at_period_end and getdate(v25.revaluation_date) == day1, str(t))
	finally:
		frappe.db.rollback(save_point="std_reval_last")

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD period-end revaluation failures: " + "; ".join(c[0] for c in failed))
