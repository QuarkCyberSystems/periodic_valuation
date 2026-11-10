"""A cancelled invoice difference or landed cost takes its amount out of the
standard-cost PPV pool shown on the period balance (client ticket
TSK-2026-01095, 10/11/2026, MH S59: the invoice's 300 PPV reversed in GL,
the balance's PPV Pool stayed at 800). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_pools_after_value_cancellation.run

  1  receipt 10 at actual 350 / standard 300: PPV Pool 500
  2  the invoice at 380: PPV 300 more, Pool 800
  3  the invoice's Cancellation: Pool back to 500, equal to what the
     settlement reads
  4  a landed cost of 200: Pool 700; its Cancellation: Pool 500 again

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import flt, get_first_day, getdate, nowdate

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
		raise Exception("Pools after value cancellation failures: " + "; ".join(c[0] for c in failed))


def _pool(item, d):
	"""(PPV Pool shown on the balance, the pool the settlement reads)."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	shown = frappe.db.get_value("Inventory Period Balance", {"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, "ppv_pool")
	return flt(shown, 2), flt(StdEngine(pack.COMPANY, item).pool_ppv(d.year, d.month), 2)


def _lcv(pr, amount):
	account = frappe.db.get_value("Account", {"company": pack.COMPANY, "is_group": 0,
		"account_type": "Expenses Included In Valuation"}) or frappe.db.get_value("Account",
		{"company": pack.COMPANY, "is_group": 0, "root_type": "Expense"})
	doc = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": pack.COMPANY, "posting_date": nowdate(),
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name,
			"supplier": pr.supplier, "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": account, "description": "Freight", "amount": amount}],
		"distribute_charges_based_on": "Amount"})
	doc.get_items_from_purchase_receipts()
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def _run():
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cur = get_first_day(getdate(nowdate()))
	wh, _ = pack.ensure_company()
	frappe.db.commit()
	frappe.db.savepoint("pool_cancel")
	try:
		pack.make_period(cur.year, cur.month, "OPEN")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)

		it = pack.std_item("_POOL-CANCEL")
		pack.scv_release(it, cur.year, cur.month, 300)
		pr = pack.make_pr(it, wh, 10, 350, posting_date=nowdate())
		check("1: receipt 10 at 350 against standard 300 - PPV Pool 500", _pool(it, cur) == (500, 500), str(_pool(it, cur)))

		pi = make_purchase_invoice(pr.name)
		for r in pi.items:
			r.rate = 380
		pi.bill_no, pi.bill_date = "POOL-01095", nowdate()
		pi.insert(ignore_permissions=True)
		pi.submit()
		check("2: the invoice at 380 - PPV Pool 800", _pool(it, cur) == (800, 800), str(_pool(it, cur)))

		cx = frappe.get_doc("Purchase Invoice", make_cancellation("Purchase Invoice", pi.name))
		cx = frappe.get_doc("Purchase Invoice", cx.name)
		cx.submit()
		check("3: the invoice's Cancellation - PPV Pool back to 500, as the settlement reads",
			_pool(it, cur) == (500, 500), str(_pool(it, cur)))

		lcv = _lcv(pr, 200)
		check("4: a landed cost of 200 - PPV Pool 700", _pool(it, cur) == (700, 700), str(_pool(it, cur)))
		lx = frappe.get_doc("Landed Cost Voucher", make_cancellation("Landed Cost Voucher", lcv.name))
		lx.submit()
		check("4: its Cancellation - PPV Pool 500 again", _pool(it, cur) == (500, 500), str(_pool(it, cur)))
	finally:
		frappe.db.rollback(save_point="pool_cancel")
