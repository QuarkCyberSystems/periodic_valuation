# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-63 (client design §3 / §4.1 / §4.5, Vivek 08/10/2026): every RELEASED
standard cost version gets its window - Effective From to Effective To,
never overlapping - read off the cost lookup, so the cost any date
resolves to is unchanged. Versions that took an Effective To before this
rule are backdated corrections (DR-57) and are flagged as such first."""

import frappe


def execute():
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		normalize_cost_windows,
	)

	frappe.db.sql("""UPDATE `tabItem Standard Cost Version` SET is_correction = 1
		WHERE effective_to IS NOT NULL AND status = 'RELEASED'""")
	totals = {"windows": 0, "superseded": [], "continued": []}
	failed = []
	for s in frappe.db.sql("""SELECT DISTINCT company, item_code, warehouse FROM `tabItem Standard Cost Version`
			WHERE status = 'RELEASED'""", as_dict=True):
		frappe.db.savepoint("std_windows")
		try:
			out = normalize_cost_windows(s.company, s.item_code, s.warehouse)
			totals["windows"] += out.get("windows", 0)
			totals["superseded"] += out.get("superseded", [])
			totals["continued"] += out.get("continued", [])
		except Exception:
			frappe.db.rollback(save_point="std_windows")
			frappe.log_error(title=f"DR-63 cost windows failed: {s.item_code}")
			failed.append(s.item_code)
	print(f"DR-63: {totals['windows']} windows set; superseded {totals['superseded']}; "
		f"continuations {totals['continued']}; FAILED {failed}")
