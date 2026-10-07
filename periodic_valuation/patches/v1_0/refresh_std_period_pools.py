# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client ticket STD-013 (06/10/2026, DR-58): the PPV and revaluation pools
on a standard-cost Inventory Period Balance were stamped only at settlement.
They now follow every valuation event; this brings the rows written before
that up to date. Display fields only - no ledger or balance value moves."""

import frappe


def execute():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	rows = frappe.db.sql(
		"""SELECT b.company, b.item_code, b.warehouse, b.period_year, b.period_month
		FROM `tabInventory Period Balance` b JOIN `tabItem` i ON i.name = b.item_code
		WHERE i.valuation_method = 'Periodic Standard Cost'""",
		as_dict=True,
	)
	for r in rows:
		StdEngine(r.company, r.item_code, r.warehouse or None).refresh_ipb_pools(r.period_year, r.period_month)
	print(f"STD-013: pools refreshed on {len(rows)} standard-cost period balance rows")
