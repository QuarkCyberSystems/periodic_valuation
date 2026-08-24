# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Account determination for periodic-valuation postings (Apr 22 restructure).

Resolution defers to ERPNext's existing Item Default / Item Group Default /
Brand / Company / Warehouse chain, extended with the two item-warehouse child
tables this app adds. The inventory account for a (company, item, warehouse)
is invariant across transaction types; only expense/offset legs vary.

Inventory account resolution order (most specific first):
1. Item Default Warehouse Account            (item, company, warehouse)
2. Item Group Default Warehouse Account      (item_group, company, warehouse)
3. Item Default.default_inventory_account    (item, company)      [core]
4. Item Group Default.default_inventory_account                    [core]
5. Brand Default.default_inventory_account                         [core]
6. Company.default_inventory_account                               [core]
7. Warehouse.account                                               [core]
"""

import frappe
from frappe import _

# Offset accounts whose absence must stop the posting rather than silently
# collapse the leg onto SRBNB. "expense" is deliberately absent: its caller
# falls back to the Company default expense account by design.
REQUIRED_OFFSETS = {
	"price_difference",
	"prd",
	"fx_gain_loss",
	"rounding_cleanup",
	"revaluation",
	"count_diff",
	"negative_stock_adjustment",
}


def _child_account(parenttype, parent, company, warehouse, fieldname):
	if not parent:
		return None
	table = (
		"Item Default Warehouse Account"
		if parenttype == "Item"
		else "Item Group Default Warehouse Account"
	)
	return frappe.db.get_value(
		table,
		{"parent": parent, "company": company, "warehouse": warehouse},
		fieldname,
	)


def get_inventory_account(company, item_code, warehouse=None):
	item_group = frappe.get_cached_value("Item", item_code, "item_group")

	if warehouse:
		for parenttype, parent in (("Item", item_code), ("Item Group", item_group)):
			account = _child_account(parenttype, parent, company, warehouse, "default_inventory_account")
			if account:
				return account

	for doctype, parent in (("Item", item_code), ("Item Group", item_group)):
		account = frappe.db.get_value(
			"Item Default", {"parent": parent, "parenttype": doctype, "company": company},
			"default_inventory_account",
		)
		if account:
			return account

	brand = frappe.get_cached_value("Item", item_code, "brand")
	if brand:
		account = frappe.db.get_value(
			"Item Default", {"parent": brand, "parenttype": "Brand", "company": company},
			"default_inventory_account",
		)
		if account:
			return account

	account = frappe.get_cached_value("Company", company, "default_inventory_account")
	if account:
		return account

	if warehouse:
		return frappe.get_cached_value("Warehouse", warehouse, "account")
	return None


def get_offset_account(company, item_code, warehouse, transaction_type, row_override=None):
	"""Expense/offset account for a posting intent.

	Order: per-row override -> item/item-group per-warehouse tables ->
	Item/Item Group Defaults -> Periodic Moving Average Settings fallback.
	`transaction_type` picks the specialised fieldname where one exists.
	"""
	from periodic_valuation.shared.settings import get_pma_setting

	if row_override:
		return row_override

	fieldmap = {
		"revaluation": "revaluation_account",
		"count_diff": "variance_account",
		"price_difference": "price_difference_account",
		"expense": "expense_account",
	}
	fieldname = fieldmap.get(transaction_type, "expense_account")

	item_group = frappe.get_cached_value("Item", item_code, "item_group")
	if warehouse:
		for parenttype, parent in (("Item", item_code), ("Item Group", item_group)):
			account = _child_account(parenttype, parent, company, warehouse, fieldname)
			if account:
				return account

	for doctype, parent in (("Item", item_code), ("Item Group", item_group)):
		account = frappe.db.get_value(
			"Item Default", {"parent": parent, "parenttype": doctype, "company": company}, fieldname
		)
		if account:
			return account

	settings_map = {
		"revaluation": "stock_revaluation_account",
		"count_diff": "inventory_variance_account",
		"price_difference": "price_difference_account",
		"prd": "prd_account",
		"fx_gain_loss": "fx_gain_loss_account",
		"rounding_cleanup": "stock_rounding_adjustment_account",
		"negative_stock_adjustment": "inventory_variance_account",
	}
	key = settings_map.get(transaction_type)
	if key:
		account = get_pma_setting(company, key)
		if not account and transaction_type in REQUIRED_OFFSETS:
			# Fail loudly. An unresolved account leaves the GL leg with no
			# account, and the posting then collapses onto the offset account
			# (SRBNB): the ledger still balances, so nothing complains, but the
			# expense never reaches P&L and SRBNB carries a permanent
			# imbalance. The client's ACC-PINV-2026-00018 lost a 60,000 price
			# difference exactly this way (WA-0003-01 item 21).
			frappe.throw(
				_(
					"No {0} is configured for {1}. Set it in Periodic Moving Average "
					"Settings, or on the Item / Item Group defaults, before posting "
					"this transaction - posting without it would silently absorb the "
					"amount into Stock Received But Not Billed."
				).format(frappe.unscrub(key), company),
				title=_("Valuation Account Not Configured"),
			)
		return account
	return None


def get_all_inventory_accounts(company, ipb_rows):
	"""Every inventory account any in-scope IPB row resolves to (reconciliation gate).

	The reconciler never hardcodes an account name (May 6 clarification).
	"""
	accounts = set()
	for row in ipb_rows:
		account = get_inventory_account(company, row.item_code, row.warehouse or None)
		if account:
			accounts.add(account)
	return accounts
