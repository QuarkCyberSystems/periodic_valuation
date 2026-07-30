# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""ISVC — governed MTD<->YTD settlement-view flip (SoD: approver != requester;
the item's periods must be fully settled under the old view before posting)."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class ItemSettlementViewChange(Document):
	def validate(self):
		from periodic_valuation.periodic_standard_cost.engine import get_settlement_view

		if self.is_new():
			self.requested_by = frappe.session.user
			self.from_view = get_settlement_view(self.company, self.item_code)
		if self.to_view == self.from_view:
			frappe.throw(_("Item already settles {0}.").format(self.to_view))
		if frappe.db.exists("Item Settlement View Change", {
			"item_code": self.item_code, "status": ("in", ["Draft", "Approved"]),
			"docstatus": ("<", 2), "name": ("!=", self.name),
		}):
			frappe.throw(_("An open view-change request already exists for {0}.").format(self.item_code))

	@frappe.whitelist()
	def approve(self):
		if self.status != "Draft":
			frappe.throw(_("Only Draft requests can be approved."))
		if frappe.session.user == self.requested_by:
			frappe.throw(_("Approver must differ from the requester (segregation of duties)."))
		self.db_set({"status": "Approved", "approved_by": frappe.session.user})

	def on_submit(self):
		"""Posting the flip: all activity must already be settled under the
		old view, then Item.settlement_view changes for future periods."""
		if self.status != "Approved":
			frappe.throw(_("Request must be Approved before posting."))

		from periodic_valuation.periodic_standard_cost.engine import StdEngine, get_active_standard_cost

		engine = StdEngine(self.company, self.item_code)
		# atomic: settle any open activity UNDER THE OLD VIEW inside this
		# transaction, then flip — no window for straddling activity
		for year, month in engine._periods_present():
			if engine.is_period_locked(year, month):
				continue
			scv = get_active_standard_cost(
				self.company, self.item_code, None, f"{year}-{month:02d}-01"
			)
			engine.close_period(
				year=year, month=month, sc=flt(scv.standard_cost),
				source=(self.doctype, self.name),
			)

		frappe.db.set_value("Item", self.item_code, "settlement_view", self.to_view,
			update_modified=False)
		frappe.clear_cache(doctype="Item")
		self.db_set({"status": "Posted", "posted_on": now_datetime()})

	def before_cancel(self):
		if self.status == "Posted":
			frappe.throw(_("Posted view changes are immutable. Raise a new request to flip back."))
