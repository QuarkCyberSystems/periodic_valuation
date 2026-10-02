"""Standard cost revaluation on the last day of the period (client ticket
STD-003 follow-up, 01/10/2026: the posting date is chosen in the system
settings — first day or last day of the period). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_reval_last_day.run

With Periodic Standard Cost Settings > Revaluation Posting Date = "Last day
of the period":
  - a version valid from the current month books its revaluation on the
    month's last day — same amounts as the day-1 rule (S05: 70 -> 90, 150
    received and 40 issued: REV In 3,000 / REV out -800);
  - receipts and deliveries entered after the release but dated before that
    last day still submit, and the month's period balance and stock ledger
    both close at the new cost;
  - a backdated version (valid from the previous, still-open month) books
    on the last day of the current period;
  - the month's Settlement Run completes;
and with the setting back on "First day of the period" the revaluation is
dated day 1 again.

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, flt, get_first_day, get_last_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def _set(value):
	frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY}, "revaluation_posting_date", value)


def _triplet(version):
	return {e.std_trans: (flt(e.total_sc, 2), getdate(e.posting_date)) for e in frappe.get_all(
		"Inventory Valuation Event",
		filters={"source_docname": version, "std_trans": ("in", ["Rev Beg", "REV In", "REV out"])},
		fields=["std_trans", "total_sc", "posting_date"])}


def _gl_dates(version):
	return set(frappe.get_all("GL Entry", filters={"voucher_no": version, "is_cancelled": 0}, pluck="posting_date"))


def _sle_value(item, wh):
	return flt(frappe.db.sql(
		"select sum(stock_value_difference) from `tabStock Ledger Entry` where item_code=%s and warehouse=%s and is_cancelled=0",
		(item, wh))[0][0], 2)


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _run():
	today = getdate(nowdate())
	day1, last = get_first_day(today), get_last_day(today)
	prev = add_days(day1, -1)
	frappe.db.savepoint("std_reval_last")
	try:
		wh, _wh2 = pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(today.year, today.month, "OPEN")
		_set("Last day of the period")

		# ---- current month, 70 -> 90, 150 in / 40 out, then more after the release
		item = pack.std_item("_STD-LASTDAY-S05")
		pack.scv_release(item, prev.year, prev.month, 70)
		pack.make_pr(item, wh, 150, 70, posting_date=str(today))
		pack.make_dn(item, wh, 40, posting_date=str(today))
		v90 = pack.scv_release(item, today.year, today.month, 90)
		t = _triplet(v90.name)
		check("S05: REV In 3,000 and REV out -800, as under the day-1 rule",
			t.get("REV In", (None,))[0] == 3000.00 and t.get("REV out", (None,))[0] == -800.00, str(t))
		check("S05: the revaluation is booked on the last day of the month",
			{d for _, d in t.values()} == {last} and _gl_dates(v90.name) == {last},
			f"{t} / GL {_gl_dates(v90.name)}")
		check("S05: its stock-ledger value row is dated the last day too",
			set(frappe.get_all("Stock Ledger Entry", filters={"voucher_no": v90.name, "is_cancelled": 0},
				pluck="posting_date")) == {last})

		ok, detail = True, ""
		try:
			pack.make_pr(item, wh, 10, 90, posting_date=str(today))
			pack.make_dn(item, wh, 5, posting_date=str(today))
		except Exception as e:
			ok, detail = False, str(e)[:200]
		check("a receipt and a delivery dated before the revaluation still submit", ok, detail)
		bal = pack.ipb(item, today.year, today.month)
		expected = 115 * 90
		check("the month closes at the new cost: period balance 115 x 90",
			bal and flt(bal.closing_qty) == 115 and flt(bal.closing_value, 2) == expected, str(bal))
		check("the stock ledger agrees with the period balance", _sle_value(item, wh) == expected,
			f"SLE {_sle_value(item, wh)} vs {expected}")

		# ---- backdated: valid from the previous, still-open month
		b = pack.std_item("_STD-LASTDAY-S12")
		pack.scv_release(b, prev.year, prev.month, 10)
		pack.make_pr(b, wh, 61, 10, posting_date=str(prev))
		v12 = pack.scv_release(b, prev.year, prev.month, 12)
		t = _triplet(v12.name)
		check("S12: a backdated version books Rev Beg 122 on the current period's last day",
			t.get("Rev Beg", (None, None)) == (122.00, last) and _gl_dates(v12.name) == {last}, str(t))

		# ---- the month-end close still runs
		run1 = pack.settlement_run(prev.year, prev.month)
		run2 = pack.settlement_run(today.year, today.month)
		check("the Settlement Runs complete with the revaluation on the last day",
			run1.status == "Completed" and run2.status == "Completed", f"{run1.status} / {run2.status}")

		# ---- back to the first day
		_set("First day of the period")
		c = pack.std_item("_STD-LASTDAY-S01")
		pack.scv_release(c, prev.year, prev.month, 20)
		pack.make_pr(c, wh, 5, 20, posting_date=str(today))
		v25 = pack.scv_release(c, today.year, today.month, 25)
		t = _triplet(v25.name)
		check("with the setting on the first day, the revaluation is dated day 1",
			{d for _, d in t.values()} == {day1} and _gl_dates(v25.name) == {day1}, str(t))
	finally:
		frappe.db.rollback(save_point="std_reval_last")

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD revaluation last-day failures: " + "; ".join(c[0] for c in failed))
