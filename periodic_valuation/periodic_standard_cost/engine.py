# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Periodic Standard Cost engine — Frappe port of the V2.03-conformant reference
simulators (sap_std_mtd / sap_std_ytd). The event log lives in Inventory
Valuation Event rows (STD columns); rollups are recomputed from the log
exactly as the simulators do; settlements are frozen snapshots in Inventory
Period Settlement.

Behavioral contract (DR-05..DR-13):
- receipts at SC with immediate PPV; issues at SC create no variance
- landed cost and invoice differences post into the PPV pool (DR-05)
- boundary revaluation = dated 3-leg triplet; Rev pool settles (DR-12/13)
- Sett back-dated to the period's last day, Sett-Rev on next day 1;
  same-FY full 4-leg reverse, cross-FY inventory-share only (DR-06)
- MTD pools chain the prior live settlement's inventory share; YTD pools are
  own-period + prior-Sett chain (full same-FY, inv-share cross-FY) (DR-07)
- Sett-Reverse may only target the immediately-previous month (DR-08)
- guard rails: PeriodLocked / BackdateLabel / RevalTSCDrift (DR-09)
"""

import calendar
from datetime import date

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime

from periodic_valuation.periodic_standard_cost.config import BD_BY_PRIMARIES, SETT_FAMILY, flags_for
from periodic_valuation.shared.immutable import KERNEL_FLAG


def r2(x):
	return flt(x, 2)


class PeriodLockedError(frappe.ValidationError):
	pass


class BackdateLabelError(frappe.ValidationError):
	pass


class RevalTSCDriftError(frappe.ValidationError):
	pass


class SettReverseRuleViolation(frappe.ValidationError):
	pass


def get_std_setting(company, key):
	name = frappe.db.get_value("Periodic Standard Cost Settings", {"company": company})
	if not name:
		frappe.throw(
			_("Periodic Standard Cost Settings not configured for company {0}.").format(company),
			title=_("Missing Configuration"),
		)
	return frappe.db.get_value("Periodic Standard Cost Settings", name, key)


def get_settlement_view(company, item_code):
	"""The item's own field is the single operative config. Group/company
	defaults are TEMPLATES: the first time a blank item resolves through one,
	the resolved value is stamped onto the item — later edits to defaults
	never touch existing items (defaults-as-templates model, DR-22)."""
	view = frappe.get_cached_value("Item", item_code, "settlement_view")
	if view in ("MTD", "YTD"):
		return view
	item_group = frappe.get_cached_value("Item", item_code, "item_group")
	group_view = frappe.db.get_value("Item Group", item_group, "default_settlement_view")
	resolved = group_view if group_view in ("MTD", "YTD") else (
		get_std_setting(company, "default_settlement_view") or "MTD"
	)
	frappe.db.set_value("Item", item_code, "settlement_view", resolved, update_modified=False)
	frappe.clear_document_cache("Item", item_code)
	return resolved


def get_active_standard_cost(company, item_code, warehouse, posting_date):
	"""Timing Rule A/B: the RELEASED cost version effective at the posting date."""
	d = getdate(posting_date)
	include_wh = frappe.get_cached_value("Item", item_code, "valuation_includes_warehouse")
	rows = frappe.get_all(
		"Item Standard Cost Version",
		filters={
			"company": company,
			"item_code": item_code,
			"warehouse": ("in", ((warehouse or "") if include_wh else "", None)),
			"status": "RELEASED",
		},
		fields=["name", "standard_cost", "valid_from_year", "valid_from_month", "released_on"],
	)
	candidates = [
		x for x in rows
		if (x.valid_from_year, x.valid_from_month) <= (d.year, d.month)
	]
	if not candidates:
		frappe.throw(
			_("No RELEASED Item Standard Cost Version covers {0} for {1}. Release one before posting.").format(
				posting_date, item_code
			),
			title=_("No Standard Cost"),
		)
	best = max(candidates, key=lambda x: (x.valid_from_year, x.valid_from_month, x.released_on or ""))
	_materialize_if_pending(best.name)
	return best


def _materialize_if_pending(scv_name):
	"""Lazy backstop for boundary revaluations (M11): if the resolved version
	became effective without its triplet, post it before pricing anything."""
	row = frappe.db.get_value(
		"Item Standard Cost Version", scv_name, ["revaluation_posted"], as_dict=True,
	)
	if not row or row.revaluation_posted or frappe.flags.in_scv_materialize:
		return
	frappe.get_doc("Item Standard Cost Version", scv_name).materialize_boundary()


def _derive_std_intent(trans, reversal_of=None):
	if reversal_of:
		return "EXACT_REVERSAL_WITH_REFERENCE"
	if trans in SETT_FAMILY or trans.endswith("Rev") or trans.startswith("Rev") \
			or trans in ("REV In", "REV out"):
		return "SYSTEM_GENERATED"
	if trans in ("PR", "SR"):
		return "RETURN_WITH_REFERENCE"
	return "NEW_CURRENT_STD_MOVEMENT"


class StdEngine:
	"""Event-log engine for one valuation scope (company, item [, warehouse])."""

	def __init__(self, company, item_code, warehouse=None):
		self.company = company
		self.item_code = item_code
		self.include_warehouse = frappe.get_cached_value(
			"Item", item_code, "valuation_includes_warehouse"
		)
		self.warehouse = (warehouse or "") if self.include_warehouse else ""
		self.physical_warehouse = warehouse
		self.view = get_settlement_view(company, item_code)

	# ------------------------------------------------------------------ query
	def _scope_filters(self):
		filters = {"company": self.company, "item_code": self.item_code, "is_cancelled": 0}
		if self.include_warehouse:
			filters["warehouse"] = self.physical_warehouse
		return filters

	def events(self, extra=None):
		filters = self._scope_filters()
		filters["std_trans"] = ("!=", "")
		if extra:
			filters.update(extra)
		return frappe.get_all(
			"Inventory Valuation Event",
			filters=filters,
			fields=["name", "std_trans", "period_year", "period_month", "qty_adj",
				"total_sc", "total_ac", "in_flag", "out_flag", "ppv_with_sett",
				"ppv_without_sett", "rev_flag", "creation", "posting_date"],
			order_by="creation asc",
		)

	def settlements(self, live_only=False):
		filters = {
			"company": self.company, "item_code": self.item_code,
			"warehouse": self.warehouse or "",
		}
		if live_only:
			filters["cancelled"] = 0
		return frappe.get_all(
			"Inventory Period Settlement",
			filters=filters,
			fields=["*"],
			order_by="creation asc",
		)

	def is_period_locked(self, year, month):
		"""A period is locked once a live settlement exists for it (DR-08)."""
		return bool(
			frappe.db.exists(
				"Inventory Period Settlement",
				{
					"company": self.company, "item_code": self.item_code,
					"warehouse": self.warehouse or "", "period_year": year,
					"period_month": month, "cancelled": 0,
				},
			)
		)

	# ------------------------------------------------------------------ post
	def post(self, *, trans, posting_date, qty=None, sc=None, ac=None, source,
			entry_date=None, ref="", t_sc_override=None, t_ac_override=None,
			cost_version=None, post_gl=True, qty_adj_override=None, reversal_of=None,
			posting_intent=None):
		"""Append one STD event (and its GL unless Sett-family)."""
		flags = flags_for(trans, self.view)
		pst = getdate(posting_date)
		ent = getdate(entry_date) if entry_date else getdate(frappe.utils.nowdate())

		if trans not in SETT_FAMILY and self.is_period_locked(pst.year, pst.month):
			raise PeriodLockedError(
				_(
					"Cannot post {0} dated {1}: period {2}-{3:02d} is settled. "
					"Reverse the settlement first (previous month only) or post a forward correction."
				).format(trans, posting_date, pst.year, pst.month)
			)

		# The (BD)/(BY) label rule classifies a NEW posting by when it is entered.
		# An exact reversal is a mirror: it inherits the original's label by
		# definition, and its date is set by the period-state rule in
		# reverse_event (original date while the period is open, current date once
		# it is settled) — not by backdate classification. Applying this check to
		# a mirror made a backdated receipt or issue impossible to undo at all:
		# Create Cancellation is the only sanctioned undo under STD and it failed
		# with this internal label error.
		if reversal_of is None and trans in BD_BY_PRIMARIES \
				and (pst.month, pst.year) == (ent.month, ent.year):
			raise BackdateLabelError(
				_(
					"{0} is only for cross-month backdates. Same-month backdates post as plain "
					"Rec/Iss at the current standard cost with no companion."
				).format(trans)
			)

		if qty_adj_override is not None:
			qty_adj = flt(qty_adj_override)
		elif trans.endswith("Rev") or trans.startswith("Rev") or trans.startswith("Sett") or qty is None:
			qty_adj = 0.0
		else:
			qty_adj = flt(qty) if flags.mvt == "In" else (-flt(qty) if flags.mvt == "Out" else 0.0)

		total_sc = t_sc_override if t_sc_override is not None else r2((sc or 0) * qty_adj)
		total_ac = t_ac_override if t_ac_override is not None else r2((ac or 0) * qty_adj)

		if trans in ("Rev Beg", "REV In", "REV out") and t_sc_override is not None \
				and sc is not None and ac is not None:
			expected_qty = self._reval_qty_at(trans, ent, sc_new=flt(sc), sc_old=flt(ac))
			# the out bucket can be net NEGATIVE (SC+ dominated) — compare magnitudes
			expected = abs(abs(flt(sc) - flt(ac)) * expected_qty)
			if abs(abs(total_sc) - expected) > 0.01:
				raise RevalTSCDriftError(
					_("{0} amount {1} does not match |SC delta| x categorized qty = {2}.").format(
						trans, abs(total_sc), r2(expected)
					)
				)

		frappe.flags[KERNEL_FLAG] = True
		try:
			ive = frappe.get_doc({
				"doctype": "Inventory Valuation Event",
				"company": self.company,
				"item_code": self.item_code,
				"warehouse": self.physical_warehouse,
				"period_year": pst.year,
				"period_month": pst.month,
				"posting_date": posting_date,
				"entry_date": now_datetime(),
				"source_doctype": source[0],
				"source_docname": source[1],
				"source_detail_name": source[2] if len(source) > 2 else None,
				"reason_code": ("settlement_reverse" if "Rev" in trans else "settlement")
					if trans in SETT_FAMILY else "std_event",
				"posting_intent": posting_intent or _derive_std_intent(trans, reversal_of),
				"std_trans": trans,
				"qty_adj": qty_adj,
				"qty_basis": abs(qty_adj),
				"standard_cost": sc,
				"actual_cost": ac,
				"total_sc": r2(total_sc),
				"total_ac": r2(total_ac),
				"value_delta": 0,  # inventory effect tracked via GL legs / IPB below
				"in_flag": flags.in_flag,
				"out_flag": flags.out_flag,
				"ppv_with_sett": flags.ppvw,
				"ppv_without_sett": flags.ppvwo,
				"rev_flag": flags.rev,
				"settlement_ref": str(ref) if ref else "",
				"cost_version": cost_version,
				"reversal_of": reversal_of,
			}).insert(ignore_permissions=True)
		finally:
			frappe.flags[KERNEL_FLAG] = False

		if post_gl and trans not in SETT_FAMILY:
			self._post_gl(ive, trans, total_sc, total_ac)
		return ive

	# -------------------------------------------------------------- GL legs
	def accounts(self):
		from periodic_valuation.shared.accounts import get_inventory_account

		company = self.company
		return frappe._dict(
			stock=get_inventory_account(company, self.item_code, self.physical_warehouse),
			ppv=get_std_setting(company, "ppv_account"),
			reserve=get_std_setting(company, "std_reval_reserve_account"),
			cogs_adj=get_std_setting(company, "cogs_adjustment_account"),
			customer_cogs=get_std_setting(company, "customer_cogs_account")
				or frappe.get_cached_value("Company", company, "default_expense_account"),
			fy_carry=get_std_setting(company, "fy_carry_forward_account"),
			cogs=frappe.get_cached_value("Company", company, "default_expense_account"),
			grir=frappe.get_cached_value("Company", company, "stock_received_but_not_billed"),
			stock_adj=frappe.get_cached_value("Company", company, "stock_adjustment_account"),
		)

	def _legs(self, trans, s, u, a, settlement=None, offset_override=None):
		"""Port of gl_legs_for_event: [(account, signed_amount)] (+Dr / -Cr)."""
		if trans == "Beg":
			return [(a.stock, s), (a.ppv, u - s), (a.fy_carry, -u)]
		if trans in ("Rec", "REC (BD)", "REC (BY)"):
			return [(a.stock, s), (a.ppv, u - s), (offset_override or a.grir, -u)]
		if trans in ("Iss", "Issue (BD)", "Issue (BY)"):
			return [(offset_override or a.cogs, -s), (a.stock, s)]
		if trans == "PR":
			# reverses the original receipt's PPV: u,s are negative on returns
			return [(a.stock, s), (a.ppv, u - s), (offset_override or a.grir, -u)]
		if trans == "SR":
			return [(a.stock, s), (a.customer_cogs, -s)]
		if trans == "LC":
			return [(a.ppv, u), (offset_override or a.grir, -u)]
		if trans == "SC+":
			return [(a.stock, s), (offset_override or a.stock_adj, -s)]
		if trans == "SC-":
			return [(offset_override or a.stock_adj, -s), (a.stock, s)]
		if trans in ("Rev Beg", "REV In", "REC (BD) - Rev", "REC (BY) - Rev"):
			return [(a.stock, s), (a.reserve, -s)]
		if trans == "REV out":
			# t_sc convention: -(delta x out_qty). SC increase (delta>0) -> s<0 ->
			# Dr COGS Adjustment / Cr Stock; SC decrease flips both legs.
			return [(a.cogs_adj, -s), (a.stock, s)]
		if trans == "Issue (BD) - Rev":
			# same-FY: consumption tops up at current SC and inventory gives
			# back the triplet's overshoot on the departed units (workbooks:
			# MTD r26, YTD issue-backdated r34 — COGS <-> Inventory)
			return [(a.cogs_adj, -s), (a.stock, s)]
		if trans == "Issue (BY) - Rev":
			# cross-FY: prior-FY COGS is closed — Inventory <-> Reserve
			return [(a.reserve, -s), (a.stock, s)]
		if settlement is not None:
			es, out = flt(settlement.es_var), flt(settlement.out_var)
			ppv, rev = flt(settlement.ppv_pool), flt(settlement.rev_pool)
			ppv_es, rev_es = flt(settlement.ppv_es), flt(settlement.rev_es)
			xfy = settlement.period_month == 12
			if trans == "Sett":
				return [(a.stock, es), (a.cogs_adj, out), (a.ppv, -ppv), (a.reserve, -rev)]
			if trans == "Sett - Rev":
				# the carryover mirrors what the view's chain re-imports:
				# MTD re-imports the inventory share only (workbook v2.05 r19),
				# YTD same-FY re-imports the full pool -> full 4-leg mirror;
				# cross-FY is inventory-share only in both views (DR-06)
				if xfy or self.view == "MTD":
					# reserve leg balances: ppv_es/rev_es are rounded shares
					# and must sum exactly to es_var for a zero-sum row
					return [(a.stock, -es), (a.ppv, ppv_es), (a.reserve, r2(es - ppv_es))]
				return [(a.stock, -es), (a.cogs_adj, -out), (a.ppv, ppv), (a.reserve, rev)]
			if trans == "Sett - Reverse":
				return [(a.stock, -es), (a.cogs_adj, -out), (a.ppv, ppv), (a.reserve, rev)]
			if trans == "Sett - Rev - Reverse":
				if xfy:
					return [(a.stock, es), (a.ppv, -ppv_es), (a.reserve, -rev_es)]
				return [(a.stock, es), (a.cogs_adj, out), (a.ppv, -ppv), (a.reserve, -rev)]
		return []

	def _post_gl(self, ive, trans, total_sc, total_ac, settlement=None, offset_override=None,
			posting_date=None):
		from erpnext.accounts.general_ledger import make_gl_entries

		a = self.accounts()
		legs = self._legs(trans, flt(total_sc), flt(total_ac), a, settlement, offset_override)
		gl_map = []
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")
		for account, amount in legs:
			amount = r2(amount)
			if not amount or not account:
				continue
			gl_map.append(frappe._dict({
				"account": account,
				"against": a.stock if account != a.stock else a.ppv,
				"debit": amount if amount > 0 else 0,
				"credit": -amount if amount < 0 else 0,
				"debit_in_account_currency": amount if amount > 0 else 0,
				"credit_in_account_currency": -amount if amount < 0 else 0,
				"posting_date": posting_date or ive.posting_date,
				"voucher_type": ive.source_doctype,
				"voucher_no": ive.source_docname,
				"company": self.company,
				"cost_center": cost_center,
				"remarks": _("STD event {0} ({1})").format(ive.name, trans),
				"valuation_event_id": ive.name,
			}))
		if gl_map:
			make_gl_entries(gl_map, merge_entries=False)
		# the period-close gates compare IVE.value_delta against inventory-account
		# GL — record this event's net stock effect (B1 audit fix)
		stock_net = sum(a2 for acct, a2 in legs if acct == a.stock)
		if r2(stock_net):
			frappe.db.set_value("Inventory Valuation Event", ive.name,
				"value_delta", r2(stock_net), update_modified=False)

	# ------------------------------------------------------------- rollups
	def _sum(self, expr, cond, params):
		wh_cond = " AND warehouse = %(warehouse)s" if self.include_warehouse else ""
		params = dict(params, company=self.company, item_code=self.item_code,
			warehouse=self.physical_warehouse)
		return flt(frappe.db.sql(
			f"""SELECT COALESCE(SUM({expr}), 0) FROM `tabInventory Valuation Event`
			WHERE company = %(company)s AND item_code = %(item_code)s AND is_cancelled = 0
				AND COALESCE(std_trans, '') != ''{wh_cond} AND {cond}""",
			params,
		)[0][0], 6)

	def _periods_present(self):
		wh_cond = " AND warehouse = %(warehouse)s" if self.include_warehouse else ""
		rows = frappe.db.sql(
			f"""SELECT DISTINCT period_year, period_month FROM `tabInventory Valuation Event`
			WHERE company = %(company)s AND item_code = %(item_code)s AND is_cancelled = 0
				AND COALESCE(std_trans, '') != ''{wh_cond}""",
			{"company": self.company, "item_code": self.item_code,
			 "warehouse": self.physical_warehouse},
		)
		return sorted((int(y), int(m)) for y, m in rows)

	# ---- MTD rollups (month-scoped, Beg chains from prior period's End).
	# Beg opening events belong to the month's BEGINNING bucket so the go-live
	# month's settlement base (beg + in) includes the opening quantity.
	def beg_qty_mtd(self, year, month):
		prior = [p for p in self._periods_present() if p < (year, month)]
		base = self.end_qty_mtd(*prior[-1]) if prior else 0.0
		return base + self._sum(
			"qty_adj", "period_year = %(y)s AND period_month = %(m)s AND std_trans = 'Beg'",
			{"y": year, "m": month},
		)

	def end_qty_mtd(self, year, month):
		return self.beg_qty_mtd(year, month) + self._sum(
			"qty_adj",
			"period_year = %(y)s AND period_month = %(m)s AND std_trans != 'Beg'",
			{"y": year, "m": month},
		)

	def in_qty_mtd(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month = %(m)s AND in_flag = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("qty_adj", cond, {"y": year, "m": month, "before": before})

	def out_qty_mtd(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month = %(m)s AND out_flag = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("qty_adj", cond, {"y": year, "m": month, "before": before})

	def own_ppv(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month = %(m)s AND ppv_without_sett = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("total_ac - total_sc", cond, {"y": year, "m": month, "before": before})

	def own_rev(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month = %(m)s AND rev_flag = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("total_ac - total_sc", cond, {"y": year, "m": month, "before": before})

	# ---- YTD rollups (FY-cumulative)
	def _beg_events(self, year):
		return self.events({"std_trans": "Beg", "period_year": year})

	def _last_live_settlement_in_year(self, year):
		rows = [s for s in self.settlements(live_only=True) if s.period_year == year]
		return max(rows, key=lambda s: s.period_month) if rows else None

	def beg_qty_ytd(self, year):
		beg = sum(flt(e.qty_adj) for e in self._beg_events(year))
		if beg:
			return beg
		prior = self._last_live_settlement_in_year(year - 1)
		return flt(prior.es_qty) if prior else 0.0

	def beg_value_ytd(self, year):
		beg = sum(flt(e.total_sc) for e in self._beg_events(year))
		if beg:
			return beg
		prior = self._last_live_settlement_in_year(year - 1)
		if not prior:
			return 0.0
		return flt(prior.beg_value_sc) + flt(prior.in_value_sc) + flt(prior.ppv_es) + flt(prior.rev_es)

	def in_qty_ytd(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month <= %(m)s AND in_flag = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("qty_adj", cond, {"y": year, "m": month, "before": before})

	def in_value_ytd(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month <= %(m)s AND in_flag = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("total_sc", cond, {"y": year, "m": month, "before": before})

	def out_qty_ytd(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month <= %(m)s AND out_flag = 1"
		if before:
			cond += " AND creation < %(before)s"
		return self._sum("qty_adj", cond, {"y": year, "m": month, "before": before})

	def end_qty_ytd(self, year, month, before=None):
		cond = "period_year = %(y)s AND period_month <= %(m)s"
		if before:
			cond += " AND creation < %(before)s"
		own = self._sum("qty_adj", cond, {"y": year, "m": month, "before": before})
		if self._beg_events(year):
			return own
		prior = self._last_live_settlement_in_year(year - 1)
		return own + (flt(prior.es_qty) if prior else 0.0)

	def _prior_live_settlement(self, year, month):
		"""Newest LIVE settlement strictly before (year, month) — scans past
		cancelled/skipped months so the chain never silently drops (M5)."""
		rows = [s for s in self.settlements(live_only=True)
			if (s.period_year, s.period_month) < (year, month)]
		return max(rows, key=lambda s: (s.period_year, s.period_month, s.creation)) if rows else None

	def pool_ppv(self, year, month, before=None):
		if self.view == "MTD":
			own = self.own_ppv(year, month, before)
			prior = self._prior_live_settlement_mtd(year, month)
			return own + (flt(prior.ppv_es) if prior else 0.0)
		own = self.own_ppv(year, month, before)
		prior = self._prior_live_settlement(year, month)
		if not prior:
			return own
		return own + (flt(prior.ppv_pool) if prior.period_year == year else flt(prior.ppv_es))

	def pool_rev(self, year, month, before=None):
		if self.view == "MTD":
			own = self.own_rev(year, month, before)
			prior = self._prior_live_settlement_mtd(year, month)
			return own + (flt(prior.rev_es) if prior else 0.0)
		own = self.own_rev(year, month, before)
		prior = self._prior_live_settlement(year, month)
		if not prior:
			return own
		return own + (flt(prior.rev_pool) if prior.period_year == year else flt(prior.rev_es))

	def _prior_live_settlement_mtd(self, year, month):
		"""MTD carry is ADJACENT-month only: the Sett-Rev lands on day 1 of the
		month after the settled one, so its inventory share belongs to THAT
		month's pool. A later month re-imports nothing until its own previous
		month settles (workbook v2.05: Apr pool = own 400, Feb's carry stays
		in unsettled March)."""
		py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
		rows = [s for s in self.settlements(live_only=True)
			if (s.period_year, s.period_month) == (py, pm)]
		return max(rows, key=lambda s: s.creation) if rows else None

	# ---- reval qty categorization (drift guard, client-verified)
	def _reval_qty_at(self, trans, ent, *, sc_new, sc_old):
		if self.view == "MTD":
			# DR-12: MTD reval buckets are MONTH-scoped — Beg = prior month's
			# end (+ any Beg openings), In/Out = month-to-date at the moment
			beg = self.beg_qty_mtd(ent.year, ent.month)
			in_qty = out_qty = 0.0
			for e in self.events({"period_year": ent.year, "period_month": ent.month}):
				if getdate(e.posting_date) > ent or not flt(e.qty_adj):
					continue
				if e.std_trans in ("Rec", "REC (BD)", "REC (BY)", "PR"):
					in_qty += flt(e.qty_adj)
				elif e.std_trans in ("Iss", "Issue (BD)", "Issue (BY)", "SC-", "SR", "SC+"):
					out_qty += -flt(e.qty_adj)
			return {"Rev Beg": beg, "REV In": in_qty, "REV out": out_qty}.get(trans, 0.0)

		year = ent.year
		beg_events = [e for e in self._beg_events(year) if getdate(e.posting_date) <= ent]
		if beg_events:
			beg = sum(flt(e.qty_adj) for e in beg_events)
		else:
			beg = self._sum("qty_adj", "period_year < %(y)s", {"y": year})
		in_qty = out_qty = 0.0
		for e in self.events({"period_year": year}):
			# exclude only FUTURE postings; same-day events already in the log
			# are part of the state being revalued
			if getdate(e.posting_date) > ent or not flt(e.qty_adj):
				continue
			if e.std_trans in ("Rec", "REC (BD)", "REC (BY)", "PR"):
				in_qty += flt(e.qty_adj)
			elif e.std_trans in ("Iss", "Issue (BD)", "Issue (BY)", "SC-"):
				out_qty += -flt(e.qty_adj)
			elif e.std_trans in ("SR", "SC+"):
				out_qty += -flt(e.qty_adj)
		return {"Rev Beg": beg, "REV In": in_qty, "REV out": out_qty}.get(trans, 0.0)

	# --------------------------------------------------------- close period
	def _assert_prior_fy_closed(self, year):
		"""Year-end gate: a new fiscal year may not settle while the prior
		year's December is live-unsettled with stock or an open pool — the
		carry (inventory-share Sett-Rev on Jan 1) would silently never seed."""
		prev = year - 1
		if not any(p[0] == prev for p in self._periods_present()):
			return
		if self.is_period_locked(prev, 12):
			return
		if self.end_qty_mtd(prev, 12) > 0 or flt(self.pool_ppv(prev, 12)) \
				or flt(self.pool_rev(prev, 12)):
			frappe.throw(
				_(
					"Fiscal year {0} is not closed for {1}: December {0} is unsettled with "
					"stock or an open variance pool. Run STD Year End Close for {0} first."
				).format(prev, self.item_code),
				title=_("Year End Open"),
			)

	def close_period(self, *, year, month, sc, source, entry_date=None, ref=None,
			es_qty_override=None, out_qty_override=None, settlement_run=None):
		self._assert_prior_fy_closed(year)
		before = now_datetime()
		if self.view == "MTD":
			beg_qty = self.beg_qty_mtd(year, month)
			in_qty = self.in_qty_mtd(year, month)
			beg_value = r2(sc * beg_qty)
			in_value = r2(sc * in_qty)
			end_qty = self.end_qty_mtd(year, month)
			out_qty = -self.out_qty_mtd(year, month)
		else:
			beg_qty = self.beg_qty_ytd(year)
			in_qty = self.in_qty_ytd(year, month)
			beg_value = r2(self.beg_value_ytd(year))
			in_value = r2(self.in_value_ytd(year, month))
			end_qty = self.end_qty_ytd(year, month)
			out_qty = -self.out_qty_ytd(year, month)

		ppv = self.pool_ppv(year, month)
		rev = self.pool_rev(year, month)
		var = ppv + rev
		if es_qty_override is not None:
			end_qty = es_qty_override
		if out_qty_override is not None:
			out_qty = out_qty_override
		denom = beg_qty + in_qty
		if denom <= 0:
			frappe.throw(
				_("Nothing to settle for {0} {1}-{2:02d}: no quantity basis this period; the pool stays open.").format(
					self.item_code, year, month
				)
			)
		# RAW proportional split always (year-transfer workbook, DR-30): when
		# ending qty exceeds the base the inventory share exceeds the pool and
		# the consumption share goes NEGATIVE — they still sum to the pool
		# because end + out = base. The old all-to-consumption/all-to-inventory
		# clamps only ever fired in exactly these cases and contradicted the
		# workbook (Jan: es 6559.5611 / out -218.652 on a 6340.91 pool).
		es_var = r2(var * end_qty / denom)
		out_var = r2(var * out_qty / denom)
		share = end_qty / denom
		cons_share = out_qty / denom

		frappe.flags[KERNEL_FLAG] = True
		try:
			sett = frappe.get_doc({
				"doctype": "Inventory Period Settlement",
				"company": self.company, "item_code": self.item_code,
				"warehouse": self.warehouse or "",
				"period_year": year, "period_month": month,
				"settlement_view": self.view, "settlement_run": settlement_run,
				"beg_qty": beg_qty, "beg_value_sc": beg_value, "standard_cost": sc,
				"in_qty": in_qty, "in_value_sc": in_value,
				"ppv_pool": r2(ppv), "rev_pool": r2(rev),
				"total_ac": r2(beg_value + in_value + ppv + rev), "variance": r2(var),
				"es_qty": end_qty, "es_var": es_var,
				"ppv_es": r2(ppv * share), "rev_es": r2(rev * share),
				"out_qty": out_qty, "out_var": out_var,
				"ppv_cons": r2(ppv * cons_share), "rev_cons": r2(rev * cons_share),
				"es_qty_override": es_qty_override,
			}).insert(ignore_permissions=True)
		finally:
			frappe.flags[KERNEL_FLAG] = False

		last_day = date(year, month, calendar.monthrange(year, month)[1])
		next_day = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
		ref_str = ref or sett.name

		sett_event = self.post(trans="Sett", posting_date=last_day, source=source,
			entry_date=entry_date, ref=ref_str, t_sc_override=es_var, post_gl=False)
		self._post_gl(sett_event, "Sett", es_var, 0, settlement=sett, posting_date=last_day)
		sett_rev_event = self.post(trans="Sett - Rev", posting_date=next_day, source=source,
			entry_date=entry_date, ref=ref_str, t_sc_override=-es_var, post_gl=False)
		self._post_gl(sett_rev_event, "Sett - Rev", -es_var, 0, settlement=sett,
			posting_date=next_day)

		frappe.db.set_value("Inventory Period Settlement", sett.name,
			{"sett_event": sett_event.name, "sett_rev_event": sett_rev_event.name},
			update_modified=False)
		self._stamp_ipb_settlement(sett, year, month, sc)
		self._absorb_settlement_value(sett, year, month, es_var)
		return frappe.get_doc("Inventory Period Settlement", sett.name)

	def _absorb_settlement_value(self, sett, year, month, es_var, sign=1):
		"""Sett debits Stock In Hand (es_var) on the period's last day and the
		Sett-Rev credits it back on the next day 1 — mirror both into the period
		balances so GL == movement table holds (B1 audit fix)."""
		if not r2(es_var):
			return
		from periodic_valuation.periodic_moving_average.kernel import ScopeState, recompute_closing
		from periodic_valuation.shared.periods import get_period

		from periodic_valuation.periodic_standard_cost.kernel import _cascade_backdated_ipb

		ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
		for py, pm, amount in ((year, month, sign * es_var), (ny, nm, -sign * es_var)):
			period = get_period(self.company, f"{py}-{pm:02d}-01")
			if not period:
				continue
			scope = ScopeState(self.company, self.item_code, self.physical_warehouse)
			ipb = scope.load(period)
			ipb.reval_value = flt(ipb.reval_value) + r2(amount)
			recompute_closing(ipb)
			if not flt(ipb.period_standard_cost):
				ipb.period_standard_cost = flt(sett.standard_cost)
				ipb.moving_avg_price = flt(sett.standard_cost)
				ipb.resolved_settlement_view = self.view
			_cascade_backdated_ipb(scope, period, 0, r2(amount), source=("Inventory Period Settlement", sett.name))
			if flt(ipb.period_standard_cost):
				ipb.moving_avg_price = flt(ipb.period_standard_cost)
			scope.save(ipb, source=("Inventory Period Settlement", sett.name))

	def _stamp_ipb_settlement(self, sett, year, month, sc):
		"""Reporting snapshot on the scope's Inventory Period Balance row."""
		name = frappe.db.get_value("Inventory Period Balance", {
			"company": self.company, "item_code": self.item_code,
			"warehouse": self.warehouse or "", "period_year": year, "period_month": month,
		})
		if name:
			closing_qty = flt(frappe.db.get_value("Inventory Period Balance", name, "closing_qty"))
			frappe.db.set_value("Inventory Period Balance", name, {
				"ppv_pool": flt(sett.ppv_pool), "rev_pool": flt(sett.rev_pool),
				"settlement": sett.name, "period_standard_cost": flt(sc),
				"resolved_settlement_view": self.view,
				"settlement_inventory_total": r2(sett.es_var),
				"settlement_consumption_total": r2(sett.out_var),
				"closing_reference_value": r2(closing_qty * flt(sc)),
			}, update_modified=False)

	# --------------------------------------------------------- sett reverse
	def sett_reverse(self, settlement_name, *, source, entry_date=None):
		sett = frappe.get_doc("Inventory Period Settlement", settlement_name)
		if sett.cancelled:
			frappe.throw(_("{0} is already cancelled.").format(settlement_name))
		ent = getdate(entry_date) if entry_date else getdate(frappe.utils.nowdate())
		ty, tm = sett.period_year, sett.period_month
		# DR-08 is a boundary against OLD settlements: reversible while the
		# settlement is the current or immediately-previous month; anything
		# older takes the forward-correction path.
		prev_ok = (
			(ty == ent.year and tm in (ent.month, ent.month - 1))
			or (ty == ent.year - 1 and tm == 12 and ent.month == 1)
		)
		if not prev_ok:
			raise SettReverseRuleViolation(
				_(
					"Sett-Reverse may only target the immediately-previous month "
					"(today {0}-{1:02d}, target {2}-{3:02d})."
				).format(ent.year, ent.month, ty, tm)
			)

		last_day = date(ty, tm, calendar.monthrange(ty, tm)[1])
		next_day = date(ty + 1, 1, 1) if tm == 12 else date(ty, tm + 1, 1)

		reverse_event = self.post(trans="Sett - Reverse", posting_date=last_day, source=source,
			entry_date=entry_date, ref=sett.name, t_sc_override=-flt(sett.es_var), post_gl=False)
		self._post_gl(reverse_event, "Sett - Reverse", 0, 0, settlement=sett,
			posting_date=last_day)
		rev_reverse_event = self.post(trans="Sett - Rev - Reverse", posting_date=next_day,
			source=source, entry_date=entry_date, ref=sett.name,
			t_sc_override=flt(sett.es_var), post_gl=False)
		self._post_gl(rev_reverse_event, "Sett - Rev - Reverse", 0, 0, settlement=sett,
			posting_date=next_day)

		frappe.db.set_value("Inventory Period Settlement", sett.name, {
			"cancelled": 1,
			"reversed_by_events": f"{reverse_event.name},{rev_reverse_event.name}",
		}, update_modified=False)
		self._absorb_settlement_value(sett, ty, tm, flt(sett.es_var), sign=-1)
		return reverse_event, rev_reverse_event


	# ------------------------------------------------- exact reversal (Phase 4)
	def reverse_event(self, original_name, *, source, posting_date=None, entry_date=None):
		"""EXACT_REVERSAL_WITH_REFERENCE (client appendix, reversal matrix).

		Measured entirely from the ORIGINAL event (original SC, original
		variance, original cost version — never current STD). Legally posted:
		- original period still open  -> into the original period
		- original period settled     -> current-dated; the caller then runs
		  post_close_delta() for every live settlement of that period (the
		  variance portion re-enters the current pool via the event flags)
		"""
		orig = frappe.get_doc("Inventory Valuation Event", original_name)
		if frappe.db.exists("Inventory Valuation Event",
				{"reversal_of": original_name, "is_cancelled": 0}):
			frappe.throw(_("{0} is already reversed.").format(original_name),
				title=_("Double Reversal Blocked"))

		locked = self.is_period_locked(orig.period_year, orig.period_month)
		pst = posting_date or (getdate(frappe.utils.nowdate()) if locked else orig.posting_date)

		mirror = self.post(
			trans=orig.std_trans, posting_date=pst, source=source, entry_date=entry_date,
			ref=orig.name, sc=orig.standard_cost, ac=orig.actual_cost,
			qty_adj_override=-flt(orig.qty_adj),
			t_sc_override=-flt(orig.total_sc), t_ac_override=-flt(orig.total_ac),
			cost_version=orig.cost_version, post_gl=False, reversal_of=orig.name,
		)
		# GL: mirror the original's legs with sides swapped, on the reversal date
		from erpnext.accounts.general_ledger import make_gl_entries

		gl_map = []
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")
		for g in frappe.get_all("GL Entry",
				filters={"valuation_event_id": original_name, "is_cancelled": 0},
				fields=["account", "debit", "credit"]):
			gl_map.append(frappe._dict({
				"account": g.account, "against": "",
				"debit": flt(g.credit), "credit": flt(g.debit),
				"debit_in_account_currency": flt(g.credit),
				"credit_in_account_currency": flt(g.debit),
				"posting_date": pst, "voucher_type": source[0], "voucher_no": source[1],
				"company": self.company, "cost_center": cost_center,
				"remarks": _("Exact reversal of {0}").format(original_name),
				"valuation_event_id": mirror.name,
			}))
		if gl_map:
			make_gl_entries(gl_map, merge_entries=False)

		if locked:
			for sett in self.settlements(live_only=True):
				if (sett.period_year, sett.period_month) == (orig.period_year, orig.period_month):
					self.post_close_delta(sett, orig, source, entry_date=entry_date)
		return mirror

	def post_close_delta(self, sett, reversed_orig, source, entry_date=None):
		"""Forward allocation correction after reversing into a settled period
		(client worked example: end 40->50 => Dr Inventory 8 / Cr COGS Adj 8).

		Pool is unchanged; the quantity basis moves by the reversed event's
		qty_adj: in-flag events change In, out-flag events change Out/End.
		"""
		qd = flt(reversed_orig.qty_adj)
		beg, in_q = flt(sett.beg_qty), flt(sett.in_qty)
		end, out = flt(sett.es_qty), flt(sett.out_qty)
		if reversed_orig.in_flag:
			in_q -= qd
			end -= qd
		elif reversed_orig.out_flag:
			out += qd  # reversing an issue (qty_adj negative) reduces Out
			end -= qd
		else:
			return
		var = flt(sett.variance)
		denom = beg + in_q
		new_es = r2(var * end / denom) if denom else 0.0
		new_out = r2(var * out / denom) if denom else 0.0
		d_es = r2(new_es - flt(sett.es_var))
		d_out = r2(new_out - flt(sett.out_var))
		if not d_es and not d_out:
			return

		today = getdate(frappe.utils.nowdate())
		delta_ive = self.post(trans="Sett - Delta", posting_date=today, source=source,
			entry_date=entry_date, ref=sett.name, t_sc_override=d_es, post_gl=False)

		from erpnext.accounts.general_ledger import make_gl_entries

		a = self.accounts()
		cost_center = frappe.get_cached_value("Company", self.company, "cost_center")

		# The two allocation legs only net to zero while the quantity basis
		# survives (new_es + new_out == variance). Reversing the very receipt that
		# created the basis collapses the denominator to zero, both shares fall to
		# zero, and the pair then carries the whole previously-allocated variance
		# with nothing on the other side — a Purchase Receipt that refuses to
		# submit with "Debit and Credit not equal ... Difference is -200". The
		# variance pool is the counterpart, exactly as in the Sett matrix
		# (Dr/Cr Inventory + COGS Adjustment against the pools), so give the
		# residual back to the pools it was allocated out of, split on their
		# original proportions.
		residual = r2(-(r2(d_es) + r2(d_out)))
		pool_legs = []
		if residual:
			total_pool = flt(sett.ppv_pool) + flt(sett.rev_pool)
			ppv_part = r2(residual * (flt(sett.ppv_pool) / total_pool)) if total_pool else residual
			pool_legs = [(a.ppv, ppv_part), (a.reserve, r2(residual - ppv_part))]

		gl_map = []
		for account, amount in ((a.stock, d_es), (a.cogs_adj, d_out), *pool_legs):
			amount = r2(amount)
			if not amount:
				continue
			gl_map.append(frappe._dict({
				"account": account, "against": "",
				"debit": amount if amount > 0 else 0, "credit": -amount if amount < 0 else 0,
				"debit_in_account_currency": amount if amount > 0 else 0,
				"credit_in_account_currency": -amount if amount < 0 else 0,
				"posting_date": today, "voucher_type": source[0], "voucher_no": source[1],
				"company": self.company, "cost_center": cost_center,
				"remarks": _("Post-close allocation delta for {0}").format(sett.name),
				"valuation_event_id": delta_ive.name,
			}))
		if gl_map:
			make_gl_entries(gl_map, merge_entries=False)
		return delta_ive
