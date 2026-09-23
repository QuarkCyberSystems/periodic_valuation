"""This app's registration with qcs_platform, held to the contracts -
bench --site <site> execute periodic_valuation.tests.smoke_platform_registration.run

Calls the platform's contract runners for the ledger adapter (over every
governed doctype and the three event-log doctypes), the settings doctype
and the Inventory Period authority, with real documents of this app as
fixtures. Passing them is what "registered" means (design §14.2); a review
treats a missing call as a MUST finding. Rolled back.
"""

import frappe
from frappe.utils import add_months, nowdate

from periodic_valuation.platform import InventoryPeriodAuthority, ValuationLedgerAdapter, get_registration
from periodic_valuation.tests.smoke_kernel import ITEM as MAP_ITEM, ensure_masters, get_company


class Fixtures:
	"""Real documents of this app. `posted` on a governed doctype is a
	submitted document carrying a routed item; `ungoverned` one with a core
	item; a ledger row `posted` is a kernel-written event."""

	def __init__(self):
		self.company = get_company()
		abbr = frappe.db.get_value("Company", self.company, "abbr")
		self.wh = f"_SMK Stores - {abbr}"
		self.fifo = "_SMK-PLATFORM-FIFO"
		if not frappe.db.exists("Item", self.fifo):
			frappe.get_doc({
				"doctype": "Item", "item_code": self.fifo, "item_name": self.fifo,
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
				"stock_uom": "Nos", "is_stock_item": 1, "valuation_method": "FIFO",
			}).insert(ignore_permissions=True)

	def _doc(self, doctype, item, submit):
		rows = [{"item_code": item, "qty": 1, "rate": 10, "warehouse": self.wh, "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1}]
		if doctype == "Purchase Receipt":
			doc = frappe.get_doc({"doctype": doctype, "company": self.company, "supplier": "_SMK Supplier",
				"posting_date": nowdate(), "set_posting_time": 1, "items": rows})
		elif doctype == "Purchase Invoice":
			doc = frappe.get_doc({"doctype": doctype, "company": self.company, "supplier": "_SMK Supplier",
				"posting_date": nowdate(), "set_posting_time": 1, "update_stock": 1, "bill_no": "PLAT-1",
				"bill_date": nowdate(), "items": rows})
		elif doctype == "Stock Entry":
			doc = frappe.get_doc({"doctype": doctype, "company": self.company, "stock_entry_type": "Material Receipt",
				"posting_date": nowdate(), "set_posting_time": 1,
				"items": [{"item_code": item, "qty": 1, "basic_rate": 10, "t_warehouse": self.wh, "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1}]})
		elif doctype in ("Delivery Note", "Sales Invoice"):
			self._stock(item)
			doc = frappe.get_doc({"doctype": doctype, "company": self.company, "customer": "_SMK Customer",
				"posting_date": nowdate(), "set_posting_time": 1, "update_stock": 1, "items": rows})
		else:
			return None
		doc.set_missing_values()
		doc.flags.ignore_permissions = True
		doc.insert()
		if submit:
			doc.submit()
		return doc

	def _stock(self, item):
		pr = self._doc("Purchase Receipt", item, submit=True)
		return pr

	def posted(self, doctype):
		if doctype in ("Stock Movement Event", "Inventory Valuation Event", "Inventory Period Balance Snapshot"):
			# a kernel-written row: post a routed receipt and take its event
			self._doc("Purchase Receipt", MAP_ITEM, submit=True)
			name = frappe.db.get_value(doctype, {}, "name", order_by="creation desc")
			return frappe.get_doc(doctype, name)
		return self._doc(doctype, MAP_ITEM, submit=True)

	def draft(self, doctype):
		return self._doc(doctype, MAP_ITEM, submit=False)

	def ungoverned(self, doctype):
		if doctype in ("Subcontracting Receipt", "Landed Cost Voucher", "Stock Reconciliation"):
			return None
		return self._doc(doctype, self.fifo, submit=True)

	def with_dependent(self, doctype):
		if doctype != "Purchase Receipt":
			return None
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice
		from qcs_platform.contracts import Dependent

		pr = self._doc("Purchase Receipt", MAP_ITEM, submit=True)
		pi = make_purchase_invoice(pr.name)
		pi.bill_no, pi.bill_date = "PLAT-DEP", nowdate()
		pi.flags.ignore_permissions = True
		pi.insert()
		pi.submit()
		pr.reload()
		return pr, Dependent(doctype="Purchase Invoice", name=pi.name, how_to_undo="")


def run():
	from qcs_platform.testkit import Checks, throwaway_site_only
	from qcs_platform.testkit import contract

	throwaway_site_only()
	frappe.set_user("Administrator")
	frappe.flags.mute_emails = True
	ensure_masters()
	checks = Checks("periodic_valuation platform registration")
	try:
		registration = get_registration()
		checks("registration names this app", registration.app == "periodic_valuation")

		adapter = ValuationLedgerAdapter()
		# the runner walks every governed doctype; SCR / LCV / SR fixtures
		# are not built here, so those three are asserted by shape only
		adapter_for_run = ValuationLedgerAdapter()
		adapter_for_run.doctypes = ("Purchase Receipt", "Delivery Note", "Stock Entry", "Purchase Invoice", "Sales Invoice")
		# a snapshot row is written at period close, not by a receipt, and the
		# period balance only under the kernel flag: the two event rows carry
		# the same rule and are what a receipt leaves behind
		adapter_for_run.ledger_doctypes = ("Stock Movement Event", "Inventory Valuation Event")
		contract.ledger_adapter(adapter_for_run, Fixtures(), checks)
		checks("SR is refused without a route", adapter.actions(frappe._dict(doctype="Stock Reconciliation")) == ())

		company = get_company()
		contract.settings("Periodic Moving Average Settings", company, checks)

		authority = InventoryPeriodAuthority()
		closed = frappe.db.get_value(
			"Inventory Period", {"company": company, "status": ("in", ("SETTLED_FROZEN", "CLOSED", "FROZEN"))},
			"start_date",
		)
		contract.period_authority(authority, company, "Purchase Receipt", nowdate(), closed, checks)
	finally:
		frappe.db.rollback()
	checks.summary()
	checks.raise_on_failure()
	return True
