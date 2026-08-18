# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.utils import flt, getdate


@frappe.whitelist()
def get_current_state(company, item_code, warehouse=None, posting_date=None, physical=0):
	"""Period-balance state for a valuation scope (form helpers).

	With `posting_date`, returns the balance AS OF that date's period — the
	newest period not after it. Without it, the latest period.

	A backdated Stock Count must show the prior month's on-hand, not today's:
	the server values the difference against the posting period (see
	StockCount.set_current_state), so a form that fetched the latest period
	showed the counter a system quantity that did not match the one the
	posting would use (WA-0003-01 item 8). Both paths now resolve the balance
	here, so they cannot drift apart again.

	`physical=1` returns closing_qty as the PHYSICAL quantity in the given
	warehouse (Stock Ledger sum as of the date) instead of the valuation-scope
	total, while rate/value context stays scope-level. A Stock Count or Stock
	Reconciliation is a physical exercise on ONE warehouse: for a company-scope
	item stocked in several warehouses, comparing the counted quantity against
	the scope total produced garbage differences — counting 55 in a warehouse
	holding 60, while 40 sat elsewhere, showed current_qty 100 and posted a
	-45 adjustment for a real loss of 5 (client approval comment 1, 2026-08-18:
	valuation scope must be consistently reflected). Stock Revaluation stays
	scope-level by design — value has no warehouse under a company scope.
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
		state = {"closing_qty": 0, "closing_value": 0, "moving_avg_price": 0, "is_negative": 0, "frozen_map": 0}
	else:
		r = rows[0]
		state = {
			"closing_qty": flt(r.closing_qty),
			"closing_value": flt(r.closing_value),
			"moving_avg_price": flt(r.moving_avg_price),
			"is_negative": r.is_negative,
			"frozen_map": flt(r.frozen_map),
		}
	if frappe.utils.cint(physical) and warehouse:
		state["closing_qty"] = get_physical_qty(item_code, warehouse, posting_date)
	return state


def get_physical_qty(item_code, warehouse, posting_date=None):
	"""Physical on-hand of ONE warehouse as of a POSTING PERIOD: the Stock
	Ledger sum through that period's end.

	Period granularity, not date granularity — the count values its difference
	against the posting period's balance (the item-8 rule), and the scope-side
	lookup this pairs with resolves whole periods. A literal date cutoff made a
	backdated mid-month count exclude same-month postings the period balance
	includes, so the two sides of the comparison disagreed (caught by the STD
	workbook replays' backdated February count). Every kernel posting writes an
	SLE row (DR-02), so the sum is complete for routed items.
	"""
	from frappe.utils import get_last_day

	cond = "item_code = %(item)s AND warehouse = %(wh)s AND is_cancelled = 0"
	params = {"item": item_code, "wh": warehouse}
	if posting_date:
		cond += " AND posting_date <= %(as_of)s"
		params["as_of"] = get_last_day(getdate(posting_date))
	return flt(
		frappe.db.sql(
			f"SELECT COALESCE(SUM(actual_qty), 0) FROM `tabStock Ledger Entry` WHERE {cond}",
			params,
		)[0][0]
	)


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
# ERPNext vocabulary, not SAP's. "Return Delivery" is SAP's term for movement
# type 122 (return to vendor), but in ERPNext it reads as something to do with a
# Delivery Note — i.e. a sales return, the opposite of what it is.
MOVEMENT_LABELS = {
	"receipt": "Material Receipt",
	"issue": "Material Issue",
	"transfer_in": "Material Transfer (In)",
	"transfer_out": "Material Transfer (Out)",
	"return_in": "Sales Return",
	"return_out": "Purchase Return",
	"count_gain": "Stock Count (Gain)",
	"count_loss": "Stock Count (Loss)",
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


@frappe.whitelist()
def get_uninvoiced_qty(purchase_receipt):
	"""How many units of a receipt are still uninvoiced, by QUANTITY.

	ERPNext derives per_billed from billed AMOUNT
	(purchase_receipt.py: percent_billed = 100 * billed_amount / amount), and
	the form only offers Create > Purchase Invoice while per_billed < 100. So
	invoicing part of the quantity at a higher rate can cover the receipt's
	whole value, mark it 100% billed, and strip the action off the form while
	units remain uninvoiced — the receipt becomes impossible to finish billing
	from the UI (WA-0003-01 item 15; the client's MAT-PRE-2026-00039 invoiced
	3,000 of 5,000 at double rate).

	Quantity is the honest measure of "is there anything left to bill", so it
	is what the button is gated on. Reversal credit notes carry negative qty
	against the same pr_detail, so they net off here exactly as they do in
	per_billed.
	"""
	frappe.has_permission("Purchase Receipt", "read", doc=purchase_receipt, throw=True)
	rows = frappe.get_all(
		"Purchase Receipt Item",
		filters={"parent": purchase_receipt, "docstatus": 1},
		fields=["name", "item_code", "qty"],
	)
	if not rows:
		return {"remaining": 0.0, "rows": []}

	invoiced = {}
	for r in frappe.get_all(
		"Purchase Invoice Item",
		filters={"pr_detail": ("in", [r.name for r in rows]), "docstatus": 1},
		fields=["pr_detail", "qty"],
	):
		invoiced[r.pr_detail] = flt(invoiced.get(r.pr_detail, 0)) + flt(r.qty)

	detail, remaining = [], 0.0
	for r in rows:
		left = flt(r.qty) - flt(invoiced.get(r.name, 0))
		if left > 0:
			remaining += left
		detail.append({
			"item_code": r.item_code,
			"received_qty": flt(r.qty),
			"invoiced_qty": flt(invoiced.get(r.name, 0)),
			"remaining_qty": left,
		})
	return {"remaining": remaining, "rows": detail}
