"""An issue reversed or returned in a later period (DR-74, client email
22/10/2026, workbook "01 MTD STD Cost V3": rows SR - Futre / Rev Fut-Re), and
a backdated return bridged into the current period (DR-73). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_previous_period_returns.run

The client's own example, with the previous month as her September and the
current month as her October:
  receipt 10 at actual 180 / standard 120, issue 1, current standard 150;
  1  the issue returned in the current month posts SR - Future: Dr Stock
     150 / Cr COGS 120 / Cr Revaluation Reserve 30; Reversed From Previous
     Periods 1 on the current month, Reversed in Later Periods 1 on the
     previous one; Issue Qty untouched; the current Available Qty 9 + 1 = 10
  2  the previous month's standard changed 120 -> 170: Rev Fut-Re 50 on
     day 1 of the current month (Dr Reserve / Cr COGS Adjustment)
  3  the previous month settled at actual 180: Rev Fut-Re 10
  4  the current month settled: Available 10, total actual 1,800, 180 a unit,
     variance 300 all to ending stock; the returned unit's expense nets to
     zero over COGS and COGS Adjustment; the reconciliation gate is clean
  5  a Cancellation of an earlier month's issue is SR - Future too, and it
     cannot be made twice
  6  within the same month the reversal nets the issue, as before
  7  undoing the return gives everything back: quantity, both counts, the
     Rev Fut-Re rows
  8  YTD: the period is the fiscal year - a return within the year nets
  9  DR-73: a purchase return dated in the previous month is bridged into
     the current month's standard

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
		raise Exception("Previous-period return failures: " + "; ".join(c[0] for c in failed))


def _gl(voucher, a):
	rows = frappe.db.sql("""SELECT account, SUM(debit - credit) v FROM `tabGL Entry` WHERE voucher_no = %s
		AND is_cancelled = 0 GROUP BY account""", voucher, as_dict=True)
	return {k: flt(r.v, 2) for r in rows for k, acc in a.items() if acc == r.account and flt(r.v, 2)}


def _ipb(item, d):
	return frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["*"])[0]


def _events(item, trans):
	return frappe.get_all("Inventory Valuation Event", filters={"item_code": item, "std_trans": trans,
		"is_cancelled": 0}, fields=["name", "posting_date", "total_sc", "total_ac", "standard_cost", "actual_cost",
		"reversal_of", "reference_receipt_event", "qty_adj"], order_by="creation asc")


def _return(dn, qty, posting_date):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	ret = make_return_doc("Delivery Note", dn.name)
	for r in ret.items:
		r.qty = -qty
	ret.posting_date = str(posting_date)
	ret.set_posting_time = 1
	ret.insert(ignore_permissions=True)
	ret = frappe.get_doc("Delivery Note", ret.name)  # loaded as a return: core's return updates apply
	ret.submit()
	return ret


def _accounts():
	a = pack.acct()
	from periodic_valuation.periodic_standard_cost.engine import get_std_setting

	return {"stock": a.stock, "cogs": frappe.get_cached_value("Company", pack.COMPANY, "default_expense_account"),
		"cogs_adj": get_std_setting(pack.COMPANY, "cogs_adjustment_account"),
		"reserve": get_std_setting(pack.COMPANY, "std_reval_reserve_account"),
		"ppv": get_std_setting(pack.COMPANY, "ppv_account")}


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.shared.period_close import run_reconciliation_gate

	today = getdate(nowdate())
	cur = get_first_day(today)
	prev = add_months(cur, -1)
	# the company and its accounts are masters every section needs; each
	# section's own postings roll back to its savepoint
	wh, _ = pack.ensure_company()
	frappe.db.commit()
	a = _accounts()

	def setup():
		frappe.db.savepoint("ppr")
		periods = (pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED"),
			pack.make_period(cur.year, cur.month, "OPEN"))
		frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
			"revaluation_posting_date", "First day of the period")
		return periods

	try:
		pp, cp = setup()

		# ---- the client's example ------------------------------------------------
		it = pack.std_item("_PPR-MTD")
		pack.scv_release(it, prev.year, prev.month, 120)
		pack.make_pr(it, wh, 10, 180, posting_date=str(prev))
		dn = pack.make_dn(it, wh, 1, posting_date=str(add_days(prev, 4)))
		pack.scv_release(it, cur.year, cur.month, 150)
		ret = _return(dn, 1, add_days(cur, 4))
		sr = _events(it, "SR - Future")
		check("1: the return of the earlier month's issue posts SR - Future (inventory at 150, the issue's cost 120)",
			len(sr) == 1 and flt(sr[0].total_sc) == 150 and flt(sr[0].total_ac) == 120, str(sr))
		check("1: GL Dr Stock 150 / Cr COGS 120 / Cr Revaluation Reserve 30",
			_gl(ret.name, a) == {"stock": 150, "cogs": -120, "reserve": -30}, str(_gl(ret.name, a)))
		cr, pr_ = _ipb(it, cur), _ipb(it, prev)
		check("1: current month - Reversed From Previous Periods 1 (150), Issue Qty untouched, closing 10",
			flt(cr.reversed_from_previous_qty) == 1 and flt(cr.reversed_from_previous_value) == 150
			and flt(cr.issue_qty) == 0 and flt(cr.closing_qty) == 10 and flt(cr.closing_value) == 1500,
			f"{cr.reversed_from_previous_qty}/{cr.reversed_from_previous_value} iss {cr.issue_qty} close {cr.closing_qty}/{cr.closing_value}")
		check("1: previous month - Reversed in Later Periods 1, its closing unchanged (9)",
			flt(pr_.reversed_in_later_qty) == 1 and flt(pr_.closing_qty) == 9, f"{pr_.reversed_in_later_qty} {pr_.closing_qty}")
		engine = StdEngine(pack.COMPANY, it)
		check("1: current Available Qty = 9 + 0 + 1 = 10", flt(engine.settlement_basis(cur.year, cur.month)) == 10,
			str(engine.settlement_basis(cur.year, cur.month)))

		frappe.get_doc({"doctype": "Item Standard Cost Version", "company": pack.COMPANY, "item_code": it,
			"valid_from_year": prev.year, "valid_from_month": prev.month, "standard_cost": 170,
			"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True).release()
		fut = _events(it, "Rev Fut-Re")
		check("2: previous month 120 -> 170: Rev Fut-Re 50 on day 1 of the current month",
			len(fut) == 1 and flt(fut[0].total_ac) == 50 and getdate(fut[0].posting_date) == cur, str(fut))
		gl = frappe.db.sql("""SELECT account, SUM(debit - credit) v FROM `tabGL Entry` WHERE valuation_event_id = %s
			AND is_cancelled = 0 GROUP BY account""", fut[0].name, as_dict=True) if fut else []
		check("2: Dr Revaluation Reserve 50 / Cr COGS Adjustment 50, no stock",
			{r.account: flt(r.v) for r in gl} == {a["reserve"]: 50, a["cogs_adj"]: -50}, str(gl))

		engine.close_period(year=prev.year, month=prev.month, sc=170, source=("Inventory Period", pp))
		fut = _events(it, "Rev Fut-Re")
		check("3: previous month settled at actual 180: Rev Fut-Re 10 (170 -> 180)",
			len(fut) == 2 and flt(fut[1].total_ac) == 10 and flt(fut[1].actual_cost) == 170
			and flt(fut[1].standard_cost) == 180, str(fut))

		pack.open_month_after(cur)
		sett = engine.close_period(year=cur.year, month=cur.month, sc=150, source=("Inventory Period", cp))
		check("4: current month settled - Available 10, actual 1,800 (180 a unit), variance 300, all to ending stock",
			flt(sett.beg_qty) + flt(sett.in_qty) == 10 and flt(sett.total_ac) == 1800 and flt(sett.variance) == 300
			and flt(sett.es_var) == 300 and flt(sett.out_var) == 0,
			f"beg {sett.beg_qty} in {sett.in_qty} ac {sett.total_ac} var {sett.variance} es {sett.es_var} out {sett.out_var}")
		check("4: PPV pool 540, revaluation pool -240 (the workbook's)",
			flt(sett.ppv_pool) == 540 and flt(sett.rev_pool) == -240, f"{sett.ppv_pool} {sett.rev_pool}")
		vouchers = [dn.name, ret.name]
		exp = frappe.db.sql("""SELECT account, SUM(debit - credit) v FROM `tabGL Entry` g
			JOIN `tabInventory Valuation Event` e ON e.name = g.valuation_event_id
			WHERE e.item_code = %s AND g.is_cancelled = 0 AND g.account IN %s GROUP BY account""",
			(it, (a["cogs"], a["cogs_adj"])), as_dict=True)
		check("4: the returned unit's expense nets to zero (COGS, COGS Adjustment)",
			all(abs(flt(r.v)) < 0.01 for r in exp), str(exp))
		gates = {p: flt(run_reconciliation_gate(frappe.get_doc("Inventory Period", p))["discrepancy"], 2)
			for p in frappe.get_all("Inventory Period", filters={"company": pack.COMPANY,
				"status": ("in", ("OPEN", "PREV_OPEN_UNSETTLED"))}, pluck="name")}
		check("4: the reconciliation gate is clean in the open months", all(v == 0 for v in gates.values()), str(gates))
		frappe.db.rollback(save_point="ppr")

		# ---- 5: Cancellation ---------------------------------------------------------
		setup()
		it = pack.std_item("_PPR-CANCEL")
		pack.scv_release(it, prev.year, prev.month, 120)
		pack.make_pr(it, wh, 10, 120, posting_date=str(prev))
		dn = pack.make_dn(it, wh, 2, posting_date=str(add_days(prev, 4)))
		pack.scv_release(it, cur.year, cur.month, 150)
		c = frappe.get_doc("Delivery Note", make_cancellation("Delivery Note", dn.name))
		c.submit()
		sr = _events(it, "SR - Future")
		check("5: a Cancellation in the current month of an earlier month's issue is SR - Future, linked to the issue",
			len(sr) == 1 and flt(sr[0].qty_adj) == 2 and bool(sr[0].reference_receipt_event), str(sr))
		check("5: GL Dr Stock 300 / Cr COGS 240 / Cr Reserve 60", _gl(c.name, a) == {"stock": 300, "cogs": -240, "reserve": -60},
			str(_gl(c.name, a)))
		try:
			make_cancellation("Delivery Note", dn.name)
			check("5: a second Cancellation is refused", False, "made")
		except frappe.ValidationError:
			frappe.clear_last_message()
			check("5: a second Cancellation is refused", True)

		# ---- 6: same month nets -------------------------------------------------------
		dn2 = pack.make_dn(it, wh, 1, posting_date=str(add_days(cur, 1)))
		_return(dn2, 1, add_days(cur, 2))
		row = _ipb(it, cur)
		check("6: within the month the return nets the issue (Issue Qty 0), no SR - Future",
			flt(row.issue_qty) == 0 and len(_events(it, "SR - Future")) == 1, f"iss {row.issue_qty}")
		frappe.db.rollback(save_point="ppr")

		# ---- 7: undoing the return ------------------------------------------------------
		# the Cancellation of a return is an un-return (DR-72, TSK-2026-00952);
		# before it, core refused a full return's Cancellation as a further return
		from periodic_valuation.periodic_moving_average import cancellation as _canc

		if not hasattr(_canc, "_un_return"):
			print("SKIP 7: needs the un-return (DR-72)")
			raise _Skip
		setup()
		it = pack.std_item("_PPR-UNDO")
		pack.scv_release(it, prev.year, prev.month, 120)
		pack.make_pr(it, wh, 10, 120, posting_date=str(prev))
		dn = pack.make_dn(it, wh, 1, posting_date=str(add_days(prev, 4)))
		pack.scv_release(it, cur.year, cur.month, 150)
		ret = _return(dn, 1, add_days(cur, 4))
		frappe.get_doc({"doctype": "Item Standard Cost Version", "company": pack.COMPANY, "item_code": it,
			"valid_from_year": prev.year, "valid_from_month": prev.month, "standard_cost": 170,
			"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True).release()
		u = frappe.get_doc("Delivery Note", make_cancellation("Delivery Note", ret.name))
		u.submit()
		cr, pr_ = _ipb(it, cur), _ipb(it, prev)
		net = frappe.db.sql("""SELECT SUM(debit - credit) FROM `tabGL Entry` WHERE voucher_no IN %s AND is_cancelled = 0
			AND account IN %s""", ((ret.name, u.name), (a["cogs"], a["cogs_adj"], a["reserve"])))[0][0]
		check("7: the return's Cancellation gives everything back - both counts 0, closing 9",
			flt(cr.reversed_from_previous_qty) == 0 and flt(pr_.reversed_in_later_qty) == 0 and flt(cr.closing_qty) == 9,
			f"{cr.reversed_from_previous_qty} {pr_.reversed_in_later_qty} {cr.closing_qty}")
		check("7: ...and the return's GL with its Rev Fut-Re nets to zero", abs(flt(net)) < 0.01, str(net))
		frappe.db.rollback(save_point="ppr")
	except _Skip:
		pass
	_rest(today, cur, prev, wh, a, setup)


class _Skip(Exception):
	pass


def _rest(today, cur, prev, wh, a, setup):
	try:
		# ---- 8: YTD within the year nets --------------------------------------------------
		if prev.year == cur.year:
			setup()
			it = pack.std_item("_PPR-YTD", view="YTD")
			pack.scv_release(it, prev.year, prev.month, 120)
			pack.make_pr(it, wh, 10, 120, posting_date=str(prev))
			dn = pack.make_dn(it, wh, 1, posting_date=str(add_days(prev, 4)))
			_return(dn, 1, add_days(cur, 4))
			check("8: YTD - a return within the fiscal year nets the issue, no SR - Future",
				not _events(it, "SR - Future") and len(_events(it, "SR")) == 1)
			frappe.db.rollback(save_point="ppr")
		else:
			print("SKIP 8: the previous month is in the prior fiscal year")

		# ---- 9: DR-73 backdated purchase return ---------------------------------------------
		setup()
		it = pack.std_item("_PPR-BDPR")
		pack.scv_release(it, prev.year, prev.month, 800)
		pr = pack.make_pr(it, wh, 10, 820, posting_date=str(prev))
		pack.scv_release(it, cur.year, cur.month, 850)
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		r = make_return_doc("Purchase Receipt", pr.name)
		for x in r.items:
			x.qty = x.received_qty = -4
		r.posting_date = str(add_days(prev, 10)); r.set_posting_time = 1
		r.insert(ignore_permissions=True)
		r = frappe.get_doc("Purchase Receipt", r.name); r.submit()
		bridge = _events(it, "Return - Rev")
		row = _ipb(it, cur)
		check("9: the backdated purchase return is bridged: -4 x (850 - 800) = -200 on the current month",
			len(bridge) == 1 and flt(bridge[0].total_sc) == -200, str(bridge))
		check("9: the current month's stock stands at its standard: 6 x 850 = 5,100",
			flt(row.closing_qty) == 6 and flt(row.closing_value) == 5100, f"{row.closing_qty} {row.closing_value}")
		frappe.db.rollback(save_point="ppr")
	finally:
		frappe.db.rollback(save_point="ppr")
