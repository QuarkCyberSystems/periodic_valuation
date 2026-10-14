# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-68 data repair: every valuation scope gets its balance in every open
month (client ticket STD-021, 14/10/2026). October 2026 was opened by hand
on 04/10/2026, outside the period machine, so no balance was carried into
it; cost-only standard-cost items never had one.

Auditable, not silent: per company the reconciliation gate (GL inventory
balance against the period balances) runs for each open month before and
after; if any figure moves the company's repair is rolled back and reported.
Each open period gets a comment listing every balance created (item, month,
opening, carryover, closing). Display rows only - no event and no GL."""

import frappe
from frappe.utils import flt


def execute():
	from periodic_valuation.periodic_moving_average.kernel import ScopeState
	from periodic_valuation.periodic_standard_cost.kernel import split_opening_at_period_open
	from periodic_valuation.shared.period_close import run_reconciliation_gate

	frappe.reload_doc("periodic_valuation", "doctype", "inventory_period")
	for company in frappe.get_all("Inventory Period", filters={"status": "OPEN"}, pluck="company", distinct=True):
		open_periods = [frappe.get_doc("Inventory Period", n) for n in frappe.get_all("Inventory Period",
			filters={"company": company, "status": ("in", ("OPEN", "PREV_OPEN_UNSETTLED"))},
			order_by="period_year asc, period_month asc", pluck="name")]
		before = {p.name: run_reconciliation_gate(p) for p in open_periods}
		scopes = {(r.item_code, r.warehouse or "") for r in frappe.get_all("Inventory Period Balance",
			filters={"company": company}, fields=["item_code", "warehouse"], distinct=True)}
		# cost-only standard-cost items (a released cost, no balance yet)
		for r in frappe.db.sql("""SELECT DISTINCT v.item_code, IFNULL(v.warehouse, '') AS warehouse
				FROM `tabItem Standard Cost Version` v JOIN `tabItem` i ON i.name = v.item_code
				WHERE v.company = %s AND v.status = 'RELEASED' AND i.disabled = 0
					AND i.valuation_method = 'Periodic Standard Cost'""", company, as_dict=True):
			scopes.add((r.item_code, r.warehouse))

		frappe.db.savepoint("dr68_repair")
		log = []
		for item_code, warehouse in sorted(scopes):
			scope = ScopeState(company, item_code, warehouse or None)
			for key in scope.ensure_open_rows():
				period = next(p for p in open_periods if (p.period_year, p.period_month) == key)
				name = frappe.db.get_value("Inventory Period Balance", scope._filters(*key))
				if frappe.get_cached_value("Item", item_code, "valuation_method") == "Periodic Standard Cost":
					# what earlier months received after the period opened is Carryover
					split_opening_at_period_open(scope, name, period)
				row = frappe.db.get_value("Inventory Period Balance", name,
					["opening_qty", "carryover_qty", "closing_qty", "closing_value"], as_dict=True)
				log.append((period.name, item_code, warehouse, row))
		after = {p.name: run_reconciliation_gate(p) for p in open_periods}
		moved = [n for n in before if abs(flt(before[n]["discrepancy"]) - flt(after[n]["discrepancy"])) > 0.005]
		if moved:
			frappe.db.rollback(save_point="dr68_repair")
			print(f"DR-68 {company}: reconciliation moved in {moved} - repair rolled back, nothing written")
			continue
		for p in open_periods:
			rows = [x for x in log if x[0] == p.name]
			if not rows:
				continue
			body = "".join(
				f"<tr><td>{i}</td><td>{w}</td><td>{flt(r.opening_qty)}</td><td>{flt(r.carryover_qty)}</td>"
				f"<td>{flt(r.closing_qty)}</td><td>{flt(r.closing_value, 2)}</td></tr>" for _, i, w, r in rows)
			frappe.get_doc("Inventory Period", p.name).add_comment("Comment",
				f"<p><b>DR-68 data repair</b>: {len(rows)} item balances created - the period had been opened "
				f"outside the period machine, or the item had a cost but no balance. Reconciliation discrepancy "
				f"before {flt(before[p.name]['discrepancy'], 2)}, after {flt(after[p.name]['discrepancy'], 2)} "
				f"(unchanged).</p><table><tr><th>Item</th><th>Warehouse</th><th>Opening Qty</th>"
				f"<th>Carryover Qty</th><th>Closing Qty</th><th>Closing Value</th></tr>{body}</table>")
		print(f"DR-68 {company}: {len(log)} balances created; reconciliation unchanged")
