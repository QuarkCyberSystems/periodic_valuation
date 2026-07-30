# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""No-manual-drift guard (signed plan, key design rule #3).

The reconciliation gate proves kernel GL == movement table; a manual Journal
Entry against an inventory account used by periodic-valuation scopes would drift
the real account balance away from both without either noticing. Such JEs are
blocked outright — corrections go through Stock Revaluation (value), Stock
Count (quantity) or Stock Reconciliation (cutover), all of which post tagged,
event-linked GL.

Only inventory accounts are protected; expense/offset accounts (COGS, PRD,
variance, price difference) remain freely journal-able.
"""

import frappe
from frappe import _

CACHE_KEY = "valuation_protected_accounts"
CACHE_TTL = 60  # seconds; scopes change rarely, misses only defer the block


def get_protected_accounts(company):
	cached = frappe.cache.get_value(f"{CACHE_KEY}:{company}")
	if cached is not None:
		return set(cached)

	from periodic_valuation.shared.accounts import get_inventory_account

	scopes = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": company},
		fields=["item_code", "warehouse"],
		distinct=True,
	)
	accounts = set()
	for scope in scopes:
		account = get_inventory_account(company, scope.item_code, scope.warehouse or None)
		if account:
			accounts.add(account)

	frappe.cache.set_value(f"{CACHE_KEY}:{company}", list(accounts), expires_in_sec=CACHE_TTL)
	return accounts


def block_manual_stock_journal(doc, method=None):
	if not frappe.get_hooks("valuation_kernels"):
		return
	protected = get_protected_accounts(doc.company)
	if not protected:
		return
	for row in doc.get("accounts") or []:
		if row.account in protected:
			frappe.throw(
				_(
					"Row {0}: {1} is an inventory account maintained by the periodic valuation kernel. "
					"Manual journal entries against it are not allowed — use Stock Revaluation "
					"(value), Stock Count (quantity) or Stock Reconciliation instead."
				).format(row.idx, frappe.bold(row.account)),
				title=_("Manual Stock Posting Blocked"),
			)
