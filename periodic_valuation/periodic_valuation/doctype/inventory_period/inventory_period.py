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
# SETTLED_FROZEN -> PREV_OPEN_UNSETTLED exists too, but only through the gated
# Inventory Period Reopen document (DR-45: immediately-previous month, reason,
# audit stamps); it is deliberately absent here so ad-hoc edits cannot do it.
STATE_TRANSITIONS = {
	"OPEN": {"PREV_OPEN_UNSETTLED"},
	"PREV_OPEN_UNSETTLED": {"SETTLED_FROZEN"},
	"SETTLED_FROZEN": set(),
}

POSTING_ALLOWED_STATES = ("OPEN", "PREV_OPEN_UNSETTLED")

# who may open a period (DR-68): segregation of duties, as SAP restricts MMPV
OPEN_NEXT_ROLES = {"Accounts Manager", "Stock Manager", "System Manager"}


class InventoryPeriod(Document):
	def autoname(self):
		self.sync_period_fields()
		abbr = frappe.db.get_value("Company", self.company, "abbr") or self.company
		self.name = f"{abbr}-{self.period_name}"

	def validate(self):
		self.refuse_outside_period_machine()
		self.sync_period_fields()
		self.validate_unique_period()
		self.validate_single_open()
		self.validate_transition()

	def refuse_outside_period_machine(self):
		"""Periods open and change state only through the period machine
		(DR-68, SAP model: the period shift is the only way): Open Next
		Period / the daily job, Inventory Period Close, Inventory Period
		Reopen. A user request - the form or the API - may create only a
		company's first period (go-live); it can neither create a later one
		nor change a status. On 04/10/2026 October was opened by hand that way
		and no balance was carried into it (client ticket STD-021)."""
		if frappe.flags.in_period_machine or not getattr(frappe.local, "request", None):
			return
		if self.is_new():
			if frappe.db.exists("Inventory Period", {"company": self.company}):
				frappe.throw(
					_("Later periods are opened with Open Next Period on the OPEN Inventory Period (or by the daily job), so every item's balance is carried into them."),
					title=_("Use Open Next Period"),
				)
			return
		if frappe.db.get_value("Inventory Period", self.name, "status") != self.status:
			frappe.throw(
				_("A period's status changes only through Open Next Period, Inventory Period Close or Inventory Period Reopen."),
				title=_("Use the Period Actions"),
			)

	@frappe.whitelist()
	def open_next(self):
		"""Open Next Period (DR-68): the controlled period shift. Restricted to
		the finance and inventory managers and logged on the new period."""
		roles = set(frappe.get_roles())
		if not roles & OPEN_NEXT_ROLES:
			frappe.throw(_("Opening a period needs one of these roles: {0}.").format(", ".join(sorted(OPEN_NEXT_ROLES))),
				frappe.PermissionError)
		from periodic_valuation.shared.period_close import open_next_period

		nxt = open_next_period(self)
		return nxt.name

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
				_("Invalid Inventory Period state transition {0} -> {1}. Allowed: {2}.").format(
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
