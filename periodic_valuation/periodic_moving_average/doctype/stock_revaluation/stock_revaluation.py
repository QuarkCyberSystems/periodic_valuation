# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from periodic_valuation.shared.accounts import get_offset_account
from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS


class StockRevaluation(Document):
	"""MR21-style value-only revaluation for PMA items."""

	def validate(self):
		from erpnext.stock.utils import get_valuation_method

		total = 0.0
		for row in self.items:
			method = get_valuation_method(row.item_code, self.company)
			if method not in KERNEL_VALUATION_METHODS:
				frappe.throw(
					_("Row {0}: {1} is not a periodic-valuation item.").format(row.idx, row.item_code)
				)
			if method == "Periodic Standard Cost":
				frappe.throw(
					_(
						"Row {0}: standard costs change by releasing a new Item Standard Cost Version, "
						"not by Stock Revaluation."
					).format(row.idx)
				)
			self.set_current_state(row)
			if flt(row.current_qty) <= 0:
				frappe.throw(
					_("Row {0}: revaluation requires positive on-hand quantity for {1}.").format(
						row.idx, row.item_code
					)
				)
			if flt(row.new_valuation_rate) <= 0:
				frappe.throw(_("Row {0}: New Valuation Rate must be positive.").format(row.idx))
			row.difference_amount = flt(
				flt(row.current_qty) * (flt(row.new_valuation_rate) - flt(row.current_valuation_rate)), 2
			)
			total += flt(row.difference_amount)
		self.total_difference_amount = flt(total, 2)

	def set_current_state(self, row):
		include_wh = frappe.get_cached_value("Item", row.item_code, "valuation_includes_warehouse")
		ipb = frappe.get_all(
			"Inventory Period Balance",
			filters={
				"company": self.company,
				"item_code": row.item_code,
				"warehouse": (row.warehouse or "") if include_wh else "",
			},
			fields=["closing_qty", "closing_value", "moving_avg_price"],
			order_by="period_year desc, period_month desc",
			limit=1,
		)
		row.current_qty = flt(ipb[0].closing_qty) if ipb else 0
		row.current_valuation_rate = flt(ipb[0].moving_avg_price) if ipb else 0
		row.current_stock_value = flt(ipb[0].closing_value) if ipb else 0

	def on_submit(self):
		from periodic_valuation.periodic_moving_average.kernel import post_value_event

		for row in self.items:
			account = row.revaluation_account or get_offset_account(
				self.company, row.item_code, row.warehouse, "revaluation"
			)
			if not account:
				frappe.throw(
					_("Row {0}: no revaluation account resolvable for {1}.").format(row.idx, row.item_code)
				)
			post_value_event(
				self.company,
				row.item_code,
				row.warehouse,
				source=(self.doctype, self.name, row.name),
				posting_date=self.posting_date,
				reason="revaluation",
				value_delta=flt(row.difference_amount),
				offset_account=account,
			)

	def before_cancel(self):
		frappe.throw(
			_(
				"Stock Revaluation cannot be cancelled - the valuation ledger is immutable. "
				"Post an opposite revaluation instead."
			),
			title=_("Immutable Ledger"),
		)
