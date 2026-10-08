# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client tickets STD-006 / STD-007 / STD-008 (29-30/09/2026), for the rows
written before them:

- every standard-cost valuation event gets the reason code of its movement
  (it read "std_event");
- every settlement row carries its PPV / revaluation split;
- every settlement, and the period balance of a live one, shows ending stock
  at actual and the actual cost per unit.

Fills classification and display fields only: no amount, date, account or
quantity of any event changes."""

import frappe
from frappe.utils import flt


def execute():
	from periodic_valuation.periodic_standard_cost.engine import (
		ending_stock_at_actual,
		settlement_split,
		std_reason_code,
	)

	for d in ("inventory_valuation_event", "inventory_period_balance"):
		frappe.reload_doc("periodic_valuation", "doctype", d)
	frappe.reload_doc("periodic_standard_cost", "doctype", "inventory_period_settlement")

	recoded = 0
	for r in frappe.db.sql("""SELECT DISTINCT std_trans, reversal_of IS NOT NULL AND reversal_of != '' AS is_reversal
			FROM `tabInventory Valuation Event` WHERE COALESCE(std_trans, '') != ''""", as_dict=True):
		code = std_reason_code(r.std_trans, "x" if r.is_reversal else None)
		frappe.db.sql("""UPDATE `tabInventory Valuation Event` SET reason_code = %s
			WHERE std_trans = %s AND (reversal_of IS NOT NULL AND reversal_of != '') = %s
				AND COALESCE(reason_code, '') != %s""", (code, r.std_trans, r.is_reversal, code))
		recoded += 1

	split = 0
	for s in frappe.get_all("Inventory Period Settlement", fields=["*"]):
		rows = [(s.sett_event, {}), (s.sett_rev_event, {"carry": True})]
		rev = [x for x in (s.reversed_by_events or "").split(",") if x]
		if len(rev) == 2:
			rows += [(rev[0], {"sign": -1}), (rev[1], {"carry": True, "sign": -1})]
		for name, kw in rows:
			if name:
				frappe.db.set_value("Inventory Valuation Event", name, settlement_split(s, **kw), update_modified=False)
				split += 1
		actual = ending_stock_at_actual(s.es_qty, s.standard_cost, s.es_var)
		frappe.db.set_value("Inventory Period Settlement", s.name, actual, update_modified=False)
		if not s.cancelled:
			ipb = frappe.db.get_value("Inventory Period Balance", {"settlement": s.name})
			if ipb:
				frappe.db.set_value("Inventory Period Balance", ipb, {
					"actual_value_after_settlement": actual["es_actual_value"],
					"actual_unit_cost_after_settlement": actual["es_actual_unit_cost"]}, update_modified=False)
	print(f"STD-006/007/008: {recoded} transaction types recoded, {split} settlement rows split")
