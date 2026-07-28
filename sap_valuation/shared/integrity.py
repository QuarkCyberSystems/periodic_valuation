# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Bin/SLE <-> IPB drift detection.

The kernel keeps SLE-compatible rows + Bin in step with the Inventory Period
Balance (the valuation ledger) by construction, but backdated/edge flows can
still let the physical shadow (Bin) drift from the ledger (IPB). The period
close reconciles GL<->IPB; this closes the remaining gap by reconciling the
Bin quantity against the IPB. A Stock Reconciliation is the correction lever.

Run:  bench --site <site> execute sap_valuation.shared.integrity.report_drift
      bench --site <site> execute sap_valuation.shared.integrity.report_drift --kwargs '{"company": "Badia Cement"}'
"""

import frappe
from frappe.utils import flt


def check_bin_ipb_drift(company=None, tolerance=0.001):
	"""Return a list of {item_code, warehouse, ipb_qty, bin_qty, drift} for
	every SAP-valuation scope whose latest IPB closing quantity disagrees with
	the physical Bin quantity. Warehouse-scope items compare per warehouse;
	company-scope items compare the IPB total against the sum of all Bins."""
	from sap_valuation.shared.routing import SAP_VALUATION_METHODS

	drifts = []
	items = frappe.get_all(
		"Item",
		filters={"valuation_method": ("in", tuple(SAP_VALUATION_METHODS)), "is_stock_item": 1},
		fields=["name", "valuation_includes_warehouse"],
	)
	for it in items:
		latest = frappe.get_all(
			"Inventory Period Balance",
			filters={"item_code": it.name, **({"company": company} if company else {})},
			fields=["company", "warehouse", "closing_qty", "period_year", "period_month"],
			order_by="period_year desc, period_month desc",
		)
		if not latest:
			continue
		# one latest row per (company, warehouse) scope
		seen = set()
		for row in latest:
			key = (row.company, row.warehouse or "")
			if key in seen:
				continue
			seen.add(key)
			ipb_qty = flt(row.closing_qty)
			if it.valuation_includes_warehouse:
				bin_qty = flt(frappe.db.get_value(
					"Bin", {"item_code": it.name, "warehouse": row.warehouse}, "actual_qty"))
			else:
				rows = frappe.get_all("Bin", filters={"item_code": it.name},
					fields=["actual_qty"])
				bin_qty = flt(sum(flt(b.actual_qty) for b in rows))
			drift = flt(bin_qty - ipb_qty, 6)
			if abs(drift) > tolerance:
				drifts.append({
					"item_code": it.name, "company": row.company,
					"warehouse": row.warehouse or "(company scope)",
					"ipb_qty": ipb_qty, "bin_qty": bin_qty, "drift": drift,
				})
	return drifts


def report_drift(company=None):
	"""Print a human-readable drift report."""
	drifts = check_bin_ipb_drift(company)
	if not drifts:
		print("No Bin <-> IPB drift detected.")
		return drifts
	print(f"Bin <-> IPB drift on {len(drifts)} scope(s) — reconcile each with a Stock Reconciliation:")
	print(f"{'Item':<32} {'Warehouse':<24} {'IPB qty':>10} {'Bin qty':>10} {'drift':>10}")
	for d in drifts:
		print(f"{d['item_code']:<32} {d['warehouse']:<24} {d['ipb_qty']:>10} {d['bin_qty']:>10} {d['drift']:>10}")
	return drifts
