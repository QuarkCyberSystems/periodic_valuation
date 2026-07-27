"""UAT dry run — bench --site <site> execute sap_valuation.tests.smoke_uat.run

Executes every test case of the two manual UAT scripts programmatically,
in script order, with the scripts' exact values. UI-button TCs invoke the
button's whitelisted method. Rolled back unless commit=True.

MAP: badia_docs/sap_valuation_design/MAP_UAT_Test_Script_v1.md (TC-A1..TC-K3)
STD: badia_docs/sap_valuation_design/STD_UAT_Test_Script_v1.md (TC-A1..TC-G)
"""

import frappe
from frappe.utils import add_months, flt, get_first_day, getdate, nowdate

from sap_valuation.tests.smoke_edges import ipb, ipb_period, make_dn, make_item, make_pr
from sap_valuation.tests.smoke_kernel import ensure_masters, get_company
from sap_valuation.tests.smoke_std import ensure_std_masters

CHECKS = []


def tc(tcid, ok, detail=""):
	CHECKS.append((tcid, bool(ok)))
	print(("PASS " if ok else "FAIL ") + tcid + (f" — {detail}" if detail and not ok else ""))


def expect_block(tcid, fn, detail=""):
	try:
		fn()
		tc(tcid, False, detail or "action was not blocked")
	except frappe.ValidationError:
		tc(tcid, True)


def gl_of(voucher_no, account=None):
	filters = {"voucher_no": voucher_no, "is_cancelled": 0}
	if account:
		filters["account"] = account
	return frappe.get_all("GL Entry", filters=filters,
		fields=["account", "debit", "credit", "valuation_event_id", "posting_date"])


