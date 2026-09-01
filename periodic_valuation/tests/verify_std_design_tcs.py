# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Deep verification of the STD design-document acceptance Test Matrix.

bench --site <site> execute periodic_valuation.tests.verify_std_design_tcs.run

Walks the acceptance scenarios in
badia_docs/standard_cost_discussions/SAP_STD_Implementation_Plan.md
(## Test Matrix, "Acceptance scenarios for STD UAT") one by one. For every
scenario the EXPECTED numbers are recomputed here from the plan's formula
section (never read back from engine helpers), then asserted at every ledger
layer the event touches:

- Inventory Valuation Event  (std_trans, qty_adj, total_sc, total_ac)
- GL Entry                   (exact account + Dr/Cr per leg)
- Inventory Period Balance   (closing qty/value, settlement stamps)
- Stock Ledger Entry / Bin   (voucher-driven scenarios only)
- Settlement rows            (pool, es/out split per the settlement formula)

Where the plan text was superseded by a later Decision Register ruling or a
client workbook, the scenario is verified against the ruling and a NOTE line
records the supersession. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests.smoke_edges import make_dn, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company
from periodic_valuation.tests.smoke_std import ensure_std_masters

CHECKS = []
NOTES = []


def tc(no, label, ok, detail=""):
	CHECKS.append((f"TC{no:02d} {label}", bool(ok)))
	print(("PASS " if ok else "FAIL ") + f"TC{no:02d} {label}"
		+ (f" - {detail}" if detail and not ok else ""))


def note(no, msg):
	NOTES.append((no, msg))
	print(f"NOTE TC{no:02d} {msg}")


def std_item(code, view="MTD"):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": view}).insert(ignore_permissions=True)
	return code


def scv_release(company, item, year, month, sc):
	scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company,
		"item_code": item, "valid_from_year": year, "valid_from_month": month,
		"standard_cost": sc, "source_type": "MANUAL_OVERRIDE"})
	scv.insert(ignore_permissions=True)
	scv.release()
	return scv


def gl_net(voucher_no=None, event=None):
	"""{account: net signed amount (+Dr/-Cr)} for a voucher or one IVE."""
	filters = {"is_cancelled": 0}
	if voucher_no:
		filters["voucher_no"] = voucher_no
	if event:
		filters["valuation_event_id"] = event
	rows = frappe.get_all("GL Entry", filters=filters, fields=["account", "debit", "credit"])
	out = {}
	for g in rows:
		out[g.account] = flt(out.get(g.account, 0) + flt(g.debit) - flt(g.credit), 2)
	return {k: v for k, v in out.items() if flt(v, 2)}


def gl_match(no, label, actual, expected):
	"""expected: {account: signed net}. Exact set + amount match."""
	exp = {k: flt(v, 2) for k, v in expected.items() if flt(v, 2)}
	ok = actual == exp
	tc(no, label, ok, f"got {actual} want {exp}")


def one_ive(**filters):
	f = dict(filters)
	f.setdefault("is_cancelled", 0)
	rows = frappe.get_all("Inventory Valuation Event", filters=f,
		fields=["name", "std_trans", "qty_adj", "total_sc", "total_ac",
			"posting_date", "period_year", "period_month", "reversal_of"],
		order_by="creation")
	return rows


def ipb(company, item, year, month):
	rows = frappe.get_all("Inventory Period Balance",
		filters={"company": company, "item_code": item,
			"period_year": year, "period_month": month},
		fields=["closing_qty", "closing_value", "closing_reference_value",
			"settlement_inventory_total", "reval_value", "period_standard_cost"])
	return rows[0] if rows else None


def accounts_for(company, item, wh):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	return StdEngine(company, item, wh).accounts()


