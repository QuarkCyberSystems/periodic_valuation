# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Landed Cost Voucher handling for PMA items (``valuation_landed_cost`` hook).

Core's LCV retroactively revalues the receipt's SLEs in place. For kernel
items the charge instead posts as a current-dated ``landed_cost`` valuation
event split by STOCK COVERAGE (DR-32: on-hand vs the receipt row's quantity;
inventory portion -> Stock In Hand + MAP recalc; consumed portion -> Price
Difference).

Returns True when the voucher was fully handled (all items routed), False
when no item is routed (core proceeds). Mixed vouchers are rejected - split
the LCV so kernel and core items don't share one in-place revaluation.
"""

import frappe
from frappe import _
from frappe.utils import flt

from periodic_valuation.periodic_moving_average.kernel import get_coverage_ratio, post_value_event
from periodic_valuation.shared.accounts import get_offset_account


def handle_landed_cost(lcv):
	from erpnext.stock.utils import get_valuation_method

	kernel_map = frappe.get_hooks("valuation_kernels")
	items = lcv.get("items") or []
	if not items or not kernel_map:
		return False

	methods = {row.name: get_valuation_method(row.item_code, lcv.company) for row in items}
	routed = [row for row in items if methods[row.name] in kernel_map]
	if not routed:
		return False
	if len(routed) != len(items):
		frappe.throw(
			_(
				"This Landed Cost Voucher mixes periodic-valuation items with conventionally valued "
				"items. Split it into separate vouchers per valuation family."
			),
			title=_("Mixed Valuation Voucher"),
		)

	if lcv.docstatus == 2:
		frappe.throw(
			_("Landed Cost Vouchers for periodic-valuation items cannot be cancelled. Use Create Cancellation."),
			title=_("Immutable Ledger"),
		)

	if lcv.get("is_cancellation"):
		_reverse_landed_cost(lcv)
		return True

	std_rows = [row for row in routed if methods[row.name] == "Periodic Standard Cost"]
	if std_rows:
		from periodic_valuation.periodic_standard_cost.kernel import handle_std_landed_cost

		handle_std_landed_cost(lcv, std_rows)
	routed = [row for row in routed if methods[row.name] != "Periodic Standard Cost"]

	# proportional credit against each charge row's expense account
	total_charges = sum(flt(t.base_amount or t.amount) for t in lcv.get("taxes") or [])
	for row in routed:
		amount = flt(row.applicable_charges)
		if not amount:
			continue
		receipt_row = frappe.db.get_value(
			row.receipt_document_type + " Item", row.purchase_receipt_item,
			["warehouse", "stock_qty"], as_dict=True,
		) if row.get("purchase_receipt_item") else None
		warehouse = receipt_row.warehouse if receipt_row else None

		# DR-32: the charge covers the RECEIPT ROW's quantity - split by how
		# much of that quantity is still on hand, not by the pool ratio
		basis_qty = flt(receipt_row.stock_qty) if receipt_row and receipt_row.stock_qty else flt(row.qty)
		ratio = get_coverage_ratio(
			lcv.company, row.item_code, warehouse, basis_qty, as_of=lcv.get("posting_date")
		)
		inventory_portion = flt(amount * ratio, 2)
		expense_portion = flt(amount - inventory_portion, 2)

		for tax in lcv.get("taxes") or []:
			share = flt(tax.base_amount or tax.amount) / total_charges if total_charges else 0
			if not share:
				continue
			post_value_event(
				lcv.company,
				row.item_code,
				warehouse,
				source=("Landed Cost Voucher", lcv.name, row.name),
				posting_date=lcv.posting_date,
				reason="landed_cost",
				value_delta=flt(inventory_portion * share, 2),
				expense_portion=flt(expense_portion * share, 2),
				offset_account=tax.expense_account,
			)
	return True


def _reverse_landed_cost(lcv):
	"""Cancellation LCV: mirror the ORIGINAL voucher's valuation events with a
	dated GL swap. The split is never re-run: coverage has moved on since the
	original posted, so recomputing it would reverse a different amount than
	was posted."""
	from erpnext.accounts.general_ledger import make_gl_entries

	from periodic_valuation.shared.immutable import KERNEL_FLAG

	original = lcv.get("cancellation_against")
	if not original:
		frappe.throw(_("Cancellation document must reference the original via Cancellation Against."))
	originals = frappe.get_all(
		"Inventory Valuation Event",
		filters={"source_doctype": "Landed Cost Voucher", "source_docname": original,
			"is_cancelled": 0},
		fields=["*"],
	)
	if not originals:
		frappe.throw(_("No valuation events found for {0} to reverse.").format(original))
	cost_center = frappe.get_cached_value("Company", lcv.company, "cost_center")

	for orig in originals:
		if frappe.db.exists("Inventory Valuation Event",
				{"reversal_of": orig.name, "is_cancelled": 0}):
			frappe.throw(_("{0} is already reversed.").format(orig.name),
				title=_("Double Reversal Blocked"))
		# DR-44 floor: the reversal removes the landed cost from inventory, but
		# never more than the scope still carries - consumption since the
		# original posting took part of the charge out at the blended MAP. The
		# uncovered part credits PRD instead of stranding negative value.
		removal = -flt(orig.value_delta)
		prd_excess = 0.0
		if orig.reason_code == "landed_cost" and removal < 0:
			scope_wh = orig.warehouse if frappe.get_cached_value(
				"Item", orig.item_code, "valuation_includes_warehouse") else ""
			row = frappe.get_all("Inventory Period Balance",
				filters={"company": orig.company, "item_code": orig.item_code,
					"warehouse": scope_wh or ""},
				fields=["closing_value", "closing_qty"],
				order_by="period_year desc, period_month desc", limit=1)
			available = max(flt(row[0].closing_value) if row else 0.0, 0.0)
			scope_qty = flt(row[0].closing_qty) if row else 0.0
			# floor only the stranding case: a negative-qty excursion carries
			# its value through the frozen-MAP machinery and stays exact
			if scope_qty >= 0 and flt(available + removal, 2) < 0:
				prd_excess = flt(available + removal, 2)  # negative
				removal = flt(-available, 2)
		frappe.flags[KERNEL_FLAG] = True
		try:
			mirror = frappe.get_doc({
				"doctype": "Inventory Valuation Event",
				"company": orig.company, "item_code": orig.item_code,
				"warehouse": orig.warehouse,
				"period_year": frappe.utils.getdate(lcv.posting_date).year,
				"period_month": frappe.utils.getdate(lcv.posting_date).month,
				"posting_date": lcv.posting_date,
				"entry_date": frappe.utils.now_datetime(),
				"source_doctype": "Landed Cost Voucher", "source_docname": lcv.name,
				"source_detail_name": orig.source_detail_name,
				"reason_code": "cancellation", "std_trans": orig.std_trans,
				"qty_basis": 0,
				"value_delta": removal if orig.reason_code == "landed_cost" else -flt(orig.value_delta),
				"prd_amount": prd_excess,
				"inventory_portion": removal if orig.reason_code == "landed_cost" else -flt(orig.inventory_portion),
				"expense_portion": -flt(orig.expense_portion),
				"total_sc": -flt(orig.total_sc or 0), "total_ac": -flt(orig.total_ac or 0),
				"in_flag": orig.in_flag, "out_flag": orig.out_flag,
				"ppv_with_sett": orig.ppv_with_sett,
				"ppv_without_sett": orig.ppv_without_sett, "rev_flag": orig.rev_flag,
				"reversal_of": orig.name,
			}).insert(ignore_permissions=True)
		finally:
			frappe.flags[KERNEL_FLAG] = False

		gl_map = []
		for g in frappe.get_all("GL Entry",
				filters={"valuation_event_id": orig.name, "is_cancelled": 0},
				fields=["account", "debit", "credit"]):
			gl_map.append(frappe._dict({
				"account": g.account, "against": "",
				"debit": flt(g.credit), "credit": flt(g.debit),
				"debit_in_account_currency": flt(g.credit),
				"credit_in_account_currency": flt(g.debit),
				"posting_date": lcv.posting_date, "voucher_type": "Landed Cost Voucher",
				"voucher_no": lcv.name, "company": lcv.company, "cost_center": cost_center,
				"remarks": _("Exact reversal of {0}").format(orig.name),
				"valuation_event_id": mirror.name,
			}))
		if prd_excess:
			from periodic_valuation.shared.accounts import get_inventory_account
			inv_acct = get_inventory_account(lcv.company, orig.item_code, orig.warehouse)
			for g in gl_map:
				if g["account"] == inv_acct and flt(g["credit"]):
					g["credit"] = flt(g["credit"]) + prd_excess
					g["credit_in_account_currency"] = g["credit"]
					break
			prd_acct = get_offset_account(lcv.company, orig.item_code, orig.warehouse, "prd")
			gl_map.append(frappe._dict({
				"account": prd_acct, "against": "",
				"debit": 0, "credit": -prd_excess,
				"debit_in_account_currency": 0, "credit_in_account_currency": -prd_excess,
				"posting_date": lcv.posting_date, "voucher_type": "Landed Cost Voucher",
				"voucher_no": lcv.name, "company": lcv.company, "cost_center": cost_center,
				"remarks": _("DR-44 floor: uncovered share of the reversed landed cost"),
				"valuation_event_id": mirror.name,
			}))
		if gl_map:
			make_gl_entries(gl_map, merge_entries=False)
		# restore the MAP scope state the original event moved - and, like the
		# original, push it through every later period's carryover and mirror the
		# stock-ledger row (DR-02). Without the cascade a reversal dated in the
		# previous month left the current month overstated for good (UAT, MH #14:
		# a reversed 2,000 landed cost carried into August).
		if flt(orig.value_delta) and orig.reason_code == "landed_cost":
			from periodic_valuation.periodic_moving_average.kernel import (
				ScopeState,
				_cascade_value_carryover,
				r2,
				recompute_closing,
				write_value_sle,
			)
			from periodic_valuation.shared.periods import assert_posting_allowed
			period = assert_posting_allowed(lcv.company, lcv.posting_date)
			scope = ScopeState(lcv.company, orig.item_code, orig.warehouse)
			ipb = scope.load(period)
			ipb.reval_value = r2(flt(ipb.reval_value) + removal)
			recompute_closing(ipb)
			scope.save(ipb, source=("Landed Cost Voucher", lcv.name))
			_cascade_value_carryover(scope, period, qty_delta=0.0,
				value_delta=removal, source=("Landed Cost Voucher", lcv.name))
			write_value_sle(scope, ipb, source=("Landed Cost Voucher", lcv.name, orig.source_detail_name),
				posting_date=lcv.posting_date, value_delta=removal)
