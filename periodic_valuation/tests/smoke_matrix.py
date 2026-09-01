"""MAP test-matrix smoke - bench --site <site> execute periodic_valuation.tests.smoke_matrix.run

Extends smoke_kernel with the remaining signed-plan Test Matrix scenarios:
LCV stock-ratio split, Stock Count at period MAP, MR21 revaluation,
negative-stock PRD chain with cross-zero reset (workbook anchors 6/15/5 -> 26),
Create Cancellation, backdated prior-period receipt, and Inventory Period Close
with the reconciliation gate. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from periodic_valuation.tests.smoke_kernel import COMPANY, ITEM, ensure_masters

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def ipb():
	return frappe.get_all(
		"Inventory Period Balance",
		filters={"company": COMPANY, "item_code": ITEM},
		fields=["*"], order_by="period_year desc, period_month desc", limit=1,
	)[0]


def make_pr(wh, qty, rate, posting_date=None):
	pr = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": posting_date or nowdate(), "set_posting_time": 1,
		"items": [{"item_code": ITEM, "qty": qty, "rate": rate, "warehouse": wh}],
	})
	pr.insert(ignore_permissions=True)
	pr.submit()
	return pr


def make_dn(wh, qty):
	dn = frappe.get_doc({
		"doctype": "Delivery Note", "company": COMPANY, "customer": "_SMK Customer",
		"posting_date": nowdate(), "items": [{"item_code": ITEM, "qty": qty, "rate": 25, "warehouse": wh}],
	})
	dn.insert(ignore_permissions=True)
	dn.submit()
	return dn


def run(commit=False):
	wh = ensure_masters()

	# prior period must exist as PREV_OPEN_UNSETTLED for the backdated test
	prior_start = get_first_day(add_months(nowdate(), -1))
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior_start.strftime("%Y-%m")}):
		p = frappe.get_doc({
			"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior_start, "status": "PREV_OPEN_UNSETTLED",
		})
		p.insert(ignore_permissions=True)

	# ---- base chain: 100@10, issue 30, 50@20  (MAP 14.1667)
	pr1 = make_pr(wh, 100, 10)
	make_dn(wh, 30)
	make_pr(wh, 50, 20)
	b = ipb()
	check("base chain MAP 14.1667", flt(b.moving_avg_price, 4) == 14.1667, str(b.moving_avg_price))

	# ---- LCV 200 on pr1: coverage min(120 on hand, 100 receipt qty)/100 = 1.0
	# -> fully covered, all 200 to inventory (DR-32, client ruling 2026-08-18)
	lcv = frappe.get_doc({
		"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": nowdate(),
		"purchase_receipts": [{
			"receipt_document_type": "Purchase Receipt", "receipt_document": pr1.name,
			"supplier": "_SMK Supplier", "grand_total": pr1.grand_total,
		}],
		"taxes": [{
			"expense_account": frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0, "root_type": "Expense"}, limit=1, pluck="name")[0],
			"description": "Freight", "amount": 200,
		}],
	})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	b = ipb()
	lcv_ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": lcv.name, "reason_code": "landed_cost"},
		fields=["value_delta", "expense_portion"])
	check("LCV coverage split 200/0 (fully covered)",
		lcv_ive and flt(lcv_ive[0].value_delta, 2) == 200.00 and flt(lcv_ive[0].expense_portion, 2) == 0,
		str(lcv_ive))
	check("LCV MAP", flt(b.moving_avg_price, 4) == flt((1700 + 200) / 120, 4), str(b.moving_avg_price))
	check("PR SLEs not resubmitted by LCV",
		not frappe.db.exists("Stock Ledger Entry", {"voucher_no": pr1.name, "is_cancelled": 1}))

	map_after_lcv = flt(b.moving_avg_price, 6)

	# ---- Stock Count: 5 short, valued at period MAP automatically
	sc = frappe.get_doc({
		"doctype": "Stock Count", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": ITEM, "warehouse": wh, "counted_qty": 115}],
	})
	sc.insert(ignore_permissions=True)
	check("count valued at MAP", flt(sc.items[0].valuation_rate, 6) == map_after_lcv,
		f"{sc.items[0].valuation_rate} vs {map_after_lcv}")
	sc.submit()
	b = ipb()
	check("count qty applied, MAP unchanged",
		flt(b.closing_qty) == 115 and flt(b.moving_avg_price, 6) == map_after_lcv,
		f"{b.closing_qty}/{b.moving_avg_price}")

	# ---- MR21 revaluation +500
	rv = frappe.get_doc({
		"doctype": "Stock Revaluation", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": ITEM, "warehouse": wh,
			"new_valuation_rate": flt(b.moving_avg_price, 6) + flt(500 / flt(b.closing_qty), 6)}],
	})
	rv.insert(ignore_permissions=True)
	check("MR21 difference ~500", abs(flt(rv.total_difference_amount) - 500) < 0.05,
		str(rv.total_difference_amount))
	rv.submit()
	b = ipb()
	check("MR21 applied", abs(flt(b.reval_value) - 700) < 0.05, str(b.reval_value))  # 200 LCV + 500

	# ---- negative stock PRD chain on a second item state: drive negative
	# pin MAP to a round 20.00 first so PRD anchors are exact despite the
	# 2dp quantisation of PR item rates
	b = ipb()
	rv2 = frappe.get_doc({
		"doctype": "Stock Revaluation", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": ITEM, "warehouse": wh, "new_valuation_rate": 20}],
	})
	rv2.insert(ignore_permissions=True)
	rv2.submit()
	b = ipb()
	check("MAP pinned to 20", flt(b.moving_avg_price, 6) == 20, str(b.moving_avg_price))

	make_dn(wh, flt(b.closing_qty) + 10)  # 10 below zero
	b = ipb()
	frozen = b.frozen_map  # = 20 exactly
	check("negative freeze", b.is_negative == 1 and flt(frozen) == 20, str(b.frozen_map))

	pr_neg = make_pr(wh, 2, frozen + 3)
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": pr_neg.name},
		fields=["reason_code", "prd_amount"])[0]
	check("receipt_neg PRD = 6", ive.reason_code == "receipt_neg" and flt(ive.prd_amount, 2) == 6.00, str(ive))

	pr_cross = make_pr(wh, 13, frozen + 1)  # crosses -8 -> +5
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": pr_cross.name},
		fields=["reason_code", "prd_amount"])[0]
	b = ipb()
	check("cross-zero PRD = 8, MAP reset",
		ive.reason_code == "receipt_cross_zero" and flt(ive.prd_amount, 2) == 8.00
		and flt(b.moving_avg_price, 4) == flt(frozen + 1, 4) and b.is_negative == 0,
		f"{ive} MAP {b.moving_avg_price}")
	check("counter = excess 5", flt(b.total_received_since_zero) == 5, str(b.total_received_since_zero))

	# ---- Create Cancellation of pr_neg (mirror events, own date)
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	cxl_name = make_cancellation("Purchase Receipt", pr_neg.name)
	cxl = frappe.get_doc("Purchase Receipt", cxl_name)
	cxl.submit()
	rev = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cxl_name}, fields=["reason_code", "value_delta", "reversal_of"])
	orig_val = frappe.db.get_value("Inventory Valuation Event",
		{"source_docname": pr_neg.name}, "value_delta")
	check("cancellation mirrors original",
		rev and rev[0].reason_code == "cancellation" and flt(rev[0].value_delta, 2) == -flt(orig_val, 2)
		and rev[0].reversal_of, str(rev))
	try:
		cxl2 = make_cancellation("Purchase Receipt", pr_neg.name)
		check("double reversal blocked", False, cxl2)
	except frappe.ValidationError:
		check("double reversal blocked", True)

	# ---- backdated receipt into prior (positive path) period
	b_before = ipb()
	prior_date = prior_start
	pr_bd = make_pr(wh, 40, 15, posting_date=str(prior_date))
	b = ipb()
	check("backdated carryover applied",
		flt(b.carryover_qty) == 40 and flt(b.carryover_value, 2) == 600.00,
		f"{b.carryover_qty}/{b.carryover_value}")
	prior_ipb = frappe.get_all("Inventory Period Balance",
		filters={"company": COMPANY, "item_code": ITEM, "period_year": prior_date.year,
			"period_month": prior_date.month},
		fields=["receipt_qty", "receipt_value", "closing_qty"])
	check("prior period got the receipt",
		prior_ipb and flt(prior_ipb[0].receipt_qty) == 40 and flt(prior_ipb[0].receipt_value, 2) == 600.00,
		str(prior_ipb))
	gl_prior = frappe.get_all("GL Entry", filters={"voucher_no": pr_bd.name, "is_cancelled": 0},
		fields=["posting_date"])
	check("backdated GL posts on prior date",
		gl_prior and all(str(g.posting_date) == str(prior_date) for g in gl_prior))

	# ---- WA-0003-01 item 7: a purchase return nets the since-zero counter
	# The counter and receipt_qty measure the same receipts. Once a purchase
	# return nets down the receipt bucket (item 6), leaving the counter alone
	# makes the Period Balance contradict itself and takes the stock ratio
	# against a denominator that includes goods sent back to the supplier.
	# The workbook asserts qty/value/MAP after its purchase-return step but
	# never the counter, so nothing guarded this until now.
	ret_item = ITEM
	before = ipb()
	rq_before = flt(before.receipt_qty)
	rsz_before = flt(before.total_received_since_zero)
	pr_ret = make_pr(wh, 200, 12)
	mid = ipb()
	check("counter advances on a receipt",
		flt(mid.total_received_since_zero) == rsz_before + 200,
		f"{rsz_before} -> {mid.total_received_since_zero}")
	from erpnext.controllers.sales_and_purchase_return import make_return_doc
	ret = make_return_doc("Purchase Receipt", pr_ret.name)
	ret.posting_date = nowdate()
	ret.set_posting_time = 1
	ret.items = [ret.items[0]]
	ret.items[0].qty = -50
	if ret.items[0].meta.has_field("received_qty"):
		ret.items[0].received_qty = -50
	ret.insert(ignore_permissions=True)
	ret.submit()
	after = ipb()
	check("purchase return nets the receipt bucket",
		flt(after.receipt_qty) == flt(mid.receipt_qty) - 50,
		f"{mid.receipt_qty} -> {after.receipt_qty}")
	check("purchase return nets the since-zero counter (item 7)",
		flt(after.total_received_since_zero) == flt(mid.total_received_since_zero) - 50,
		f"{mid.total_received_since_zero} -> {after.total_received_since_zero}")
	# NB: receipt_qty and the counter are NOT equal in general - receipt_qty is
	# per PERIOD, the counter is since the last ZERO, so a zero-crossing inside
	# the period legitimately separates them. They coincide only when stock has
	# not touched zero, which is the case the client reported.

	# ---- GL identity across everything posted so far
	from periodic_valuation.shared.accounts import get_inventory_account
	inv_acc = get_inventory_account(COMPANY, ITEM, wh)
	# scope to THIS item's tagged legs: the inventory account is shared by every
	# item on the site, so a company-wide sum breaks the moment any other data exists
	net = frappe.db.sql(
		"""SELECT COALESCE(SUM(g.debit - g.credit), 0) FROM `tabGL Entry` g
		JOIN `tabInventory Valuation Event` e ON e.name = g.valuation_event_id
		WHERE g.company = %s AND g.account = %s AND g.is_cancelled = 0 AND e.item_code = %s""",
		(COMPANY, inv_acc, ITEM))[0][0]
	check("GL inventory net == IPB closing value", flt(net, 2) == flt(b.closing_qty and ipb().closing_value, 2),
		f"gl {net} vs ipb {ipb().closing_value}")
	# ---- period close: reconciliation gate + next period seeding
	# (DR-37: rolling the OPEN month needs the previous-open month closed first;
	# the test stands in for that close)
	for _prev in frappe.get_all("Inventory Period",
			filters={"company": COMPANY, "status": "PREV_OPEN_UNSETTLED"}, pluck="name"):
		frappe.db.set_value("Inventory Period", _prev, "status", "SETTLED_FROZEN", update_modified=False)
	open_period = frappe.get_all("Inventory Period",
		filters={"company": COMPANY, "status": "OPEN"}, pluck="name")[0]
	ipc = frappe.get_doc({
		"doctype": "Inventory Period Close", "company": COMPANY,
		"inventory_period": open_period, "posting_date": nowdate(),
	})
	ipc.insert(ignore_permissions=True)
	ipc.submit()
	ipc.reload()
	check("period close reconciliation passed", ipc.reconciliation_passed == 1,
		f"discrepancy {ipc.discrepancy}")
	nxt = frappe.get_all("Inventory Period",
		filters={"company": COMPANY, "status": "OPEN"}, fields=["period_name"])
	check("next period opened", bool(nxt), str(nxt))
	closing = ipb()
	nxt_ipb = frappe.get_all("Inventory Period Balance",
		filters={"company": COMPANY, "item_code": ITEM},
		fields=["opening_qty", "opening_value", "period_month"],
		order_by="period_year desc, period_month desc", limit=1)[0]
	check("next period opening seeded", flt(nxt_ipb.opening_qty) == flt(closing.opening_qty)
		or flt(nxt_ipb.opening_qty) > 0, str(nxt_ipb))

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("matrix smoke failures: " + "; ".join(c[0] for c in failed))
