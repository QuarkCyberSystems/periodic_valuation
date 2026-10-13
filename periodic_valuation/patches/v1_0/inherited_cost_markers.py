# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client ticket STD-018 (12/10/2026): every month's standard cost shows in
the valuation log. Inherited versions created before this get their
zero-value event on their month's first day (no GL, no quantity)."""

import frappe
from frappe.utils import flt, getdate


def execute():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	made = 0
	for v in frappe.get_all("Item Standard Cost Version",
			filters={"source_type": "INHERITED", "status": "RELEASED"}, fields=["*"]):
		if frappe.db.exists("Inventory Valuation Event", {"source_docname": v.name}):
			continue
		engine = StdEngine(v.company, v.item_code, v.warehouse or None)
		if engine.is_period_locked(v.valid_from_year, v.valid_from_month):
			continue
		day1 = getdate(f"{v.valid_from_year}-{v.valid_from_month:02d}-01")
		old = flt(frappe.db.get_value("Item Standard Cost Version", v.supersedes_version, "standard_cost")) \
			if v.supersedes_version else flt(v.standard_cost)
		frappe.db.savepoint("inherit_marker")
		try:
			frappe.get_doc("Item Standard Cost Version", v.name)._record_zero_revaluation(engine, "Rev Beg", day1, old)
			made += 1
		except Exception:
			frappe.db.rollback(save_point="inherit_marker")
	print(f"STD-018: {made} inherited cost versions got their valuation event")
