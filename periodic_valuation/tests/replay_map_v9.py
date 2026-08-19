# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Replay of the signed MAP workbook + Book2 through REAL ERPNext vouchers —
bench --site <site> execute periodic_valuation.tests.replay_map_v9.run

Scenario 1: `SAP_MA_Sample_Entries_v9.xlsx` Main Scenario (Feb-Mar 2026):
receipts/issues, LCV stock-ratio split 133.33/66.67, Stock Count, MR21 +500,
sales return WITH reference (at original issue cost), backdated Feb receipt
(carryover buckets, Feb restated 160/2300), purchase return at the original
receipt cost EXCLUDING landed cost (DR-20). Final 95 / 1,672.50 / MAP 17.61.

Scenario 2: `Book2.xlsx` (client stock-ratio worked example): MAP 14.6154
after 100@10 / -30 / 60@20; issue 110 leaves 20 on hand; LCV 200 splits by
stock ratio 20/160 = 0.125 -> 25 inventory / 175 expense.

The PRD/negative-stock (Negative.xlsx) and client-locked C1/C2 backdated
cases already run on real vouchers in smoke_edges.
Rolled back unless commit=True.
"""

import frappe
from frappe.utils import flt

from periodic_valuation.tests.replay_ytd_v203 import Clock, Co

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))


class MapCo(Co):
	def __init__(self, name, abbr, item):
		super().__init__(name, abbr, item, method="Periodic Moving Average")
		if not frappe.db.exists("Periodic Moving Average Settings", {"company": name}):
			def acc(hint, root="Expense"):
				rows = frappe.get_all("Account", filters={"company": name, "is_group": 0,
					"account_name": ("like", f"%{hint}%")}, limit=1, pluck="name")
				return rows[0] if rows else frappe.get_all("Account",
					filters={"company": name, "is_group": 0, "root_type": root},
					limit=1, pluck="name")[0]
			frappe.get_doc({
				"doctype": "Periodic Moving Average Settings", "company": name,
				"negative_stock_allowed": 1,
				"prd_account": acc("Cost of Goods Sold"),
				"fx_gain_loss_account": acc("Exchange Gain"),
				"stock_rounding_adjustment_account": acc("Stock Adjustment"),
				"price_difference_account": acc("Cost of Goods Sold"),
				"inventory_variance_account": acc("Stock Adjustment"),
				"stock_revaluation_account": acc("Stock Adjustment"),
			}).insert(ignore_permissions=True)

	def mr21(self, amount, posting_date):
		b = self.ipb()
		rate = flt(flt(b.moving_avg_price) + amount / flt(b.closing_qty), 6)
		doc = frappe.get_doc({"doctype": "Stock Revaluation", "company": self.name,
			"posting_date": posting_date,
			"items": [{"item_code": self.item, "warehouse": self.wh,
				"new_valuation_rate": rate}]})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def ipb(self, year=None, month=None):
		filters = {"company": self.name, "item_code": self.item}
		if year:
			filters.update({"period_year": year, "period_month": month})
		rows = frappe.get_all("Inventory Period Balance", filters=filters,
			fields=["closing_qty", "closing_value", "moving_avg_price",
				"carryover_qty", "carryover_value", "receipt_qty", "receipt_value"],
			order_by="period_year desc, period_month desc", limit=1)
		return rows[0] if rows else None

	def state(self, tag, qty, value, map_2dp):
		b = self.ipb()
		check(f"{tag}: {qty} / {value} MAP {map_2dp}",
			b and flt(b.closing_qty) == qty and abs(flt(b.closing_value) - value) <= 0.01
			and abs(flt(b.moving_avg_price, 2) - map_2dp) <= 0.01,
			b and f"got {b.closing_qty}/{b.closing_value}/{flt(b.moving_avg_price, 4)}")

	def verify_gl(self, tag, inv_value):
		from periodic_valuation.shared.accounts import get_inventory_account

		gl = frappe.get_all("GL Entry", filters={"company": self.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"])
		inv = get_inventory_account(self.name, self.item, self.wh)
		inv_net = flt(sum(g.debit - g.credit for g in gl if g.account == inv), 2)
		tb = flt(sum(g.debit - g.credit for g in gl), 2)
		check(f"{tag} GL Inventory == {inv_value}", abs(inv_net - inv_value) <= 0.01, str(inv_net))
		check(f"{tag} trial balance nets 0", abs(tb) <= 0.01, str(tb))


def scenario_v9(clock):
	co = MapCo("MAP Replay Co A", "MPA", "_MAP-V9")
	clock.set("2026-02-01"); co.period(2026, 2, "OPEN")
	clock.set("2026-02-05"); co.pr(100, 10, "2026-02-05")
	co.state("v9#1 Rec 100@10", 100, 1000, 10)
	clock.set("2026-02-10"); dn_feb = co.dn(30, "2026-02-10")
	co.state("v9#2 Iss 30", 70, 700, 10)
	clock.set("2026-02-20"); pr2 = co.pr(50, 20, "2026-02-20")
	co.state("v9#3 Rec 50@20", 120, 1700, 14.17)

	clock.set("2026-03-03"); co.advance(2026, 3)
	co.dn(20, "2026-03-03")
	co.state("v9#4 Iss 20 (-283.33)", 100, 1416.67, 14.17)
	clock.set("2026-03-05"); lcv = co.lcv(pr2, 200, "2026-03-05")
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"company": co.name, "source_docname": lcv.name, "is_cancelled": 0},
		fields=["value_delta", "expense_portion"])
	inv_share = flt(sum(flt(x.value_delta) for x in ive), 2)
	exp_share = flt(sum(flt(x.expense_portion) for x in ive), 2)
	# DR-32 (client ruling 2026-08-18): coverage split — 100 on hand covers the
	# 50-unit receipt the charge applies to -> 1.0 -> all 200 to inventory.
	# (The workbook's pool-ratio row 133.33/66.67 is superseded.)
	check("v9#5 LCV coverage 1.0 -> 200 / 0",
		abs(inv_share - 200) <= 0.01 and abs(exp_share - 0) <= 0.01,
		f"{inv_share}/{exp_share}")
	co.state("v9#5 post-LC", 100, 1616.67, 16.17)
	clock.set("2026-03-08"); co.count(95, "2026-03-08")
	co.state("v9#6 count -5 (-80.83)", 95, 1535.84, 16.17)
	clock.set("2026-03-10"); co.mr21(500, "2026-03-10")
	co.state("v9#7 MR21 +500", 95, 2035.84, 21.43)
	clock.set("2026-03-12"); co.dn(-10, "2026-03-12", is_return=True, against=dn_feb.name)
	co.state("v9#8 sales return w/ ref (+100 at orig 10)", 105, 2135.84, 20.34)
	clock.set("2026-03-15"); co.pr(40, 15, "2026-02-20")
	feb = co.ipb(2026, 2)
	check("v9#9 Feb restated: receipts 190/2600, closing 160/2300",
		feb and flt(feb.receipt_qty) == 190 and abs(flt(feb.receipt_value) - 2600) <= 0.01
		and flt(feb.closing_qty) == 160 and abs(flt(feb.closing_value) - 2300) <= 0.01,
		str(feb))
	mar = co.ipb(2026, 3)
	check("v9#9 Mar carryover 40/600",
		mar and flt(mar.carryover_qty) == 40 and abs(flt(mar.carryover_value) - 600) <= 0.01,
		str(mar))
	clock.set("2026-03-20"); co.pr(-50, 20, "2026-03-20", is_return=True, against=pr2.name)
	co.state("v9#10 purchase return w/ ref (-1000, excl LC)", 95, 1735.84, 18.27)
	co.verify_gl("v9", 1735.84)


def scenario_book2(clock):
	co = MapCo("MAP Replay Co B", "MPB", "_MAP-BOOK2")
	clock.set("2026-02-01"); co.period(2026, 2, "OPEN")
	clock.set("2026-02-05"); co.pr(100, 10, "2026-02-05")
	clock.set("2026-02-10"); co.dn(30, "2026-02-10")
	clock.set("2026-02-15"); pr2 = co.pr(60, 20, "2026-02-15")
	co.state("Book2#1 MAP 14.6154 (1900/130)", 130, 1900, 14.62)

	clock.set("2026-03-05"); co.advance(2026, 3)
	co.dn(110, "2026-03-05")
	co.state("Book2#2 issue 110 -> 20 on hand", 20, 292.31, 14.62)
	clock.set("2026-03-10"); lcv = co.lcv(pr2, 200, "2026-03-10")
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"company": co.name, "source_docname": lcv.name, "is_cancelled": 0},
		fields=["value_delta", "expense_portion"])
	inv_share = flt(sum(flt(x.value_delta) for x in ive), 2)
	exp_share = flt(sum(flt(x.expense_portion) for x in ive), 2)
	# DR-32 coverage: 20 on hand vs the 60-unit receipt -> 0.333333 ->
	# 66.67 inventory / 133.33 expense (workbook's 0.125 pool row superseded)
	check("Book2#3 LC 200 splits 66.67 / 133.33 (coverage 20/60)",
		abs(inv_share - 66.67) <= 0.01 and abs(exp_share - 133.33) <= 0.01,
		f"{inv_share}/{exp_share}")
	co.state("Book2#3 post-LC", 20, 358.98, 17.95)
	co.verify_gl("Book2", 358.98)


def run(commit=False):
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	frappe.db.set_single_value("Selling Settings", "maintain_same_sales_rate", 0)

	clock = Clock()
	try:
		print("--- Scenario 1: SAP_MA_Sample_Entries_v9 Main ---")
		scenario_v9(clock)
		print("\n--- Scenario 2: Book2 stock-ratio example ---")
		scenario_book2(clock)
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
