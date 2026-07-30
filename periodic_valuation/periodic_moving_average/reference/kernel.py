# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""MAP reference kernel — single (item, warehouse) scope, period-based.

Encodes the signed MAP plan's formulas verbatim:

- receipt (positive):  value += q*cost; MAP = value/qty; counter += q
- issue:               value -= q*MAP; MAP unchanged
- revaluation (MR21):  value += delta (qty > 0); MAP recalcs
- count:               qty +- d at MAP; MAP unchanged; value at system MAP
- landed cost / invoice diff / FX inventory component: Stock Ratio split
      ratio = closing_qty / total_received_since_zero  (clamped [0, 1])
- returns: with_reference at original unit cost (MAP recalcs);
           without_reference at current MAP (MAP unchanged)
- negative stock: MAP frozen; PRD per receipt; cross-zero resets MAP to the
  crossing receipt's price; counter restarts at the excess qty
- backdated postings: prior-period recompute + carryover to current;
  Case C1/C2 cross-period PRD absorb entries
- zero-qty cleanup: residual value -> Stock Rounding Adjustment; counter reset

Internal arithmetic 6 decimals; GL 2 decimals.
"""

from dataclasses import dataclass, field

INTERNAL = 6
CURRENCY = 2


def r6(x):
	return round(x + 0.0, INTERNAL)


def r2(x):
	return round(x + 0.0, CURRENCY)


class Accounts:
	STOCK = "Stock In Hand"
	GRNI = "GR/IR Clearing"
	COGS = "COGS"
	PRICE_DIFF = "Price Difference"
	PRD = "PRD"
	FREIGHT = "Freight Clearing"
	COUNT_LOSS = "Inventory Count Loss"
	COUNT_GAIN = "Inventory Count Gain"
	REVAL = "Revaluation Gain/Loss"
	FX = "Exchange Gain/Loss"
	ROUNDING = "Stock Rounding Adjustment"
	INV_VARIANCE = "Inventory Variance"
	AP = "Accounts Payable"


@dataclass
class GLEntry:
	period: str
	account: str
	debit: float = 0.0
	credit: float = 0.0
	event_id: int = 0


@dataclass
class Event:
	event_id: int
	period: str
	reason: str
	qty_delta: float
	value_delta: float
	map_before: float
	map_after: float
	prd_amount: float = 0.0
	inventory_portion: float = 0.0
	expense_portion: float = 0.0
	fx_variance: float = 0.0
	caused_by: int | None = None
	reference_event: int | None = None


@dataclass
class PeriodBalance:
	period: str
	opening_qty: float = 0.0
	opening_value: float = 0.0
	carryover_qty: float = 0.0
	carryover_value: float = 0.0
	receipt_qty: float = 0.0
	receipt_value: float = 0.0
	issue_qty: float = 0.0
	issue_value: float = 0.0
	adjust_qty: float = 0.0
	adjust_value: float = 0.0
	reval_value: float = 0.0
	prd_value: float = 0.0

	@property
	def closing_qty(self):
		return r6(
			self.opening_qty + self.carryover_qty + self.receipt_qty - self.issue_qty + self.adjust_qty
		)

	@property
	def closing_value(self):
		return r6(
			self.opening_value
			+ self.carryover_value
			+ self.receipt_value
			- self.issue_value
			+ self.adjust_value
			+ self.reval_value
			+ self.prd_value
		)


@dataclass
class MapLedger:
	rounding_tolerance: float = 0.01
	negative_stock_allowed: bool = True

	qty: float = 0.0
	value: float = 0.0
	map: float = 0.0
	counter: float = 0.0  # total_received_since_zero
	frozen_map: float = 0.0
	is_negative: bool = False

	events: list = field(default_factory=list)
	gl: list = field(default_factory=list)
	periods: dict = field(default_factory=dict)  # period -> PeriodBalance
	current_period: str = ""
	_seq: int = 0

	# ------------------------------------------------------------------ infra
	def _pb(self, period):
		if period not in self.periods:
			self.periods[period] = PeriodBalance(period=period)
		return self.periods[period]

	def open_period(self, period):
		"""Open a new current period seeded from the running state."""
		pb = self._pb(period)
		pb.opening_qty = self.qty
		pb.opening_value = self.value
		self.current_period = period
		return pb

	def _event(self, period, reason, qty_delta, value_delta, map_before, **kw):
		self._seq += 1
		evt = Event(
			event_id=self._seq,
			period=period,
			reason=reason,
			qty_delta=r6(qty_delta),
			value_delta=r6(value_delta),
			map_before=map_before,
			map_after=self.map,
			**kw,
		)
		self.events.append(evt)
		return evt

	def _post(self, period, *legs):
		"""legs: (account, signed_amount) — positive = Dr, negative = Cr."""
		for account, amount in legs:
			amount = r2(amount)
			if not amount:
				continue
			self.gl.append(
				GLEntry(
					period=period,
					account=account,
					debit=amount if amount > 0 else 0.0,
					credit=-amount if amount < 0 else 0.0,
					event_id=self._seq,
				)
			)

	def _recalc_map(self):
		if self.qty > 0:
			self.map = r6(self.value / self.qty)
		# qty <= 0: MAP unchanged (frozen handling is explicit elsewhere)

	def _after_mutation(self, period):
		self._maybe_freeze()
		self._zero_qty_cleanup(period)

	def _maybe_freeze(self):
		if self.qty < 0 and not self.is_negative:
			self.is_negative = True
			self.frozen_map = self.map
		elif self.qty >= 0 and self.is_negative:
			# cross-zero handled explicitly in receipt(); direct returns to
			# non-negative (e.g. count gain) unfreeze at current MAP
			self.is_negative = False
			self.frozen_map = 0.0

	def _zero_qty_cleanup(self, period):
		if self.qty != 0:
			return
		self.counter = 0.0
		residual = r2(self.value)
		if residual and abs(residual) <= self.rounding_tolerance * 100:
			# mandatory cleanup: clear any residual value on zero qty
			map_before = self.map
			self.value = 0.0
			self.map = 0.0
			self._event(period, "rounding_cleanup", 0, -residual, map_before)
			self._post(period, (Accounts.ROUNDING, residual), (Accounts.STOCK, -residual))
		else:
			self.map = 0.0
			self.value = r6(self.value)

	# ------------------------------------------------------------ transactions
	def receipt(self, qty, cost, period=None, reason="receipt", account=Accounts.GRNI):
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		receipt_value = r6(qty * cost)

		if self.qty >= 0:
			self.qty = r6(self.qty + qty)
			self.value = r6(self.value + receipt_value)
			self.counter += qty
			self._recalc_map()
			pb.receipt_qty = r6(pb.receipt_qty + qty)
			pb.receipt_value = r6(pb.receipt_value + receipt_value)
			evt = self._event(period, reason, qty, receipt_value, map_before)
			self._post(period, (Accounts.STOCK, receipt_value), (account, -receipt_value))
			self._after_mutation(period)
			return evt

		# ---- negative stock: PRD model ("correct the past, value the future")
		frozen = self.frozen_map
		if self.qty + qty <= 0:
			# Case A — still negative
			prd = r6((cost - frozen) * qty)
			net_to_inventory = r6(qty * frozen)
			self.qty = r6(self.qty + qty)
			self.value = r6(self.value + net_to_inventory)
			self.counter += qty
			pb.receipt_qty = r6(pb.receipt_qty + qty)
			pb.receipt_value = r6(pb.receipt_value + receipt_value)
			pb.prd_value = r6(pb.prd_value - prd)
			evt = self._event(
				period, "receipt_neg", qty, net_to_inventory, map_before, prd_amount=prd
			)
			self._post(
				period,
				(Accounts.STOCK, receipt_value),
				(account, -receipt_value),
				(Accounts.PRD, prd),
				(Accounts.STOCK, -prd),
			)
			self._after_mutation(period)
			return evt

		# Case B — crossing zero
		clearing = r6(-self.qty)
		excess = r6(qty - clearing)
		prd = r6((cost - frozen) * clearing)
		net_to_inventory = r6(clearing * frozen + excess * cost)
		self.qty = r6(self.qty + qty)
		self.value = r6(self.value + net_to_inventory)
		self.map = cost  # RESET to the crossing receipt's price
		self.is_negative = False
		self.frozen_map = 0.0
		self.counter = excess  # fresh allocation cycle
		pb.receipt_qty = r6(pb.receipt_qty + qty)
		pb.receipt_value = r6(pb.receipt_value + receipt_value)
		pb.prd_value = r6(pb.prd_value - prd)
		evt = self._event(
			period, "receipt_cross_zero", qty, net_to_inventory, map_before, prd_amount=prd
		)
		self._post(
			period,
			(Accounts.STOCK, receipt_value),
			(account, -receipt_value),
			(Accounts.PRD, prd),
			(Accounts.STOCK, -prd),
		)
		self._after_mutation(period)
		return evt

	def issue(self, qty, period=None, account=Accounts.COGS):
		period = period or self.current_period
		if self.qty - qty < 0 and not self.negative_stock_allowed:
			raise ValueError("negative stock not allowed")
		pb = self._pb(period)
		map_before = self.map
		rate = self.frozen_map if self.is_negative else self.map
		issue_value = r6(qty * rate)
		self.qty = r6(self.qty - qty)
		self.value = r6(self.value - issue_value)
		pb.issue_qty = r6(pb.issue_qty + qty)
		pb.issue_value = r6(pb.issue_value + issue_value)
		evt = self._event(period, "issue", -qty, -issue_value, map_before)
		self._post(period, (account, issue_value), (Accounts.STOCK, -issue_value))
		self._after_mutation(period)
		return evt

	def landed_cost(self, amount, period=None, account=Accounts.FREIGHT):
		"""Stock Ratio split of a late cost."""
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		ratio = min(max(self.qty / self.counter, 0.0), 1.0) if self.counter else 0.0
		inventory_portion = r2(amount * ratio)
		expense_portion = r2(amount - inventory_portion)
		self.value = r6(self.value + inventory_portion)
		self._recalc_map()
		pb.reval_value = r6(pb.reval_value + inventory_portion)
		evt = self._event(
			period,
			"landed_cost",
			0,
			inventory_portion,
			map_before,
			inventory_portion=inventory_portion,
			expense_portion=expense_portion,
		)
		self._post(
			period,
			(Accounts.STOCK, inventory_portion),
			(Accounts.PRICE_DIFF, expense_portion),
			(account, -amount),
		)
		self._after_mutation(period)
		return evt

	def invoice_diff(self, amount, period=None, receipt_base_total=None):
		"""Functional-currency invoice-vs-GRN difference through Stock Ratio.

		GL: clear GRNI at receipt total, book split, balance to AP.
		"""
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		ratio = min(max(self.qty / self.counter, 0.0), 1.0) if self.counter else 0.0
		inventory_portion = r2(amount * ratio)
		expense_portion = r2(amount - inventory_portion)
		self.value = r6(self.value + inventory_portion)
		self._recalc_map()
		pb.reval_value = r6(pb.reval_value + inventory_portion)
		evt = self._event(
			period,
			"invoice_diff",
			0,
			inventory_portion,
			map_before,
			inventory_portion=inventory_portion,
			expense_portion=expense_portion,
		)
		legs = [(Accounts.STOCK, inventory_portion), (Accounts.PRICE_DIFF, expense_portion)]
		if receipt_base_total is not None:
			legs = [
				(Accounts.GRNI, receipt_base_total),
				*legs,
				(Accounts.AP, -(receipt_base_total + amount)),
			]
		self._post(period, *legs)
		self._after_mutation(period)
		return evt

	def fx_invoice_diff(
		self,
		qty,
		receipt_unit_foreign,
		invoice_unit_foreign,
		fx_at_receipt,
		fx_at_invoice,
		period=None,
	):
		"""IFRS split: price component at receipt FX -> stock ratio; FX -> P&L."""
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map

		receipt_total_base = r6(qty * receipt_unit_foreign * fx_at_receipt)
		invoice_total_base = r6(qty * invoice_unit_foreign * fx_at_invoice)
		total_diff = r6(invoice_total_base - receipt_total_base)
		inventory_component = r6((invoice_unit_foreign - receipt_unit_foreign) * qty * fx_at_receipt)
		fx_variance = r2(total_diff - inventory_component)

		ratio = min(max(self.qty / self.counter, 0.0), 1.0) if self.counter else 0.0
		inventory_portion = r2(inventory_component * ratio)
		expense_portion = r2(inventory_component - inventory_portion)

		self.value = r6(self.value + inventory_portion)
		self._recalc_map()
		pb.reval_value = r6(pb.reval_value + inventory_portion)
		evt = self._event(
			period,
			"fx_adjust",
			0,
			inventory_portion,
			map_before,
			inventory_portion=inventory_portion,
			expense_portion=expense_portion,
			fx_variance=fx_variance,
		)
		self._post(
			period,
			(Accounts.GRNI, receipt_total_base),
			(Accounts.STOCK, inventory_portion),
			(Accounts.PRICE_DIFF, expense_portion),
			(Accounts.FX, fx_variance),
			(Accounts.AP, -invoice_total_base),
		)
		self._after_mutation(period)
		return evt

	def count(self, counted_delta, period=None):
		"""Physical count difference: qty-only, valued at system MAP."""
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		rate = self.frozen_map if self.is_negative else self.map
		delta_value = r6(counted_delta * rate)
		self.qty = r6(self.qty + counted_delta)
		self.value = r6(self.value + delta_value)
		pb.adjust_qty = r6(pb.adjust_qty + counted_delta)
		pb.adjust_value = r6(pb.adjust_value + delta_value)
		evt = self._event(period, "count_diff", counted_delta, delta_value, map_before)
		offset = Accounts.COUNT_LOSS if counted_delta < 0 else Accounts.COUNT_GAIN
		self._post(period, (Accounts.STOCK, delta_value), (offset, -delta_value))
		self._after_mutation(period)
		return evt

	def revaluation(self, delta_value, period=None, account=Accounts.REVAL):
		"""MR21 value-only revaluation. Requires positive on-hand qty."""
		if self.qty <= 0:
			raise ValueError("revaluation requires positive stock")
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		self.value = r6(self.value + delta_value)
		self._recalc_map()
		pb.reval_value = r6(pb.reval_value + delta_value)
		evt = self._event(period, "revaluation", 0, delta_value, map_before)
		self._post(period, (Accounts.STOCK, delta_value), (account, -delta_value))
		self._after_mutation(period)
		return evt

	def return_in(self, qty, period=None, reference_event=None, account=Accounts.COGS):
		"""Sales return. With reference: at original issue unit cost, MAP recalcs.
		Without: at current MAP, MAP unchanged."""
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		if reference_event is not None:
			ref = self.events[reference_event - 1]
			unit_cost = abs(ref.value_delta / ref.qty_delta)
			reason = "return_with_ref"
		else:
			unit_cost = self.map
			reason = "return_no_ref"
		value = r6(qty * unit_cost)
		self.qty = r6(self.qty + qty)
		self.value = r6(self.value + value)
		if reference_event is not None:
			self._recalc_map()
		pb.receipt_qty = r6(pb.receipt_qty + qty)
		pb.receipt_value = r6(pb.receipt_value + value)
		evt = self._event(period, reason, qty, value, map_before, reference_event=reference_event)
		self._post(period, (Accounts.STOCK, value), (account, -value))
		self._after_mutation(period)
		return evt

	def return_out(self, qty, period=None, reference_event=None, account=Accounts.GRNI):
		"""Purchase return. With reference: at original receipt unit cost."""
		period = period or self.current_period
		pb = self._pb(period)
		map_before = self.map
		if reference_event is not None:
			ref = self.events[reference_event - 1]
			unit_cost = abs(ref.value_delta / ref.qty_delta)
			reason = "return_with_ref"
		else:
			unit_cost = self.map
			reason = "return_no_ref"
		value = r6(qty * unit_cost)
		self.qty = r6(self.qty - qty)
		self.value = r6(self.value - value)
		if reference_event is not None:
			self._recalc_map()
		pb.issue_qty = r6(pb.issue_qty + qty)
		pb.issue_value = r6(pb.issue_value + value)
		evt = self._event(period, reason, -qty, -value, map_before, reference_event=reference_event)
		self._post(period, (account, value), (Accounts.STOCK, -value))
		self._after_mutation(period)
		return evt

	# ------------------------------------------------------- backdated posting
	def backdated_receipt(self, qty, cost, prior_period, prior_state, account=Accounts.GRNI):
		"""Backdated receipt into the previous (still-open) period.

		``prior_state`` is a dict of the prior period's closing state at the
		time of backdating: {qty, value, frozen_map (0 if positive)}.

		Handles all three cases of the signed plan:
		- prior positive              -> plain receipt math in the prior period
		- prior negative, current pos -> Case C1 (PRD in prior + absorb in current)
		- prior negative, current neg -> Case C2 (cross-zero both, value absorb)

		The prior-period GL posts on the prior date; carryover propagates the
		prior closing delta into the current period; already-posted current-
		period events are never recalculated.
		"""
		pb_prior = self._pb(prior_period)
		pb_cur = self._pb(self.current_period)
		receipt_value = r6(qty * cost)

		p_qty = prior_state["qty"]
		p_frozen = prior_state.get("frozen_map") or 0.0

		if p_qty >= 0:
			# plain backdated receipt: prior recomputes, carryover to current
			pb_prior.receipt_qty = r6(pb_prior.receipt_qty + qty)
			pb_prior.receipt_value = r6(pb_prior.receipt_value + receipt_value)
			map_before = self.map
			self.qty = r6(self.qty + qty)
			self.value = r6(self.value + receipt_value)
			self.counter += qty
			self._recalc_map()
			pb_cur.carryover_qty = r6(pb_cur.carryover_qty + qty)
			pb_cur.carryover_value = r6(pb_cur.carryover_value + receipt_value)
			evt = self._event(prior_period, "receipt", qty, receipt_value, map_before)
			self._post(prior_period, (Accounts.STOCK, receipt_value), (account, -receipt_value))
			self._after_mutation(self.current_period)
			return evt

		# ---- prior period closed negative: PRD math at the prior frozen MAP
		clearing_p = min(qty, -p_qty)
		excess_p = r6(qty - clearing_p)
		prd_prior = r6((cost - p_frozen) * clearing_p) if excess_p > 0 else r6((cost - p_frozen) * qty)
		if excess_p > 0:
			prior_inventory = r6(clearing_p * p_frozen + excess_p * cost)
		else:
			prior_inventory = r6(qty * p_frozen)

		pb_prior.receipt_qty = r6(pb_prior.receipt_qty + qty)
		pb_prior.receipt_value = r6(pb_prior.receipt_value + receipt_value)
		pb_prior.prd_value = r6(pb_prior.prd_value - prd_prior)

		map_before = self.map
		evt = self._event(
			prior_period, "receipt_neg" if excess_p <= 0 else "receipt_cross_zero",
			qty, prior_inventory, map_before, prd_amount=prd_prior,
		)
		self._post(
			prior_period,
			(Accounts.STOCK, receipt_value),
			(account, -receipt_value),
			(Accounts.PRD, prd_prior),
			(Accounts.STOCK, -prd_prior),
		)

		# carryover: the prior closing delta cascades into the current period
		pb_cur.carryover_qty = r6(pb_cur.carryover_qty + qty)
		pb_cur.carryover_value = r6(pb_cur.carryover_value + prior_inventory)

		# current-period "should-have": the receipt as if posted today
		if self.qty >= 0:
			# Sub-case C1 — current positive
			should_have = receipt_value
		else:
			# Sub-case C2 — current also negative: cross-zero at current frozen
			clearing_c = min(qty, -self.qty)
			excess_c = r6(qty - clearing_c)
			should_have = r6(clearing_c * self.frozen_map + excess_c * cost)

		absorb = r2(should_have - prior_inventory)

		# apply carryover to the running state
		was_negative = self.qty < 0
		self.qty = r6(self.qty + qty)
		self.value = r6(self.value + prior_inventory)
		self.counter += qty

		if absorb:
			self.value = r6(self.value + absorb)
			pb_cur.adjust_value = r6(pb_cur.adjust_value + absorb)
			self._event(
				self.current_period, "prd_split", 0, absorb, self.map, caused_by=evt.event_id
			)
			self._post(
				self.current_period, (Accounts.STOCK, absorb), (Accounts.INV_VARIANCE, -absorb)
			)

		if was_negative and self.qty >= 0:
			# the backdated receipt crossed the current period's zero as well
			self.is_negative = False
			self.frozen_map = 0.0
			self.map = cost
		else:
			self._recalc_map()
		self._after_mutation(self.current_period)
		return evt

	# ------------------------------------------------------------ cancellation
	def cancel(self, event_id, period=None):
		"""Dated reversal: mirrors the original event with flipped signs in the
		cancellation's own period. Never mutates the original."""
		period = period or self.current_period
		orig = self.events[event_id - 1]
		if any(e.reason == "cancellation" and e.reference_event == event_id for e in self.events):
			raise ValueError("event already cancelled")
		pb = self._pb(period)
		map_before = self.map
		self.qty = r6(self.qty - orig.qty_delta)
		self.value = r6(self.value - orig.value_delta)
		if orig.qty_delta > 0:
			pb.receipt_qty = r6(pb.receipt_qty - orig.qty_delta)
			pb.receipt_value = r6(pb.receipt_value - orig.value_delta)
		elif orig.qty_delta < 0:
			pb.issue_qty = r6(pb.issue_qty + orig.qty_delta)
			pb.issue_value = r6(pb.issue_value + orig.value_delta)
		else:
			pb.reval_value = r6(pb.reval_value - orig.value_delta)
		self._recalc_map()
		evt = self._event(
			period, "cancellation", -orig.qty_delta, -orig.value_delta, map_before,
			reference_event=event_id,
		)
		# mirror the original's GL with swapped sides, on the cancellation date
		for leg in [g for g in self.gl if g.event_id == event_id]:
			self._post(period, (leg.account, leg.credit - leg.debit))
		self._after_mutation(period)
		return evt

	# ------------------------------------------------------------------ audit
	def stock_gl_net(self):
		return r2(
			sum(g.debit - g.credit for g in self.gl if g.account == Accounts.STOCK)
		)

	def gl_balanced(self):
		return r2(sum(g.debit for g in self.gl)) == r2(sum(g.credit for g in self.gl))
