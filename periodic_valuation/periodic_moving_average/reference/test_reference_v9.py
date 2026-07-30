# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Conformance tests: MAP reference kernel vs SAP_MA_Sample_Entries_v9 anchors.

Every number asserted here is a client-locked value from the workbook /
simulation narrative / signed plan. Run: pytest test_reference_v9.py
(no Frappe required).
"""

import pytest

from periodic_valuation.periodic_moving_average.reference.kernel import Accounts, MapLedger


def r2(x):
	return round(x, 2)


class TestMainScenario:
	"""Workbook v9 Main Scenario — Limestone @ Stores, Feb-Mar."""

	@pytest.fixture()
	def ledger(self):
		led = MapLedger()
		led.open_period("2026-02")

		led.receipt(100, 10)               # 1  PR-001
		led.issue(30)                      # 2  SE-001
		led.receipt(50, 20)                # 3  PR-002

		led.open_period("2026-03")
		led.issue(20)                      # 4  SE-002
		led.landed_cost(200)               # 5  LCV-001
		led.count(-5)                      # 6  SC-001
		led.revaluation(500)               # 7  RV-001
		led.return_in(10, reference_event=2)   # 8  SR-001 (orig issue at 10)
		led.backdated_receipt(              # 9  PR-003 -> Feb (Feb was positive)
			40, 15, "2026-02",
			prior_state={"qty": 120, "value": 1700, "frozen_map": 0},
		)
		led.return_out(50, reference_event=3)  # 10 RET-001 (orig receipt at 20)
		return led

	def test_step_by_step_map(self):
		led = MapLedger()
		led.open_period("2026-02")
		led.receipt(100, 10)
		assert led.map == 10
		led.issue(30)
		assert led.map == 10 and r2(led.value) == 700
		led.receipt(50, 20)
		assert r2(led.map) == 14.17 and r2(led.value) == 1700

		led.open_period("2026-03")
		led.issue(20)
		assert r2(led.events[-1].value_delta) == -283.33
		assert r2(led.value) == 1416.67
		led.landed_cost(200)
		assert r2(led.events[-1].inventory_portion) == 133.33
		assert r2(led.events[-1].expense_portion) == 66.67
		assert r2(led.map) == 15.50 and r2(led.value) == 1550.00
		led.count(-5)
		assert r2(led.events[-1].value_delta) == -77.50
		assert r2(led.map) == 15.50
		led.revaluation(500)
		assert r2(led.map) == 20.76
		led.return_in(10, reference_event=2)
		assert r2(led.events[-1].value_delta) == 100.00
		assert r2(led.map) == 19.74

	def test_backdated_feb_receipt(self, ledger):
		feb = ledger.periods["2026-02"]
		assert feb.receipt_qty == 190 and r2(feb.receipt_value) == 2600
		assert feb.closing_qty == 160 and r2(feb.closing_value) == 2300
		assert r2(feb.closing_value / feb.closing_qty) == 14.38

		mar = ledger.periods["2026-03"]
		assert mar.carryover_qty == 40 and r2(mar.carryover_value) == 600
		# Mar issue NOT recalculated
		assert r2(mar.issue_value - 1000) == 283.33  # 283.33 issue + 1000 return_out

	def test_final_state(self, ledger):
		mar = ledger.periods["2026-03"]
		assert mar.closing_qty == 95
		assert r2(mar.closing_value) == 1672.50
		assert r2(ledger.qty) == 95
		assert r2(ledger.value) == 1672.50
		assert r2(ledger.map) == 17.61

	def test_gl_identity(self, ledger):
		assert ledger.gl_balanced()
		assert ledger.stock_gl_net() == r2(ledger.value) == 1672.50


class TestZeroQtyReset:
	"""Scenario B — zero-qty reset + stock-ratio reset."""

	def test_reset_cycle(self):
		led = MapLedger()
		led.open_period("2026-03")
		# reconstruct the 95 @ 17.605263 state
		led.receipt(95, 1672.50 / 95)
		led.counter = 150.0
		led.open_period("2026-04")

		led.issue(95)
		assert led.qty == 0 and r2(led.value) == 0
		assert led.counter == 0  # reset on zero

		led.receipt(80, 12)
		assert led.counter == 80 and led.map == 12
		led.landed_cost(150)
		assert r2(led.events[-1].inventory_portion) == 150.00  # ratio 1.0
		assert r2(led.value) == 1110.00
		assert round(led.map, 3) == 13.875


class TestNegativeStockPRD:
	"""Scenario C — Negative Balance sheet (client's Negative.xlsx numbers)."""

	def test_prd_flow(self):
		led = MapLedger()
		led.open_period("2026-04")
		led.receipt(55, 12)
		assert led.map == 12

		led.issue(65)
		assert led.qty == -10 and r2(led.value) == -120
		assert led.is_negative and led.frozen_map == 12

		e = led.receipt(2, 15)  # still negative
		assert e.reason == "receipt_neg" and r2(e.prd_amount) == 6
		assert led.qty == -8 and r2(led.value) == -96
		assert led.frozen_map == 12

		e = led.receipt(3, 17)
		assert r2(e.prd_amount) == 15
		assert led.qty == -5 and r2(led.value) == -60

		e = led.receipt(10, 13)  # crosses zero
		assert e.reason == "receipt_cross_zero" and r2(e.prd_amount) == 5
		assert led.qty == 5 and r2(led.value) == 65
		assert led.map == 13  # reset to crossing price, NOT blended
		assert not led.is_negative
		assert led.counter == 5  # fresh cycle = excess qty

		total_prd = r2(sum(g.debit for g in led.gl if g.account == Accounts.PRD))
		assert total_prd == 26
		assert led.gl_balanced()
		assert led.stock_gl_net() == r2(led.value) == 65


class TestBackdatedCaseC1:
	"""Scenario E — backdated receipt into previously-negative prior period,
	current period positive (client-locked: Mar PRD 252, Apr 19/587/30.8947)."""

	def test_c1(self):
		led = MapLedger()
		led.open_period("2026-03")
		led.receipt(5, 30)          # 5 @ 150
		led.issue(15)               # -> -10 @ -150, frozen 15... build state:
		# rebuild exactly: want Mar close -10/-150 frozen 15
		led = MapLedger()
		led.open_period("2026-03")
		led.receipt(10, 15)         # 10/150 MAP 15
		led.issue(20)               # -10/-150 frozen 15
		assert led.qty == -10 and r2(led.value) == -150 and led.frozen_map == 15

		led.open_period("2026-04")
		led.receipt(20, 17.5)       # crosses zero: clearing 10, excess 10 @ 17.5
		# client setup wants Apr at 10 @ 20 / value 200: use explicit numbers
		# instead — cross-zero math gives excess*17.5=175 ... adjust to match
		# the client's setup by revaluing to 200
		led.revaluation(200 - led.value)
		assert led.qty == 10 and r2(led.value) == 200 and r2(led.map) == 20

		mar_state = {"qty": -10, "value": -150, "frozen_map": 15}
		led.backdated_receipt(9, 43, "2026-03", prior_state=mar_state)

		mar = led.periods["2026-03"]
		assert r2(mar.prd_value) == -252  # PRD reduces Mar inventory value
		prd_gl = r2(
			sum(g.debit for g in led.gl if g.account == Accounts.PRD and g.period == "2026-03")
		)
		assert prd_gl == 252

		apr = led.periods["2026-04"]
		assert apr.carryover_qty == 9 and r2(apr.carryover_value) == 135
		assert r2(apr.adjust_value) == 252  # system absorb entry
		absorb_gl = [
			g for g in led.gl if g.account == Accounts.INV_VARIANCE and g.period == "2026-04"
		]
		assert len(absorb_gl) == 1 and r2(absorb_gl[0].credit) == 252

		assert led.qty == 19
		assert r2(led.value) == 587
		assert round(led.map, 4) == 30.8947


class TestBackdatedCaseC2:
	"""Signed plan C2 — both periods negative (Mar -5/-75 fz 15, Apr -3/-45 fz 15,
	backdated 9@43 -> Mar inv 247 + PRD 140, Apr absorb 56, final 6 @ 43)."""

	def test_c2(self):
		led = MapLedger()
		led.open_period("2026-03")
		led.receipt(5, 15)
		led.issue(10)               # -5/-75 frozen 15
		assert led.qty == -5 and r2(led.value) == -75 and led.frozen_map == 15

		led.open_period("2026-04")
		led.issue(0)                # no-op to anchor period
		# Apr activity: +2 receipts/issues to reach -3/-45 while staying frozen
		led.receipt(2, 15)          # still neg: -3, value -75+30=-45
		assert led.qty == -3 and r2(led.value) == -45 and led.frozen_map == 15

		mar_state = {"qty": -5, "value": -75, "frozen_map": 15}
		led.backdated_receipt(9, 43, "2026-03", prior_state=mar_state)

		mar = led.periods["2026-03"]
		assert r2(mar.prd_value) == -140
		prd_gl = r2(
			sum(g.debit for g in led.gl if g.account == Accounts.PRD and g.period == "2026-03")
		)
		assert prd_gl == 140

		apr = led.periods["2026-04"]
		assert r2(apr.carryover_value) == 247
		assert r2(apr.adjust_value) == 56

		assert led.qty == 6
		assert r2(led.value) == 258  # 6 x 43
		assert r2(led.map) == 43
		assert not led.is_negative
		assert led.gl_balanced()


class TestFXInvoiceDiff:
	"""Scenario D — EUR receipt + later invoice at different price AND FX."""

	def test_fx_split(self):
		led = MapLedger()
		led.open_period("2026-06")
		led.receipt(20, 50 * 1.10)  # base receipt 1,100
		assert r2(led.value) == 1100

		e = led.fx_invoice_diff(
			qty=20, receipt_unit_foreign=50, invoice_unit_foreign=52,
			fx_at_receipt=1.10, fx_at_invoice=1.15,
		)
		assert r2(e.inventory_portion) == 44.00   # ratio 1.0
		assert r2(e.fx_variance) == 52.00
		assert r2(led.value) == 1144.00
		assert r2(led.map) == 57.20
		assert led.gl_balanced()

	def test_client_locked_lc_example(self):
		"""FX with Stock Ratio sheet: 3 units USD 100->120, FX 10,000->13,000 LC."""
		led = MapLedger()
		led.open_period("2026-06")
		led.receipt(3, 100 * 10000)  # 3,000,000 LC
		e = led.fx_invoice_diff(
			qty=3, receipt_unit_foreign=100, invoice_unit_foreign=120,
			fx_at_receipt=10000, fx_at_invoice=13000,
		)
		# inventory component 3 x 20 x 10,000 = 600,000 (all to stock, ratio 1)
		assert r2(e.inventory_portion) == 600000
		# FX variance: (3x120x13000 - 3x100x10000) - 600000 = 1,080,000
		assert r2(e.fx_variance) == 1080000


class TestCancellation:
	def test_cancel_receipt(self):
		led = MapLedger()
		led.open_period("2026-05")
		led.receipt(100, 10)
		led.receipt(50, 20)
		evt = led.cancel(2)  # cancel the 50 @ 20 receipt, dated today
		assert evt.qty_delta == -50 and r2(evt.value_delta) == -1000
		assert led.qty == 100 and r2(led.value) == 1000 and led.map == 10
		assert led.gl_balanced()
		with pytest.raises(ValueError):
			led.cancel(2)  # double-reversal refused

	def test_cancellation_posts_on_own_date(self):
		led = MapLedger()
		led.open_period("2026-05")
		led.receipt(10, 10)
		led.open_period("2026-06")
		led.cancel(1, period="2026-06")
		months = {g.period for g in led.gl if g.event_id == led.events[-1].event_id}
		assert months == {"2026-06"}


class TestReturnWithoutReference:
	def test_no_ref_return_keeps_map(self):
		led = MapLedger()
		led.open_period("2026-05")
		led.receipt(100, 10)
		led.receipt(50, 20)          # MAP 13.3333
		map_before = led.map
		led.return_in(10)            # without reference -> at current MAP
		assert led.map == map_before
