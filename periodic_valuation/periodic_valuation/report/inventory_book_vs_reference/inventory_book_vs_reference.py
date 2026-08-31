# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Why the Stock Balance report and the General Ledger disagree for periodic
valuation items: the stock ledger carries the REFERENCE value (qty x period
cost) while the GL and the period ledger carry the BOOK value, which includes
the settlement variance capitalized into inventory. This report shows both per
item/warehouse/period, the capitalized amount that separates them, and a check
column that must be zero."""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 160},
		{"fieldname": "warehouse", "label": _("Warehouse"), "fieldtype": "Link", "options": "Warehouse", "width": 130},
		{"fieldname": "valuation_method", "label": _("Method"), "fieldtype": "Data", "width": 110},
		{"fieldname": "settlement_view", "label": _("View"), "fieldtype": "Data", "width": 60},
		{"fieldname": "closing_qty", "label": _("Closing Qty"), "fieldtype": "Float", "width": 100},
		{"fieldname": "unit_cost", "label": _("Period Cost"), "fieldtype": "Currency", "width": 100},
		{"fieldname": "reference_value", "label": _("Reference Value"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "capitalized_variance", "label": _("Capitalized Variance"), "fieldtype": "Currency", "width": 140},
		{"fieldname": "book_value", "label": _("Book Value"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "stock_ledger_value", "label": _("Stock Ledger Value"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "check", "label": _("Check (must be 0)"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "settlement", "label": _("Settlement"), "fieldtype": "Link", "options": "Inventory Period Settlement", "width": 150},
	]


def get_data(filters):
	conditions = ["ipb.company = %(company)s", "ipb.period_year = %(period_year)s",
		"ipb.period_month = %(period_month)s"]
	for field in ("item_code", "warehouse"):
		if filters.get(field):
			conditions.append(f"ipb.{field} = %({field})s")
	if filters.get("valuation_method"):
		conditions.append("i.valuation_method = %(valuation_method)s")

	rows = frappe.db.sql(
		f"""
		SELECT ipb.item_code, ipb.warehouse, i.valuation_method,
			ipb.resolved_settlement_view AS settlement_view,
			ipb.closing_qty, ipb.closing_value, ipb.closing_reference_value,
			ipb.settlement_inventory_total, ipb.period_standard_cost,
			ipb.moving_avg_price, ipb.settlement
		FROM `tabInventory Period Balance` ipb
		JOIN `tabItem` i ON i.name = ipb.item_code
		WHERE {" AND ".join(conditions)}
		ORDER BY ipb.item_code, ipb.warehouse
		""",
		filters,
		as_dict=True,
	)

	sle_values = get_stock_ledger_values(filters)
	data = []
	for r in rows:
		if not filters.get("show_zero") and not flt(r.closing_qty) and not flt(r.closing_value):
			continue
		is_std = r.valuation_method == "Periodic Standard Cost"
		unit = flt(r.period_standard_cost) if is_std else flt(r.moving_avg_price)
		reference = (
			flt(r.closing_reference_value)
			if is_std and flt(r.closing_reference_value)
			else flt(flt(r.closing_qty) * unit, 2)
		)
		capitalized = flt(r.settlement_inventory_total) if is_std else 0.0
		data.append({
			"item_code": r.item_code,
			"warehouse": r.warehouse,
			"valuation_method": r.valuation_method,
			"settlement_view": r.settlement_view,
			"closing_qty": flt(r.closing_qty),
			"unit_cost": unit,
			"reference_value": reference,
			"capitalized_variance": capitalized,
			"book_value": flt(r.closing_value, 2),
			# a company-scope balance row carries no warehouse; the stock ledger
			# always does - fall back to the item's total across warehouses
			"stock_ledger_value": (
				sle_values.get((r.item_code, r.warehouse), 0.0)
				if r.warehouse
				else flt(sum(v for (it, _wh), v in sle_values.items() if it == r.item_code), 2)
			),
			"check": flt(flt(r.closing_value) - reference - capitalized, 2),
			"settlement": r.settlement,
		})
	return data


def get_stock_ledger_values(filters):
	"""SLE value to the period end - the figure Stock Balance shows."""
	last_day = frappe.utils.get_last_day(
		f"{filters.period_year}-{int(filters.period_month):02d}-01"
	)
	rows = frappe.db.sql(
		"""
		SELECT item_code, warehouse, SUM(stock_value_difference) AS value
		FROM `tabStock Ledger Entry`
		WHERE company = %(company)s AND is_cancelled = 0 AND posting_date <= %(last_day)s
		GROUP BY item_code, warehouse
		""",
		{"company": filters.company, "last_day": last_day},
		as_dict=True,
	)
	return {(r.item_code, r.warehouse or ""): flt(r.value, 2) for r in rows}