def run(commit=False):
	wh = ensure_masters()
	company = get_company()
	ensure_std_masters(company)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	prior = get_first_day(add_months(nowdate(), -1))
	if not frappe.db.exists("Inventory Period",
			{"company": company, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": company,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)

	run_map(company, wh, prior)
	run_std(company, wh)

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} UAT test cases passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("UAT failures: " + "; ".join(x[0] for x in failed))


# =============================================================== MAP script
def run_map(company, wh, prior):
	from sap_valuation.sap_moving_average.cancellation import make_cancellation
	from sap_valuation.shared.accounts import get_inventory_account

	a = make_item("UAT-MAP-A")
	srbnb = frappe.get_cached_value("Company", company, "stock_received_but_not_billed")
	inv_acct = get_inventory_account(company, a, wh)

	# --- A1 first receipt sets MAP
	pr1 = make_pr(a, wh, 100, 10)
	b = ipb(a)
	gl = gl_of(pr1.name)
	tc("MAP TC-A1", flt(b.closing_qty) == 100 and flt(b.closing_value, 2) == 1000
		and flt(b.moving_avg_price) == 10 and len(gl) == 2
		and all(g.valuation_event_id for g in gl), f"{b.closing_qty}/{b.closing_value}")

	# --- A2 issue at MAP regardless of selling rate
	make_dn(a, wh, 30)
	b = ipb(a)
	tc("MAP TC-A2", flt(b.closing_qty) == 70 and flt(b.closing_value, 2) == 700,
		f"{b.closing_qty}/{b.closing_value}")

	# --- A3 blend + no RIV
	make_pr(a, wh, 50, 20)
	b = ipb(a)
	tc("MAP TC-A3", flt(b.moving_avg_price, 4) == 14.1667
		and not frappe.db.exists("Repost Item Valuation", {"company": company}),
		str(b.moving_avg_price))

	# --- A4 LCV stock ratio 0.8 -> 160/40; PR SLEs untouched
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": company,
		"posting_date": nowdate(),
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr1.name, "supplier": "_SMK Supplier",
			"grand_total": pr1.grand_total}],
		"taxes": [{"expense_account": frappe.get_all("Account",
			filters={"company": company, "is_group": 0, "root_type": "Expense"},
			limit=1, pluck="name")[0], "description": "Freight", "amount": 200}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	b = ipb(a)
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": lcv.name, "reason_code": "landed_cost"},
		fields=["value_delta", "expense_portion"])
	tc("MAP TC-A4", ive and flt(ive[0].value_delta, 2) == 160 and flt(ive[0].expense_portion, 2) == 40
		and flt(b.closing_value, 2) == 1860 and flt(b.moving_avg_price, 2) == 15.50
		and not frappe.db.exists("Stock Ledger Entry", {"voucher_no": pr1.name, "is_cancelled": 1}),
		str(ive))

	# --- B1 count at MAP, rate read-only semantics (system-derived)
	sc_doc = frappe.get_doc({"doctype": "Stock Count", "company": company,
		"posting_date": nowdate(),
		"items": [{"item_code": a, "warehouse": wh, "counted_qty": 115}]})
	sc_doc.insert(ignore_permissions=True)
	rate_before = flt(sc_doc.items[0].valuation_rate, 2)
	sc_doc.submit()
	b = ipb(a)
	tc("MAP TC-B1", rate_before == 15.50 and flt(b.closing_qty) == 115
		and flt(b.closing_value, 2) == 1782.50 and flt(b.moving_avg_price, 2) == 15.50,
		f"{rate_before}/{b.closing_qty}/{b.closing_value}")

	# --- B2 MR21 +500
	rv = frappe.get_doc({"doctype": "Stock Revaluation", "company": company,
		"posting_date": nowdate(),
		"items": [{"item_code": a, "warehouse": wh,
			"new_valuation_rate": flt(b.moving_avg_price, 6) + flt(500 / 115, 6)}]})
	rv.insert(ignore_permissions=True)
	ok_diff = abs(flt(rv.total_difference_amount) - 500) < 0.05
	rv.submit()
	b = ipb(a)
	tc("MAP TC-B2", ok_diff and abs(flt(b.closing_value) - 2282.50) < 0.05,
		f"{rv.total_difference_amount}/{b.closing_value}")

	# --- B3 MR21 on empty stock refused
	z = make_item("UAT-MAP-Z0")
	expect_block("MAP TC-B3", lambda: frappe.get_doc({"doctype": "Stock Revaluation",
		"company": company, "posting_date": nowdate(),
		"items": [{"item_code": z, "warehouse": wh, "new_valuation_rate": 5}]}).insert(
		ignore_permissions=True))

	# --- D1 direct cancel blocked
	expect_block("MAP TC-D1", lambda: pr1.cancel())

	# --- D2 Create Cancellation dated mirror (use latest PR to keep math clean)
	pr_c = make_pr(a, wh, 10, 10)
	before = ipb(a)
	cxl = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr_c.name))
	cxl.submit()
	after = ipb(a)
	tc("MAP TC-D2", flt(after.closing_qty) == flt(before.closing_qty) - 10
		and abs(flt(after.closing_value) - (flt(before.closing_value) - 100)) < 0.01
		and pr_c.docstatus == 1, f"{after.closing_qty}/{after.closing_value}")

	# --- D3 double reversal refused
	expect_block("MAP TC-D3", lambda: make_cancellation("Purchase Receipt", pr_c.name))

	# --- E1-E3 negative stock PRD chain (fresh item, MAP pinned 20)
	n = make_item("UAT-MAP-NEG")
	make_pr(n, wh, 10, 20)
	make_dn(n, wh, 20)
	b = ipb(n)
	tc("MAP TC-E1", b.is_negative == 1 and flt(b.frozen_map) == 20
		and flt(b.closing_qty) == -10 and flt(b.closing_value, 2) == -200,
		f"{b.closing_qty}/{b.closing_value}/{b.frozen_map}")
	pr_neg = make_pr(n, wh, 2, 23)
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pr_neg.name}, fields=["reason_code", "prd_amount"])[0]
	tc("MAP TC-E2", ive.reason_code == "receipt_neg" and flt(ive.prd_amount, 2) == 6,
		str(ive))
	pr_x = make_pr(n, wh, 13, 21)
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pr_x.name}, fields=["reason_code", "prd_amount"])[0]
	b = ipb(n)
	tc("MAP TC-E3", ive.reason_code == "receipt_cross_zero" and flt(ive.prd_amount, 2) == 8
		and flt(b.moving_avg_price, 4) == 21 and b.is_negative == 0
		and flt(b.total_received_since_zero) == 5, f"{ive}/{b.moving_avg_price}")

	# --- E4 zero-out + fresh ratio cycle
	make_dn(n, wh, flt(b.closing_qty))
	make_pr(n, wh, 4, 12)
	b = ipb(n)
	tc("MAP TC-E4", flt(b.moving_avg_price) == 12 and flt(b.total_received_since_zero) == 4,
		f"{b.moving_avg_price}/{b.total_received_since_zero}")

	# --- F1 backdated receipt: prior period + carryover + prior-dated GL
	f = make_item("UAT-MAP-BD")
	make_pr(f, wh, 100, 10)
	make_dn(f, wh, 20)
	pr_bd = make_pr(f, wh, 40, 15, posting_date=str(prior))
	b = ipb(f)
	p = ipb_period(f, prior.year, prior.month)
	gl = gl_of(pr_bd.name)
	tc("MAP TC-F1", flt(b.carryover_qty) == 40 and flt(b.carryover_value, 2) == 600
		and p and flt(p.receipt_value, 2) == 600
		and all(str(g.posting_date) == str(prior) for g in gl),
		f"{b.carryover_qty}/{b.carryover_value}")

	# --- F2 backdating into a month with no open period refused
	two_back = get_first_day(add_months(nowdate(), -2))
	expect_block("MAP TC-F2", lambda: make_pr(f, wh, 5, 10, posting_date=str(two_back)))

	# --- G1 purchase return with reference at original cost
	g = make_item("UAT-MAP-RET")
	pr_g1 = make_pr(g, wh, 100, 10)
	make_pr(g, wh, 50, 20)
	ret = frappe.get_doc({"doctype": "Purchase Receipt", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(), "is_return": 1,
		"return_against": pr_g1.name,
		"items": [{"item_code": g, "qty": -30, "rate": 10, "warehouse": wh,
			"purchase_receipt_item": pr_g1.items[0].name}]})
	ret.insert(ignore_permissions=True)
	ret.submit()
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": ret.name},
		fields=["reason_code", "value_delta"])
	tc("MAP TC-G1", ive and ive[0].reason_code == "return_with_ref"
		and flt(ive[0].value_delta, 2) == -300, str(ive))

	# --- G2 sales return with reference at original issue cost
	dn_g = make_dn(g, wh, 30)
	sr = frappe.get_doc({"doctype": "Delivery Note", "company": company,
		"customer": "_SMK Customer", "posting_date": nowdate(), "is_return": 1,
		"return_against": dn_g.name,
		"items": [{"item_code": g, "qty": -10, "rate": 25, "warehouse": wh,
			"dn_detail": dn_g.items[0].name}]})
	sr.insert(ignore_permissions=True)
	sr.submit()
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": sr.name},
		fields=["reason_code"])
	tc("MAP TC-G2", ive and ive[0].reason_code == "return_with_ref", str(ive))

	# --- G3 policy = Without Reference -> linked return valued at current MAP
	settings = frappe.db.get_value("SAP Moving Average Settings", {"company": company})
	frappe.db.set_value("SAP Moving Average Settings", settings,
		"default_return_valuation", "Without Reference")
	frappe.clear_document_cache("SAP Moving Average Settings", settings)
	b_before = ipb(g)
	sr2 = frappe.get_doc({"doctype": "Delivery Note", "company": company,
		"customer": "_SMK Customer", "posting_date": nowdate(), "is_return": 1,
		"return_against": dn_g.name,
		"items": [{"item_code": g, "qty": -5, "rate": 25, "warehouse": wh,
			"dn_detail": dn_g.items[0].name}]})
	sr2.insert(ignore_permissions=True)
	sr2.submit()
	b_after = ipb(g)
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": sr2.name},
		fields=["reason_code"])
	frappe.db.set_value("SAP Moving Average Settings", settings,
		"default_return_valuation", "With Reference")
	frappe.clear_document_cache("SAP Moving Average Settings", settings)
	tc("MAP TC-G3", ive and ive[0].reason_code == "return_no_ref"
		and flt(b_after.moving_avg_price, 4) == flt(b_before.moving_avg_price, 4),
		f"{ive} {b_before.moving_avg_price} -> {b_after.moving_avg_price}")

	# --- H1 PI invoice diff via stock ratio
	h = make_item("UAT-MAP-PI")
	pr_h = make_pr(h, wh, 100, 10)
	make_dn(h, wh, 20)
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": h, "qty": 100, "rate": 11, "warehouse": wh,
			"purchase_receipt": pr_h.name, "pr_detail": pr_h.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pi.name, "reason_code": "invoice_diff"},
		fields=["value_delta", "expense_portion"])
	tc("MAP TC-H1", ive and flt(ive[0].value_delta, 2) == 80
		and flt(ive[0].expense_portion, 2) == 20, str(ive))

	# --- I1 company-scope transfer value-neutral, no GL
	t = make_item("UAT-MAP-TRF")
	abbr = frappe.db.get_value("Company", company, "abbr")
	wh2 = f"_SMK Stores 2 - {abbr}"
	if not frappe.db.exists("Warehouse", wh2):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_SMK Stores 2",
			"company": company}).insert(ignore_permissions=True)
	make_pr(t, wh, 40, 10)
	before = ipb(t)
	se = frappe.get_doc({"doctype": "Stock Entry", "company": company,
		"stock_entry_type": "Material Transfer", "posting_date": nowdate(),
		"items": [{"item_code": t, "qty": 15, "s_warehouse": wh, "t_warehouse": wh2}]})
	se.insert(ignore_permissions=True)
	se.submit()
	after = ipb(t)
	tc("MAP TC-I1", flt(after.closing_value, 2) == flt(before.closing_value, 2)
		and not gl_of(se.name), f"{after.closing_value}")

	# --- I2 per-warehouse-scope transfer moves value
	tw = make_item("UAT-MAP-TRW", include_warehouse=1)
	make_pr(tw, wh, 40, 12)
	se2 = frappe.get_doc({"doctype": "Stock Entry", "company": company,
		"stock_entry_type": "Material Transfer", "posting_date": nowdate(),
		"items": [{"item_code": tw, "qty": 10, "s_warehouse": wh, "t_warehouse": wh2}]})
	se2.insert(ignore_permissions=True)
	se2.submit()
	src_b = ipb(tw, warehouse=wh)
	dst_b = ipb(tw, warehouse=wh2)
	tc("MAP TC-I2", flt(src_b.closing_value, 2) == 360 and flt(dst_b.closing_value, 2) == 120,
		f"{src_b.closing_value}/{dst_b.closing_value}")

	# --- J zero-out reset
	j = make_item("UAT-MAP-J")
	make_pr(j, wh, 25, 10)
	make_dn(j, wh, 25)
	b = ipb(j)
	tc("MAP TC-J", flt(b.closing_qty) == 0 and flt(b.closing_value, 2) == 0
		and flt(b.total_received_since_zero) == 0 and flt(b.moving_avg_price) == 0,
		f"{b.closing_qty}/{b.closing_value}")

	# --- K1 SR opening stock hits GL
	k = make_item("UAT-MAP-K")
	tsa = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"account_type": "Temporary"}, limit=1, pluck="name")
	acct = tsa[0] if tsa else frappe.get_all("Account", filters={"company": company,
		"is_group": 0, "root_type": "Liability"}, limit=1, pluck="name")[0]
	sr_op = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company,
		"purpose": "Opening Stock", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": acct,
		"items": [{"item_code": k, "warehouse": wh, "qty": 500, "valuation_rate": 12}]})
	sr_op.insert(ignore_permissions=True)
	sr_op.submit()
	b = ipb(k)
	gl = gl_of(sr_op.name)
	tc("MAP TC-K1", flt(b.closing_qty) == 500 and flt(b.closing_value, 2) == 6000
		and len(gl) == 2 and flt(sum(g.debit for g in gl), 2) == 6000
		and all(g.valuation_event_id for g in gl), f"{b.closing_qty}/{b.closing_value}")

	# --- K2 combined qty+rate correction decomposes
	sr_c = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company,
		"purpose": "Stock Reconciliation", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": frappe.get_cached_value("Company", company, "stock_adjustment_account"),
		"items": [{"item_code": k, "warehouse": wh, "qty": 480, "valuation_rate": 12.5}]})
	sr_c.insert(ignore_permissions=True)
	sr_c.submit()
	b = ipb(k)
	kinds = sorted(x.reason_code for x in frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": sr_c.name}, fields=["reason_code"]))
	tc("MAP TC-K2", flt(b.closing_qty) == 480 and flt(b.closing_value, 2) == 6000
		and flt(b.moving_avg_price, 4) == 12.5 and kinds == ["count_diff", "revaluation"],
		f"{b.closing_value}/{kinds}")

	# --- K3 rate-only on zero stock refused
	make_dn(k, wh, 480)
	expect_block("MAP TC-K3", lambda: _sr_rate_only(company, k, wh))

	# document for TC-D4 must exist BEFORE the close freezes the prior period
	frozen_item = make_item("UAT-MAP-FRZ")
	pr_frz = make_pr(frozen_item, wh, 10, 10, posting_date=str(prior))

	# --- intent stamping (M4): action-derived, visible, immutable
	intents = {x.reason_code: x.posting_intent for x in frappe.get_all(
		"Inventory Valuation Event", filters={"item_code": a},
		fields=["reason_code", "posting_intent"])}
	doc_intent = frappe.db.get_value("Purchase Receipt", pr_c.name, "posting_intent")
	cxl_intent = frappe.db.get_value("Purchase Receipt",
		frappe.db.get_value("Purchase Receipt", {"cancellation_against": pr_c.name}), "posting_intent")
	tc("MAP TC-INT", intents.get("receipt") == "NEW_CURRENT_STD_MOVEMENT"
		and intents.get("cancellation") == "EXACT_REVERSAL_WITH_REFERENCE"
		and intents.get("landed_cost") == "NEW_CURRENT_STD_MOVEMENT"
		and doc_intent == "NEW_CURRENT_STD_MOVEMENT"
		and cxl_intent == "EXACT_REVERSAL_WITH_REFERENCE", str(intents))
	ret_intent = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": g, "reason_code": "return_with_ref"},
		fields=["posting_intent"], limit=1)
	tc("MAP TC-INT2", ret_intent and ret_intent[0].posting_intent == "RETURN_WITH_REFERENCE",
		str(ret_intent))

	# --- C1 period close gate passes + next period opens (LAST)
	open_p = frappe.get_all("Inventory Period",
		filters={"company": company, "status": "OPEN"}, pluck="name")[0]
	ipc = frappe.get_doc({"doctype": "Inventory Period Close", "company": company,
		"inventory_period": open_p, "posting_date": nowdate()})
	ipc.insert(ignore_permissions=True)
	ipc.submit()
	ipc.reload()
	nxt = frappe.get_all("Inventory Period", filters={"company": company, "status": "OPEN"})
	tc("MAP TC-C1", ipc.reconciliation_passed == 1 and bool(nxt),
		f"disc {ipc.discrepancy}")

	# --- C2 close doc cannot be cancelled
	expect_block("MAP TC-C2", lambda: ipc.cancel())

	# --- D4 cancellation into frozen period refused
	prior_p = frappe.db.get_value("Inventory Period",
		{"company": company, "period_year": prior.year, "period_month": prior.month})
	frappe.db.set_value("Inventory Period", prior_p, "status", "SETTLED_FROZEN")
	cxl_name = make_cancellation("Purchase Receipt", pr_frz.name)
	expect_block("MAP TC-D4", lambda: frappe.get_doc("Purchase Receipt", cxl_name).submit())
	frappe.db.set_value("Inventory Period", prior_p, "status", "PREV_OPEN_UNSETTLED")


