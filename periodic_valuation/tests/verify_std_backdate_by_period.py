"""A Backdate Transaction is one dated in a PREV_OPEN_UNSETTLED period, not one
dated in an earlier calendar month; the current period is the OPEN Inventory
Period (client 08/10/2026, DR-64). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_backdate_by_period.run

The client's example: the calendar is in month M but period M has not been
created, so the current period is M-1 (OPEN) and the previous one M-2
(PREV_OPEN_UNSETTLED). Under each Revaluation Posting Date option:

  1  the current period's day is held inside the OPEN period (the calendar
     day under it; its last day while the calendar is past it)
  2  a receipt dated in M-1 (OPEN, an earlier calendar month) posts plain
     ("Rec"), no companion
  3  a receipt dated in M-2 (PREV_OPEN_UNSETTLED) is a Backdate Transaction:
     REC (BD) at M-2's standard, its companion dated day 1 of M-1 ("First
     day") or M-1's last day ("Latest day", the newest day the current period
     has)
  4  a cost change for M-1 is a current-period change: its triplet in M-1,
     no correction, no reversal
  5  a cost change for M-2 is a backdated correction: M-2 revalued, the
     reversal dated day 1 of M-1 / M-1's last day (DR-59)
  6  a cost change for M is refused - M is after the current period
  7  a posting dated in M is refused - period M does not exist and cannot
     open while M-2 is unsettled (DR-37)

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


def _events(source):
	return sorted((e.std_trans, flt(e.total_sc, 2), str(e.posting_date))
		for e in frappe.get_all("Inventory Valuation Event",
			filters={"source_docname": source, "is_cancelled": 0},
			fields=["std_trans", "total_sc", "posting_date"]))


def _release(item, when, sc):
	doc = frappe.get_doc({"doctype": SCV, "company": pack.COMPANY, "item_code": item,
		"valid_from_year": when.year, "valid_from_month": when.month, "standard_cost": sc,
		"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
	doc.release()
	return doc


def _refused(fn):
	frappe.db.savepoint("bdp_refusal")
	try:
		fn()
		return False
	except frappe.ValidationError:
		frappe.clear_last_message()
		return True
	finally:
		frappe.db.rollback(save_point="bdp_refusal")


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD backdate-by-period failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.shared.periods import current_period_day

	today = getdate(nowdate())
	m = get_first_day(today)
	cur = add_months(m, -1)                       # OPEN, an earlier calendar month
	prev = add_months(m, -2)                      # PREV_OPEN_UNSETTLED
	cur_last = get_last_day(cur)
	frappe.db.savepoint("std_bd_period")
	try:
		wh, _wh2 = pack.ensure_company()
		# period M not created yet: the machine stands one month behind the calendar
		for name in frappe.get_all("Inventory Period", filters={"company": pack.COMPANY,
				"start_date": (">=", str(m))}, pluck="name"):
			frappe.db.delete("Inventory Period", name)
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(cur.year, cur.month, "OPEN")

		check("1: the current period's day is the OPEN period's last day while the calendar is past it",
			current_period_day(pack.COMPANY) == cur_last
			and current_period_day(pack.COMPANY, add_days(cur, 4)) == add_days(cur, 4)
			and current_period_day(pack.COMPANY, add_days(prev, 4)) == cur,
			str(current_period_day(pack.COMPANY)))

		for tag, mode in MODES:
			frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
				"revaluation_posting_date", mode)
			entry = str(cur_last) if tag == "Latest" else str(cur)      # the current period's entry day

			it = pack.std_item(f"_STD-BDP-{tag}")
			_release(it, prev, 300)
			pack.make_pr(it, wh, 100, 300, posting_date=str(add_days(prev, 2)))
			_release(it, cur, 320)

			pr = pack.make_pr(it, wh, 10, 320, posting_date=str(add_days(cur, 3)))
			check(f"{tag} 2: a receipt dated in the OPEN period (an earlier calendar month) posts plain",
				_events(pr.name) == [("Rec", 3200.0, str(add_days(cur, 3)))], str(_events(pr.name)))

			bd = pack.make_pr(it, wh, 5, 300, posting_date=str(add_days(prev, 5)))
			check(f"{tag} 3: a receipt dated in the PREV_OPEN_UNSETTLED period is a Backdate Transaction, its companion on {entry}",
				_events(bd.name) == sorted([("REC (BD)", 1500.0, str(add_days(prev, 5))),
					("REC (BD) - Rev", 100.0, entry)]), str(_events(bd.name)))

			v4 = frappe.get_all(SCV, filters={"item_code": it, "valid_from_month": cur.month,
				"valid_from_year": cur.year, "status": "RELEASED"}, fields=["name", "effective_to", "is_correction"])
			ev4 = _events(v4[0].name) if v4 else []
			check(f"{tag} 4: a change for the OPEN period is a current-period change on {entry}, no correction",
				v4 and not v4[0].effective_to and not v4[0].is_correction
				and ("Rev Beg", 2000.0, entry) in ev4 and not [x for x in ev4 if x[0] == "Rev Reverse"], str(ev4))

			c = pack.std_item(f"_STD-BDP-COR-{tag}")
			_release(c, prev, 300)
			pack.make_pr(c, wh, 100, 300, posting_date=str(add_days(prev, 2)))
			v5 = _release(c, prev, 400)
			ev5 = _events(v5.name)
			check(f"{tag} 5: a change for the PREV_OPEN_UNSETTLED period corrects it; the reversal on {entry}",
				frappe.db.get_value(SCV, v5.name, "is_correction") == 1
				and ("Rev Reverse", -10000.0, entry) in ev5
				and all(getdate(x[2]) <= getdate(get_last_day(prev)) for x in ev5 if x[0] != "Rev Reverse"), str(ev5))

			check(f"{tag} 6: a change for the calendar month after the current period is refused",
				_refused(lambda: _release(c, m, 450)))
			check(f"{tag} 7: a posting dated in the calendar month after the current period is refused",
				_refused(lambda: pack.make_pr(c, wh, 1, 300, posting_date=str(today))))
	finally:
		frappe.db.rollback(save_point="std_bd_period")
