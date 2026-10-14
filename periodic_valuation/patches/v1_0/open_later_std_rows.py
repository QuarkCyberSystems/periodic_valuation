# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client ticket STD-015 (13/10/2026, MH S44: "why the October not opened"):
a standard-cost item whose stock arrived only through postings into the
previous month has no balance row in the later open month. Each gets it now,
seeded as if the period had opened with it (Opening as at the period's
opening, the rest Carryover). Display rows only - no event or GL."""

import frappe


def execute():
	from periodic_valuation.periodic_moving_average.kernel import ScopeState
	from periodic_valuation.periodic_standard_cost.kernel import _open_later_rows

	made = 0
	for r in frappe.db.sql("""SELECT DISTINCT b.company, b.item_code, b.warehouse, b.period_year, b.period_month
			FROM `tabInventory Period Balance` b
			JOIN `tabItem` i ON i.name = b.item_code
			JOIN `tabInventory Period` p ON p.company = b.company AND p.period_year = b.period_year
				AND p.period_month = b.period_month AND p.status = 'PREV_OPEN_UNSETTLED'
			WHERE i.valuation_method = 'Periodic Standard Cost'""", as_dict=True):
		scope = ScopeState(r.company, r.item_code, r.warehouse or None)
		period = frappe.get_doc("Inventory Period", {"company": r.company, "period_year": r.period_year,
			"period_month": r.period_month})
		made += len(_open_later_rows(scope, period))
	print(f"STD-015: {made} later-month balance rows opened")
