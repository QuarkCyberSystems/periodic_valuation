# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""A non-monetary advance's basis correction on stock this app values
(GA-0009-01 GAP-054, the platform's `basis_correction_owners`; manual §9.8).

Treasury's reconciliation fixes the economic amount D of the correction and
leaves it in a clearing account; this app propagates it as it propagates an
invoice price difference, under its own period rules:

- Periodic Moving Average: a value-only `invoice_diff` event split by stock
  coverage (DR-32) - the share still on hand moves Stock In Hand and the
  moving average, the rest goes where a consumed price difference goes.
- Periodic Standard Cost: the actual-cost movement enters the variance pool
  (the LC route), inventory stays at standard.

The reversal mirrors exactly the events the original produced
(`invoice_diff._reverse_source_events`). Stock this app does not value is
not its to correct (None).
"""

import frappe
from frappe.utils import flt


def basis_correction(action, pre, destination, delta, clearing_account):
	if destination.get("kind") != "Stock":
		return None
	from erpnext.stock.utils import get_valuation_method

	item, warehouse = destination.get("item_code"), destination.get("warehouse")
	kernels = frappe.get_hooks("valuation_kernels") or {}
	method = get_valuation_method(item, pre.company)
	if method not in kernels:
		return None
	if action == "reverse":
		from periodic_valuation.periodic_moving_average.invoice_diff import _reverse_source_events

		_reverse_source_events(pre, pre.reversal_of, source_doctype=pre.doctype)
		return ("Inventory Valuation Event", _last_event(pre))
	source = (pre.doctype, pre.name, pre.origin_slice)
	if method == "Periodic Standard Cost":
		from periodic_valuation.periodic_standard_cost.kernel import StdEngine, get_active_standard_cost

		engine = StdEngine(pre.company, item, warehouse)
		scv = get_active_standard_cost(pre.company, item, warehouse, pre.posting_date)
		ive = engine.post(trans="LC", posting_date=pre.posting_date, source=source, t_sc_override=0,
			t_ac_override=flt(delta), cost_version=scv.name, post_gl=False)
		engine._post_gl(ive, "LC", 0, flt(delta), offset_override=clearing_account)
		return ("Inventory Valuation Event", ive.name if hasattr(ive, "name") else ive)
	from periodic_valuation.periodic_moving_average.kernel import get_coverage_ratio, post_value_event

	# the stock quantity this application stands for: the slice's quantity in its share of the slice's principal
	qty = flt(destination.get("stock_qty")) * flt(pre.source_qty) / flt(destination.get("principal") or 1)
	ratio = get_coverage_ratio(pre.company, item, warehouse, qty, as_of=pre.posting_date)
	inventory_portion = flt(flt(delta) * ratio, 2)
	ive = post_value_event(pre.company, item, warehouse, source=source, posting_date=pre.posting_date, reason="invoice_diff",
		value_delta=inventory_portion, expense_portion=flt(flt(delta) - inventory_portion, 2), offset_account=clearing_account)
	return ("Inventory Valuation Event", ive)


def _last_event(doc):
	return frappe.db.get_value("Inventory Valuation Event", {"source_doctype": doc.doctype, "source_docname": doc.name},
		"name", order_by="creation desc")
