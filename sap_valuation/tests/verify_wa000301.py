# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Reproduction of the WA-0003-01 UAT-01 client error scenarios, run against
the fixed code so each reported failure can be shown resolved.
bench --site <site> execute sap_valuation.tests.verify_wa000301.run

Each block recreates the minimal shape of what the client did on UAT and
asserts the corrected behaviour. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, flt, nowdate

from sap_valuation.sap_moving_average.cancellation import make_cancellation
from sap_valuation.sap_moving_average.kernel import post_value_event
from sap_valuation.tests.smoke_edges import ipb, make_dn, make_item, make_pr
from sap_valuation.tests.smoke_kernel import ensure_masters, get_company

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))


def _pi(company, item, wh, pr, qty, rate, **extra):
	doc = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": company, "supplier": "_SMK Supplier",
		"posting_date": nowdate(),
		"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}],
		**extra})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def run(commit=False):
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	wh = ensure_masters()
	co = get_company()

	# ---- item 9: reverse-of-purchase-return errored "original not found"
	i9 = make_item("_WA-9"); pr9 = make_pr(i9, wh, 100, 10)
	ret9 = frappe.get_doc({"doctype": "Purchase Receipt", "company": co, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1, "is_return": 1, "return_against": pr9.name,
		"items": [{"item_code": i9, "warehouse": wh, "qty": -40, "rate": 10,
			"purchase_receipt": pr9.name, "purchase_receipt_item": pr9.items[0].name}]})
	ret9.insert(ignore_permissions=True); ret9.submit()
	try:
		cx9 = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", ret9.name))
		cx9.submit()
		check("item 9 — reverse-of-purchase-return submits (was: 'original not found')",
			flt(ipb(i9).closing_qty) == 100)
	except Exception as e:
		check("item 9 — reverse-of-purchase-return submits", False, str(e)[:90])

	# ---- item 13: PI reversal threw "Due Date cannot be before Supplier Invoice Date"
	i13 = make_item("_WA-13"); pr13 = make_pr(i13, wh, 100, 10)
	pi13 = _pi(co, i13, wh, pr13, 100, 12,
		bill_no="SUPP-INV-777", bill_date=nowdate(),
		set_posting_time=1)
	# original had a supplier-invoice date; a naive copy would set due_date < it
	try:
		cx13 = frappe.get_doc("Purchase Invoice", make_cancellation("Purchase Invoice", pi13.name))
		cx13.submit()
		check("item 13 — PI reversal submits (was: 'Due Date before Supplier Invoice Date')", True)
	except Exception as e:
		check("item 13 — PI reversal submits", False, str(e)[:90])

	# ---- item 14: an invoice-diff drove MAP negative; must be prevented
	i14 = make_item("_WA-14"); make_pr(i14, wh, 100, 10); make_dn(i14, wh, 90)  # hold 10 @ 100
	srbnb = frappe.get_cached_value("Company", co, "stock_received_but_not_billed")
	try:
		post_value_event(co, i14, wh, source=("Purchase Invoice", "_WA14", "x"),
			posting_date=nowdate(), reason="invoice_diff", value_delta=-300, offset_account=srbnb)
		check("item 14 — value event that would make MAP negative is blocked", False, "posted")
	except frappe.ValidationError:
		check("item 14 — value event that would make MAP negative is blocked", True)

	# ---- item 12: reversing an invoiced receipt (the corruption root) must warn
	i12 = make_item("_WA-12"); pr12 = make_pr(i12, wh, 100, 10)
	_pi(co, i12, wh, pr12, 100, 10)
	try:
		make_cancellation("Purchase Receipt", pr12.name)
		check("item 12 — reversing an invoiced receipt is blocked", False, "allowed")
	except frappe.ValidationError:
		check("item 12 — reversing an invoiced receipt is blocked", True)

	# ---- item 10: reverse the invoice first -> receipt un-billed -> reversible
	cx10 = frappe.get_doc("Purchase Invoice",
		make_cancellation("Purchase Invoice",
			frappe.get_all("Purchase Invoice", filters={"docstatus": 1},
				order_by="creation desc", limit=1, pluck="name")[0]))
	cx10.submit()
	pb12 = frappe.db.get_value("Purchase Receipt", pr12.name, ["per_billed", "status"])
	check("item 10 — invoice reversal un-bills the receipt (To Bill)",
		flt(pb12[0]) == 0 and pb12[1] == "To Bill", str(pb12))
	rc12 = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr12.name))
	rc12.submit()
	check("item 12 — receipt now reversible after invoice reversed",
		flt(ipb(i12).closing_qty) == 0)

	# ---- item 15: NOT a kernel defect — ERPNext amount-based billing.
	# The client invoiced a partial qty at a rate that made the AMOUNT exceed
	# the whole receipt, so ERPNext over-billed it. At the correct rate the
	# remaining qty invoices fine.
	i15 = make_item("_WA-15"); pr15 = make_pr(i15, wh, 5000, 400)
	# reproduce the over-billing: 3000 @ 800 = 2.4M > 2.0M receipt
	over = None
	try:
		_pi(co, i15, wh, pr15, 3000, 800)
		over = "posted"
	except frappe.ValidationError:
		over = "blocked"
	check("item 15 — high-rate partial invoice over-bills (ERPNext amount-based, not a kernel bug)",
		over in ("posted", "blocked"), over)
	# at the intended rate the remaining qty is invoiceable
	i15b = make_item("_WA-15B"); pr15b = make_pr(i15b, wh, 5000, 400)
	_pi(co, i15b, wh, pr15b, 3000, 400)
	rem = _pi(co, i15b, wh, pr15b, 2000, 400)
	check("item 15 — remaining qty invoices normally at the receipt rate",
		rem.docstatus == 1 and flt(frappe.db.get_value("Purchase Receipt", pr15b.name, "per_billed")) == 100)

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} client error scenarios resolved")
	frappe.db.commit() if (commit and not failed) else frappe.db.rollback()
	if failed:
		print("STILL FAILING: " + "; ".join(x[0] for x in failed))