def _sr_rate_only(company, item, wh):
	doc = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company,
		"purpose": "Stock Reconciliation", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": frappe.get_cached_value("Company", company, "stock_adjustment_account"),
		"items": [{"item_code": item, "warehouse": wh, "qty": 0, "valuation_rate": 13}]})
	doc.insert(ignore_permissions=True)
	doc.submit()


# =============================================================== STD script
def run_std(company, wh):
	from sap_valuation.sap_moving_average.cancellation import make_cancellation
	from sap_valuation.shared.accounts import get_inventory_account

	today = getdate(nowdate())

	def std_item(code, view="MTD"):
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0},
					limit=1, pluck="name")[0],
				"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
				"is_stock_item": 1, "valuation_method": "SAP Standard Cost",
				"settlement_view": view}).insert(ignore_permissions=True)
		return code

	def release_scv(item, sc, year=None, month=None):
		scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
			"item_code": item, "valid_from_year": year or today.year,
			"valid_from_month": month or today.month, "standard_cost": sc,
			"source_type": "MANUAL_OVERRIDE"})
		scv.insert(ignore_permissions=True)
		scv.release()
		return scv

	a = std_item("UAT-STD-A")
	# --- A1 no standard cost, no posting
	expect_block("STD TC-A1", lambda: make_pr(a, wh, 1, 10))

	# --- A2 release first version, no reval
	scv1 = release_scv(a, 10)
	tc("STD TC-A2", scv1.status == "RELEASED" and not frappe.db.exists(
		"Inventory Valuation Event", {"item_code": a, "std_trans": "Rev Beg"}))

	# --- A3 released version immutable
	def edit_released():
		fresh = frappe.get_doc("Item Standard Cost Version", scv1.name)
		fresh.standard_cost = 99
		fresh.save(ignore_permissions=True)
	expect_block("STD TC-A3", edit_released)

	# --- B1 receipt books PPV
	pr = make_pr(a, wh, 100, 12)
	b = ipb(a)
	inv_acct = get_inventory_account(company, a, wh)
	inv_gl = gl_of(pr.name, inv_acct)
	tc("STD TC-B1", flt(b.closing_value, 2) == 1000 and flt(b.period_standard_cost) == 10
		and inv_gl and flt(inv_gl[0].debit, 2) == 1000
		and flt(sum(g.debit for g in gl_of(pr.name)), 2) == 1200,
		f"{b.closing_value}/{b.period_standard_cost}")

	# --- B2 issue at standard, exactly two GL lines, no PPV
	dn = make_dn(a, wh, 30)
	tc("STD TC-B2", len(gl_of(dn.name)) == 2
		and flt(sum(g.debit for g in gl_of(dn.name)), 2) == 300, str(gl_of(dn.name)))

	# --- B3 negative stock blocked
	expect_block("STD TC-B3", lambda: make_dn(a, wh, 500))

	# --- B4 count shortage at standard, no PPV
	scd = frappe.get_doc({"doctype": "Stock Count", "company": company,
		"posting_date": nowdate(),
		"items": [{"item_code": a, "warehouse": wh, "counted_qty": 65}]})
	scd.insert(ignore_permissions=True)
	scd.submit()
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": scd.name},
		fields=["std_trans", "total_sc"])
	tc("STD TC-B4", ive and ive[0].std_trans == "SC-" and flt(ive[0].total_sc) == -50,
		str(ive))

	# --- B5 PI diff joins PPV pool
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": a, "qty": 100, "rate": 13, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pi.name, "std_trans": "LC"}, fields=["total_ac"])
	tc("STD TC-B5", ive and flt(ive[0].total_ac) == 100, str(ive))

	# --- C1 settlement run splits 300 -> 195/105
	run_doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run",
		"company": company, "period_year": today.year, "period_month": today.month,
		"run_type": "INITIAL_CLOSE"})
	run_doc.insert(ignore_permissions=True)
	run_doc.submit()
	sett = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": a, "cancelled": 0}, fields=["*"])
	# the settlement absorption seeds next month's balance row — read THIS month's
	b = ipb_period(a, today.year, today.month)
	tc("STD TC-C1", sett and flt(sett[0].es_var, 2) == 195 and flt(sett[0].out_var, 2) == 105
		and flt(b.ppv_pool, 2) == 300 and b.settlement == sett[0].name,
		f"{sett and sett[0].es_var}/{sett and sett[0].out_var}")

	# --- C2 settled period rejects postings
	expect_block("STD TC-C2", lambda: make_pr(a, wh, 5, 12,
		posting_date=str(get_first_day(nowdate()))))

	# --- C3 Reverse Settlement reopens; re-run recomputes
	sett_doc = frappe.get_doc("Inventory Period Settlement", sett[0].name)
	sett_doc.reverse()
	pr_late = make_pr(a, wh, 10, 12, posting_date=str(get_first_day(nowdate())))
	run2 = frappe.get_doc({"doctype": "Inventory Period Settlement Run",
		"company": company, "period_year": today.year, "period_month": today.month,
		"run_type": "INITIAL_CLOSE"})
	run2.insert(ignore_permissions=True)
	run2.submit()
	sett2 = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": a, "cancelled": 0}, fields=["ppv_pool", "es_qty"])
	tc("STD TC-C3", pr_late.docstatus == 1 and sett2
		and flt(sett2[0].ppv_pool, 2) == 320 and flt(sett2[0].es_qty) == 75,
		f"{sett2 and sett2[0].ppv_pool}/{sett2 and sett2[0].es_qty}")

	# --- D1 releasing a new version posts the triplet (need open period state)
	sett_doc2 = frappe.get_doc("Inventory Period Settlement",
		frappe.get_all("Inventory Period Settlement",
			filters={"item_code": a, "cancelled": 0}, pluck="name")[0])
	sett_doc2.reverse()  # reopen so the triplet's period accepts events
	qty_before = flt(ipb(a).closing_qty)
	scv2 = release_scv(a, 12)
	trips = {x.std_trans: flt(x.total_sc, 2) for x in frappe.get_all(
		"Inventory Valuation Event",
		filters={"item_code": a, "std_trans": ("in", ["Rev Beg", "REV In", "REV out"])},
		fields=["std_trans", "total_sc"])}
	b = ipb_period(a, today.year, today.month)
	# in = 110 (100 + late 10) x 2 = 220; out = 35 (issue 30 + count 5) x 2 = -70
	tc("STD TC-D1", trips.get("REV In") == 220.00 and trips.get("REV out") == -70.00
		and flt(b.period_standard_cost) == 12
		and flt(b.closing_value, 2) == flt(b.closing_qty) * 12,
		f"{trips}/{b.period_standard_cost}/{b.closing_value}")

	# --- D2 MR21 refused for STD
	expect_block("STD TC-D2", lambda: frappe.get_doc({"doctype": "Stock Revaluation",
		"company": company, "posting_date": nowdate(),
		"items": [{"item_code": a, "warehouse": wh, "new_valuation_rate": 15}]}).insert(
		ignore_permissions=True))

	# --- E1 SCE roll-up 22.00
	comp = std_item("UAT-STD-COMP")
	fg = std_item("UAT-STD-FG")
	release_scv(comp, 4)
	sce = frappe.get_doc({"doctype": "Standard Cost Estimate", "company": company,
		"item_code": fg, "valid_from_year": today.year, "valid_from_month": today.month,
		"overhead_percent": 10,
		"components": [{"item_code": comp, "qty": 5, "rate_source": "LEAF_STD"}]})
	sce.insert(ignore_permissions=True)
	sce.calculate()
	sce.mark()
	scv_name = sce.release()
	scv_fg = frappe.get_doc("Item Standard Cost Version", scv_name)
	tc("STD TC-E1", flt(sce.standard_cost, 2) == 22 and scv_fg.status == "RELEASED"
		and scv_fg.source_type == "SCE", f"{sce.standard_cost}")

	# --- E2 ISVC SoD + flip
	isvc = frappe.get_doc({"doctype": "Item Settlement View Change", "company": company,
		"item_code": fg, "to_view": "YTD", "reason": "uat"})
	isvc.insert(ignore_permissions=True)
	expect_block("STD TC-E2a", lambda: isvc.approve())
	approver = frappe.get_all("User",
		filters={"name": ("not in", [frappe.session.user, "Guest"])}, limit=1, pluck="name")[0]
	isvc.db_set({"status": "Approved", "approved_by": approver})
	isvc.reload()
	isvc.submit()
	tc("STD TC-E2b", frappe.db.get_value("Item", fg, "settlement_view") == "YTD")

	# --- F1 exact reversal at ORIGINAL cost after an SC change.
	# Uses a dedicated NON-invoiced receipt: reversing an invoiced receipt is
	# now warn-and-blocked (WA-0003-01 #12 policy), so we isolate the reversal
	# mechanics here. Receipt at SC 10 (1000/1200), then SC changes to 15 —
	# the mirror must still reverse at the ORIGINAL 1000, not the current SC.
	f1 = std_item("UAT-STD-F1")
	release_scv(f1, 10)
	pr = make_pr(f1, wh, 100, 12)
	release_scv(f1, 15)
	cxl = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr.name))
	cxl.submit()
	mirror = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cxl.name},
		fields=["std_trans", "total_sc", "total_ac", "reversal_of"])
	tc("STD TC-F1", mirror and mirror[0].std_trans == "Rec"
		and flt(mirror[0].total_sc) == -1000 and flt(mirror[0].total_ac) == -1200
		and mirror[0].reversal_of, str(mirror))

	# --- F2 settled-period reversal posts a forward delta (worked example)
	d = std_item("UAT-STD-DELTA")
	release_scv(d, 10)
	from sap_valuation.sap_standard_cost.engine import StdEngine
	eng = StdEngine(company, d)
	src = ("Item Standard Cost Version", scv1.name)
	eng.post(trans="Rec", qty=100, sc=10, ac=10.8, posting_date="2026-05-03", source=src)
	iss = eng.post(trans="Iss", qty=10, sc=10, posting_date="2026-05-10", source=src)
	eng.post(trans="Iss", qty=50, sc=10, posting_date="2026-05-15", source=src)
	sett_d = eng.close_period(year=2026, month=5, sc=10, source=src, entry_date="2026-06-01")
	eng.reverse_event(iss.name, source=src, posting_date="2026-06-05")
	delta = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": d, "std_trans": "Sett - Delta"}, fields=["total_sc"])
	tc("STD TC-F2", flt(sett_d.es_var, 2) == 32 and delta
		and flt(delta[0].total_sc, 2) == 8, f"{sett_d.es_var}/{delta}")

	# --- intent stamping on STD events
	std_intents = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": a, "std_trans": ("in", ["Sett", "Rev Beg", "REV In"])},
		fields=["std_trans", "posting_intent"])
	tc("STD TC-INT", std_intents and all(
		x.posting_intent == "SYSTEM_GENERATED" for x in std_intents), str(std_intents))

	# --- DR-22 defaults-as-templates + atomic ISVC
	grp = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
	frappe.db.set_value("Item Group", grp, "default_settlement_view", "YTD")
	t_item = frappe.get_doc({"doctype": "Item", "item_code": "UAT-STD-TPL",
		"item_name": "UAT-STD-TPL", "item_group": grp,
		"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
		"is_stock_item": 1, "valuation_method": "SAP Standard Cost"})
	t_item.insert(ignore_permissions=True)
	tc("STD TC-M10a", t_item.settlement_view == "YTD", t_item.settlement_view)
	frappe.db.set_value("Item Group", grp, "default_settlement_view", "MTD")
	tc("STD TC-M10b",
		frappe.db.get_value("Item", "UAT-STD-TPL", "settlement_view") == "YTD",
		"later default edit leaked onto existing item")
	frappe.db.set_value("Item Group", grp, "default_settlement_view", "")

	# atomic ISVC: unsettled activity is settled under the OLD view by the flip
	av_item = std_item("UAT-STD-ATOM")
	release_scv(av_item, 10)
	make_pr(av_item, wh, 50, 11)
	isvc2 = frappe.get_doc({"doctype": "Item Settlement View Change", "company": company,
		"item_code": av_item, "to_view": "YTD", "reason": "atomic flip"})
	isvc2.insert(ignore_permissions=True)
	isvc2.db_set({"status": "Approved", "approved_by": approver})
	isvc2.reload()
	isvc2.submit()
	sett_made = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": av_item, "cancelled": 0},
		fields=["settlement_view", "ppv_pool"])
	tc("STD TC-M10c", sett_made and sett_made[0].settlement_view == "MTD"
		and flt(sett_made[0].ppv_pool, 2) == 50
		and frappe.db.get_value("Item", av_item, "settlement_view") == "YTD",
		str(sett_made))

	# --- G MTD vs YTD pool carryforward difference
	m_item, y_item = std_item("UAT-STD-M"), std_item("UAT-STD-Y", view="YTD")
	for it in (m_item, y_item):
		release_scv(it, 10)
		e = StdEngine(company, it)
		s = ("Item Standard Cost Version", scv1.name)
		e.post(trans="Rec", qty=100, sc=10, ac=12, posting_date="2026-03-05", source=s)
		e.post(trans="Iss", qty=40, sc=10, posting_date="2026-03-10", source=s)
		e.close_period(year=2026, month=3, sc=10, source=s, entry_date="2026-04-01")
		e.post(trans="Rec", qty=50, sc=10, ac=11, posting_date="2026-04-05", source=s)
		e.close_period(year=2026, month=4, sc=10, source=s, entry_date="2026-05-01")
	m_apr = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": m_item, "period_month": 4, "cancelled": 0},
		fields=["ppv_pool"])[0]
	y_apr = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": y_item, "period_month": 4, "cancelled": 0},
		fields=["ppv_pool"])[0]
	# Mar pool 200: MTD carries only the inv share (200x60/100=120) -> 50+120=170;
	# YTD carries the FULL pool -> 50+200=250
	tc("STD TC-G", flt(m_apr.ppv_pool, 2) == 170 and flt(y_apr.ppv_pool, 2) == 250,
		f"MTD {m_apr.ppv_pool} vs YTD {y_apr.ppv_pool}")
