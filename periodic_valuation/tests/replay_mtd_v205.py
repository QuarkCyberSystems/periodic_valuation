# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Replay of the client workbook `01 MTD STD Cost V2.05.xlsx` through REAL
ERPNext vouchers - bench --site <site> execute periodic_valuation.tests.replay_mtd_v205.run

The workbook's event log (rows 1-25, Feb-Apr 2026) is walked in ENTRY-date
order with the system clock patched to each row's Ent Date, so backdate
labels (BD), companions, and settlement dating behave exactly as they would
have on the day. Everything system-generated in the workbook (Rev triplets,
(BD) - Rev companions, Sett / Sett - Rev) is expected to be produced by the
kernel, never posted directly.

Anchors compared at the end:
- every IVE's (trans, total_sc, total_ac) vs the workbook event log
- period pools: Feb PPV 320 @ settle; Mar PPV 1205.4737 / Rev -880;
  Apr PPV 400 / Rev -1208
- Feb settlement: ES Var 269.4737 / Out Var 50.5263
- final state: qty 156, value 3900, SC 25
- GL totals per account group vs the workbook GL table

Rolled back unless commit=True.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

CHECKS = []
ITEM = "_MTD-V205"
COMPANY = "MTD Replay Co"
ABBR = "MRC"


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


# --------------------------------------------------------------- clock patch
class Clock:
	"""Patches frappe.utils.nowdate/today so kernel 'today' = workbook Ent Date."""

	def __init__(self):
		self._orig_nowdate = frappe.utils.nowdate
		self._orig_today = frappe.utils.today
		self.current = None

	def set(self, date_str):
		self.current = date_str
		frappe.utils.nowdate = lambda: self.current
		frappe.utils.today = lambda: self.current

	def restore(self):
		frappe.utils.nowdate = self._orig_nowdate
		frappe.utils.today = self._orig_today


# --------------------------------------------------------------- masters
def acc(hint, root="Expense"):
	rows = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0,
		"account_name": ("like", f"%{hint}%")}, limit=1, pluck="name")
	return rows[0] if rows else frappe.get_all("Account",
		filters={"company": COMPANY, "is_group": 0, "root_type": root},
		limit=1, pluck="name")[0]


def ensure_replay_company():
	if not frappe.db.exists("Company", COMPANY):
		base = frappe.get_all("Company", limit=1, fields=["default_currency", "country"])[0]
		frappe.get_doc({
			"doctype": "Company", "company_name": COMPANY, "abbr": ABBR,
			"default_currency": base.default_currency, "country": base.country,
			"create_chart_of_accounts_based_on": "Standard Template",
		}).insert(ignore_permissions=True)

	def make_acc(name, root):
		full = f"{name} - {ABBR}"
		if not frappe.db.exists("Account", full):
			parent = frappe.get_all("Account", filters={"company": COMPANY,
				"is_group": 1, "root_type": root}, limit=1, pluck="name")[0]
			frappe.get_doc({"doctype": "Account", "account_name": name,
				"company": COMPANY, "parent_account": parent,
				"root_type": root}).insert(ignore_permissions=True)
		return full

	if not frappe.db.exists("Periodic Standard Cost Settings", {"company": COMPANY}):
		frappe.get_doc({
			"doctype": "Periodic Standard Cost Settings", "company": COMPANY,
			"ppv_account": make_acc("Replay PPV", "Expense"),
			"std_reval_reserve_account": make_acc("Replay STD Reserve", "Liability"),
			"cogs_adjustment_account": make_acc("Replay COGS Adjustment", "Expense"),
			"customer_cogs_account": make_acc("Replay Customer COGS", "Expense"),
			"fy_carry_forward_account": make_acc("Replay FY Carry", "Liability"),
			"default_settlement_view": "MTD",
		}).insert(ignore_permissions=True)

	wh = f"Stores - {ABBR}"
	if not frappe.db.exists("Warehouse", wh):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores",
			"company": COMPANY}).insert(ignore_permissions=True)

	for dt, name, extra in (
		("Supplier", "_Replay Supplier", {"supplier_group": frappe.get_all("Supplier Group", limit=1, pluck="name")[0]}),
		("Customer", "_Replay Customer", {"customer_group": frappe.get_all("Customer Group", limit=1, pluck="name")[0],
			"territory": frappe.get_all("Territory", limit=1, pluck="name")[0]}),
	):
		if not frappe.db.exists(dt, name):
			frappe.get_doc({"doctype": dt, dt.lower() + "_name": name, **extra}).insert(ignore_permissions=True)

	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)
	return wh


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


