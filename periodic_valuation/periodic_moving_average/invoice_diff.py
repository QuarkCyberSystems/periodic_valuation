# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Purchase Invoice price differences for PMA items (doc_event on_submit).

When a PI bills a routed Purchase Receipt at a different price, the receipt's
SLEs are never touched (core's adjust-incoming-rate/repost flow is disabled
for routed items). Instead the difference posts as a current-dated
``invoice_diff`` valuation event:

- functional currency: the whole difference splits by Stock Ratio
  (inventory portion -> Stock In Hand + MAP recalc; rest -> Price Difference).
- foreign currency (IFRS): the price component is measured at the RECEIPT
  exchange rate and splits by Stock Ratio; the residual FX movement goes to
  Exchange Gain/Loss and never touches inventory.

The GL offset is Stock Received But Not Billed: the kernel credited SRBNB at
receipt value, the PI debits it at billed value — this event clears exactly
the residue.
"""

import frappe
from frappe import _
from frappe.utils import flt

from periodic_valuation.periodic_moving_average.kernel import get_stock_ratio, post_value_event


def on_purchase_invoice_submit(doc, method=None):
	if doc.get("is_cancellation") and not doc.get("update_stock"):
		_reverse_invoice_diff(doc)
		return
	if doc.get("is_return") or doc.get("update_stock") or doc.get("is_cancellation"):
		return
	kernel_map = frappe.get_hooks("valuation_kernels")
	if not kernel_map:
		return

	from erpnext.stock.utils import get_valuation_method

	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	srbnb = frappe.get_cached_value("Company", doc.company, "stock_received_but_not_billed")

	for item in doc.items:
		if not (item.purchase_receipt and item.pr_detail):
			continue
		method = get_valuation_method(item.item_code, doc.company)
		if method not in kernel_map:
			continue

		pr_row = frappe.db.get_value(
			"Purchase Receipt Item",
			item.pr_detail,
			["net_rate", "base_net_rate", "warehouse"],
			as_dict=True,
		)
		if not pr_row:
			continue

		if method == "Periodic Standard Cost":
			from periodic_valuation.periodic_standard_cost.kernel import on_purchase_invoice_submit_std

			on_purchase_invoice_submit_std(doc, item, pr_row)
			continue

		qty = flt(item.qty)
		base_diff = flt((flt(item.base_net_rate) - flt(pr_row.base_net_rate)) * qty, 2)
		if not base_diff:
			continue

		fx_variance = 0.0
		inventory_component = base_diff
		if doc.currency != company_currency:
			receipt_fx = flt(
				frappe.db.get_value("Purchase Receipt", item.purchase_receipt, "conversion_rate")
			)
			unit_diff_foreign = flt(item.net_rate) - flt(pr_row.net_rate)
			inventory_component = flt(unit_diff_foreign * qty * receipt_fx, 2)
			fx_variance = flt(base_diff - inventory_component, 2)

		ratio = get_stock_ratio(doc.company, item.item_code, pr_row.warehouse, as_of=doc.posting_date)
		inventory_portion = flt(inventory_component * ratio, 2)
		expense_portion = flt(inventory_component - inventory_portion, 2)

		post_value_event(
			doc.company,
			item.item_code,
			pr_row.warehouse,
			source=("Purchase Invoice", doc.name, item.name),
			posting_date=doc.posting_date,
			reason="invoice_diff" if not fx_variance else "fx_adjust",
			value_delta=inventory_portion,
			expense_portion=expense_portion,
			fx_variance=fx_variance,
			offset_account=srbnb,
		)


def _reverse_invoice_diff(doc):
	"""Cancellation PI: mirror the original PI's invoice-diff events (M7)."""
	original = doc.get("cancellation_against")
	if not original:
		return
	if not frappe.db.exists("Inventory Valuation Event",
			{"source_doctype": "Purchase Invoice", "source_docname": original,
			 "is_cancelled": 0}):
		return  # original PI produced no valuation events — nothing to reverse

	_reverse_source_events(doc, original)


def _reverse_source_events(doc, original):
	from erpnext.accounts.general_ledger import make_gl_entries

	from periodic_valuation.periodic_moving_average.kernel import (
		ScopeState,
		_cascade_value_carryover,
		r2,
		recompute_closing,
	)
	from periodic_valuation.shared.immutable import KERNEL_FLAG
	from periodic_valuation.shared.periods import assert_posting_allowed

	cost_center = frappe.get_cached_value("Company", doc.company, "cost_center")
	originals = frappe.get_all("Inventory Valuation Event",
		filters={"source_doctype": "Purchase Invoice", "source_docname": original,
			"is_cancelled": 0}, fields=["*"])
	for orig in originals:
		if frappe.db.exists("Inventory Valuation Event",
				{"reversal_of": orig.name, "is_cancelled": 0}):
			frappe.throw(_("{0} is already reversed.").format(orig.name),
				title=_("Double Reversal Blocked"))
		frappe.flags[KERNEL_FLAG] = True
		try:
			mirror = frappe.get_doc({
				"doctype": "Inventory Valuation Event",
				"company": orig.company, "item_code": orig.item_code,
				"warehouse": orig.warehouse,
				"period_year": frappe.utils.getdate(doc.posting_date).year,
				"period_month": frappe.utils.getdate(doc.posting_date).month,
				"posting_date": doc.posting_date,
				"entry_date": frappe.utils.now_datetime(),
				"source_doctype": "Purchase Invoice", "source_docname": doc.name,
				"source_detail_name": orig.source_detail_name,
				"reason_code": "cancellation", "std_trans": orig.std_trans,
				"qty_basis": 0, "value_delta": -flt(orig.value_delta),
				"inventory_portion": -flt(orig.inventory_portion),
				"expense_portion": -flt(orig.expense_portion),
				"fx_variance": -flt(orig.fx_variance),
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
				"posting_date": doc.posting_date, "voucher_type": "Purchase Invoice",
				"voucher_no": doc.name, "company": doc.company, "cost_center": cost_center,
				"remarks": _("Exact reversal of {0}").format(orig.name),
				"valuation_event_id": mirror.name,
			}))
		if gl_map:
			make_gl_entries(gl_map, merge_entries=False)

		if flt(orig.value_delta) and orig.reason_code in ("invoice_diff", "fx_adjust"):
			period = assert_posting_allowed(doc.company, doc.posting_date)
			scope = ScopeState(doc.company, orig.item_code, orig.warehouse)
			ipb = scope.load(period)
			ipb.reval_value = r2(flt(ipb.reval_value) - flt(orig.value_delta))
			recompute_closing(ipb)
			scope.save(ipb, source=("Purchase Invoice", doc.name))
			# ...and push the same delta through every LATER period's carryover,
			# exactly as the forward posting does. Without this the reversal
			# lands only in its own period: the original invoice-diff cascaded
			# forward, its reversal did not, and every later period stayed
			# overstated by the reversed amount — GL and the Period Balance
			# then disagree permanently (WA-0003-01 item 20 follow-up; found
			# replaying the client's MH #7 chain, where a reversed 100,000
			# invoice difference left the next period 100,000 high).
			_cascade_value_carryover(
				scope, period,
				qty_delta=0.0,
				value_delta=-flt(orig.value_delta),
				source=("Purchase Invoice", doc.name),
			)
