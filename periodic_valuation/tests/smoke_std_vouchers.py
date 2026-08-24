"""STD voucher integration - bench --site <site> execute periodic_valuation.tests.smoke_std_vouchers.run

Drives REAL vouchers through the STD routing: PR -> Rec (SC + PPV legs),
DN -> Iss at SC, Stock Count -> SC-, PI diff -> PPV, then a Settlement Run.
Rolled back unless commit=True.
"""

import frappe
from frappe.utils import flt, get_first_day, getdate, nowdate

from periodic_valuation.tests.smoke_edges import make_dn, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company
from periodic_valuation.tests.smoke_std import ensure_std_masters

CHECKS = []
ITEM = "_STDV-ITEM"


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def run(commit=False):
	wh = ensure_masters()
	company = get_company()
	ensure_std_masters(company)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)

	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)

	today = getdate(nowdate())
	scv = frappe.get_doc({
		"doctype": "Item Standard Cost Version", "company": company, "item_code": ITEM,
		"valid_from_year": today.year, "valid_from_month": today.month, "standard_cost": 10,
		"source_type": "MANUAL_OVERRIDE",
	})
	scv.insert(ignore_permissions=True)
	scv.release()

	# ---- PR 100 @ AC 12 -> Rec at SC 10, PPV 200
	pr = make_pr(ITEM, wh, 100, 12)
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pr.name}, fields=["std_trans", "total_sc", "total_ac"])
	check("PR routes as Rec (SC 1000 / AC 1200)",
		ive and ive[0].std_trans == "Rec" and flt(ive[0].total_sc) == 1000
		and flt(ive[0].total_ac) == 1200, str(ive))
	gl = frappe.get_all("GL Entry", filters={"voucher_no": pr.name, "is_cancelled": 0},
		fields=["account", "debit", "credit", "valuation_event_id"])
	dr = flt(sum(g.debit for g in gl), 2)
	check("PR GL balanced with PPV leg (dr 1200, all tagged)",
		dr == flt(sum(g.credit for g in gl), 2) == 1200
		and all(g.valuation_event_id for g in gl), f"dr {dr}")
	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": pr.name},
		fields=["posted_via_valuation_kernel", "valuation_rate", "stock_value_difference"])
	check("PR SLE at standard (rate 10, svd 1000)",
		sle and sle[0].posted_via_valuation_kernel and flt(sle[0].valuation_rate) == 10
		and flt(sle[0].stock_value_difference) == 1000, str(sle))

	# ---- DN 30 -> Iss at SC (no variance)
	dn = make_dn(ITEM, wh, 30)
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": dn.name}, fields=["std_trans", "total_sc", "total_ac"])
	check("DN routes as Iss at SC (-300, no variance)",
		ive and ive[0].std_trans == "Iss" and flt(ive[0].total_sc) == -300
		and flt(ive[0].total_ac) == 0, str(ive))

	# ---- negative stock blocked
	try:
		make_dn(ITEM, wh, 500)
		check("STD negative stock blocked", False, "submitted")
	except frappe.ValidationError:
		check("STD negative stock blocked", True)

	# ---- Stock Count shortage -> SC- at standard
	sc_doc = frappe.get_doc({
		"doctype": "Stock Count", "company": company, "posting_date": nowdate(),
		"items": [{"item_code": ITEM, "warehouse": wh, "counted_qty": 65}],
	})
	sc_doc.insert(ignore_permissions=True)
	sc_doc.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": sc_doc.name}, fields=["std_trans", "total_sc"])
	check("Count shortage routes as SC- (-50)",
		ive and ive[0].std_trans == "SC-" and flt(ive[0].total_sc) == -50, str(ive))

	# ---- PI price difference -> PPV pool (LC-shaped)
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": company, "supplier": "_SMK Supplier",
		"posting_date": nowdate(),
		"items": [{"item_code": ITEM, "qty": 100, "rate": 13, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}],
	})
	pi.insert(ignore_permissions=True)
	pi.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pi.name, "std_trans": "LC"}, fields=["total_ac"])
	check("PI diff 100 lands in PPV pool", ive and flt(ive[0].total_ac) == 100, str(ive))

	# ---- MR21 blocked for STD; transfers blocked
	try:
		rv = frappe.get_doc({"doctype": "Stock Revaluation", "company": company,
			"posting_date": nowdate(),
			"items": [{"item_code": ITEM, "warehouse": wh, "new_valuation_rate": 15}]})
		rv.insert(ignore_permissions=True)
		check("MR21 blocked for STD", False, "inserted")
	except frappe.ValidationError:
		check("MR21 blocked for STD", True)

	# ---- Settlement Run: pool 200 (PPV) + 100 (PI diff) = 300; end 65 of 100
	run_doc = frappe.get_doc({
		"doctype": "Inventory Period Settlement Run", "company": company,
		"period_year": today.year, "period_month": today.month,
		"run_type": "INITIAL_CLOSE",
	})
	run_doc.insert(ignore_permissions=True)
	run_doc.submit()
	run_doc.reload()
	sett = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": ITEM, "cancelled": 0}, fields=["*"])
	expected_es = flt(300 * 65 / 100, 2)
	expected_out = flt(300 * 35 / 100, 2)
	check("Settlement Run settles the scope",
		run_doc.status == "Completed" and run_doc.scopes_settled >= 1, run_doc.status)
	check(f"Settlement 300-pool split {expected_es}/{expected_out}",
		sett and flt(sett[0].es_var, 2) == expected_es and flt(sett[0].out_var, 2) == expected_out,
		f"{sett and sett[0].es_var}/{sett and sett[0].out_var}")

	# ---- posting into the settled period now blocked
	try:
		make_pr(ITEM, wh, 5, 12, posting_date=str(get_first_day(nowdate())))
		check("settled period blocks vouchers", False, "submitted")
	except frappe.ValidationError:
		check("settled period blocks vouchers", True)

	run_reversals(company, wh)
	run_sce_isvc(company)
	run_boundary_scv(company, wh)
	run_year_end(company, wh)

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("STD voucher failures: " + "; ".join(x[0] for x in failed))


