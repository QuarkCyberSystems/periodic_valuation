# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from periodic_valuation.shared.immutable import KERNEL_FLAG


class InventoryPeriodSettlement(Document):
	def before_insert(self):
		if not frappe.flags.get(KERNEL_FLAG):
			frappe.throw(_("Settlements are created by the settlement engine only."))

	@frappe.whitelist()
	def reverse(self):
		"""Sett-Reverse this settlement (previous month only). Reopens the
		period for corrections; a Settlement Run re-closes it afterwards."""
		self.check_permission("write")
		if self.cancelled:
			frappe.throw(_("{0} is already cancelled.").format(self.name))

		from periodic_valuation.periodic_standard_cost.engine import StdEngine

		engine = StdEngine(self.company, self.item_code, self.warehouse or None)
		engine.sett_reverse(self.name, source=(self.doctype, self.name))
		return self.name

	def on_trash(self):
		frappe.throw(_("Settlements are immutable. Reverse instead."), title=_("Immutable Ledger"))
