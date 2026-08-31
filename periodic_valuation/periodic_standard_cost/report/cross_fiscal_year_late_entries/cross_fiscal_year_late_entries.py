# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Back-year entries and their bridges. A (BY) primary books quantity into the
prior fiscal year at the prior year's standard cost; its companion posts the
cost difference into the CURRENT period's pool, and the current period's
settlement absorbs it (the prior year stays untouched). One row per primary,
with the companion and the absorbing settlement alongside."""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{"fieldname": "event", "label": _("Event"), "fieldtype": "Link", "options": "Inventory Valuation Event", "width": 150},
		{"fieldname": "std_trans", "label": _("Movement"), "fieldtype": "Data", "width": 90},
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "original_period", "label": _("Original Period"), "fieldtype": "Data", "width": 110},
		{"fieldname": "entry_date", "label": _("Entered On"), "fieldtype": "Date", "width": 100},
		{"fieldname": "source", "label": _("Document"), "fieldtype": "Dynamic Link", "options": "source_doctype", "width": 160},
		{"fieldname": "qty_adj", "label": _("Qty"), "fieldtype": "Float", "width": 80},
		{"fieldname": "prior_sc", "label": _("Prior-FY Cost"), "fieldtype": "Currency", "width": 105},
		{"fieldname": "total_sc", "label": _("Booked Value"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "bridge_event", "label": _("Bridge Event"), "fieldtype": "Link", "options": "Inventory Valuation Event", "width": 150},
		{"fieldname": "bridge_period", "label": _("Bridge Period"), "fieldtype": "Data", "width": 100},
		{"fieldname": "bridge_amount", "label": _("Bridge Amount"), "fieldtype": "Currency", "width": 115},
		{"fieldname": "absorbing_settlement", "label": _("Absorbed By"), "fieldtype": "Link", "options": "Inventory Period Settlement", "width": 150},
	]


def get_data(filters):
	conditions = ["e.company = %(company)s", "e.is_cancelled = 0",
		"e.std_trans IN ('REC (BY)', 'Issue (BY)')"]
	if filters.get("fiscal_year"):
		conditions.append("e.period_year = %(fiscal_year)s")
	if filters.get("item_code"):
		conditions.append("e.item_code = %(item_code)s")

	primaries = frappe.db.sql(
		f"""
		SELECT e.name, e.std_trans, e.item_code, e.warehouse, e.period_year,
			e.period_month, e.entry_date, e.source_doctype, e.source_docname,
			e.qty_adj, e.standard_cost, e.total_sc
		FROM `tabInventory Valuation Event` e
		WHERE {" AND ".join(conditions)}
		ORDER BY e.period_year, e.period_month, e.creation
		""",
		filters,
		as_dict=True,
	)

	data = []
	for p in primaries:
		bridge = frappe.db.get_value(
			"Inventory Valuation Event",
			{
				"company": filters.company, "item_code": p.item_code,
				"source_docname": p.source_docname, "is_cancelled": 0,
				"std_trans": f"{p.std_trans} - Rev",
			},
			["name", "period_year", "period_month", "total_sc"],
			as_dict=True,
		)
		absorbing = None
		if bridge:
			absorbing = frappe.db.get_value(
				"Inventory Period Settlement",
				{
					"company": filters.company, "item_code": p.item_code,
					"period_year": bridge.period_year, "period_month": bridge.period_month,
					"cancelled": 0,
				},
			)
		data.append({
			"event": p.name,
			"std_trans": p.std_trans,
			"item_code": p.item_code,
			"original_period": f"{p.period_year}-{p.period_month:02d}",
			"entry_date": p.entry_date,
			"source_doctype": p.source_doctype,
			"source": p.source_docname,
			"qty_adj": flt(p.qty_adj),
			"prior_sc": flt(p.standard_cost, 2),
			"total_sc": flt(p.total_sc, 2),
			"bridge_event": bridge and bridge.name,
			"bridge_period": bridge and f"{bridge.period_year}-{bridge.period_month:02d}",
			"bridge_amount": bridge and flt(bridge.total_sc, 2),
			"absorbing_settlement": absorbing,
		})
	return data
