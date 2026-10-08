"""Standard cost revaluation posting date (client tickets STD-003 / STD-004,
ruled 01/10/2026). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_reval_date.run

The figures are the client's own UAT tests on badiav16 (Badia Cement):
  S05  ISCV-2026-00041  MTD, valid the current month, 70 -> 90, 150 received
       and 40 issued in the month: REV In 3,000 / REV out -800
  S12  ISCV-2026-00064  MTD, backdated (valid the previous month), 10 -> 12,
       61 received in the previous month: that month revalues on its day 1
       (REV In 122) and the revaluation reverses on day 1 of the current
       month, which keeps its standard - DR-57
Both posted on the release day on UAT; the rule is day 1 of the month the
revaluation posts in. A future version stays pending; a month whose period
cannot take the posting refuses the release and keeps an overnight
materialization pending.

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company
from periodic_valuation.tests.smoke_std import ensure_std_masters

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def _item(code):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)
	return code


class _ReleasedOn:
	"""Release as if it were `day` (the 15th of the month): on the 1st the
	old rule (release day) and the new one (day 1) give the same date and a
	date check proves nothing."""

	def __init__(self, day):
		self.day = str(day)

	def __enter__(self):
		import frappe.utils
		self._orig = frappe.utils.nowdate
		frappe.utils.nowdate = lambda: self.day
		return self

	def __exit__(self, *exc):
		import frappe.utils
		frappe.utils.nowdate = self._orig


def _version(company, item, when, sc, release=True, on=None):
	d = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company, "item_code": item,
		"valid_from_year": when.year, "valid_from_month": when.month, "standard_cost": sc,
		"source_type": "MANUAL_OVERRIDE"})
	d.insert(ignore_permissions=True)
	if release:
		if on:
			with _ReleasedOn(on):
				d.release()
		else:
			d.release()
	return frappe.get_doc(d.doctype, d.name)


def _triplet(version):
	return {e.std_trans: (flt(e.total_sc, 2), getdate(e.posting_date)) for e in frappe.get_all(
		"Inventory Valuation Event",
		filters={"source_docname": version, "std_trans": ("in", ["Rev Beg", "REV In", "REV out"])},
		fields=["std_trans", "total_sc", "posting_date"])}


def _gl_dates(version):
	return set(frappe.get_all("GL Entry", filters={"voucher_no": version, "is_cancelled": 0}, pluck="posting_date"))


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	ensure_masters()
	company = get_company()
	ensure_std_masters(company)
	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	mid = add_days(day1, 14)  # the release "happens" on the 15th
	frappe.db.savepoint("std_reval_date")
	try:
		# the two open months: S12's backdated change needs the previous one
		for when, status in ((prev, "PREV_OPEN_UNSETTLED"), (day1, "OPEN")):
			name = frappe.db.get_value("Inventory Period",
				{"company": company, "period_year": when.year, "period_month": when.month})
			if not name:
				doc = frappe.get_doc({"doctype": "Inventory Period", "company": company, "start_date": str(when)})
				doc.flags.ignore_validate = True
				name = doc.insert(ignore_permissions=True).name
			frappe.db.set_value("Inventory Period", name, "status", status, update_modified=False)
		# ---- S05: current month, 70 -> 90, 150 in / 40 out ----------------
		s05 = _item("_STD-S05-REVDATE")
		v70 = _version(company, s05, prev, 70)
		eng = StdEngine(company, s05)
		src = ("Item Standard Cost Version", v70.name)
		eng.post(trans="Rec", qty=150, sc=70, ac=70, posting_date=str(today), source=src)
		eng.post(trans="Iss", qty=40, sc=70, posting_date=str(today), source=src)
		v90 = _version(company, s05, today, 90, on=mid)
		t = _triplet(v90.name)
		check("S05 (client ISCV-2026-00041): REV In 3,000 and REV out -800",
			t.get("REV In", (None,))[0] == 3000.00 and t.get("REV out", (None,))[0] == -800.00, str(t))
		check("S05: the revaluation is dated day 1 of the month, not the release day",
			{d for _, d in t.values()} == {day1} and _gl_dates(v90.name) == {day1},
			f"{t} / GL {_gl_dates(v90.name)}")

		# ---- S12: backdated (valid last month), 10 -> 12, 61 on hand -------
		s12 = _item("_STD-S12-REVDATE")
		v10 = _version(company, s12, prev, 10)
		e12 = StdEngine(company, s12)
		e12.post(trans="Rec", qty=61, sc=10, ac=10, posting_date=str(add_days(day1, -1)),
			source=("Item Standard Cost Version", v10.name))
		v12 = _version(company, s12, prev, 12, on=mid)
		ev = sorted((e.std_trans, flt(e.total_sc, 2), getdate(e.posting_date)) for e in frappe.get_all(
			"Inventory Valuation Event", filters={"source_docname": v12.name, "is_cancelled": 0},
			fields=["std_trans", "total_sc", "posting_date"]))
		check("S12 (client ISCV-2026-00064): the previous month revalues on its day 1 (REV In 122)",
			("REV In", 122.0, get_first_day(prev)) in ev, str(ev))
		check("S12: reversed on day 1 of the current period, which keeps its standard (DR-57)",
			ev == sorted([("REV In", 122.0, get_first_day(prev)), ("Rev Reverse", -122.0, day1)])
			and _gl_dates(v12.name) == {get_first_day(prev), day1},
			f"{ev} / GL {_gl_dates(v12.name)}")

		# ---- a future version cannot be released (client, 06/10/2026, DR-55) --
		nxt = add_months(day1, 1)
		future = _version(company, s05, nxt, 95, release=False)
		refused = False
		try:
			future.release()
		except frappe.ValidationError:
			frappe.clear_last_message()
			refused = True
		check("a next-month version cannot be released", refused)
		# one released ahead before that ruling stays pending until its month
		from periodic_valuation.tests.uat_std_pack import release_or_pend

		v_next = release_or_pend(future)
		check("a version released ahead earlier stays pending, nothing posted",
			not v_next.revaluation_posted and not _triplet(v_next.name), str(_triplet(v_next.name)))

		# ---- no open period for the month: refused / kept pending ----------
		period = frappe.db.get_value("Inventory Period",
			{"company": company, "period_year": today.year, "period_month": today.month}, ["name", "status"], as_dict=True)
		frappe.db.set_value("Inventory Period", period.name, "status", "SETTLED_FROZEN", update_modified=False)
		s09 = _item("_STD-S09-REVDATE")
		_version(company, s09, prev, 250)
		StdEngine(company, s09).post(trans="Rec", qty=10, sc=250, ac=250, posting_date=str(add_days(day1, -1)),
			source=("Item Standard Cost Version", frappe.db.get_value("Item Standard Cost Version", {"item_code": s09}, "name")))
		draft = _version(company, s09, prev, 270, release=False)
		refusal = ""
		try:
			draft.release()
		except frappe.ValidationError as e:
			frappe.clear_last_message()
			refusal = str(e)
		check("a release into a month whose period cannot take it is refused, nothing posted",
			bool(refusal) and not _gl_dates(draft.name), refusal[:90])

		from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
			materialize_pending_revaluations,
		)
		frappe.db.set_value("Item Standard Cost Version", v_next.name,
			{"valid_from_year": today.year, "valid_from_month": today.month}, update_modified=False)
		materialize_pending_revaluations()
		still = frappe.db.get_value("Item Standard Cost Version", v_next.name, "revaluation_posted")
		check("the overnight job keeps it pending while the period is closed (no GL without a period)",
			not still and not _gl_dates(v_next.name), f"posted={still}")
		frappe.db.set_value("Inventory Period", period.name, "status", period.status, update_modified=False)
	finally:
		frappe.db.rollback(save_point="std_reval_date")

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD revaluation date failures: " + "; ".join(c[0] for c in failed))
