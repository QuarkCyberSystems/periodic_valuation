"""A purchase return with reference is valued at the original receipt's rate
and reverses the receipt's price difference (client ticket TSK-2026-00994,
27/10/2026, ABC Item - MAP Test - MH #81). Run:
bench --site <site> execute periodic_valuation.tests.verify_return_at_original_rate.run

  1  her example: receipt 10 @ 100, issue 15 (-5, MAP frozen at 100),
     receipt 3 @ 120 into negative stock (inventory 300, PRD 60, GR/IR 360),
     return 1 against it: Dr GR/IR 120 / Cr Stock 100 / Cr PRD 20 - GR/IR and
     PRD keep exactly the two units still held (240 / 40)
  2  the period balance mirrors it: receipts net 120 (gross), PRD 20 back,
     closing -3 / -300, MAP still frozen at 100
  3  a receipt crossing zero, returned in part: each unit carries its share
     (receipt 5 @ 130 over a -2 deficit at 100: inventory 590, PRD 60;
     return 1 -> GR/IR 130, Stock 118, PRD 12)
  4  an ordinary receipt's return is unchanged (no PRD leg)
  5  the return's own Cancellation gives everything back (GR/IR, PRD, Stock)

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, flt, get_first_day, nowdate

from periodic_valuation.tests.smoke_edges import make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters

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
		raise Exception("Return-at-original-rate failures: " + "; ".join(c[0] for c in failed))


def _gl(names, accounts):
	rows = frappe.db.sql("""SELECT account, SUM(debit - credit) v FROM `tabGL Entry` WHERE voucher_no IN %s
		AND is_cancelled = 0 GROUP BY account""", (tuple(names),), as_dict=True)
	by = {r.account: flt(r.v, 2) for r in rows}
	return {k: by.get(acc, 0.0) for k, acc in accounts.items()}


def _return(pr, qty):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	ret = make_return_doc("Purchase Receipt", pr.name)
	for r in ret.items:
		r.qty = r.received_qty = -qty
	ret.insert(ignore_permissions=True)
	ret = frappe.get_doc("Purchase Receipt", ret.name)  # loaded as a return: core's return updates apply
	ret.submit()
	return ret


def _row(item):
	d = get_first_day(nowdate())
	return frappe.get_all("Inventory Period Balance", filters={"company": COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["*"])[0]


def _run():
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.shared.accounts import get_inventory_account, get_offset_account

	wh = ensure_masters()
	frappe.db.commit()
	day = get_first_day(nowdate())
	frappe.db.savepoint("ret_rate")
	try:
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)

		it = make_item("_RAO-MH81")
		acc = {"stock": get_inventory_account(COMPANY, it, wh), "prd": get_offset_account(COMPANY, it, wh, "prd"),
			"grir": frappe.get_cached_value("Company", COMPANY, "stock_received_but_not_billed")}
		make_pr(it, wh, 10, 100, posting_date=str(add_days(day, 1)))
		make_dn(it, wh, 15, posting_date=str(add_days(day, 2)))
		neg = make_pr(it, wh, 3, 120, posting_date=str(add_days(day, 3)))
		check("1: the receipt into negative stock - Stock 300, PRD 60, GR/IR 360",
			_gl([neg.name], acc) == {"stock": 300, "prd": 60, "grir": -360}, str(_gl([neg.name], acc)))
		ret = _return(neg, 1)
		check("1: the return - Dr GR/IR 120 / Cr Stock 100 / Cr PRD 20",
			_gl([ret.name], acc) == {"stock": -100, "prd": -20, "grir": 120}, str(_gl([ret.name], acc)))
		check("1: GR/IR and PRD keep the two units still held (240 / 40)",
			_gl([neg.name, ret.name], acc) == {"stock": 200, "prd": 40, "grir": -240}, str(_gl([neg.name, ret.name], acc)))
		row = _row(it)
		check("2: the balance - receipts 1,000 + 360 - 120, PRD -40, closing -3 / -300, MAP frozen at 100",
			flt(row.receipt_value) == 1240 and flt(row.prd_value) == -40 and flt(row.closing_qty) == -3
			and flt(row.closing_value) == -300 and row.is_negative and flt(row.frozen_map) == 100,
			f"rv {row.receipt_value} prd {row.prd_value} close {row.closing_qty}/{row.closing_value} frozen {row.frozen_map}")

		undo = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", ret.name))
		undo = frappe.get_doc("Purchase Receipt", undo.name)
		undo.submit()
		check("5: the return's Cancellation gives GR/IR, PRD and Stock back",
			_gl([ret.name, undo.name], acc) == {"stock": 0, "prd": 0, "grir": 0}, str(_gl([ret.name, undo.name], acc)))

		it2 = make_item("_RAO-CROSS")
		make_pr(it2, wh, 4, 100, posting_date=str(add_days(day, 1)))
		make_dn(it2, wh, 6, posting_date=str(add_days(day, 2)))
		cross = make_pr(it2, wh, 5, 130, posting_date=str(add_days(day, 3)))
		check("3: the crossing receipt - Stock 590, PRD 60, GR/IR 650",
			_gl([cross.name], acc) == {"stock": 590, "prd": 60, "grir": -650}, str(_gl([cross.name], acc)))
		r2_ = _return(cross, 1)
		check("3: its partial return - Dr GR/IR 130 / Cr Stock 118 / Cr PRD 12",
			_gl([r2_.name], acc) == {"stock": -118, "prd": -12, "grir": 130}, str(_gl([r2_.name], acc)))

		it3 = make_item("_RAO-PLAIN")
		plain = make_pr(it3, wh, 10, 100, posting_date=str(add_days(day, 1)))
		r3 = _return(plain, 2)
		check("4: an ordinary receipt's return - Dr GR/IR 200 / Cr Stock 200, no PRD",
			_gl([r3.name], acc) == {"stock": -200, "prd": 0, "grir": 200}, str(_gl([r3.name], acc)))
	finally:
		frappe.db.rollback(save_point="ret_rate")
