# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client ticket STD-013, reopened 07/10/2026 ("YTD should be accumulated
across the Year"): a YTD standard-cost period balance shows the pools
accumulated from the start of the year. Refreshes the YTD rows written
before that; MTD rows are unchanged. Display fields only."""

import frappe


def execute():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

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
	print(f"STD-013: year-to-date pools accumulated for {done} YTD scope-years")
