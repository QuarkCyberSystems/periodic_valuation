# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Bulk ISVC creation (plan TC46/47, DR-16 "bulk wizard creates drafts only";
DR-43). Picks every Periodic Standard Cost item in an Item Group (and its
sub-groups), shows which ones would change, and creates one Draft Item
Settlement View Change per eligible item with the common target view and
reason. It never approves or posts: each request still goes through the
segregation-of-duties approval and the settle-under-old-view flip."""

import frappe
from frappe import _
from frappe.model.document import Document


class BulkItemSettlementViewChange(Document):
	def validate(self):
		# the lock is judged on the stored state, so the save that records the
		# creation passes and every later edit is refused
		if not self.is_new() and frappe.db.get_value(self.doctype, self.name, "status") == "Created":
			frappe.throw(_("Requests have already been created from this record."), title=_("Locked"))

	def _candidates(self):
		lft, rgt = frappe.db.get_value("Item Group", self.item_group, ["lft", "rgt"])
		return frappe.db.sql(
			"""
			SELECT i.name, i.settlement_view, i.disabled
			FROM `tabItem` i JOIN `tabItem Group` g ON g.name = i.item_group
			WHERE g.lft >= %s AND g.rgt <= %s AND i.is_stock_item = 1
				AND i.valuation_method = 'Periodic Standard Cost'
			ORDER BY i.name
			""",
			(lft, rgt), as_dict=True,
		)

	@frappe.whitelist()
	def preview(self):
		from periodic_valuation.periodic_standard_cost.engine import get_settlement_view

		self.set("items", [])
		eligible = 0
		for it in self._candidates():
			current = it.settlement_view if it.settlement_view in ("MTD", "YTD") else get_settlement_view(self.company, it.name)
			note, ok = "", True
			if it.disabled:
				ok, note = False, _("item disabled")
			elif current == self.to_view:
				ok, note = False, _("already {0}").format(self.to_view)
			else:
				open_req = frappe.db.get_value("Item Settlement View Change",
					{"item_code": it.name, "status": ("in", ["Draft", "Approved"]), "docstatus": ("<", 2)})
				if open_req:
					ok, note = False, _("open request {0}").format(open_req)
			if not frappe.has_permission("Item Settlement View Change", "create"):
				ok, note = False, _("no permission to create requests")
			eligible += 1 if ok else 0
			self.append("items", {"item_code": it.name, "current_view": current, "eligible": 1 if ok else 0, "note": note})
		self.status = "Previewed"
		self.save(ignore_permissions=True)
		return {"total": len(self.items), "eligible": eligible}

	@frappe.whitelist()
	def create_drafts(self):
		if self.status == "Created":
			frappe.throw(_("Requests have already been created from this record."))
		if self.status != "Previewed":
			self.preview()
		created, log = 0, []
		for row in self.items:
			if not row.eligible:
				continue
			isvc = frappe.get_doc({
				"doctype": "Item Settlement View Change", "company": self.company,
				"item_code": row.item_code, "to_view": self.to_view, "reason": self.reason,
			})
			isvc.insert(ignore_permissions=False)
			row.isvc = isvc.name
			created += 1
			log.append(f"{row.item_code}: {isvc.name}")
		self.created_count = created
		self.log = "\n".join(log) or _("nothing to create")
		self.status = "Created"
		self.save(ignore_permissions=True)
		return {"created": created}
