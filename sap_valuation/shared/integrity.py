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


def align_bin_to_ledger(item_code=None, company=None, max_drift=None):
	"""Repair the physical Bin to match the valuation ledger (IPB) — the IPB is
	authoritative (built from the immutable event log), the Bin is the shadow.
	This is a data repair, not a reconciliation document: it moves no GL and no
	stock value in the ledger, only re-aligns the Bin quantity/value to the IPB.

	`max_drift` (optional) skips scopes whose |drift| exceeds it, so a mildly
	drifted shadow is fixed while a badly-corrupted item is left for review.
	Returns the list of scopes it corrected.
	"""
	from erpnext.stock.utils import get_or_make_bin

	targets = [d for d in check_bin_ipb_drift(company)
		if (item_code is None or d["item_code"] == item_code)]
	fixed = []
	for d in targets:
		if max_drift is not None and abs(d["drift"]) > max_drift:
			continue
		it = d["item_code"]
		include_wh = frappe.get_cached_value("Item", it, "valuation_includes_warehouse")
		ipb = frappe.get_all(
			"Inventory Period Balance",
			filters={"company": d["company"], "item_code": it},
			fields=["closing_qty", "closing_value", "moving_avg_price", "warehouse"],
			order_by="period_year desc, period_month desc", limit=1)[0]
		# company-scope single-warehouse only: align the one Bin to the IPB total
		bins = frappe.get_all("Bin", filters={"item_code": it}, pluck="name") if not include_wh \
			else [frappe.db.get_value("Bin", {"item_code": it, "warehouse": ipb.warehouse})]
		bins = [b for b in bins if b]
		if len(bins) != 1:
			continue  # multi-warehouse company-scope needs a per-warehouse decision
		frappe.db.set_value("Bin", bins[0], {
			"actual_qty": flt(ipb.closing_qty),
			"valuation_rate": flt(ipb.moving_avg_price),
			"stock_value": flt(ipb.closing_value),
		}, update_modified=True)
		fixed.append({**d, "aligned_to": flt(ipb.closing_qty)})
	return fixed


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
