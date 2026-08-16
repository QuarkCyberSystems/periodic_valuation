# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Replay of the client YTD workbooks through REAL ERPNext vouchers —
bench --site <site> execute periodic_valuation.tests.replay_ytd_v203.run

Scenario A: `02 YTD STD Cost V2.03.xlsx` (base walkthrough, Feb-Jun 2026)
 - includes the deliberately-WRONG March settlement that the client reverses
   and re-closes (the workbook's own check column flags v1 as imbalanced, so
   its value is not anchored; the re-close 5070.8026 is),
 - and the post-reopen backdated receipt that must post PLAIN `Rec` (DR-09).
Scenario B: `02 YTD ... year_transfer.xlsx` (Dec 2025 - Mar 2026)
 - cross-FY BY entries, Dec settlement with inventory-share-only carry to
   Jan 1, sequential Jan/Feb settlements, backdated Feb count.
The issue-backdated variant's distinctive rows are identical to Scenario A
rows 9-11 (1000 / 830 / -275) and are covered there.

Each scenario runs in its own company with the clock patched to Ent Dates.
Rolled back unless commit=True.
"""

import frappe
from frappe.utils import flt

CHECKS = []


def _fy_covered(fy):
	"""A differently-named Fiscal Year may already span this year on a shared
	site (e.g. `_Test Fiscal Year 2025` from framework fixtures). Creating our
	own would overlap and be rejected, so reuse whatever is already there."""
	return bool(frappe.db.exists("Fiscal Year", {
		"year_start_date": ("<=", f"{fy}-01-01"),
		"year_end_date": (">=", f"{fy}-12-31"),
	}))

def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))


class Clock:
	def __init__(self):
		self._nowdate, self._today = frappe.utils.nowdate, frappe.utils.today
		self.current = None

	def set(self, d):
		self.current = d
		frappe.utils.nowdate = lambda: self.current
		frappe.utils.today = lambda: self.current

	def restore(self):
		frappe.utils.nowdate, frappe.utils.today = self._nowdate, self._today


class Co:
	"""Per-scenario isolated company + masters + voucher makers."""

	def __init__(self, name, abbr, item, view="YTD", method="Periodic Standard Cost"):
		self.name, self.abbr, self.item = name, abbr, item
		if not frappe.db.exists("Company", name):
			base = frappe.get_all("Company", filters={"company_name": ("not like", "%Replay%")},
				limit=1, fields=["default_currency", "country"])[0]
			frappe.get_doc({"doctype": "Company", "company_name": name, "abbr": abbr,
				"default_currency": base.default_currency, "country": base.country,
				"create_chart_of_accounts_based_on": "Standard Template",
			}).insert(ignore_permissions=True)
		for fy in ("2025", "2026"):
			if not frappe.db.exists("Fiscal Year", fy) and not _fy_covered(fy):
				frappe.get_doc({"doctype": "Fiscal Year", "year": fy,
					"year_start_date": f"{fy}-01-01", "year_end_date": f"{fy}-12-31",
				}).insert(ignore_permissions=True)
			if not frappe.db.exists("Fiscal Year Company", {"parent": fy, "company": name}):
				frappe.get_doc({"doctype": "Fiscal Year Company", "parent": fy,
					"parenttype": "Fiscal Year", "parentfield": "companies",
					"company": name}).insert(ignore_permissions=True)
			frappe.cache.delete_value("fiscal_years")
		self.wh = f"Stores - {abbr}"
		if not frappe.db.exists("Warehouse", self.wh):
			frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores",
				"company": name}).insert(ignore_permissions=True)

		def make_acc(nm, root):
			full = f"{nm} - {abbr}"
			if not frappe.db.exists("Account", full):
				parent = frappe.get_all("Account", filters={"company": name,
					"is_group": 1, "root_type": root}, limit=1, pluck="name")[0]
				frappe.get_doc({"doctype": "Account", "account_name": nm, "company": name,
					"parent_account": parent, "root_type": root}).insert(ignore_permissions=True)
			return full

		if not frappe.db.exists("Periodic Standard Cost Settings", {"company": name}):
			frappe.get_doc({
				"doctype": "Periodic Standard Cost Settings", "company": name,
				"ppv_account": make_acc("Replay PPV", "Expense"),
				"std_reval_reserve_account": make_acc("Replay STD Reserve", "Liability"),
				"cogs_adjustment_account": make_acc("Replay COGS Adjustment", "Expense"),
				"customer_cogs_account": make_acc("Replay Customer COGS", "Expense"),
				"fy_carry_forward_account": make_acc("Replay FY Carry", "Liability"),
				"default_settlement_view": "MTD",
			}).insert(ignore_permissions=True)

		for dt, nm, extra in (
			("Supplier", "_Replay Supplier", {"supplier_group": frappe.get_all("Supplier Group", limit=1, pluck="name")[0]}),
			("Customer", "_Replay Customer", {"customer_group": frappe.get_all("Customer Group", limit=1, pluck="name")[0],
				"territory": frappe.get_all("Territory", limit=1, pluck="name")[0]}),
		):
			if not frappe.db.exists(dt, nm):
				frappe.get_doc({"doctype": dt, dt.lower() + "_name": nm, **extra}).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", item):
			frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item,
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
				"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
				"is_stock_item": 1, "valuation_method": method,
				**({"settlement_view": view} if method == "Periodic Standard Cost" else {}),
			}).insert(ignore_permissions=True)

	def period(self, year, month, status="OPEN"):
		name = frappe.db.get_value("Inventory Period",
			{"company": self.name, "period_year": year, "period_month": month})
		if not name:
			doc = frappe.get_doc({"doctype": "Inventory Period", "company": self.name,
				"start_date": f"{year}-{month:02d}-01", "status": status})
			doc.flags.ignore_validate = True
			doc.insert(ignore_permissions=True)
			name = doc.name
		frappe.db.set_value("Inventory Period", name, "status", status, update_modified=False)

	def advance(self, year, month):
		for p in frappe.get_all("Inventory Period",
				filters={"company": self.name, "status": "PREV_OPEN_UNSETTLED"}, pluck="name"):
			frappe.db.set_value("Inventory Period", p, "status", "SETTLED_FROZEN", update_modified=False)
		for p in frappe.get_all("Inventory Period",
				filters={"company": self.name, "status": "OPEN"}, pluck="name"):
			frappe.db.set_value("Inventory Period", p, "status", "PREV_OPEN_UNSETTLED", update_modified=False)
		self.period(year, month, "OPEN")

	def pr(self, qty, rate, posting_date, is_return=False, against=None):
		doc = frappe.get_doc({"doctype": "Purchase Receipt", "company": self.name,
			"supplier": "_Replay Supplier", "posting_date": posting_date, "set_posting_time": 1,
			"is_return": 1 if is_return else 0, "return_against": against,
			"items": [{"item_code": self.item, "warehouse": self.wh, "qty": qty, "rate": rate}]})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def dn(self, qty, posting_date, is_return=False, against=None):
		doc = frappe.get_doc({"doctype": "Delivery Note", "company": self.name,
			"customer": "_Replay Customer", "posting_date": posting_date, "set_posting_time": 1,
			"is_return": 1 if is_return else 0, "return_against": against,
			"items": [{"item_code": self.item, "warehouse": self.wh, "qty": qty, "rate": 100}]})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def lcv(self, pr_doc, amount, posting_date):
		freight = frappe.get_all("Account", filters={"company": self.name, "is_group": 0,
			"account_name": ("like", "%Freight%")}, limit=1, pluck="name")
		doc = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": self.name,
			"posting_date": posting_date,
			"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
				"receipt_document": pr_doc.name, "supplier": pr_doc.supplier,
				"grand_total": pr_doc.grand_total}],
			"taxes": [{"expense_account": freight[0], "description": "Freight", "amount": amount}],
			"distribute_charges_based_on": "Amount"})
		doc.get_items_from_purchase_receipts()
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def count(self, counted, posting_date):
		doc = frappe.get_doc({"doctype": "Stock Count", "company": self.name,
			"posting_date": posting_date,
			"items": [{"item_code": self.item, "warehouse": self.wh, "counted_qty": counted}]})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def scv(self, sc, year, month):
		doc = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": self.name,
			"item_code": self.item, "valid_from_year": year, "valid_from_month": month,
			"standard_cost": sc, "source_type": "MANUAL_OVERRIDE"})
		doc.insert(ignore_permissions=True)
		doc.release()
		return doc

	def settle(self, year, month):
		doc = frappe.get_doc({"doctype": "Inventory Period Settlement Run", "company": self.name,
			"period_year": year, "period_month": month, "run_type": "INITIAL_CLOSE"})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def reverse_settlement(self, year, month):
		from periodic_valuation.periodic_standard_cost.engine import StdEngine

		name = frappe.db.get_value("Inventory Period Settlement",
			{"company": self.name, "item_code": self.item, "period_year": year,
				"period_month": month, "cancelled": 0})
		StdEngine(self.name, self.item, self.wh).sett_reverse(
			name, source=("Inventory Period Settlement", name))

	def opening_sr(self, qty, rate, posting_date):
		tsa = frappe.get_all("Account", filters={"company": self.name, "is_group": 0,
			"root_type": "Liability"}, limit=1, pluck="name")[0]
		doc = frappe.get_doc({"doctype": "Stock Reconciliation", "company": self.name,
			"purpose": "Opening Stock", "posting_date": posting_date, "set_posting_time": 1,
			"expense_account": tsa,
			"items": [{"item_code": self.item, "warehouse": self.wh, "qty": qty,
				"valuation_rate": rate}]})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def events(self):
		return frappe.get_all("Inventory Valuation Event",
			filters={"company": self.name, "item_code": self.item, "is_cancelled": 0},
			fields=["std_trans", "period_month", "total_sc", "total_ac"],
			order_by="creation asc")

	def sett_row(self, year, month):
		rows = frappe.get_all("Inventory Period Settlement",
			filters={"company": self.name, "item_code": self.item, "period_year": year,
				"period_month": month, "cancelled": 0},
			fields=["es_var", "out_var"])
		return rows[0] if rows else None

	def verify_events(self, tag, expected):
		actual = self.events()
		for i, exp in enumerate(expected):
			trans, month, t_sc, t_ac = exp
			if i >= len(actual):
				check(f"{tag}#{i + 1} {trans}", False, "missing")
				continue
			a = actual[i]
			ok = a.std_trans == trans and a.period_month == month
			if ok and t_sc is not None:
				ok = abs(flt(a.total_sc) - t_sc) <= 0.01
			if ok and t_ac is not None:
				ok = abs(flt(a.total_ac) - t_ac) <= 0.01
			check(f"{tag}#{i + 1} {trans} P{month} SC {t_sc} AC {t_ac}", ok,
				f"got {a.std_trans} P{a.period_month} SC {a.total_sc} AC {a.total_ac}")
		if len(actual) != len(expected):
			check(f"{tag} event count {len(expected)}", False,
				f"got {len(actual)}: {[x.std_trans for x in actual[len(expected):]]}")

	def verify_final(self, tag, year, month, qty, value, sc):
		from periodic_valuation.shared.accounts import get_inventory_account

		ipb = frappe.get_all("Inventory Period Balance",
			filters={"company": self.name, "item_code": self.item,
				"period_year": year, "period_month": month},
			fields=["closing_qty", "closing_value", "period_standard_cost"])
		check(f"{tag} final IPB {qty}/{value}@{sc}",
			ipb and flt(ipb[0].closing_qty) == qty
			and abs(flt(ipb[0].closing_value) - value) <= 0.01
			and flt(ipb[0].period_standard_cost) == sc, str(ipb))
		gl = frappe.get_all("GL Entry", filters={"company": self.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"])
		inv = get_inventory_account(self.name, self.item, self.wh)
		inv_net = flt(sum(g.debit - g.credit for g in gl if g.account == inv), 2)
		tb = flt(sum(g.debit - g.credit for g in gl), 2)
		check(f"{tag} GL Inventory == {value}", abs(inv_net - value) <= 0.01, str(inv_net))
		check(f"{tag} trial balance nets 0", abs(tb) <= 0.01, str(tb))

	def verify_sett(self, tag, year, month, es, out):
		s = self.sett_row(year, month)
		check(f"{tag} Sett {year}-{month:02d} ES {es} / Out {out}",
			s and abs(flt(s.es_var) - es) <= 0.01 and abs(flt(s.out_var) - out) <= 0.01,
			s and f"got {s.es_var}/{s.out_var}")


# ------------------------------------------------------------- scenario A
def scenario_a(clock):
	co = Co("YTD Replay Co A", "YRA", "_YTD-V203A")
	clock.set("2026-01-15")
	co.period(2026, 2, "OPEN")
	co.scv(12, 2026, 2)

	clock.set("2026-02-01"); co.opening_sr(200, 15, "2026-02-01")          # Beg 2400/3000
	clock.set("2026-02-05"); co.pr(100, 10, "2026-02-01")
	clock.set("2026-02-10"); co.dn(30, "2026-02-05"); pr_feb8 = co.pr(50, 20, "2026-02-08")
	clock.set("2026-03-06"); co.advance(2026, 3)
	dn_mar5 = co.dn(20, "2026-03-05"); co.lcv(pr_feb8, 200, "2026-03-05")
	clock.set("2026-03-10"); co.count(295, "2026-03-07")                   # SC- 5
	clock.set("2026-03-14"); pr_mar10 = co.pr(16, 25, "2026-03-10")
	clock.set("2026-03-15"); co.scv(17, 2026, 3)                           # triplet 1000/830/-275
	clock.set("2026-03-16"); co.lcv(pr_mar10, 678, "2026-03-14")
	co.dn(-10, "2026-03-15", is_return=True, against=dn_mar5.name)         # SR 170
	clock.set("2026-03-20"); co.pr(40, 15, "2026-02-20")                   # REC (BD)+comp
	clock.set("2026-03-25"); co.settle(2026, 2)                            # Feb sett
	clock.set("2026-03-30"); co.pr(-50, 20, "2026-03-30", is_return=True, against=pr_feb8.name)
	clock.set("2026-04-02"); co.advance(2026, 4)
	co.settle(2026, 3)                                                     # Mar v1 (wrong, unanchored)
	clock.set("2026-04-03"); co.reverse_settlement(2026, 3)                # reverse pair
	clock.set("2026-04-05")
	rec_reopen = co.pr(100, 75, "2026-03-25")                              # PLAIN Rec (post-reopen)
	co.settle(2026, 3)                                                     # Mar v2
	clock.set("2026-04-10"); co.pr(-50, 75, "2026-04-07", is_return=True, against=rec_reopen.name)
	clock.set("2026-04-12"); co.dn(20, "2026-04-10")
	clock.set("2026-05-05"); co.advance(2026, 5)
	co.scv(25, 2026, 5)                                                    # triplet 1600/1648/-520
	clock.set("2026-05-10"); co.dn(5, "2026-04-25")                        # Issue (BD)+comp
	clock.set("2026-05-15"); co.settle(2026, 4)
	clock.set("2026-05-25"); co.pr(104, 70.6664, "2026-05-20")
	clock.set("2026-06-01"); co.advance(2026, 6)
	co.settle(2026, 5)

	# (trans, period_month, total_sc, total_ac) — None = not compared
	co.verify_events("A", [
		("Beg", 2, 2400, 3000), ("Rec", 2, 1200, 1000), ("Iss", 2, -360, 0),
		("Rec", 2, 600, 1000), ("Iss", 3, -240, 0), ("LC", 3, 0, 200),
		("SC-", 3, -60, 0), ("Rec", 3, 192, 400),
		("Rev Beg", 3, 1000, 0), ("REV In", 3, 830, 0), ("REV out", 3, -275, 0),
		("LC", 3, 0, 678), ("SR", 3, 170, 170),
		("REC (BD)", 2, 480, 600), ("REC (BD) - Rev", 3, 200, 0),
		("Sett", 2, 849.2308, 0), ("Sett - Rev", 3, -849.2308, 0),
		("PR", 3, -850, -1000),
		("Sett", 3, None, 0), ("Sett - Rev", 4, None, 0),                 # v1 (wrong by design)
		("Sett - Reverse", 3, None, 0), ("Sett - Rev - Reverse", 4, None, 0),
		("Rec", 3, 1700, 7500),                                            # post-reopen PLAIN label
		("Sett", 3, 5070.8026, 0), ("Sett - Rev", 4, -5070.8026, 0),
		("PR", 4, -850, -3750), ("Iss", 4, -340, 0),
		("Rev Beg", 5, 1600, 0), ("REV In", 5, 1648, 0), ("REV out", 5, -520, 0),
		("Issue (BD)", 4, -85, 0), ("Issue (BD) - Rev", 5, -40, 0),
		("Sett", 4, 2256, 0), ("Sett - Rev", 5, -2256, 0),
		("Rec", 5, 2600, 7349.68),  # wb 7349.3014 @70.6664; 2dp pricing -> 70.67
		("Sett", 5, 3647.41, 0), ("Sett - Rev", 6, -3647.41, 0),  # wb 3647.0836 + 0.38 AC drift
	])
	co.verify_sett("A", 2026, 2, 849.2308, 70.7692)
	co.verify_sett("A", 2026, 3, 5070.8026, 555.1974)
	co.verify_sett("A", 2026, 4, 2256, 470)
	co.verify_sett("A", 2026, 5, 3647.41, 580.27)  # wb 3647.0836/580.2178 + 2dp-rate drift
	co.verify_final("A", 2026, 6, 440, 11000, 25)


# ------------------------------------------------------------- scenario B
def scenario_b(clock):
	co = Co("YTD Replay Co B", "YRB", "_YTD-V203B")
	clock.set("2025-12-01")
	co.period(2025, 12, "OPEN")
	co.scv(12, 2025, 12)

	clock.set("2025-12-13"); co.pr(1000, 15, "2025-12-13")
	clock.set("2025-12-14"); co.dn(300, "2025-12-14")
	clock.set("2026-01-05"); co.advance(2026, 1)
	co.count(800, "2026-01-02")                                            # SC+ 100 @12
	pr_jan3 = co.pr(500, 25, "2026-01-03")
	co.scv(15, 2026, 1)                                                    # triplet 2100/1500/300
	clock.set("2026-01-06"); co.pr(100, 20, "2025-12-30")                  # REC (BY)+comp
	clock.set("2026-01-07"); co.dn(50, "2025-12-30")                       # Issue (BY)+comp
	clock.set("2026-01-10"); co.settle(2025, 12)                           # Dec sett, carry Jan 1
	clock.set("2026-02-02"); co.advance(2026, 2)
	co.pr(-50, 25, "2026-02-02", is_return=True, against=pr_jan3.name)     # PR
	clock.set("2026-02-05"); pr_feb3 = co.pr(1000, 30, "2026-02-03")
	dn_feb4 = co.dn(500, "2026-02-04")
	clock.set("2026-02-10"); co.scv(25, 2026, 2)                           # triplet 7500/14500/-4000
	clock.set("2026-02-15"); co.pr(200, 20, "2026-01-25")                  # REC (BD)+comp
	clock.set("2026-02-18"); co.dn(50, "2026-01-25")                       # Issue (BD)+comp
	clock.set("2026-02-20"); co.dn(-100, "2026-02-20", is_return=True, against=dn_feb4.name)  # SR
	clock.set("2026-02-25"); co.settle(2026, 1)                            # Jan sett
	clock.set("2026-02-26"); co.lcv(pr_feb3, 5000, "2026-02-26")
	clock.set("2026-03-05"); co.advance(2026, 3)
	co.count(2000, "2026-02-28")                                           # SC- 50 backdated (2050 on hand)
	clock.set("2026-03-06"); co.settle(2026, 2)                            # Feb sett

	co.verify_events("B", [
		("Rec", 12, 12000, 15000), ("Iss", 12, -3600, 0),
		("SC+", 1, 1200, 0), ("Rec", 1, 6000, 12500),
		("Rev Beg", 1, 2100, 0), ("REV In", 1, 1500, 0), ("REV out", 1, 300, 0),
		("REC (BY)", 12, 1200, 2000), ("REC (BY) - Rev", 1, 300, 0),
		("Issue (BY)", 12, -600, 0), ("Issue (BY) - Rev", 1, -150, 0),
		("Sett", 12, 2590.9091, 0), ("Sett - Rev", 1, -2590.9091, 0),
		("PR", 2, -750, -1250),
		("Rec", 2, 15000, 30000), ("Iss", 2, -7500, 0),
		("Rev Beg", 2, 7500, 0), ("REV In", 2, 14500, 0), ("REV out", 2, -4000, 0),
		("REC (BD)", 1, 3000, 4000), ("REC (BD) - Rev", 2, 2000, 0),
		("Issue (BD)", 1, -750, 0), ("Issue (BD) - Rev", 2, -500, 0),
		("SR", 2, 2500, 2500),
		("Sett", 1, 6559.5611, 0), ("Sett - Rev", 2, -6559.5611, 0),
		("LC", 2, 0, 5000),
		("SC-", 2, -1250, 0),
		("Sett", 2, 1534.0909, 0), ("Sett - Rev", 3, -1534.0909, 0),
	])
	co.verify_sett("B", 2025, 12, 2590.9091, 1209.0909)
	co.verify_sett("B", 2026, 1, 6559.5611, -218.652)
	co.verify_sett("B", 2026, 2, 1534.0909, 306.8182)
	co.verify_final("B", 2026, 3, 2000, 50000, 25)


def run(commit=False):
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	frappe.db.set_single_value("Selling Settings", "maintain_same_sales_rate", 0)

	clock = Clock()
	try:
		print("--- Scenario A: 02 YTD V2.03 base ---")
		scenario_a(clock)
		print("\n--- Scenario B: year transfer ---")
		scenario_b(clock)
	finally:
		clock.restore()

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
