# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.utils import flt


@frappe.whitelist()
def get_current_state(company, item_code, warehouse=None):
	"""Current period-balance state for a valuation scope (form helpers)."""
	frappe.has_permission("Inventory Period Balance", "read", throw=True)
	include_wh = frappe.get_cached_value("Item", item_code, "valuation_includes_warehouse")
	rows = frappe.get_all(
		"Inventory Period Balance",
		filters={
			"company": company,
			"item_code": item_code,
			"warehouse": (warehouse or "") if include_wh else "",
		},
		fields=["closing_qty", "closing_value", "moving_avg_price", "is_negative", "frozen_map"],
		order_by="period_year desc, period_month desc",
		limit=1,
	)
	if not rows:
		return {"closing_qty": 0, "closing_value": 0, "moving_avg_price": 0, "is_negative": 0, "frozen_map": 0}
	r = rows[0]
	return {
		"closing_qty": flt(r.closing_qty),
		"closing_value": flt(r.closing_value),
		"moving_avg_price": flt(r.moving_avg_price),
		"is_negative": r.is_negative,
		"frozen_map": flt(r.frozen_map),
	}