def advance_period(year, month):
	"""Open (year, month); previous OPEN -> PREV_OPEN; older PREV_OPEN -> FROZEN."""
	for p in frappe.get_all("Inventory Period",
			filters={"company": COMPANY, "status": "PREV_OPEN_UNSETTLED"}, pluck="name"):
		frappe.db.set_value("Inventory Period", p, "status", "SETTLED_FROZEN", update_modified=False)
	for p in frappe.get_all("Inventory Period",
			filters={"company": COMPANY, "status": "OPEN"}, pluck="name"):
		frappe.db.set_value("Inventory Period", p, "status", "PREV_OPEN_UNSETTLED", update_modified=False)
	make_period(year, month, "OPEN")


# --------------------------------------------------------------- vouchers
def make_pr(wh, qty, rate, posting_date, is_return=False, return_against=None):
	doc = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_Replay Supplier",
		"posting_date": posting_date, "set_posting_time": 1,
		"is_return": 1 if is_return else 0, "return_against": return_against,
		"items": [{"item_code": ITEM, "warehouse": wh, "qty": qty, "rate": rate}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def make_dn(wh, qty, posting_date, is_return=False, return_against=None):
	doc = frappe.get_doc({
		"doctype": "Delivery Note", "company": COMPANY, "customer": "_Replay Customer",
		"posting_date": posting_date, "set_posting_time": 1,
		"is_return": 1 if is_return else 0, "return_against": return_against,
		"items": [{"item_code": ITEM, "warehouse": wh, "qty": qty, "rate": 100}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def make_lcv(pr, amount, posting_date):
	doc = frappe.get_doc({
		"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": posting_date,
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name, "supplier": pr.supplier,
			"grand_total": pr.grand_total}],
		"taxes": [{"expense_account": acc("Freight"), "description": "Freight",
			"amount": amount}],
		"distribute_charges_based_on": "Amount",
	})
	doc.get_items_from_purchase_receipts()
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def make_count(wh, counted, posting_date):
	doc = frappe.get_doc({
		"doctype": "Stock Count", "company": COMPANY, "posting_date": posting_date,
		"items": [{"item_code": ITEM, "warehouse": wh, "counted_qty": counted}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def release_scv(sc, year, month):
	doc = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": COMPANY,
		"item_code": ITEM, "valid_from_year": year, "valid_from_month": month,
		"standard_cost": sc, "source_type": "MANUAL_OVERRIDE"})
	doc.insert(ignore_permissions=True)
	doc.release()
	return doc


def run_settlement(year, month):
	doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run",
		"company": COMPANY, "period_year": year, "period_month": month,
		"run_type": "INITIAL_CLOSE"})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


# --------------------------------------------------------------- verification
def ive_rows():
	return frappe.get_all("Inventory Valuation Event",
		filters={"company": COMPANY, "item_code": ITEM, "is_cancelled": 0},
		fields=["std_trans", "posting_date", "period_month", "total_sc", "total_ac", "qty_adj"],
		order_by="creation asc")


# workbook event log: (row#, trans, period_month, total_sc, total_ac)
WB_EVENTS = [
	(1, "Rec", 2, 1200, 1000), (2, "Iss", 2, -360, 0), (3, "Rec", 2, 600, 1000),
	(4, "Iss", 3, -240, 0), (5, "LC", 3, 0, 200), (6, "SC-", 3, -60, 0),
	(7, "Rec", 3, 192, 400),
	(8, "Rev Beg", 3, 600, 0), (9, "REV In", 3, 80, 0), (10, "REV out", 3, -125, 0),
	(11, "LC", 3, 0, 678), (12, "SR", 3, 170, 170),
	(13, "REC (BD)", 2, 480, 600), (14, "REC (BD) - Rev", 3, 200, 0),
	(15, "PR", 3, -850, -1000),
	(16, "Sett", 2, 269.4737, 0), (17, "Sett - Rev", 3, -269.4737, 0),
	(18, "Rec", 4, 850, 1250), (19, "Iss", 4, -340, 0),
	(20, "Rev Beg", 4, 888, 0), (21, "REV In", 4, 400, 0), (22, "REV out", 4, -160, 0),
	(23, "Issue (BD)", 3, -170, 0), (24, "Issue (BD) - Rev", 4, -80, 0),
	(25, "SC+", 4, 625, 0),
]

# workbook GL totals by account group (r25's client-flagged spurious PPV 170 excluded)
WB_GL = {
	"Inventory": 3900.0,
	"GR/IR+Freight": -4128.0,
	"COGS group": 1355.5263,
	"PPV": 1605.4737,
	"Reserve": -2168.0,
	"Stock Adj": -565.0,
}


def compare_gl(wh):
	from periodic_valuation.shared.accounts import get_inventory_account

	inv = get_inventory_account(COMPANY, ITEM, wh)
	srbnb = frappe.get_cached_value("Company", COMPANY, "stock_received_but_not_billed")
	st = frappe.get_doc("Periodic Standard Cost Settings", {"company": COMPANY})
	freight = acc("Freight")
	groups = {
		"Inventory": {inv},
		"GR/IR+Freight": {srbnb, freight},
		"COGS group": {st.cogs_adjustment_account, st.customer_cogs_account,
			frappe.get_cached_value("Company", COMPANY, "default_expense_account") or "",
			acc("Cost of Goods Sold")},
		"PPV": {st.ppv_account},
		"Reserve": {st.std_reval_reserve_account},
		"Stock Adj": {acc("Stock Adjustment")},
	}

	gl = frappe.get_all("GL Entry", filters={"company": COMPANY, "is_cancelled": 0},
		fields=["account", "debit", "credit"])
	totals = {}
	for g in gl:
		totals[g.account] = totals.get(g.account, 0.0) + flt(g.debit) - flt(g.credit)

	print("\n--- GL account groups: replay vs workbook ---")
	assigned = set()
	for label, accounts in groups.items():
		actual = flt(sum(totals.get(a, 0.0) for a in accounts if a), 4)
		assigned |= {a for a in accounts if a}
		expected = flt(WB_GL[label], 4)
		check(f"GL {label}: {expected}", abs(actual - expected) <= 0.01,
			f"actual {actual}")
	other = flt(sum(v for a, v in totals.items() if a not in assigned), 4)
	check("GL all other accounts net 0", abs(other) <= 0.01, f"net {other}")


def run(commit=False):
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	frappe.db.set_single_value("Selling Settings", "maintain_same_sales_rate", 0)

	clock = Clock()
	try:
		wh = ensure_replay_company()
		from periodic_valuation.periodic_standard_cost.engine import StdEngine

		# ---- setup in workbook time: SC 12 valid Feb 2026
		clock.set("2026-02-01")
		make_period(2026, 2, "OPEN")
		release_scv(12, 2026, 2)

		# ---- the 18 user actions, in Ent Date order
		clock.set("2026-02-05"); pr1 = make_pr(wh, 100, 10, "2026-02-05")          # 1
		clock.set("2026-02-15"); make_dn(wh, 30, "2026-02-15")                     # 2
		clock.set("2026-02-25"); pr3 = make_pr(wh, 50, 20, "2026-02-25")           # 3
		clock.set("2026-03-05"); advance_period(2026, 3)
		dn4 = make_dn(wh, 20, "2026-03-05")                                        # 4
		clock.set("2026-03-15"); make_lcv(pr3, 200, "2026-03-15")                  # 5
		make_count(wh, 95, "2026-03-15")                                           # 6
		clock.set("2026-03-18"); pr7 = make_pr(wh, 16, 25, "2026-03-18")           # 7
		clock.set("2026-03-20"); release_scv(17, 2026, 3)                          # 8 (triplet)
		clock.set("2026-03-21"); make_lcv(pr7, 678, "2026-03-21")                  # 9
		clock.set("2026-03-25"); make_dn(wh, -10, "2026-03-25", is_return=True,
			return_against=dn4.name)                                               # 10 SR
		clock.set("2026-03-28")
		make_pr(wh, 40, 15, "2026-02-20")                                          # 11 REC (BD)+companion
		# wb row 15 is Ent Mar 28 / Pst Mar 30 (future-dated) - ERPNext refuses
		# future posting dates, so we enter it on its posting day (same month,
		# numerically identical)
		clock.set("2026-03-30")
		make_pr(wh, -50, 20, "2026-03-30", is_return=True, return_against=pr3.name)  # 12 PR
		clock.set("2026-03-28")
		run_settlement(2026, 2)                                                    # 13 Sett+Sett-Rev
		clock.set("2026-04-05"); advance_period(2026, 4)
		make_pr(wh, 50, 25, "2026-04-03")                                          # 14
		clock.set("2026-04-07"); make_dn(wh, 20, "2026-04-05")                     # 15
		clock.set("2026-04-10"); release_scv(25, 2026, 4)                          # 16 (triplet)
		clock.set("2026-04-11"); make_dn(wh, 10, "2026-03-25")                     # 17 Issue (BD)+companion
		clock.set("2026-04-15"); make_count(wh, 156, "2026-04-15")                 # 18
	finally:
		clock.restore()

	# ---------------- event-by-event comparison
	print("\n--- event log: replay vs workbook ---")
	actual = ive_rows()
	for i, (row, trans, month, t_sc, t_ac) in enumerate(WB_EVENTS):
		if i >= len(actual):
			check(f"wb#{row} {trans}", False, "missing event")
			continue
		a = actual[i]
		ok = (a.std_trans == trans and a.period_month == month
			and abs(flt(a.total_sc) - t_sc) <= 0.01 and abs(flt(a.total_ac) - t_ac) <= 0.01)
		check(f"wb#{row} {trans} P{month} SC {t_sc} AC {t_ac}", ok,
			f"got {a.std_trans} P{a.period_month} SC {a.total_sc} AC {a.total_ac}")
	if len(actual) != len(WB_EVENTS):
		check(f"event count {len(WB_EVENTS)}", False,
			f"got {len(actual)}: extra {[x.std_trans for x in actual[len(WB_EVENTS):]]}")

	# ---------------- pools, settlement, final state
	engine = StdEngine(COMPANY, ITEM, wh)
	sett = frappe.get_all("Inventory Period Settlement",
		filters={"company": COMPANY, "item_code": ITEM, "period_year": 2026,
			"period_month": 2, "cancelled": 0},
		fields=["es_var", "out_var", "ppv_pool", "es_qty", "out_qty"])
	print("\n--- anchors ---")
	check("Feb settlement ES Var 269.4737", sett and abs(flt(sett[0].es_var) - 269.4737) <= 0.01,
		str(sett))
	check("Feb settlement Out Var 50.5263", sett and abs(flt(sett[0].out_var) - 50.5263) <= 0.01,
		sett and str(sett[0].out_var))
	check("Feb pool 320 / ES qty 160 / out 30",
		sett and abs(flt(sett[0].ppv_pool) - 320) <= 0.01
		and flt(sett[0].es_qty) == 160 and flt(sett[0].out_qty) == 30,
		str(sett))
	check("Mar pool PPV 1205.4737", abs(flt(engine.pool_ppv(2026, 3)) - 1205.4737) <= 0.01,
		str(engine.pool_ppv(2026, 3)))
	check("Mar pool Rev -880", abs(flt(engine.pool_rev(2026, 3)) - (-880)) <= 0.01,
		str(engine.pool_rev(2026, 3)))
	check("Apr pool PPV 400", abs(flt(engine.pool_ppv(2026, 4)) - 400) <= 0.01,
		str(engine.pool_ppv(2026, 4)))
	check("Apr pool Rev -1208", abs(flt(engine.pool_rev(2026, 4)) - (-1208)) <= 0.01,
		str(engine.pool_rev(2026, 4)))

	end_qty = engine.end_qty_mtd(2026, 4)
	ipb = frappe.get_all("Inventory Period Balance",
		filters={"company": COMPANY, "item_code": ITEM, "period_year": 2026, "period_month": 4},
		fields=["closing_qty", "closing_value", "period_standard_cost"])
	check("final qty 156", flt(end_qty) == 156, str(end_qty))
	check("final IPB 156 / 3900 @ SC 25",
		ipb and flt(ipb[0].closing_qty) == 156 and abs(flt(ipb[0].closing_value) - 3900) <= 0.01
		and flt(ipb[0].period_standard_cost) == 25, str(ipb))

	compare_gl(wh)

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
