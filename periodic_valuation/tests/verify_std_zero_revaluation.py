"""A standard cost change with nothing to revalue still shows in the
valuation log (client ticket STD-010), and the version form names the
version that replaces it (STD-011). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_zero_revaluation.run

  A  First day of the period: a change for an item with no stock and no
     movement records one Rev Beg of zero amount on day 1, linked to the
     version (old and new cost on it), with no GL and no stock-ledger row;
     the form says it was recorded with zero value and offers the events
  B  Latest day of the period: the same change, backdated (a correction of
     that month, DR-57), records one zero Rev Beg on that month's last day
     and no reversal; the earlier version stays RELEASED
  C  an item's first cost records one zero-value event (client, 06/10/2026,
     DR-55); an unchanged cost records none
  D  the zero events leave the period-close gates green (event / GL
     identity, orphan events, settlement gate) and the settlement treats
     the scope as having nothing to settle

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
SCV = "Item Standard Cost Version"


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def _set(value):
	frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY}, "revaluation_posting_date", value)


def _events(version):
	return frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": version, "is_cancelled": 0},
		fields=["name", "std_trans", "total_sc", "qty_adj", "value_delta", "standard_cost",
			"actual_cost", "cost_version", "posting_date"])


def _ledger_rows(version):
	gl = frappe.db.count("GL Entry", {"voucher_no": version, "is_cancelled": 0})
	sle = frappe.db.count("Stock Ledger Entry", {"voucher_no": version, "is_cancelled": 0})
	return gl, sle


def _onload(version):
	doc = frappe.get_doc(SCV, version)
	doc.onload()
	return doc.get("__onload") or {}


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.shared.period_close import (
		assert_event_gl_identity,
		assert_no_orphans,
		assert_std_scopes_settled,
	)

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_days(day1, -1)
	frappe.db.savepoint("std_zero_reval")
	try:
		pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")

		# ---- A: first day, no stock, no movement --------------------------
		_set("First day of the period")
		a = pack.std_item("_STD-ZERO-A")
		pack.scv_release(a, prev.year, prev.month, 100)
		v_a = pack.scv_release(a, today.year, today.month, 120)
		ev = _events(v_a.name)
		check("A: one zero Rev Beg on day 1, linked to the version",
			len(ev) == 1 and ev[0].std_trans == "Rev Beg" and flt(ev[0].total_sc) == 0
			and getdate(ev[0].posting_date) == day1 and ev[0].cost_version == v_a.name, str(ev))
		check("A: it carries the old and the new cost (100 -> 120) and no quantity or value",
			ev and flt(ev[0].actual_cost) == 100 and flt(ev[0].standard_cost) == 120
			and flt(ev[0].qty_adj) == 0 and flt(ev[0].value_delta) == 0, str(ev))
		check("A: no GL and no stock-ledger row", _ledger_rows(v_a.name) == (0, 0), str(_ledger_rows(v_a.name)))
		check("A: the version is marked revalued",
			frappe.db.get_value(SCV, v_a.name, "revaluation_posted"))
		rev = _onload(v_a.name).get("revaluation", {})
		check("A: the form reports a zero revaluation with its event",
			rev.get("reason") == "nothing_to_revalue" and rev.get("events") == 1, str(rev))

		# ---- B: date of release, no stock, no movement --------------------
		_set("Latest day of the period")
		b = pack.std_item("_STD-ZERO-B")
		v_b_old = pack.scv_release(b, prev.year, prev.month, 150)
		v_b = pack.scv_release(b, prev.year, prev.month, 180)  # backdated, as ISCV-2026-00096
		v_b.reload()
		ev = _events(v_b.name)
		check("B: one zero Rev Beg on the corrected month's last day, with the old and the new cost",
			len(ev) == 1 and ev[0].std_trans == "Rev Beg" and flt(ev[0].total_sc) == 0
			and getdate(ev[0].posting_date) == prev and flt(ev[0].actual_cost) == 150
			and flt(ev[0].standard_cost) == 180, str(ev))
		check("B: no GL and no stock-ledger row", _ledger_rows(v_b.name) == (0, 0), str(_ledger_rows(v_b.name)))
		rev = _onload(v_b.name).get("revaluation", {})
		check("B: the form reports a zero revaluation with its event",
			rev.get("reason") == "nothing_to_revalue" and rev.get("events") == 1, str(rev))
		check("B: the current month keeps 150 through its inherited version (DR-65)",
			frappe.db.get_value(SCV, {"item_code": b, "source_type": "INHERITED", "status": "RELEASED"},
				"standard_cost") == 150)

		# ---- C: first cost, unchanged cost --------------------------------
		_set("First day of the period")
		c = pack.std_item("_STD-ZERO-C")
		v_c1 = pack.scv_release(c, today.year, today.month, 40)
		ev = _events(v_c1.name)
		check("C: an item's first cost records one zero-value event on day 1, no GL",
			len(ev) == 1 and flt(ev[0].total_sc) == 0 and getdate(ev[0].posting_date) == day1
			and _ledger_rows(v_c1.name) == (0, 0), str(ev))
		d = pack.std_item("_STD-ZERO-D")
		pack.scv_release(d, prev.year, prev.month, 60)
		v_same = pack.scv_release(d, today.year, today.month, 60)
		check("C: an unchanged cost records no event", not _events(v_same.name))

		# ---- D: period-close gates and settlement -------------------------
		period = frappe.get_doc("Inventory Period", cur_period)
		check("D: inventory GL equals the valuation events for the month",
			assert_event_gl_identity(period)["ok"])
		orphans = assert_no_orphans(period)
		check("D: the zero events are not orphan events",
			orphans["no_orphan_events"], str(orphans["detail"]))
		gate = assert_std_scopes_settled(period)
		check("D: the settlement gate does not ask for the zero-only scopes",
			not [u for u in gate["unsettled"] if u["item_code"] in (a, b)], str(gate["unsettled"]))
		try:
			StdEngine(pack.COMPANY, a).close_period(year=today.year, month=today.month, sc=120,
				source=("Inventory Period", cur_period))
			nothing = False
		except frappe.ValidationError as e:
			nothing = "Nothing to settle" in str(e)
			frappe.clear_last_message()
		check("D: the settlement treats a zero-only scope as having nothing to settle", nothing)
	finally:
		frappe.db.rollback(save_point="std_zero_reval")

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD zero revaluation failures: " + "; ".join(c[0] for c in failed))
