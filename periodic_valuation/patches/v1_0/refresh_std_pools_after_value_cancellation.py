# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client ticket TSK-2026-01095 (10/11/2026): the exact reversal of a
standard-cost invoice difference or landed cost (a Purchase Invoice's or
Landed Cost Voucher's Cancellation) did not refresh the period balance's
pools, so the PPV Pool kept the reversed amount. Refresh every standard-cost
balance once. Display fields only - no ledger or balance value moves."""

from periodic_valuation.patches.v1_0.refresh_std_period_pools import execute as refresh_all


def execute():
	refresh_all()
