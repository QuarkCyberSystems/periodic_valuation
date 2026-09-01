# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Bin/SLE <-> IPB drift detection.

The kernel keeps SLE-compatible rows + Bin in step with the Inventory Period
Balance (the valuation ledger) by construction, but backdated/edge flows can
still let the physical shadow (Bin) drift from the ledger (IPB). The period
close reconciles GL<->IPB; this closes the remaining gap by reconciling the
Bin quantity against the IPB. A Stock Reconciliation is the correction lever.

Run:  bench --site <site> execute periodic_valuation.shared.integrity.report_drift
      bench --site <site> execute periodic_valuation.shared.integrity.report_drift --kwargs '{"company": "Badia Cement"}'
"""

import frappe
from frappe.utils import flt


def check_sle_ipb_value_drift(company=None, tolerance=0.01):
	"""Return a list of scopes whose Stock Ledger value disagrees with the IPB.

	The Bin check below compares *quantity*; this compares *value*. Core stock
	reporting (Stock Balance, Stock Analytics, Gross Profit) sums SLE
	``stock_value_difference``, so a value event that never wrote an SLE leaves
	the ledger correct while every core report under-states inventory by that
	amount - silently, because nothing asserted the identity (DR-02).

	Warehouse-scope items compare per warehouse; company-scope items compare
	the IPB total against the sum of SLE value across all warehouses.
	"""
	from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS

	drifts = []
	items = frappe.get_all(
		"Item",
		filters={"valuation_method": ("in", tuple(KERNEL_VALUATION_METHODS)), "is_stock_item": 1},
		fields=["name", "valuation_includes_warehouse"],
	)
	for it in items:
		latest = frappe.get_all(
			"Inventory Period Balance",
			filters={"item_code": it.name, **({"company": company} if company else {})},
			fields=["company", "warehouse", "closing_value", "period_year", "period_month"],
			order_by="period_year desc, period_month desc",
		)
		if not latest:
			continue
		seen = set()
		for row in latest:
			key = (row.company, row.warehouse or "")
			if key in seen:
				continue
			seen.add(key)
			ipb_value = flt(row.closing_value)
			conds = ["item_code = %(item)s", "is_cancelled = 0", "company = %(company)s"]
			params = {"item": it.name, "company": row.company}
			if it.valuation_includes_warehouse and row.warehouse:
				conds.append("warehouse = %(warehouse)s")
				params["warehouse"] = row.warehouse
			sle_value = flt(
				frappe.db.sql(
					"select sum(stock_value_difference) from `tabStock Ledger Entry` where "
					+ " and ".join(conds),
					params,
				)[0][0] or 0
			)
			drift = flt(sle_value - ipb_value, 6)
			if abs(drift) > tolerance:
				drifts.append({
					"item_code": it.name, "company": row.company,
					"warehouse": row.warehouse or "(company scope)",
					"ipb_value": ipb_value, "sle_value": sle_value, "drift": drift,
				})
	return drifts


def check_bin_ipb_drift(company=None, tolerance=0.001):
	"""Return a list of {item_code, warehouse, ipb_qty, bin_qty, drift} for
	every periodic-valuation scope whose latest IPB closing quantity disagrees with
	the physical Bin quantity. Warehouse-scope items compare per warehouse;
	company-scope items compare the IPB total against the sum of all Bins."""
	from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS

	drifts = []
	items = frappe.get_all(
		"Item",
		filters={"valuation_method": ("in", tuple(KERNEL_VALUATION_METHODS)), "is_stock_item": 1},
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
	"""Repair the physical Bin to match the valuation ledger (IPB) - the IPB is
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


