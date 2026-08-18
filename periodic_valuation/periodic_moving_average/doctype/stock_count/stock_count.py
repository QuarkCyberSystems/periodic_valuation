# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from periodic_valuation.shared.accounts import get_offset_account
from periodic_valuation.shared.routing import KERNEL_VALUATION_METHODS


class StockCount(Document):
	"""MI07-style physical count for PMA items.

	Client rule: the user enters quantity ONLY; the system values the
	difference at the period MAP. No manual value entry, ever.
	"""

	def validate(self):
		from erpnext.stock.utils import get_valuation_method

		for row in self.items:
			if get_valuation_method(row.item_code, self.company) not in KERNEL_VALUATION_METHODS:
				frappe.throw(
					_("Row {0}: {1} is not a periodic-valuation item.").format(row.idx, row.item_code)
				)
			self.set_current_state(row)
			row.quantity_difference = flt(flt(row.counted_qty) - flt(row.current_qty), 6)
			row.difference_amount = flt(flt(row.quantity_difference) * flt(row.valuation_rate), 2)

	def set_current_state(self, row):
		# Read the balance AS OF the count's posting period, not the latest one:
		# a count dated in a prior month must compare against that month's
		# on-hand, not today's total (WA-0003-01 item 8). Backdated adjustments
		# then cascade forward via the kernel's carryover propagation.
		#
		# Shared with the form helper (api.get_current_state) so what the user
		# sees on screen is the same figure the posting uses.
		from periodic_valuation.periodic_moving_average.api import get_current_state

		# physical=True: a count is a physical exercise on ONE warehouse — for a
		# company-scope item the comparison quantity is that warehouse's stock,
		# not the scope total; valuation stays at the scope MAP (client
		# approval comment 1, 2026-08-18)
		state = get_current_state(
			self.company, row.item_code, row.warehouse,
			posting_date=self.posting_date, physical=True
		)
		row.current_qty = flt(state["closing_qty"])
		row.valuation_rate = flt(
			state["frozen_map"] if state["is_negative"] else state["moving_avg_price"]
		)

	def on_submit(self):
		from erpnext.stock.utils import get_valuation_method

		from periodic_valuation.periodic_moving_average.kernel import post_value_event

		for row in self.items:
			if not flt(row.quantity_difference):
				continue
			if get_valuation_method(row.item_code, self.company) == "Periodic Standard Cost":
				self.post_std_count(row)
				continue
			account = row.variance_account or get_offset_account(
				self.company, row.item_code, row.warehouse, "count_diff"
			)
			if not account:
				frappe.throw(
					_("Row {0}: no variance account resolvable for {1}.").format(row.idx, row.item_code)
				)
			# The picker is company-filtered client-side; enforce it here too so
			# an imported or API-created row cannot post another company's
			# account (client meeting 2026-08-12: "Stock Adjustment - WP"
			# offered on a Badia Cement count).
			acct_company = frappe.get_cached_value("Account", account, "company")
			if acct_company != self.company:
				frappe.throw(
					_("Row {0}: variance account {1} belongs to {2}, not {3}.").format(
						row.idx, account, acct_company, self.company
					)
				)
			post_value_event(
				self.company,
				row.item_code,
				row.warehouse,
				source=(self.doctype, self.name, row.name),
				posting_date=self.posting_date,
				reason="count_diff",
				value_delta=0,  # derived from qty x period MAP inside the kernel
				qty_delta=flt(row.quantity_difference),
				movement_type="count_gain" if flt(row.quantity_difference) > 0 else "count_loss",
				offset_account=account,
			)

	def before_cancel(self):
		frappe.throw(
			_(
				"Stock Count cannot be cancelled — the valuation ledger is immutable. "
				"Post a corrective count instead."
			),
			title=_("Immutable Ledger"),
		)

	def post_std_count(self, row):
		from periodic_valuation.periodic_moving_average.kernel import ScopeState, r2, recompute_closing
		from periodic_valuation.periodic_standard_cost.engine import StdEngine, get_active_standard_cost
		from periodic_valuation.shared.periods import assert_posting_allowed

		engine = StdEngine(self.company, row.item_code, row.warehouse)
		scv = get_active_standard_cost(self.company, row.item_code, row.warehouse, self.posting_date)
		qty = abs(flt(row.quantity_difference))
		engine.post(
			trans="SC+" if flt(row.quantity_difference) > 0 else "SC-",
			posting_date=self.posting_date, qty=qty, sc=flt(scv.standard_cost),
			source=(self.doctype, self.name, row.name), cost_version=scv.name,
		)
		# keep the period balance in step (GL == movement table across counts)
		period = assert_posting_allowed(self.company, self.posting_date)
		scope = ScopeState(self.company, row.item_code, row.warehouse)
		ipb = scope.load(period)
		delta = flt(row.quantity_difference)
		ipb.adjust_qty = flt(ipb.adjust_qty) + delta
		ipb.adjust_value = r2(flt(ipb.adjust_value) + delta * flt(scv.standard_cost))
		recompute_closing(ipb)
		ipb.moving_avg_price = flt(scv.standard_cost)
		ipb.period_standard_cost = flt(scv.standard_cost)
		scope.save(ipb, source=(self.doctype, self.name))
		# ...and the stock ledger + Bin shadows (DR-02). The MAP count path has
		# always written these via post_value_event; the STD branch never did,
		# so Stock Balance and the physical-warehouse quantity were blind to
		# STD count differences (surfaced by the workbook replays when the
		# physical-vs-scope fix started reading the Stock Ledger).
		from periodic_valuation.periodic_moving_average.kernel import (
			_sync_bin_to_ledger,
			write_value_sle,
		)

		_sync_bin_to_ledger(scope, delta)
		write_value_sle(
			scope, ipb, source=(self.doctype, self.name, row.name),
			posting_date=self.posting_date,
			value_delta=r2(delta * flt(scv.standard_cost)), qty_delta=delta,
		)
