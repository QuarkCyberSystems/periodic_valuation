"""Standard-cost event reasons, settlement split and actual cost after
settlement (client tickets STD-006 / STD-007 / STD-008 / STD-009). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_settlement_detail.run

  1  every event's reason names its movement: receipt, issue,
     purchase_return, sales_return, revaluation, backdate_bridge,
     cancellation - none reads "std_event"
  2  the settlement row carries its split: PPV and revaluation to ending
     stock and to consumption, adding up to the settlement's shares
  3  its carry into the next month (Sett - Rev) carries the ending-stock
     split with the opposite sign; a Sett-Reverse negates what it reverses
  4  the settlement and the period balance show ending stock at actual and
     the actual cost per unit; a Sett-Reverse clears the balance's
  5  a Settlement Run reports an item once, however many warehouses its
     events name
  6  the backfill patch recodes and splits rows written before

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
IVE = "Inventory Valuation Event"


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
		raise Exception("STD settlement detail failures: " + "; ".join(c[0] for c in failed))


def _reasons(item):
	return {(e.std_trans, bool(e.reversal_of)): e.reason_code for e in frappe.get_all(IVE,
		filters={"item_code": item, "company": pack.COMPANY}, fields=["std_trans", "reversal_of", "reason_code"])}


def _ret(doctype, name, qty):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	r = make_return_doc(doctype, name)
	r.items[0].qty = -qty
	r.insert(ignore_permissions=True)
	r.submit()


def _run():
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	today = getdate(nowdate())
	if today.day < 3:
		print("SKIP: needs two days of the current month before today")
		return
	day1 = get_first_day(today)
	o2 = add_days(day1, 1)
	prev = add_months(day1, -1)
	frappe.db.savepoint("std_sett_detail")
	try:
		wh, wh2 = pack.ensure_company()
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)

		# ---- 1: reasons -------------------------------------------------------
		it = pack.std_item("_STD-DETAIL")
		pack.scv_release(it, prev.year, prev.month, 10)
		pack.make_pr(it, wh, 100, 12, posting_date=str(add_days(prev, 2)))   # PPV 200
		pack.make_dn(it, wh, 40, posting_date=str(add_days(prev, 3)))
		pack.scv_release(it, today.year, today.month, 11)
		bd = pack.make_pr(it, wh, 10, 13, posting_date=str(add_days(prev, 4)))  # backdated + bridge
		r = pack.make_pr(it, wh, 20, 12, posting_date=str(o2))
		_ret("Purchase Receipt", r.name, 5)
		d = pack.make_dn(it, wh, 6, posting_date=str(o2))
		_ret("Delivery Note", d.name, 2)
		cx = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", bd.name))
		cx.posting_date, cx.posting_time = nowdate(), "23:00:00"
		cx.save(); cx.submit()
		rs = _reasons(it)
		want = {("Rec", False): "receipt", ("Iss", False): "issue", ("PR", False): "purchase_return",
			("SR", False): "sales_return", ("REC (BD)", False): "receipt", ("REC (BD) - Rev", False): "backdate_bridge",
			("REV In", False): "revaluation", ("Rev Beg", False): "revaluation", ("REC (BD)", True): "cancellation"}
		check("1: every event's reason names its movement; none reads std_event",
			all(rs.get(k) == v for k, v in want.items() if k in rs) and "std_event" not in rs.values()
			and len([k for k in want if k in rs]) >= 7, str(rs))

		# ---- 2-4: settle the previous month ---------------------------------------
		sett = StdEngine(pack.COMPANY, it).close_period(year=prev.year, month=prev.month, sc=10,
			source=("Inventory Period", pp))
		ev = frappe.get_doc(IVE, sett.sett_event)
		carry = frappe.get_doc(IVE, sett.sett_rev_event)
		check("2: the settlement row carries its PPV / revaluation split, ending stock and consumption",
			(flt(ev.sett_ppv_es, 2), flt(ev.sett_rev_es, 2), flt(ev.sett_ppv_cons, 2), flt(ev.sett_rev_cons, 2))
			== (flt(sett.ppv_es, 2), flt(sett.rev_es, 2), flt(sett.ppv_cons, 2), flt(sett.rev_cons, 2))
			and flt(ev.sett_ppv_es + ev.sett_rev_es, 2) == flt(sett.es_var, 2) and flt(sett.ppv_pool) != 0,
			f"{ev.sett_ppv_es}/{ev.sett_rev_es}/{ev.sett_ppv_cons}/{ev.sett_rev_cons} vs {sett.as_dict()}")
		check("3: the carry into the next month carries the ending-stock split with the opposite sign",
			flt(carry.sett_ppv_es, 2) == -flt(sett.ppv_es, 2) and flt(carry.sett_rev_es, 2) == -flt(sett.rev_es, 2)
			and flt(carry.sett_ppv_cons) == 0 and carry.reason_code == "settlement_carry" and ev.reason_code == "settlement")
		es_value = flt(flt(sett.es_qty) * 10 + flt(sett.es_var), 2)
		ipb = frappe.db.get_value("Inventory Period Balance", {"settlement": sett.name},
			["actual_value_after_settlement", "actual_unit_cost_after_settlement"], as_dict=True)
		check("4: settlement and balance show ending stock at actual and its unit cost",
			flt(sett.es_actual_value, 2) == es_value
			and abs(flt(sett.es_actual_unit_cost) - es_value / flt(sett.es_qty)) < 1e-6
			and ipb and flt(ipb.actual_value_after_settlement, 2) == es_value,
			f"{sett.es_actual_value}/{sett.es_actual_unit_cost} vs {es_value}; ipb {ipb}")
		frappe.get_doc("Inventory Period Settlement", sett.name).reverse()
		rev_rows = frappe.db.get_value("Inventory Period Settlement", sett.name, "reversed_by_events").split(",")
		r0 = frappe.get_doc(IVE, rev_rows[0])
		bal = frappe.db.get_value("Inventory Period Balance", {"company": pack.COMPANY, "item_code": it,
			"period_year": prev.year, "period_month": prev.month}, ["actual_value_after_settlement", "settlement"], as_dict=True)
		check("3/4: a Sett-Reverse negates the split and clears the balance's actual figures",
			flt(r0.sett_ppv_es, 2) == -flt(sett.ppv_es, 2) and flt(r0.sett_ppv_cons, 2) == -flt(sett.ppv_cons, 2)
			and r0.reason_code == "settlement_reverse" and not bal.settlement and not flt(bal.actual_value_after_settlement),
			f"{r0.as_dict()} {bal}")

		# ---- 5: run reports an item once ----------------------------------------
		two = pack.std_item("_STD-DETAIL-2WH")
		pack.scv_release(two, prev.year, prev.month, 10)
		pack.make_pr(two, wh, 10, 12, posting_date=str(add_days(prev, 2)))
		pack.make_pr(two, wh2, 10, 12, posting_date=str(add_days(prev, 3)))
		pack.make_pr(two, wh, 3, 12, posting_date=str(o2))
		run_doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": pack.COMPANY,
			"period_year": today.year, "period_month": today.month, "run_type": "INITIAL_CLOSE", "item_code": two})
		run_doc.insert(ignore_permissions=True)
		run_doc.submit()
		run_doc.reload()
		check("5: a run lists a company-level item once (refused: previous month first)",
			len([ln for ln in (run_doc.remarks or "").splitlines() if "_STD-DETAIL-2WH" in ln]) == 1, run_doc.remarks)

		# ---- 6: backfill patch ----------------------------------------------------
		from periodic_valuation.patches.v1_0 import std_event_reasons_and_settlement_detail as patch

		frappe.db.sql("UPDATE `tabInventory Valuation Event` SET reason_code = 'std_event' WHERE item_code = %s AND std_trans IN ('Rec', 'PR')", it)
		frappe.db.set_value(IVE, rev_rows[0], {"sett_ppv_es": 0, "sett_ppv_cons": 0}, update_modified=False)
		patch.execute()
		rs = _reasons(it)
		check("6: the patch recodes old events and splits old settlement rows",
			rs.get(("Rec", False)) == "receipt" and rs.get(("PR", False)) == "purchase_return"
			and flt(frappe.db.get_value(IVE, rev_rows[0], "sett_ppv_es"), 2) == -flt(sett.ppv_es, 2), str(rs))
	finally:
		frappe.db.rollback(save_point="std_sett_detail")
