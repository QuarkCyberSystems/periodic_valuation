# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from periodic_valuation.shared.immutable import KERNEL_FLAG, block_delete, kernel_only_insert


class InventoryPeriodBalance(Document):
	def before_insert(self):
		kernel_only_insert(self)

	def validate(self):
		self.validate_kernel_only_mutation()
		self.validate_unique_scope()

	def validate_kernel_only_mutation(self):
		# Runs BEFORE the database write - the block holds even when a caller
		# catches the exception inside a larger transaction.
		if self.is_new() or self.flags.in_insert:
			return
		if not frappe.flags.get(KERNEL_FLAG):
			frappe.throw(
				_("Inventory Period Balance is maintained by the posting kernel and cannot be edited manually."),
				title=_("Immutable Ledger"),
			)

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

	def on_trash(self):
		block_delete(self)

	@property
	def effective_opening_qty(self):
		return (self.opening_qty or 0) + (self.carryover_qty or 0)

	@property
	def effective_opening_value(self):
		return (self.opening_value or 0) + (self.carryover_value or 0)