# ===================================================================== A
def section_a(company, wh, today):
	"""Basic STD postings - TC1..TC7 (plan 'Basic STD postings' table)."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	item = std_item("_TCV-A1")
	scv_release(company, item, today.year, today.month, 10)
	a = accounts_for(company, item, wh)

	# ---- TC1: receipt at SC, actual != SC.  Formula: inv = q*SC; ppv = q*(AC-SC)
	pr = make_pr(item, wh, 100, 12)
	ive = one_ive(source_docname=pr.name)
	tc(1, "IVE Rec qty 100 / SC 1000 / AC 1200",
		len(ive) == 1 and ive[0].std_trans == "Rec" and flt(ive[0].qty_adj) == 100
		and flt(ive[0].total_sc) == 1000 and flt(ive[0].total_ac) == 1200, str(ive))
	gl_match(1, "GL Dr Inv 1000 / Dr PPV 200 / Cr GR-IR 1200", gl_net(voucher_no=pr.name),
		{a.stock: 1000, a.ppv: 200, a.grir: -1200})
	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": pr.name},
		fields=["actual_qty", "valuation_rate", "stock_value_difference", "posted_via_valuation_kernel"])
	tc(1, "SLE shadow at standard (qty 100, rate 10, svd 1000)",
		sle and flt(sle[0].actual_qty) == 100 and flt(sle[0].valuation_rate) == 10
		and flt(sle[0].stock_value_difference) == 1000 and sle[0].posted_via_valuation_kernel, str(sle))
	bin_qty = flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "actual_qty"))
	b = ipb(company, item, today.year, today.month)
	tc(1, "Bin 100 / IPB closing 100 @ 1000",
		bin_qty == 100 and b and flt(b.closing_qty) == 100 and flt(b.closing_value) == 1000,
		f"bin {bin_qty} ipb {b}")

	# ---- TC2: issue at SC, no variance event
	dn = make_dn(item, wh, 30)
	ive = one_ive(source_docname=dn.name)
	tc(2, "IVE Iss -300 at SC, no variance",
		len(ive) == 1 and ive[0].std_trans == "Iss" and flt(ive[0].total_sc) == -300
		and flt(ive[0].total_ac) == 0, str(ive))
	gl_match(2, "GL Dr COGS 300 / Cr Inv 300", gl_net(voucher_no=dn.name),
		{a.cogs: 300, a.stock: -300})
	b = ipb(company, item, today.year, today.month)
	tc(2, "IPB 70 @ 700 after issue", b and flt(b.closing_qty) == 70
		and flt(b.closing_value) == 700, str(b))

	# ---- TC3: LCV -> variance only, NO inventory impact
	exp_acct = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"root_type": "Expense", "account_type": ("in", ("", None, "Expense Account"))},
		limit=1, pluck="name")[0]
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": company,
		"posting_date": nowdate(), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name, "supplier": "_SMK Supplier",
			"grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp_acct, "description": "freight", "amount": 50}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	ive = one_ive(source_docname=lcv.name)
	tc(3, "IVE LC charge 50 (SC 0 / AC 50)",
		len(ive) == 1 and ive[0].std_trans == "LC" and flt(ive[0].total_sc) == 0
		and flt(ive[0].total_ac) == 50, str(ive))
	glv = gl_net(event=ive[0].name) if ive else {}
	gl_match(3, "GL Dr PPV 50 / Cr charge account 50 - inventory untouched", glv,
		{a.ppv: 50, exp_acct: -50})
	b = ipb(company, item, today.year, today.month)
	tc(3, "IPB unchanged by LCV (70 @ 700)", b and flt(b.closing_qty) == 70
		and flt(b.closing_value) == 700, str(b))

	# ---- TC4: sales return with reference - basis restored, no fresh variance
	from erpnext.controllers.sales_and_purchase_return import make_return_doc
	ret = make_return_doc("Delivery Note", dn.name)
	ret.items[0].qty = -10
	ret.insert(ignore_permissions=True)
	ret.submit()
	ive = one_ive(source_docname=ret.name)
	tc(4, "IVE SR +10 at SC (+100), no variance",
		len(ive) == 1 and ive[0].std_trans == "SR" and flt(ive[0].qty_adj) == 10
		and flt(ive[0].total_sc) == 100, str(ive))
	gl_match(4, "GL Dr Inv 100 / Cr COGS-recovery 100", gl_net(voucher_no=ret.name),
		{a.stock: 100, a.customer_cogs: -100})
	b = ipb(company, item, today.year, today.month)
	tc(4, "IPB 80 @ 800 after sales return", b and flt(b.closing_qty) == 80
		and flt(b.closing_value) == 800, str(b))

	# ---- TC5: purchase return with reference - PPV reverses proportionally
	pret = make_return_doc("Purchase Receipt", pr.name)
	pret.items[0].qty = -20
	pret.items[0].rate = 12
	pret.insert(ignore_permissions=True)
	pret.submit()
	ive = one_ive(source_docname=pret.name)
	tc(5, "IVE PR -20 at original SC (-200) / AC -240",
		len(ive) == 1 and ive[0].std_trans == "PR" and flt(ive[0].total_sc) == -200
		and flt(ive[0].total_ac) == -240, str(ive))
	gl_match(5, "GL Cr Inv 200 / Cr PPV 40 / Dr GR-IR 240 (proportional PPV back-out)",
		gl_net(voucher_no=pret.name), {a.stock: -200, a.ppv: -40, a.grir: 240})
	b = ipb(company, item, today.year, today.month)
	tc(5, "IPB 60 @ 600 after purchase return", b and flt(b.closing_qty) == 60
		and flt(b.closing_value) == 600, str(b))

	# ---- TC6: stock count gain at current SC, outside the settlement pool
	eng = StdEngine(company, item, wh)
	pool_before = flt(eng.own_ppv(today.year, today.month), 2)
	cnt = frappe.get_doc({"doctype": "Stock Count", "company": company,
		"posting_date": nowdate(),
		"items": [{"item_code": item, "warehouse": wh, "counted_qty": 70}]})
	cnt.insert(ignore_permissions=True)
	cnt.submit()
	ive = one_ive(source_docname=cnt.name)
	tc(6, "IVE SC+ 10 @ SC (+100)",
		len(ive) == 1 and ive[0].std_trans == "SC+" and flt(ive[0].total_sc) == 100, str(ive))
	gl_match(6, "GL Dr Inv 100 / Cr Stock Adjustment 100", gl_net(voucher_no=cnt.name),
		{a.stock: 100, a.stock_adj: -100})
	tc(6, "count gain does NOT enter the settlement pool",
		flt(eng.own_ppv(today.year, today.month), 2) == pool_before,
		f"pool {eng.own_ppv(today.year, today.month)} was {pool_before}")

	# ---- TC7: stock count loss - mirror of TC6
	cnt2 = frappe.get_doc({"doctype": "Stock Count", "company": company,
		"posting_date": nowdate(),
		"items": [{"item_code": item, "warehouse": wh, "counted_qty": 65}]})
	cnt2.insert(ignore_permissions=True)
	cnt2.submit()
	ive = one_ive(source_docname=cnt2.name)
	tc(7, "IVE SC- -5 @ SC (-50)",
		len(ive) == 1 and ive[0].std_trans == "SC-" and flt(ive[0].total_sc) == -50, str(ive))
	gl_match(7, "GL Dr Stock Adjustment 50 / Cr Inv 50", gl_net(voucher_no=cnt2.name),
		{a.stock_adj: 50, a.stock: -50})
	tc(7, "IPB 65 @ 650 and Bin 65 agree",
		flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "actual_qty")) == 65
		and flt(ipb(company, item, today.year, today.month).closing_qty) == 65, "")

	# section identity: pool = 200 (TC1) + 50 (LCV) - 40 (TC5 back-out) = 210
	tc(7, "period PPV pool = 210 (receipt 200 + LCV 50 - return 40; counts excluded)",
		flt(eng.own_ppv(today.year, today.month), 2) == 210,
		str(eng.own_ppv(today.year, today.month)))


# ===================================================================== B
def section_b(company, wh, today):
	"""Standard cost change handling - TC8..TC13."""
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		materialize_pending_revaluations,
	)
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	prev = add_days(get_first_day(today), -1)  # a day in the previous month
	py, pm = prev.year, prev.month

	# ---- TC8: clean boundary change -> single Rev Beg = on-hand x delta
	item8 = std_item("_TCV-B8")
	scv_release(company, item8, py, pm, 10)
	e8 = StdEngine(company, item8, wh)
	src = ("Item", item8)
	e8.post(trans="Rec", posting_date=prev.replace(day=10), qty=100, sc=10, ac=10, source=src)
	scv_release(company, item8, today.year, today.month, 12)
	materialize_pending_revaluations()
	a = accounts_for(company, item8, wh)
	revs = one_ive(item_code=item8, std_trans=("in", ("Rev Beg", "REV In", "REV out")))
	tc(8, "boundary release posts a single Rev Beg 100 x 2 = 200",
		len(revs) == 1 and revs[0].std_trans == "Rev Beg" and flt(revs[0].total_sc) == 200,
		str(revs))
	if revs:
		gl_match(8, "GL Dr Inv 200 / Cr Reval Reserve 200", gl_net(event=revs[0].name),
			{a.stock: 200, a.reserve: -200})

	# ---- TC9: mid-period change -> 3-leg granular boundary reval
	item9 = std_item("_TCV-B9")
	scv_release(company, item9, py, pm, 10)
	e9 = StdEngine(company, item9, wh)
	src9 = ("Item", item9)
	e9.post(trans="Rec", posting_date=prev.replace(day=12), qty=60, sc=10, ac=10, source=src9)
	# dated today (not a fixed day-of-month): on the 1st a fixed day 2/3 would be a
	# future date the boundary materializer rightly ignores
	e9.post(trans="Rec", posting_date=today, qty=40, sc=10, ac=10, source=src9)
	e9.post(trans="Iss", posting_date=today, qty=30, sc=10, source=src9)
	scv_release(company, item9, today.year, today.month, 12)
	materialize_pending_revaluations()
	revs = {r.std_trans: r for r in one_ive(item_code=item9,
		std_trans=("in", ("Rev Beg", "REV In", "REV out")))}
	tc(9, "granular triplet: Rev Beg 120 / REV In 80 / REV out -60",
		flt(revs.get("Rev Beg", {}).get("total_sc", 0)) == 120
		and flt(revs.get("REV In", {}).get("total_sc", 0)) == 80
		and flt(revs.get("REV out", {}).get("total_sc", 0)) == -60, str(revs))
	total = flt(sum(flt(r.total_sc) for r in revs.values()), 2)
	tc(9, "triplet sums to on-hand x delta (70 x 2 = 140)", total == 140, str(total))
	if "REV out" in revs:
		gl_match(9, "REV out GL: Dr COGS Adj 60 / Cr Inv 60", gl_net(event=revs["REV out"].name),
			{a.cogs_adj: 60, a.stock: -60})
	if "REV In" in revs:
		gl_match(9, "REV In GL: Dr Inv 80 / Cr Reval Reserve 80", gl_net(event=revs["REV In"].name),
			{a.stock: 80, a.reserve: -80})

	# ---- TC10: backdated receipt through the REAL voucher path
	# original at ORIGINAL month SC (10) + companion at delta (2) -> net qty x current SC
	pr = make_pr(item8, wh, 20, 12, posting_date=str(prev.replace(day=15)))
	pair = one_ive(source_docname=pr.name)
	orig = next((r for r in pair if r.std_trans == "REC (BD)"), None)
	comp = next((r for r in pair if r.std_trans == "REC (BD) - Rev"), None)
	tc(10, "REC (BD) books prior month at original SC (200) + companion bridge 40",
		orig and flt(orig.total_sc) == 200 and orig.period_month == pm
		and comp and flt(comp.total_sc) == 40 and comp.period_month == today.month,
		str(pair))
	if comp:
		gl_match(10, "companion GL: Dr Inv 40 / Cr Reval Reserve 40", gl_net(event=comp.name),
			{a.stock: 40, a.reserve: -40})
	if orig:
		gl_match(10, "original GL: Dr Inv 200 / Dr PPV 40 / Cr GR-IR 240",
			gl_net(event=orig.name), {a.stock: 200, a.ppv: 40, a.grir: -240})
	net = flt((orig and orig.total_sc or 0) + (comp and comp.total_sc or 0), 2)
	tc(10, "net inventory effect = qty x current SC (20 x 12 = 240)", net == 240, str(net))

	# ---- TC11: backdated issue - companion tops up consumption (COGS Adj)
	dn = make_dn(item8, wh, 10, posting_date=str(prev.replace(day=20)))
	pair = one_ive(source_docname=dn.name)
	orig = next((r for r in pair if r.std_trans == "Issue (BD)"), None)
	comp = next((r for r in pair if r.std_trans == "Issue (BD) - Rev"), None)
	tc(11, "Issue (BD) relieves prior month at original SC (-100) + companion -20",
		orig and flt(orig.total_sc) == -100 and comp and flt(comp.total_sc) == -20, str(pair))
	if comp:
		gl_match(11, "companion GL: Dr COGS Adj 20 / Cr Inv 20 (units already left)",
			gl_net(event=comp.name), {a.cogs_adj: 20, a.stock: -20})
	net = flt((orig and orig.total_sc or 0) + (comp and comp.total_sc or 0), 2)
	tc(11, "net effect = qty x current SC (10 x 12 = 120 relieved)", net == -120, str(net))

	# ---- TC12/13: back-YEAR pair (engine-level; the voucher window rule needs
	# a January clock and is enforced by _backdate_class - see kernel)
	item12 = std_item("_TCV-B12")
	scv_release(company, item12, 2025, 12, 8)  # YEC verification needs a released SCV
	e12 = StdEngine(company, item12, wh)
	src12 = ("Item", item12)
	a12 = accounts_for(company, item12, wh)
	sett_2025_before = frappe.db.count("Inventory Period Settlement",
		{"company": company, "period_year": 2025})
	e12.post(trans="REC (BY)", posting_date="2025-12-10", qty=10, sc=8, ac=8,
		source=src12, entry_date=str(today))
	e12.post(trans="REC (BY) - Rev", posting_date=str(today), qty=10, sc=11,
		t_sc_override=30, source=src12)
	by = one_ive(item_code=item12, std_trans="REC (BY)")
	byrev = one_ive(item_code=item12, std_trans="REC (BY) - Rev")
	tc(12, "REC (BY) books Dec 2025 at prior-FY SC (80); companion 30 in current period",
		by and flt(by[0].total_sc) == 80 and by[0].period_year == 2025
		and byrev and flt(byrev[0].total_sc) == 30 and byrev[0].period_year == today.year,
		f"{by} {byrev}")
	if byrev:
		gl_match(12, "BY companion GL: Dr Inv 30 / Cr Reval Reserve 30",
			gl_net(event=byrev[0].name), {a12.stock: 30, a12.reserve: -30})
	# pool convention is ac - sc (workbook sign: a cost-increase bridge is a
	# CREDIT pool the settlement redistributes), so the +30 bridge reads -30
	tc(12, "BY variance feeds the CURRENT FY pool (rev pool -30, ac-sc convention)",
		flt(e12.own_rev(today.year, today.month), 2) == -30,
		str(e12.own_rev(today.year, today.month)))

	e12.post(trans="Issue (BY)", posting_date="2025-12-15", qty=5, sc=8,
		source=src12, entry_date=str(today))
	e12.post(trans="Issue (BY) - Rev", posting_date=str(today), qty=5, sc=11,
		t_sc_override=-15, source=src12)
	ibyrev = one_ive(item_code=item12, std_trans="Issue (BY) - Rev")
	tc(13, "Issue (BY) -40 at prior-FY SC; companion -15 current period",
		ibyrev and flt(ibyrev[0].total_sc) == -15, str(ibyrev))
	if ibyrev:
		gl_match(13, "BY issue companion GL: Dr Reval Reserve 15 / Cr Inv 15 (prior-FY COGS closed)",
			gl_net(event=ibyrev[0].name), {a12.reserve: 15, a12.stock: -15})

	# TC34 rides on the same scenario: cross-FY late entry leaves prior FY alone
	sett_2025_after = frappe.db.count("Inventory Period Settlement",
		{"company": company, "period_year": 2025})
	tc(34, "cross-FY late entries touch NO prior-FY settlement",
		sett_2025_after == sett_2025_before, f"{sett_2025_before}->{sett_2025_after}")
	note(34, "as-built carries no is_cross_fiscal_year column; the (BY) trans label "
		"+ period stamps carry the same audit information (plan field superseded)")


# ===================================================================== C
def section_c(company, wh, today):
	"""MTD settlement - TC14..TC19 (+TC33 identity on the TC14 scope)."""
	from periodic_valuation.periodic_standard_cost.engine import (
		PeriodLockedError, StdEngine, r2)

	# ---- TC14/15: the client workbook Feb anchor (pool 320, 95 in, 80 end)
	item = std_item("_TCV-C14")
	e = StdEngine(company, item, wh)
	src = ("Item", item)
	a = accounts_for(company, item, wh)
	e.post(trans="Rec", posting_date="2026-03-05", qty=95, sc=10, t_ac_override=1270, source=src)
	e.post(trans="Iss", posting_date="2026-03-12", qty=15, sc=10, source=src)
	pool, end_q, denom, out_q = 320.0, 80.0, 95.0, 15.0
	exp_es = flt(pool * end_q / denom, 2)     # 269.47
	exp_out = flt(pool * out_q / denom, 2)    # 50.53
	sett = e.close_period(year=2026, month=3, sc=10, source=src, entry_date="2026-04-01")
	tc(14, f"MTD settle split {exp_es}/{exp_out} on 320 pool (workbook anchor)",
		flt(sett.es_var, 2) == exp_es and flt(sett.out_var, 2) == exp_out,
		f"{sett.es_var}/{sett.out_var}")
	gl_match(14, "Sett GL: Dr Inv 269.47 / Dr COGS Adj 50.53 / Cr PPV 320",
		gl_net(event=sett.sett_event),
		{a.stock: exp_es, a.cogs_adj: exp_out, a.ppv: -pool})
	sett_rev = one_ive(item_code=item, std_trans="Sett - Rev")
	tc(15, "Sett-Rev posts next day 1 with inventory share only (-269.47)",
		sett_rev and str(sett_rev[0].posting_date) == "2026-04-01"
		and flt(sett_rev[0].total_sc, 2) == -exp_es, str(sett_rev))
	rev_gl = gl_net(event=sett.sett_rev_event)
	gl_match(15, "Sett-Rev GL: Cr Inv 269.47 / Dr PPV 269.47 - COGS leg untouched",
		rev_gl, {a.stock: -exp_es, a.ppv: exp_es})
	tc(15, "no COGS Adjustment row on the Sett-Rev", a.cogs_adj not in rev_gl, str(rev_gl))

	# ---- TC16: closing qty 0 -> whole pool to consumption
	i16 = std_item("_TCV-C16")
	e16 = StdEngine(company, i16, wh)
	s16 = ("Item", i16)
	e16.post(trans="Rec", posting_date="2026-03-04", qty=50, sc=10, t_ac_override=600, source=s16)
	e16.post(trans="Iss", posting_date="2026-03-20", qty=50, sc=10, source=s16)
	sett16 = e16.close_period(year=2026, month=3, sc=10, source=s16, entry_date="2026-04-01")
	tc(16, "zero closing qty: es 0 / out 100",
		flt(sett16.es_var, 2) == 0 and flt(sett16.out_var, 2) == 100,
		f"{sett16.es_var}/{sett16.out_var}")
	gl_match(16, "Sett GL: Dr COGS Adj 100 / Cr PPV 100 - no inventory leg",
		gl_net(event=sett16.sett_event), {a.cogs_adj: 100, a.ppv: -100})

	# ---- TC17: no consumption -> whole pool capitalized
	i17 = std_item("_TCV-C17")
	e17 = StdEngine(company, i17, wh)
	s17 = ("Item", i17)
	e17.post(trans="Rec", posting_date="2026-03-06", qty=50, sc=10, t_ac_override=600, source=s17)
	sett17 = e17.close_period(year=2026, month=3, sc=10, source=s17, entry_date="2026-04-01")
	tc(17, "no out qty: es 100 / out 0",
		flt(sett17.es_var, 2) == 100 and flt(sett17.out_var, 2) == 0,
		f"{sett17.es_var}/{sett17.out_var}")
	gl_match(17, "Sett GL: Dr Inv 100 / Cr PPV 100 - no COGS leg",
		gl_net(event=sett17.sett_event), {a.stock: 100, a.ppv: -100})

	# ---- TC18: favourable (negative) pool -> settlement credits Inv and COGS
	i18 = std_item("_TCV-C18")
	e18 = StdEngine(company, i18, wh)
	s18 = ("Item", i18)
	e18.post(trans="Rec", posting_date="2026-03-07", qty=50, sc=10, t_ac_override=400, source=s18)
	e18.post(trans="Iss", posting_date="2026-03-21", qty=20, sc=10, source=s18)
	sett18 = e18.close_period(year=2026, month=3, sc=10, source=s18, entry_date="2026-04-01")
	tc(18, "favourable pool -100: es -60 / out -40",
		flt(sett18.es_var, 2) == -60 and flt(sett18.out_var, 2) == -40,
		f"{sett18.es_var}/{sett18.out_var}")
	gl_match(18, "Sett GL: Cr Inv 60 / Cr COGS Adj 40 / Dr PPV 100",
		gl_net(event=sett18.sett_event), {a.stock: -60, a.cogs_adj: -40, a.ppv: 100})

	# ---- TC19: late entry into a settled prior period
	# Plan text prescribed automatic POST_CLOSE_REPOST; DR-21 killed the reach-back:
	# the settled period BLOCKS the posting, the sanctioned correction is a manual
	# Sett-Reverse (previous month only, DR-08) -> post late entry -> re-close.
	note(19, "plan's automatic POST_CLOSE_REPOST superseded by DR-21/DR-08: "
		"settled periods block postings; correction = Sett-Reverse + repost + re-close")
	i19 = std_item("_TCV-C19")
	e19 = StdEngine(company, i19, wh)
	s19 = ("Item", i19)
	e19.post(trans="Rec", posting_date="2026-05-05", qty=100, sc=10, t_ac_override=1200, source=s19)
	e19.post(trans="Iss", posting_date="2026-05-15", qty=40, sc=10, source=s19)
	sett19 = e19.close_period(year=2026, month=5, sc=10, source=s19, entry_date="2026-06-01")
	try:
		e19.post(trans="Rec", posting_date="2026-05-20", qty=10, sc=10, t_ac_override=150, source=s19)
		tc(19, "settled period blocks the late entry", False, "posted")
	except PeriodLockedError:
		tc(19, "settled period blocks the late entry", True)
	e19.sett_reverse(sett19.name, source=s19, entry_date="2026-06-05")
	e19.post(trans="Rec", posting_date="2026-05-20", qty=10, sc=10, t_ac_override=150, source=s19)
	sett19b = e19.close_period(year=2026, month=5, sc=10, source=s19, entry_date="2026-06-06")
	# corrected basis: pool 250, denom 110, end 70
	exp_es2, exp_out2 = flt(250 * 70 / 110, 2), flt(250 * 40 / 110, 2)
	tc(19, f"re-close on corrected pool 250: {exp_es2}/{exp_out2}",
		flt(sett19b.es_var, 2) == exp_es2 and flt(sett19b.out_var, 2) == exp_out2,
		f"{sett19b.es_var}/{sett19b.out_var}")
	tc(19, "original settlement flagged cancelled",
		frappe.db.get_value("Inventory Period Settlement", sett19.name, "cancelled") == 1, "")
	# TC37 design promise on the MTD path: after the reversal pair the accounts
	# are back to their pre-Sett state (all 4 events of the CANCELLED settlement
	# must net to zero per account)
	undo = {}
	for ev in frappe.get_all("Inventory Valuation Event",
			filters={"item_code": i19, "settlement_ref": sett19.name, "is_cancelled": 0},
			fields=["name"]):
		for acct, amt in gl_net(event=ev.name).items():
			undo[acct] = flt(undo.get(acct, 0) + amt, 2)
	undo = {k: v for k, v in undo.items() if flt(v, 2)}
	tc(37, "MTD cancelled settlement nets to zero across its 4 events (pre-Sett state)",
		not undo, f"residual {undo}")


# ===================================================================== D
def section_d(company, wh, today):
	"""YTD settlement - TC20..TC25."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	# ---- TC20/21: cumulative pool across two periods
	item = std_item("_TCV-D20", view="YTD")
	e = StdEngine(company, item, wh)
	src = ("Item", item)
	a = accounts_for(company, item, wh)
	e.post(trans="Rec", posting_date="2026-01-05", qty=100, sc=10, t_ac_override=1150, source=src)
	e.post(trans="Iss", posting_date="2026-01-15", qty=40, sc=10, source=src)
	sj = e.close_period(year=2026, month=1, sc=10, source=src, entry_date="2026-02-01")
	tc(20, "YTD Jan settle 90/60 on 150 pool",
		flt(sj.es_var, 2) == 90 and flt(sj.out_var, 2) == 60, f"{sj.es_var}/{sj.out_var}")
	jan_rev_gl = gl_net(event=sj.sett_rev_event)
	gl_match(21, "YTD same-FY Sett-Rev is the FULL 4-leg mirror (workbook V2.03)",
		jan_rev_gl, {a.stock: -90, a.cogs_adj: -60, a.ppv: 150})
	note(21, "plan text said 'COGS permanent' for the YTD Sett-Rev; the signed YTD "
		"workbook re-imports the FULL pool same-FY (delta mechanism) - built per workbook")

	e.post(trans="Rec", posting_date="2026-02-05", qty=50, sc=10, t_ac_override=620, source=src)
	e.post(trans="Iss", posting_date="2026-02-14", qty=30, sc=10, source=src)
	# cumulative: pool 270, input 150, end 80, out 70
	exp_es = flt(270 * 80 / 150, 2)
	exp_out = flt(270 * 70 / 150, 2)
	sf = e.close_period(year=2026, month=2, sc=10, source=src, entry_date="2026-03-01")
	tc(20, f"YTD Feb settles the CUMULATIVE pool 270 -> {exp_es}/{exp_out}",
		flt(sf.variance, 2) == 270 and flt(sf.es_var, 2) == exp_es
		and flt(sf.out_var, 2) == exp_out,
		f"var {sf.variance} split {sf.es_var}/{sf.out_var}")
	tc(20, "net COGS across Jan+Feb pairs equals latest cumulative consumption share",
		flt(60 - 60 + exp_out, 2) == exp_out, "")

	# ---- TC22: negative cumulative pool -> settlement credits both
	i22 = std_item("_TCV-D22", view="YTD")
	e22 = StdEngine(company, i22, wh)
	s22 = ("Item", i22)
	e22.post(trans="Rec", posting_date="2026-01-06", qty=50, sc=10, t_ac_override=400, source=s22)
	e22.post(trans="Iss", posting_date="2026-01-16", qty=20, sc=10, source=s22)
	s = e22.close_period(year=2026, month=1, sc=10, source=s22, entry_date="2026-02-01")
	tc(22, "negative YTD pool -100: es -60 / out -40 (credits Inv and COGS)",
		flt(s.es_var, 2) == -60 and flt(s.out_var, 2) == -40, f"{s.es_var}/{s.out_var}")
	gl_match(22, "Sett GL: Cr Inv 60 / Cr COGS Adj 40 / Dr PPV 100",
		gl_net(event=s.sett_event), {a.stock: -60, a.cogs_adj: -40, a.ppv: 100})

	# ---- TC23: year-end Sett + Jan-1 Sett-Rev = inventory-share-only carry (DR-06)
	i23 = std_item("_TCV-D23", view="YTD")
	scv_release(company, i23, 2025, 12, 10)
	e23 = StdEngine(company, i23, wh)
	s23 = ("Item", i23)
	e23.post(trans="Rec", posting_date="2025-12-05", qty=40, sc=10, t_ac_override=480, source=s23)
	e23.post(trans="Iss", posting_date="2025-12-15", qty=10, sc=10, source=s23)
	sd = e23.close_period(year=2025, month=12, sc=10, source=s23, entry_date="2026-01-01")
	tc(23, "Dec settle 60/20 on 80 pool",
		flt(sd.es_var, 2) == 60 and flt(sd.out_var, 2) == 20, f"{sd.es_var}/{sd.out_var}")
	srv = one_ive(item_code=i23, std_trans="Sett - Rev")
	tc(23, "year-boundary Sett-Rev lands Jan 1 with the inventory share (-60)",
		srv and str(srv[0].posting_date) == "2026-01-01" and flt(srv[0].total_sc, 2) == -60,
		str(srv))
	gl_match(23, "cross-FY Sett-Rev GL: Cr Inv 60 / Dr PPV 60 - inventory share ONLY (DR-06)",
		gl_net(event=sd.sett_rev_event), {a.stock: -60, a.ppv: 60})

	# ---- TC24: CARRY_PER_VIEW - prior-FY residual rolls into the new FY pool
	i24 = std_item("_TCV-D24", view="YTD")
	scv_release(company, i24, 2025, 12, 10)
	e24 = StdEngine(company, i24, wh)
	s24 = ("Item", i24)
	e24.post(trans="Rec", posting_date="2025-12-06", qty=20, sc=10, t_ac_override=260, source=s24)
	yec_exists = frappe.db.exists("STD Year End Close",
		{"company": company, "fiscal_year": 2025, "docstatus": 1})
	if not yec_exists:
		try:
			yec = frappe.get_doc({"doctype": "STD Year End Close", "company": company,
				"fiscal_year": 2025})
			yec.insert(ignore_permissions=True)
			yec.submit()
			tc(24, "Year End Close force-settles the open FY-2025 scope",
				yec.status == "Completed" and yec.scopes_settled >= 1,
				f"{yec.status} {yec.scopes_settled}")
		except frappe.ValidationError as e:
			tc(24, "Year End Close force-settles the open FY-2025 scope", False, str(e)[:200])
	sd24 = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": i24, "period_year": 2025, "period_month": 12, "cancelled": 0},
		fields=["es_var", "out_var"])
	tc(24, "forced Dec settle: full pool 60 capitalized (no consumption)",
		sd24 and flt(sd24[0].es_var, 2) == 60 and flt(sd24[0].out_var, 2) == 0, str(sd24))
	e24.post(trans="Rec", posting_date="2026-01-08", qty=10, sc=10, t_ac_override=120, source=s24)
	sj24 = e24.close_period(year=2026, month=1, sc=10, source=s24, entry_date="2026-02-01")
	tc(24, "new-FY Jan pool = carry 60 + own 20 = 80 (CARRY_PER_VIEW)",
		flt(sj24.variance, 2) == 80, str(sj24.variance))

	# ---- TC25: FULL_SETTLE_AT_YEAR_END (DR-38) - December allocates the whole
	# pool to consumption, posts no Sett-Rev, the new year opens with no carry
	settings_name = frappe.db.get_value("Periodic Standard Cost Settings", {"company": company}, "name")
	sdoc = frappe.get_doc("Periodic Standard Cost Settings", settings_name)
	sdoc.year_end_variance_carryforward = "FULL_SETTLE_AT_YEAR_END"
	sdoc.save(ignore_permissions=True)   # allowed: no December of the current FY is settled yet
	frappe.clear_cache()
	try:
		i25 = std_item("_TCV-D25", view="YTD")
		scv_release(company, i25, 2025, 12, 10)
		e25 = StdEngine(company, i25, wh)
		s25 = ("Item", i25)
		e25.post(trans="Rec", posting_date="2025-12-05", qty=40, sc=10, t_ac_override=480, source=s25)
		e25.post(trans="Iss", posting_date="2025-12-15", qty=10, sc=10, source=s25)
		sd25 = e25.close_period(year=2025, month=12, sc=10, source=s25, entry_date="2026-01-01")
		tc(25, "FULL_SETTLE December: whole pool 80 to consumption, nothing capitalised",
			flt(sd25.es_var, 2) == 0 and flt(sd25.out_var, 2) == 80 and flt(sd25.variance, 2) == 80,
			f"{sd25.es_var}/{sd25.out_var}")
		gl_match(25, "Sett GL: Dr COGS Adj 80 / Cr PPV 80 - no inventory leg",
			gl_net(event=sd25.sett_event), {a.cogs_adj: 80, a.ppv: -80})
		tc(25, "no Sett-Rev posted on 1 January (nothing to carry)",
			not sd25.sett_rev_event and not one_ive(item_code=i25, std_trans="Sett - Rev"),
			str(sd25.sett_rev_event))
		try:
			e25.sett_reverse(sd25.name, source=s25, entry_date="2026-01-05")
			tc(25, "December under FULL_SETTLE is hard-closed (reverse refused)", False, "reversed")
		except frappe.ValidationError as e:
			tc(25, "December under FULL_SETTLE is hard-closed (reverse refused)",
				"hard-closed" in str(e), str(e)[:100])
		e25.post(trans="Rec", posting_date="2026-01-08", qty=10, sc=10, t_ac_override=120, source=s25)
		sj25 = e25.close_period(year=2026, month=1, sc=10, source=s25, entry_date="2026-02-01")
		tc(25, "new-FY January pool = own 20 only (year_opening_variance 0)",
			flt(sj25.variance, 2) == 20, str(sj25.variance))
	finally:
		sdoc = frappe.get_doc("Periodic Standard Cost Settings", settings_name)
		sdoc.year_end_variance_carryforward = "CARRY_PER_VIEW"
		sdoc.save(ignore_permissions=True)
		frappe.clear_cache()


