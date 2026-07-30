# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_first_day, get_last_day, getdate

# Legal transitions of the period machine (DR-25). The plan's intermediate
# SETTLEMENT_ALLOWED / SETTLING_LOCKED phases never occur in this build: the
# close ceremony validates and freezes inside one transaction, so the ledger
# is never observable in a half-locked state.
STATE_TRANSITIONS = {
	"OPEN": {"PREV_OPEN_UNSETTLED"},
	"PREV_OPEN_UNSETTLED": {"SETTLED_FROZEN"},
	"SETTLED_FROZEN": set(),
}

POSTING_ALLOWED_STATES = ("OPEN", "PREV_OPEN_UNSETTLED")


class InventoryPeriod(Document):
	def autoname(self):
		self.sync_period_fields()
		abbr = frappe.db.get_value("Company", self.company, "abbr") or self.company
		self.name = f"{abbr}-{self.period_name}"

	def validate(self):
		self.sync_period_fields()
		self.validate_unique_period()
		self.validate_single_open()
		self.validate_transition()

	def sync_period_fields(self):
		start = getdate(self.start_date)
		self.period_year = start.year
		self.period_month = start.month
		self.period_name = f"{start.year}-{start.month:02d}"
		self.start_date = get_first_day(start)
		self.end_date = get_last_day(start)

	def validate_unique_period(self):
		if frappe.db.exists(
			"Inventory Period",
			{
				"company": self.company,
				"period_year": self.period_year,
				"period_month": self.period_month,
				"name": ("!=", self.name),
			},
		):
			frappe.throw(
				_("Inventory Period {0} already exists for {1}.").format(self.period_name, self.company),
				title=_("Duplicate Period"),
			)

	def validate_single_open(self):
		if self.status == "OPEN" and frappe.db.exists(
			"Inventory Period",
			{"company": self.company, "status": "OPEN", "name": ("!=", self.name)},
		):
			frappe.throw(
				_("There is already an OPEN Inventory Period for {0}. Exactly one period may be OPEN per company.").format(self.company),
				title=_("Single Open Period"),
			)
		if self.status and self.status.startswith("PREV_OPEN") and frappe.db.exists(
			"Inventory Period",
			{
				"company": self.company,
				"status": "PREV_OPEN_UNSETTLED",
				"name": ("!=", self.name),
			},
		):
			frappe.throw(
				_("There is already a previous-open Inventory Period for {0}. At most one PREV_OPEN_* period is allowed per company.").format(self.company),
				title=_("Single Previous-Open Period"),
			)

	def validate_transition(self):
		if self.is_new():
			return
		old = frappe.db.get_value("Inventory Period", self.name, "status")
		if old == self.status:
			return
		allowed = STATE_TRANSITIONS.get(old, set())
		if self.status not in allowed:
			frappe.throw(
				_("Invalid Inventory Period state transition {0} → {1}. Allowed: {2}.").format(
					old, self.status, ", ".join(sorted(allowed)) or _("none")
				),
				title=_("Invalid State Transition"),
			)

	def on_trash(self):
		if frappe.db.exists(
			"Inventory Valuation Event",
			{"company": self.company, "period_year": self.period_year, "period_month": self.period_month},
		):
			frappe.throw(
				_("Inventory Period {0} has valuation events and cannot be deleted.").format(self.name),
				title=_("Period In Use"),
			)
