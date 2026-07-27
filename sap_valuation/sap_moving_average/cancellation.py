# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Create Cancellation — the only legal way to undo a posted document that
contains SAP-valuation items (signed MAP plan; May 6 universal rule).

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

	cancellation = frappe.copy_doc(original)
	cancellation.is_cancellation = 1
	cancellation.cancellation_against = name
	cancellation.posting_date = nowdate()
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


def _block_if_has_dependents(doctype, name, original):
	"""Warn-and-block (client decision 2026-07): a document cannot be reversed
	while dependent documents still stand — the user must reverse those first
	(WA-0003-01 #11 returns, #12 invoices). Prevents the dangling-invoice /
	dangling-return corruption seen in UAT."""
	# (a) returns raised against this document
	if original.meta.has_field("is_return"):
		returns = frappe.get_all(
			doctype,
			filters={"return_against": name, "docstatus": 1, "is_cancellation": 0},
			pluck="name",
		)
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
			invoices = sorted(set(invoices))
			if invoices:
				frappe.throw(
					_(
						"{0} has been invoiced ({1}). Reverse the invoice(s) first, "
						"then reverse this receipt."
					).format(name, ", ".join(invoices)),
					title=_("Reverse Dependents First"),
				)
