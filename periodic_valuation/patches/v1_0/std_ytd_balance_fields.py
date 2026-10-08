# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client ticket STD-013, reopened 08/10/2026 ("all fields in the period
balance should be accumulated in STD YTD"): fills the new Year to Date
section on every YTD standard-cost period balance. Display fields only;
the monthly fields are unchanged."""

import frappe


def execute():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	frappe.reload_doc("periodic_valuation", "doctype", "inventory_period_balance")
	rows = frappe.db.sql(
		"""SELECT b.company, b.item_code, b.warehouse, b.period_year, MIN(b.period_month) AS first_month
		FROM `tabInventory Period Balance` b JOIN `tabItem` i ON i.name = b.item_code
		WHERE i.valuation_method = 'Periodic Standard Cost'
		GROUP BY b.company, b.item_code, b.warehouse, b.period_year""",
		as_dict=True,
	)
	done = 0
	for r in rows:
		engine = StdEngine(r.company, r.item_code, r.warehouse or None)
		if engine.view == "YTD":
			engine.refresh_ipb_pools(r.period_year, r.first_month)
			done += 1
	print(f"STD-013: Year to Date balance filled for {done} YTD scope-years")
