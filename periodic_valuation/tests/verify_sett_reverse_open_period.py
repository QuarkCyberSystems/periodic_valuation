"""A settlement is reversible while its month is still open in the period
machine, however far the calendar has moved on (client ticket TSK-2026-01150,
24/11/2026: September, still PREV_OPEN_UNSETTLED, could not be reversed
because the calendar month was November). Run:
bench --site <site> execute periodic_valuation.tests.verify_sett_reverse_open_period.run

  1  a month two calendar months back, still previous-open, is settled
  2  Reverse Settlement on it succeeds: the settlement is cancelled, the
     balance reads unsettled, and its GL nets to zero
  3  the month settles again
  4  once the month is frozen, Reverse Settlement is refused
  5  an engine call that states its own entry date keeps the calendar rule

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
		raise Exception("Settlement reversal by open period failures: " + "; ".join(c[0] for c in failed))


def _refusal(fn):
	frappe.db.savepoint("srop_refusal")
	try:
		fn()
		return ""
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="srop_refusal")


def _gl_by_account(events):
	rows = frappe.db.sql("""select account, sum(debit - credit) from `tabGL Entry`
		where is_cancelled = 0 and voucher_detail_no in %(ev)s group by account""", {"ev": tuple(events)})
	return {a: flt(v, 2) for a, v in rows}


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	today = getdate(nowdate())
	day1 = get_first_day(today)
	old = add_months(day1, -2)      # e.g. September when the calendar is in November
	opn = add_months(day1, -1)
	frappe.db.savepoint("sett_reverse_open_period")
	try:
		wh, _ = pack.ensure_company()
		sp = pack.make_period(old.year, old.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(opn.year, opn.month, "OPEN")
		it = pack.std_item("_STD-SROP")
		pack.scv_release(it, old.year, old.month, 10)
		pack.make_pr(it, wh, 100, 12, posting_date=str(add_days(old, 2)))
		engine = StdEngine(pack.COMPANY, it)

		# ---- 1 ------------------------------------------------------------------
		s1 = engine.close_period(year=old.year, month=old.month, sc=10, source=("Inventory Period", sp))
		check("1: the previous-open month two calendar months back settles", s1 and flt(s1.es_var) == 200,
			s1 and s1.es_var)

		# ---- 2 ------------------------------------------------------------------
		msg = ""
		try:
			frappe.get_doc("Inventory Period Settlement", s1.name).reverse()
		except frappe.ValidationError as e:
			msg = str(e)
		s1r = frappe.db.get_value("Inventory Period Settlement", s1.name,
			["cancelled", "reversed_by_events"], as_dict=True)
		check("2: Reverse Settlement succeeds while the month is still open", not msg and s1r.cancelled == 1, msg)
		link = frappe.db.get_value("Inventory Period Balance", {"company": pack.COMPANY, "item_code": it,
			"period_year": old.year, "period_month": old.month}, "settlement")
		check("2: the balance reads unsettled", not link, link)
		events = [s1.sett_event, s1.sett_rev_event, *(s1r.reversed_by_events or "").split(",")]
		gl = _gl_by_account([e for e in events if e])
		check("2: the settlement and its reversal net to zero on every account",
			gl and all(abs(v) < 0.01 for v in gl.values()), gl)

		# ---- 3 ------------------------------------------------------------------
		s2 = engine.close_period(year=old.year, month=old.month, sc=10, source=("Inventory Period", sp))
		check("3: the month settles again", s2 and s2.name != s1.name and flt(s2.es_var) == 200, s2 and s2.es_var)

		# ---- 4 ------------------------------------------------------------------
		pack.make_period(old.year, old.month, "SETTLED_FROZEN")
		msg = _refusal(lambda: frappe.get_doc("Inventory Period Settlement", s2.name).reverse())
		check("4: once the month is frozen, Reverse Settlement is refused", "still open" in msg, msg)
		pack.make_period(old.year, old.month, "PREV_OPEN_UNSETTLED")

		# ---- 5 ------------------------------------------------------------------
		msg = _refusal(lambda: engine.sett_reverse(s2.name, source=("Inventory Period", sp), entry_date=str(today)))
		check("5: an engine call with its own entry date keeps the calendar rule",
			"immediately-previous" in msg, msg)
	finally:
		frappe.db.rollback(save_point="sett_reverse_open_period")
