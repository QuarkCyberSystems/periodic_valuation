# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PeriodicStandardCostSettings(Document):
	def validate(self):
		if frappe.db.exists(
			"Periodic Standard Cost Settings", {"company": self.company, "name": ("!=", self.name)}
		):
			frappe.throw(_("Periodic Standard Cost Settings already exist for {0}.").format(self.company))
		if not self.is_new():
			old = frappe.db.get_value(self.doctype, self.name,
				["default_settlement_view", "view_default_locked"], as_dict=True)
			if old.view_default_locked and old.default_settlement_view != self.default_settlement_view:
				frappe.throw(
					_("The default settlement view is locked (items already resolve through it)."),
					title=_("Write Once"),
				)
