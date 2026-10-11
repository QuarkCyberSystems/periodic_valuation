"""A document is refused at Save, not only at Submit, when its month no
longer takes postings (client ticket STD-016, reopened 11/10/2026). Run:
bench --site <site> execute periodic_valuation.tests.verify_refuse_on_save.run

  1  standard cost: the month is settled for the item - a receipt dated in
     it cannot be saved as a draft
  2  her example: a return dated in the settled month cannot be saved
  3  the same receipt dated in the open month saves
  4  a month whose period is frozen: a draft cannot be saved (standard cost
     and moving average)
  5  a document without periodic-valuation items is untouched

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, get_first_day, getdate, nowdate

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
		raise Exception("Refuse-on-save failures: " + "; ".join(c[0] for c in failed))


def _draft_pr(item, wh, qty, date):
	return frappe.get_doc({"doctype": "Purchase Receipt", "company": pack.COMPANY, "supplier": pack.SUPPLIER,
		"posting_date": str(date), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": 12, "warehouse": wh}]})


def _save_refused(doc):
	frappe.db.savepoint("ros")
	try:
		doc.insert(ignore_permissions=True)
		return ""
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="ros")


def _run():
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	pp2 = add_months(day1, -2)
	frappe.db.savepoint("refuse_on_save")
	try:
		wh, _ = pack.ensure_company()
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		frozen = pack.make_period(pp2.year, pp2.month, "SETTLED_FROZEN")
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(today.year, today.month, "OPEN")

		it = pack.std_item("_STD-ROS")
		pack.scv_release(it, prev.year, prev.month, 10)
		rec = pack.make_pr(it, wh, 50, 12, posting_date=str(add_days(prev, 2)))
		StdEngine(pack.COMPANY, it).close_period(year=prev.year, month=prev.month, sc=10,
			source=("Inventory Period", pp))

		msg = _save_refused(_draft_pr(it, wh, 5, add_days(prev, 5)))
		check("1: a draft receipt dated in the settled month cannot be saved", "already settled" in msg, msg)
		ret = make_return_doc("Purchase Receipt", rec.name)
		ret.items[0].qty = -5
		ret.posting_date, ret.set_posting_time = str(add_days(prev, 6)), 1
		msg = _save_refused(ret)
		check("2: a return dated in the settled month cannot be saved (the client's example)",
			"already settled" in msg, msg)
		check("3: the same receipt dated in the open month saves",
			not _save_refused(_draft_pr(it, wh, 5, day1)))

		msg = _save_refused(_draft_pr(it, wh, 5, add_days(pp2, 3)))
		check("4: standard cost - a draft dated in a frozen month cannot be saved",
			"no longer accepts postings" in msg, msg)
		from periodic_valuation.tests.smoke_edges import make_item

		m = make_item("_MAP-ROS")
		msg = _save_refused(_draft_pr(m, wh, 5, add_days(pp2, 3)))
		check("4: moving average - a draft dated in a frozen month cannot be saved",
			"no longer accepts postings" in msg, msg)

		plain = "_PLAIN-ROS"
		if not frappe.db.exists("Item", plain):
			frappe.get_doc({"doctype": "Item", "item_code": plain, "item_name": plain,
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
				"stock_uom": "Nos", "is_stock_item": 1, "valuation_method": "FIFO"}).insert(ignore_permissions=True)
		check("5: a document without periodic-valuation items is untouched",
			not _save_refused(_draft_pr(plain, wh, 5, add_days(pp2, 3))))
		del frozen
	finally:
		frappe.db.rollback(save_point="refuse_on_save")
