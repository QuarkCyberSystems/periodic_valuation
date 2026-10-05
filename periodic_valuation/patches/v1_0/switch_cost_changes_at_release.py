# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-50 amendment (05/10/2026): the "Last day of the period" option becomes
"Date of release" - a standard cost change switches when it is released,
inside the current period, instead of at the period end. Settings move to
the new option, and every version still waiting for a month-end
revaluation switches at its own release instead (restamp_period_end_switches)."""

import frappe


def execute():
	frappe.db.sql(
		"""UPDATE `tabPeriodic Standard Cost Settings`
		SET revaluation_posting_date = 'Date of release'
		WHERE revaluation_posting_date = 'Last day of the period'"""
	)
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		restamp_period_end_switches,
	)

	restamp_period_end_switches()
