"""Phase-2 kernel smoke - bench --site <site> execute periodic_valuation.tests.smoke_kernel.run

End-to-end on real vouchers, all inside one transaction, rolled back at the end
(pass commit=True to keep the data). Mirrors workbook v9 main-scenario steps
1-3 (receipt 100@10, issue 30, receipt 50@20 -> MAP 14.17, value 1700) and
verifies SME/IVE/IPB/SLE/GL identity.
"""

import frappe
from frappe.utils import flt, nowdate

def get_company():
	"""Site's default company - keeps the suites client-agnostic."""
	return (
		frappe.defaults.get_global_default("company")
		or frappe.get_all("Company", limit=1, pluck="name")[0]
	)


def __getattr__(name):
	if name == "COMPANY":
		return get_company()
	raise AttributeError(name)
ITEM = "_SMK-LIMESTONE"
CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def ensure_masters():
	abbr = frappe.db.get_value("Company", get_company(), "abbr")
	wh_name = f"_SMK Stores - {abbr}"
	if not frappe.db.exists("Warehouse", wh_name):
		frappe.get_doc({
			"doctype": "Warehouse", "warehouse_name": "_SMK Stores", "company": get_company(),
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({
			"doctype": "Item", "item_code": ITEM, "item_name": "Smoke Limestone",
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Moving Average",
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Supplier", "_SMK Supplier"):
		frappe.get_doc({
			"doctype": "Supplier", "supplier_name": "_SMK Supplier",
			"supplier_group": frappe.get_all("Supplier Group", limit=1, pluck="name")[0],
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer", "_SMK Customer"):
		frappe.get_doc({
			"doctype": "Customer", "customer_name": "_SMK Customer",
			"customer_group": frappe.get_all("Customer Group", limit=1, pluck="name")[0],
			"territory": frappe.get_all("Territory", limit=1, pluck="name")[0],
		}).insert(ignore_permissions=True)

	if not frappe.db.exists("Periodic Moving Average Settings", {"company": get_company()}):
		accounts = {}
		for key, hint in [
			("prd_account", "Cost of Goods Sold"),
			("fx_gain_loss_account", "Exchange Gain/Loss"),
			("stock_rounding_adjustment_account", "Stock Adjustment"),
			("price_difference_account", "Cost of Goods Sold"),
			("inventory_variance_account", "Stock Adjustment"),
			("stock_revaluation_account", "Stock Adjustment"),
		]:
			acc = frappe.get_all(
				"Account",
				filters={"company": get_company(), "is_group": 0, "account_name": ("like", f"%{hint}%")},
				limit=1, pluck="name",
			) or frappe.get_all(
				"Account",
				filters={"company": get_company(), "is_group": 0, "root_type": "Expense"},
				limit=1, pluck="name",
			)
			accounts[key] = acc[0]
		frappe.get_doc({
			"doctype": "Periodic Moving Average Settings", "company": get_company(),
			"negative_stock_allowed": 1, **accounts,
		}).insert(ignore_permissions=True)

	today = nowdate()
	from frappe.utils import get_first_day
	if not frappe.db.exists(
		"Inventory Period", {"company": get_company(), "status": "OPEN"}
	):
		frappe.get_doc({
			"doctype": "Inventory Period", "company": get_company(),
			"start_date": get_first_day(today), "status": "OPEN",
		}).insert(ignore_permissions=True)
	return wh_name


def run(commit=False):
	wh = ensure_masters()

	pr = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": get_company(), "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "items": [{
			"item_code": ITEM, "qty": 100, "rate": 10, "warehouse": wh,
		}],
	})
	pr.insert(ignore_permissions=True)
	pr.submit()
	check("PR submitted", pr.docstatus == 1)

	ipb = frappe.get_all("Inventory Period Balance",
		filters={"company": get_company(), "item_code": ITEM},
		fields=["*"], limit=1)[0]
	check("IPB after receipt: qty 100", flt(ipb.closing_qty) == 100, str(ipb.closing_qty))
	check("IPB after receipt: value 1000", flt(ipb.closing_value) == 1000, str(ipb.closing_value))
	check("IPB MAP 10", flt(ipb.moving_avg_price) == 10, str(ipb.moving_avg_price))

	sme = frappe.get_all("Stock Movement Event", filters={"source_docname": pr.name}, fields=["movement_type", "qty_delta"])
	ive = frappe.get_all("Inventory Valuation Event", filters={"source_docname": pr.name}, fields=["name", "reason_code", "value_delta", "map_after"])
	check("SME receipt written", sme and sme[0].movement_type == "receipt" and sme[0].qty_delta == 100)
	check("IVE receipt written", ive and ive[0].reason_code == "receipt" and flt(ive[0].value_delta) == 1000)

	sle = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": pr.name, "is_cancelled": 0},
		fields=["posted_via_valuation_kernel", "stock_value_difference", "qty_after_transaction", "valuation_rate"])
	check("SLE kernel-flagged", sle and sle[0].posted_via_valuation_kernel == 1)
	check("SLE svd 1000", sle and flt(sle[0].stock_value_difference) == 1000)

	gl = frappe.get_all("GL Entry", filters={"voucher_no": pr.name, "is_cancelled": 0},
		fields=["account", "debit", "credit", "valuation_event_id"])
	tagged = [g for g in gl if g.valuation_event_id]
	check("GL entries tagged with IVE", len(tagged) == len(gl) and len(gl) >= 2, f"{len(tagged)}/{len(gl)}")
	dr = sum(flt(g.debit) for g in gl)
	cr = sum(flt(g.credit) for g in gl)
	check("GL balanced", flt(dr, 2) == flt(cr, 2) == 1000, f"dr {dr} cr {cr}")

	# no repost entries for routed voucher
	check("no RIV created", not frappe.db.exists("Repost Item Valuation", {"voucher_no": pr.name}))

	# --- issue via Delivery Note: 30 units at MAP 10
	dn = frappe.get_doc({
		"doctype": "Delivery Note", "company": get_company(), "customer": "_SMK Customer",
		"posting_date": nowdate(), "items": [{
			"item_code": ITEM, "qty": 30, "rate": 25, "warehouse": wh,
		}],
	})
	dn.insert(ignore_permissions=True)
	dn.submit()
	ipb = frappe.get_all("Inventory Period Balance",
		filters={"company": get_company(), "item_code": ITEM}, fields=["*"], limit=1)[0]
	check("IPB after issue: qty 70 value 700", flt(ipb.closing_qty) == 70 and flt(ipb.closing_value) == 700,
		f"{ipb.closing_qty}/{ipb.closing_value}")

	# --- second receipt 50 @ 20 -> MAP 14.166667
	pr2 = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": get_company(), "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "items": [{
			"item_code": ITEM, "qty": 50, "rate": 20, "warehouse": wh,
		}],
	})
	pr2.insert(ignore_permissions=True)
	pr2.submit()
	ipb = frappe.get_all("Inventory Period Balance",
		filters={"company": get_company(), "item_code": ITEM}, fields=["*"], limit=1)[0]
	check("IPB qty 120 value 1700", flt(ipb.closing_qty) == 120 and flt(ipb.closing_value) == 1700,
		f"{ipb.closing_qty}/{ipb.closing_value}")
	check("MAP 14.1667", flt(ipb.moving_avg_price, 4) == 14.1667, str(ipb.moving_avg_price))
	check("counter 150", flt(ipb.total_received_since_zero) == 150, str(ipb.total_received_since_zero))

	# GL stock account net == inventory value
	from periodic_valuation.shared.accounts import get_inventory_account
	inv_acc = get_inventory_account(get_company(), ITEM, wh)
	# scope to THIS item's tagged legs - the inventory account is shared site-wide
	net = frappe.db.sql(
		"""SELECT COALESCE(SUM(g.debit - g.credit), 0) FROM `tabGL Entry` g
		JOIN `tabInventory Valuation Event` e ON e.name = g.valuation_event_id
		WHERE g.company = %s AND g.account = %s AND g.is_cancelled = 0 AND e.item_code = %s""",
		(get_company(), inv_acc, ITEM),
	)[0][0]
	check("GL inventory net == IPB closing value", flt(net, 2) == flt(ipb.closing_value, 2),
		f"gl {net} vs ipb {ipb.closing_value}")

	# direct cancel must be blocked
	try:
		pr2.cancel()
		check("direct cancel blocked", False, "cancel succeeded")
	except frappe.ValidationError as e:
		check("direct cancel blocked", "Create Cancellation" in str(e) or "Cancellation" in str(e))

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("kernel smoke failures: " + "; ".join(c[0] for c in failed))
