# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Frozen MAP of a moving-average balance at zero quantity shows the retained
MAP (client ticket MAP-003). Before, the kernel cleared it to 0 whenever the
quantity was not negative. Display only: pricing reads Frozen MAP only while
the balance is negative, so no quantity, value or GL moves. Periods already
settled and frozen are left as closed. Idempotent: only rows still at 0."""

import frappe


def execute():
	frappe.db.sql(
		"""update `tabInventory Period Balance` b
		   join `tabInventory Period` p
		     on p.company = b.company and p.period_year = b.period_year
		    and p.period_month = b.period_month
		   set b.frozen_map = b.moving_avg_price
		   where b.valuation_method = 'Periodic Moving Average'
		     and b.closing_qty = 0 and b.is_negative = 0
		     and ifnull(b.frozen_map, 0) = 0 and ifnull(b.moving_avg_price, 0) != 0
		     and p.status != 'SETTLED_FROZEN'"""
	)
	print(f"periodic_valuation: zero-quantity balances now show their retained MAP as Frozen MAP: {frappe.db._cursor.rowcount}")