def run_reversals(company, wh):
	"""Phase-4: exact reversal with reference - open period + settled period."""
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	item = "_STDV-CXL"
	if not frappe.db.exists("Item", item):
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)
	today = getdate(nowdate())
	scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": item, "valid_from_year": today.year, "valid_from_month": today.month,
		"standard_cost": 10, "source_type": "MANUAL_OVERRIDE"})
	scv.insert(ignore_permissions=True)
	scv.release()

	# --- open-period reversal via Create Cancellation (PR at AC 12, PPV 200)
	pr = make_pr(item, wh, 100, 12)
	cxl = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr.name))
	cxl.submit()
	mirror = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": cxl.name}, fields=["std_trans", "total_sc", "total_ac", "reversal_of"])
	check("open-period reversal mirrors original (SC -1000 / AC -1200, linked)",
		mirror and mirror[0].std_trans == "Rec" and flt(mirror[0].total_sc) == -1000
		and flt(mirror[0].total_ac) == -1200 and mirror[0].reversal_of, str(mirror))
	gl = frappe.get_all("GL Entry", filters={"voucher_no": cxl.name, "is_cancelled": 0},
		fields=["debit", "credit"])
	check("reversal GL balanced 1200/1200",
		flt(sum(g.debit for g in gl), 2) == flt(sum(g.credit for g in gl), 2) == 1200,
		str(gl))
	try:
		c2 = make_cancellation("Purchase Receipt", pr.name)
		frappe.get_doc("Purchase Receipt", c2).submit()
		check("STD double reversal blocked", False, "submitted")
	except frappe.ValidationError:
		check("STD double reversal blocked", True)

	# --- settled-period issue reversal (client worked example: +8 / -8)
	item2 = "_STDV-DELTA"
	if not frappe.db.exists("Item", item2):
		frappe.get_doc({"doctype": "Item", "item_code": item2, "item_name": item2,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)
	eng = StdEngine(company, item2)
	src = ("Item Standard Cost Version", scv.name)
	# May: input 100 with var 80 (ac 10.8), issues 60 -> end 40; settle 32/48
	eng.post(trans="Rec", qty=100, sc=10, ac=10.8, posting_date="2026-05-03", source=src)
	iss = eng.post(trans="Iss", qty=10, sc=10, posting_date="2026-05-10", source=src)
	eng.post(trans="Iss", qty=50, sc=10, posting_date="2026-05-15", source=src)
	sett = eng.close_period(year=2026, month=5, sc=10, source=src, entry_date="2026-06-01")
	check("worked-example settle 32/48",
		flt(sett.es_var, 2) == 32.00 and flt(sett.out_var, 2) == 48.00,
		f"{sett.es_var}/{sett.out_var}")

	eng.reverse_event(iss.name, source=src, posting_date="2026-06-05")
	delta = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": item2, "std_trans": "Sett - Delta"}, fields=["name", "total_sc"])
	check("post-close delta event +8", delta and flt(delta[0].total_sc, 2) == 8.00, str(delta))
	dgl = frappe.get_all("GL Entry",
		filters={"valuation_event_id": delta[0].name if delta else "x", "is_cancelled": 0},
		fields=["account", "debit", "credit"])
	a = eng.accounts()
	inv_leg = next((g for g in dgl if g.account == a.stock), None)
	adj_leg = next((g for g in dgl if g.account == a.cogs_adj), None)
	check("delta GL: Dr Inventory 8 / Cr COGS Adjustment 8",
		inv_leg and flt(inv_leg.debit, 2) == 8.00 and adj_leg and flt(adj_leg.credit, 2) == 8.00,
		str(dgl))
	# variance of the reversed issue is zero, so the current pool is untouched
	check("current pool untouched by issue reversal",
		flt(eng.own_ppv(2026, 6), 2) == 0, str(eng.own_ppv(2026, 6)))


def run_sce_isvc(company):
	"""SCE roll-up releases a cost version; ISVC governs the view flip."""
	today = getdate(nowdate())
	comp = "_STDV-COMP"
	fg = "_STDV-FG"
	for code, method in ((comp, "Periodic Standard Cost"), (fg, "Periodic Standard Cost")):
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
				"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
				"is_stock_item": 1, "valuation_method": method,
				"settlement_view": "MTD"}).insert(ignore_permissions=True)
	scv_c = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": comp, "valid_from_year": today.year, "valid_from_month": today.month,
		"standard_cost": 4, "source_type": "MANUAL_OVERRIDE"})
	scv_c.insert(ignore_permissions=True)
	scv_c.release()

	sce = frappe.get_doc({"doctype": "Standard Cost Estimate", "company": company,
		"item_code": fg, "valid_from_year": today.year, "valid_from_month": today.month,
		"overhead_percent": 10,
		"components": [{"item_code": comp, "qty": 5, "rate_source": "LEAF_STD"}]})
	sce.insert(ignore_permissions=True)
	sce.calculate()
	check("SCE roll-up 5x4 +10% = 22", flt(sce.standard_cost, 2) == 22.00, sce.standard_cost)
	sce.mark()
	scv_name = sce.release()
	scv = frappe.get_doc("Item Standard Cost Version", scv_name)
	check("SCE release creates RELEASED SCV (source SCE)",
		scv.status == "RELEASED" and scv.source_type == "SCE"
		and flt(scv.standard_cost) == 22, scv.status)

	# ---- ISVC: SoD + unsettled-period gate + flip
	isvc = frappe.get_doc({"doctype": "Item Settlement View Change", "company": company,
		"item_code": fg, "to_view": "YTD", "reason": "slow mover"})
	isvc.insert(ignore_permissions=True)
	check("ISVC captures from-view MTD", isvc.from_view == "MTD", isvc.from_view)
	try:
		isvc.approve()
		check("ISVC self-approval blocked", False, "approved")
	except frappe.ValidationError:
		check("ISVC self-approval blocked", True)
	approver = frappe.get_all("User", filters={"name": ("not in", [frappe.session.user, "Guest"])}, limit=1, pluck="name")[0]
	isvc.db_set({"status": "Approved", "approved_by": approver})
	isvc.reload()
	isvc.submit()
	check("ISVC flips view to YTD",
		frappe.db.get_value("Item", fg, "settlement_view") == "YTD"
		and isvc.status == "Posted", isvc.status)


