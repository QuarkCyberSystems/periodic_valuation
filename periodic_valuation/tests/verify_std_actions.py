# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Common desk actions on STD documents - bench --site <site> execute periodic_valuation.tests.verify_std_actions.run

Exercises what a user can click on a Periodic Standard Cost document and its
module records: core Cancel (must be refused everywhere the ledger is
immutable), Create Cancellation on every supported doctype, Duplicate of a
reversal document, Reverse Settlement rules, Settlement Run cancel, edits and
deletes of released cost versions, settlement rows and valuation events.
Rolled back unless commit=True.
"""

import frappe
from frappe.utils import flt, getdate, nowdate

from periodic_valuation.tests.smoke_edges import make_dn, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company
from periodic_valuation.tests.smoke_std import ensure_std_masters
from periodic_valuation.tests.verify_std_design_tcs import one_ive, scv_release, std_item

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def refused(label, fn, *needle):
	"""Run fn expecting a ValidationError whose text contains every needle."""
	try:
		fn()
		check(label, False, "went through")
		return None
	except frappe.ValidationError as e:
		msg = frappe.utils.strip_html(str(e))
		ok = all(n.lower() in msg.lower() for n in needle)
		check(label, ok, msg[:160])
		return msg
	except Exception as e:  # any other exception is a crash, not a guard
		check(label, False, f"{type(e).__name__}: {str(e)[:140]}")
		return None


def run(commit=False):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	wh = ensure_masters()
	company = get_company()
	ensure_std_masters(company)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	today = getdate(nowdate())

	item = std_item("_ACT-STD-1")
	scv = scv_release(company, item, today.year, today.month, 10)
	pr = make_pr(item, wh, 100, 12)
	dn = make_dn(item, wh, 20)

	# ------------------------------------------------ core Cancel is refused
	refused("core Cancel refused on a routed Purchase Receipt",
		lambda: frappe.get_doc("Purchase Receipt", pr.name).cancel(), "Create Cancellation")
	refused("core Cancel refused on a routed Delivery Note",
		lambda: frappe.get_doc("Delivery Note", dn.name).cancel(), "Create Cancellation")
	cnt = frappe.get_doc({"doctype": "Stock Count", "company": company, "posting_date": nowdate(),
		"items": [{"item_code": item, "warehouse": wh, "counted_qty": 75}]})
	cnt.insert(ignore_permissions=True)
	cnt.submit()
	refused("core Cancel refused on a Stock Count", lambda: frappe.get_doc("Stock Count", cnt.name).cancel())
	exp_acct = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"root_type": "Expense"}, limit=1, pluck="name")[0]
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": company,
		"posting_date": nowdate(), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name, "supplier": "_SMK Supplier", "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp_acct, "description": "freight", "amount": 30}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	refused("core Cancel refused on a routed Landed Cost Voucher",
		lambda: frappe.get_doc("Landed Cost Voucher", lcv.name).cancel(), "Create Cancellation")

	item_sr = std_item("_ACT-STD-SR")
	scv_release(company, item_sr, today.year, today.month, 12)
	tsa = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"account_type": "Temporary"}, limit=1, pluck="name")
	sr = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company,
		"purpose": "Opening Stock", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": tsa[0] if tsa else exp_acct,
		"items": [{"item_code": item_sr, "warehouse": wh, "qty": 10, "valuation_rate": 15}]})
	sr.insert(ignore_permissions=True)
	sr.submit()
	refused("core Cancel refused on a routed Stock Reconciliation",
		lambda: frappe.get_doc("Stock Reconciliation", sr.name).cancel(), "Create Cancellation")

	# ------------------------------------- Create Cancellation per doctype
	cx_dn = frappe.get_doc("Delivery Note", make_cancellation("Delivery Note", dn.name))
	cx_dn.submit()
	m = one_ive(source_docname=cx_dn.name)
	check("Create Cancellation on Delivery Note mirrors the issue (+200 at SC, linked)",
		len(m) == 1 and flt(m[0].total_sc) == 200 and m[0].reversal_of, str(m))
	check("cancellation document carries is_cancellation + cancellation_against",
		cx_dn.is_cancellation == 1 and cx_dn.cancellation_against == dn.name, "")

	cx_lcv = frappe.get_doc("Landed Cost Voucher", make_cancellation("Landed Cost Voucher", lcv.name))
	cx_lcv.submit()
	m = one_ive(source_docname=cx_lcv.name)
	check("Create Cancellation on Landed Cost Voucher mirrors the charge (LC -30)",
		m and any(flt(r.total_ac) == -30 for r in m), str(m))

	refused("Create Cancellation refused for Stock Count (not a supported doctype)",
		lambda: make_cancellation("Stock Count", cnt.name), "does not support")
	refused("Create Cancellation refused for Stock Reconciliation (not a supported doctype)",
		lambda: make_cancellation("Stock Reconciliation", sr.name), "does not support")

	# the LCV above is already reversed, so its receipt is free to cancel; a receipt
	# with a LIVE landed cost voucher must be blocked until the LCV is reversed
	pr2 = make_pr(item, wh, 10, 12)
	lcv2 = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": company,
		"posting_date": nowdate(), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr2.name, "supplier": "_SMK Supplier", "grand_total": pr2.grand_total}],
		"taxes": [{"expense_account": exp_acct, "description": "freight", "amount": 5}]})
	lcv2.get_items_from_purchase_receipts()
	lcv2.insert(ignore_permissions=True)
	lcv2.submit()
	refused("cancelling a receipt that has a LIVE landed cost voucher is blocked (reverse the LCV first)",
		lambda: frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr2.name)).submit())
	cx_pr = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr.name))
	cx_pr.submit()
	check("receipt whose landed cost voucher was already reversed can be cancelled",
		cx_pr.docstatus == 1, "")

	# Stock Entry receipt/issue for an STD item
	item_se = std_item("_ACT-STD-SE")
	scv_release(company, item_se, today.year, today.month, 10)
	try:
		se = frappe.get_doc({"doctype": "Stock Entry", "company": company,
			"stock_entry_type": "Material Receipt", "posting_date": nowdate(),
			"items": [{"item_code": item_se, "qty": 5, "t_warehouse": wh, "basic_rate": 11,
				"uom": frappe.db.get_value("Item", item_se, "stock_uom"), "conversion_factor": 1}]})
		se.insert(ignore_permissions=True)
		se.submit()
		m = one_ive(source_docname=se.name)
		check("Stock Entry Material Receipt routes as Rec (SC 50 / AC 55)",
			m and m[0].std_trans == "Rec" and flt(m[0].total_sc) == 50 and flt(m[0].total_ac) == 55, str(m))
		cx_se = frappe.get_doc("Stock Entry", make_cancellation("Stock Entry", se.name))
		cx_se.submit()
		m = one_ive(source_docname=cx_se.name)
		check("Create Cancellation on Stock Entry mirrors it (-50 / -55)",
			m and flt(m[0].total_sc) == -50 and flt(m[0].total_ac) == -55, str(m))
	except Exception as e:
		check("Stock Entry Material Receipt for an STD item", False,
			f"{type(e).__name__}: {frappe.utils.strip_html(str(e))[:160]}")

	# ------------------------------------- Duplicate of a reversal document
	dup = frappe.copy_doc(cx_dn, ignore_no_copy=False)  # what the desk Duplicate action does
	check("Duplicate of a reversal document does not inherit the reversal flags",
		not dup.is_cancellation and not dup.cancellation_against,
		f"is_cancellation={dup.is_cancellation} against={dup.cancellation_against}")

	# double cancellation
	refused("second Create Cancellation of the same document refused",
		lambda: frappe.get_doc("Delivery Note", make_cancellation("Delivery Note", dn.name)).submit())

	# ------------------------------------- cancellation after the month is settled
	item_s = std_item("_ACT-STD-SETTLED")
	scv_release(company, item_s, today.year, today.month, 10)
	pr_s = make_pr(item_s, wh, 10, 12)
	eng = StdEngine(company, item_s, wh)
	sett = eng.close_period(year=today.year, month=today.month, sc=10, source=("Item", item_s))
	cx = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr_s.name))
	try:
		cx.submit()
		m = one_ive(source_docname=cx.name)
		delta = one_ive(item_code=item_s, std_trans="Sett - Delta")
		check("cancellation of a document in a settled month posts current-dated mirror + settlement delta",
			m and str(m[0].posting_date) == nowdate() and delta, f"mirror {m} delta {delta}")
	except frappe.ValidationError as e:
		check("cancellation of a document in a settled month is refused with a clear message",
			"settled" in str(e).lower(), frappe.utils.strip_html(str(e))[:160])

	# ------------------------------------- settlement / run / governance records
	run_doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": company,
		"period_year": today.year, "period_month": today.month, "run_type": "INITIAL_CLOSE"})
	run_doc.insert(ignore_permissions=True)
	run_doc.submit()
	refused("Settlement Run cannot be cancelled", lambda: run_doc.cancel(), "cannot be cancelled")

	old = frappe.get_doc("Inventory Period Settlement", sett.name)
	# a settlement older than the previous month cannot be reversed (DR-08)
	item_old = std_item("_ACT-STD-OLD")
	e_old = StdEngine(company, item_old, wh)
	e_old.post(trans="Rec", posting_date="2026-03-05", qty=10, sc=10, t_ac_override=120, source=("Item", item_old))
	s_old = e_old.close_period(year=2026, month=3, sc=10, source=("Item", item_old), entry_date="2026-04-01")
	refused("Reverse Settlement refused for a month older than the previous one (DR-08)",
		lambda: frappe.get_doc("Inventory Period Settlement", s_old.name).reverse(), "immediately-previous")
	refused("settlement row cannot be deleted",
		lambda: frappe.delete_doc("Inventory Period Settlement", s_old.name, ignore_permissions=True))
	ev = one_ive(item_code=item_old)[0]
	refused("valuation event cannot be deleted",
		lambda: frappe.delete_doc("Inventory Valuation Event", ev.name, ignore_permissions=True))

	def edit_released():
		d = frappe.get_doc("Item Standard Cost Version", scv.name)
		d.standard_cost = 99
		d.save(ignore_permissions=True)
	refused("released cost version cannot be edited", edit_released, "immutable")
	deleted = False
	try:
		frappe.delete_doc("Item Standard Cost Version", scv.name, ignore_permissions=True)
		deleted = not frappe.db.exists("Item Standard Cost Version", scv.name)
	except Exception:
		deleted = False
	check("released cost version cannot be deleted", not deleted, "deleted")

	# ISVC: approve without a second user is refused; status is not editable
	isvc = frappe.get_doc({"doctype": "Item Settlement View Change", "company": company,
		"item_code": item_old, "to_view": "YTD", "reason": "actions test"})
	isvc.insert(ignore_permissions=True)
	refused("ISVC self-approval refused", isvc.approve, "differ")
	refused("ISVC cannot be submitted while Draft", isvc.submit, "Approved")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
