# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-65 (client, 08/10/2026): every open month carries its own standard
cost version. Creates the inherited version for each standard-cost scope
whose cost runs into an open month without one. Posts nothing; checks that
every day of the open months resolves to the same cost before and after."""

import frappe
from frappe.utils import add_days, get_last_day, getdate


def execute():
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		inherit_into_open_periods,
	)
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	frappe.reload_doc("periodic_standard_cost", "doctype", "item_standard_cost_version")
	made, failed = [], []
	for company in frappe.get_all("Inventory Period", filters={"status": "OPEN"}, pluck="company", distinct=True):
		periods = frappe.get_all("Inventory Period", filters={"company": company,
			"status": ("in", ("PREV_OPEN_UNSETTLED", "OPEN"))}, fields=["start_date"], order_by="start_date asc")
		if not periods:
			continue
		first = getdate(periods[0].start_date)
		last = getdate(get_last_day(periods[-1].start_date))
		days = [add_days(first, i) for i in range((last - first).days + 1)]
		scopes = frappe.db.sql("""SELECT DISTINCT v.item_code, IFNULL(v.warehouse, '') AS warehouse
			FROM `tabItem Standard Cost Version` v JOIN `tabItem` i ON i.name = v.item_code
			WHERE v.company = %s AND v.status = 'RELEASED' AND i.disabled = 0
				AND i.valuation_method = 'Periodic Standard Cost'""", company, as_dict=True)
		for s in scopes:
			wh = s.warehouse or None

			def costs():
				out = []
				for d in days:
					v = get_active_standard_cost(company, s.item_code, wh, d)
					out.append(v.standard_cost if v else None)
				return out

			frappe.db.savepoint("inherit_period_cost")
			try:
				before = costs()
				names = inherit_into_open_periods(company, s.item_code, wh)
				if costs() != before:
					raise frappe.ValidationError("a resolved cost changed")
				made += names
			except Exception as e:
				frappe.db.rollback(save_point="inherit_period_cost")
				failed.append(f"{company} / {s.item_code}: {e}")
	print(f"DR-65: {len(made)} inherited cost versions created; {len(failed)} scopes skipped")
	for f in failed:
		print("  skipped", f)
