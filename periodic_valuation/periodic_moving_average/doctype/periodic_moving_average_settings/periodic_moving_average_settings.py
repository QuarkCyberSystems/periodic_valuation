# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PeriodicMovingAverageSettings(Document):
	def validate(self):
		if frappe.db.exists(
			"Periodic Moving Average Settings", {"company": self.company, "name": ("!=", self.name)}
		):
			frappe.throw(
				_("Periodic Moving Average Settings already exist for {0}.").format(self.company),
				title=_("Duplicate Settings"),
			)
		for fieldname in ("rounding_tolerance", "reconciliation_tolerance"):
			if (self.get(fieldname) or 0) < 0:
				frappe.throw(_("{0} cannot be negative.").format(self.meta.get_label(fieldname)))
