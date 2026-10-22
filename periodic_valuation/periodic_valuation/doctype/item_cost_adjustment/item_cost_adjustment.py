# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Item Cost Adjustment - Stage B of a Material allocation (GA-0002-02
GAP-029, CAR-16, CAR-22, bench D-079).

A cost allocation run assigns overhead to an item without touching stock:
it debits the Material allocation account on a technical cost centre and
keeps the position pending. This document takes pending positions through
`qcs_platform.handover` - never by importing the allocating app - and
values each under the item's own method, crediting the allocation account
on the same cost centre, in the statutory book, in the open inventory
month:

    Periodic Moving Average   a cost adjustment through the Cost Adjustment
                              tree (DR-32 / DR-33 / DR-34): the covered part
                              to inventory and MAP, the rest to price
                              difference - exactly as a landed cost
    Periodic Standard Cost    the standard stays; the cost joins the
                              variance pool, as a landed cost does

An item valued outside periodic valuation is refused: ERPNext's own
valuation has no value-only adjustment without a receipt to attach it to.

Never cancelled: Create Cancellation raises a Cancellation document that
mirrors the valuation events on its own date and releases the positions
it took, which are then pending again.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_first_day, get_last_day, getdate

from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS

RECEIVER_TYPE = "MATERIAL"
# a line with no quantity named takes MR21's rule: any stock on hand carries it
ANY_STOCK_ON_HAND = 1e-9


class ItemCostAdjustment(Document):
	def validate(self):
		if self.get("is_cancellation"):
			return
		from erpnext.stock.utils import get_valuation_method

		total = 0.0
		for row in self.items:
			row.receiver_type = RECEIVER_TYPE
			if not flt(row.amount):
				frappe.throw(_("Row #{0}: enter the amount to take, or remove the line.").format(row.idx))
			if not row.handover_key:
				frappe.throw(_("Row #{0}: take lines from Get Pending Allocations; a line names its allocation position.").format(row.idx))
			method = get_valuation_method(row.item_code, self.company)
			if method not in KERNEL_VALUATION_METHODS:
				frappe.throw(
					_(
						"Row #{0}: {1} is valued by {2}. An Item Cost Adjustment takes allocated cost into a "
						"periodic-valuation item (Moving Average or Standard Cost); ERPNext's own valuation has no "
						"value-only adjustment without a receipt. Leave the position pending, or move the item to "
						"periodic valuation."
					).format(row.idx, row.item_code, method),
					title=_("Not a Periodic-Valuation Item"),
				)
			row.valuation_method = method
			if frappe.get_cached_value("Item", row.item_code, "valuation_includes_warehouse") and not row.warehouse:
				frappe.throw(_("Row #{0}: {1} is valued per warehouse; choose the warehouse that carries the cost.").format(
					row.idx, row.item_code))
			if flt(row.pending_amount) and flt(row.amount) * flt(row.pending_amount) < 0:
				frappe.throw(_("Row #{0}: the amount takes the pending position's sign ({1}).").format(
					row.idx, frappe.format(row.pending_amount, "Currency")))
			total += flt(row.amount)
		self.total_amount = flt(total, self.precision("total_amount"))

	def before_submit(self):
		if self.get("is_cancellation"):
			return
		from qcs_platform import handover
		from qcs_platform.contracts import HandoverLine

		# the source refuses a take beyond the position's net pending (VR-040)
		handover.consume(
			self, [HandoverLine(RECEIVER_TYPE, row.handover_key, flt(row.amount)) for row in self.items]
		)

	def on_submit(self):
		if self.get("is_cancellation"):
			self._reverse()
			return
		for row in self.items:
			if row.valuation_method == "Periodic Standard Cost":
				self._post_standard(row)
			else:
				self._post_moving_average(row)
			row.db_update()

	def _post_moving_average(self, row):
		from periodic_valuation.periodic_moving_average.kernel import get_coverage_ratio, post_value_event

		basis = flt(row.basis_qty) or ANY_STOCK_ON_HAND
		ratio = get_coverage_ratio(self.company, row.item_code, row.warehouse, basis, as_of=self.posting_date)
		places = row.precision("inventory_amount")
		row.inventory_amount = flt(flt(row.amount) * ratio, places)
		row.expense_amount = flt(flt(row.amount) - row.inventory_amount, places)
		post_value_event(
			self.company,
			row.item_code,
			row.warehouse,
			source=(self.doctype, self.name, row.name),
			posting_date=self.posting_date,
			reason="landed_cost",
			value_delta=row.inventory_amount,
			expense_portion=row.expense_amount,
			offset_account=row.allocation_account,
		)

	def _post_standard(self, row):
		from periodic_valuation.periodic_standard_cost.engine import StdEngine, get_active_standard_cost

		engine = StdEngine(self.company, row.item_code, row.warehouse)
		scv = get_active_standard_cost(self.company, row.item_code, row.warehouse, self.posting_date)
		ive = engine.post(
			trans="LC",
			posting_date=self.posting_date,
			source=(self.doctype, self.name, row.name),
			t_sc_override=0,
			t_ac_override=flt(row.amount),
			cost_version=scv.name,
			post_gl=False,
		)
		engine._post_gl(ive, "LC", 0, flt(row.amount), offset_override=row.allocation_account)
		row.inventory_amount = 0
		row.expense_amount = flt(row.amount)

	def _reverse(self):
		"""The Cancellation: mirror the original's events on this document's
		date, then give the positions back."""
		from qcs_platform import handover

		from periodic_valuation.periodic_moving_average.landed_cost import reverse_cost_events

		reverse_cost_events(self)
		handover.release(frappe.get_doc(self.doctype, self.cancellation_against))


@frappe.whitelist()
def get_pending(company, item_code=None, warehouse=None):
	"""The pending Material positions of `company`, ready to become lines;
	{"available": False} when no installed app holds any."""
	frappe.has_permission("Item Cost Adjustment", "create", throw=True)
	from qcs_platform import handover

	if not handover.available(RECEIVER_TYPE):
		return {"available": False, "lines": []}
	filters = {k: v for k, v in (("object", item_code), ("warehouse", warehouse)) if v}
	lines = []
	for line in handover.pending(RECEIVER_TYPE, company, filters):
		context = line.context or {}
		lines.append(
			{
				"handover_key": line.key,
				"item_code": line.object_name,
				"warehouse": context.get("warehouse"),
				"bucket": line.bucket,
				"pending_amount": line.amount,
				"amount": line.amount,
				"allocation_account": line.account,
				"cost_center": line.cost_center,
				"basis_qty": month_receipts(company, line.object_name, context.get("warehouse"), context.get("last_posting_date")),
			}
		)
	return {"available": True, "lines": lines}


def month_receipts(company, item_code, warehouse, in_month):
	"""What the allocation month received of the item - the quantity the
	allocated cost applies to (one query)."""
	if not in_month:
		return 0
	day = getdate(in_month)
	return flt(
		frappe.db.sql(
			"""select sum(actual_qty) from `tabStock Ledger Entry`
			where company = %(company)s and item_code = %(item)s and is_cancelled = 0 and actual_qty > 0
				and posting_date between %(from)s and %(to)s
				and (%(warehouse)s = '' or warehouse = %(warehouse)s)""",
			{"company": company, "item": item_code, "from": get_first_day(day), "to": get_last_day(day),
				"warehouse": warehouse or ""},
		)[0][0]
	)
