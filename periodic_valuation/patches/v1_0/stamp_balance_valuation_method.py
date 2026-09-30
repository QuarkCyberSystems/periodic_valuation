# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Stamp Inventory Period Balance rows written before the Valuation Method
field existed (client ticket STD-001, 28/09). A descriptive label derived
from the item — no quantity or value moves. Idempotent: only rows without
a method are touched."""

import frappe


def execute():
	from periodic_valuation.periodic_valuation.doctype.inventory_period_balance.inventory_period_balance import (
		balance_valuation_method,
	)

	scopes = frappe.db.sql(
		"""select distinct item_code, company from `tabInventory Period Balance`
		   where ifnull(valuation_method, '') = ''""",
		as_dict=True,
	)
	stamped = {}
	for s in scopes:
		method = balance_valuation_method(s.item_code, s.company)
		if not method:
			continue
		frappe.db.sql(
			"""update `tabInventory Period Balance` set valuation_method = %s
			   where item_code = %s and company = %s and ifnull(valuation_method, '') = ''""",
			(method, s.item_code, s.company),
		)
		stamped[method] = stamped.get(method, 0) + 1
	print(f"periodic_valuation: balance rows stamped with their valuation method, items per method: {stamped or 'none'}")
