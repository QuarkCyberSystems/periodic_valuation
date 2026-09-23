# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from periodic_valuation.shared.immutable import kernel_only_insert


class StockMovementEvent(Document):
	def before_insert(self):
		kernel_only_insert(self)


