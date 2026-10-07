"""The PPV and revaluation pools on a standard-cost Inventory Period Balance
follow every valuation event (client ticket STD-013, 06/10/2026; DR-58). Run:
bench --site <site> execute periodic_valuation.tests.smoke_std_live_pools.run

  1  a receipt above standard puts its PPV in the pool at once (100 x 2 = 200)
  2  a receipt below standard takes it back down (50 x -1 -> 150)
  3  an issue leaves both pools unchanged
  4  a standard cost change puts its revaluation in the revaluation pool at
     once (REV In 150 x 1 -> -150; REV out stays out of the pool)
  5  at settlement the period balance's pools equal the settled pools
  6  the next month's balance carries the settlement's ending-stock share
     as soon as the settlement posts (MTD carry)
  7  YTD: the pools follow the same events
  8  every value equals what the settlement engine would read at that moment

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def _pools(item, d):
	d = getdate(d)
	row = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["ppv_pool", "rev_pool"])
	return (flt(row[0].ppv_pool, 2), flt(row[0].rev_pool, 2)) if row else (None, None)


def _engine_pools(item, d):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	d = getdate(d)
	e = StdEngine(pack.COMPANY, item)
	return (flt(e.pool_ppv(d.year, d.month), 2), flt(e.pool_rev(d.year, d.month), 2))


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD live pool failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_days(day1, -1)
	p2, p3 = add_days(get_first_day(prev), 1), add_days(get_first_day(prev), 2)
	frappe.db.savepoint("std_live_pools")
	try:
		wh, _wh2 = pack.ensure_company()
		prev_period = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
			"revaluation_posting_date", "First day of the period")

		for view in ("MTD", "YTD"):
			it = pack.std_item(f"_STD-LIVEPOOL-{view}", view=view)
			pack.scv_release(it, today.year, today.month, 10)
			pack.make_pr(it, wh, 100, 12)
			check(f"{view} 1: a receipt above standard puts 200 in the PPV pool at once",
				_pools(it, today) == (200.0, 0.0), str(_pools(it, today)))
			pack.make_pr(it, wh, 50, 9)
			check(f"{view} 2: a receipt below standard takes it to 150",
				_pools(it, today) == (150.0, 0.0), str(_pools(it, today)))
			pack.make_dn(it, wh, 30)
			check(f"{view} 3: an issue leaves both pools unchanged",
				_pools(it, today) == (150.0, 0.0), str(_pools(it, today)))
			pack.scv_release(it, today.year, today.month, 11)
			check(f"{view} 4: a cost change puts its revaluation in the pool at once (REV In 150 -> -150)",
				_pools(it, today) == (150.0, -150.0), str(_pools(it, today)))
			check(f"{view} 8: the balance shows what the settlement engine reads now",
				_pools(it, today) == _engine_pools(it, today), f"{_pools(it, today)} vs {_engine_pools(it, today)}")
			sett = StdEngine(pack.COMPANY, it).close_period(year=today.year, month=today.month, sc=11,
				source=("Inventory Period", cur_period))
			check(f"{view} 5: at settlement the pools equal the settled pools",
				_pools(it, today) == (flt(sett.ppv_pool, 2), flt(sett.rev_pool, 2)),
				f"{_pools(it, today)} vs {sett.ppv_pool}/{sett.rev_pool}")

		# ---- 6: MTD carry into the next month ---------------------------------
		it = pack.std_item("_STD-LIVEPOOL-CARRY")
		pack.scv_release(it, prev.year, prev.month, 10)
		pack.make_pr(it, wh, 100, 12, posting_date=str(p2))
		pack.make_dn(it, wh, 50, posting_date=str(p3))
		pack.make_pr(it, wh, 10, 10)                     # gives the current month a balance row
		before = _pools(it, today)
		sett = StdEngine(pack.COMPANY, it).close_period(year=prev.year, month=prev.month, sc=10,
			source=("Inventory Period", prev_period))
		check("6: the previous month's balance shows its settled pools",
			_pools(it, prev) == (flt(sett.ppv_pool, 2), flt(sett.rev_pool, 2)), f"{_pools(it, prev)}")
		check("6: the current month's balance carries the ending-stock share at once (200 x 50/100 = 100)",
			before == (0.0, 0.0) and _pools(it, today) == (flt(sett.ppv_es, 2), 0.0) == (100.0, 0.0),
			f"before {before} after {_pools(it, today)} es {sett.ppv_es}")
	finally:
		frappe.db.rollback(save_point="std_live_pools")
