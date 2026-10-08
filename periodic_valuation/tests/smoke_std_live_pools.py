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
  7  YTD: the pools follow the same events and accumulate across the year
     (client, 07/10/2026): an unsettled previous month's pools count in the
     current month; a backdated PPV moves both months; once the months
     settle the current month shows the settled YTD pool
  8  every value equals what the settlement engine would read at that moment
  9  YTD: the Year to Date section adds every balance field across the
     year's months (client, reopened 08/10/2026); MTD balances leave it empty

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
			ytd_rec = flt(frappe.db.get_value("Inventory Period Balance", {"company": pack.COMPANY, "item_code": it,
				"period_year": today.year, "period_month": today.month}, "ytd_receipt_qty"))
			check(f"{view} 9: the Year to Date section is {'filled' if view == 'YTD' else 'left empty'}",
				ytd_rec == (150.0 if view == "YTD" else 0.0), str(ytd_rec))
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
		# ---- 7: YTD accumulates across the year ------------------------------
		if prev.year == today.year:
			it = pack.std_item("_STD-LIVEPOOL-YTDACC", view="YTD")
			pack.scv_release(it, prev.year, prev.month, 10)
			pack.make_pr(it, wh, 100, 12, posting_date=str(p2))     # previous month PPV 200
			pack.make_pr(it, wh, 50, 11)                              # current month PPV 50
			check("7: YTD - the current month shows the year to date (200 + 50), the previous month its own 200",
				_pools(it, prev) == (200.0, 0.0) and _pools(it, today) == (250.0, 0.0),
				f"{_pools(it, prev)} {_pools(it, today)}")
			pack.make_pr(it, wh, 20, 13, posting_date=str(p3))      # backdated PPV 60
			check("7: YTD - a backdated PPV moves the previous month and every later month (260 / 310)",
				_pools(it, prev) == (260.0, 0.0) and _pools(it, today) == (310.0, 0.0),
				f"{_pools(it, prev)} {_pools(it, today)}")
			pack.scv_release(it, today.year, today.month, 11)       # YTD triplet: REV In 170 x 1
			check("7: YTD - a cost change adds its revaluation to the year to date (-170)",
				_pools(it, today) == (310.0, -170.0), str(_pools(it, today)))
			# every balance field accumulates for YTD (client, reopened 08/10/2026)
			cur = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": it,
				"period_year": today.year, "period_month": today.month}, fields=["*"])[0]
			prv = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": it,
				"period_year": prev.year, "period_month": prev.month}, fields=["*"])[0]
			check("9: YTD - the Year to Date section adds the year's months: receipts 120 + 50, value from both months",
				flt(cur.ytd_receipt_qty) == 170 and flt(cur.ytd_receipt_value, 2) == flt(flt(prv.receipt_value) + flt(cur.receipt_value), 2)
				and flt(cur.ytd_reval_value, 2) == flt(flt(prv.reval_value) + flt(cur.reval_value), 2)
				and flt(cur.receipt_qty) == 50 and cur.resolved_settlement_view == "YTD",
				f"ytd {cur.ytd_receipt_qty}/{cur.ytd_receipt_value} month {cur.receipt_qty}")
			check("9: YTD - opening is the year's first month's, closing this month's",
				flt(cur.ytd_opening_qty) == flt(prv.ytd_opening_qty) == flt(prv.opening_qty)
				and flt(cur.ytd_closing_qty) == flt(cur.closing_qty)
				and flt(cur.ytd_closing_value, 2) == flt(cur.closing_value, 2),
				f"{cur.ytd_opening_qty}/{prv.opening_qty} {cur.ytd_closing_qty}/{cur.closing_qty}")
			StdEngine(pack.COMPANY, it).close_period(year=prev.year, month=prev.month, sc=10,
				source=("Inventory Period", prev_period))
			sett = StdEngine(pack.COMPANY, it).close_period(year=today.year, month=today.month, sc=11,
				source=("Inventory Period", cur_period))
			check("7: YTD - once both months settle the current month shows the settled YTD pools",
				_pools(it, today) == (flt(sett.ppv_pool, 2), flt(sett.rev_pool, 2)) == (310.0, -170.0),
				f"{_pools(it, today)} vs {sett.ppv_pool}/{sett.rev_pool}")
		else:
			print("SKIP 7: the previous month is in the prior fiscal year")
	finally:
		frappe.db.rollback(save_point="std_live_pools")
