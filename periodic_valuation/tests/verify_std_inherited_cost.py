"""Each open period carries its own standard cost version, inherited from the
cost in force at its start (client, 08/10/2026, "Clarification of Standard
Cost Version"; DR-65). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_inherited_cost.run

Previous month P (PREV_OPEN_UNSETTLED), current month C (OPEN):

  1  scenario 2: a cost for P released while C is open - C inherits it at
     once (same cost, source INHERITED, nothing posted)
  2  scenario 2: after movements in P, P is corrected 1,000 -> 2,000: P is
     revalued, the revaluation reverses in C (dated by the option), C's
     inherited version is untouched and still prices C at 1,000; P's
     original version, which no day resolves to any more, is SUPERSEDED
  3  the correction warns that C keeps its cost
  4  scenario 3: a first cost for C creates nothing for P
  5  scenario 1: the period roll - when the next month opens, it inherits
     C's cost; a month that already has its own version inherits nothing
  6  a manual version for C replaces the inherited one, revalued from it

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
SCV = "Item Standard Cost Version"


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
		raise Exception("STD inherited cost failures: " + "; ".join(c[0] for c in failed))


def _versions(item):
	return frappe.get_all(SCV, filters={"company": pack.COMPANY, "item_code": item},
		fields=["name", "valid_from_year", "valid_from_month", "standard_cost", "source_type", "status",
			"effective_from", "effective_to", "supersedes_version"], order_by="creation asc")


def _sc(item, day):
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	return flt(get_active_standard_cost(pack.COMPANY, item, None, day).standard_cost)


def _events(source):
	return sorted((e.std_trans, flt(e.total_sc, 2), str(e.posting_date))
		for e in frappe.get_all("Inventory Valuation Event", filters={"source_docname": source, "is_cancelled": 0},
			fields=["std_trans", "total_sc", "posting_date"]))


def _release(item, when, sc):
	d = frappe.get_doc({"doctype": SCV, "company": pack.COMPANY, "item_code": item,
		"valid_from_year": when.year, "valid_from_month": when.month, "standard_cost": sc,
		"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
	d.release()
	return d


def _run():
	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	nxt = add_months(day1, 1)
	frappe.db.savepoint("std_inherit")
	try:
		wh, _wh2 = pack.ensure_company()
		for name in frappe.get_all("Inventory Period", filters={"company": pack.COMPANY,
				"start_date": (">=", str(nxt))}, pluck="name"):
			frappe.db.delete("Inventory Period", name)
		pack.make_period(add_months(day1, -2).year, add_months(day1, -2).month, "SETTLED_FROZEN")
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cp = pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
			"revaluation_posting_date", "First day of the period")

		# ---- scenario 2 -------------------------------------------------------
		it = pack.std_item("_STD-INHERIT-S2")
		v1000 = _release(it, prev, 1000)
		inh = [x for x in _versions(it) if x.source_type == "INHERITED"]
		check("1: the current month inherits the previous month's 1,000 at once",
			len(inh) == 1 and (inh[0].valid_from_year, inh[0].valid_from_month) == (today.year, today.month)
			and flt(inh[0].standard_cost) == 1000 and inh[0].status == "RELEASED"
			and inh[0].supersedes_version == v1000.name and not _events(inh[0].name),
			str(_versions(it)))
		check("1: the previous version now ends with its month",
			str(frappe.db.get_value(SCV, v1000.name, "effective_to")) == str(add_days(day1, -1)))

		pack.make_pr(it, wh, 100, 1000, posting_date=str(add_days(prev, 2)))
		pack.make_dn(it, wh, 20, posting_date=str(add_days(prev, 3)))
		frappe.message_log = []
		v2000 = _release(it, prev, 2000)
		warned = any("keeps its standard cost" in str(m) for m in frappe.message_log)
		check("2: the previous month is revalued, the revaluation reverses on day 1 of the current month",
			_events(v2000.name) == sorted([("REV In", 100000.0, str(prev)), ("REV out", -20000.0, str(prev)),
				("Rev Reverse", -80000.0, str(day1))]), str(_events(v2000.name)))
		check("2: the current month's inherited version is untouched and still prices 1,000",
			frappe.db.get_value(SCV, inh[0].name, ["status", "standard_cost"]) == ("RELEASED", 1000.0)
			and _sc(it, today) == 1000 and _sc(it, add_days(prev, 5)) == 2000)
		check("2: the original 1,000 version, which no day resolves to any more, is SUPERSEDED",
			frappe.db.get_value(SCV, v1000.name, "status") == "SUPERSEDED")
		check("3: the correction warns that the current month keeps its cost", warned, str(frappe.message_log)[:200])

		# ---- scenario 3 -------------------------------------------------------
		it3 = pack.std_item("_STD-INHERIT-S3")
		_release(it3, today, 3000)
		check("4: a first cost for the current month creates nothing for the previous month",
			[(x.valid_from_month, x.source_type) for x in _versions(it3)] == [(today.month, "MANUAL_OVERRIDE")],
			str(_versions(it3)))

		# ---- scenario 1: the period roll -----------------------------------------
		from periodic_valuation.shared.period_close import open_next_period

		it1 = pack.std_item("_STD-INHERIT-S1")
		_release(it1, today, 500)
		it1b = pack.std_item("_STD-INHERIT-S1B")
		_release(it1b, today, 600)
		frappe.db.set_value("Inventory Period", pp, "status", "SETTLED_FROZEN", update_modified=False)
		open_next_period(frappe.get_doc("Inventory Period", cp))
		nv = [x for x in _versions(it1) if (x.valid_from_year, x.valid_from_month) == (nxt.year, nxt.month)]
		check("5: when the next month opens it inherits the cost in force (500)",
			len(nv) == 1 and nv[0].source_type == "INHERITED" and flt(nv[0].standard_cost) == 500
			and _sc(it1, nxt) == 500, str(_versions(it1)))
		check("5: an item without a cost running into it inherits nothing (scenario 3 item still only its own month)",
			not [x for x in _versions(it3) if x.valid_from_month == prev.month], str(_versions(it3)))

		# ---- a manual version replaces the inherited one --------------------------
		inh_b = [x for x in _versions(it1b) if x.source_type == "INHERITED"][0]
		vm = _release(it1b, nxt, 650)
		check("6: a manual version replaces the inherited one, its delta measured from it",
			frappe.db.get_value(SCV, inh_b.name, "status") == "SUPERSEDED"
			and frappe.db.get_value(SCV, vm.name, "supersedes_version") == inh_b.name
			and _sc(it1b, nxt) == 650,
			f"{_events(vm.name)} {_versions(it1b)}")
	finally:
		frappe.db.rollback(save_point="std_inherit")
