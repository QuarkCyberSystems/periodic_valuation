# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from periodic_valuation.shared.immutable import block_delete, block_update, kernel_only_insert


class StockMovementEvent(Document):
	def before_insert(self):
		kernel_only_insert(self)

	def validate(self):
		block_update(self)

	def on_trash(self):
		block_delete(self)
