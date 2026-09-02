# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""MAP Rule demo pack - the client's Cost Adjustment tree (23 Aug 2026,
DR-33/DR-34) and the DR-44 reversal floor entered the way a user enters them,
in a dedicated company, so they can be persisted on UAT and opened one by one.

bench --site <site> execute periodic_valuation.tests.uat_map_rule_pack.run --kwargs "{'commit': True}"

One scenario per branch of the tree, using the diagram's own example numbers:

  CA1  ES qty > adjusted qty        -> no stock ratio, all to inventory
       (tree example: ES 100, invoicing 500 on PO qty 50 -> 500 inventory)
  CA2  ES qty < adjusted qty        -> ratio = ESQ / total received since zero
  CA3  ES value + adj < 0           -> inventory to exactly zero, excess PRD
       (tree example: 300 + (-500) -> -300 inventory, -200 PRD)
  CA4  ES value + adj = 0           -> all inventory, lands exactly on zero
       (tree example: 300 + (-300) -> -300 inventory)
  D44A receipt cancelled after part of the blended stock was issued
       (the client's Cancel Netting numbers: floor -1,696,875 / PRD -65,625)
  D44B landed cost reversed after consumption (floor -15 / PRD -35)

Company "MAP Rule Demo Co" (abbr MRD) is created on first run, with ONE
"Price Difference (PRD)" account so the GL reads like the client's tree.
Rolled back unless commit=True; the scenario -> document index is printed
between INDEX markers.
"""

import frappe
from frappe.utils import flt, get_first_day, nowdate

COMPANY = "MAP Rule Demo Co"
ABBR = "MRD"
SUPPLIER = "MAP Demo Supplier"
CUSTOMER = "MAP Demo Customer"

CHECKS = []
INDEX = []  # (ref, scenario, [(doctype, name)], expected, observed)


def check(ref, label, ok, detail=""):
	CHECKS.append((f"{ref} {label}", bool(ok)))
	print(("PASS " if ok else "FAIL ") + f"{ref} {label}" + (f" - {detail}" if detail and not ok else ""))


def idx(ref, scenario, docs, expected, observed):
	INDEX.append((ref, scenario, docs, expected, observed))


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
		base = frappe.get_all("Company", filters={"name": ("not like", "%Demo%")},
			limit=1, fields=["default_currency", "country"])[0]
		frappe.get_doc({"doctype": "Company", "company_name": COMPANY, "abbr": ABBR,
			"default_currency": base.default_currency, "country": base.country,
			"create_chart_of_accounts_based_on": "Standard Template",
		}).insert(ignore_permissions=True)
	from frappe.utils import getdate
	for fy in frappe.get_all("Fiscal Year", fields=["name", "year_start_date", "year_end_date"]):
		if fy.year_start_date.year <= getdate(nowdate()).year <= fy.year_end_date.year:
			has_rows = frappe.db.exists("Fiscal Year Company", {"parent": fy.name})
			ours = frappe.db.exists("Fiscal Year Company", {"parent": fy.name, "company": COMPANY})
			if has_rows and not ours:
				frappe.get_doc({"doctype": "Fiscal Year Company", "parent": fy.name,
					"parenttype": "Fiscal Year", "parentfield": "companies",
					"company": COMPANY}).insert(ignore_permissions=True)

	prd = make_acc("Price Difference (PRD)", "Expense")
	adj = make_acc("Stock Adjustment MAP", "Expense")
	fx = make_acc("Exchange Gain Loss MAP", "Expense")
	if not frappe.db.exists("Periodic Moving Average Settings", {"company": COMPANY}):
		frappe.get_doc({"doctype": "Periodic Moving Average Settings", "company": COMPANY,
			"negative_stock_allowed": 1,
			"prd_account": prd,                       # DR-44 floor / cross-period PRD
			"price_difference_account": prd,          # coverage expense + DR-34 floor excess
			"inventory_variance_account": adj,
			"stock_revaluation_account": adj,
			"stock_rounding_adjustment_account": adj,
			"fx_gain_loss_account": fx,
		}).insert(ignore_permissions=True)
	make_acc("Freight Charges MAP", "Expense")

	wh = f"Stores - {ABBR}"
	if not frappe.db.exists("Warehouse", wh):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores",
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
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "status": "OPEN"}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": get_first_day(nowdate()), "status": "OPEN"}).insert(ignore_permissions=True)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)
	return wh


def map_item(code, item_name):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": item_name,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Moving Average"}).insert(ignore_permissions=True)
	return code


# ----------------------------------------------------------------- vouchers
def make_pr(item, wh, qty, rate):
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": SUPPLIER,
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh}]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	return pr


def make_dn(item, wh, qty):
	dn = frappe.get_doc({"doctype": "Delivery Note", "company": COMPANY, "customer": CUSTOMER,
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": 2000, "warehouse": wh}]})
	dn.insert(ignore_permissions=True)
	dn.submit()
	return dn


def make_pi(pr, qty, rate):
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": COMPANY, "supplier": SUPPLIER,
		"posting_date": nowdate(),
		"items": [{"item_code": pr.items[0].item_code, "qty": qty, "rate": rate,
			"warehouse": pr.items[0].warehouse,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	return pi


def make_lcv(pr, amount):
	exp = f"Freight Charges MAP - {ABBR}"
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": nowdate(), "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name, "supplier": SUPPLIER, "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp, "description": "freight", "amount": amount}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	return lcv


def cancel(doctype, name):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	cx = frappe.get_doc(doctype, make_cancellation(doctype, name))
	cx.submit()
	return cx


# ------------------------------------------------------------------ readers
def ipb(item):
	rows = frappe.get_all("Inventory Period Balance",
		filters={"company": COMPANY, "item_code": item},
		fields=["closing_qty", "closing_value", "moving_avg_price"],
		order_by="period_year desc, period_month desc", limit=1)
	return rows[0] if rows else None


def ive(source_docname, reason):
	rows = frappe.get_all("Inventory Valuation Event",
		filters={"company": COMPANY, "source_docname": source_docname,
			"reason_code": reason, "is_cancelled": 0},
		fields=["name", "value_delta", "expense_portion", "prd_amount"], order_by="creation")
	return rows[0] if rows else None


def gl_net(voucher_no, account):
	rows = frappe.get_all("GL Entry", filters={"voucher_no": voucher_no,
		"account": account, "is_cancelled": 0, "company": COMPANY}, fields=["debit", "credit"])
	return flt(sum(flt(g.debit) - flt(g.credit) for g in rows), 2)


# ===================================================================== main
def run(commit=False):
	wh = ensure_company()
	prd = f"Price Difference (PRD) - {ABBR}"
	from periodic_valuation.shared.accounts import get_inventory_account

	# ---------------- CA1: ES qty > adjusted qty -> all to inventory
	it = map_item("CA1 Full Coverage", "CA1 - Full Coverage: All To Inventory")
	stock = get_inventory_account(COMPANY, it, wh)
	pr_a = make_pr(it, wh, 50, 10)          # the PO being invoiced
	pr_b = make_pr(it, wh, 50, 10)          # other stock -> ES 100
	pi = make_pi(pr_a, 50, 20)              # +10/unit on qty 50 -> diff 500
	ev, c = ive(pi.name, "invoice_diff"), ipb(it)
	check("CA1", "ES 100 > adj qty 50: the whole 500 posts to inventory, no ratio",
		ev and flt(ev.value_delta, 2) == 500 and flt(ev.expense_portion or 0, 2) == 0
		and c and flt(c.closing_value, 2) == 1500 and gl_net(pi.name, stock) == 500,
		f"ev {ev} closing {c and c.closing_value} gl {gl_net(pi.name, stock)}")
	idx("CA1", "ES qty > adjusted qty (tree example: invoicing 500 on PO qty 50 with ES 100)",
		[("Purchase Receipt", pr_a.name), ("Purchase Receipt", pr_b.name), ("Purchase Invoice", pi.name)],
		"Dr Stock In Hand 500 / Cr SRBNB 500; closing 100 qty / 1,500",
		f"inventory {gl_net(pi.name, stock):+.2f}; closing {flt(c.closing_qty)}/{flt(c.closing_value, 2)}")

	# ---------------- CA2: ES qty < adjusted qty -> stock ratio (since-zero)
	it = map_item("CA2 Partial Coverage", "CA2 - Partial Coverage: Stock Ratio")
	stock = get_inventory_account(COMPANY, it, wh)
	pr = make_pr(it, wh, 100, 10)
	dn = make_dn(it, wh, 80)                # ES 20 of 100 received since zero
	pi = make_pi(pr, 100, 11)               # diff 100 on adj qty 100 > ES 20
	ev, c = ive(pi.name, "invoice_diff"), ipb(it)
	check("CA2", "ES 20 < adj 100: ratio 20/100 -> 20 inventory / 80 expense (PRD)",
		ev and flt(ev.value_delta, 2) == 20 and flt(ev.expense_portion, 2) == 80
		and gl_net(pi.name, stock) == 20 and gl_net(pi.name, prd) == 80,
		f"ev {ev} gl stock {gl_net(pi.name, stock)} prd {gl_net(pi.name, prd)}")
	idx("CA2", "ES qty < adjusted qty (ratio = ESQ / total received since last zero)",
		[("Purchase Receipt", pr.name), ("Delivery Note", dn.name), ("Purchase Invoice", pi.name)],
		"Diff 100: Dr Stock In Hand 20 / Dr PRD 80 / Cr SRBNB 100",
		f"inventory {gl_net(pi.name, stock):+.2f}, PRD {gl_net(pi.name, prd):+.2f}")

	# ---------------- CA3: value floor -> inventory to zero, excess PRD
	# (a negative landed-cost charge - a credit note on freight - fully covered
	# by the on-hand quantity, so the ratio is 1 and only the VALUE test acts)
	it = map_item("CA3 Floor To Zero", "CA3 - Floor: Inventory To Zero, Excess PRD")
	stock = get_inventory_account(COMPANY, it, wh)
	pr = make_pr(it, wh, 30, 10)            # ES value 300
	lcv = make_lcv(pr, -500)                # tree example: 300 + (-500) < 0
	ev, c = ive(lcv.name, "landed_cost"), ipb(it)
	check("CA3", "300 + (-500): -300 inventory (lands on zero), -200 PRD",
		ev and flt(ev.value_delta, 2) == -300 and flt(ev.expense_portion, 2) == -200
		and c and flt(c.closing_value, 2) == 0 and flt(c.closing_qty) == 30
		and gl_net(lcv.name, stock) == -300 and gl_net(lcv.name, prd) == -200,
		f"ev {ev} closing {c and c.closing_value} gl stock {gl_net(lcv.name, stock)} prd {gl_net(lcv.name, prd)}")
	idx("CA3", "ES value + adjustment < 0 (tree example: 300 + (-500))",
		[("Purchase Receipt", pr.name), ("Landed Cost Voucher", lcv.name)],
		"Cr Stock In Hand 300 (exactly to zero) / Cr PRD 200; closing 30 qty / 0.00",
		f"inventory {gl_net(lcv.name, stock):+.2f}, PRD {gl_net(lcv.name, prd):+.2f}; closing {flt(c.closing_qty)}/{flt(c.closing_value, 2)}")

	# ---------------- CA4: exact zero landing -> all inventory, no PRD
	it = map_item("CA4 Exact Zero", "CA4 - Exact Zero Landing: All Inventory")
	stock = get_inventory_account(COMPANY, it, wh)
	pr = make_pr(it, wh, 30, 10)
	lcv = make_lcv(pr, -300)                # tree example: 300 + (-300) = 0
	ev, c = ive(lcv.name, "landed_cost"), ipb(it)
	check("CA4", "300 + (-300) = 0: all -300 to inventory, no PRD leg",
		ev and flt(ev.value_delta, 2) == -300 and flt(ev.expense_portion or 0, 2) == 0
		and c and flt(c.closing_value, 2) == 0 and gl_net(lcv.name, prd) == 0,
		f"ev {ev} closing {c and c.closing_value} prd {gl_net(lcv.name, prd)}")
	idx("CA4", "ES value + adjustment = 0 (tree example: 300 + (-300))",
		[("Purchase Receipt", pr.name), ("Landed Cost Voucher", lcv.name)],
		"Cr Stock In Hand 300, no PRD; closing 30 qty / 0.00",
		f"inventory {gl_net(lcv.name, stock):+.2f}, PRD {gl_net(lcv.name, prd):+.2f}; closing {flt(c.closing_qty)}/{flt(c.closing_value, 2)}")

	# ---------------- D44A: cancel after issue -> DR-44 floor (client's numbers)
	it = map_item("D44A Cancel After Issue", "D44A - Cancel After Issue: Floored To PRD")
	stock = get_inventory_account(COMPANY, it, wh)
	pr1 = make_pr(it, wh, 1500, 1175)
	pr2 = make_pr(it, wh, 500, 1000)        # MAP 1131.25
	dn = make_dn(it, wh, 500)               # -565,625 at the blended MAP
	cx = cancel("Purchase Receipt", pr1.name)
	ev, c = ive(cx.name, "cancellation"), ipb(it)
	check("D44A", "cancel 1,762,500 with 1,696,875 on hand: floored, PRD 65,625, ends 0/0",
		ev and flt(ev.value_delta, 2) == -1696875 and flt(ev.prd_amount, 2) == -65625
		and c and flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0
		and gl_net(cx.name, stock) == -1696875 and gl_net(cx.name, prd) == -65625,
		f"ev {ev} closing {c and c.closing_value} gl stock {gl_net(cx.name, stock)} prd {gl_net(cx.name, prd)}")
	idx("D44A", "receipt cancelled after a partial issue (the Cancel Netting numbers)",
		[("Purchase Receipt", pr1.name), ("Purchase Receipt", pr2.name),
			("Delivery Note", dn.name), ("Purchase Receipt", cx.name)],
		"Dr SRBNB 1,762,500 / Cr Stock In Hand 1,696,875 / Cr PRD 65,625; closing 0 / 0.00",
		f"inventory {gl_net(cx.name, stock):+.2f}, PRD {gl_net(cx.name, prd):+.2f}; closing {flt(c.closing_qty)}/{flt(c.closing_value, 2)}")

	# ---------------- D44B: LCV reversed after consumption -> floored
	it = map_item("D44B LCV Reversal", "D44B - Landed Cost Reversal: Floored To PRD")
	stock = get_inventory_account(COMPANY, it, wh)
	pr = make_pr(it, wh, 10, 10)
	lcv = make_lcv(pr, 50)                  # 10 qty / 150, MAP 15
	dn = make_dn(it, wh, 9)                 # 1 qty / 15
	cxl = cancel("Landed Cost Voucher", lcv.name)
	ev, c = ive(cxl.name, "cancellation"), ipb(it)
	check("D44B", "reverse LC 50 with 15 on hand: -15 inventory, -35 PRD, ends 1 qty / 0",
		ev and flt(ev.value_delta, 2) == -15 and flt(ev.prd_amount, 2) == -35
		and c and flt(c.closing_qty) == 1 and flt(c.closing_value, 2) == 0
		and gl_net(cxl.name, stock) == -15 and gl_net(cxl.name, prd) == -35,
		f"ev {ev} closing {c and c.closing_value} gl stock {gl_net(cxl.name, stock)} prd {gl_net(cxl.name, prd)}")
	idx("D44B", "landed cost reversed after 9 of 10 units were issued",
		[("Purchase Receipt", pr.name), ("Landed Cost Voucher", lcv.name),
			("Delivery Note", dn.name), ("Landed Cost Voucher", cxl.name)],
		"Cr Stock In Hand 15 / Cr PRD 35 / Dr Freight 50; closing 1 qty / 0.00",
		f"inventory {gl_net(cxl.name, stock):+.2f}, PRD {gl_net(cxl.name, prd):+.2f}; closing {flt(c.closing_qty)}/{flt(c.closing_value, 2)}")

	# ------------------------------------------------------------- summary
	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	print("\n===== INDEX =====")
	for ref, scenario, docs, expected, observed in INDEX:
		print(f"{ref}: {scenario}")
		for dt, dn_ in docs:
			print(f"    {dt}: {dn_}")
		print(f"    expected: {expected}")
		print(f"    observed: {observed}")
	print("===== END INDEX =====")
	if commit and not failed:
		frappe.db.commit()
		print("COMMITTED")
	else:
		frappe.db.rollback()
		print("ROLLED BACK")
