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


# SAP-style movement labels. ERPNext has no movement-type field on Purchase
# Receipt / Delivery Note — direction is spread across is_return, is_cancellation
# and the quantity sign — so a reversal of a return reads as "-500" with nothing
# saying what it is. SAP shows a positive quantity plus a movement type (122
# return delivery, 123 its reversal); the client reads documents that way, hence
# "reverse of the purchase return should be + qty" (WA-0003-01 item 9).
#
# DERIVED, not stamped: the Stock Movement Event already records the movement
# authoritatively. Copying it onto the document would be a third shadow to keep
# in step, and today's SLE gap is what happens when a shadow is not maintained.
# Deriving also means every historical document gets a label with no migration.
MOVEMENT_LABELS = {
	"receipt": "Goods Receipt",
	"issue": "Goods Issue",
	"transfer_in": "Transfer In",
	"transfer_out": "Transfer Out",
	"return_in": "Customer Return",
	"return_out": "Return Delivery",
	"count_gain": "Physical Count (gain)",
	"count_loss": "Physical Count (loss)",
	"cancellation": "Cancellation",
}


@frappe.whitelist()
def get_movement_summary(doctype, docname):
	"""What movement(s) did this document actually post? Read from the event log.

	Returns {"label": <str or None>, "detail": [...]} — `label` is the headline
	for the form, e.g. "Cancellation of Return Delivery".
	"""
	frappe.has_permission("Stock Movement Event", "read", throw=True)
	rows = frappe.get_all(
		"Stock Movement Event",
		filters={"source_doctype": doctype, "source_docname": docname},
		fields=["movement_type", "qty_delta", "item_code", "reversal_of"],
		order_by="creation",
	)
	if not rows:
		return {"label": None, "detail": []}

	kinds = []
	for r in rows:
		if r.movement_type not in kinds:
			kinds.append(r.movement_type)

	label = " + ".join(MOVEMENT_LABELS.get(k, k) for k in kinds)

	# A cancellation is only meaningful as "cancellation of WHAT" — resolve the
	# movement it reverses so the document reads the way an SAP 123 does.
	if kinds == ["cancellation"]:
		ive = frappe.get_all(
			"Inventory Valuation Event",
			filters={"source_doctype": doctype, "source_docname": docname,
				"reason_code": "cancellation"},
			fields=["reversal_of"], limit=1,
		)
		orig_movement = None
		if ive and ive[0].reversal_of:
			orig_sme = frappe.db.get_value(
				"Inventory Valuation Event", ive[0].reversal_of, "movement_event_id")
			if orig_sme:
				orig_movement = frappe.db.get_value("Stock Movement Event", orig_sme, "movement_type")
		if orig_movement:
			label = "Cancellation of {0}".format(
				MOVEMENT_LABELS.get(orig_movement, orig_movement))

	return {
		"label": label,
		"detail": [
			{"movement_type": r.movement_type,
			 "label": MOVEMENT_LABELS.get(r.movement_type, r.movement_type),
			 "item_code": r.item_code, "qty": flt(r.qty_delta)}
			for r in rows
		],
	}
