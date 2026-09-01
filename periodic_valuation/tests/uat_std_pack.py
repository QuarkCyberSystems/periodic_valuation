# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""STD UAT pack - the design-document acceptance scenarios entered the way a
user enters them, in a dedicated company, so they can be persisted on UAT and
opened one by one.

bench --site <site> execute periodic_valuation.tests.uat_std_pack.run --kwargs "{'commit': True}"

Everything goes through real documents: Purchase Receipt, Delivery Note,
returns, Landed Cost Voucher, Stock Count, Stock Reconciliation (opening),
Item Standard Cost Version release, Item Settlement View Change, Create
Cancellation, and the Inventory Period Settlement Run for both month-ends
(previous month, then current month) plus the settlement's own Reverse
action. Only the two periods a user can post to are used (current OPEN,
previous PREV_OPEN_UNSETTLED), so previous-month documents carry the
(BD) label the kernel gives any cross-month backdate.

Scenarios that need a fiscal-year boundary or an engine-only path (TC12/13,
TC23-25, TC34, TC35, TC39) are covered by verify_std_design_tcs and the
workbook replays and are listed as such in the index.

Company "STD UAT Co" (abbr SUC) is created on first run. Rolled back unless
commit=True; the TC -> document index is printed between INDEX markers.
"""

import frappe
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

COMPANY = "STD UAT Co"
ABBR = "SUC"
SUPPLIER = "STD UAT Supplier"
CUSTOMER = "STD UAT Customer"
APPROVER = "std-uat-approver@quarkcyber.systems"

CHECKS = []
INDEX = []  # (tc, scenario, [(doctype, name)], expected, observed)


def check(tc, label, ok, detail=""):
	CHECKS.append((f"TC{tc:02d} {label}", bool(ok)))
	print(("PASS " if ok else "FAIL ") + f"TC{tc:02d} {label}" + (f" - {detail}" if detail and not ok else ""))


def idx(tc, scenario, docs, expected, observed):
	INDEX.append((tc, scenario, docs, expected, observed))


# ------------------------------------------------------------------ masters
def make_acc(name, root, account_type=None):
	full = f"{name} - {ABBR}"
	if not frappe.db.exists("Account", full):
		parent = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 1,
			"root_type": root}, limit=1, pluck="name")[0]
		doc = {"doctype": "Account", "account_name": name, "company": COMPANY,
			"parent_account": parent, "root_type": root}
		if account_type:
			doc["account_type"] = account_type
		frappe.get_doc(doc).insert(ignore_permissions=True)
	return full


def ensure_company():
	if not frappe.db.exists("Company", COMPANY):
		base = frappe.get_all("Company", filters={"name": ("not like", "%Replay%")},
			limit=1, fields=["default_currency", "country"])[0]
		frappe.get_doc({"doctype": "Company", "company_name": COMPANY, "abbr": ABBR,
			"default_currency": base.default_currency, "country": base.country,
			"create_chart_of_accounts_based_on": "Standard Template",
		}).insert(ignore_permissions=True)
	for fy in frappe.get_all("Fiscal Year", fields=["name", "year_start_date", "year_end_date"]):
		if fy.year_start_date.year <= getdate(nowdate()).year <= fy.year_end_date.year:
			has_rows = frappe.db.exists("Fiscal Year Company", {"parent": fy.name})
			ours = frappe.db.exists("Fiscal Year Company", {"parent": fy.name, "company": COMPANY})
			if has_rows and not ours:
				frappe.get_doc({"doctype": "Fiscal Year Company", "parent": fy.name,
					"parenttype": "Fiscal Year", "parentfield": "companies",
					"company": COMPANY}).insert(ignore_permissions=True)

	if not frappe.db.exists("Periodic Standard Cost Settings", {"company": COMPANY}):
		frappe.get_doc({
			"doctype": "Periodic Standard Cost Settings", "company": COMPANY,
			"ppv_account": make_acc("STD Purchase Price Variance", "Expense"),
			"std_reval_reserve_account": make_acc("STD Revaluation Reserve", "Liability"),
			"cogs_adjustment_account": make_acc("STD COGS Adjustment", "Expense"),
			"customer_cogs_account": make_acc("STD Customer COGS", "Expense"),
			"fy_carry_forward_account": make_acc("STD FY Carry Forward", "Liability"),
			"default_settlement_view": "MTD",
		}).insert(ignore_permissions=True)
	make_acc("STD Freight Charges", "Expense")

	wh = f"Stores - {ABBR}"
	if not frappe.db.exists("Warehouse", wh):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores",
			"company": COMPANY}).insert(ignore_permissions=True)
	wh2 = f"Stores 2 - {ABBR}"
	if not frappe.db.exists("Warehouse", wh2):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores 2",
			"company": COMPANY}).insert(ignore_permissions=True)

	if not frappe.db.exists("Supplier", SUPPLIER):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER,
			"supplier_group": frappe.get_all("Supplier Group", limit=1, pluck="name")[0],
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer", CUSTOMER):
		frappe.get_doc({"doctype": "Customer", "customer_name": CUSTOMER,
			"customer_group": frappe.get_all("Customer Group", limit=1, pluck="name")[0],
			"territory": frappe.get_all("Territory", limit=1, pluck="name")[0],
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("User", APPROVER):
		frappe.get_doc({"doctype": "User", "email": APPROVER, "first_name": "STD UAT Approver",
			"send_welcome_email": 0, "roles": [{"role": "Stock Manager"}],
		}).insert(ignore_permissions=True)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	return wh, wh2


def make_period(year, month, status):
	name = frappe.db.get_value("Inventory Period",
		{"company": COMPANY, "period_year": year, "period_month": month})
	if name:
		frappe.db.set_value("Inventory Period", name, "status", status, update_modified=False)
		return name
	doc = frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
		"start_date": f"{year}-{month:02d}-01", "status": status})
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Inventory Period", doc.name, "status", status, update_modified=False)
	return doc.name


def std_item(code, view="MTD"):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": view}).insert(ignore_permissions=True)
	return code


def scv_release(item, year, month, sc):
	scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": COMPANY,
		"item_code": item, "valid_from_year": year, "valid_from_month": month,
		"standard_cost": sc, "source_type": "MANUAL_OVERRIDE"})
	scv.insert(ignore_permissions=True)
	scv.release()
	return scv


# ----------------------------------------------------------------- vouchers
def make_pr(item, wh, qty, rate, posting_date=None, **extra):
	row = {"item_code": item, "qty": qty, "rate": rate, "warehouse": wh}
	row.update(extra)
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": SUPPLIER,
		"posting_date": posting_date or nowdate(), "set_posting_time": 1, "items": [row]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	return pr


def make_dn(item, wh, qty, posting_date=None):
	dn = frappe.get_doc({"doctype": "Delivery Note", "company": COMPANY, "customer": CUSTOMER,
		"posting_date": posting_date or nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": 25, "warehouse": wh}]})
	dn.insert(ignore_permissions=True)
	dn.submit()
	return dn


def make_count(item, wh, counted):
	doc = frappe.get_doc({"doctype": "Stock Count", "company": COMPANY, "posting_date": nowdate(),
		"items": [{"item_code": item, "warehouse": wh, "counted_qty": counted}]})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def settlement_run(year, month):
	run = frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": COMPANY,
		"period_year": year, "period_month": month, "run_type": "INITIAL_CLOSE"})
	run.insert(ignore_permissions=True)
	run.submit()
	run.reload()
	return run


# ------------------------------------------------------------------ readers
def gl_net(voucher_no=None, event=None):
	filters = {"is_cancelled": 0, "company": COMPANY}
	if voucher_no:
		filters["voucher_no"] = voucher_no
	if event:
		filters["valuation_event_id"] = event
	out = {}
	for g in frappe.get_all("GL Entry", filters=filters, fields=["account", "debit", "credit"]):
		out[g.account] = flt(out.get(g.account, 0) + flt(g.debit) - flt(g.credit), 2)
	return {k: v for k, v in out.items() if flt(v, 2)}


def gl_match(tc, label, actual, expected):
	exp = {k: flt(v, 2) for k, v in expected.items() if flt(v, 2)}
	check(tc, label, actual == exp, f"got {actual} want {exp}")
	return actual == exp


def ives(**filters):
	f = dict(filters)
	f.setdefault("is_cancelled", 0)
	return frappe.get_all("Inventory Valuation Event", filters=f,
		fields=["name", "std_trans", "qty_adj", "total_sc", "total_ac", "posting_date",
			"period_year", "period_month", "reversal_of"], order_by="creation")


def ipb(item, year, month):
	rows = frappe.get_all("Inventory Period Balance", filters={"company": COMPANY,
		"item_code": item, "period_year": year, "period_month": month},
		fields=["closing_qty", "closing_value", "closing_reference_value",
			"settlement_inventory_total", "settlement_consumption_total"])
	return rows[0] if rows else None


def sett(item, year, month):
	rows = frappe.get_all("Inventory Period Settlement", filters={"company": COMPANY,
		"item_code": item, "period_year": year, "period_month": month, "cancelled": 0},
		fields=["name", "es_var", "out_var", "variance", "settlement_view", "sett_event",
			"sett_rev_event", "settlement_run"])
	return rows[0] if rows else None


def acct():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	return StdEngine(COMPANY, "UAT-STD-TC01", f"Stores - {ABBR}").accounts()


def fmt(d):
	return " / ".join(f"{k.replace(' - ' + ABBR, '')} {v:+.2f}" for k, v in sorted(d.items()))


# ===================================================================== main
def run(commit=False):
	today = getdate(nowdate())
	prev = add_days(get_first_day(today), -1)
	cy, cm, py, pm = today.year, today.month, prev.year, prev.month

	wh, wh2 = ensure_company()
	make_period(py, pm, "PREV_OPEN_UNSETTLED")
	make_period(cy, cm, "OPEN")
	for it in [f"UAT-STD-TC{n:02d}" for n in (1, 8, 9, 14, 16, 17, 18, 19, 20, 22, 29, 31, 32, 38, 40, 42)]:
		std_item(it, "YTD" if it.endswith(("20", "22")) else "MTD")
	a = acct()

	# ---- previous-month activity first (TC08/09 opening stock, TC20 July leg)
	previous_month_postings(wh, a, py, pm, cy, cm, today, prev)
	# the boundary cost change and the backdated documents into the previous
	# month must be entered BEFORE that month is closed - a settled month
	# refuses them (TC19 proves the refusal later)
	cost_change_current(wh, a, cy, cm, prev, today)

	# ---- previous month-end: the first Settlement Run a user would make
	run1 = settlement_run(py, pm)
	check(36, f"{py}-{pm:02d} Settlement Run completes", run1.status == "Completed", run1.status)
	assert_prev_month_settlements(a, py, pm, cy, cm, run1)

	# ---- current-month activity
	basic_postings(wh, a, cy, cm)
	settlement_setups(wh, cy, cm)
	migration_and_governance(wh, wh2, a, cy, cm)
	reversal_and_edges(wh, a, cy, cm)

	# ---- current month-end run + settlement assertions
	run2 = settlement_run(cy, cm)
	check(36, f"{cy}-{cm:02d} Settlement Run completes and settles the pack scopes",
		run2.status == "Completed" and run2.scopes_settled >= 12,
		f"{run2.status} {run2.scopes_settled}")
	assert_current_month_settlements(a, cy, cm, run2)

	# ---- TC19/37: reverse a settled month, post the late entry, re-close
	reverse_and_reclose(wh, a, cy, cm)

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	print_index()
	if commit and not failed:
		frappe.db.commit()
		print("COMMITTED")
	else:
		frappe.db.rollback()
		print("ROLLED BACK")


# ---------------------------------------------------------------- sections
def previous_month_postings(wh, a, py, pm, cy, cm, today, prev):
	d10, d12, d15, d20 = (str(prev.replace(day=d)) for d in (10, 12, 15, 20))

	# TC08 opening stock in the previous month at SC 10
	scv_release("UAT-STD-TC08", py, pm, 10)
	pr = make_pr("UAT-STD-TC08", wh, 100, 10, posting_date=d10)
	ev = ives(source_docname=pr.name)
	check(8, "prev-month receipt labels REC (BD) with a zero-value bridge (SC unchanged)",
		{e.std_trans for e in ev} == {"REC (BD)", "REC (BD) - Rev"}
		and sum(flt(e.total_sc) for e in ev if e.std_trans.endswith("Rev")) == 0, str(ev))
	idx(8, "Opening stock 100 @ SC 10 in previous month", [("Purchase Receipt", pr.name)],
		"REC (BD) 1000; bridge 0", fmt(gl_net(voucher_no=pr.name)))

	# TC09 opening 60 in previous month
	scv_release("UAT-STD-TC09", py, pm, 10)
	pr9 = make_pr("UAT-STD-TC09", wh, 60, 10, posting_date=d12)
	idx(9, "Opening stock 60 @ SC 10 in previous month", [("Purchase Receipt", pr9.name)],
		"REC (BD) 600", fmt(gl_net(voucher_no=pr9.name)))

	# TC20 YTD July leg: PR 100 @ 11.50 (PPV 150), DN 40
	scv_release("UAT-STD-TC20", py, pm, 10)
	pr20 = make_pr("UAT-STD-TC20", wh, 100, 11.50, posting_date=d10)
	dn20 = make_dn("UAT-STD-TC20", wh, 40, posting_date=d20)
	gl_match(20, "YTD prev-month receipt GL: Dr Inv 1000 / Dr PPV 150 / Cr GR-IR 1150",
		gl_net(voucher_no=pr20.name), {a.stock: 1000, a.ppv: 150, a.grir: -1150})
	idx(20, "YTD item, previous month: receipt 100 @ 11.50 and issue 40",
		[("Purchase Receipt", pr20.name), ("Delivery Note", dn20.name)],
		"PPV 150 pool; end 60 of 100", fmt(gl_net(voucher_no=pr20.name)))


def assert_prev_month_settlements(a, py, pm, cy, cm, run1):
	s = sett("UAT-STD-TC20", py, pm)
	check(20, "YTD prev-month settle 90/60 (pool 150, 60 of 100 on hand)",
		s and flt(s.es_var, 2) == 90 and flt(s.out_var, 2) == 60, str(s))
	if s:
		gl_match(20, "Sett GL: Dr Inv 90 / Dr COGS Adj 60 / Cr PPV 150",
			gl_net(event=s.sett_event), {a.stock: 90, a.cogs_adj: 60, a.ppv: -150})
		rev = gl_net(event=s.sett_rev_event)
		gl_match(21, "YTD Sett-Rev on day 1 of next month is the full 4-leg mirror",
			rev, {a.stock: -90, a.cogs_adj: -60, a.ppv: 150})
		idx(20, "Previous month-end settlement (YTD item)",
			[("Inventory Period Settlement Run", run1.name), ("Inventory Period Settlement", s.name)],
			"es 90 / out 60", f"es {s.es_var} / out {s.out_var}")
		idx(21, "YTD Sett-Rev day 1 (full mirror re-imports the pool)",
			[("Inventory Valuation Event", s.sett_rev_event)],
			"Cr Inv 90 / Cr COGS Adj 60 / Dr PPV 150", fmt(rev))


def basic_postings(wh, a, cy, cm):
	item = "UAT-STD-TC01"
	scv_release(item, cy, cm, 10)

	pr = make_pr(item, wh, 100, 12)
	ev = ives(source_docname=pr.name)
	check(1, "receipt: IVE Rec 100 / SC 1000 / AC 1200",
		len(ev) == 1 and ev[0].std_trans == "Rec" and flt(ev[0].total_sc) == 1000
		and flt(ev[0].total_ac) == 1200, str(ev))
	g = gl_net(voucher_no=pr.name)
	gl_match(1, "receipt GL: Dr Inv 1000 / Dr PPV 200 / Cr GR-IR 1200", g,
		{a.stock: 1000, a.ppv: 200, a.grir: -1200})
	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": pr.name},
		fields=["actual_qty", "valuation_rate", "stock_value_difference"])
	check(1, "receipt SLE at standard (100 @ 10, value 1000)",
		sle and flt(sle[0].actual_qty) == 100 and flt(sle[0].valuation_rate) == 10
		and flt(sle[0].stock_value_difference) == 1000, str(sle))
	idx(1, "Receipt 100 @ 12 against SC 10", [("Purchase Receipt", pr.name)],
		"Inv 1000 / PPV 200 / GR-IR -1200; SLE 100 @ 10", fmt(g))

	dn = make_dn(item, wh, 30)
	g = gl_net(voucher_no=dn.name)
	gl_match(2, "issue GL: Dr COGS 300 / Cr Inv 300, no variance", g, {a.cogs: 300, a.stock: -300})
	b = ipb(item, cy, cm)
	check(2, "IPB 70 @ 700 after issue", b and flt(b.closing_qty) == 70 and flt(b.closing_value) == 700, str(b))
	idx(2, "Issue 30 at SC", [("Delivery Note", dn.name)], "COGS 300 / Inv -300; IPB 70 @ 700", fmt(g))

	freight = f"STD Freight Charges - {ABBR}"
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": nowdate(), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name, "supplier": SUPPLIER, "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": freight, "description": "Freight", "amount": 50}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	ev = ives(source_docname=lcv.name)
	g = gl_net(event=ev[0].name) if ev else {}
	gl_match(3, "landed cost GL: Dr PPV 50 / Cr Freight 50 - inventory untouched", g,
		{a.ppv: 50, freight: -50})
	b = ipb(item, cy, cm)
	check(3, "IPB unchanged by landed cost (70 @ 700)", b and flt(b.closing_value) == 700, str(b))
	idx(3, "Landed cost 50 on the receipt", [("Landed Cost Voucher", lcv.name)],
		"PPV 50 / Freight -50; no inventory leg; IPB still 700", fmt(g))

	from erpnext.controllers.sales_and_purchase_return import make_return_doc
	ret = make_return_doc("Delivery Note", dn.name)
	ret.items[0].qty = -10
	ret.insert(ignore_permissions=True)
	ret.submit()
	g = gl_net(voucher_no=ret.name)
	gl_match(4, "sales return GL: Dr Inv 100 / Cr Customer COGS 100, no variance", g,
		{a.stock: 100, a.customer_cogs: -100})
	idx(4, "Sales return 10 with reference", [("Delivery Note", ret.name)],
		"SR +100 at SC; IPB 80 @ 800", fmt(g))

	pret = make_return_doc("Purchase Receipt", pr.name)
	pret.items[0].qty = -20
	pret.items[0].rate = 12
	pret.insert(ignore_permissions=True)
	pret.submit()
	g = gl_net(voucher_no=pret.name)
	gl_match(5, "purchase return GL: Cr Inv 200 / Cr PPV 40 / Dr GR-IR 240", g,
		{a.stock: -200, a.ppv: -40, a.grir: 240})
	b = ipb(item, cy, cm)
	check(5, "IPB 60 @ 600 after purchase return", b and flt(b.closing_qty) == 60
		and flt(b.closing_value) == 600, str(b))
	idx(5, "Purchase return 20 with reference", [("Purchase Receipt", pret.name)],
		"PR -200 at original SC; PPV -40 proportional; IPB 60 @ 600", fmt(g))

	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	eng = StdEngine(COMPANY, item, wh)
	pool_before = flt(eng.own_ppv(cy, cm), 2)
	c1 = make_count(item, wh, 70)
	g = gl_net(voucher_no=c1.name)
	gl_match(6, "count gain GL: Dr Inv 100 / Cr Stock Adjustment 100", g, {a.stock: 100, a.stock_adj: -100})
	check(6, "count gain stays out of the settlement pool", flt(eng.own_ppv(cy, cm), 2) == pool_before, "")
	idx(6, "Stock count gain +10 (60 -> 70)", [("Stock Count", c1.name)],
		"SC+ 100; pool unchanged", fmt(g))

	c2 = make_count(item, wh, 60)
	g = gl_net(voucher_no=c2.name)
	gl_match(7, "count loss GL: Dr Stock Adjustment 100 / Cr Inv 100", g, {a.stock_adj: 100, a.stock: -100})
	check(7, "Bin and IPB agree at 60",
		flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "actual_qty")) == 60
		and flt(ipb(item, cy, cm).closing_qty) == 60, "")
	check(7, "period PPV pool = 210 (200 receipt + 50 landed cost - 40 return; counts excluded)",
		flt(eng.own_ppv(cy, cm), 2) == 210, str(eng.own_ppv(cy, cm)))
	idx(7, "Stock count loss -10 (70 -> 60)", [("Stock Count", c2.name)],
		"SC- -100; Bin 60 = IPB 60; pool 210", fmt(g))


def cost_change_current(wh, a, cy, cm, prev, today):
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		materialize_pending_revaluations,
	)
	d15, d20 = str(prev.replace(day=15)), str(prev.replace(day=20))

	# TC08: clean boundary change 10 -> 12 with 100 on hand from last month
	scv8 = scv_release("UAT-STD-TC08", cy, cm, 12)
	materialize_pending_revaluations()
	revs = ives(item_code="UAT-STD-TC08", std_trans=("in", ("Rev Beg", "REV In", "REV out")))
	check(8, "boundary release posts one Rev Beg = 100 x 2 = 200",
		len(revs) == 1 and revs[0].std_trans == "Rev Beg" and flt(revs[0].total_sc) == 200, str(revs))
	g = gl_net(event=revs[0].name) if revs else {}
	gl_match(8, "Rev Beg GL: Dr Inv 200 / Cr Revaluation Reserve 200", g, {a.stock: 200, a.reserve: -200})
	idx(8, "SC change 10 -> 12 at month boundary (100 on hand)",
		[("Item Standard Cost Version", scv8.name)] + [("Inventory Valuation Event", r.name) for r in revs],
		"single Rev Beg 200: Inv +200 / Reserve -200", fmt(g))

	# TC09: change with in-month activity -> granular triplet
	pr9b = make_pr("UAT-STD-TC09", wh, 40, 10, posting_date=str(today))
	dn9 = make_dn("UAT-STD-TC09", wh, 30, posting_date=str(today))
	scv9 = scv_release("UAT-STD-TC09", cy, cm, 12)
	materialize_pending_revaluations()
	revs = {r.std_trans: r for r in ives(item_code="UAT-STD-TC09",
		std_trans=("in", ("Rev Beg", "REV In", "REV out")))}
	check(9, "granular triplet Rev Beg 120 / REV In 80 / REV out -60",
		flt(revs.get("Rev Beg", {}).get("total_sc", 0)) == 120
		and flt(revs.get("REV In", {}).get("total_sc", 0)) == 80
		and flt(revs.get("REV out", {}).get("total_sc", 0)) == -60, str(revs))
	total = flt(sum(flt(r.total_sc) for r in revs.values()), 2)
	check(9, "triplet sums to on-hand x delta (70 x 2 = 140)", total == 140, str(total))
	if "REV out" in revs:
		gl_match(9, "REV out GL: Dr COGS Adj 60 / Cr Inv 60", gl_net(event=revs["REV out"].name),
			{a.cogs_adj: 60, a.stock: -60})
	idx(9, "SC change 10 -> 12 after in-month receipt 40 and issue 30 (60 opening)",
		[("Purchase Receipt", pr9b.name), ("Delivery Note", dn9.name),
			("Item Standard Cost Version", scv9.name)]
		+ [("Inventory Valuation Event", r.name) for r in revs.values()],
		"Rev Beg 120 + REV In 80 + REV out -60 = 140", ", ".join(f"{k} {flt(v.total_sc):+.2f}" for k, v in revs.items()))

	# TC10: backdated receipt into last month at today's SC 12 (original SC 10)
	pr10 = make_pr("UAT-STD-TC08", wh, 20, 12, posting_date=d15)
	pair = ives(source_docname=pr10.name)
	orig = next((r for r in pair if r.std_trans == "REC (BD)"), None)
	comp = next((r for r in pair if r.std_trans == "REC (BD) - Rev"), None)
	check(10, "REC (BD) at original SC 200 (+PPV 40) and bridge 40 in the current month",
		orig and flt(orig.total_sc) == 200 and comp and flt(comp.total_sc) == 40
		and comp.period_month == cm, str(pair))
	if comp:
		gl_match(10, "bridge GL: Dr Inv 40 / Cr Revaluation Reserve 40", gl_net(event=comp.name),
			{a.stock: 40, a.reserve: -40})
	idx(10, "Backdated receipt 20 @ 12 into previous month (SC was 10, now 12)",
		[("Purchase Receipt", pr10.name)],
		"REC (BD) Inv 200 / PPV 40 / GR-IR -240 + bridge Inv 40 / Reserve -40 -> net 240 = 20 x 12",
		fmt(gl_net(voucher_no=pr10.name)))

	# TC11: backdated issue into last month
	dn11 = make_dn("UAT-STD-TC08", wh, 10, posting_date=d20)
	pair = ives(source_docname=dn11.name)
	orig = next((r for r in pair if r.std_trans == "Issue (BD)"), None)
	comp = next((r for r in pair if r.std_trans == "Issue (BD) - Rev"), None)
	check(11, "Issue (BD) -100 at original SC and bridge -20 to COGS Adjustment",
		orig and flt(orig.total_sc) == -100 and comp and flt(comp.total_sc) == -20, str(pair))
	if comp:
		gl_match(11, "bridge GL: Dr COGS Adj 20 / Cr Inv 20", gl_net(event=comp.name),
			{a.cogs_adj: 20, a.stock: -20})
	idx(11, "Backdated issue 10 into previous month (SC was 10, now 12)",
		[("Delivery Note", dn11.name)],
		"Issue (BD) COGS 100 / Inv -100 + bridge COGS Adj 20 / Inv -20 -> net 120 = 10 x 12",
		fmt(gl_net(voucher_no=dn11.name)))


def settlement_setups(wh, cy, cm):
	for item, sc, qty, rate, out in (
			("UAT-STD-TC14", 10, 100, 13.20, 20),   # pool 320, end 80 -> 256 / 64
			("UAT-STD-TC16", 10, 50, 12, 50),       # end 0 -> 0 / 100
			("UAT-STD-TC17", 10, 50, 12, 0),        # no issue -> 100 / 0
			("UAT-STD-TC18", 10, 50, 8, 20),        # pool -100 -> -60 / -40
			("UAT-STD-TC19", 10, 100, 12, 40),      # pool 200 -> 120 / 80, then reversed
			("UAT-STD-TC22", 10, 50, 8, 20),        # YTD, pool -100 -> -60 / -40
			("UAT-STD-TC20", 10, 50, 12.40, 30),    # YTD Aug leg: PPV 120, end 80 cumulative
	):
		if not frappe.db.exists("Item Standard Cost Version",
				{"item_code": item, "valid_from_year": cy, "valid_from_month": cm, "status": "RELEASED"}):
			scv_release(item, cy, cm, sc)
		pr = make_pr(item, wh, qty, rate)
		docs = [("Purchase Receipt", pr.name)]
		if out:
			docs.append(("Delivery Note", make_dn(item, wh, out).name))
		tcno = int(item[-2:])
		idx(tcno, f"{item}: receipt {qty} @ {rate} (SC {sc}), issue {out}", docs,
			f"PPV {flt(qty * (rate - sc), 2):+.2f}; end {qty - out}", "posted")


def assert_current_month_settlements(a, cy, cm, run2):
	cases = {
		14: (320, 256, 64, "Dr Inv 256 / Dr COGS Adj 64 / Cr PPV 320"),
		16: (100, 0, 100, "Dr COGS Adj 100 / Cr PPV 100 (no inventory leg)"),
		17: (100, 100, 0, "Dr Inv 100 / Cr PPV 100 (no COGS leg)"),
		18: (-100, -60, -40, "Cr Inv 60 / Cr COGS Adj 40 / Dr PPV 100"),
		19: (200, 120, 80, "Dr Inv 120 / Dr COGS Adj 80 / Cr PPV 200"),
		22: (-100, -60, -40, "YTD: Cr Inv 60 / Cr COGS Adj 40 / Dr PPV 100"),
	}
	for n, (pool, es, out, legs) in cases.items():
		item = f"UAT-STD-TC{n:02d}"
		s = sett(item, cy, cm)
		check(n, f"{item} settles {es}/{out} on pool {pool}",
			s and flt(s.variance, 2) == pool and flt(s.es_var, 2) == es and flt(s.out_var, 2) == out,
			str(s))
		if s:
			g = gl_net(event=s.sett_event)
			gl_match(n, f"{item} Sett GL: {legs}", g, {a.stock: es, a.cogs_adj: out, a.ppv: -pool})
			idx(n, f"Month-end settlement {item}", [("Inventory Period Settlement", s.name),
				("Inventory Valuation Event", s.sett_event)], legs, fmt(g))
	# TC15: MTD Sett-Rev carries the inventory share only
	s = sett("UAT-STD-TC14", cy, cm)
	if s:
		rev = gl_net(event=s.sett_rev_event)
		gl_match(15, "MTD Sett-Rev (day 1 next month): Cr Inv 256 / Dr PPV 256, COGS untouched",
			rev, {a.stock: -256, a.ppv: 256})
		idx(15, "MTD Sett-Rev day 1 (inventory share only)",
			[("Inventory Valuation Event", s.sett_rev_event)], "Cr Inv 256 / Dr PPV 256", fmt(rev))
	# TC20 YTD cumulative: Jul 150 + Aug 120 = 270 on input 150, end 80, out 70
	s20 = sett("UAT-STD-TC20", cy, cm)
	check(20, "YTD current month settles the CUMULATIVE pool 270 -> 144/126",
		s20 and flt(s20.variance, 2) == 270 and flt(s20.es_var, 2) == 144
		and flt(s20.out_var, 2) == 126, str(s20))
	if s20:
		idx(20, "YTD current month-end (cumulative across both months)",
			[("Inventory Period Settlement", s20.name)], "pool 270 -> es 144 / out 126",
			f"pool {s20.variance} -> es {s20.es_var} / out {s20.out_var}")
	# TC33 identity on TC14
	b = ipb("UAT-STD-TC14", cy, cm)
	check(33, "IPB identity: closing_value 1056 = 80 x SC 10 + capitalized 256",
		b and flt(b.closing_value, 2) == 1056 and flt(b.closing_reference_value, 2) == 800
		and flt(b.settlement_inventory_total, 2) == 256, str(b))
	idx(33, "Period balance after close (UAT-STD-TC14)", [("Inventory Period Balance", "list")],
		"closing 80; value 1056 = 800 reference + 256 capitalized", str(b))
	# TC01 item settles its 210 pool: end 60 of input 80 -> 157.50 / 52.50
	s1 = sett("UAT-STD-TC01", cy, cm)
	check(36, "UAT-STD-TC01 pool 210 settles 157.50/52.50 (input 80, end 60)",
		s1 and flt(s1.es_var, 2) == 157.5 and flt(s1.out_var, 2) == 52.5, str(s1))
	idx(36, "Multi-item Settlement Run (current month)", [("Inventory Period Settlement Run", run2.name)],
		"one run, every eligible scope settled, failures isolated", f"{run2.status}, {run2.scopes_settled} scopes")


def migration_and_governance(wh, wh2, a, cy, cm):
	# TC29 transfer refused
	scv_release("UAT-STD-TC29", cy, cm, 10)
	pr = make_pr("UAT-STD-TC29", wh, 20, 10)
	msg = ""
	try:
		se = frappe.get_doc({"doctype": "Stock Entry", "company": COMPANY,
			"stock_entry_type": "Material Transfer", "posting_date": nowdate(),
			"items": [{"item_code": "UAT-STD-TC29", "qty": 5, "s_warehouse": wh, "t_warehouse": wh2,
				"transfer_qty": 5, "uom": frappe.db.get_value("Item", "UAT-STD-TC29", "stock_uom"),
				"conversion_factor": 1}]})
		se.insert(ignore_permissions=True)
		se.submit()
		check(29, "warehouse transfer refused for STD items", False, "submitted")
	except frappe.ValidationError as e:
		msg = frappe.utils.strip_html(str(e))
		check(29, "warehouse transfer refused for STD items", "not supported" in msg.lower(), msg[:120])
	idx(29, "Material Transfer attempt (refused by design in v1)", [("Purchase Receipt", pr.name)],
		"clear refusal message", msg[:100])

	# TC31 opening via Stock Reconciliation -> Beg
	scv_release("UAT-STD-TC31", cy, cm, 12)
	tsa = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0,
		"account_type": "Temporary"}, limit=1, pluck="name")
	opening_acct = tsa[0] if tsa else f"STD FY Carry Forward - {ABBR}"
	sr = frappe.get_doc({"doctype": "Stock Reconciliation", "company": COMPANY,
		"purpose": "Opening Stock", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": opening_acct,
		"items": [{"item_code": "UAT-STD-TC31", "warehouse": wh, "qty": 100, "valuation_rate": 15}]})
	sr.insert(ignore_permissions=True)
	sr.submit()
	g = gl_net(voucher_no=sr.name)
	gl_match(31, "opening Beg GL: Dr Inv 1200 / Dr PPV 300 / Cr FY Carry-Forward 1500", g,
		{a.stock: 1200, a.ppv: 300, a.fy_carry: -1500})
	idx(31, "Go-live opening 100 @ legacy 15 against SC 12", [("Stock Reconciliation", sr.name)],
		"Beg: Inv 1200 / PPV 300 / FY Carry -1500", fmt(g))

	# TC32 ISVC MTD -> YTD with pending variance
	scv_release("UAT-STD-TC32", cy, cm, 10)
	pr32 = make_pr("UAT-STD-TC32", wh, 10, 11)
	isvc = frappe.get_doc({"doctype": "Item Settlement View Change", "company": COMPANY,
		"item_code": "UAT-STD-TC32", "to_view": "YTD", "reason": "UAT pack TC32"})
	isvc.insert(ignore_permissions=True)
	try:
		isvc.approve()
		check(32, "self-approval blocked (segregation of duties)", False, "approved")
	except frappe.ValidationError:
		check(32, "self-approval blocked (segregation of duties)", True)
	isvc.db_set({"status": "Approved", "approved_by": APPROVER})
	isvc.reload()
	isvc.submit()
	s = sett("UAT-STD-TC32", cy, cm)
	check(32, "flip posted: pending variance settled under MTD first, item now YTD",
		frappe.db.get_value("Item", "UAT-STD-TC32", "settlement_view") == "YTD"
		and s and s.settlement_view == "MTD" and flt(s.es_var, 2) == 10, str(s))
	idx(32, "Settlement view change MTD -> YTD", [("Purchase Receipt", pr32.name),
		("Item Settlement View Change", isvc.name)] + ([("Inventory Period Settlement", s.name)] if s else []),
		"SoD enforced; forced MTD settle es 10; Item.settlement_view = YTD",
		f"item {frappe.db.get_value('Item', 'UAT-STD-TC32', 'settlement_view')}, settle {s and s.es_var}")


def reversal_and_edges(wh, a, cy, cm):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	# TC38 exact reversal with reference via Create Cancellation
	scv_release("UAT-STD-TC38", cy, cm, 10)
	pr = make_pr("UAT-STD-TC38", wh, 100, 12)
	cxl = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr.name))
	cxl.submit()
	m = ives(source_docname=cxl.name)
	check(38, "cancellation mirrors the original exactly (-1000 SC / -1200 AC, linked)",
		len(m) == 1 and flt(m[0].total_sc) == -1000 and flt(m[0].total_ac) == -1200 and m[0].reversal_of,
		str(m))
	g = gl_net(voucher_no=cxl.name)
	gl_match(38, "cancellation GL: Cr Inv 1000 / Cr PPV 200 / Dr GR-IR 1200", g,
		{a.stock: -1000, a.ppv: -200, a.grir: 1200})
	try:
		frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr.name)).submit()
		check(38, "second cancellation blocked", False, "submitted")
	except frappe.ValidationError:
		check(38, "second cancellation blocked", True)
	idx(38, "Create Cancellation of a receipt (exact reversal with reference)",
		[("Purchase Receipt", pr.name), ("Purchase Receipt", cxl.name)],
		"mirror -1000/-1200; Cr Inv 1000 / Cr PPV 200 / Dr GR-IR 1200; 2nd blocked", fmt(g))

	# TC40 negative stock blocked
	scv_release("UAT-STD-TC40", cy, cm, 10)
	pr40 = make_pr("UAT-STD-TC40", wh, 10, 10)
	msg = ""
	try:
		make_dn("UAT-STD-TC40", wh, 50)
		check(40, "issue past zero blocked (block_negative_stock_std ON)", False, "posted")
	except frappe.ValidationError as e:
		msg = frappe.utils.strip_html(str(e))
		check(40, "issue past zero blocked (block_negative_stock_std ON)", True)
	idx(40, "Issue 50 against 10 on hand", [("Purchase Receipt", pr40.name)], "blocked with message", msg[:100])

	# TC42 alternate UOM
	box = "Box" if frappe.db.exists("UOM", "Box") else "UAT Box"
	if not frappe.db.exists("UOM", box):
		frappe.get_doc({"doctype": "UOM", "uom_name": box}).insert(ignore_permissions=True)
	it = frappe.get_doc("Item", "UAT-STD-TC42")
	if not any(u.uom == box for u in it.uoms):
		it.append("uoms", {"uom": box, "conversion_factor": 10})
		it.save(ignore_permissions=True)
	scv_release("UAT-STD-TC42", cy, cm, 10)
	pr42 = make_pr("UAT-STD-TC42", wh, 5, 120, uom=box, conversion_factor=10)
	ev = ives(source_docname=pr42.name)
	check(42, "5 Box x 10 books 50 base units: SC 500 / AC 600 / PPV 100",
		ev and flt(ev[0].qty_adj) == 50 and flt(ev[0].total_sc) == 500 and flt(ev[0].total_ac) == 600, str(ev))
	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": pr42.name},
		fields=["actual_qty", "valuation_rate"])
	check(42, "SLE in stock UOM (50 @ 10)", sle and flt(sle[0].actual_qty) == 50
		and flt(sle[0].valuation_rate) == 10, str(sle))
	idx(42, f"Receipt 5 {box} (x10) @ 120 per {box}", [("Purchase Receipt", pr42.name)],
		"50 units; Inv 500 / PPV 100 / GR-IR -600", fmt(gl_net(voucher_no=pr42.name)))

	# TC43 idempotency
	from periodic_valuation.periodic_standard_cost.kernel import post_via_std_kernel
	before = frappe.db.count("Inventory Valuation Event", {"source_docname": pr42.name, "is_cancelled": 0})
	post_via_std_kernel(pr42, [])
	after = frappe.db.count("Inventory Valuation Event", {"source_docname": pr42.name, "is_cancelled": 0})
	check(43, "re-posting the same receipt is a no-op", before == after == 1, f"{before}->{after}")
	idx(43, "Duplicate posting attempt on the TC42 receipt", [("Purchase Receipt", pr42.name)],
		"still exactly one valuation event", f"{after} event(s)")


def reverse_and_reclose(wh, a, cy, cm):
	item = "UAT-STD-TC19"
	s = sett(item, cy, cm)
	if not s:
		check(19, "settlement to reverse exists", False, "missing")
		return
	# the late entry is refused while the month is settled
	msg = ""
	try:
		make_pr(item, wh, 10, 15)
		check(19, "late receipt refused while the month is settled", False, "posted")
	except frappe.ValidationError as e:
		msg = frappe.utils.strip_html(str(e))
		check(19, "late receipt refused while the month is settled", True)
	# user action: Reverse on the settlement
	frappe.get_doc("Inventory Period Settlement", s.name).reverse()
	check(19, "settlement marked cancelled after Reverse",
		frappe.db.get_value("Inventory Period Settlement", s.name, "cancelled") == 1, "")
	undo = {}
	for ev in frappe.get_all("Inventory Valuation Event",
			filters={"item_code": item, "settlement_ref": s.name, "is_cancelled": 0}, fields=["name"]):
		for acct_, amt in gl_net(event=ev.name).items():
			undo[acct_] = flt(undo.get(acct_, 0) + amt, 2)
	undo = {k: v for k, v in undo.items() if flt(v, 2)}
	check(37, "reversed MTD settlement nets to zero on every account (4 events)", not undo, str(undo))
	late = make_pr(item, wh, 10, 15)  # PPV 50 -> pool 250, input 110, end 70
	run3 = settlement_run(cy, cm)
	s2 = sett(item, cy, cm)
	es, out = flt(250 * 70 / 110, 2), flt(250 * 40 / 110, 2)
	check(19, f"re-close on the corrected pool 250 -> {es}/{out}",
		s2 and flt(s2.es_var, 2) == es and flt(s2.out_var, 2) == out and s2.settlement_run == run3.name,
		str(s2))
	idx(19, "Late receipt into a settled month: refused -> Reverse settlement -> post -> re-close",
		[("Inventory Period Settlement", s.name), ("Purchase Receipt", late.name),
			("Inventory Period Settlement Run", run3.name)] + ([("Inventory Period Settlement", s2.name)] if s2 else []),
		f"blocked first; after reverse and repost {es}/{out}", f"{msg[:60]}... -> {s2 and s2.es_var}/{s2 and s2.out_var}")
	idx(37, "Reverse of Sett + Reverse of Sett-Rev (UAT-STD-TC19)",
		[("Inventory Period Settlement", s.name)], "all four events net to zero per account",
		"residue " + (fmt(undo) if undo else "none"))


def print_index():
	print("\n=== INDEX ===")
	for tc, scenario, docs, expected, observed in sorted(INDEX, key=lambda r: (r[0], r[1])):
		d = "; ".join(f"{dt}:{nm}" for dt, nm in docs)
		print(f"TC{tc:02d}|{scenario}|{d}|{expected}|{observed}")
	print("=== END INDEX ===")
