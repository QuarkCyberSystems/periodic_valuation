# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Immutability guards for the periodic-valuation event log doctypes.

Legal rows (Stock Movement Event, Inventory Valuation Event, snapshots) are
append-only: they may be inserted by the posting kernel and never modified or
deleted afterwards. Corrections post new rows linked via ``reversal_of``.

Who may INSERT a row is this app's rule and stays here. That a persisted row
never changes or disappears is the platform's: the three doctypes are
registered as this app's ``ledger_doctypes`` (platform.py) and the dispatcher
refuses updates and deletes, asking ``rows_may_change`` for the one exception
(the kernel flipping ``is_cancelled`` during reversal pairing).
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
