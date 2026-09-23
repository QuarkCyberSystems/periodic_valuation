# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from periodic_valuation.shared.immutable import KERNEL_FLAG, kernel_only_insert


class InventoryPeriodBalance(Document):
	"""Maintained by the posting kernel. Who may insert is this app's rule
	(below); that a row never changes by hand and never deletes is the
	platform's - registered in platform.py as a ledger doctype whose
	`rows_may_change` is the kernel flag."""

	def before_insert(self):
		kernel_only_insert(self)

	def validate(self):
		self.validate_unique_scope()

	def validate_unique_scope(self):
		filters = {
			"company": self.company,
			"item_code": self.item_code,
			"warehouse": self.warehouse or "",
			"period_year": self.period_year,
			"period_month": self.period_month,
			"name": ("!=", self.name),
		}
		if frappe.db.exists("Inventory Period Balance", filters):
			frappe.throw(
				_("Inventory Period Balance already exists for {0} / {1} / {2}-{3}.").format(
					self.item_code, self.warehouse or self.company, self.period_year, self.period_month
				),
				title=_("Duplicate Period Balance"),
			)

	def on_update(self):
		# Mutable only through the kernel (buckets), never by hand. Opening is
		# fixed forever; the kernel appends backdated deltas to carryover_*.
		if self.is_new() or self.flags.in_insert:
			return
		if not frappe.flags.get(KERNEL_FLAG):
			frappe.throw(
				_("Inventory Period Balance is maintained by the posting kernel and cannot be edited manually."),
				title=_("Immutable Ledger"),
			)

	@property
	def effective_opening_qty(self):
		return (self.opening_qty or 0) + (self.carryover_qty or 0)

	@property
	def effective_opening_value(self):
		return (self.opening_value or 0) + (self.carryover_value or 0)

	@frappe.whitelist()
	def settlement_state(self):
		"""What the form needs to offer 'Settle This Item' (DR-47): rendered
		from the server's own predicates, not re-derived on the client."""
		from periodic_valuation.periodic_standard_cost.engine import StdEngine
		from periodic_valuation.shared.periods import POSTING_ALLOWED_STATES

		self.check_permission("read")
		is_std = frappe.get_cached_value("Item", self.item_code, "valuation_method") == "Periodic Standard Cost"
		status = frappe.db.get_value("Inventory Period", {"company": self.company,
			"period_year": self.period_year, "period_month": self.period_month}, "status")
		settled = False
		if is_std:
			settled = StdEngine(self.company, self.item_code, self.warehouse or None).is_period_locked(
				self.period_year, self.period_month)
		return {"is_std": is_std, "postable": status in POSTING_ALLOWED_STATES,
			"period_status": status, "settled": settled}