def run_boundary_scv(company, wh):
	"""M11: a future-effective release posts NO triplet now; the boundary
	materializer posts it once the period arrives (simulated by aging the
	valid-from back to the current month)."""
	from dateutil.relativedelta import relativedelta

	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		materialize_pending_revaluations,
	)

	item = "_STDV-BOUND"
	if not frappe.db.exists("Item", item):
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)

	today = getdate(nowdate())
	scv1 = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": item, "valid_from_year": today.year, "valid_from_month": today.month,
		"standard_cost": 10, "source_type": "MANUAL_OVERRIDE"})
	scv1.insert(ignore_permissions=True)
	scv1.release()
	make_pr(item, wh, 50, 10)

	nxt = today + relativedelta(months=1)
	scv2 = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": item, "valid_from_year": nxt.year, "valid_from_month": nxt.month,
		"standard_cost": 12, "source_type": "MANUAL_OVERRIDE"})
	scv2.insert(ignore_permissions=True)
	scv2.release()

	revs = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": item, "std_trans": ("in", ("Rev Beg", "REV In", "REV out"))})
	check("future-effective release posts no triplet at release",
		not revs and not frappe.db.get_value(
			"Item Standard Cost Version", scv2.name, "revaluation_posted"), str(revs))

	sc = frappe.db.get_value  # active SC today must still be v1
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost
	active = get_active_standard_cost(company, item, wh, today)
	check("pending future version does not price today's postings",
		active and flt(active.standard_cost) == 10, str(active and active.standard_cost))

	# boundary arrives: age the version back to the current month, then materialize
	frappe.db.set_value("Item Standard Cost Version", scv2.name,
		{"valid_from_year": today.year, "valid_from_month": today.month},
		update_modified=False)
	materialize_pending_revaluations()

	revs = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": item, "std_trans": ("in", ("Rev Beg", "REV In", "REV out"))},
		fields=["std_trans", "total_sc"])
	# receipt landed in the simulated boundary month itself, so the granular
	# triplet classifies it as REV In; net reval value must be 50 x 2 = 100
	total = flt(sum(r.total_sc for r in revs), 2)
	check("materializer posts boundary reval (net 50x2=100)",
		revs and total == 100
		and frappe.db.get_value("Item Standard Cost Version", scv2.name, "revaluation_posted"),
		str(revs))

	# frozen-target guard: a version aimed at a SETTLED_FROZEN period is refused
	frozen = frappe.get_all("Inventory Period",
		filters={"company": company, "status": "SETTLED_FROZEN"}, limit=1,
		fields=["period_year", "period_month"])
	if frozen:
		try:
			frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
				"item_code": item, "valid_from_year": frozen[0].period_year,
				"valid_from_month": frozen[0].period_month,
				"standard_cost": 15, "source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
			check("frozen-target SCV refused", False, "inserted")
		except frappe.ValidationError:
			check("frozen-target SCV refused", True)