def align_stock_ledger_value(company, item_code=None, posting_date=None, dry_run=True):
	"""Repair: write the missing value-only Stock Ledger rows so the stock
	ledger agrees with the valuation ledger (IPB) again.

	Value events posted before DR-02 landed never wrote their SLE row, so the
	IPB and GL are right while every core stock report under-states inventory
	by the missing amount. For each drifted scope this posts ONE zero-quantity,
	kernel-flagged SLE carrying the difference, dated `posting_date` (default:
	the last day of the scope's latest period) and recorded against that
	Inventory Period as its voucher. It moves no GL and no IPB value - the
	ledger is already correct; only the shadow catches up. Single-warehouse
	company-scope and warehouse-scope items only; multi-warehouse company-scope
	scopes are reported and skipped. dry_run=True (default) only reports.
	"""
	from frappe.utils import get_last_day

	from periodic_valuation.periodic_moving_average.kernel import write_value_sle

	drifts = [d for d in check_sle_ipb_value_drift(company)
		if item_code is None or d["item_code"] == item_code]
	done, skipped = [], []
	for d in drifts:
		it = d["item_code"]
		include_wh = frappe.get_cached_value("Item", it, "valuation_includes_warehouse")
		latest = frappe.get_all(
			"Inventory Period Balance",
			filters={"company": d["company"], "item_code": it,
				**({"warehouse": d["warehouse"]} if include_wh else {})},
			fields=["name", "warehouse", "closing_qty", "closing_value", "moving_avg_price",
				"period_year", "period_month"],
			order_by="period_year desc, period_month desc", limit=1)[0]
		if include_wh:
			physical = latest.warehouse
		else:
			whs = frappe.get_all("Stock Ledger Entry", filters={"item_code": it, "company": d["company"],
				"is_cancelled": 0}, pluck="warehouse", distinct=True)
			if len(whs) != 1:
				skipped.append({**d, "reason": f"{len(whs)} warehouses in the stock ledger - per-warehouse decision needed"})
				continue
			physical = whs[0]
		period_name = frappe.db.get_value("Inventory Period", {"company": d["company"],
			"period_year": latest.period_year, "period_month": latest.period_month})
		if not period_name:
			skipped.append({**d, "reason": "no Inventory Period row for the latest balance"})
			continue
		when = posting_date or get_last_day(f"{latest.period_year}-{latest.period_month:02d}-01")
		catch_up = flt(d["ipb_value"] - d["sle_value"], 2)
		entry = {**d, "warehouse": physical, "catch_up": catch_up, "posting_date": str(when),
			"voucher": period_name}
		if dry_run:
			done.append(entry)
			continue
		scope = frappe._dict(company=d["company"], item_code=it, physical_warehouse=physical)
		ipb = frappe._dict(moving_avg_price=latest.moving_avg_price, closing_qty=latest.closing_qty,
			closing_value=latest.closing_value)
		sle = write_value_sle(scope, ipb, source=("Inventory Period", period_name, None),
			posting_date=when, value_delta=catch_up)
		entry["sle"] = sle
		done.append(entry)
	return {"aligned" if not dry_run else "would_align": done, "skipped": skipped}


def align_carryover(company, period_year, period_month, item_code=None, dry_run=True):
	"""Repair: make a period's opening + carryover agree with the previous
	period's closing (the continuity identity the close gate asserts).

	A value event's reversal that did not cascade forward leaves the later month
	carrying the reversed amount (MH #14 on UAT: a reversed 2,000 landed cost).
	The correction goes into carryover_value / carryover_qty - the bucket the
	forward cascade itself writes - and the closing is recomputed. No GL and no
	events move: the GL already carries the reversal. dry_run=True only reports.
	"""
	from periodic_valuation.periodic_moving_average.kernel import recompute_closing, r6
	from periodic_valuation.shared.immutable import KERNEL_FLAG

	py, pm = (period_year - 1, 12) if int(period_month) == 1 else (period_year, int(period_month) - 1)
	rows = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": company, "period_year": period_year, "period_month": period_month,
			**({"item_code": item_code} if item_code else {})},
		fields=["name", "item_code", "warehouse", "opening_qty", "opening_value",
			"carryover_qty", "carryover_value", "closing_qty", "closing_value"],
	)
	out = []
	for r in rows:
		prev = frappe.db.get_value(
			"Inventory Period Balance",
			{"company": company, "item_code": r.item_code, "warehouse": r.warehouse or "",
				"period_year": py, "period_month": pm},
			["closing_qty", "closing_value"], as_dict=True,
		)
		if not prev:
			continue
		dq = flt(prev.closing_qty) - (flt(r.opening_qty) + flt(r.carryover_qty))
		dv = flt(prev.closing_value) - (flt(r.opening_value) + flt(r.carryover_value))
		if abs(dq) < 0.000001 and abs(dv) < 0.01:
			continue
		entry = {"item_code": r.item_code, "warehouse": r.warehouse or "(company scope)",
			"prior_closing": (flt(prev.closing_qty), flt(prev.closing_value, 2)),
			"opening_plus_carryover": (flt(r.opening_qty) + flt(r.carryover_qty),
				flt(flt(r.opening_value) + flt(r.carryover_value), 2)),
			"correction": (dq, flt(dv, 2))}
		if not dry_run:
			doc = frappe.get_doc("Inventory Period Balance", r.name)
			doc.carryover_qty = r6(flt(doc.carryover_qty) + dq)
			doc.carryover_value = r6(flt(doc.carryover_value) + dv)
			recompute_closing(doc)
			frappe.flags[KERNEL_FLAG] = True
			try:
				doc.save(ignore_permissions=True)
			finally:
				frappe.flags[KERNEL_FLAG] = False
			entry["new_closing"] = (flt(doc.closing_qty), flt(doc.closing_value, 2))
		out.append(entry)
	return out