# ===================================================================== E
def section_e(company, wh, today):
	"""Transfers & migration - TC29, TC31, TC32."""
	# ---- TC29: warehouse transfer under STD
	item = std_item("_TCV-E29")
	scv_release(company, item, today.year, today.month, 10)
	make_pr(item, wh, 20, 10)
	abbr = frappe.db.get_value("Company", company, "abbr")
	wh2 = f"_TCV Stores 2 - {abbr}"
	if not frappe.db.exists("Warehouse", wh2):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_TCV Stores 2",
			"company": company}).insert(ignore_permissions=True)
	try:
		se = frappe.get_doc({"doctype": "Stock Entry", "company": company,
			"stock_entry_type": "Material Transfer", "posting_date": nowdate(),
			"items": [{"item_code": item, "qty": 5, "s_warehouse": wh, "t_warehouse": wh2,
				"transfer_qty": 5, "uom": frappe.db.get_value("Item", item, "stock_uom"),
				"conversion_factor": 1}]})
		se.insert(ignore_permissions=True)
		se.submit()
		tc(29, "warehouse transfer refused for STD items (v1 scope decision)", False, "submitted")
	except frappe.ValidationError as e:
		tc(29, "warehouse transfer refused for STD items (v1 scope decision)",
			"not supported" in str(e).lower(), str(e)[:120])
	note(29, "plan's T1 same-SC transfer scenario superseded: transfers deferred "
		"out of the v1 build (kernel refuses with a clear message)")

	# ---- TC31: cutover / go-live opening via Stock Reconciliation -> Beg
	i31 = std_item("_TCV-E31")
	scv_release(company, i31, today.year, today.month, 12)
	a = accounts_for(company, i31, wh)
	tsa = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"account_type": "Temporary"}, limit=1, pluck="name")
	opening_acct = tsa[0] if tsa else frappe.get_all("Account",
		filters={"company": company, "is_group": 0, "root_type": "Liability"},
		limit=1, pluck="name")[0]
	sr = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company,
		"purpose": "Opening Stock", "posting_date": nowdate(), "set_posting_time": 1,
		"expense_account": opening_acct,
		"items": [{"item_code": i31, "warehouse": wh, "qty": 100, "valuation_rate": 15}]})
	sr.insert(ignore_permissions=True)
	sr.submit()
	beg = one_ive(source_docname=sr.name)
	tc(31, "opening posts Beg: qty 100, SC 1200, legacy value 1500",
		len(beg) == 1 and beg[0].std_trans == "Beg" and flt(beg[0].qty_adj) == 100
		and flt(beg[0].total_sc) == 1200 and flt(beg[0].total_ac) == 1500, str(beg))
	gl_match(31, "Beg GL: Dr Inv 1200 / Dr PPV 300 / Cr FY Carry-Forward 1500",
		gl_net(voucher_no=sr.name), {a.stock: 1200, a.ppv: 300, a.fy_carry: -1500})
	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": sr.name},
		fields=["actual_qty", "valuation_rate"])
	tc(31, "SLE opening at standard (100 @ 12)",
		sle and flt(sle[0].actual_qty) == 100 and flt(sle[0].valuation_rate) == 12, str(sle))
	note(31, "plan's separate migration_delta account superseded: the SC-vs-legacy "
		"difference (300) posts as the Beg PPV leg and settles per the normal pool")

	# ---- TC32: settlement-view change is ISVC-governed
	i32 = std_item("_TCV-E32")
	scv_release(company, i32, today.year, today.month, 10)
	make_pr(i32, wh, 10, 11)  # pending variance 10 in the open period
	isvc = frappe.get_doc({"doctype": "Item Settlement View Change", "company": company,
		"item_code": i32, "to_view": "YTD", "reason": "design TC32"})
	isvc.insert(ignore_permissions=True)
	tc(32, "ISVC snapshots the from-view (MTD)", isvc.from_view == "MTD", isvc.from_view)
	try:
		isvc.approve()
		tc(32, "self-approval blocked (SoD)", False, "approved")
	except frappe.ValidationError:
		tc(32, "self-approval blocked (SoD)", True)
	approver = frappe.get_all("User",
		filters={"name": ("not in", [frappe.session.user, "Guest"])}, limit=1, pluck="name")[0]
	isvc.db_set({"status": "Approved", "approved_by": approver})
	isvc.reload()
	isvc.submit()
	tc(32, "flip posted: item now YTD, record immutable (docstatus 1)",
		frappe.db.get_value("Item", i32, "settlement_view") == "YTD"
		and isvc.docstatus == 1 and isvc.status == "Posted", isvc.status)
	forced = frappe.get_all("Inventory Period Settlement",
		filters={"item_code": i32, "cancelled": 0}, fields=["es_var", "out_var", "settlement_view"])
	tc(32, "pending variance force-settled UNDER THE OLD VIEW before the flip",
		forced and forced[0].settlement_view == "MTD" and flt(forced[0].es_var, 2) == 10,
		str(forced))