def _make_std_item(item, company, view="MTD"):
	if not frappe.db.exists("Item", item):
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": view}).insert(ignore_permissions=True)


def run_year_end(company, wh):
	"""M12: Beg producer (SR opening), MTD opening in the settlement base,
	FY gate, and STD Year End Close force-settle + identity verification."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine, get_active_standard_cost

	today = getdate(nowdate())

	# ---- Beg producer: SR opening for a scope with no history
	item = "_STDV-YE"
	_make_std_item(item, company)
	scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": item, "valid_from_year": today.year, "valid_from_month": today.month,
		"standard_cost": 12, "source_type": "MANUAL_OVERRIDE"})
	scv.insert(ignore_permissions=True)
	scv.release()

	tsa = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"account_type": "Temporary"}, limit=1, pluck="name")
	opening_acct = tsa[0] if tsa else frappe.get_all("Account",
		filters={"company": company, "is_group": 0, "root_type": "Liability"}, limit=1, pluck="name")[0]
	sr = frappe.get_doc({
		"doctype": "Stock Reconciliation", "company": company, "purpose": "Opening Stock",
		"posting_date": nowdate(), "set_posting_time": 1, "expense_account": opening_acct,
		"items": [{"item_code": item, "warehouse": wh, "qty": 100, "valuation_rate": 15}],
	})
	sr.insert(ignore_permissions=True)
	sr.submit()

	beg = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": item, "std_trans": "Beg"},
		fields=["total_sc", "total_ac", "qty_adj"])
	check("SR opening posts Beg (100 qty, SC 1200 / AC 1500)",
		len(beg) == 1 and flt(beg[0].qty_adj) == 100 and flt(beg[0].total_sc) == 1200
		and flt(beg[0].total_ac) == 1500, str(beg))
	gl = frappe.get_all("GL Entry", filters={"voucher_no": sr.name, "is_cancelled": 0},
		fields=["account", "debit", "credit"])
	fy_acct = frappe.db.get_value("Periodic Standard Cost Settings", {"company": company},
		"fy_carry_forward_account")
	carry = [g for g in gl if g.account == fy_acct]
	check("Beg GL: balanced, Cr FY Carry Forward 1500",
		flt(sum(g.debit for g in gl), 2) == flt(sum(g.credit for g in gl), 2) == 1500
		and carry and flt(carry[0].credit) == 1500, str(gl))

	try:
		sr2 = frappe.get_doc({
			"doctype": "Stock Reconciliation", "company": company, "purpose": "Stock Reconciliation",
			"posting_date": nowdate(), "set_posting_time": 1, "expense_account": opening_acct,
			"items": [{"item_code": item, "warehouse": wh, "qty": 90, "valuation_rate": 15}],
		})
		sr2.insert(ignore_permissions=True)
		sr2.submit()
		check("second SR blocked (opening only)", False, "submitted")
	except frappe.ValidationError:
		check("second SR blocked (opening only)", True)

	# ---- MTD settlement base includes the opening quantity
	make_pr(item, wh, 50, 14)          # Rec: PPV 100 -> pool 400
	make_dn(item, wh, 30)              # Iss at SC -> end 120
	engine = StdEngine(company, item, wh)
	sett = engine.close_period(year=today.year, month=today.month, sc=12,
		source=("Stock Reconciliation", sr.name))
	check("go-live month base includes Beg (es 320 / out 80)",
		flt(sett.es_var, 2) == 320 and flt(sett.out_var, 2) == 80,
		f"{sett.es_var}/{sett.out_var}")

	# ---- FY gate + Year End Close
	item2 = "_STDV-YE2"
	_make_std_item(item2, company)
	scv2 = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": item2, "valid_from_year": 2025, "valid_from_month": 12,
		"standard_cost": 12, "source_type": "MANUAL_OVERRIDE"})
	scv2.insert(ignore_permissions=True)
	scv2.release()
	e2 = StdEngine(company, item2, wh)
	e2.post(trans="Rec", posting_date="2025-12-10", qty=100, sc=12, ac=15,
		source=("Stock Reconciliation", sr.name))

	try:
		e2.close_period(year=today.year, month=today.month, sc=12,
			source=("Stock Reconciliation", sr.name))
		check("FY gate blocks new-year settlement", False, "settled")
	except frappe.ValidationError as e:
		check("FY gate blocks new-year settlement", "Year End" in str(e), str(e)[:100])

	try:
		frappe.get_doc({"doctype": "STD Year End Close", "company": company,
			"fiscal_year": today.year}).insert(ignore_permissions=True)
		check("YEC refuses unfinished fiscal year", False, "inserted")
	except frappe.ValidationError:
		check("YEC refuses unfinished fiscal year", True)

	yec = frappe.get_doc({"doctype": "STD Year End Close", "company": company,
		"fiscal_year": 2025})
	yec.insert(ignore_permissions=True)
	yec.submit()
	check("YEC completes: Dec 2025 force-settled + verified",
		yec.status == "Completed" and yec.scopes_settled >= 1
		and yec.scopes_verified == yec.scopes_total,
		f"{yec.status} {yec.scopes_settled}/{yec.scopes_total}")

	dec = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": item2, "period_year": 2025, "period_month": 12, "cancelled": 0},
		fields=["es_var", "out_var", "variance"])
	check("Dec 2025 settle: all-ES 300 (end >= base)",
		dec and flt(dec[0].es_var, 2) == 300 and flt(dec[0].out_var, 2) == 0, str(dec))
	srv = frappe.get_all("Inventory Valuation Event",
		filters={"item_code": item2, "std_trans": "Sett - Rev"},
		fields=["posting_date", "total_sc"])
	check("cross-FY Sett-Rev lands Jan 1 (inventory-share -300)",
		srv and str(srv[0].posting_date) == "2026-01-01" and flt(srv[0].total_sc, 2) == -300,
		str(srv))

	# MTD carry is adjacent-month (workbook v2.05): the Dec carry sits in
	# January's pool and rolls forward through SEQUENTIAL monthly settlements
	sett2 = None
	for m in range(1, today.month + 1):
		sett2 = e2.close_period(year=today.year, month=m, sc=12,
			source=("Stock Reconciliation", sr.name))
	check("new-year sequential settles roll the carry (last var 300)",
		flt(sett2.variance, 2) == 300, f"{sett2.variance}")
