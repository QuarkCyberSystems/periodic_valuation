"""Block Negative Stock holds for every standard-cost posting that takes
quantity out, not issues only (DR-10, STD design premise #7; client ticket
TSK-2026-01151, 24/11/2026: MH S51 returned 100 to the supplier with 50 on
hand and went to -50). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_block_negative_outflows.run

ERPNext's own negative-stock check is switched off for the run, so only the
standard-cost block can refuse.

  1  an issue beyond stock is refused (as before)
  2  a purchase return beyond stock is refused; a return within stock posts
  3  a receipt's Cancellation after part of it was issued is refused
  4  with Block Negative Stock off, the over-return posts (the setting rules)

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
BLOCKED = "negative stock is blocked"


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
		raise Exception("STD negative-stock block failures: " + "; ".join(c[0] for c in failed))


def _refusal(fn):
	frappe.db.savepoint("sbn_refusal")
	try:
		fn()
		return ""
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="sbn_refusal")


def _return(pr, qty):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	ret = make_return_doc("Purchase Receipt", pr.name)
	for r in ret.items:
		r.qty = r.received_qty = -qty
		r.rejected_qty = 0
	ret.insert(ignore_permissions=True)
	ret.submit()
	return ret


def _on_hand(item):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	eng = StdEngine(pack.COMPANY, item)
	periods = eng._periods_present()
	return flt(eng.end_qty_mtd(*periods[-1])) if periods else 0.0


def _run():
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cur = get_first_day(getdate(nowdate()))
	wh, _ = pack.ensure_company()
	frappe.db.commit()
	frappe.db.savepoint("std_block_negative")
	try:
		pack.make_period(cur.year, cur.month, "OPEN")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		settings = frappe.db.get_value("Periodic Standard Cost Settings", {"company": pack.COMPANY})
		frappe.db.set_value("Periodic Standard Cost Settings", settings, "block_negative_stock_std", 1)

		it = pack.std_item("_STD-BLOCK-NEG")
		pack.scv_release(it, cur.year, cur.month, 10)
		pr = pack.make_pr(it, wh, 10, 12)
		pack.make_dn(it, wh, 6)
		check("setup: 10 received, 6 issued, 4 on hand", _on_hand(it) == 4, _on_hand(it))

		# ---- 1 ------------------------------------------------------------------
		msg = _refusal(lambda: pack.make_dn(it, wh, 5))
		check("1: an issue of 5 with 4 on hand is refused", BLOCKED in msg, msg)

		# ---- 2 ------------------------------------------------------------------
		msg = _refusal(lambda: _return(pr, 10))
		check("2: a purchase return of 10 with 4 on hand is refused", BLOCKED in msg, msg)
		_return(pr, 3)
		check("2: a purchase return of 3 posts, 1 left", _on_hand(it) == 1, _on_hand(it))

		# ---- 3 ------------------------------------------------------------------
		pr2 = pack.make_pr(it, wh, 5, 12)
		pack.make_dn(it, wh, 4)
		msg = _refusal(lambda: frappe.get_doc("Purchase Receipt",
			make_cancellation("Purchase Receipt", pr2.name)).submit())
		check("3: cancelling a receipt of 5 with 2 on hand is refused", BLOCKED in msg, msg)

		# ---- 4 ------------------------------------------------------------------
		frappe.db.set_value("Periodic Standard Cost Settings", settings, "block_negative_stock_std", 0)
		msg = _refusal(lambda: _return(pr2, 5))
		check("4: with the block off, a purchase return of 5 with 2 on hand is not refused by it",
			BLOCKED not in msg, msg)
	finally:
		frappe.db.rollback(save_point="std_block_negative")
