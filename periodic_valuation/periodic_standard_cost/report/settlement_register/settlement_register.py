# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""The month-end audit trail: one row per settlement with its pools, its
inventory / consumption split, whether it still stands, and the events it
posted. Live rows are totalled at the bottom; a reversed settlement stays on
the register (immutable ledger) flagged Reversed."""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{"fieldname": "settlement", "label": _("Settlement"), "fieldtype": "Link", "options": "Inventory Period Settlement", "width": 150},
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "period", "label": _("Period"), "fieldtype": "Data", "width": 80},
		{"fieldname": "settlement_view", "label": _("View"), "fieldtype": "Data", "width": 60},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 90},
		{"fieldname": "ppv_pool", "label": _("PPV Pool"), "fieldtype": "Currency", "width": 100},
		{"fieldname": "rev_pool", "label": _("Reval Pool"), "fieldtype": "Currency", "width": 100},
		{"fieldname": "variance", "label": _("Total Variance"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "input_qty", "label": _("Input Qty"), "fieldtype": "Float", "width": 90},
		{"fieldname": "es_qty", "label": _("Closing Qty"), "fieldtype": "Float", "width": 95},
		{"fieldname": "es_var", "label": _("Inventory Share"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "out_var", "label": _("Consumption Share"), "fieldtype": "Currency", "width": 130},
		{"fieldname": "settlement_run", "label": _("Run"), "fieldtype": "Link", "options": "Inventory Period Settlement Run", "width": 130},
		{"fieldname": "sett_event", "label": _("Sett Event"), "fieldtype": "Link", "options": "Inventory Valuation Event", "width": 140},
		{"fieldname": "sett_rev_event", "label": _("Sett-Rev Event"), "fieldtype": "Link", "options": "Inventory Valuation Event", "width": 140},
		{"fieldname": "reversed_by_events", "label": _("Reversed By"), "fieldtype": "Data", "width": 180},
	]


def get_data(filters):
	conditions = ["s.company = %(company)s"]
	if filters.get("period_year"):
		conditions.append("s.period_year = %(period_year)s")
	if filters.get("period_month"):
		conditions.append("s.period_month = %(period_month)s")
	if filters.get("item_code"):
		conditions.append("s.item_code = %(item_code)s")
	if filters.get("settlement_view"):
		conditions.append("s.settlement_view = %(settlement_view)s")
	if filters.get("status") == "Live":
		conditions.append("s.cancelled = 0")
	elif filters.get("status") == "Reversed":
		conditions.append("s.cancelled = 1")

	rows = frappe.db.sql(
		f"""
		SELECT s.name AS settlement, s.item_code, s.period_year, s.period_month,
			s.settlement_view, s.cancelled, s.ppv_pool, s.rev_pool, s.variance,
			s.beg_qty, s.in_qty, s.es_qty, s.es_var, s.out_var,
			s.settlement_run, s.sett_event, s.sett_rev_event, s.reversed_by_events
		FROM `tabInventory Period Settlement` s
		WHERE {" AND ".join(conditions)}
		ORDER BY s.period_year, s.period_month, s.item_code, s.creation
		""",
		filters,
		as_dict=True,
	)

	data = []
	live = {"variance": 0.0, "es_var": 0.0, "out_var": 0.0, "count": 0}
	for r in rows:
		row = {
			"settlement": r.settlement,
			"item_code": r.item_code,
			"period": f"{r.period_year}-{r.period_month:02d}",
			"settlement_view": r.settlement_view,
			"status": _("Reversed") if r.cancelled else _("Live"),
			"ppv_pool": flt(r.ppv_pool, 2),
			"rev_pool": flt(r.rev_pool, 2),
			"variance": flt(r.variance, 2),
			"input_qty": flt(r.beg_qty) + flt(r.in_qty),
			"es_qty": flt(r.es_qty),
			"es_var": flt(r.es_var, 2),
			"out_var": flt(r.out_var, 2),
			"settlement_run": r.settlement_run,
			"sett_event": r.sett_event,
			"sett_rev_event": r.sett_rev_event,
			"reversed_by_events": r.reversed_by_events,
		}
		data.append(row)
		if not r.cancelled:
			live["variance"] += flt(r.variance)
			live["es_var"] += flt(r.es_var)
			live["out_var"] += flt(r.out_var)
			live["count"] += 1

	if data:
		data.append({
			"settlement": _("Live totals ({0})").format(live["count"]),
			"variance": flt(live["variance"], 2),
			"es_var": flt(live["es_var"], 2),
			"out_var": flt(live["out_var"], 2),
			"bold": 1,
		})
	return data
