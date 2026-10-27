# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-70: Total Received Since Zero restarts when the quantity passes through
zero on its way down (client tickets STD-022 / TSK-2026-00936 and
TSK-2026-00981, ABC Item - MAP Test - MH #63: 15 at a closing of -15).

The counter is the kernel's own running state, kept in the order documents
were entered - the signed MAP rule (31 Aug 2026, Special Cases #1 / #2)
measures a backdated receipt against each later period's stock as it stands
- so existing rows are re-derived by replaying every movement of the scope in
that order with the kernel's rules: in its own month, a receipt or transfer
in adds (a receipt crossing from negative restarts at its excess), a
purchase return and the cancellation of either net it (floored at 0); a
backdated movement carries into each later month already open, where only a
receipt counts (as _carry_receipt_into_open); landing on zero restarts it,
and now so does passing through zero on the way down. A month's row starts
from the month before it as it stood when the row was opened.

Display field and the Cost Adjustment ratio's denominator; no GL. Only rows
whose counter changes are written."""

import frappe
from frappe.utils import flt

RECEIPTS = ("receipt", "transfer_in")
NETTING = ("return_out",)


def _apply(state, kind, d, own):
	qty, counter = state
	if own and kind == "cancellation_netting":
		counter = max(0.0, counter + d)
	elif kind in RECEIPTS:
		counter = (qty + d) if (qty < 0 and qty + d > 0) else counter + d
	elif own and kind in NETTING:
		counter = max(0.0, counter + d)
	new_qty = round(qty + d, 6)
	if abs(new_qty) < 1e-9 or (qty >= 0 and new_qty < 0):
		counter = 0.0
	return [new_qty, counter]


def replay(company, item_code, warehouse):
	"""{(year, month): counter} for the scope, as the kernel keeps it."""
	wh_cond = "AND sme.warehouse = %(wh)s" if warehouse else ""
	rows = frappe.db.sql(f"""SELECT sme.period_year y, sme.period_month m, sme.qty_delta d, sme.movement_type t,
			orig.movement_type ot
		FROM `tabStock Movement Event` sme
		LEFT JOIN `tabStock Movement Event` orig ON orig.name = sme.reversal_of
		WHERE sme.company = %(company)s AND sme.item_code = %(item)s AND sme.is_cancelled = 0 {wh_cond}
		ORDER BY sme.creation, sme.name""",
		{"company": company, "item": item_code, "wh": warehouse}, as_dict=True)
	states = {}
	for r in rows:
		key = (int(r.y), int(r.m))
		if key not in states:
			earlier = [k for k in states if k < key]
			states[key] = list(states[max(earlier)]) if earlier else [0.0, 0.0]
		kind = r.t
		if kind == "cancellation":
			kind = "cancellation_netting" if r.ot in RECEIPTS + NETTING else None
		d = flt(r.d)
		for k in sorted(states):
			if k >= key:
				states[k] = _apply(states[k], kind, d, own=(k == key))
	return {k: round(v[1], 6) for k, v in states.items()}


def execute():
	scopes = frappe.db.sql("""SELECT DISTINCT b.company, b.item_code, b.warehouse
		FROM `tabInventory Period Balance` b JOIN `tabItem` i ON i.name = b.item_code
		WHERE i.valuation_method = 'Periodic Moving Average'""", as_dict=True)
	changed = 0
	for s in scopes:
		counters = replay(s.company, s.item_code, s.warehouse or None)
		for row in frappe.get_all("Inventory Period Balance", filters={"company": s.company, "item_code": s.item_code,
				"warehouse": s.warehouse or ""}, fields=["name", "period_year", "period_month", "closing_qty",
				"total_received_since_zero"]):
			key = (row.period_year, row.period_month)
			if key not in counters or flt(row.closing_qty) >= 0:
				# DR-70 changes a counter only through a downward pass, which
				# leaves the row negative until a crossing receipt restarts it;
				# a row at or above zero keeps the figure it has
				continue
			if abs(counters[key] - flt(row.total_received_since_zero)) > 1e-6:
				frappe.db.set_value("Inventory Period Balance", row.name, "total_received_since_zero",
					counters[key], update_modified=False)
				changed += 1
	print(f"DR-70: {changed} since-zero counters re-derived")
