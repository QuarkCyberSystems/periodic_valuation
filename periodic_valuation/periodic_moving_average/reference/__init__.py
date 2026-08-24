# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Pure-Python MAP reference simulator (consolidated design DR-18).

Executable behavioral spec for the Periodic Moving Average kernel, validated
against workbook SAP_MA_Sample_Entries_v9 anchors (main scenario 1,672.50;
negative-stock PRD 26; backdated-negative C1 252/587/30.8947 and C2 247/140/56;
zero-qty reset; FX split 44/52). No Frappe imports - runnable under pytest.

The Frappe kernel in periodic_valuation.periodic_moving_average.kernel must reproduce
this module's numbers exactly; conformance tests replay both.
"""

from periodic_valuation.periodic_moving_average.reference.kernel import Accounts, MapLedger

__all__ = ["Accounts", "MapLedger"]
