# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Exact-reversal mirrors posted before the stamp existed carry value_delta 0
while their GL inventory leg is real. Read the leg back from GL and stamp it,
so event-level inventory effects reconcile without re-posting anything."""

import frappe
from frappe.utils import flt


def execute():
	rows = frappe.db.sql(
		"""
		SELECT e.name, e.company, e.item_code, e.warehouse
		FROM `tabInventory Valuation Event` e
		WHERE e.reversal_of IS NOT NULL AND e.reversal_of != ''
			AND e.is_cancelled = 0 AND COALESCE(e.value_delta, 0) = 0
		""",
		as_dict=True,
	)
	if not rows:
		return
	from periodic_valuation.shared.accounts import get_inventory_account

	stamped = 0
	for r in rows:
		try:
			stock_acct = get_inventory_account(r.company, r.item_code, r.warehouse)
		except Exception:
			continue
		net = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(debit - credit), 0) FROM `tabGL Entry`
			WHERE valuation_event_id = %s AND account = %s AND is_cancelled = 0
			""",
			(r.name, stock_acct),
		)[0][0]
		if flt(net, 2):
			frappe.db.set_value(
				"Inventory Valuation Event", r.name, "value_delta", flt(net, 2), update_modified=False
			)
			stamped += 1
	print(f"backfill_reversal_value_delta: stamped {stamped} of {len(rows)} reversal events")
