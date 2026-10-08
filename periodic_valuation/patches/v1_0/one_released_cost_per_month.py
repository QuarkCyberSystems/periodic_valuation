# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-62 (client, 08/10/2026: "two released Standard Costs for the same
period"). The version a backdated correction replaced for its month keeps
only the months after it, and a version no date resolves to any more is
SUPERSEDED. Brings the versions released before that rule into line; the
cost each date resolves to does not change."""

import frappe


def _next(y, m):
	return (y + 1, 1) if m == 12 else (y, m + 1)


def _in_force(company, item_code, warehouse, day):
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	try:
		return get_active_standard_cost(company, item_code, warehouse, day).name
	except frappe.ValidationError:
		frappe.clear_last_message()
		return None


def execute():
	from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
		supersede_shadowed_versions,
	)
	from periodic_valuation.periodic_standard_cost.engine import price_from

	moved = 0
	for c in frappe.get_all("Item Standard Cost Version",
			filters={"effective_to": ("is", "set"), "status": "RELEASED"},
			fields=["name", "company", "item_code", "warehouse", "valid_from_year", "valid_from_month", "released_on"]):
		y, m = _next(c.valid_from_year, c.valid_from_month)
		for o in frappe.get_all("Item Standard Cost Version", filters={
				"company": c.company, "item_code": c.item_code, "warehouse": ("in", (c.warehouse or "", None)),
				"status": "RELEASED", "name": ("!=", c.name), "effective_to": ("is", "not set"),
				"switch_on_release": 0, "released_on": ("<", c.released_on)},
				fields=["name", "valid_from_year", "valid_from_month", "price_from_year", "price_from_month"]):
			if price_from(o) == (c.valid_from_year, c.valid_from_month) \
					and _in_force(c.company, c.item_code, c.warehouse, f"{y}-{m:02d}-01") == o.name:
				frappe.db.set_value("Item Standard Cost Version", o.name, {"price_from_year": y, "price_from_month": m,
					"effective_from": f"{y}-{m:02d}-01"}, update_modified=False)
				moved += 1
	retired = []
	for s in frappe.db.sql("""SELECT DISTINCT company, item_code, warehouse FROM `tabItem Standard Cost Version`
			WHERE status = 'RELEASED'""", as_dict=True):
		retired += supersede_shadowed_versions(s.company, s.item_code, s.warehouse)
	print(f"DR-62: {moved} versions now start after the month a correction took; {len(retired)} superseded: {', '.join(retired)}")
