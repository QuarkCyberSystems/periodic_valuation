"""Edge-scenario smoke — bench --site badiav16.localhost execute sap_valuation.tests.smoke_edges.run

Covers the scenarios previously proven only in the reference simulator:
backdated C1/C2 (negative prior period, client-locked anchors), PI invoice
difference via stock ratio, purchase return with reference, company-scope
transfer neutrality, warehouse-scope two-leg transfer, full issue-out reset,
and SI update_stock issues. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, nowdate

from sap_valuation.tests.smoke_kernel import COMPANY, ensure_masters

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))


def make_item(code, include_warehouse=0):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "SAP Moving Average",
			"valuation_includes_warehouse": include_warehouse,
		}).insert(ignore_permissions=True)
	return code


def make_pr(item, wh, qty, rate, posting_date=None):
	pr = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": posting_date or nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh}],
	})
	pr.insert(ignore_permissions=True)
	pr.submit()
	return pr


def make_dn(item, wh, qty, posting_date=None):
	dn = frappe.get_doc({
		"doctype": "Delivery Note", "company": COMPANY, "customer": "_SMK Customer",
		"posting_date": posting_date or nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": 25, "warehouse": wh}],
	})
	dn.insert(ignore_permissions=True)
	dn.submit()
	return dn


def ipb(item, warehouse=""):
	rows = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": COMPANY, "item_code": item, "warehouse": warehouse},
		fields=["*"], order_by="period_year desc, period_month desc", limit=1,
	)
	return rows[0] if rows else None


def ipb_period(item, year, month, warehouse=""):
	rows = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": COMPANY, "item_code": item, "warehouse": warehouse,
			"period_year": year, "period_month": month},
		fields=["*"], limit=1,
	)
	return rows[0] if rows else None


def run(commit=False):
	wh = ensure_masters()
	# invoice differences require PI rates to diverge from the PR
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	prior = get_first_day(add_months(nowdate(), -1))
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({
			"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED",
		}).insert(ignore_permissions=True)

	# ============ C1: prior negative, current positive (anchors 252/587/30.8947)
	it = make_item("_SMK-C1")
	make_pr(it, wh, 10, 15, posting_date=str(prior))   # prior: 10/150
	make_dn(it, wh, 20, posting_date=str(prior))        # prior: -10/-150 frozen 15
	p = ipb_period(it, prior.year, prior.month)
	check("C1 prior -10/-150 frozen 15",
		flt(p.closing_qty) == -10 and flt(p.closing_value, 2) == -150 and flt(p.frozen_map) == 15,
		f"{p.closing_qty}/{p.closing_value}/{p.frozen_map}")

	make_pr(it, wh, 20, 17.5)                            # current: crosses to +10/175, MAP 17.5
	rv = frappe.get_doc({
		"doctype": "Stock Revaluation", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh, "new_valuation_rate": 20}],
	})
	rv.insert(ignore_permissions=True)
	rv.submit()                                          # current: 10/200, MAP 20 (client setup)
	c = ipb(it)
	check("C1 current 10/200 MAP 20",
		flt(c.closing_qty) == 10 and flt(c.closing_value, 2) == 200, f"{c.closing_qty}/{c.closing_value}")

	make_pr(it, wh, 9, 43, posting_date=str(prior))      # THE backdated receipt
	p = ipb_period(it, prior.year, prior.month)
	c = ipb(it)
	check("C1 prior after: -1/-15, PRD 252",
		flt(p.closing_qty) == -1 and flt(p.closing_value, 2) == -15 and flt(p.prd_value, 2) == -252,
		f"{p.closing_qty}/{p.closing_value}/{p.prd_value}")
	check("C1 current after: 19/587, MAP 30.8947",
		flt(c.closing_qty) == 19 and flt(c.closing_value, 2) == 587
		and flt(c.moving_avg_price, 4) == 30.8947,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	check("C1 absorb 252 in adjust bucket", flt(c.adjust_value, 2) == 252, str(c.adjust_value))

	# ============ C2: both periods negative (anchors 247/140/56, final 6@43)
	it = make_item("_SMK-C2")
	make_pr(it, wh, 5, 15, posting_date=str(prior))      # prior 5/75
	make_dn(it, wh, 10, posting_date=str(prior))          # prior -5/-75 frozen 15
	make_pr(it, wh, 2, 15)                                # current: opening -5 -> -3/-45 (PRD 0)
	c = ipb(it)
	check("C2 current -3/-45 frozen 15",
		flt(c.closing_qty) == -3 and flt(c.closing_value, 2) == -45 and flt(c.frozen_map) == 15,
		f"{c.closing_qty}/{c.closing_value}/{c.frozen_map}")

	make_pr(it, wh, 9, 43, posting_date=str(prior))      # backdated into negative prior
	p = ipb_period(it, prior.year, prior.month)
	c = ipb(it)
	check("C2 prior: closing 4/172, PRD 140",
		flt(p.closing_qty) == 4 and flt(p.closing_value, 2) == 172 and flt(p.prd_value, 2) == -140,
		f"{p.closing_qty}/{p.closing_value}/{p.prd_value}")
	check("C2 current: 6/258 MAP 43, absorb 56",
		flt(c.closing_qty) == 6 and flt(c.closing_value, 2) == 258
		and flt(c.moving_avg_price, 2) == 43 and flt(c.adjust_value, 2) == 56,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}/{c.adjust_value}")

	# ============ PI invoice difference via stock ratio
	it = make_item("_SMK-PIDIFF")
	pr = make_pr(it, wh, 100, 10)
	make_dn(it, wh, 20)                                   # ratio 80/100 = 0.8
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(),
		"items": [{
			"item_code": it, "qty": 100, "rate": 11, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name,
		}],
	})
	pi.insert(ignore_permissions=True)
	pi.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pi.name, "reason_code": "invoice_diff"},
		fields=["value_delta", "expense_portion"])
	check("PI diff 100: 80 inv / 20 exp (ratio 0.8)",
		ive and flt(ive[0].value_delta, 2) == 80.00 and flt(ive[0].expense_portion, 2) == 20.00,
		str(ive))
	c = ipb(it)
	check("PI diff MAP recalc", flt(c.moving_avg_price, 2) == flt((800 + 80) / 80, 2),
		str(c.moving_avg_price))

	# ============ purchase return WITH reference at original cost
	it = make_item("_SMK-RET")
	pr = make_pr(it, wh, 100, 10)
	make_pr(it, wh, 50, 20)                               # MAP 13.3333
	ret = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "is_return": 1, "return_against": pr.name,
		"items": [{
			"item_code": it, "qty": -30, "rate": 10, "warehouse": wh,
			"purchase_receipt_item": pr.items[0].name,
		}],
	})
	ret.insert(ignore_permissions=True)
	ret.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": ret.name}, fields=["reason_code", "value_delta"])
	c = ipb(it)
	check("purchase return w/ref at original 10 (300)",
		ive and ive[0].reason_code == "return_with_ref" and flt(ive[0].value_delta, 2) == -300.00,
		str(ive))
	check("return MAP recalc (1700/120)", flt(c.moving_avg_price, 4) == flt(1700 / 120, 4),
		str(c.moving_avg_price))

	# ============ company-scope transfer: value-neutral
	it = make_item("_SMK-TRF")
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	wh2 = f"_SMK Stores 2 - {abbr}"
	if not frappe.db.exists("Warehouse", wh2):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_SMK Stores 2",
			"company": COMPANY}).insert(ignore_permissions=True)
	make_pr(it, wh, 40, 10)
	before = ipb(it)
	se = frappe.get_doc({
		"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Transfer",
		"posting_date": nowdate(),
		"items": [{"item_code": it, "qty": 15, "s_warehouse": wh, "t_warehouse": wh2}],
	})
	se.insert(ignore_permissions=True)
	se.submit()
	after = ipb(it)
	check("company-scope transfer is value-neutral",
		flt(after.closing_qty) == flt(before.closing_qty)
		and flt(after.closing_value, 2) == flt(before.closing_value, 2)
		and flt(after.moving_avg_price, 6) == flt(before.moving_avg_price, 6),
		f"{after.closing_qty}/{after.closing_value}")
	check("transfer posts no GL",
		not frappe.db.exists("GL Entry", {"voucher_no": se.name, "is_cancelled": 0}))
	smes = frappe.get_all("Stock Movement Event", filters={"source_docname": se.name},
		pluck="movement_type")
	check("transfer SMEs both legs", sorted(smes) == ["transfer_in", "transfer_out"], str(smes))
	bin_qty = flt(frappe.db.get_value("Bin", {"item_code": it, "warehouse": wh2}, "actual_qty"))
	check("bin at destination", bin_qty == 15, str(bin_qty))

	# ============ warehouse-scope transfer: two-leg at source MAP
	it = make_item("_SMK-TRFW", include_warehouse=1)
	make_pr(it, wh, 40, 12)
	se = frappe.get_doc({
		"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Transfer",
		"posting_date": nowdate(),
		"items": [{"item_code": it, "qty": 10, "s_warehouse": wh, "t_warehouse": wh2}],
	})
	se.insert(ignore_permissions=True)
	se.submit()
	src = ipb(it, warehouse=wh)
	dst = ipb(it, warehouse=wh2)
	check("wh-scope source 30/360", flt(src.closing_qty) == 30 and flt(src.closing_value, 2) == 360,
		f"{src.closing_qty}/{src.closing_value}")
	check("wh-scope dest 10/120 MAP 12", flt(dst.closing_qty) == 10 and flt(dst.closing_value, 2) == 120,
		f"{dst.closing_qty}/{dst.closing_value}")

	# ============ full issue-out: counter + MAP reset
	it = make_item("_SMK-ZERO")
	make_pr(it, wh, 25, 10)
	make_dn(it, wh, 25)
	c = ipb(it)
	check("issue-out resets: qty 0 value 0 counter 0 MAP 0",
		flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0
		and flt(c.total_received_since_zero) == 0 and flt(c.moving_avg_price) == 0,
		f"{c.closing_qty}/{c.closing_value}/{c.total_received_since_zero}/{c.moving_avg_price}")

	# ============ SI update_stock issue
	it = make_item("_SMK-SI")
	make_pr(it, wh, 10, 10)
	si = frappe.get_doc({
		"doctype": "Sales Invoice", "company": COMPANY, "customer": "_SMK Customer",
		"posting_date": nowdate(), "update_stock": 1,
		"items": [{"item_code": it, "qty": 4, "rate": 30, "warehouse": wh}],
	})
	si.insert(ignore_permissions=True)
	si.submit()
	c = ipb(it)
	check("SI update_stock issue at MAP", flt(c.closing_qty) == 6 and flt(c.closing_value, 2) == 60,
		f"{c.closing_qty}/{c.closing_value}")

	# ============ count GAIN valued at period MAP (matrix row 3, second half)
	it = make_item("_SMK-GAIN")
	make_pr(it, wh, 10, 10)
	sc = frappe.get_doc({
		"doctype": "Stock Count", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh, "counted_qty": 13}],
	})
	sc.insert(ignore_permissions=True)
	sc.submit()
	c = ipb(it)
	sme = frappe.get_all("Stock Movement Event", filters={"source_docname": sc.name},
		pluck="movement_type")
	check("count gain +3 at MAP, MAP stable",
		flt(c.closing_qty) == 13 and flt(c.closing_value, 2) == 130
		and flt(c.moving_avg_price, 6) == 10 and sme == ["count_gain"],
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}/{sme}")

	# ============ SALES return with reference at original issue cost (matrix row 4)
	it = make_item("_SMK-SRET")
	make_pr(it, wh, 100, 10)
	dn = make_dn(it, wh, 30)                              # issue at MAP 10
	make_pr(it, wh, 50, 20)                               # MAP now (700+1000)/120
	sr = frappe.get_doc({
		"doctype": "Delivery Note", "company": COMPANY, "customer": "_SMK Customer",
		"posting_date": nowdate(), "is_return": 1, "return_against": dn.name,
		"items": [{
			"item_code": it, "qty": -10, "rate": 25, "warehouse": wh,
			"dn_detail": dn.items[0].name,
		}],
	})
	sr.insert(ignore_permissions=True)
	sr.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": sr.name}, fields=["reason_code", "value_delta"])
	c = ipb(it)
	check("sales return w/ref at original issue cost 10 (+100)",
		ive and ive[0].reason_code == "return_with_ref" and flt(ive[0].value_delta, 2) == 100.00,
		str(ive))
	check("sales return MAP recalc (1800/130)",
		flt(c.moving_avg_price, 4) == flt(1800 / 130, 4), str(c.moving_avg_price))

	# ============ ineligible cancellation: original period settled/frozen (matrix row 7)
	it = make_item("_SMK-FRZ")
	pr_old = make_pr(it, wh, 10, 10, posting_date=str(prior))
	prior_period_name = frappe.db.get_value(
		"Inventory Period", {"company": COMPANY, "period_year": prior.year,
			"period_month": prior.month})
	frappe.db.set_value("Inventory Period", prior_period_name, "status", "SETTLED_FROZEN")
	from sap_valuation.sap_moving_average.cancellation import make_cancellation
	cxl_name = make_cancellation("Purchase Receipt", pr_old.name)
	cxl = frappe.get_doc("Purchase Receipt", cxl_name)
	try:
		cxl.submit()
		check("frozen-period cancellation blocked", False, "submit succeeded")
	except frappe.ValidationError as e:
		check("frozen-period cancellation blocked",
			"Not Eligible" in str(e) or "no longer be cancelled" in str(e), str(e)[:120])
	frappe.db.set_value("Inventory Period", prior_period_name, "status", "PREV_OPEN_UNSETTLED")

	# ============ GL AUDIT: Stock Reconciliation opening stock (client defect)
	it = make_item("_SMK-RECO")
	tsa = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0,
		"account_type": "Temporary"}, limit=1, pluck="name")
	opening_acct = tsa[0] if tsa else frappe.get_all("Account",
		filters={"company": COMPANY, "is_group": 0, "root_type": "Liability"}, limit=1, pluck="name")[0]
	sr = frappe.get_doc({
		"doctype": "Stock Reconciliation", "company": COMPANY, "purpose": "Opening Stock",
		"posting_date": nowdate(), "set_posting_time": 1, "expense_account": opening_acct,
		"items": [{"item_code": it, "warehouse": wh, "qty": 500, "valuation_rate": 12}],
	})
	sr.insert(ignore_permissions=True)
	sr.submit()
	c = ipb(it)
	check("SR opening: 500/6000 MAP 12",
		flt(c.closing_qty) == 500 and flt(c.closing_value, 2) == 6000
		and flt(c.moving_avg_price, 6) == 12,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	gl = frappe.get_all("GL Entry", filters={"voucher_no": sr.name, "is_cancelled": 0},
		fields=["account", "debit", "credit", "valuation_event_id"])
	check("SR opening hits GL, fully tagged",
		len(gl) == 2 and all(g.valuation_event_id for g in gl)
		and flt(sum(g.debit for g in gl), 2) == 6000, str(gl))

	# SR correction: qty down to 480 AND rate up to 12.50 in one row
	sr2 = frappe.get_doc({
		"doctype": "Stock Reconciliation", "company": COMPANY,
		"purpose": "Stock Reconciliation",
		"posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": frappe.get_cached_value("Company", COMPANY, "stock_adjustment_account"),
		"items": [{"item_code": it, "warehouse": wh, "qty": 480, "valuation_rate": 12.5}],
	})
	sr2.insert(ignore_permissions=True)
	sr2.submit()
	c = ipb(it)
	check("SR correction: 480/6000 MAP 12.5",
		flt(c.closing_qty) == 480 and flt(c.closing_value, 2) == 6000
		and flt(c.moving_avg_price, 4) == 12.5,
		f"{c.closing_qty}/{c.closing_value}/{c.moving_avg_price}")
	ives = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": sr2.name}, fields=["reason_code", "value_delta"], order_by="name")
	check("SR correction decomposed into count + reval",
		sorted(i.reason_code for i in ives) == ["count_diff", "revaluation"], str(ives))

	# ============ GL AUDIT: PI update_stock posts stock GL exactly once
	it = make_item("_SMK-PIUS")
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "update_stock": 1,
		"items": [{"item_code": it, "qty": 10, "rate": 15, "warehouse": wh}],
	})
	pi.insert(ignore_permissions=True)
	pi.submit()
	from sap_valuation.shared.accounts import get_inventory_account
	inv_acct = get_inventory_account(COMPANY, it, wh)
	stock_lines = frappe.get_all("GL Entry",
		filters={"voucher_no": pi.name, "account": inv_acct, "is_cancelled": 0},
		fields=["debit", "credit", "valuation_event_id"])
	check("PI update_stock: exactly one tagged stock debit (no double post)",
		len(stock_lines) == 1 and stock_lines[0].valuation_event_id
		and flt(stock_lines[0].debit, 2) == 150, str(stock_lines))

	# ============ GL AUDIT: DN issue expense never falls to SRBNB
	it = make_item("_SMK-EXP")
	make_pr(it, wh, 10, 10)
	dn = make_dn(it, wh, 4)
	srbnb = frappe.get_cached_value("Company", COMPANY, "stock_received_but_not_billed")
	dn_gl = frappe.get_all("GL Entry", filters={"voucher_no": dn.name, "is_cancelled": 0},
		fields=["account", "debit"])
	check("DN issue debit is an expense account, not SRBNB",
		dn_gl and all(g.account != srbnb for g in dn_gl), str(dn_gl))

	# ============ batch/serial items cannot select the SAP method
	try:
		frappe.get_doc({
			"doctype": "Item", "item_code": "_SMK-BATCH", "item_name": "x",
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "has_batch_no": 1, "create_new_batch": 1,
			"valuation_method": "SAP Moving Average",
		}).insert(ignore_permissions=True)
		check("batch item blocked from SAP method", False, "insert succeeded")
	except frappe.ValidationError:
		check("batch item blocked from SAP method", True)

	# ============ negative-MAP guard (WA-0003-01 item 14): a value event that
	# would drive inventory value < 0 while positive stock remains is blocked
	from sap_valuation.sap_moving_average.kernel import post_value_event
	ng = make_item("_SMK-NEGMAP")
	make_pr(ng, wh, 100, 10); make_dn(ng, wh, 90)          # hold 10 @ value 100
	srbnb = frappe.get_cached_value("Company", COMPANY, "stock_received_but_not_billed")
	try:
		post_value_event(COMPANY, ng, wh, source=("Purchase Invoice", "_NEG", "x"),
			posting_date=nowdate(), reason="invoice_diff", value_delta=-300, offset_account=srbnb)
		check("negative-MAP value event blocked", False, "posted")
	except frappe.ValidationError as e:
		check("negative-MAP value event blocked", "negative" in str(e).lower())

	# ============ Stock Ageing works for routed items (OI-8 / DR-27):
	# the report replays SLE-compatible rows FIFO by date — valuation-agnostic
	age_it = make_item("_SMK-AGE")
	make_pr(age_it, wh, 100, 10, posting_date=add_days(nowdate(), -20))
	make_pr(age_it, wh, 50, 12)
	make_dn(age_it, wh, 40)
	from erpnext.stock.report.stock_ageing.stock_ageing import execute as _age_exec

	res = _age_exec(frappe._dict({"company": COMPANY, "to_date": nowdate(),
		"range": "10, 30, 60", "item_code": age_it, "warehouse": wh}))
	cols, rows = res[0], [r for r in res[1] if r[0] == age_it]
	if rows:
		row = dict(zip([c.get("fieldname") for c in cols], rows[0]))
		check("Stock Ageing: FIFO ages exact for routed item",
			flt(row.get("qty")) == 110 and int(row.get("earliest") or row.get("earliest_age") or 0) == 20
			and flt(row.get("average_age"), 2) == flt(1200 / 110, 2),
			str(row))
	else:
		check("Stock Ageing: FIFO ages exact for routed item", False, "item missing from report")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("edge smoke failures: " + "; ".join(x[0] for x in failed))
