# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Immutability guards for the periodic-valuation event log doctypes.

Legal rows (Stock Movement Event, Inventory Valuation Event, snapshots) are
append-only: they may be inserted by the posting kernel and never modified or
deleted afterwards. Corrections post new rows linked via ``reversal_of``.
"""

import frappe
from frappe import _

KERNEL_FLAG = "via_periodic_valuation_kernel"


def kernel_only_insert(doc, method=None):
	"""before_insert guard: rows are created by the posting kernel, not by hand."""
	if not (frappe.flags.get(KERNEL_FLAG) or frappe.flags.in_install or frappe.flags.in_patch or frappe.flags.in_migrate or frappe.flags.in_test):
		frappe.throw(
			_("{0} rows are created by the periodic valuation posting kernel and cannot be entered manually.").format(_(doc.doctype)),
			title=_("Immutable Ledger"),
		)


def block_update(doc, method=None):
	"""Immutability guard: no field of a persisted legal row may change.

	MUST be called from validate() - it runs BEFORE the database write, so the
	block holds even when the caller catches the exception inside a larger
	transaction. (on_update fires after the write and is advisory only.)
	"""
	if doc.is_new() or doc.flags.in_insert:
		return
	if frappe.flags.get(KERNEL_FLAG) and getattr(doc, "_kernel_allowed_update", False):
		# The kernel may flip is_cancelled as part of a reversal pairing; nothing else.
		return
	frappe.throw(
		_("{0} is part of the immutable valuation ledger and cannot be modified. Post a reversal instead.").format(doc.name),
		title=_("Immutable Ledger"),
	)


def block_delete(doc, method=None):
	if frappe.flags.in_install or frappe.flags.in_uninstall or frappe.flags.in_patch:
		return
	frappe.throw(
		_("{0} is part of the immutable valuation ledger and cannot be deleted.").format(doc.name),
		title=_("Immutable Ledger"),
	)
