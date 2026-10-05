# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

from periodic_valuation.periodic_standard_cost.engine import StdEngine, price_from, r2

LAST_DAY = "Last day of the period"


class ItemStandardCostVersion(Document):
	def validate(self):
		if not (1 <= (self.valid_from_month or 0) <= 12):
			frappe.throw(_("Valid From Month must be 1-12."))
		# the first day this version prices: its valid-from month, or the month
		# after the switch when it switches at period end (DR-50)
		y, m = price_from(self)
		self.effective_from = f"{y}-{m:02d}-01"
		if flt(self.standard_cost) <= 0:
			frappe.throw(_("Standard Cost must be positive."))

		from erpnext.stock.utils import get_valuation_method

		if get_valuation_method(self.item_code, self.company) != "Periodic Standard Cost":
			frappe.throw(
				_("{0} is not a Periodic Standard Cost item.").format(self.item_code)
			)

		include_wh = frappe.get_cached_value("Item", self.item_code, "valuation_includes_warehouse")
		self.warehouse = self.warehouse if include_wh else None

		target_locked = frappe.db.get_value(
			"Inventory Period",
			{"company": self.company, "period_year": self.valid_from_year,
			 "period_month": self.valid_from_month, "status": "SETTLED_FROZEN"},
		)
		if target_locked:
			frappe.throw(
				_("The target period {0}-{1:02d} is settled and frozen; a cost version cannot take effect there.").format(
					self.valid_from_year, self.valid_from_month
				)
			)

		# two RELEASED versions may share a valid-from month only when one
		# switches at period end (DR-50): the earlier keeps pricing until the
		# switch, so they never price the same month
		if self.status == "RELEASED" and any(
			price_from(x) == price_from(self)
			for x in frappe.get_all(
				"Item Standard Cost Version",
				filters={
					"company": self.company, "item_code": self.item_code,
					"warehouse": ("in", (self.warehouse or "", None)),
					"valid_from_year": self.valid_from_year,
					"valid_from_month": self.valid_from_month, "status": "RELEASED",
					"name": ("!=", self.name),
				},
				fields=["valid_from_year", "valid_from_month", "price_from_year", "price_from_month"],
			)
		):
			frappe.throw(
				_("A RELEASED version already exists for this scope and period."),
				title=_("Duplicate Version"),
			)

		if not self.is_new():
			old_status = frappe.db.get_value(self.doctype, self.name, "status")
			if old_status == "RELEASED" and not self.flags.via_release_flow:
				frappe.throw(
					_("Released cost versions are immutable. Release a new version instead."),
					title=_("Immutable"),
				)

	def onload(self):
		"""What the form shows about this version's revaluation (client
		ticket STD-002, 28/09: "the system does not display the generated
		Journal Voucher"). The revaluation posts GL rows under this document
		itself — there is no separate Journal Entry — so the form links to
		them, or says why there are none."""
		if self.status == "DRAFT":
			return
		gl = frappe.db.sql(
			"""select min(posting_date) as first, max(posting_date) as last, count(*) as n
			   from `tabGL Entry` where voucher_type = %s and voucher_no = %s and is_cancelled = 0""",
			(self.doctype, self.name),
			as_dict=True,
		)[0]
		events = frappe.db.count("Inventory Valuation Event", {
			"source_doctype": self.doctype, "source_docname": self.name, "is_cancelled": 0,
		})
		if gl.n:
			state = {"posted": True, "from_date": gl.first, "to_date": gl.last}
		elif not self.revaluation_posted:
			# client ticket STD-011 (04/10): a version that switches at period
			# end says so, with the dates, instead of "posts when <valid-from>
			# begins"
			state = {"posted": False, "reason": "switch_pending" if self.switch_at_period_end else "pending",
				"revaluation_date": self.revaluation_date, "effective_from": self.effective_from}
		elif not self.supersedes_version:
			state = {"posted": False, "reason": "first_version"}
		else:
			state = {"posted": False, "reason": "nothing_to_revalue"}
		state["events"] = events
		self.set_onload("revaluation", state)

		# the version that replaces this one (STD-011): under "Last day of the
		# period" the earlier version stays RELEASED and keeps pricing until
		# the switch - the resolver still needs it for that month - so the
		# form names its successor and the last day it is in force
		if self.status == "RELEASED":
			successor = frappe.get_all(
				"Item Standard Cost Version",
				filters={"supersedes_version": self.name, "status": "RELEASED"},
				fields=["name", "effective_from", "switch_at_period_end", "revaluation_posted"],
				order_by="released_on desc", limit=1,
			)
			if successor:
				s = successor[0]
				s["in_force_until"] = frappe.utils.add_days(s.effective_from, -1)
				self.set_onload("replaced_by", s)

	@frappe.whitelist()
	def release(self):
		"""Release this version. A same-period prior is replaced outright
		(SUPERSEDED); a prior from an earlier period stays RELEASED and simply
		stops being resolved once this version's boundary arrives. The
		revaluation triplet posts at the EFFECTIVE moment (plan: "posts a
		revaluation event on the boundary"): immediately for a version
		effective in the current or a past period (DR-12 granular), deferred
		to the valid-from boundary for a future-dated version."""
		if self.status != "DRAFT":
			frappe.throw(_("Only DRAFT versions can be released."))

		today = getdate(frappe.utils.nowdate())
		effective_now = (self.valid_from_year, self.valid_from_month) <= (today.year, today.month)

		# same-period re-price: replace outright so the unique-RELEASED check
		# passes. If the sibling's period had already arrived it WAS live, so
		# the delta is measured against it (ensure its own triplet is on the
		# books first); a future-period sibling was never live and is ignored
		# for delta purposes.
		same_period_prior = frappe.db.get_value(
			"Item Standard Cost Version",
			{
				"company": self.company, "item_code": self.item_code,
				"warehouse": ("in", (self.warehouse or "", None)), "status": "RELEASED",
				"valid_from_year": self.valid_from_year,
				"valid_from_month": self.valid_from_month,
				"name": ("!=", self.name),
			},
		)
		live_prior_sc = None
		last_day_mode = _last_day_mode(self.company)
		if same_period_prior:
			spp = frappe.get_doc("Item Standard Cost Version", same_period_prior)
			# a sibling that switches at period end is live only from its
			# prices-from month (DR-50)
			live = price_from(spp) <= (today.year, today.month)
			if live:
				spp.materialize_boundary()
				live_prior_sc = flt(spp.standard_cost)
			if live and last_day_mode:
				# DR-50: the live sibling keeps pricing until this version's
				# switch at period end - it is replaced then, not now
				same_period_prior = None
			else:
				frappe.db.set_value("Item Standard Cost Version", same_period_prior, "status", "SUPERSEDED")

		# DR-50 ("Last day of the period"): the old cost prices the switch
		# month - the valid-from month, or the release month when that is
		# later (a backdated change switches in the current period, as
		# STD-004) - and this version prices from the month after
		switch = max((self.valid_from_year, self.valid_from_month), (today.year, today.month))
		if last_day_mode:
			self.switch_at_period_end = 1
			self.price_from_year, self.price_from_month = _next_month(*switch)

		prior_name, prior_sc = self._resolve_effective_prior()
		if live_prior_sc is not None:
			prior_sc = live_prior_sc
		if last_day_mode and prior_sc is None:
			# the item's first cost: nothing to switch from, it prices its
			# valid-from month
			self.switch_at_period_end = 0
			self.price_from_year = self.price_from_month = None
		elif last_day_mode:
			from frappe.utils import get_last_day

			self.revaluation_date = get_last_day(f"{switch[0]}-{switch[1]:02d}-01")

		self.flags.via_release_flow = True
		self.status = "RELEASED"
		self.supersedes_version = same_period_prior or prior_name
		self.released_on = now_datetime()
		self.released_by = frappe.session.user
		self.save(ignore_permissions=False)

		if prior_sc is None:
			self.db_set("revaluation_posted", 1, update_modified=False)
		elif self.switch_at_period_end:
			# the closing stock is revalued once the switch month has ended
			# (materialize_boundary), or when that month settles
			if flt(self.standard_cost) == prior_sc:
				self.db_set("revaluation_posted", 1, update_modified=False)
		elif effective_now:
			if flt(self.standard_cost) == prior_sc:
				self.db_set("revaluation_posted", 1, update_modified=False)
			else:
				self.post_revaluation_triplet(prior_sc)
		# else: future-effective - the prior version keeps pricing until the
		# boundary; materialize_pending_revaluations (or the lazy backstop in
		# get_active_standard_cost) posts the triplet when the period arrives
		return self.name

	def _resolve_effective_prior(self):
		"""The version whose standard cost is in force just before this one
		takes effect: latest RELEASED with an effective period <= ours,
		excluding self (and any future-dated siblings)."""
		rows = frappe.get_all(
			"Item Standard Cost Version",
			filters={
				"company": self.company, "item_code": self.item_code,
				"warehouse": ("in", (self.warehouse or "", None)), "status": "RELEASED",
				"name": ("!=", self.name),
			},
			fields=["name", "standard_cost", "valid_from_year", "valid_from_month", "released_on",
				"price_from_year", "price_from_month"],
		)
		mine = price_from(self)
		candidates = [x for x in rows if price_from(x) <= mine]
		if not candidates:
			return None, None
		best = max(candidates, key=lambda x: (*price_from(x), x.released_on or ""))
		return best.name, flt(best.standard_cost)

	def materialize_boundary(self, force=False):
		"""Post this version's revaluation triplet once it is effective.
		old_sc is resolved NOW (not at release) so a superseded-in-between
		sibling never distorts the delta. Reentrancy-guarded because the
		engine's lazy backstop can reach here from inside a posting flow."""
		if frappe.flags.in_scv_materialize:
			return
		frappe.flags.in_scv_materialize = True
		try:
			if frappe.db.get_value(self.doctype, self.name, "revaluation_posted"):
				return
			if self.switch_at_period_end and not force and \
					getdate(frappe.utils.nowdate()) <= getdate(self.revaluation_date):
				return  # the switch month has not ended yet
			_prior_name, prior_sc = self._resolve_effective_prior()
			if prior_sc is None or flt(self.standard_cost) == prior_sc:
				self.db_set("revaluation_posted", 1, update_modified=False)
				return
			if self.switch_at_period_end:
				self.post_period_end_revaluation(prior_sc)
			else:
				self.post_revaluation_triplet(prior_sc)
		finally:
			frappe.flags.in_scv_materialize = False

	def post_revaluation_triplet(self, old_sc):
		engine = StdEngine(self.company, self.item_code, self.warehouse)
		today = getdate(frappe.utils.nowdate())
		# quantities are the month to date; the entries are dated day 1
		post_date = revaluation_posting_date(today)
		# The engine refuses only settled periods; a document posting checks
		# the period itself, and this one has to as well. Without it the
		# overnight job booked an October revaluation to the GL with no
		# October period, so the period balance and stock ledger never
		# received it (ISCV-2026-00044, badiav16, 01/10/2026).
		from periodic_valuation.shared.periods import assert_posting_allowed

		assert_posting_allowed(self.company, post_date)
		delta = flt(self.standard_cost) - old_sc

		if engine.view == "MTD":
			beg = engine.beg_qty_mtd(today.year, today.month)
			in_qty = engine.in_qty_mtd(today.year, today.month)
			out_qty = -engine.out_qty_mtd(today.year, today.month)
		else:
			beg = engine._reval_qty_at("Rev Beg", today, sc_new=flt(self.standard_cost), sc_old=old_sc)
			in_qty = engine._reval_qty_at("REV In", today, sc_new=flt(self.standard_cost), sc_old=old_sc)
			out_qty = engine._reval_qty_at("REV out", today, sc_new=flt(self.standard_cost), sc_old=old_sc)

		source = (self.doctype, self.name)
		posted = False
		for trans, qty in (("Rev Beg", beg), ("REV In", in_qty)):
			amount = r2(delta * qty)
			if amount:
				engine.post(trans=trans, posting_date=post_date, source=source,
					sc=self.standard_cost, ac=old_sc, t_sc_override=amount,
					cost_version=self.name)
				posted = True
		out_amount = r2(-(delta * out_qty))
		if out_amount:
			engine.post(trans="REV out", posting_date=post_date, source=source,
				sc=self.standard_cost, ac=old_sc, t_sc_override=out_amount,
				cost_version=self.name)
			posted = True
		if not posted:
			self._record_zero_revaluation(engine, "Rev Beg", post_date, old_sc)

		# restate the period balance: the triplet's net stock effect lands in
		# the reval bucket so GL == movement table holds across SC changes
		self._restate_period_balance(engine, post_date, delta, beg, in_qty, out_qty)
		self.db_set({"revaluation_posted": 1, "revaluation_date": post_date}, update_modified=False)

	def post_period_end_revaluation(self, old_sc):
		"""DR-50 ("Last day of the period"), after the client design §4.4 /
		§5.7: the movements of the switch month stay at the cost they were
		posted at, and only the quantity still on hand at the switch point is
		revalued - closing qty x (new - old), Dr Stock / Cr Standard Cost
		Revaluation Reserve, dated the month's last day (Rev End). The
		month's settlement gives it wholly to ending stock, so it carries into
		the next month's pool exactly as a day-1 boundary revaluation (Rev Beg)
		would sit there."""
		from periodic_valuation.periodic_moving_average.kernel import (
			ScopeState,
			ensure_physical_warehouse,
			recompute_closing,
			write_value_sle,
		)
		from periodic_valuation.periodic_standard_cost.kernel import _cascade_backdated_ipb
		from periodic_valuation.shared.periods import assert_posting_allowed, get_period

		engine = StdEngine(self.company, self.item_code, self.warehouse)
		day = getdate(self.revaluation_date)
		assert_posting_allowed(self.company, day)
		closing = engine.end_qty_mtd(day.year, day.month) if engine.view == "MTD" \
			else engine.end_qty_ytd(day.year, day.month)
		amount = r2((flt(self.standard_cost) - old_sc) * closing)
		if amount:
			source = (self.doctype, self.name)
			engine.post(trans="Rev End", posting_date=day, source=source, sc=self.standard_cost,
				ac=old_sc, t_sc_override=amount, cost_version=self.name)
			period = get_period(self.company, day)
			if period:
				scope = ScopeState(self.company, self.item_code, self.warehouse)
				ipb = scope.load(period)
				ipb.reval_value = flt(ipb.reval_value) + amount
				recompute_closing(ipb)
				scope.save(ipb, source=source)
				# a later period's balance row already open carries the
				# restated stock in its opening
				_cascade_backdated_ipb(scope, period, 0, amount, source=source)
				write_value_sle(ensure_physical_warehouse(scope), ipb, source=(self.doctype, self.name, None),
					posting_date=day, value_delta=amount)
		else:
			self._record_zero_revaluation(engine, "Rev End", day, old_sc)
		self.db_set("revaluation_posted", 1, update_modified=False)

	def _record_zero_revaluation(self, engine, trans, posting_date, old_sc):
		"""Client ticket STD-010 (04/10/2026): a cost change shows in the
		valuation log even when no stock was on hand or moved. One event
		of zero amount, linked to this version and dated as the revaluation
		would be (Rev Beg on day 1, or Rev End on the switch month's last
		day), records the old and new cost. It has no GL, no stock-ledger row
		and no quantity, so the period-close gates and the settlement pools
		are unchanged (as the zero-delta backdate companions in kernel.py)."""
		engine.post(trans=trans, posting_date=posting_date, source=(self.doctype, self.name),
			sc=self.standard_cost, ac=old_sc, t_sc_override=0, cost_version=self.name)

	def _restate_period_balance(self, engine, today, delta, beg, in_qty, out_qty):
		from periodic_valuation.periodic_moving_average.kernel import (
			ScopeState,
			ensure_physical_warehouse,
			recompute_closing,
			write_value_sle,
		)
		from periodic_valuation.shared.periods import get_period

		period = get_period(self.company, today)
		if not period:
			return
		scope = ScopeState(self.company, self.item_code, self.warehouse)
		ipb = scope.load(period)
		net_stock_effect = r2(delta * (beg + in_qty - out_qty))
		ipb.reval_value = flt(ipb.reval_value) + net_stock_effect
		recompute_closing(ipb)
		ipb.moving_avg_price = flt(self.standard_cost)
		ipb.period_standard_cost = flt(self.standard_cost)
		scope.save(ipb, source=(self.doctype, self.name))
		# mirror the net stock effect into the stock ledger (DR-02): the
		# revaluation triplet restates on-hand value at the new SC with no
		# SLE of its own, so core stock reports kept the old valuation
		write_value_sle(ensure_physical_warehouse(scope), ipb,
			source=(self.doctype, self.name, None),
			posting_date=today, value_delta=net_stock_effect)

	def on_trash(self):
		if self.status != "DRAFT":
			frappe.throw(_("Only DRAFT versions can be deleted."))