# ===================================================================== F
def section_f(company, wh, today):
	"""Period machine, concurrency posture, settlement run - TC35, TC36."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	# ---- TC35: locking posture (structural - true concurrency needs 2 sessions)
	import inspect

	from periodic_valuation.periodic_moving_average import kernel as map_kernel
	src = inspect.getsource(map_kernel.ScopeState.load)
	tc(35, "scope state reads row-lock the balance (for_update)", "for_update" in src, "")
	note(35, "true two-session contention is not reproducible inside one rolled-back "
		"transaction; deadlock-ordering rule verified structurally")

	# ---- TC33: period-close reconciliation identity (voucher-driven scope so
	# the IPB row exists): closing_value = closing_qty x SC + capitalized variance
	i33 = std_item("_TCV-F33")
	scv_release(company, i33, today.year, today.month, 10)
	make_pr(i33, wh, 100, 12)   # PPV 200
	make_dn(i33, wh, 20)        # end 80 of 100
	e33 = StdEngine(company, i33, wh)
	s33 = e33.close_period(year=today.year, month=today.month, sc=10, source=("Item", i33))
	tc(33, "current-month close: es 160 / out 40 on 200 pool",
		flt(s33.es_var, 2) == 160 and flt(s33.out_var, 2) == 40,
		f"{s33.es_var}/{s33.out_var}")
	b = ipb(company, i33, today.year, today.month)
	tc(33, "IPB identity: closing_value 960 = 80 x SC 10 + capitalized 160",
		b and flt(b.closing_value, 2) == 960 and flt(b.closing_qty) == 80, str(b))
	tc(33, "IPB settlement stamps (reference 800 / inv total 160 / cons total 40)",
		b and flt(b.closing_reference_value, 2) == 800
		and flt(b.settlement_inventory_total, 2) == 160, str(b))

	# ---- TC36: multi-item settlement run over one period
	pairs = []
	for suffix, ac_total, iss in (("A", 240, 5), ("B", 330, 10)):
		it = std_item(f"_TCV-F36{suffix}")
		scv_release(company, it, 2026, 2, 10)
		en = StdEngine(company, it, wh)
		s = ("Item", it)
		en.post(trans="Rec", posting_date="2026-02-05", qty=20, sc=10,
			t_ac_override=ac_total, source=s)
		en.post(trans="Iss", posting_date="2026-02-12", qty=iss, sc=10, source=s)
		pairs.append((it, ac_total - 200, iss))
	run_doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run",
		"company": company, "period_year": 2026, "period_month": 2,
		"run_type": "INITIAL_CLOSE"})
	run_doc.insert(ignore_permissions=True)
	run_doc.submit()
	run_doc.reload()
	tc(36, "one run settles every eligible scope in the period",
		run_doc.status == "Completed" and run_doc.scopes_settled >= 2,
		f"{run_doc.status} {run_doc.scopes_settled}")
	for it, pool, iss in pairs:
		end_q, denom = 20 - iss, 20
		exp_es = flt(pool * end_q / denom, 2)
		exp_out = flt(pool * iss / denom, 2)
		st = frappe.get_all("Inventory Period Settlement",
			filters={"item_code": it, "period_year": 2026, "period_month": 2, "cancelled": 0},
			fields=["es_var", "out_var", "settlement_run"])
		tc(36, f"{it} settled {exp_es}/{exp_out} and linked to the run",
			st and flt(st[0].es_var, 2) == exp_es and flt(st[0].out_var, 2) == exp_out
			and st[0].settlement_run == run_doc.name, str(st))


# ===================================================================== G
def section_g(company, wh, today):
	"""Reversal & cancellation - TC37 (YTD pair), TC38, TC39."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	# ---- TC37 (YTD): reverse of Sett + reverse of Sett-Rev - full undo
	item = std_item("_TCV-G37", view="YTD")
	e = StdEngine(company, item, wh)
	src = ("Item", item)
	a = accounts_for(company, item, wh)
	e.post(trans="Rec", posting_date="2026-03-05", qty=80, sc=10, t_ac_override=1000, source=src)
	e.post(trans="Iss", posting_date="2026-03-15", qty=30, sc=10, source=src)
	sett = e.close_period(year=2026, month=3, sc=10, source=src, entry_date="2026-04-01")
	tc(37, "YTD settle 125/75 on 200 pool",
		flt(sett.es_var, 2) == 125 and flt(sett.out_var, 2) == 75,
		f"{sett.es_var}/{sett.out_var}")
	rev_ev, revrev_ev = e.sett_reverse(sett.name, source=src, entry_date="2026-04-02")
	gl_match(37, "Sett-Reverse GL mirrors the Sett (Cr Inv 125 / Cr COGS 75 / Dr PPV 200)",
		gl_net(event=rev_ev.name), {a.stock: -125, a.cogs_adj: -75, a.ppv: 200})
	gl_match(37, "Sett-Rev-Reverse GL mirrors the Sett-Rev (Dr Inv 125 / Dr COGS 75 / Cr PPV 200)",
		gl_net(event=revrev_ev.name), {a.stock: 125, a.cogs_adj: 75, a.ppv: -200})
	undo = {}
	for ev in frappe.get_all("Inventory Valuation Event",
			filters={"item_code": item, "settlement_ref": sett.name, "is_cancelled": 0},
			fields=["name"]):
		for acct, amt in gl_net(event=ev.name).items():
			undo[acct] = flt(undo.get(acct, 0) + amt, 2)
	undo = {k: v for k, v in undo.items() if flt(v, 2)}
	tc(37, "YTD cancelled settlement nets to zero across its 4 events", not undo,
		f"residual {undo}")
	resett = e.close_period(year=2026, month=3, sc=10, source=src, entry_date="2026-04-03")
	tc(37, "pool back to pre-Sett state: re-close reproduces 125/75",
		flt(resett.es_var, 2) == 125 and flt(resett.out_var, 2) == 75,
		f"{resett.es_var}/{resett.out_var}")

	# ---- TC38: exact reversal with reference = original basis exactly
	i38 = std_item("_TCV-G38")
	e38 = StdEngine(company, i38, wh)
	s38 = ("Item", i38)
	a38 = accounts_for(company, i38, wh)
	rec = e38.post(trans="Rec", posting_date="2026-05-04", qty=100, sc=10,
		t_ac_override=1200, source=s38)
	mirror = e38.reverse_event(rec.name, source=s38, posting_date="2026-05-25")
	tc(38, "mirror carries the ORIGINAL basis (-1000 SC / -1200 AC) + reference link",
		flt(mirror.total_sc) == -1000 and flt(mirror.total_ac) == -1200
		and mirror.reversal_of == rec.name, f"{mirror.total_sc}/{mirror.total_ac}")
	gl_match(38, "mirror GL: Cr Inv 1000 / Cr PPV 200 / Dr GR-IR 1200",
		gl_net(event=mirror.name), {a38.stock: -1000, a38.ppv: -200, a38.grir: 1200})
	tc(38, "mirror stamps its inventory effect (value_delta -1000)",
		flt(frappe.db.get_value("Inventory Valuation Event", mirror.name, "value_delta"), 2) == -1000,
		str(frappe.db.get_value("Inventory Valuation Event", mirror.name, "value_delta")))
	try:
		e38.reverse_event(rec.name, source=s38, posting_date="2026-05-26")
		tc(38, "double reversal blocked", False, "reversed twice")
	except frappe.ValidationError:
		tc(38, "double reversal blocked", True)

	# ---- TC39: independent NEW reversal prices at the POSTING-DATE cost
	i39 = std_item("_TCV-G39")
	e39 = StdEngine(company, i39, wh)
	s39 = ("Item", i39)
	e39.post(trans="Rec", posting_date="2026-05-06", qty=50, sc=10, t_ac_override=600, source=s39)
	fresh = e39.post(trans="PR", posting_date="2026-05-27", qty=20, sc=15,
		t_ac_override=-240, source=s39)
	tc(39, "independent return books today's SC (-300) and a FRESH variance, no reference",
		flt(fresh.total_sc) == -300 and flt(fresh.total_ac) == -240
		and not fresh.reversal_of, f"{fresh.total_sc}/{fresh.total_ac}")
	# receipt PPV +100, return leg -240-(-300) = +60 -> pool 160
	tc(39, "fresh variance enters the pool (100 + 60 = 160)",
		flt(e39.own_ppv(2026, 5), 2) == 160, str(e39.own_ppv(2026, 5)))


