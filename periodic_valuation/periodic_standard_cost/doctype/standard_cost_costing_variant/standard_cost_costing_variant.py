# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Costing assumptions shared by Standard Cost Estimates (plan: new doctype;
DR-43). An estimate that names a variant takes its overhead percentage and
its default component rate source at calculation time unless the estimate
overrides them. Changing a variant affects estimates calculated afterwards
only - released cost versions are immutable (DR-16)."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class StandardCostCostingVariant(Document):
	def validate(self):
		if flt(self.overhead_percent) < 0:
			frappe.throw(_("Overhead % cannot be negative."))