def report_drift(company=None):
	"""Print a human-readable drift report."""
	drifts = check_bin_ipb_drift(company)
	if not drifts:
		print("No Bin <-> IPB drift detected.")
		return drifts
	print(f"Bin <-> IPB drift on {len(drifts)} scope(s) - reconcile each with a Stock Reconciliation:")
	print(f"{'Item':<32} {'Warehouse':<24} {'IPB qty':>10} {'Bin qty':>10} {'drift':>10}")
	for d in drifts:
		print(f"{d['item_code']:<32} {d['warehouse']:<24} {d['ipb_qty']:>10} {d['bin_qty']:>10} {d['drift']:>10}")
	return drifts


def check_billing_consistency(company):
	"""per_billed on routed receipts must equal quantity coverage.

	The billing writers derive per_billed from qty coverage for kernel-routed
	rows (SAP GR/IR semantics). This asserts no receipt has drifted back to an
	amount-based figure - the drift that let a half-invoiced receipt read
	"Completed" and disappear from every billing flow (client meeting
	2026-08-12, MAT-PRE-2026-00281). Returns drift rows; empty means clean.
	"""
	drifts = []
	for pr in frappe.get_all(
		"Purchase Receipt",
		filters={"company": company, "docstatus": 1, "is_cancellation": 0, "is_return": 0},
		fields=["name", "per_billed"],
	):
		doc = frappe.get_doc("Purchase Receipt", pr.name)
		routed = doc.get_kernel_routed_items() if hasattr(doc, "get_kernel_routed_items") else set()
		if not routed:
			continue
		billed = dict(
			frappe.db.sql(
				"""select pr_detail, sum(qty) from `tabPurchase Invoice Item`
				where purchase_receipt=%s and docstatus=1 and ifnull(pr_detail,'') != ''
				group by pr_detail""",
				pr.name,
			)
		)
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
			get_item_wise_returned_qty,
		)

		returned = get_item_wise_returned_qty(doc)
		total_ref = total_billed = 0.0
		for item in doc.items:
			ref = abs(flt(item.amount))
			total_ref += ref
			if item.item_code in routed:
				# net of returns, exactly as the per_billed writer computes it
				eff = flt(item.qty) - flt(returned.get(item.name))
				frac = 1.0 if eff <= 0 else min(flt(billed.get(item.name)) / eff, 1.0)
				total_billed += frac * ref
			else:
				total_billed += min(abs(flt(item.billed_amt)), ref)
		expected = round(100.0 * total_billed / (total_ref or 1), 6)
		if abs(flt(pr.per_billed) - expected) > 0.01:
			drifts.append({"receipt": pr.name, "per_billed": flt(pr.per_billed), "expected": expected})
	return drifts
