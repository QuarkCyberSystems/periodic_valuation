# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Which documents this app owns rows on (May 6 decision).

`has_routed_items` is the one answer: a document carrying at least one item
on a periodic valuation method. The refusal of direct cancellation that used
to live here is now the answer of this app's LedgerAdapter (platform.py),
raised by qcs_platform's dispatcher in one dialog with every other owner's.
"""

import frappe
from frappe.utils import cstr
from frappe import _


def get_kernel_methods():
	from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS

	return KERNEL_VALUATION_METHODS


def has_routed_items(doc):
	from erpnext.stock.utils import get_valuation_method

	kernel_methods = get_kernel_methods()
	for row in doc.get("items") or []:
		item_code = row.get("item_code")
		if not item_code:
			continue
		if get_valuation_method(item_code, doc.get("company")) in kernel_methods:
			return True
	return False


@frappe.whitelist()
def is_routed_document(doctype: str, name: str) -> bool:
	"""True when the submitted document carries at least one periodic-valuation
	item. Used by the client to swap the cancellation UX: routed documents hide
	core Cancel and show Create Cancellation; standard documents keep core
	ERPNext behaviour untouched."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return has_routed_items(doc)


def stamp_settlement_view(doc, method=None):
	"""Item validate hook: copy the group default onto a blank Periodic
	Standard Cost item so the operative config is always visible on the item
	(defaults-as-templates, DR-22). Company default stamps at first posting."""
	if doc.valuation_method != "Periodic Standard Cost" or doc.settlement_view in ("MTD", "YTD"):
		return
	group_view = frappe.db.get_value("Item Group", doc.item_group, "default_settlement_view")
	if group_view in ("MTD", "YTD"):
		doc.settlement_view = group_view


PERIODIC_METHODS = ("Periodic Moving Average", "Periodic Standard Cost")
LOCKED_AFTER_TRANSACTION = ("valuation_includes_warehouse", "settlement_view")


def validate_periodic_item(doc, method=None):
	"""Item validate (re-homed from the fork's Item controller, D-029 §7 step 4):
	a periodic method needs this app's kernel, supports no batch or serial
	valuation, and reminds the user to configure settings; after the first
	submitted transaction the two valuation fields are locked, and a periodic
	item never leaves its method - upstream's cant_change allows any method
	to become Moving Average, which would silently re-value a routed item's
	ledger as non-routed."""
	_validate_periodic_valuation_method(doc)
	_lock_after_transaction(doc)


def _validate_periodic_valuation_method(doc):
	if doc.valuation_method not in PERIODIC_METHODS:
		return
	if doc.has_batch_no or doc.has_serial_no:
		frappe.throw(
			_(
				"Valuation Method {0} does not support batch or serial valuation in this release. "
				"Disable Has Batch No / Has Serial No, or use a core valuation method."
			).format(frappe.bold(doc.valuation_method))
		)
	if doc.valuation_method == "Periodic Moving Average" and not frappe.db.exists("Periodic Moving Average Settings", {}):
		frappe.msgprint(
			_(
				"No Periodic Moving Average Settings exist yet. Configure them (and Inventory Periods) "
				"before posting transactions for this item."
			),
			indicator="orange",
		)


def _lock_after_transaction(doc):
	if doc.is_new():
		return
	before = frappe.db.get_value("Item", doc.name, ("valuation_method",) + LOCKED_AFTER_TRANSACTION, as_dict=True)
	if not before:
		return
	changed = [f for f in LOCKED_AFTER_TRANSACTION if cstr(doc.get(f)) != cstr(before.get(f))]
	if before.valuation_method in PERIODIC_METHODS and cstr(doc.valuation_method) != cstr(before.valuation_method):
		changed.append("valuation_method")
	if not changed:
		return
	if linked := doc._get_linked_submitted_documents(changed):
		labels = ", ".join(frappe.bold(_(doc.meta.get_label(f))) for f in changed)
		msg = _(
			"As there are existing submitted transactions against item {0}, you can not change the value of {1}."
		).format(doc.name, labels)
		if isinstance(linked, dict):
			msg += "<br>" + _("Example of a linked document: {0}").format(frappe.get_desk_link(linked.doctype, linked.docname))
		frappe.throw(msg, title=_("Cannot Change"))
