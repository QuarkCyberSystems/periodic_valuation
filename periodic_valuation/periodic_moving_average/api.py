# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.utils import flt, getdate


@frappe.whitelist()
def get_current_state(company, item_code, warehouse=None, posting_date=None):
	"""Period-balance state for a valuation scope (form helpers).

	With `posting_date`, returns the balance AS OF that date's period — the
	newest period not after it. Without it, the latest period.

	A backdated Stock Count must show the prior month's on-hand, not today's:
	the server values the difference against the posting period (see
	StockCount.set_current_state), so a form that fetched the latest period
	showed the counter a system quantity that did not match the one the
	posting would use (WA-0003-01 item 8). Both paths now resolve the balance
	here, so they cannot drift apart again.
	"""
	frappe.has_permission("Inventory Period Balance", "read", throw=True)
	include_wh = frappe.get_cached_value("Item", item_code, "valuation_includes_warehouse")
	rows = frappe.get_all(
		"Inventory Period Balance",
		filters={
			"company": company,
			"item_code": item_code,
			"warehouse": (warehouse or "") if include_wh else "",
		},
		fields=["closing_qty", "closing_value", "moving_avg_price", "is_negative", "frozen_map",
			"period_year", "period_month"],
		order_by="period_year desc, period_month desc",
		limit=1 if not posting_date else 60,
	)
	if posting_date:
		d = getdate(posting_date)
		rows = [r for r in rows if (r.period_year, r.period_month) <= (d.year, d.month)]
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
