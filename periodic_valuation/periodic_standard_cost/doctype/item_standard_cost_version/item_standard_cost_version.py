# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

from periodic_valuation.periodic_standard_cost.engine import StdEngine, price_from, r2, switch_order

RELEASE_DAY = "Date of release"
# the option's name before the DR-50 amendment (05/10/2026); the
# switch_cost_changes_at_release patch moves settings off it
LEGACY_LAST_DAY = "Last day of the period"


class ItemStandardCostVersion(Document):
	def validate(self):
		if not (1 <= (self.valid_from_month or 0) <= 12):
			frappe.throw(_("Valid From Month must be 1-12."))
		# the first day this version prices: day 1 of its valid-from month, or
		# its release date when it switches at release (DR-50 as amended)
		if self.switch_on_release and self.revaluation_date:
			self.effective_from = self.revaluation_date
		else:
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
		# switches at its release (DR-50): the earlier keeps pricing the dates
		# before the switch, so they never price the same day
		if self.status == "RELEASED" and not self.switch_on_release and any(
			price_from(x) == price_from(self) and not x.switch_on_release
			for x in frappe.get_all(
				"Item Standard Cost Version",
				filters={
					"company": self.company, "item_code": self.item_code,
					"warehouse": ("in", (self.warehouse or "", None)),
					"valid_from_year": self.valid_from_year,
					"valid_from_month": self.valid_from_month, "status": "RELEASED",
					"name": ("!=", self.name),
				},
				fields=["valid_from_year", "valid_from_month", "price_from_year", "price_from_month",
					"switch_on_release"],
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

		# the version that replaces this one (STD-011): when the successor
		# switches at its release the earlier version stays RELEASED and keeps
		# pricing the dates before the switch - the resolver still needs it -
		# so the form names its successor and the last day it is in force
		if self.status == "RELEASED":
			successor = frappe.get_all(
				"Item Standard Cost Version",
				filters={"supersedes_version": self.name, "status": "RELEASED"},
				fields=["name", "effective_from", "switch_at_period_end", "switch_on_release",
					"revaluation_posted"],
				order_by="released_on desc", limit=1,
			)
			if successor:
				s = successor[0]
				s["in_force_until"] = frappe.utils.add_days(s.effective_from, -1)
				self.set_onload("replaced_by", s)

	@frappe.whitelist()
	def release(self):
		"""Release this version.

		"First day of the period": a same-period prior is replaced outright
		(SUPERSEDED); a prior from an earlier period stays RELEASED and simply
		stops being resolved once this version's boundary arrives. The
		revaluation triplet posts at the EFFECTIVE moment: immediately for a
		version effective in the current or a past period (DR-12 granular),
		deferred to the valid-from boundary for a future-dated version.

		"Date of release" (DR-50 as amended 05/10/2026): a version effective
		in the current or a past period switches at its release, inside the
		current period. Movements dated before the release keep the cost in
		force then; this version prices from the release date; the stock on
		hand is revalued once, dated the release day (Rev Rel). A
		future-dated version switches on day 1 of its valid-from month, as
		under the first-day rule."""
		if self.status != "DRAFT":
			frappe.throw(_("Only DRAFT versions can be released."))

		today = getdate(frappe.utils.nowdate())
		effective_now = (self.valid_from_year, self.valid_from_month) <= (today.year, today.month)
		switch_now = effective_now and _release_day_mode(self.company)

		# same-period re-price: replace outright so the unique-RELEASED check
		# passes. If the sibling's period had already arrived it WAS live, so
		# the delta is measured against it (ensure its own revaluation is on
		# the books first); a future-period sibling was never live and is
		# ignored for delta purposes.
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
			spp = frappe.get_doc("Item Standard Cost Version", same_period_prior)
			live = price_from(spp) <= (today.year, today.month)
			if live:
				spp.materialize_boundary()
				live_prior_sc = flt(spp.standard_cost)
			if live and switch_now:
				# the live sibling keeps pricing the dates before this
				# version's switch - it is replaced from the release, not
				# retroactively
				same_period_prior = None
			else:
				frappe.db.set_value("Item Standard Cost Version", same_period_prior, "status", "SUPERSEDED")

		if switch_now:
			self.switch_on_release = 1
			self.price_from_year, self.price_from_month = today.year, today.month
			self.revaluation_date = today

		prior_name, prior_sc = self._resolve_effective_prior()
		if live_prior_sc is not None:
			prior_sc = live_prior_sc
		if switch_now and prior_sc is None:
			# the item's first cost: nothing to switch from, it prices its
			# whole valid-from month
			self.switch_on_release = 0
			self.price_from_year = self.price_from_month = None
			self.revaluation_date = None

		self.flags.via_release_flow = True
		self.status = "RELEASED"
		self.supersedes_version = same_period_prior or prior_name
		self.released_on = now_datetime()
		self.released_by = frappe.session.user
		self.save(ignore_permissions=False)

		if prior_sc is None or (effective_now and flt(self.standard_cost) == prior_sc):
			self.db_set("revaluation_posted", 1, update_modified=False)
		elif self.switch_on_release:
			self.post_release_revaluation(prior_sc)
		elif effective_now:
			self.post_revaluation_triplet(prior_sc)
		# else: future-effective - the prior version keeps pricing until the
		# boundary; materialize_pending_revaluations (or the lazy backstop in
		# get_active_standard_cost) posts the triplet when the period arrives
		return self.name

	def _resolve_effective_prior(self):
		"""The version whose standard cost is in force just before this one
		takes effect: the latest RELEASED version to take over pricing no
		later than this one, excluding self (and any future-dated siblings)."""
		rows = frappe.get_all(
			"Item Standard Cost Version",
			filters={
				"company": self.company, "item_code": self.item_code,
				"warehouse": ("in", (self.warehouse or "", None)), "status": "RELEASED",
				"name": ("!=", self.name),
			},
			fields=["name", "standard_cost", "valid_from_year", "valid_from_month", "released_on",
				"price_from_year", "price_from_month", "switch_on_release", "effective_from"],
		)
		mine = switch_order(frappe._dict(
			price_from_year=self.price_from_year, price_from_month=self.price_from_month,
			valid_from_year=self.valid_from_year, valid_from_month=self.valid_from_month,
			switch_on_release=self.switch_on_release, effective_from=self.revaluation_date,
			released_on=self.released_on or now_datetime(),
		))
		candidates = [x for x in rows if switch_order(x) < mine]
		if not candidates:
			return None, None
		best = max(candidates, key=switch_order)
		return best.name, flt(best.standard_cost)

	def materialize_boundary(self, force=False):
		"""Post this version's revaluation once it is effective. old_sc is
		resolved NOW (not at release) so a superseded-in-between sibling never
		distorts the delta. Reentrancy-guarded because the engine's lazy
		backstop can reach here from inside a posting flow."""
		if frappe.flags.in_scv_materialize:
			return
		frappe.flags.in_scv_materialize = True
		try:
			if frappe.db.get_value(self.doctype, self.name, "revaluation_posted"):
				return
			if self.switch_at_period_end and not force and \
					getdate(frappe.utils.nowdate()) <= getdate(self.revaluation_date):
				return  # legacy period-end switch: the month has not ended yet
			_prior_name, prior_sc = self._resolve_effective_prior()
			if prior_sc is None or flt(self.standard_cost) == prior_sc:
				self.db_set("revaluation_posted", 1, update_modified=False)
				return
			if self.switch_on_release:
				self.post_release_revaluation(prior_sc)
			elif self.switch_at_period_end:
				self.post_period_end_revaluation(prior_sc)
			else:
				self.post_revaluation_triplet(prior_sc)
		finally:
			frappe.flags.in_scv_materialize = False

	def post_release_revaluation(self, old_sc):
		"""DR-50 as amended (05/10/2026, "Date of release"): the stock on hand
		when the cost changes is revalued once, dated the release day -
		on hand x (new - old), Dr Stock In Hand / Cr Standard Cost
		Revaluation Reserve (Rev Rel). Movements already posted keep their
		cost; later entries dated before the release bridge into it
		(kernel._bridge_release_switch). The settlement shares it between
		ending stock and the consumption after the switch
		(StdEngine._switch_revaluations)."""
		from periodic_valuation.periodic_standard_cost.kernel import book_revaluation
		from periodic_valuation.shared.periods import assert_posting_allowed

		engine = StdEngine(self.company, self.item_code, self.warehouse)
		day = getdate(self.revaluation_date)
		assert_posting_allowed(self.company, day)
		on_hand = engine.end_qty_mtd(day.year, day.month) if engine.view == "MTD" \
			else engine.end_qty_ytd(day.year, day.month)
		amount = r2((flt(self.standard_cost) - old_sc) * on_hand)
		source = (self.doctype, self.name)
		if amount:
			engine.post(trans="Rev Rel", posting_date=day, source=source, sc=self.standard_cost,
				ac=old_sc, t_sc_override=amount, cost_version=self.name)
			book_revaluation(engine, day, amount, source, standard_cost=self.standard_cost)
		else:
			self._record_zero_revaluation(engine, "Rev Rel", day, old_sc)
		self.db_set("revaluation_posted", 1, update_modified=False)

	def post_period_end_revaluation(self, old_sc):
		"""DR-50 before its 05/10/2026 amendment ("Last day of the period"):
		only the quantity still on hand at the end of the switch month is
		revalued - closing qty x (new - old), dated the month's last day
		(Rev End), given wholly to ending stock at settlement. New releases
		no longer switch this way; this posts for a version released under
		the old rule and not yet re-stamped."""
		from periodic_valuation.periodic_standard_cost.kernel import book_revaluation
		from periodic_valuation.shared.periods import assert_posting_allowed

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
			book_revaluation(engine, day, amount, source)
		else:
			self._record_zero_revaluation(engine, "Rev End", day, old_sc)
		self.db_set("revaluation_posted", 1, update_modified=False)

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

	def _record_zero_revaluation(self, engine, trans, posting_date, old_sc):
		"""Client ticket STD-010 (04/10/2026): a cost change shows in the
		valuation log even when no stock was on hand or moved. One event
		of zero amount, linked to this version and dated as the revaluation
		would be (Rev Beg on day 1, or Rev Rel on the release day), records the old and new cost. It has no GL, no stock-ledger row
		and no quantity, so the period-close gates and the settlement pools
		are unchanged (as the zero-delta backdate companions in kernel.py)."""
		engine.post(trans=trans, posting_date=posting_date, source=(self.doctype, self.name),
			sc=self.standard_cost, ac=old_sc, t_sc_override=0, cost_version=self.name)

	def _restate_period_balance(self, engine, today, delta, beg, in_qty, out_qty):
		"""The triplet's net stock effect lands in the reval bucket so GL ==
		movement table holds across SC changes, and is mirrored into the
		stock ledger (DR-02) - the triplet restates on-hand value at the new
		SC with no SLE of its own."""
		from periodic_valuation.periodic_standard_cost.kernel import book_revaluation

		book_revaluation(engine, today, r2(delta * (beg + in_qty - out_qty)), (self.doctype, self.name),
			standard_cost=self.standard_cost)

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
	quantities are still the month to date when it posts. The date-of-release
	option is a different rule, not a different date: see
	post_release_revaluation (DR-50)."""
	from frappe.utils import get_first_day, nowdate

	return get_first_day(getdate(today or nowdate()))


def _release_day_mode(company):
	"""Also true on the pre-amendment value, so a release made between the
	deploy and the migration patch already switches at release."""
	return frappe.db.get_value(
		"Periodic Standard Cost Settings", {"company": company}, "revaluation_posting_date"
	) in (RELEASE_DAY, LEGACY_LAST_DAY)


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


def restamp_period_end_switches():
	"""DR-50 amendment (05/10/2026): a version released under the old
	"Last day of the period" rule and still waiting for its month-end
	revaluation switches at its release instead. In release order, each
	version prices from its release date and revalues the stock on hand,
	dated its release day; a movement posted since the release at the old
	cost is part of that stock, so it carries the new cost from here on.
	A version released in an earlier month than the re-stamp switches on the
	re-stamp day instead: the current month's movements were priced at the
	old cost and stay so. A version for a later month than its release goes
	back to the day-1 boundary of that month. A version whose switch day
	falls in a month that cannot take a posting stays as it is.

	Returns {"restamped": [...], "boundary": [...], "skipped": [...],
	"failed": [...]} so the caller can report what it changed."""
	from periodic_valuation.shared.periods import get_period, period_refusal

	today = getdate(frappe.utils.nowdate())
	outcome = {"restamped": [], "boundary": [], "skipped": [], "failed": []}
	for row in frappe.get_all(
		"Item Standard Cost Version",
		filters={"status": "RELEASED", "switch_at_period_end": 1, "revaluation_posted": 0},
		fields=["name", "company", "released_on"],
		order_by="released_on asc",
	):
		released = getdate(row.released_on)
		doc = frappe.get_doc("Item Standard Cost Version", row.name)
		if (doc.valid_from_year, doc.valid_from_month) > (released.year, released.month):
			# a change for a later month than its release: under the amended
			# rule it switches on day 1 of that month (first-day boundary);
			# materialize_pending_revaluations posts it when the month begins
			doc.flags.via_release_flow = True
			doc.switch_at_period_end = 0
			doc.price_from_year = doc.price_from_month = None
			doc.revaluation_date = None
			doc.save(ignore_permissions=True)
			outcome["boundary"].append(row.name)
			continue
		day = released if (released.year, released.month) == (today.year, today.month) else today
		period = get_period(row.company, day)
		if not period or period_refusal(period):
			outcome["skipped"].append(row.name)
			continue
		frappe.db.savepoint("scv_restamp")
		try:
			doc.flags.via_release_flow = True
			doc.switch_at_period_end = 0
			doc.switch_on_release = 1
			doc.price_from_year, doc.price_from_month = day.year, day.month
			doc.revaluation_date = day
			doc.save(ignore_permissions=True)
			doc.add_comment("Info", _(
				"Re-stamped under the amended DR-50: switches on {0} instead of the period end."
			).format(frappe.format(day, "Date")))
			doc.materialize_boundary()
			if not frappe.db.get_value(doc.doctype, doc.name, "revaluation_posted"):
				raise frappe.ValidationError(f"{row.name}: revaluation not posted")
			outcome["restamped"].append(row.name)
		except Exception:
			frappe.db.rollback(save_point="scv_restamp")
			frappe.log_error(title=f"DR-50 re-stamp failed: {row.name}")
			outcome["failed"].append(row.name)
	return outcome