def revaluation_posting_date(today=None):
	"""The date a day-1 cost version's revaluation posts: day 1 of the month it
	posts in (client tickets STD-003 / STD-004, ruled 01/10/2026). A
	version valid from the current month posts on day 1 of that month, not
	on the day someone released it; a backdated version (valid from an
	earlier, still-open month) posts on day 1 of the current period; a
	future version posts when its month begins, on day 1. The revalued
	quantities are still the month to date when it posts. The last-day
	option is a different rule, not a different date: see
	post_period_end_revaluation (DR-50)."""
	from frappe.utils import get_first_day, nowdate

	return get_first_day(getdate(today or nowdate()))


def _last_day_mode(company):
	return frappe.db.get_value(
		"Periodic Standard Cost Settings", {"company": company}, "revaluation_posting_date"
	) == LAST_DAY


def _next_month(year, month):
	return (year + 1, 1) if month == 12 else (year, month + 1)


def materialize_period_end_revaluations(company, item_code, warehouse, year, month):
	"""Called as a period settles: any cost change that switches at this
	period's end revalues the closing stock now, before the settlement
	(after it nothing more can be dated in the period)."""
	from frappe.utils import get_first_day, get_last_day

	first = get_first_day(f"{year}-{month:02d}-01")
	for name in frappe.get_all(
		"Item Standard Cost Version",
		filters={
			"company": company, "item_code": item_code,
			"warehouse": ("in", (warehouse or "", None)),
			"status": "RELEASED", "switch_at_period_end": 1, "revaluation_posted": 0,
			"revaluation_date": ("between", (first, get_last_day(first))),
		},
		pluck="name",
	):
		frappe.get_doc("Item Standard Cost Version", name).materialize_boundary(force=True)


def materialize_pending_revaluations():
	"""Daily scheduler (with a lazy backstop in get_active_standard_cost):
	post the boundary revaluation for released versions whose valid-from
	period has arrived without a posted triplet. At a true boundary the
	triplet degenerates to Rev Beg = on-hand x delta (plan: 'std_revaluation
	for on-hand qty x delta on the target valid-from boundary'); any
	same-period activity since the boundary is picked up by the granular
	quantities."""
	from frappe.utils import getdate, nowdate

	today = getdate(nowdate())
	pending = frappe.get_all(
		"Item Standard Cost Version",
		filters={"status": "RELEASED", "revaluation_posted": 0},
		fields=["name", "valid_from_year", "valid_from_month"],
	)
	for row in pending:
		if (row.valid_from_year, row.valid_from_month) > (today.year, today.month):
			continue  # still future
		# a month whose period is not open yet keeps the version pending;
		# the next run posts it once the period opens
		frappe.db.savepoint("scv_materialize")
		try:
			frappe.get_doc("Item Standard Cost Version", row.name).materialize_boundary()
		except frappe.ValidationError:
			frappe.db.rollback(save_point="scv_materialize")
			frappe.clear_last_message()