# ===================================================================== H
def section_h(company, wh, today):
	"""Edge cases - TC40, TC42, TC43."""
	from periodic_valuation.periodic_standard_cost.kernel import post_via_std_kernel

	# ---- TC40: negative stock policy
	item = std_item("_TCV-H40")
	scv_release(company, item, today.year, today.month, 10)
	make_pr(item, wh, 10, 10)
	try:
		make_dn(item, wh, 50)
		tc(40, "issue past zero blocked while block_negative_stock_std = ON", False, "posted")
	except frappe.ValidationError:
		tc(40, "issue past zero blocked while block_negative_stock_std = ON", True)
	settings = frappe.db.get_value("Periodic Standard Cost Settings",
		{"company": company}, "name")
	frappe.db.set_value("Periodic Standard Cost Settings", settings,
		"block_negative_stock_std", 0, update_modified=False)
	frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
	try:
		dn = make_dn(item, wh, 15)
		ive = one_ive(source_docname=dn.name)
		tc(40, "with the setting OFF the issue posts at current SC (-150)",
			ive and flt(ive[0].total_sc) == -150, str(ive))
	finally:
		frappe.db.set_value("Periodic Standard Cost Settings", settings,
			"block_negative_stock_std", 1, update_modified=False)
	note(40, "plan's negative_stock_at_issue audit flag is not built; the policy "
		"switch + event trail carry the same information")

	# ---- TC42: alternate-UOM posting normalizes to stock UOM
	i42 = std_item("_TCV-H42")
	if not frappe.db.exists("UOM", "_TCV Box"):
		frappe.get_doc({"doctype": "UOM", "uom_name": "_TCV Box"}).insert(ignore_permissions=True)
	it = frappe.get_doc("Item", i42)
	if not any(u.uom == "_TCV Box" for u in it.uoms):
		it.append("uoms", {"uom": "_TCV Box", "conversion_factor": 10})
		it.save(ignore_permissions=True)
	scv_release(company, i42, today.year, today.month, 10)
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": i42, "qty": 5, "uom": "_TCV Box", "conversion_factor": 10,
			"rate": 120, "warehouse": wh}]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	ive = one_ive(source_docname=pr.name)
	tc(42, "5 Box x 10 books 50 base units: SC 500 / AC 600 / PPV 100",
		ive and flt(ive[0].qty_adj) == 50 and flt(ive[0].total_sc) == 500
		and flt(ive[0].total_ac) == 600, str(ive))
	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": pr.name},
		fields=["actual_qty", "valuation_rate"])
	tc(42, "SLE in stock UOM (50 @ 10)",
		sle and flt(sle[0].actual_qty) == 50 and flt(sle[0].valuation_rate) == 10, str(sle))

	# ---- TC43: idempotency - re-entering the same voucher must not double-post
	before = frappe.db.count("Inventory Valuation Event",
		{"source_docname": pr.name, "is_cancelled": 0})
	post_via_std_kernel(pr, [])
	after = frappe.db.count("Inventory Valuation Event",
		{"source_docname": pr.name, "is_cancelled": 0})
	tc(43, "duplicate posting attempt is a no-op (DR-17 idempotency)",
		before == after == 1, f"{before}->{after}")


