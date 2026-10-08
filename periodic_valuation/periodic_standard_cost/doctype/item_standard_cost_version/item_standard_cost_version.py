# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_first_day, getdate, now_datetime

from periodic_valuation.periodic_standard_cost.engine import StdEngine, price_from, r2, switch_order

RELEASE_DAY = "Latest day of the period"
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

		# DR-23: a release may not target a frozen month. A version re-stamped
		# under the DR-50 amendment was released while its valid-from month was
		# open and now takes effect at its switch month, so that is the month
		# checked (the valid-from month may have closed since)
		target = price_from(self) if self.flags.restamp else (self.valid_from_year, self.valid_from_month)
		target_locked = frappe.db.get_value(
			"Inventory Period",
			{"company": self.company, "period_year": target[0],
			 "period_month": target[1], "status": "SETTLED_FROZEN"},
		)
		if target_locked:
			frappe.throw(
				_("The target period {0}-{1:02d} is settled and frozen; a cost version cannot take effect there.").format(
					*target
				)
			)

		# two RELEASED versions may share a valid-from month only when one
		# switches at its release (DR-50): the earlier keeps pricing the dates
		# before the switch, so they never price the same day
		if self.status == "RELEASED" and not self.switch_on_release and not self.effective_to and any(
			price_from(x) == price_from(self) and not x.switch_on_release and not x.effective_to
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
					"switch_on_release", "effective_to"],
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
		"""Release, then give every RELEASED version of the item its window -
		Effective From to Effective To, never overlapping (client design
		§3 / §4.1 / §4.5; DR-63) - and refuse the release if two would
		overlap."""
		name = self._release()
		normalize_cost_windows(self.company, self.item_code, self.warehouse)
		return name

	def _release(self):
		"""Release this version (DR-55, client answers 06/10/2026).

		Both settings price from the valid-from month and revalue with the
		day-1 triplet (Rev Beg / REV In / REV out over the month to date);
		Periodic Standard Cost Settings > Revaluation Posting Date chooses
		only the dates:
		- a change for the current month: day 1 of the month ("First day of
		  the period") or the release day ("Latest day of the period");
		- a change for the previous period (PREV_OPEN_UNSETTLED) not settled
		  for the item is a correction of that month only (DR-57): that month
		  is revalued on its day 1 or its last day, the revaluation reverses
		  on day 1 of the current period or the release day (DR-59), and the
		  current period keeps its own standard;
		- a change for a month after the current period cannot be released.
		The current period is the OPEN Inventory Period, not the calendar
		month (DR-64).
		A same-period prior is replaced outright (SUPERSEDED)."""
		if self.status != "DRAFT":
			frappe.throw(_("Only DRAFT versions can be released."))

		from periodic_valuation.shared.periods import current_period_day

		today = current_period_day(self.company)
		if (self.valid_from_year, self.valid_from_month) > (today.year, today.month):
			frappe.throw(
				_("A cost version for {0}-{1:02d} cannot be released: the current inventory period is {2}-{3:02d}. Release it once that period is open.").format(
					self.valid_from_year, self.valid_from_month, today.year, today.month
				),
				title=_("Future Month"),
			)
		latest = _release_day_mode(self.company)

		# same-period re-price: the latest release for the same month (a
		# corrected month can hold its original version and corrections,
		# DR-57); its own revaluation is on the books first and the delta is
		# measured against it. It is replaced outright, except by a correction
		# of an earlier month, which leaves it pricing the months after
		same_period_prior = (frappe.get_all(
			"Item Standard Cost Version",
			filters={
				"company": self.company, "item_code": self.item_code,
				"warehouse": ("in", (self.warehouse or "", None)), "status": "RELEASED",
				"valid_from_year": self.valid_from_year,
				"valid_from_month": self.valid_from_month,
				"name": ("!=", self.name),
			},
			order_by="released_on desc", limit=1, pluck="name",
		) or [None])[0]
		correction = self._prior_period_open(today)
		live_prior_sc = None
		if same_period_prior:
			spp = frappe.get_doc("Item Standard Cost Version", same_period_prior)
			if price_from(spp) <= (today.year, today.month):
				spp.materialize_boundary()
				live_prior_sc = flt(spp.standard_cost)
			if not correction:
				frappe.db.set_value("Item Standard Cost Version", same_period_prior, "status", "SUPERSEDED")
			# a correction leaves it RELEASED: it keeps pricing the months after

		prior_name, prior_sc = self._resolve_effective_prior()
		if live_prior_sc is not None:
			prior_sc = live_prior_sc
		if correction and prior_sc is not None:
			# DR-57: a backdated change corrects its own month only
			from frappe.utils import get_last_day

			self.effective_to = get_last_day(f"{self.valid_from_year}-{self.valid_from_month:02d}-01")
			self.is_correction = 1

		self.flags.via_release_flow = True
		self.status = "RELEASED"
		self.supersedes_version = same_period_prior or prior_name
		self.released_on = now_datetime()
		self.released_by = frappe.session.user
		self.save(ignore_permissions=False)

		if prior_sc is None:
			# the item's first cost is logged as a zero-value event too
			# (client, 06/10/2026, case 6), dated as a revaluation would be
			engine = StdEngine(self.company, self.item_code, self.warehouse)
			first_day = getdate(f"{self.valid_from_year}-{self.valid_from_month:02d}-01")
			self._record_zero_revaluation(engine, "Rev Beg", today if latest else first_day, 0.0)
			self.db_set({"revaluation_posted": 1, "revaluation_date": today if latest else first_day},
				update_modified=False)
			return self.name
		if flt(self.standard_cost) == prior_sc:
			self.db_set("revaluation_posted", 1, update_modified=False)
			return self.name
		if self.effective_to:
			# DR-57 (client 06/10/2026: "the first reversal should exist";
			# STD-004): revalue the corrected month, reverse it on day 1 of the
			# current month; the current month keeps its own standard
			self.post_prior_period_revaluation(prior_sc, today, latest)
		else:
			self.post_revaluation_triplet(prior_sc, post_on=today if latest else None)
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
				"price_from_year", "price_from_month", "switch_on_release", "effective_from", "effective_to"],
		)
		# a correction of an earlier month (DR-57) is not in force after it
		my_start = getdate(f"{price_from(self)[0]}-{price_from(self)[1]:02d}-01")
		rows = [x for x in rows if not (x.effective_to and getdate(x.effective_to) < my_start)]
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

	def _prior_period_open(self, today):
		"""A backdated version: its valid-from month is the previous period
		(PREV_OPEN_UNSETTLED - DR-64, not an earlier calendar month) and is not
		settled for this scope (DR-54). A settled month keeps the forward
		revaluation in the current period (client design §8.A)."""
		from periodic_valuation.shared.periods import get_period, is_backdate_period

		period = get_period(self.company, f"{self.valid_from_year}-{self.valid_from_month:02d}-01")
		if not is_backdate_period(period):
			return False
		engine = StdEngine(self.company, self.item_code, self.warehouse)
		return not engine.is_period_locked(self.valid_from_year, self.valid_from_month)

	def post_prior_period_revaluation(self, old_sc, today, latest=False):
		"""DR-57 (client 06/10/2026, tickets STD-003 / STD-004): a change for
		an earlier month that is still open and not settled corrects THAT
		month only (effective_to = its last day). The month is revalued with
		its triplet, dated its day 1 ("First day of the period") or its last
		day ("Latest day of the period"); on day 1 of the current month the
		stock it handed over is revalued back (Rev Reverse, dated day 1 or the
		release day per the option - DR-59 - Standard Cost
		Revaluation Reserve against Stock In Hand), because the current month
		keeps the standard in force there and its stock must stand at that
		standard. The corrected month's consumption adjustment (REV out) stays
		in that month. Nothing else posts in the current month."""
		from frappe.utils import get_first_day, get_last_day

		from periodic_valuation.periodic_standard_cost.kernel import book_revaluation
		from periodic_valuation.shared.periods import assert_posting_allowed

		from periodic_valuation.periodic_standard_cost.engine import backdate_entry_date

		# the reversal lands in the current month, dated by the Revaluation
		# Posting Date option (STD-004, DR-59): refuse before posting anything
		# into the earlier month when the current one cannot take it
		day1 = backdate_entry_date(self.company, today)
		assert_posting_allowed(self.company, day1)
		month_end = get_last_day(f"{self.valid_from_year}-{self.valid_from_month:02d}-01")
		post_on = month_end if latest else None
		_rev, _out, net = self.post_revaluation_triplet(old_sc, as_of=month_end, prior_period=True, post_on=post_on)
		engine = StdEngine(self.company, self.item_code, self.warehouse)
		source = (self.doctype, self.name)
		revalued_on = month_end if latest else get_first_day(month_end)
		if r2(net):
			engine.post(trans="Rev Reverse", posting_date=day1, source=source, sc=old_sc,
				ac=self.standard_cost, t_sc_override=-r2(net), cost_version=self.name)
			book_revaluation(engine, day1, -r2(net), source, standard_cost=old_sc)
		elif not frappe.db.exists("Inventory Valuation Event", {"source_docname": self.name, "is_cancelled": 0}):
			# nothing to revalue: the change is still logged (DR-51)
			self._record_zero_revaluation(engine, "Rev Beg", revalued_on, old_sc)
		self.db_set({"revaluation_posted": 1, "revaluation_date": revalued_on}, update_modified=False)

	def post_release_revaluation(self, old_sc):
		"""DR-50 as amended (05/10/2026, "Latest day of the period"): the stock on hand
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

	def post_revaluation_triplet(self, old_sc, as_of=None, prior_period=False, post_on=None):
		"""The day-1 revaluation triplet (DR-12 / DR-49) of the month `as_of`
		falls in - today's month by default, or a backdated version's
		valid-from month (`prior_period`, DR-54) revalued over that whole
		month. Returns (Rev Beg + REV In amount, REV out amount, net stock
		effect) as posted."""
		from periodic_valuation.shared.periods import current_period_day

		engine = StdEngine(self.company, self.item_code, self.warehouse)
		today = getdate(as_of) if as_of else current_period_day(self.company)
		# quantities are the month to date; the entries are dated day 1, or on
		# `post_on` under "Latest day of the period" (DR-55)
		post_date = getdate(post_on) if post_on else revaluation_posting_date(today)
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
		# the drift guard measures the buckets as of the same day (entry_date)
		for trans, qty in (("Rev Beg", beg), ("REV In", in_qty)):
			amount = r2(delta * qty)
			if amount:
				engine.post(trans=trans, posting_date=post_date, source=source,
					sc=self.standard_cost, ac=old_sc, t_sc_override=amount,
					cost_version=self.name, entry_date=today)
				posted = True
		out_amount = r2(-(delta * out_qty))
		rev_amount = r2(delta * beg) + r2(delta * in_qty)
		if out_amount:
			engine.post(trans="REV out", posting_date=post_date, source=source,
				sc=self.standard_cost, ac=old_sc, t_sc_override=out_amount,
				cost_version=self.name, entry_date=today)
			posted = True
		if not posted and not prior_period:
			self._record_zero_revaluation(engine, "Rev Beg", post_date, old_sc)

		# restate the period balance: the triplet's net stock effect lands in
		# the reval bucket so GL == movement table holds across SC changes
		self._restate_period_balance(engine, post_date, delta, beg, in_qty, out_qty)
		if not prior_period:
			self.db_set({"revaluation_posted": 1, "revaluation_date": post_date}, update_modified=False)
		return rev_amount, out_amount, r2(delta * (beg + in_qty - out_qty))

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
	re-stamp day instead, so its revaluation lands in the current period
	(the release month could not take a movement at the new cost by then). A version for a later month than its release goes
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
			frappe.db.savepoint("scv_restamp")
			try:
				doc.flags.via_release_flow = True
				doc.flags.restamp = True
				doc.switch_at_period_end = 0
				doc.price_from_year = doc.price_from_month = None
				doc.revaluation_date = None
				doc.save(ignore_permissions=True)
				outcome["boundary"].append(row.name)
			except Exception:
				frappe.db.rollback(save_point="scv_restamp")
				frappe.log_error(title=f"DR-50 re-stamp failed: {row.name}")
				outcome["failed"].append(row.name)
			continue
		day = released if (released.year, released.month) == (today.year, today.month) else today
		period = get_period(row.company, day)
		if not period or period_refusal(period):
			outcome["skipped"].append(row.name)
			continue
		frappe.db.savepoint("scv_restamp")
		try:
			doc.flags.via_release_flow = True
			doc.flags.restamp = True
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


WINDOW_FIELDS = ["name", "company", "item_code", "warehouse", "standard_cost", "price_unit",
	"valid_from_year", "valid_from_month", "price_from_year", "price_from_month",
	"effective_from", "effective_to", "released_on", "switch_on_release"]


def normalize_cost_windows(company, item_code, warehouse):
	"""Client design §3 / §4.1 / §4.5 (DR-63): every RELEASED version holds a
	window - Effective From to Effective To (open for the last) - and no two
	windows overlap; several versions may share a month. The windows are
	read off the cost lookup itself (engine.resolve_version), day by day, so
	they can never disagree with the cost any date resolves to:
	- the days a version resolves become its window;
	- a version that resolves no day any more is SUPERSEDED (DR-62);
	- a version whose days break into two runs - a backdated correction
	  took a month out of its middle (DR-57) - keeps the first run, and a
	  continuation version at the same cost takes each later run
	  (source "Correction Continuation", nothing to revalue).
	Then the windows are checked: an overlap refuses the release."""
	from datetime import timedelta

	from periodic_valuation.periodic_standard_cost.engine import resolve_version

	filters = {"company": company, "item_code": item_code,
		"warehouse": ("in", (warehouse or "", None)), "status": "RELEASED"}
	rows = frappe.get_all("Item Standard Cost Version", filters=filters, fields=WINDOW_FIELDS)
	if not rows:
		return {}
	today = getdate(frappe.utils.nowdate())

	def month_start(x):
		y, m = price_from(x)
		return getdate(f"{y}-{m:02d}-01")

	start = min(month_start(x) for x in rows)
	horizon = max([today] + [month_start(x) for x in rows]) + timedelta(days=62)
	runs = {x.name: [] for x in rows}
	day = start
	while day <= horizon:
		v = resolve_version(rows, day)
		if v:
			r = runs[v.name]
			if r and r[-1][1] == day - timedelta(days=1):
				r[-1][1] = day
			else:
				r.append([day, day])
		day += timedelta(days=1)

	outcome = {"superseded": [], "continued": [], "windows": 0}
	for x in rows:
		r = runs[x.name]
		if not r:
			frappe.db.set_value("Item Standard Cost Version", x.name, "status", "SUPERSEDED", update_modified=False)
			outcome["superseded"].append(x.name)
			continue
		for i, (s_, e_) in enumerate(r):
			is_open = i == len(r) - 1 and e_ == horizon
			to = None if is_open else e_
			if i == 0:
				values = {"effective_from": s_, "effective_to": to}
				if s_.day == 1 and (s_.year, s_.month) > price_from(x) and not x.switch_on_release:
					values.update(price_from_year=s_.year, price_from_month=s_.month)
				if (getdate(x.effective_from) if x.effective_from else None) != s_ \
						or (getdate(x.effective_to) if x.effective_to else None) != to \
						or "price_from_year" in values:
					frappe.db.set_value("Item Standard Cost Version", x.name, values, update_modified=False)
					outcome["windows"] += 1
			else:
				outcome["continued"].append(_continue_version(x, s_, to))
	assert_no_overlap(company, item_code, warehouse)
	return outcome


def _continue_version(version, start, end):
	"""A continuation of `version` from `start` (a month's day 1): same cost,
	nothing to revalue - it carries the version on after a correction took a
	month out of its window (DR-63)."""
	if start.day != 1:
		frappe.throw(_("{0} cannot continue from {1}: a continuation starts on the first day of a month.").format(
			version.name, frappe.format(start, "Date")))
	doc = frappe.get_doc({
		"doctype": "Item Standard Cost Version", "company": version.company,
		"item_code": version.item_code, "warehouse": version.warehouse,
		"valid_from_year": start.year, "valid_from_month": start.month,
		"standard_cost": version.standard_cost, "price_unit": version.price_unit,
		"source_type": "CORRECTION_CONTINUATION", "status": "RELEASED",
		"supersedes_version": version.name, "released_on": now_datetime(),
		"released_by": frappe.session.user, "revaluation_posted": 1,
		"remarks": _("Continues {0} after a correction of an earlier month.").format(version.name),
	})
	doc.flags.via_release_flow = True
	doc.insert(ignore_permissions=True)
	if end:
		doc.db_set("effective_to", end, update_modified=False)
	return doc.name


def assert_no_overlap(company, item_code, warehouse):
	"""Design §4.1 rule 5: no two RELEASED versions may price the same day."""
	rows = frappe.get_all("Item Standard Cost Version", filters={
		"company": company, "item_code": item_code,
		"warehouse": ("in", (warehouse or "", None)), "status": "RELEASED"},
		fields=["name", "effective_from", "effective_to"], order_by="effective_from asc, released_on asc")
	for prev, nxt in zip(rows, rows[1:]):
		if not prev.effective_to or getdate(prev.effective_to) >= getdate(nxt.effective_from):
			frappe.throw(
				_("Standard cost windows overlap for {0}: {1} ({2} to {3}) and {4} (from {5}).").format(
					item_code, prev.name, prev.effective_from, prev.effective_to or _("open"),
					nxt.name, nxt.effective_from),
				title=_("Overlapping Standard Costs"),
			)


def supersede_shadowed_versions(company, item_code, warehouse):
	"""Kept for the DR-62 patch already applied on deployed sites."""
	return normalize_cost_windows(company, item_code, warehouse).get("superseded", [])
