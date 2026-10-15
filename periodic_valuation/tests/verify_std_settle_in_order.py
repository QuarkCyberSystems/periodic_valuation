"""Which month can be settled (DR-69; SAP: period-end settlement runs after
the period shift) and in which order (client ticket STD-016). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_settle_in_order.run

  1  the OPEN month cannot be settled - engine, Settlement Run, Settle This
     Item - its carry would land in a month with no period
  2  once the next month is open (the month is previous-open) it settles,
     and its carry lands in the open month's balance
  3  a frozen month cannot be settled

STD-016's own refusal (a month whose previous month is unsettled for the
item) stays in the engine as a backstop; under the period machine it cannot
arise any more: the month before a previous-open month is frozen, and the
freeze requires its items settled (DR-47).

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
		raise Exception("STD settle-in-order failures: " + "; ".join(c[0] for c in failed))


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
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	nxt = add_months(day1, 1)
	frappe.db.savepoint("std_settle_order")
	try:
		wh, _ = pack.ensure_company()
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cp = pack.make_period(today.year, today.month, "OPEN")
		it = pack.std_item("_STD-SIO")
		pack.scv_release(it, prev.year, prev.month, 10)
		pack.make_pr(it, wh, 100, 12, posting_date=str(add_days(prev, 2)))
		pack.make_pr(it, wh, 10, 11, posting_date=str(day1))
		engine = StdEngine(pack.COMPANY, it)

		# ---- 1 ------------------------------------------------------------------
		msg = _refusal(lambda: engine.close_period(year=today.year, month=today.month, sc=10,
			source=("Inventory Period", cp)))
		check("1: the OPEN month cannot be settled (engine)", "is settled once" in msg, msg)
		msg = _refusal(lambda: frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": pack.COMPANY,
			"period_year": today.year, "period_month": today.month, "run_type": "INITIAL_CLOSE"}).insert(ignore_permissions=True))
		check("1: the OPEN month cannot be settled (Settlement Run)", "is settled once" in msg, msg)
		row = frappe.get_doc("Inventory Period Balance", frappe.db.get_value("Inventory Period Balance",
			{"company": pack.COMPANY, "item_code": it, "period_year": today.year, "period_month": today.month}))
		check("1: Settle This Item is not offered for the OPEN month", not row.settlement_state()["postable"])

		# ---- 2 ------------------------------------------------------------------
		sett = engine.close_period(year=prev.year, month=prev.month, sc=10, source=("Inventory Period", pp))
		carry = frappe.db.get_value("Inventory Valuation Event", sett.sett_rev_event, ["posting_date", "total_sc"], as_dict=True)
		check("2: the previous-open month settles; its carry is dated the open month's day 1",
			sett and str(carry.posting_date) == str(day1) and flt(carry.total_sc) == -flt(sett.es_var))
		pack.open_month_after(today)          # Open Next Period: the current month becomes previous-open
		sett2 = engine.close_period(year=today.year, month=today.month, sc=10, source=("Inventory Period", cp))
		nrow = frappe.db.get_value("Inventory Period Balance", {"company": pack.COMPANY, "item_code": it,
			"period_year": nxt.year, "period_month": nxt.month}, ["reval_value", "closing_value"], as_dict=True)
		check("2: once the next month is open the month settles, and its carry lands in that month's balance",
			sett2 and nrow and abs(flt(nrow.reval_value) + flt(sett2.es_var)) < 0.01, f"{nrow} es {sett2.es_var}")

		# ---- 3 ------------------------------------------------------------------
		frozen = add_months(day1, -2)
		pack.make_period(frozen.year, frozen.month, "SETTLED_FROZEN")
		msg = _refusal(lambda: engine.close_period(year=frozen.year, month=frozen.month, sc=10,
			source=("Inventory Period", cp)))
		check("3: a frozen month cannot be settled", "is settled once" in msg or "settled" in msg, msg)

	finally:
		frappe.db.rollback(save_point="std_settle_order")