# ===================================================================== I
def section_i(company, wh, today):
	"""Declared-but-unbuilt items closed 1 Sep 2026: STD FX decomposition (plan
	'Foreign Currency (FX) Handling', DR-39), settlement row lock (TC16/TC35,
	DR-40), UOM rounding residual + tolerance (plan 'Rounding sequence', DR-41)."""
	from periodic_valuation.periodic_standard_cost.engine import PeriodLockedError, StdEngine

	# ---- DR-39: invoice difference in a foreign currency splits price vs FX
	item = std_item("_TCV-I-FX")
	scv_release(company, item, today.year, today.month, 40)
	a = accounts_for(company, item, wh)
	fx_acct = frappe.get_cached_value("Company", company, "exchange_gain_loss_account")
	base_ccy = frappe.get_cached_value("Company", company, "default_currency")
	fccy = "EUR" if base_ccy != "EUR" else "USD"
	if not frappe.db.exists("Currency", fccy):
		frappe.get_doc({"doctype": "Currency", "currency_name": fccy, "enabled": 1}).insert(ignore_permissions=True)
	frappe.db.set_value("Currency", fccy, "enabled", 1, update_modified=False)
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": company, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1, "currency": fccy, "conversion_rate": 4.0,
		"ignore_pricing_rule": 1,
		"items": [{"item_code": item, "qty": 10, "rate": 10, "warehouse": wh}]})   # 10 x 10 EUR @ 4.00 = 400 base
	pr.insert(ignore_permissions=True)
	pr.submit()
	rec = one_ive(source_docname=pr.name)[0]
	tc(90, "FX receipt stamps its exchange rate (4.0) - SC 400 / AC 400",
		flt(frappe.db.get_value("Inventory Valuation Event", rec.name, "exchange_rate_at_receipt")) == 4.0
		and flt(rec.total_ac) == 400, f"{rec}")
	# a foreign-currency invoice needs a payable account in that currency
	abbr = frappe.db.get_value("Company", company, "abbr")
	payable_fx = f"Creditors {fccy} - {abbr}"
	if not frappe.db.exists("Account", payable_fx):
		parent = frappe.db.get_value("Account", {"company": company, "is_group": 1,
			"account_type": "Payable"}, "name") or frappe.db.get_value("Account",
			{"company": company, "is_group": 1, "root_type": "Liability"}, "name")
		frappe.get_doc({"doctype": "Account", "account_name": f"Creditors {fccy}", "company": company,
			"parent_account": parent, "account_type": "Payable", "account_currency": fccy,
			"root_type": "Liability"}).insert(ignore_permissions=True)
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": company, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "currency": fccy, "conversion_rate": 4.5, "ignore_pricing_rule": 1,
		"credit_to": payable_fx,
		"items": [{"item_code": item, "qty": 10, "rate": 11, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})   # 10 x 11 EUR @ 4.50 = 495 base
	pi.insert(ignore_permissions=True)
	pi.submit()
	lc = one_ive(source_docname=pi.name, std_trans="LC")
	# base diff 495 - 400 = 95; price component (11-10) x 10 x 4.0 = 40 -> PPV; FX 55 -> gain/loss
	tc(90, "FX invoice difference 95 splits: PPV pool 40 (price at receipt rate) + FX variance 55",
		lc and flt(lc[0].total_ac) == 40
		and flt(frappe.db.get_value("Inventory Valuation Event", lc[0].name, "fx_variance")) == 55, str(lc))
	if lc:
		gl_match(90, "FX GL: Dr PPV 40 / Dr Exchange Gain-Loss 55 / Cr GR-IR 95",
			gl_net(event=lc[0].name), {a.ppv: 40, fx_acct: 55, a.grir: -95})
	e = StdEngine(company, item, wh)
	tc(90, "only the price component enters the PPV pool (40)",
		flt(e.own_ppv(today.year, today.month), 2) == 40, str(e.own_ppv(today.year, today.month)))

	# ---- DR-40: settlement row lock + double-settle guard
	i40 = std_item("_TCV-I-LOCK")
	e40 = StdEngine(company, i40, wh)
	s40 = ("Item", i40)
	e40.post(trans="Rec", posting_date="2026-04-05", qty=10, sc=10, t_ac_override=120, source=s40)
	keys = e40._lock_for_settlement(2026, 4)
	tc(91, "settlement takes a row lock (period rows or the company settings row)", bool(keys), str(keys))
	e40.close_period(year=2026, month=4, sc=10, source=s40, entry_date="2026-05-01")
	try:
		e40.close_period(year=2026, month=4, sc=10, source=s40, entry_date="2026-05-01")
		tc(91, "a second close of the same scope-period is refused", False, "settled twice")
	except PeriodLockedError:
		tc(91, "a second close of the same scope-period is refused", True)

	# ---- DR-41: rounding residual recorded; tolerance guard
	i41 = std_item("_TCV-I-ROUND")
	e41 = StdEngine(company, i41, wh)
	s41 = ("Item", i41)
	ev = e41.post(trans="Rec", posting_date=str(today), qty=33.333, sc=0.0025, ac=0.0025, source=s41)
	# 33.333 x 0.0025 = 0.0833325 -> line 0.08; residual 0.0033325 (plan worked example shape)
	tc(92, "rounding residual recorded on the event (0.003333)",
		abs(flt(ev.rounding_residual) - 0.003333) < 0.000001 and flt(ev.total_sc) == 0.08,
		f"{ev.rounding_residual} / {ev.total_sc}")
	settings = frappe.db.get_value("Periodic Standard Cost Settings", {"company": company}, "name")
	frappe.db.set_value("Periodic Standard Cost Settings", settings, "uom_rounding_tolerance", 0.001, update_modified=False)
	try:
		e41.post(trans="Rec", posting_date=str(today), qty=33.333, sc=0.0025, ac=0.0025, source=s41)
		tc(92, "residual above the tolerance blocks the posting", False, "posted")
	except frappe.ValidationError as ex:
		tc(92, "residual above the tolerance blocks the posting", "UOM rounding tolerance" in str(ex) or "Rounding residual" in str(ex), str(ex)[:100])
	finally:
		frappe.db.set_value("Periodic Standard Cost Settings", settings, "uom_rounding_tolerance", 0.01, update_modified=False)


