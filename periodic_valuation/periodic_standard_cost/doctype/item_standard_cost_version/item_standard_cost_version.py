# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

from periodic_valuation.periodic_standard_cost.engine import StdEngine, r2


class ItemStandardCostVersion(Document):
	def validate(self):
		if not (1 <= (self.valid_from_month or 0) <= 12):
			frappe.throw(_("Valid From Month must be 1-12."))
		self.effective_from = f"{self.valid_from_year}-{self.valid_from_month:02d}-01"
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

		if self.status == "RELEASED" and frappe.db.exists(
			"Item Standard Cost Version",
			{
				"company": self.company, "item_code": self.item_code,
				"warehouse": ("in", (self.warehouse or "", None)),
				"valid_from_year": self.valid_from_year,
				"valid_from_month": self.valid_from_month, "status": "RELEASED",
				"name": ("!=", self.name),
			},
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
		if gl.n:
			state = {"posted": True, "from_date": gl.first, "to_date": gl.last}
		elif not self.revaluation_posted:
			state = {"posted": False, "reason": "pending"}
		elif not self.supersedes_version:
			state = {"posted": False, "reason": "first_version"}
		else:
			state = {"posted": False, "reason": "nothing_to_revalue"}
		self.set_onload("revaluation", state)

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
		if same_period_prior:
			if effective_now:
				spp = frappe.get_doc("Item Standard Cost Version", same_period_prior)
				spp.materialize_boundary()
				live_prior_sc = flt(spp.standard_cost)
			frappe.db.set_value("Item Standard Cost Version", same_period_prior, "status", "SUPERSEDED")

		prior_name, prior_sc = self._resolve_effective_prior()
		if live_prior_sc is not None:
			prior_sc = live_prior_sc

		self.flags.via_release_flow = True
		self.status = "RELEASED"
		self.supersedes_version = same_period_prior or prior_name
		self.released_on = now_datetime()
		self.released_by = frappe.session.user
		self.save(ignore_permissions=False)

		if prior_sc is None:
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
			fields=["name", "standard_cost", "valid_from_year", "valid_from_month", "released_on"],
		)
		candidates = [
			x for x in rows
			if (x.valid_from_year, x.valid_from_month) <= (self.valid_from_year, self.valid_from_month)
		]
		if not candidates:
			return None, None
		best = max(candidates, key=lambda x: (x.valid_from_year, x.valid_from_month, x.released_on or ""))
		return best.name, flt(best.standard_cost)

	def materialize_boundary(self):
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
			_prior_name, prior_sc = self._resolve_effective_prior()
			if prior_sc is None or flt(self.standard_cost) == prior_sc:
				self.db_set("revaluation_posted", 1, update_modified=False)
				return
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
		for trans, qty in (("Rev Beg", beg), ("REV In", in_qty)):
			amount = r2(delta * qty)
			if amount:
				engine.post(trans=trans, posting_date=post_date, source=source,
					sc=self.standard_cost, ac=old_sc, t_sc_override=amount,
					cost_version=self.name)
		out_amount = r2(-(delta * out_qty))
		if out_amount:
			engine.post(trans="REV out", posting_date=post_date, source=source,
				sc=self.standard_cost, ac=old_sc, t_sc_override=out_amount,
				cost_version=self.name)

		# restate the period balance: the triplet's net stock effect lands in
		# the reval bucket so GL == movement table holds across SC changes
		self._restate_period_balance(engine, post_date, delta, beg, in_qty, out_qty)
		self.db_set("revaluation_posted", 1, update_modified=False)

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
	"""The date a cost version's revaluation posts: day 1 of the month it
	posts in (client tickets STD-003 / STD-004, ruled 01/10/2026). A
	version valid from the current month posts on day 1 of that month, not
	on the day someone released it; a backdated version (valid from an
	earlier, still-open month) posts on day 1 of the current period; a
	future version posts when its month begins, on day 1. The revalued
	quantities are still the month to date when it posts."""
	from frappe.utils import get_first_day, nowdate

	return get_first_day(getdate(today or nowdate()))


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
