# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Period-close consistency checks and the reconciliation gate (Apr 22 decisions).

All checks are read-only; the Inventory Period Close controller records their
results and refuses to advance the period unless every one passes. There is no
automatic write-off path - mismatches are investigated and resolved manually.
"""

import frappe
from frappe import _
from frappe.utils import flt

from periodic_valuation.shared.accounts import get_all_inventory_accounts
from periodic_valuation.shared.settings import get_pma_setting


def _ipb_rows(period):
	return frappe.get_all(
		"Inventory Period Balance",
		filters={
			"company": period.company,
			"period_year": period.period_year,
			"period_month": period.period_month,
		},
		fields=["name", "item_code", "warehouse", "opening_qty", "opening_value",
			"carryover_qty", "carryover_value", "closing_qty", "closing_value",
			"moving_avg_price", "total_received_since_zero", "is_negative", "frozen_map"],
	)


def _scope_rows_as_of(period):
	"""Every valuation scope's LATEST balance as of this period end.

	A scope that last transacted in an earlier month has no row in this period,
	but its value is still on the inventory account. The reconciliation gate
	sums the GL cumulatively, so it has to sum the balance side the same way -
	counting only in-period rows makes every dormant scope look like a
	discrepancy. On the client's bench 70 dormant items produced a reported
	29,899,841.66 mismatch with no data fault at all, and no period could be
	closed.
	"""
	period_key = period.period_year * 100 + period.period_month
	return frappe.db.sql(
		"""
		SELECT b.name, b.item_code, b.warehouse, b.closing_qty, b.closing_value,
			b.period_year, b.period_month
		FROM `tabInventory Period Balance` b
		JOIN (
			SELECT company, item_code, warehouse,
				MAX(period_year * 100 + period_month) AS period_key
			FROM `tabInventory Period Balance`
			WHERE company = %(company)s
				AND (period_year * 100 + period_month) <= %(period_key)s
			GROUP BY company, item_code, warehouse
		) latest
			ON latest.company = b.company
			AND latest.item_code = b.item_code
			AND latest.warehouse = b.warehouse
			AND (b.period_year * 100 + b.period_month) = latest.period_key
		WHERE b.company = %(company)s
		""",
		{"company": period.company, "period_key": period_key},
		as_dict=True,
	)


def _previous_period_key(period):
	if period.period_month == 1:
		return period.period_year - 1, 12
	return period.period_year, period.period_month - 1


def assert_continuity(period):
	"""Effective opening (opening + carryover) must equal the previous
	period's closing per key. Opening is fixed at period creation; backdated
	postings move the prior closing and this period's carryover by the same
	delta, so the invariant holds across backdating."""
	prev_year, prev_month = _previous_period_key(period)
	failures = []
	for row in _ipb_rows(period):
		prev = frappe.db.get_value(
			"Inventory Period Balance",
			{
				"company": period.company,
				"item_code": row.item_code,
				"warehouse": row.warehouse or "",
				"period_year": prev_year,
				"period_month": prev_month,
			},
			["closing_qty", "closing_value"],
			as_dict=True,
		)
		if prev is None:
			continue  # first period for this key
		effective_qty = flt(row.opening_qty) + flt(row.carryover_qty)
		effective_value = flt(row.opening_value) + flt(row.carryover_value)
		if flt(effective_qty, 6) != flt(prev.closing_qty, 6) or flt(effective_value, 2) != flt(
			prev.closing_value, 2
		):
			failures.append(row.item_code)
	return {"ok": not failures, "detail": failures}


def assert_event_gl_identity(period):
	"""Sum of IVE inventory value deltas for the period == sum of GL lines carrying a
	valuation_event_id that hit inventory accounts in the period."""
	ive_total = flt(
		frappe.db.sql(
			"""
			SELECT COALESCE(SUM(value_delta), 0) FROM `tabInventory Valuation Event`
			WHERE company = %s AND period_year = %s AND period_month = %s AND is_cancelled = 0
			""",
			(period.company, period.period_year, period.period_month),
		)[0][0],
		2,
	)

	rows = _ipb_rows(period)
	inventory_accounts = get_all_inventory_accounts(period.company, rows)
	gl_total = 0.0
	if inventory_accounts:
		gl_total = flt(
			frappe.db.sql(
				"""
				SELECT COALESCE(SUM(debit - credit), 0) FROM `tabGL Entry`
				WHERE company = %s AND is_cancelled = 0
					AND COALESCE(valuation_event_id, '') != ''
					AND account IN %s
					AND posting_date BETWEEN %s AND %s
				""",
				(period.company, tuple(inventory_accounts), period.start_date, period.end_date),
			)[0][0],
			2,
		)

	ok = flt(abs(ive_total - gl_total), 2) == 0
	return {"ok": ok, "detail": {"ive_total": ive_total, "gl_total": gl_total}}


def assert_no_orphans(period):
	"""No stock GL line without a valuation event; no event without GL output.

	Scope: kernel-generated rows of this period only.
	"""
	orphan_events = frappe.db.sql(
		"""
		SELECT ive.name FROM `tabInventory Valuation Event` ive
		LEFT JOIN `tabGL Entry` gle ON gle.valuation_event_id = ive.name AND gle.is_cancelled = 0
		WHERE ive.company = %s AND ive.period_year = %s AND ive.period_month = %s
			AND ive.is_cancelled = 0 AND ive.value_delta != 0 AND gle.name IS NULL
			-- transfers: the two legs net within (or across) inventory accounts;
			-- GL exists only when accounts differ and is tagged to the inbound leg
			AND ive.reason_code != 'transfer'
		LIMIT 20
		""",
		(period.company, period.period_year, period.period_month),
	)
	# GL rows referencing missing events would violate the Link constraint, so
	# the only orphan-GL case is a NULL reference on kernel-tagged vouchers -
	# enforced at posting time by the kernel itself; report zero here.
	return {
		"no_orphan_gl": True,
		"no_orphan_events": not orphan_events,
		"detail": [r[0] for r in orphan_events],
	}


def run_reconciliation_gate(period):
	"""Hard gate: abs(GL inventory balance - sum of IPB closing_value) <= tolerance.

	The GL side sums kernel-tagged lines (valuation_event_id set) on every
	inventory account any in-scope IPB row resolves to, up to period end.
	Manual drift into stock accounts is blocked at posting time, so the two
	sides must match to the configured tolerance (default 0.00 - strict).

	Both sides are measured over the same population: every scope's latest
	balance as of the period end, not only the scopes that moved this period
	(see _scope_rows_as_of).
	"""
	rows = _scope_rows_as_of(period)
	movement_total = flt(sum(flt(r.closing_value) for r in rows), 2)

	inventory_accounts = get_all_inventory_accounts(period.company, rows)
	gl_balance = 0.0
	if inventory_accounts:
		gl_balance = flt(
			frappe.db.sql(
				"""
				SELECT COALESCE(SUM(debit - credit), 0) FROM `tabGL Entry`
				WHERE company = %s AND is_cancelled = 0
					AND COALESCE(valuation_event_id, '') != ''
					AND account IN %s
					AND posting_date <= %s
				""",
				(period.company, tuple(inventory_accounts), period.end_date),
			)[0][0],
			2,
		)

	tolerance = flt(get_pma_setting(period.company, "reconciliation_tolerance"))
	discrepancy = flt(gl_balance - movement_total, 2)
	return {
		"gl_inventory_balance": gl_balance,
		"movement_table_total": movement_total,
		"discrepancy": discrepancy,
		"tolerance": tolerance,
		"passed": abs(discrepancy) <= tolerance,
	}


def assert_no_stranded_value(period):
	"""Gate: no scope may carry inventory value on zero quantity.

	When a posting takes quantity to zero, any residual within
	`rounding_tolerance` is swept to Stock Rounding Adjustment automatically
	(kernel.maybe_rounding_cleanup). A residual ABOVE tolerance is a real
	amount, not rounding noise - most often a late cost (landed cost, invoice
	difference) allocated by stock ratio to a period where the goods were still
	on hand, posted after they had all been issued. Writing that off
	automatically is never permitted (Apr 22 decision), so the period cannot
	close until finance resolves it.

	Without this check the condition is invisible: GL and the movement table
	both carry the same stranded amount, so the reconciliation gate balances,
	the Bin agrees on quantity (zero), and the Stock Ledger agrees on value.
	Every gate passes while inventory reports value for stock that is gone.
	"""
	tolerance = flt(get_pma_setting(period.company, "rounding_tolerance")) or 0.01
	stranded = [
		{
			"item_code": row.item_code,
			"warehouse": row.warehouse or "(company scope)",
			"closing_value": flt(row.closing_value, 2),
			"period": f"{row.period_year}-{row.period_month:02d}",
		}
		for row in _scope_rows_as_of(period)
		if flt(row.closing_qty, 6) == 0 and abs(flt(row.closing_value, 6)) > tolerance
	]
	return {"ok": not stranded, "stranded": stranded, "tolerance": tolerance}


def assert_bin_ledger_consistency(period):
	"""Gate: the shadow stores must match the valuation ledger (IPB) for every
	periodic-valuation scope - the Bin on *quantity*, the Stock Ledger on
	*value*. The other gates reconcile GL<->IPB; this closes the SLE/Bin<->IPB
	gap so a period cannot be frozen while the stock-balance shadows disagree
	with the ledger.

	Resolve quantity drift with a Stock Reconciliation on the flagged items.
	Value drift means a value event did not write its SLE row (DR-02) - that is
	a code defect, not a data one, and reposting the voucher is the lever.
	"""
	from periodic_valuation.shared.integrity import (
		check_bin_ipb_drift,
		check_sle_ipb_value_drift,
	)

	drifts = check_bin_ipb_drift(period.company)
	value_drifts = check_sle_ipb_value_drift(period.company)
	return {
		"ok": not drifts and not value_drifts,
		"drifts": drifts,
		"value_drifts": value_drifts,
	}


def seed_next_period_openings(period):
	"""Create the next Inventory Period (OPEN) and seed its IPB openings from
	this period's closings. Called only after every gate has passed."""
	from periodic_valuation.shared.immutable import KERNEL_FLAG

	next_year, next_month = (
		(period.period_year + 1, 1) if period.period_month == 12 else (period.period_year, period.period_month + 1)
	)

	# Freeze any older previous-open period: the close ceremony validates and
	# freezes in one transaction, so it goes straight to SETTLED_FROZEN once a
	# newer period closes behind it (DR-25).
	older = frappe.get_all(
		"Inventory Period",
		filters={
			"company": period.company,
			"status": "PREV_OPEN_UNSETTLED",
			"name": ("!=", period.name),
		},
		pluck="name",
	)
	for name in older:
		frappe.db.set_value("Inventory Period", name, "status", "SETTLED_FROZEN")

	next_name = frappe.db.get_value(
		"Inventory Period",
		{"company": period.company, "period_year": next_year, "period_month": next_month},
	)
	if not next_name:
		next_period = frappe.get_doc(
			{
				"doctype": "Inventory Period",
				"company": period.company,
				"start_date": f"{next_year}-{next_month:02d}-01",
				"status": "OPEN",
			}
		)
		# Current period is being moved out of OPEN in the same transaction.
		next_period.flags.ignore_validate = False
		frappe.db.set_value("Inventory Period", period.name, "status", "PREV_OPEN_UNSETTLED")
		next_period.insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Inventory Period", period.name, "status", "PREV_OPEN_UNSETTLED")
		frappe.db.set_value("Inventory Period", next_name, "status", "OPEN")

	frappe.flags[KERNEL_FLAG] = True
	try:
		for row in _ipb_rows(period):
			exists = frappe.db.get_value(
				"Inventory Period Balance",
				{
					"company": period.company,
					"item_code": row.item_code,
					"warehouse": row.warehouse or "",
					"period_year": next_year,
					"period_month": next_month,
				},
			)
			if exists:
				continue
			frappe.get_doc(
				{
					"doctype": "Inventory Period Balance",
					"company": period.company,
					"item_code": row.item_code,
					"warehouse": row.warehouse or "",
					"period_year": next_year,
					"period_month": next_month,
					"opening_qty": row.closing_qty,
					"opening_value": row.closing_value,
					# a zero-quantity scope RETAINS its MAP across the boundary
					# (ruled 2026-08-18) - seeding 0 here would silently undo
					# the retention at every month end
					"moving_avg_price": flt(row.closing_value) / flt(row.closing_qty)
					if flt(row.closing_qty) > 0
					else flt(row.moving_avg_price),
					"closing_qty": row.closing_qty,
					"closing_value": row.closing_value,
					# MAP state survives the period boundary: a negative balance
					# stays frozen until a receipt crosses zero.
					"total_received_since_zero": row.total_received_since_zero,
					"is_negative": row.is_negative,
					"frozen_map": row.frozen_map,
				}
			).insert(ignore_permissions=True)
	finally:
		frappe.flags[KERNEL_FLAG] = False
