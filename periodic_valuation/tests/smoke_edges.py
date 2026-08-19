"""Edge-scenario smoke — bench --site badiav16.localhost execute periodic_valuation.tests.smoke_edges.run

Covers the scenarios previously proven only in the reference simulator:
backdated C1/C2 (negative prior period, client-locked anchors), PI invoice
difference via stock ratio, purchase return with reference, company-scope
transfer neutrality, warehouse-scope two-leg transfer, full issue-out reset,
and SI update_stock issues. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters

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
			"is_stock_item": 1, "valuation_method": "Periodic Moving Average",
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

	# ============ full issue-out: counter resets, MAP RETAINED (ruled 2026-08-18;
	# client behaviour review MAT-STE-2026-00150 — SAP material-master behaviour)
	it = make_item("_SMK-ZERO")
	make_pr(it, wh, 25, 10)
	make_dn(it, wh, 25)
	c = ipb(it)
	check("issue-out: qty 0 value 0 counter 0, MAP retained at 10",
		flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0
		and flt(c.total_received_since_zero) == 0 and flt(c.moving_avg_price) == 10,
		f"{c.closing_qty}/{c.closing_value}/{c.total_received_since_zero}/{c.moving_avg_price}")
	# ...and the retained MAP is what an issue FROM the zero balance costs at.
	# When the MAP was zeroed here, frozen_map captured 0 and the units left
	# stock at no cost: 25 in, 25 out, then 5 more out booked ZERO COGS while
	# the same 30 issued in one movement booked 300.
	dn_z = make_dn(it, wh, 5)
	c = ipb(it)
	ive_z = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": dn_z.name}, fields=["value_delta"])
	check("issue from zero books COGS at the retained MAP",
		flt(ive_z[0].value_delta, 2) == -50 and flt(c.closing_qty) == -5
		and c.is_negative == 1 and flt(c.frozen_map) == 10,
		f"value_delta={ive_z and ive_z[0].value_delta} frozen={c.frozen_map}")
	# a crossing receipt still re-prices the pool exactly as before
	make_pr(it, wh, 20, 14)
	c = ipb(it)
	check("crossing receipt after zero re-prices to its own rate",
		flt(c.closing_qty) == 15 and flt(c.moving_avg_price) == 14,
		f"{c.closing_qty}/{c.moving_avg_price}")

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
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
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
	from periodic_valuation.shared.accounts import get_inventory_account
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

	# ============ batch/serial items cannot select the periodic method
	try:
		frappe.get_doc({
			"doctype": "Item", "item_code": "_SMK-BATCH", "item_name": "x",
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "has_batch_no": 1, "create_new_batch": 1,
			"valuation_method": "Periodic Moving Average",
		}).insert(ignore_permissions=True)
		check("batch item blocked from periodic method", False, "insert succeeded")
	except frappe.ValidationError:
		check("batch item blocked from periodic method", True)

	# ============ Stock Reconciliation corrects Bin<->IPB drift (SR fix): the
	# form reads current from the IPB, the posting SETS the Bin to the counted
	# absolute, and the drift detector flags divergence.
	from erpnext.stock.utils import get_or_make_bin
	from periodic_valuation.shared.integrity import check_bin_ipb_drift
	dr = make_item("_SMK-SRDRIFT")
	make_pr(dr, wh, 100, 10)                                    # IPB 100 @ 10
	binname = get_or_make_bin(dr, wh)
	frappe.db.set_value("Bin", binname, "actual_qty", 106, update_modified=False)  # drift
	flagged = [x for x in check_bin_ipb_drift(COMPANY) if x["item_code"] == dr]
	check("drift detector flags Bin!=IPB", flagged and flt(flagged[0]["drift"]) == 6, str(flagged))
	diff_acct = (frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0,
		"account_name": ("like", "%Temporary%")}, limit=1, pluck="name")
		or frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0,
		"account_name": ("like", "%Stock Adjustment%")}, limit=1, pluck="name"))[0]
	sr = frappe.get_doc({"doctype": "Stock Reconciliation", "company": COMPANY,
		"purpose": "Stock Reconciliation", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": diff_acct,
		"items": [{"item_code": dr, "warehouse": wh, "qty": 120, "valuation_rate": 12}]})
	sr.insert(ignore_permissions=True)
	check("SR form current from IPB (100), not Bin (106): diff 120*12-100*10=440",
		flt(sr.items[0].amount_difference, 2) == 440.00, str(sr.items[0].amount_difference))
	sr.submit()
	post_bin = flt(frappe.db.get_value("Bin", binname, "actual_qty"))
	post_ipb = flt(ipb(dr).closing_qty)
	check("SR sets Bin to counted absolute; Bin==IPB==120 (drift erased)",
		post_bin == 120 and post_ipb == 120, f"Bin {post_bin} IPB {post_ipb}")
	check("no residual drift after reconciliation",
		not [x for x in check_bin_ipb_drift(COMPANY) if x["item_code"] == dr])

	# ============ returns net against origin bucket (WA-0003-01 item 6): a
	# purchase return reduces net In/receipts, a sales return reduces net
	# Out/issues (matches STD). Closing/MAP unchanged; only the breakdown.
	rn = make_item("_SMK-RETNET")
	make_pr(rn, wh, 100, 10)                    # In 100
	make_dn(rn, wh, 40)                         # Out 40 -> hold 60
	pr_ret = frappe.get_doc({"doctype": "Purchase Receipt", "company": COMPANY,
		"supplier": "_SMK Supplier", "posting_date": nowdate(), "set_posting_time": 1,
		"is_return": 1, "return_against": frappe.get_all("Purchase Receipt",
			filters={"docstatus": 1}, order_by="creation desc", limit=1, pluck="name")[0],
		"items": [{"item_code": rn, "warehouse": wh, "qty": -20, "rate": 10}]})
	pr_ret.insert(ignore_permissions=True); pr_ret.submit()
	rb = ipb(rn)
	check("purchase return nets down In (receipt 80, not issue 60)",
		flt(rb.receipt_qty) == 80 and flt(rb.issue_qty) == 40,
		f"receipt {rb.receipt_qty} issue {rb.issue_qty}")
	check("closing unchanged by netting (40 on hand @ 10)",
		flt(rb.closing_qty) == 40 and flt(rb.closing_value, 2) == 400,
		f"{rb.closing_qty}/{rb.closing_value}")

	# ============ PI reversal is a debit note (WA-0003-01 item 10): reverses
	# party GL + valuation, un-bills the receipt so it can be re-invoiced
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	iv = make_item("_SMK-INVREV")
	prv = make_pr(iv, wh, 100, 10)
	piv = frappe.get_doc({"doctype": "Purchase Invoice", "company": COMPANY,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": iv, "qty": 100, "rate": 13, "warehouse": wh,
			"purchase_receipt": prv.name, "pr_detail": prv.items[0].name}]})
	piv.insert(ignore_permissions=True); piv.submit()
	check("PI diff lifts MAP to 13", flt(ipb(iv).moving_avg_price, 2) == 13.00, str(ipb(iv).moving_avg_price))
	cxv = frappe.get_doc("Purchase Invoice", make_cancellation("Purchase Invoice", piv.name))
	cxv.submit()
	pbv = frappe.db.get_value("Purchase Receipt", prv.name, ["per_billed", "status"])
	check("PI reversal un-bills receipt (0 / To Bill) and restores MAP 10",
		flt(pbv[0]) == 0 and pbv[1] == "To Bill" and flt(ipb(iv).moving_avg_price, 2) == 10.00,
		f"{pbv} MAP {ipb(iv).moving_avg_price}")
	credit = frappe.get_cached_value("Company", COMPANY, "default_payable_account")
	cr_net = frappe.db.sql("""select coalesce(sum(debit-credit),0) from `tabGL Entry`
		where voucher_no=%s and account=%s and is_cancelled=0""", (cxv.name, credit))[0][0]
	check("PI reversal debits Creditors (party GL reversed)", flt(cr_net, 2) == 1300.00, str(cr_net))
	prv2 = frappe.get_doc({"doctype": "Purchase Invoice", "company": COMPANY,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": iv, "qty": 100, "rate": 13, "warehouse": wh,
			"purchase_receipt": prv.name, "pr_detail": prv.items[0].name}]})
	prv2.insert(ignore_permissions=True); prv2.submit()
	check("receipt can be re-invoiced after reversal", prv2.docstatus == 1)

	# ============ backdated Stock Count carryover (WA-0003-01 item 8): a count
	# dated in the previous period compares against THAT period's on-hand and
	# cascades the adjustment into the current period's carryover
	from periodic_valuation.tests.smoke_kernel import get_company as _gc
	_prior = get_first_day(add_months(nowdate(), -1))
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": _prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": _prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	bc = make_item("_SMK-BDCOUNT")
	make_pr(bc, wh, 100, 10, posting_date=str(_prior))    # prior: 100/1000
	make_pr(bc, wh, 10, 10)                                # current: +10
	scb = frappe.get_doc({"doctype": "Stock Count", "company": COMPANY,
		"posting_date": str(_prior), "set_posting_time": 1,
		"items": [{"item_code": bc, "warehouse": wh, "counted_qty": 90}]})
	scb.insert(ignore_permissions=True)
	check("backdated count compares to prior period (diff -10, not -20)",
		flt(scb.items[0].quantity_difference) == -10, str(scb.items[0].quantity_difference))
	scb.submit()
	pbk = ipb_period(bc, _prior.year, _prior.month)
	curk = ipb(bc)
	check("backdated count: prior 90/900, current cascades to 100/1000",
		flt(pbk.closing_qty) == 90 and flt(pbk.closing_value, 2) == 900
		and flt(curk.closing_qty) == 100 and flt(curk.closing_value, 2) == 1000,
		f"prior {pbk.closing_qty}/{pbk.closing_value} cur {curk.closing_qty}/{curk.closing_value}")

	# ============ negative-MAP guard (WA-0003-01 item 14): a value event that
	# would drive inventory value < 0 while positive stock remains is blocked
	from periodic_valuation.periodic_moving_average.kernel import post_value_event
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

	# ============ period-aware rate resolution (client meeting 2026-08-12)
	# A backdated issue must be valued -- and displayed -- at the MAP of ITS
	# OWN period, not the latest one. Prior period MAP 920, current 1072.
	it = make_item("_SMK-PERIODRATE")
	make_pr(it, wh, 100, 920, posting_date=str(prior))
	make_pr(it, wh, 100, 1072)
	pp = ipb_period(it, prior.year, prior.month)
	cur = getdate(nowdate())
	cp = ipb_period(it, cur.year, cur.month)
	check("period rate: prior MAP 920, current MAP 996",
		flt(pp.moving_avg_price) == 920 and flt(cp.moving_avg_price) == 996,
		f"{pp.moving_avg_price}/{cp.moving_avg_price}")

	from periodic_valuation.shared.routing import get_incoming_rate

	check("period rate: resolver as of prior period returns 920",
		flt(get_incoming_rate({"item_code": it, "company": COMPANY, "warehouse": wh,
			"posting_date": str(prior)}, "Periodic Moving Average")) == 920,
		str(get_incoming_rate({"item_code": it, "company": COMPANY, "warehouse": wh,
			"posting_date": str(prior)}, "Periodic Moving Average")))
	check("period rate: resolver as of today returns 996",
		flt(get_incoming_rate({"item_code": it, "company": COMPANY, "warehouse": wh,
			"posting_date": nowdate()}, "Periodic Moving Average")) == 996,
		str(get_incoming_rate({"item_code": it, "company": COMPANY, "warehouse": wh,
			"posting_date": nowdate()}, "Periodic Moving Average")))

	dn = make_dn(it, wh, 10, posting_date=str(prior))
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": dn.name},
		fields=["value_delta", "map_before"])[0]
	check("period rate: backdated issue valued at 920, not 996",
		flt(ive.value_delta, 2) == -9200 and flt(ive.map_before) == 920,
		f"{ive.value_delta}/{ive.map_before}")
	sle = frappe.get_all("Stock Ledger Entry",
		filters={"voucher_no": dn.name, "is_cancelled": 0},
		fields=["stock_value_difference"])[0]
	check("period rate: backdated issue SLE value is -9,200",
		flt(sle.stock_value_difference, 2) == -9200, str(sle.stock_value_difference))

	# Stock Reconciliation prefill reads the same period balance
	from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import get_stock_balance_for

	bal = get_stock_balance_for(it, wh, str(prior), "12:00:00")
	check("period rate: reconciliation prefill uses the period MAP",
		flt(bal["rate"]) == 920, str(bal["rate"]))

	# ============ cancellation nets the ORIGIN bucket (OI-5 / DR-31, client
	# behaviour review 2026-08-18 MAT-PRE-2026-00375: "Netting the OUT/IN,
	# same behaviour STD" — the rule returns already follow, WA-0003-01 6/7)
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	# (a) cancelling a receipt nets In, not inflates Out — and matches the
	# period picture a purchase RETURN of the same goods paints
	it = make_item("_SMK-CXNET")
	pr = make_pr(it, wh, 1000, 10)
	make_dn(it, wh, 200)
	cn = make_cancellation("Purchase Receipt", pr.name)
	frappe.get_doc("Purchase Receipt", cn).submit()
	c = ipb(it)
	check("cancel receipt nets In (in 0 / out 200), closing unchanged",
		flt(c.receipt_qty) == 0 and flt(c.receipt_value, 2) == 0
		and flt(c.issue_qty) == 200 and flt(c.closing_qty) == -200
		and flt(c.closing_value, 2) == -2000
		and flt(c.total_received_since_zero) == 0,
		f"in={c.receipt_qty}/{c.receipt_value} out={c.issue_qty} close={c.closing_qty} "
		f"counter={c.total_received_since_zero}")

	# (b) PARTIAL cancellation: the counter nets with the bucket, so a later
	# landed cost splits against surviving receipts, not cancelled ones
	it = make_item("_SMK-CXPART")
	pr = make_pr(it, wh, 1000, 10)
	make_dn(it, wh, 200)
	cn = make_cancellation("Purchase Receipt", pr.name)
	cx = frappe.get_doc("Purchase Receipt", cn)
	cx.items[0].qty = 500
	cx.items[0].received_qty = 500
	cx.save(ignore_permissions=True)
	cx.submit()
	c = ipb(it)
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": nowdate(),
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name, "supplier": "_SMK Supplier",
			"grand_total": pr.grand_total}],
		"taxes": [{"expense_account": frappe.get_all("Account",
			filters={"company": COMPANY, "is_group": 0, "root_type": "Expense"},
			limit=1, pluck="name")[0], "description": "Freight", "amount": 100}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	lc_ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": lcv.name, "reason_code": "landed_cost"},
		fields=["value_delta", "expense_portion"])[0]
	# DR-32: the LCV covers the RECEIPT's 1000 units; 300 remain on hand ->
	# coverage 0.3 -> 30/70. (The since-zero counter still nets with the
	# cancellation — asserted here — but no longer drives value splits.)
	check("partial cancel: in 500 / counter 500; LCV coverage 300/1000 splits 30/70",
		flt(c.receipt_qty) == 500 and flt(c.total_received_since_zero) == 500
		and flt(c.closing_qty) == 300
		and flt(lc_ive.value_delta, 2) == 30 and flt(lc_ive.expense_portion, 2) == 70,
		f"in={c.receipt_qty} counter={c.total_received_since_zero} close={c.closing_qty} "
		f"split={lc_ive.value_delta}/{lc_ive.expense_portion}")

	# (c) cancelling an issue nets Out
	it = make_item("_SMK-CXISS")
	make_pr(it, wh, 500, 10)
	dn = make_dn(it, wh, 100)
	cn = make_cancellation("Delivery Note", dn.name)
	frappe.get_doc("Delivery Note", cn).submit()
	c = ipb(it)
	check("cancel issue nets Out (in 500 / out 0), closing 500",
		flt(c.receipt_qty) == 500 and flt(c.issue_qty) == 0
		and flt(c.issue_value, 2) == 0 and flt(c.closing_qty) == 500,
		f"in={c.receipt_qty} out={c.issue_qty}/{c.issue_value} close={c.closing_qty}")

	# (d) cancelling a purchase RETURN un-nets what the return netted
	it = make_item("_SMK-CXRET")
	pr = make_pr(it, wh, 100, 10)
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
	mid = ipb(it)
	cn = make_cancellation("Purchase Receipt", ret.name)
	frappe.get_doc("Purchase Receipt", cn).submit()
	c = ipb(it)
	check("cancel of a purchase return restores In and the counter (70 -> 100)",
		flt(mid.receipt_qty) == 70 and flt(mid.total_received_since_zero) == 70
		and flt(c.receipt_qty) == 100 and flt(c.total_received_since_zero) == 100
		and flt(c.closing_qty) == 100 and flt(c.closing_value, 2) == 1000,
		f"after return in={mid.receipt_qty}/{mid.total_received_since_zero}; after cancel "
		f"in={c.receipt_qty}/{c.total_received_since_zero} close={c.closing_qty}")

	# ============ valuation scope vs PHYSICAL warehouse (client approval
	# comment 1, 2026-08-18): a count/reconciliation is a physical exercise on
	# ONE warehouse — company-scope items must compare against that warehouse's
	# stock, valued at the scope MAP, never against the scope total
	from periodic_valuation.periodic_moving_average.api import get_current_state

	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	wh2_name = f"_SMK Depot - {abbr}"
	if not frappe.db.exists("Warehouse", wh2_name):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_SMK Depot",
			"company": COMPANY}).insert(ignore_permissions=True)
	it = make_item("_SMK-SCOPE")                     # company scope (flag OFF)
	make_pr(it, wh, 60, 10)
	make_pr(it, wh2_name, 40, 10)                    # scope total 100, MAP 10

	st = get_current_state(COMPANY, it, wh, physical=1)
	check("physical state: warehouse qty 60, scope MAP 10",
		flt(st["closing_qty"]) == 60 and flt(st["moving_avg_price"]) == 10,
		f"{st['closing_qty']}/{st['moving_avg_price']}")
	bal = get_stock_balance_for(it, wh, nowdate(), "12:00:00")
	check("reconciliation prefill shows the warehouse's 60, not the scope's 100",
		flt(bal["qty"]) == 60 and flt(bal["rate"]) == 10, f"{bal['qty']}/{bal['rate']}")

	sc_doc = frappe.get_doc({"doctype": "Stock Count", "company": COMPANY,
		"posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh, "counted_qty": 55}]})
	sc_doc.insert(ignore_permissions=True)
	row = sc_doc.items[0]
	check("count row compares against the warehouse (60), difference -5",
		flt(row.current_qty) == 60 and flt(row.quantity_difference) == -5,
		f"{row.current_qty}/{row.quantity_difference}")
	sc_doc.submit()
	c = ipb(it)
	from erpnext.stock.utils import get_or_make_bin
	bin_a = flt(frappe.db.get_value("Bin", get_or_make_bin(it, wh), "actual_qty"))
	bin_b = flt(frappe.db.get_value("Bin", get_or_make_bin(it, wh2_name), "actual_qty"))
	check("count posts the real 5-unit loss: scope 95/950, bins 55 + 40",
		flt(c.closing_qty) == 95 and flt(c.closing_value, 2) == 950
		and bin_a == 55 and bin_b == 40,
		f"scope {c.closing_qty}/{c.closing_value} bins {bin_a}/{bin_b}")

	# Stock Reconciliation: qty-only row targets the warehouse's physical stock
	sr = frappe.get_doc({"doctype": "Stock Reconciliation", "company": COMPANY,
		"posting_date": nowdate(), "set_posting_time": 1, "purpose": "Stock Reconciliation",
		"expense_account": frappe.get_cached_value("Company", COMPANY, "stock_adjustment_account"),
		"items": [{"item_code": it, "warehouse": wh, "qty": 58}]})
	sr.insert(ignore_permissions=True)
	sr.submit()
	c = ipb(it)
	bin_a = flt(frappe.db.get_value("Bin", get_or_make_bin(it, wh), "actual_qty"))
	check("reconciliation sets the warehouse to 58 (+3): scope 98/980, bin 58",
		flt(c.closing_qty) == 98 and flt(c.closing_value, 2) == 980 and bin_a == 58,
		f"scope {c.closing_qty}/{c.closing_value} bin {bin_a}")

	# a RATE on a multi-warehouse company scope is refused — value is scope-level
	sr2 = frappe.get_doc({"doctype": "Stock Reconciliation", "company": COMPANY,
		"posting_date": nowdate(), "set_posting_time": 1, "purpose": "Stock Reconciliation",
		"expense_account": frappe.get_cached_value("Company", COMPANY, "stock_adjustment_account"),
		"items": [{"item_code": it, "warehouse": wh, "qty": 58, "valuation_rate": 12}]})
	sr2.insert(ignore_permissions=True)
	try:
		sr2.submit()
		check("rate on a multi-warehouse company scope is refused", False, "not blocked")
	except frappe.ValidationError:
		check("rate on a multi-warehouse company scope is refused", True)

	# ============ inventory / P&L decomposition on EVERY event row (client
	# approval comment 2, 2026-08-18: portions "populated in each relevant
	# transaction, not only in the LC transaction"). Display-only; the rule is
	# inventory_portion = net inventory effect, expense_portion = signed P&L
	# side (positive = debit), zero when the offset is balance-sheet (GR/IR).
	def portions(docname, reason=None):
		f = {"source_docname": docname}
		if reason:
			f["reason_code"] = reason
		e = frappe.get_all("Inventory Valuation Event", filters=f,
			fields=["reason_code", "value_delta", "inventory_portion", "expense_portion",
				"prd_amount"], order_by="creation")[0]
		return e

	it = make_item("_SMK-PORTIONS")
	pr = make_pr(it, wh, 100, 10)
	e = portions(pr.name)
	check("portions: receipt inv=1000 exp=0 (GR/IR offset)",
		flt(e.inventory_portion, 2) == 1000 and flt(e.expense_portion, 2) == 0, str(e))

	dn = make_dn(it, wh, 30)
	e = portions(dn.name)
	check("portions: issue inv=-300 exp=+300 (COGS debit)",
		flt(e.inventory_portion, 2) == -300 and flt(e.expense_portion, 2) == 300, str(e))

	sc_doc = frappe.get_doc({"doctype": "Stock Count", "company": COMPANY,
		"posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh, "counted_qty": 65}]})
	sc_doc.insert(ignore_permissions=True)
	sc_doc.submit()
	e = portions(sc_doc.name)
	check("portions: count loss inv=-50 exp=+50 (variance debit)",
		flt(e.inventory_portion, 2) == -50 and flt(e.expense_portion, 2) == 50, str(e))

	rv = frappe.get_doc({"doctype": "Stock Revaluation", "company": COMPANY,
		"posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh, "new_valuation_rate": 12}]})
	rv.insert(ignore_permissions=True)
	rv.submit()
	e = portions(rv.name)
	check("portions: revaluation +130 inv=+130 exp=-130 (gain credit)",
		flt(e.inventory_portion, 2) == 130 and flt(e.expense_portion, 2) == -130, str(e))

	# negative-stock receipt: P&L side is exactly the PRD component
	it2 = make_item("_SMK-PORT-NEG")
	make_pr(it2, wh, 10, 10)
	make_dn(it2, wh, 30)                              # -20, frozen 10
	pr2 = make_pr(it2, wh, 5, 14)                     # stays negative: PRD (14-10)x5=20
	e = portions(pr2.name)
	check("portions: negative-stock receipt inv=net 50 exp=PRD 20",
		flt(e.inventory_portion, 2) == 50 and flt(e.expense_portion, 2) == 20
		and flt(e.prd_amount, 2) == 20, str(e))

	# purchase return offsets GR/IR -> no P&L side; cancellation mirrors the original
	it3 = make_item("_SMK-PORT-RET")
	pr3 = make_pr(it3, wh, 100, 10)
	ret3 = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "is_return": 1, "return_against": pr3.name,
		"items": [{"item_code": it3, "qty": -20, "rate": 10, "warehouse": wh,
			"purchase_receipt_item": pr3.items[0].name}]})
	ret3.insert(ignore_permissions=True)
	ret3.submit()
	e = portions(ret3.name)
	check("portions: purchase return inv=-200 exp=0 (GR/IR offset)",
		flt(e.inventory_portion, 2) == -200 and flt(e.expense_portion, 2) == 0, str(e))

	it4 = make_item("_SMK-PORT-CX")
	dn4_src = make_pr(it4, wh, 50, 10)
	dn4 = make_dn(it4, wh, 20)                        # issue: exp +200
	cn4 = make_cancellation("Delivery Note", dn4.name)
	frappe.get_doc("Delivery Note", cn4).submit()
	e = portions(cn4)
	check("portions: cancellation of an issue mirrors it (inv=+200 exp=-200)",
		flt(e.inventory_portion, 2) == 200 and flt(e.expense_portion, 2) == -200, str(e))

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()

	if failed:
		raise Exception("edge smoke failures: " + "; ".join(x[0] for x in failed))
