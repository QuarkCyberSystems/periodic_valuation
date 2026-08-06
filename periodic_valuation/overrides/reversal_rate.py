# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Keep a Cancellation document's displayed rate equal to the original's.

`make_cancellation` copies the source document, so the reversal starts with the
original rates. ERPNext's Stock Entry then recalculates them: `validate()` calls
`calculate_rate_and_amount(reset_outgoing_rate=True)`, and
`set_rate_for_outgoing_items` overwrites `basic_rate` on every row carrying an
`s_warehouse` with `get_incoming_rate()` — i.e. the valuation at *reversal* time.

The result is a document whose face contradicts its own posting: the client's
MAT-STE-2026-00029 reversed a 79,411.76 issue while displaying 102,500.00,
because the moving average had moved from 158.82 to 205 in between
(WA-0003-01 item 5).

The design is explicit that a reversal is measured at the original basis —
"a referenced reversal always uses the original STD; a current-date posting at
today's STD is not a reversal", and Sett-Reverse rows are "exact mirrors of the
rows they cancel". MAP UAT TC-D2 likewise expects the balance to move by "the
receipt's original value".

This is display-only: the kernel never reads `basic_rate` (valuation flows from
the IVE/IPB path), which is why the GL was already correct. Restoring the rate
makes the paperwork agree with the ledger.

Runs as a `validate` doc_event, so it re-applies on every save — ERPNext
recalculates on each validate, and doc_events run after the controller method.
"""

import frappe
from frappe.utils import flt


def restore_original_rates(doc, method=None):
	if not doc.get("is_cancellation") or not doc.get("cancellation_against"):
		return
	if doc.doctype != "Stock Entry":
		# Only Stock Entry re-derives its rate from live valuation. Purchase
		# Receipt / Delivery Note carry the transacted price, which copy_doc
		# already preserves.
		return

	try:
		original = frappe.get_doc(doc.doctype, doc.cancellation_against)
	except frappe.DoesNotExistError:
		return

	src_rows = original.get("items") or []
	for idx, row in enumerate(doc.get("items") or []):
		src = src_rows[idx] if idx < len(src_rows) else None
		# copy_doc preserves row order; verify the item before trusting it
		if not src or src.item_code != row.item_code:
			src = next((r for r in src_rows if r.item_code == row.item_code), None)
		if not src:
			continue
		if flt(row.basic_rate) == flt(src.basic_rate):
			continue
		row.basic_rate = flt(src.basic_rate)
		row.basic_amount = flt(flt(row.transfer_qty or row.qty) * flt(src.basic_rate),
			row.precision("basic_amount"))
		if row.meta.has_field("amount"):
			row.amount = flt(flt(row.qty) * flt(src.basic_rate), row.precision("amount"))
		if row.meta.has_field("valuation_rate"):
			row.valuation_rate = flt(src.basic_rate)
