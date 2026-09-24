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
		elif doctype == "Landed Cost Voucher":
			pr = self._doc("Purchase Receipt", item, submit=True)
			expense = frappe.db.get_value("Account", {"company": self.company, "root_type": "Expense", "is_group": 0,
				"account_type": ("in", ("", None))}, "name")
			doc = frappe.get_doc({"doctype": doctype, "company": self.company, "posting_date": nowdate(),
				"distribute_charges_based_on": "Amount",
				"purchase_receipts": [{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name,
					"supplier": pr.supplier, "grand_total": pr.grand_total}],
				"taxes": [{"description": "freight", "amount": 5, "expense_account": expense}]})
			doc.get_items_from_purchase_receipts()
		elif doctype == "Stock Reconciliation":
			self._stock(item)
			doc = frappe.get_doc({"doctype": doctype, "company": self.company, "purpose": "Stock Reconciliation",
				"posting_date": nowdate(), "set_posting_time": 1,
				"items": [{"item_code": item, "warehouse": self.wh, "qty": 3, "valuation_rate": 10}]})
		else:
			return None
		if hasattr(doc, "set_missing_values"):
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
		if doctype in ("Stock Movement Event", "Inventory Valuation Event", "Inventory Period Balance"):
			# a kernel-written row: post a routed receipt and take what it left
			self._doc("Purchase Receipt", MAP_ITEM, submit=True)
			name = frappe.db.get_value(doctype, {}, "name", order_by="modified desc")
			return frappe.get_doc(doctype, name)
		return self._doc(doctype, MAP_ITEM, submit=True)

	def draft(self, doctype):
		return self._doc(doctype, MAP_ITEM, submit=False)

	def ungoverned(self, doctype):
		if doctype in ("Subcontracting Receipt",):
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




def _reversal_pair(fixtures, adapter, checks):
	"""V-05 / V-06: Create Cancellation end to end through the platform's
	ui_state, and the reversal half is itself immutable."""
	from qcs_platform.ledger.dispatcher import LedgerRefusal, ui_state

	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	pr = fixtures.posted("Purchase Receipt")
	checks("V-05 Create Cancellation offered on a routed receipt", [a.label for a in adapter.actions(pr)] == ["Create Cancellation"])
	canc_name = make_cancellation("Purchase Receipt", pr.name)
	checks("V-05 a draft Cancellation stands: the offer is withdrawn", adapter.actions(pr) == ())
	drafted = ui_state("Purchase Receipt", pr.name)
	checks("V-05 the original says where the action went while the draft stands",
		(drafted["banner"] or "").startswith("Cancellation " + canc_name + " is drafted") and not drafted["actions"], str(drafted["banner"]))
	try:
		make_cancellation("Purchase Receipt", pr.name)
		checks("V-05 a second Cancellation is refused while one stands", False, "made")
	except frappe.ValidationError as exc:
		checks("V-05 a second Cancellation is refused while one stands", "already exists" in str(exc), str(exc)[:120])
	canc = frappe.get_doc("Purchase Receipt", canc_name)
	canc.flags.ignore_permissions = True
	canc.submit()
	original = ui_state("Purchase Receipt", pr.name)
	checks("V-05 the original says it is reversed and offers nothing",
		(original["banner"] or "").startswith("Reversed by " + canc_name) and not original["actions"] and original["hide_cancel"],
		str(original))
	half = ui_state("Purchase Receipt", canc_name)
	checks("V-05 the Cancellation is display-only with Cancel hidden",
		(half["banner"] or "").startswith("Reversal of " + pr.name) and half["hide_cancel"] and "items" in half["locked_fields"] and not half["actions"],
		str({k: half[k] for k in ("banner", "hide_cancel", "actions")}))
	canc.reload()
	try:
		canc.cancel()
		checks("V-06 native cancel of the Cancellation is refused", False, "cancelled")
	except LedgerRefusal as exc:
		checks("V-06 native cancel of the Cancellation is refused", "periodic_valuation" in exc.owners, str(exc.owners))
	try:
		make_cancellation("Purchase Receipt", canc_name)
		checks("V-06 a Cancellation of a Cancellation is refused", False, "made")
	except frappe.ValidationError as exc:
		checks("V-06 a Cancellation of a Cancellation is refused", "itself a Cancellation" in str(exc), str(exc)[:120])


def _item_delta(fixtures, checks):
	"""PV-00 (Build 0.1 §12.2, D-029 §7 step 4): the fork's Item delta, now
	this app's - on a transacted periodic item the method never leaves the
	periodic set (upstream would allow Moving Average) and the two valuation
	fields are locked; an untransacted item may change them."""
	from qcs_platform.testkit import isolated

	with isolated():
		fixtures.posted("Purchase Receipt")  # a routed receipt of MAP_ITEM
		item = frappe.get_doc("Item", MAP_ITEM)
		checks("PV-00 the periodic methods are valid options of the core Select",
			"Periodic Standard Cost" in frappe.get_meta("Item").get_field("valuation_method").options)
		for field, value in (("valuation_method", "Moving Average"),
				("valuation_includes_warehouse", 0 if item.valuation_includes_warehouse else 1)):
			item.reload()
			item.set(field, value)
			try:
				item.save()
				checks(f"PV-00 {field} is locked on a transacted periodic item", False, "saved")
			except frappe.ValidationError as exc:
				checks(f"PV-00 {field} is locked on a transacted periodic item", "existing submitted transactions" in str(exc), str(exc)[:100])
		fresh = frappe.get_doc({"doctype": "Item", "item_code": f"_PV00-{frappe.generate_hash(length=5)}",
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos", "is_stock_item": 1, "valuation_method": "Periodic Moving Average"}).insert(ignore_permissions=True)
		fresh.valuation_includes_warehouse = 1
		fresh.save()
		checks("PV-00 an untransacted periodic item may change its valuation fields", True)
		fresh.has_batch_no = 1
		try:
			fresh.save()
			checks("PV-00 a periodic item refuses batch valuation", False, "saved")
		except frappe.ValidationError as exc:
			checks("PV-00 a periodic item refuses batch valuation", "batch or serial" in str(exc), str(exc)[:100])


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
		# Subcontracting Receipt needs a subcontracting BOM and order the smoke
		# masters do not carry; a Period Balance Snapshot is written only at
		# period close. Both are asserted by shape below; the other seven
		# doctypes and three rows run through the contract with real documents
		adapter_for_run.doctypes = tuple(d for d in ValuationLedgerAdapter.doctypes if d != "Subcontracting Receipt")
		adapter_for_run.ledger_doctypes = ("Stock Movement Event", "Inventory Valuation Event", "Inventory Period Balance")
		contract.ledger_adapter(adapter_for_run, Fixtures(), checks)
		checks("SCR and the snapshot are declared", "Subcontracting Receipt" in adapter.doctypes
			and "Inventory Period Balance Snapshot" in adapter.ledger_doctypes)
		checks("SR is refused without a route", adapter.actions(frappe._dict(doctype="Stock Reconciliation", is_cancellation=0)) == ())

		_reversal_pair(Fixtures(), adapter, checks)
		_item_delta(Fixtures(), checks)
		from qcs_platform.testkit import migrate_guard_fires

		from periodic_valuation.setup.custom_fields import after_migrate

		migrate_guard_fires(checks, "after_migrate refuses a site without the platform first", after_migrate)

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