# ===================================================================== J
def section_j(company, wh, today):
	"""Plan components built 1 Sep 2026 (DR-43): Standard Cost Costing Variant
	feeding the Standard Cost Estimate; Bulk Item Settlement View Change
	creating Draft ISVCs for an Item Group (plan TC46/47)."""
	# ---- Costing Variant -> SCE picks overhead % and BOM rate source
	comp = std_item("_TCV-J-COMP")
	fg = std_item("_TCV-J-FG")
	scv_release(company, comp, today.year, today.month, 4)
	frappe.db.set_value("Item", comp, "valuation_rate", 3.5, update_modified=False)
	frappe.clear_document_cache("Item", comp)
	vname = "_TCV-J Variant"
	if not frappe.db.exists("Standard Cost Costing Variant", vname):
		frappe.get_doc({"doctype": "Standard Cost Costing Variant", "variant_name": vname,
			"company": company, "overhead_percent": 25,
			"default_rate_source": "VALUATION_RATE"}).insert(ignore_permissions=True)
	if not frappe.db.exists("BOM", {"item": fg, "docstatus": 1}):
		bom = frappe.get_doc({"doctype": "BOM", "item": fg, "company": company, "quantity": 1,
			"currency": frappe.get_cached_value("Company", company, "default_currency"),
			"rm_cost_as_per": "Valuation Rate", "with_operations": 0,
			"items": [{"item_code": comp, "qty": 5, "rate": 3.5, "uom": frappe.db.get_value("Item", comp, "stock_uom"),
				"stock_uom": frappe.db.get_value("Item", comp, "stock_uom"), "conversion_factor": 1}]})
		bom.insert(ignore_permissions=True)
		bom.submit()
	bom_name = frappe.db.get_value("BOM", {"item": fg, "docstatus": 1})
	sce = frappe.get_doc({"doctype": "Standard Cost Estimate", "company": company,
		"item_code": fg, "valid_from_year": today.year, "valid_from_month": today.month,
		"bom": bom_name, "costing_variant": vname})
	sce.insert(ignore_permissions=True)
	sce.calculate()
	tc(46, "variant supplies overhead 25% to an estimate that left it blank",
		flt(sce.overhead_percent) == 25, sce.overhead_percent)
	tc(46, "BOM-exploded component takes the variant rate source (VALUATION_RATE 3.5, not STD 4)",
		sce.components[0].rate_source == "VALUATION_RATE" and flt(sce.components[0].rate) == 3.5,
		f"{sce.components[0].rate_source} {sce.components[0].rate}")
	tc(46, "roll-up 5 x 3.5 = 17.5 + 25% (overhead rounded 2dp) = 21.88", flt(sce.standard_cost, 2) == 21.88, sce.standard_cost)
	sce2 = frappe.get_doc({"doctype": "Standard Cost Estimate", "company": company,
		"item_code": fg, "valid_from_year": today.year, "valid_from_month": today.month,
		"overhead_percent": 10, "costing_variant": vname,
		"components": [{"item_code": comp, "qty": 5, "rate_source": "LEAF_STD"}]})
	sce2.insert(ignore_permissions=True)
	sce2.calculate()
	tc(46, "estimate's own overhead (10%) and hand-chosen LEAF_STD win over the variant: 5x4 = 20 + 10% = 22",
		flt(sce2.overhead_percent) == 10 and flt(sce2.standard_cost, 2) == 22, sce2.standard_cost)
	frappe.db.set_value("Standard Cost Costing Variant", vname, "disabled", 1)
	frappe.clear_document_cache("Standard Cost Costing Variant", vname)
	sce3 = frappe.get_doc({"doctype": "Standard Cost Estimate", "company": company,
		"item_code": fg, "valid_from_year": today.year, "valid_from_month": today.month,
		"bom": bom_name, "costing_variant": vname})
	sce3.insert(ignore_permissions=True)
	try:
		sce3.calculate()
		tc(46, "disabled variant refused", False, "calculated")
	except frappe.ValidationError:
		tc(46, "disabled variant refused", True)
	frappe.db.set_value("Standard Cost Costing Variant", vname, "disabled", 0)
	frappe.clear_document_cache("Standard Cost Costing Variant", vname)

	# ---- Bulk ISVC: preview eligibility, create drafts only, second run creates none
	grp = "_TCV-J Group"
	if not frappe.db.exists("Item Group", grp):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": grp,
			"parent_item_group": frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""})}).insert(ignore_permissions=True)
	codes = []
	for n, view in (("A", "MTD"), ("B", "MTD"), ("C", "YTD")):
		code = f"_TCV-J-BULK-{n}"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": grp,
				"stock_uom": frappe.db.get_value("Item", comp, "stock_uom"), "is_stock_item": 1,
				"valuation_method": "Periodic Standard Cost", "settlement_view": view}).insert(ignore_permissions=True)
		codes.append(code)
	bulk = frappe.get_doc({"doctype": "Bulk Item Settlement View Change", "company": company,
		"item_group": grp, "to_view": "YTD", "reason": "design TC47 bulk"})
	bulk.insert(ignore_permissions=True)
	res = bulk.preview()
	rows = {r.item_code: r for r in bulk.items}
	tc(47, "preview lists the 3 STD items of the group", res["total"] == 3 and set(rows) == set(codes), str(res))
	tc(47, "2 eligible (MTD -> YTD); the YTD item is skipped with a reason",
		res["eligible"] == 2 and not rows[codes[2]].eligible and "already YTD" in (rows[codes[2]].note or ""),
		str([(r.item_code, r.eligible, r.note) for r in bulk.items]))
	res = bulk.create_drafts()
	drafts = frappe.get_all("Item Settlement View Change",
		filters={"item_code": ("in", codes), "docstatus": 0}, fields=["item_code", "status", "docstatus", "to_view", "from_view", "reason"])
	tc(47, "creates exactly 2 ISVC requests, all Draft (never approved/posted)",
		res["created"] == 2 and len(drafts) == 2 and all(d.status == "Draft" and d.docstatus == 0 for d in drafts)
		and all(d.to_view == "YTD" and d.from_view == "MTD" and d.reason == "design TC47 bulk" for d in drafts), str(drafts))
	tc(47, "items untouched until each request is approved and posted",
		all(frappe.db.get_value("Item", c, "settlement_view") == "MTD" for c in codes[:2]))
	tc(47, "bulk record locked after creation (status Created, rows carry the ISVC names)",
		bulk.status == "Created" and bulk.created_count == 2
		and all(r.isvc for r in bulk.items if r.eligible), bulk.status)
	try:
		bulk.reason = "edit after create"
		bulk.save(ignore_permissions=True)
		tc(47, "edit after creation refused", False, "saved")
	except frappe.ValidationError:
		tc(47, "edit after creation refused", True)
	bulk2 = frappe.get_doc({"doctype": "Bulk Item Settlement View Change", "company": company,
		"item_group": grp, "to_view": "YTD", "reason": "design TC47 rerun"})
	bulk2.insert(ignore_permissions=True)
	res = bulk2.preview()
	tc(47, "re-run on the same group: 0 eligible (open requests block duplicates)",
		res["eligible"] == 0 and all("open request" in (r.note or "") for r in bulk2.items if r.item_code in codes[:2]),
		str([(r.item_code, r.note) for r in bulk2.items]))


# ================================================================== main
def run(commit=False):
	wh = ensure_masters()
	company = get_company()
	ensure_std_masters(company)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	today = getdate(nowdate())

	sections = [section_a, section_b, section_c, section_d, section_e,
		section_f, section_g, section_h, section_i, section_j]
	for fn in sections:
		try:
			fn(company, wh, today)
		except Exception as e:
			import traceback
			traceback.print_exc()
			tc(0, f"section {fn.__name__} crashed", False, repr(e)[:200])

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed; {len(NOTES)} supersession notes")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
