# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from periodic_valuation.shared.immutable import block_delete, block_update, kernel_only_insert


class InventoryValuationEvent(Document):
	def before_insert(self):
		kernel_only_insert(self)
		self.validate_source_reference()

	def validate_source_reference(self):
		# Apr 22 decision: source reference is mandatory on ALL valuation events,
		# including value-only ones (landed cost split rows need source_detail_name).
		if not (self.source_doctype and self.source_docname):
			frappe.throw(
				_("Inventory Valuation Event requires a source document reference (source_doctype + source_docname)."),
				title=_("Missing Source Reference"),
			)

	def validate(self):
		block_update(self)
		if self.is_new() or self.flags.in_insert:
			self.validate_posting_intent()

	def validate_posting_intent(self):
		"""Intent-vs-shape invariants (client appendix, enforced at the ledger
		boundary so every code path and integration is covered):
		- an EXACT_REVERSAL must reference the event it reverses
		- a reversal reference implies the EXACT_REVERSAL intent
		Legacy rows without an intent are tolerated."""
		if self.posting_intent == "EXACT_REVERSAL_WITH_REFERENCE" and not self.reversal_of:
			frappe.throw(
				_("An exact reversal must reference the original valuation event (Reversal Of)."),
				title=_("Posting Intent Violation"),
			)
		if self.reversal_of and self.posting_intent and \
				self.posting_intent != "EXACT_REVERSAL_WITH_REFERENCE":
			frappe.throw(
				_(
					"Event references {0} as a reversal but declares intent {1}. A referenced "
					"reversal always uses the original basis; a posting at current cost is not a reversal."
				).format(self.reversal_of, self.posting_intent),
				title=_("Posting Intent Violation"),
			)

	def on_trash(self):
		block_delete(self)
