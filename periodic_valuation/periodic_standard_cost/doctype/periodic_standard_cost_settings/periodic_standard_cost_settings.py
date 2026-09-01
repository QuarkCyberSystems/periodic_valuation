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
				["default_settlement_view", "view_default_locked", "year_end_variance_carryforward"],
				as_dict=True)
			if old.view_default_locked and old.default_settlement_view != self.default_settlement_view:
				frappe.throw(
					_("The default settlement view is locked (items already resolve through it)."),
					title=_("Write Once"),
				)
			if (old.year_end_variance_carryforward or "CARRY_PER_VIEW") != (
					self.year_end_variance_carryforward or "CARRY_PER_VIEW"):
				self.validate_year_end_mode_change()

	def validate_year_end_mode_change(self):
		"""One year-end mode per company for the whole fiscal year (ruling 1 Sep
		2026): the mode may change only while no December of the current fiscal
		year has been settled and no Year End Close has run for it."""
		from frappe.utils import getdate, nowdate

		fy = getdate(nowdate()).year
		blocked = frappe.db.exists("STD Year End Close",
			{"company": self.company, "fiscal_year": fy, "docstatus": 1}) or frappe.db.exists(
			"Inventory Period Settlement",
			{"company": self.company, "period_year": fy, "period_month": 12, "cancelled": 0})
		if blocked:
			frappe.throw(
				_("The year-end variance mode is fixed for fiscal year {0}: December has already been settled or the Year End Close has run. Change it in the next fiscal year.").format(fy),
				title=_("Year-End Mode Locked"),
			)
