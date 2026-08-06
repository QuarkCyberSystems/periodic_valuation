# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Create Cancellation — the only legal way to undo a posted document that
contains periodic-valuation items (signed MAP plan; May 6 universal rule).

Creates a new draft of the SAME doctype with is_cancellation = 1 and
cancellation_against set, items copied from the original, posting_date
defaulted to today. On submit the kernel posts dated mirror events; both
documents survive at docstatus 1.
"""

import frappe
from frappe import _
from frappe.utils import flt, nowdate

CANCELLABLE = (
	"Purchase Receipt",
	"Delivery Note",
	"Stock Entry",
	"Purchase Invoice",
	"Sales Invoice",
	"Subcontracting Receipt",
	"Landed Cost Voucher",
)


@frappe.whitelist()
def make_cancellation(doctype, name):
	if doctype not in CANCELLABLE:
		frappe.throw(_("{0} does not support Cancellation documents.").format(_(doctype)))

	original = frappe.get_doc(doctype, name)
	original.check_permission("cancel")

	if original.docstatus != 1:
		frappe.throw(_("Only submitted documents can be cancelled."))
	if original.get("is_cancellation"):
		frappe.throw(_("{0} is itself a Cancellation document.").format(name))
	if frappe.db.exists(
		doctype, {"cancellation_against": name, "is_cancellation": 1, "docstatus": ("<", 2)}
	):
		frappe.throw(
			_("A Cancellation document already exists for {0}.").format(name),
			title=_("Double Reversal Blocked"),
		)

	_block_if_has_dependents(doctype, name, original)

	if doctype in ("Purchase Invoice", "Sales Invoice"):
		# A PI/SI reversal is a debit/credit note: this reverses the party
		# accounting (creditor/debtor, SRBNB/GRIR) and nets the receipt's
		# billing status natively, so the receipt returns to 'To Bill' and can
		# be re-invoiced (WA-0003-01 item 10). A positive copy would instead
		# re-post the party GL and over-bill the receipt. The valuation-side
		# events are still reversed by on_purchase_invoice_submit's
		# is_cancellation branch.
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		cancellation = make_return_doc(doctype, name)
	else:
		cancellation = frappe.copy_doc(original)
	cancellation.is_cancellation = 1
	cancellation.cancellation_against = name
	cancellation.posting_date = nowdate()
	_retype_reversal(cancellation)
	if cancellation.meta.has_field("set_posting_time"):
		cancellation.set_posting_time = 1
	# a reversal carries the ORIGINAL's dates for valuation, but its own
	# supplier-invoice/bill dates must not trip forward-looking validations
	# (e.g. "Due Date cannot be before Supplier Invoice Date", WA-0003-01 #13)
	for fld in ("bill_date", "bill_no", "due_date"):
		if cancellation.meta.has_field(fld):
			cancellation.set(fld, None)
	cancellation.flags.ignore_permissions = False
	cancellation.insert()
	return cancellation.name


def _retype_reversal(cancellation):
	"""Label a Stock Entry reversal with its own transaction type.

	A reversal of a Material Issue is mechanically still an issue-shaped Stock
	Entry — the kernel decides direction from `is_cancellation`, not from the
	purpose — but a document that reverses an issue should not announce itself
	as "Material Issue" (WA-0003-01 item 5).

	The reversal Stock Entry Types carry the SAME `purpose` as the originals
	(Stock Entry.purpose is read-only and fetched from the type), so this
	changes the label only: every ERPNext validation, warehouse rule and
	downstream report behaves exactly as before.
	"""
	if cancellation.doctype != "Stock Entry":
		return
	from periodic_valuation.setup.custom_fields import REVERSAL_STOCK_ENTRY_TYPES

	target = {v: k for k, v in REVERSAL_STOCK_ENTRY_TYPES.items()}.get(cancellation.purpose)
	if not target or not frappe.db.exists("Stock Entry Type", target):
		return          # type not seeded (older site) — keep the original label
	cancellation.stock_entry_type = target


def _still_standing(doctype, names):
	"""Of `names`, the ones not already reversed.

	A dependent that has itself been reversed no longer stands, so it must not
	block the parent — otherwise the sanctioned order the client asked for
	("reverse the return, then reverse the original") is impossible: the return
	stays docstatus 1 forever (immutable ledger), so a guard keyed on docstatus
	alone never clears (WA-0003-01 item 11).
	"""
	if not names:
		return []
	reversed_ones = set(
		frappe.get_all(
			doctype,
			filters={"cancellation_against": ("in", list(names)), "is_cancellation": 1,
				"docstatus": 1},
			pluck="cancellation_against",
		)
	)
	return [n for n in names if n not in reversed_ones]


def _block_if_has_dependents(doctype, name, original):
	"""Warn-and-block (client decision 2026-07): a document cannot be reversed
	while dependent documents still stand — the user must reverse those first
	(WA-0003-01 #11 returns, #12 invoices). Prevents the dangling-invoice /
	dangling-return corruption seen in UAT."""
	# (a) returns raised against this document
	# (Stock Entry has is_return but no return_against — guard on the
	# field actually queried, or every Stock Entry reversal crashes.)
	if original.meta.has_field("return_against"):
		returns = frappe.get_all(
			doctype,
			filters={"return_against": name, "docstatus": 1, "is_cancellation": 0},
			pluck="name",
		)
		returns = _still_standing(doctype, returns)
		if returns:
			frappe.throw(
				_(
					"{0} has return document(s) against it ({1}). Reverse the "
					"return(s) first, then reverse this document."
				).format(name, ", ".join(returns)),
				title=_("Reverse Dependents First"),
			)

	# (b) this receipt/delivery has been invoiced
	if flt(original.get("per_billed")) > 0:
		child_map = {
			"Purchase Receipt": ("Purchase Invoice Item", "purchase_receipt"),
			"Delivery Note": ("Sales Invoice Item", "delivery_note"),
		}
		if doctype in child_map:
			child_dt, link = child_map[doctype]
			invoices = frappe.get_all(
				child_dt,
				filters={link: name, "docstatus": 1},
				pluck="parent",
			)
			invoices = _still_standing(child_dt.replace(" Item", ""), sorted(set(invoices)))
			if invoices:
				frappe.throw(
					_(
						"{0} has been invoiced ({1}). Reverse the invoice(s) first, "
						"then reverse this receipt."
					).format(name, ", ".join(invoices)),
					title=_("Reverse Dependents First"),
				)
