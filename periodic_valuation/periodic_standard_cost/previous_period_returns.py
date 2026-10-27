# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Goods issued in one period and reversed or returned in a later one (DR-74,
client email 22/10/2026 with the workbook "01 MTD STD Cost V3": the "SR -
Futre" and "Rev Fut-Re" rows).

A reversal (Create Cancellation) or a referenced return of an issue whose
period is earlier than the reversal's - the month for an MTD item, the
fiscal year for a YTD item (Vivek, 22/10/2026) - posts as SR - Future:

- inventory comes back at the standard in force on the reversal's date;
- the expense is reversed at what the original issue cost - its standard on
  its own date as it stands now (backdated corrections included), plus its
  backdate bridge, plus its period's consumption variance per unit once that
  period is settled - each part against the account that carried it (the
  issue's expense account, COGS Adjustment);
- the difference goes to the Standard Cost Revaluation Reserve;
- the quantity is the period balance's Reversed From Previous Periods: it is
  In for the settlement (part of the Available QTY the actual cost is shared
  over) and is not a receipt. The original period's balance counts it as
  Reversed in Later Periods.

When the original period's issue cost changes later - a backdated standard
change, its settlement, the settlement's reversal - each SR - Future of an
issue from it is brought to the new cost by a Rev Fut-Re row on day 1 of the
reversal's period (Dr/Cr Revaluation Reserve against COGS Adjustment). No
quantity moves and the reversal's inventory value stays as it was posted.

A reversal within the same period nets the issue as before, and a return
that names no original issue posts at today's standard as before.

A return dated in the previous open period (a backdated return, DR-73,
folded in here) is bridged into the current period like a backdated receipt
or issue: its quantity x (current standard - its own) against the Reserve
(Return - Rev).
"""

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, getdate

from periodic_valuation.periodic_standard_cost.engine import (
	StdEngine,
	backdate_entry_date,
	get_active_standard_cost,
	r2,
)

SR_FUTURE = "SR - Future"
REV_FUTURE = "Rev Fut-Re"
RETURN_BRIDGE = "Return - Rev"
ISSUE_PRIMARIES = ("Iss", "Issue (BD)", "Issue (BY)")


# ---------------------------------------------------------------- classify
def earlier_period(engine, orig, posting_date):
	"""Is the original issue's period earlier than a reversal dated
	`posting_date`? MTD: the month; YTD: the fiscal year."""
	posting_date = getdate(posting_date)
	if engine.view == "YTD":
		return int(orig.period_year) < posting_date.year
	return (int(orig.period_year), int(orig.period_month)) < (posting_date.year, posting_date.month)


def referenced_issue(controller, sle, engine):
	"""The original issue event a return row names, if any: the row's link to
	the delivery or invoice row it returns."""
	against = controller.get("return_against")
	row = next((x for x in controller.get("items") or [] if x.name == sle.get("voucher_detail_no")), None)
	detail = row and (row.get("dn_detail") or row.get("sales_invoice_item") or row.get("si_detail"))
	if not (against and detail):
		return None
	rows = frappe.get_all("Inventory Valuation Event", filters={
		**engine._scope_filters(), "source_doctype": controller.doctype, "source_docname": against,
		"source_detail_name": detail, "std_trans": ("in", ISSUE_PRIMARIES), "reversal_of": ("is", "not set"),
	}, pluck="name", order_by="creation asc", limit=1)
	return frappe.get_doc("Inventory Valuation Event", rows[0]) if rows else None


# ------------------------------------------------------------- the cost
def historical_unit_cost(engine, orig):
	"""What one unit of the original issue cost, as booked now: its standard on
	its own date (corrections of that period included), its backdate bridge
	(an Issue (BD)/(BY) - Rev row of the same document row), and once its
	period is settled the consumption variance per unit. Returns (total, the
	part at the standard the issue posted at)."""
	sc_posted = flt(orig.standard_cost)
	sc_now = flt(get_active_standard_cost(engine.company, engine.item_code, engine.physical_warehouse,
		getdate(orig.posting_date)).standard_cost) or sc_posted
	qty = abs(flt(orig.qty_adj)) or 1.0
	bridge = sum(-flt(b.total_sc) for b in frappe.get_all("Inventory Valuation Event", filters={
		**engine._scope_filters(), "source_docname": orig.source_docname, "source_detail_name": orig.source_detail_name,
		"std_trans": f"{orig.std_trans} - Rev", "reversal_of": ("is", "not set")}, fields=["total_sc"])) / qty
	return r2(sc_now + bridge + _consumption_variance_per_unit(engine, orig)), sc_posted


def _consumption_variance_per_unit(engine, orig):
	sett = None
	live = engine.settlements(live_only=True)
	if engine.view == "YTD":
		in_year = [s for s in live if s.period_year == orig.period_year]
		sett = max(in_year, key=lambda s: (s.period_month, s.creation)) if in_year else None
	else:
		same = [s for s in live if (s.period_year, s.period_month) == (orig.period_year, orig.period_month)]
		sett = same[-1] if same else None
	if not sett or not flt(sett.out_qty):
		return 0.0
	return flt(sett.out_var) / flt(sett.out_qty)


def _issue_expense_account(engine, orig):
	"""The account the original issue was expensed to (its GL leg that is not
	the inventory account)."""
	stock = engine.accounts().stock
	row = frappe.db.sql("""SELECT account FROM `tabGL Entry` WHERE valuation_event_id = %s AND is_cancelled = 0
		AND account != %s ORDER BY debit DESC LIMIT 1""", (orig.name, stock))
	return row[0][0] if row else engine.accounts().cogs


# ---------------------------------------------------------------- posting
def post_previous_period_return(controller, engine, sle, period, orig, qty, posting_date, source):
	"""Post SR - Future for `qty` (> 0) of `orig` coming back on `posting_date`."""
	from periodic_valuation.periodic_standard_cost.kernel import _bridge_release_switch, _write_sle_and_state
	from periodic_valuation.shared.event_gl import post_event_gl
	from periodic_valuation.shared.periods import current_period_day, is_backdate_period

	posting_date = getdate(posting_date)
	scv = get_active_standard_cost(engine.company, engine.item_code, engine.physical_warehouse, posting_date)
	sc_now = flt(scv.standard_cost)
	cost, sc_posted = historical_unit_cost(engine, orig)
	total_sc, total_ac = r2(qty * sc_now), r2(qty * cost)
	ive = engine.post(trans=SR_FUTURE, posting_date=posting_date, qty=qty, sc=sc_now, ac=cost, source=source,
		cost_version=scv.name, post_gl=False, posting_intent="RETURN_WITH_REFERENCE",
		extra={"reason_code": "previous_period_return", "reference_receipt_event": orig.name,
			"original_period": f"{orig.period_year}-{int(orig.period_month):02d}"})
	a = engine.accounts()
	at_posted = r2(qty * sc_posted)
	legs = [(a.stock, total_sc), (_issue_expense_account(engine, orig), -at_posted),
		(a.cogs_adj, -r2(total_ac - at_posted)), (a.reserve, -r2(total_sc - total_ac))]
	post_event_gl(ive, [(acct, amt, a.stock if acct != a.stock else a.reserve) for acct, amt in legs if r2(amt)],
		posting_date=posting_date, company=engine.company,
		remarks=_("STD event {0} ({1}): {2} of {3} reversed from {4}").format(
			ive.name, SR_FUTURE, qty, orig.source_docname, ive.original_period))
	frappe.db.set_value("Inventory Valuation Event", ive.name, "value_delta", total_sc, update_modified=False)

	row = dict(sle)
	row["actual_qty"] = qty
	_write_sle_and_state(controller, engine, row, period, qty, sc_now, total_sc, scv_name=scv.name,
		bucket="previous_return")
	_count_reversed_later(engine, orig, qty)
	engine.refresh_ipb_pools(posting_date.year, posting_date.month)

	today = current_period_day(engine.company)
	if is_backdate_period(period):
		bridge_backdated_return(engine, sle, qty, sc_now, source, today)
	else:
		_bridge_release_switch(engine, posting_date, qty, sc_now, source, today)
	return ive


def _count_reversed_later(engine, orig, qty):
	"""The original period's balance: Reversed in Later Periods. Display only -
	it is not in that period's closing (the units left in it)."""
	name = frappe.db.get_value("Inventory Period Balance", {
		"company": engine.company, "item_code": engine.item_code, "warehouse": engine.warehouse or "",
		"period_year": orig.period_year, "period_month": orig.period_month})
	if name:
		current = flt(frappe.db.get_value("Inventory Period Balance", name, "reversed_in_later_qty"))
		frappe.db.set_value("Inventory Period Balance", name, "reversed_in_later_qty", current + qty,
			update_modified=False)


def uncount_reversed_later(engine, sr_event, qty):
	"""A cancelled SR - Future takes its quantity off the original period."""
	orig = sr_event.reference_receipt_event and frappe.get_doc("Inventory Valuation Event", sr_event.reference_receipt_event)
	if orig:
		_count_reversed_later(engine, orig, -abs(flt(qty)))


def bridge_backdated_return(engine, sle, qty, sc_posted, source, today):
	"""DR-73: a return dated in the previous open period carries into the
	current period at its own standard; the current period's stock stands at
	its standard, so the difference is bridged - qty (signed: + back in, -
	out to the supplier) x (current - posted standard), Stock In Hand against
	the Revaluation Reserve, dated by the Revaluation Posting Date option
	(DR-59), as a backdated receipt's or issue's companion is."""
	from periodic_valuation.periodic_standard_cost.kernel import book_revaluation

	on = backdate_entry_date(engine.company, today)
	current = get_active_standard_cost(engine.company, engine.item_code, engine.physical_warehouse, on)
	amount = r2(flt(qty) * (flt(current.standard_cost) - flt(sc_posted)))
	engine.post(trans=RETURN_BRIDGE, posting_date=on, qty=abs(flt(qty)), sc=current.standard_cost, ac=sc_posted,
		source=source, ref=source[1], t_sc_override=amount, cost_version=current.name)
	if amount:
		book_revaluation(engine, on, amount, source, stock_uom=sle.get("stock_uom"))


# ------------------------------------------------------------------ sync
def sync_previous_period_returns(engine):
	"""Bring every standing SR - Future of the scope to its original issue's
	cost as it stands now (Rev Fut-Re). Idempotent: run after anything that
	can change a period's issue cost - a backdated standard change, a
	settlement, its reversal."""
	from periodic_valuation.shared.event_gl import post_event_gl
	from periodic_valuation.shared.periods import current_period_day

	# the mirror that undoes an SR - Future names no issue, so it is not one
	returns = frappe.get_all("Inventory Valuation Event", filters={
		**engine._scope_filters(), "std_trans": SR_FUTURE, "reference_receipt_event": ("is", "set")},
		fields=["name", "qty_adj", "actual_cost", "period_year", "period_month", "posting_date",
			"source_doctype", "source_docname", "source_detail_name", "reference_receipt_event"],
		order_by="creation asc")
	if not returns:
		return
	reversed_ = set(frappe.get_all("Inventory Valuation Event", filters={
		**engine._scope_filters(), "reversal_of": ("in", [r.name for r in returns])}, pluck="reversal_of"))
	a = engine.accounts()
	for sr in returns:
		if sr.name in reversed_:
			continue
		orig = frappe.get_doc("Inventory Valuation Event", sr.reference_receipt_event)
		cost, _posted = historical_unit_cost(engine, orig)
		last = frappe.get_all("Inventory Valuation Event", filters={
			**engine._scope_filters(), "std_trans": REV_FUTURE, "settlement_ref": sr.name,
			"reversal_of": ("is", "not set")}, fields=["standard_cost"], order_by="creation desc", limit=1)
		applied = flt(last[0].standard_cost) if last else flt(sr.actual_cost)
		qty = flt(sr.qty_adj)
		amount = r2(qty * (cost - applied))
		if not amount:
			continue
		day = get_first_day(sr.posting_date)
		if engine.is_period_locked(day.year, day.month):
			day = current_period_day(engine.company)
		ive = engine.post(trans=REV_FUTURE, posting_date=day, qty=qty, sc=cost, ac=applied,
			source=(sr.source_doctype, sr.source_docname, sr.source_detail_name), ref=sr.name,
			t_sc_override=0, t_ac_override=amount, post_gl=False,
			extra={"reason_code": "previous_return_adjust", "reference_receipt_event": orig.name,
				"original_period": f"{orig.period_year}-{int(orig.period_month):02d}"})
		post_event_gl(ive, [(a.reserve, amount, a.cogs_adj), (a.cogs_adj, -amount, a.reserve)],
			posting_date=day, company=engine.company,
			remarks=_("STD event {0} ({1}): {2} reversed in a later period, its issue cost {3} -> {4}").format(
				ive.name, REV_FUTURE, sr.name, applied, cost))
		engine.refresh_ipb_pools(day.year, day.month)


def sync_scope(company, item_code, warehouse=None):
	sync_previous_period_returns(StdEngine(company, item_code, warehouse))
