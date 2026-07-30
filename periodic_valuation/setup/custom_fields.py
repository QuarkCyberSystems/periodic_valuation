# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Custom fields the periodic_valuation app adds to core doctypes.

Applied idempotently on after_migrate. Where the consolidated design earmarks
a field for a future upstream core PR (e.g. GL Entry.valuation_event_id), the
Custom Field here is the interim carrier with the same fieldname, so a later
core adoption is a data no-op.
"""

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
			{
				"fieldname": "is_cancellation",
				"label": "Is Cancellation",
				"fieldtype": "Check",
				"default": "0",
				"no_copy": 1,
				"read_only_depends_on": "eval:!doc.__islocal",
				"insert_after": "is_return" if doctype != "Landed Cost Voucher" else "company",
				"description": "Dated reversal document preserving the immutable ledger.",
			},
			{
				"fieldname": "cancellation_against",
				"label": "Cancellation Against",
				"fieldtype": "Link",
				"options": doctype,
				"no_copy": 1,
				"depends_on": "is_cancellation",
				"mandatory_depends_on": "is_cancellation",
				"insert_after": "is_cancellation",
			},
		]

	return custom_fields


def apply_custom_fields():
	create_custom_fields(get_custom_fields(), ignore_validate=True)


def ensure_module_defs():
	"""Create Module Def rows for any module in modules.txt that is missing.
	When the app was first installed before a module existed (e.g. a site on an
	older build), `bench migrate` will not sync that module's doctypes until its
	Module Def exists — so a version jump silently skips the new doctypes. This
	guard closes that gap on every migrate (idempotent)."""
	app = "periodic_valuation"
	for module_name in frappe.get_module_list(app):
		if not frappe.db.exists("Module Def", module_name):
			frappe.get_doc({
				"doctype": "Module Def", "module_name": module_name, "app_name": app,
			}).insert(ignore_permissions=True)


def after_migrate():
	ensure_module_defs()
	apply_custom_fields()
