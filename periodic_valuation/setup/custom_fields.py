# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Custom fields the periodic_valuation app adds to core doctypes.

Applied idempotently on after_migrate. Where the consolidated design earmarks
a field for a future upstream core PR (e.g. GL Entry.valuation_event_id), the
Custom Field here is the interim carrier with the same fieldname, so a later
core adoption is a data no-op.
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Stock-posting doctypes that get the cancellation pattern (signed MAP plan:
# same-doctype Cancellation documents, never docstatus 1 -> 2 for routed items).
CANCELLATION_DOCTYPES = [
	"Purchase Receipt",
	"Delivery Note",
	"Stock Entry",
	"Purchase Invoice",
	"Sales Invoice",
	"Subcontracting Receipt",
	"Landed Cost Voucher",
]


def get_custom_fields():
	custom_fields = {
		"GL Entry": [
			{
				"fieldname": "valuation_event_id",
				"label": "Valuation Event",
				"fieldtype": "Link",
				"options": "Inventory Valuation Event",
				"read_only": 1,
				"no_copy": 1,
				"search_index": 1,
				"insert_after": "voucher_detail_no",
			}
		],
		"Item": [
			{
				"fieldname": "item_default_warehouse_accounts",
				"label": "Warehouse-level Default Accounts (Periodic Valuation)",
				"fieldtype": "Table",
				"options": "Item Default Warehouse Account",
				"insert_after": "item_defaults",
				"depends_on": "eval:['Periodic Moving Average','Periodic Standard Cost'].includes(doc.valuation_method)",
			},
		],
		"Item Group": [
			{
				"fieldname": "default_settlement_view",
				"label": "Default Settlement View (Periodic Standard Cost)",
				"fieldtype": "Select",
				"options": "\nMTD\nYTD",
				"insert_after": "item_group_defaults",
			},
			{
				"fieldname": "item_group_default_warehouse_accounts",
				"label": "Warehouse-level Default Accounts (Periodic Valuation)",
				"fieldtype": "Table",
				"options": "Item Group Default Warehouse Account",
				"insert_after": "item_group_defaults",
			},
		],
		"Item Default": [
			{
				"fieldname": "revaluation_account",
				"label": "Revaluation Account",
				"fieldtype": "Link",
				"options": "Account",
				"insert_after": "default_provisional_account",
			},
			{
				"fieldname": "variance_account",
				"label": "Variance Account",
				"fieldtype": "Link",
				"options": "Account",
				"insert_after": "revaluation_account",
			},
			{
				"fieldname": "price_difference_account",
				"label": "Price Difference Account",
				"fieldtype": "Link",
				"options": "Account",
				"insert_after": "variance_account",
			},
		],
		"Stock Entry Type": [
			{
				"fieldname": "periodic_valuation_section",
				"label": "Default Accounts (Periodic Valuation)",
				"fieldtype": "Section Break",
				"insert_after": "add_to_transit",
			},
			{
				"fieldname": "default_accounts",
				"label": "Per-Company Default Accounts",
				"fieldtype": "Table",
				"options": "Stock Entry Type Account",
				"insert_after": "periodic_valuation_section",
			},
			{
				"fieldname": "expense_account_overrides_item_default",
				"label": "Expense Account Overrides Item Default",
				"fieldtype": "Check",
				"default": "0",
				"insert_after": "default_accounts",
				"description": "When on, this Stock Entry Type's expense account wins over Item / Item Group defaults.",
			},
		],
	}

	# is_cancellation / cancellation_against are the platform's fields on every
	# governed doctype (qcs_platform.core.fields, D-029); only the kernel's own
	# stamp is installed here
	for doctype in CANCELLATION_DOCTYPES:
		custom_fields[doctype] = [
			{
				"fieldname": "posting_intent",
				"label": "Posting Intent",
				"fieldtype": "Select",
				"options": "\nNEW_CURRENT_STD_MOVEMENT\nRETURN_WITH_REFERENCE\nEXACT_REVERSAL_WITH_REFERENCE",
				"read_only": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
				"insert_after": "cancellation_against",
				"description": "Stamped by the valuation kernel from the action taken; never user-selected.",
			},
		]

	return custom_fields


# The fork's Item delta, re-homed here (D-029 §7 step 4 - it names this app's
# valuation concepts): the two periodic methods on the core Select, and the
# two fields the kernel reads. Generated from the fork's item.json.
ITEM_FIELDS = json.loads(r"""[
 {
  "default": "0",
  "depends_on": "eval:['Periodic Moving Average','Periodic Standard Cost'].includes(doc.valuation_method)",
  "description": "OFF: one valuation per (company, item); transfers between warehouses are physical-only. ON: valuation per (company, item, warehouse). Locked after the first periodic-valuation transaction.",
  "fieldname": "valuation_includes_warehouse",
  "fieldtype": "Check",
  "label": "Valuation Includes Warehouse",
  "insert_after": "valuation_method"
 },
 {
  "depends_on": "eval:doc.valuation_method == 'Periodic Standard Cost'",
  "description": "Variance settlement scope for Periodic Standard Cost. Blank inherits Item Group -> Periodic Standard Cost Settings. Locked after the first transaction.",
  "fieldname": "settlement_view",
  "fieldtype": "Select",
  "label": "Settlement View",
  "options": "\nMTD\nYTD",
  "insert_after": "valuation_includes_warehouse"
 }
]""")


def apply_custom_fields():
	create_custom_fields(get_custom_fields(), ignore_validate=True)
	# update=True: this app's definition is the definition (the fork's JSON
	# carried these as DocFields; the migrate that reverts it syncs them out first).
	# A DocField still on disk is refused rather than shadowed (the sync-first rule)
	from qcs_platform.core.fields import docfield_collisions

	collisions = docfield_collisions({"Item": [f["fieldname"] for f in ITEM_FIELDS]})
	if collisions:
		frappe.throw(
			"periodic_valuation cannot install its Item fields beside a DocField of the same name: "
			+ ", ".join(collisions["Item"]) + ". Revert the schema first, then migrate (the sync-first rule).",
			title="Custom Field Collision",
		)
	create_custom_fields({"Item": [{**f, "module": "Periodic Valuation"} for f in ITEM_FIELDS]}, ignore_validate=True, update=True)
	apply_item_valuation_options()


def apply_item_valuation_options():
	"""The core Select gains the two periodic methods; `_validate_selects`
	rejects an item's value that the options do not list, so this is in
	place before any item saves after the fork's item.json reverts."""
	from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS

	# upstream's own options plus this app's methods - one source for the method set
	# the DocField row itself: meta already carries this setter's own value
	core = frappe.db.get_value("DocField", {"parent": "Item", "fieldname": "valuation_method"}, "options") or ""
	core_options = [o for o in core.split("\n") if o not in KERNEL_VALUATION_METHODS]
	options = "\n".join(core_options + list(KERNEL_VALUATION_METHODS))
	from qcs_platform.core.fields import ensure_property_setter

	if ensure_property_setter("Item", "valuation_method", "options", options, "Text", "Periodic Valuation"):
		frappe.clear_cache(doctype="Item")


def ensure_module_defs():
	"""Create Module Def rows for any module in modules.txt that is missing.
	When the app was first installed before a module existed (e.g. a site on an
	older build), `bench migrate` will not sync that module's doctypes until its
	Module Def exists - so a version jump silently skips the new doctypes. This
	guard closes that gap on every migrate (idempotent)."""
	app = "periodic_valuation"
	for module_name in frappe.get_module_list(app):
		if not frappe.db.exists("Module Def", module_name):
			frappe.get_doc({
				"doctype": "Module Def", "module_name": module_name, "app_name": app,
			}).insert(ignore_permissions=True)


# Stock Entry Types used by reversal documents. A Cancellation of a Material
# Issue is still mechanically an issue-shaped Stock Entry (the kernel decides
# direction from `is_cancellation`, not from the purpose), but showing
# "Material Issue" on a reversal misreads to users - the client asked for the
# transaction type not to say "issue" (WA-0003-01 item 5).
#
# A Stock Entry Type is just a named record carrying a `purpose`, and Stock
# Entry.purpose is read-only and fetched from it. So a dedicated type changes
# the label only and leaves every ERPNext validation and warehouse rule intact.
REVERSAL_STOCK_ENTRY_TYPES = {
	"Reversal of Issue": "Material Issue",
	"Reversal of Receipt": "Material Receipt",
	"Reversal of Transfer": "Material Transfer",
}


def ensure_reversal_stock_entry_types():
	"""Create the reversal Stock Entry Types if missing (idempotent)."""
	for name, purpose in REVERSAL_STOCK_ENTRY_TYPES.items():
		if frappe.db.exists("Stock Entry Type", name):
			continue
		doc = frappe.get_doc({
			"doctype": "Stock Entry Type",
			"purpose": purpose,
			"is_standard": 0,
		})
		doc.name = name
		doc.insert(ignore_permissions=True)


def after_install():
	apply_custom_fields()
	ensure_reversal_stock_entry_types()



def after_migrate():
	from qcs_platform.compat import require_registrant

	require_registrant("periodic_valuation")  # bench migrate never reads required_apps
	ensure_module_defs()
	apply_custom_fields()
	ensure_reversal_stock_entry_types()
