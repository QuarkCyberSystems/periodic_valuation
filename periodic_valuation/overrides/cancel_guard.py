# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Universal cancellation rule for periodic-valuation items (May 6 decision).

Direct cancellation (docstatus 1 -> 2) is NEVER allowed for a document that
contains routed items — even same-period with no downstream consumption,
because an intervening revaluation could have landed. The user is redirected
to Create Cancellation, which posts a dated reversal document of the same
doctype and preserves the immutable ledger.
"""

import frappe
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


def block_direct_cancel(doc, method=None):
	if not has_routed_items(doc):
		return
	frappe.throw(
		_(
			"This document contains periodic-valuation items. Direct cancellation would mutate the "
			"immutable ledger. Use <b>Create Cancellation</b> to post a dated reversal instead."
		),
		title=_("Cancellation Blocked"),
	)


def stamp_settlement_view(doc, method=None):
	"""Item validate hook: copy the group default onto a blank Periodic
	Standard Cost item so the operative config is always visible on the item
	(defaults-as-templates, DR-22). Company default stamps at first posting."""
	if doc.valuation_method != "Periodic Standard Cost" or doc.settlement_view in ("MTD", "YTD"):
		return
	group_view = frappe.db.get_value("Item Group", doc.item_group, "default_settlement_view")
	if group_view in ("MTD", "YTD"):
		doc.settlement_view = group_view
