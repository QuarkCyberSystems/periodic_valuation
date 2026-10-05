"""Standard cost change switching at its release (DR-50 as amended
05/10/2026; client tickets STD-003 / ISCV-2026-00118). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_reval_release_day.run

Periodic Standard Cost Settings > Revaluation Posting Date = "Date of
release": the change switches when it is released, inside the current
period. Movements dated before the release keep the earlier cost; the new
cost prices from the release date; the stock on hand is revalued once,
dated the release day (Rev Rel).

  A  a change for the current month, 70 -> 90, released on day D: one Rev
     Rel = on hand 110 x 20 = 2,200 dated D (GL and stock ledger the same
     day); dates before D resolve 70, D onwards 90; a receipt dated D is at
     90; a late receipt / issue dated before D is at 70 and bridges
     qty x 20 into D as Rev Rel; the settlement shares the release's
     amount between ending stock and the consumption after the switch
     (102 / 122), the rest of the pool over the whole month; GL / event
     identity holds
  B  cancelling a receipt that was on hand at the release gives the
     revaluation back with it: the item's stock value returns to zero;
     cancelling a late receipt reverses its bridge with it
  C  a backdated change (valid from the previous, still-open month)
     switches at the release too: the previous month and the days before
     the release keep the earlier cost; with nothing consumed since, the
     settlement gives the revaluation wholly to ending stock
  D  an item's first cost prices its whole month at once
  E  a change for a future month switches on day 1 of that month
     (first-day boundary), with nothing posted at release
  F  two releases on the same day: the second revalues from the first
  G  re-stamp: a version released under the old "Last day of the period"
     rule and still pending switches at its release and revalues now;
     cancelling a receipt entered between its release and the re-stamp
     gives its revaluation back; a pending version for a later month goes
     back to switching on day 1 of that month, nothing posted
  H  back on "First day of the period" the day-1 rule is unchanged
  I  YTD: a release in the previous month stays in the year's pool; the
     current month's settlement still shares it by the consumption since
     the switch across both months (40 / 90), not by the year's share
  J  the migration patch moves a "Last day of the period" setting to
     "Date of release"

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, flt, get_first_day, get_last_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
SCV = "Item Standard Cost Version"


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


class _Today:
	def __init__(self, day):
		self.day = str(day)

	def __enter__(self):
		import frappe.utils
		self._orig = frappe.utils.nowdate
		frappe.utils.nowdate = lambda: self.day

	def __exit__(self, *exc):
		import frappe.utils
		frappe.utils.nowdate = self._orig


def _set(value):
	frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY}, "revaluation_posting_date", value)


def _events(source, trans=None):
	f = {"source_docname": source, "is_cancelled": 0}
	if trans:
		f["std_trans"] = trans
	return frappe.get_all("Inventory Valuation Event", filters=f,
		fields=["std_trans", "total_sc", "posting_date", "standard_cost", "actual_cost", "cost_version"])


def _sc(item, day):
	from periodic_valuation.periodic_standard_cost.engine import get_active_standard_cost

	return flt(get_active_standard_cost(pack.COMPANY, item, None, day).standard_cost)


def _stock_value(item):
	"""Inventory value of the item on the books: its valuation events' net
	stock effect."""
	return flt(frappe.db.sql(
		"""select coalesce(sum(value_delta), 0) from `tabInventory Valuation Event`
		where company = %s and item_code = %s and is_cancelled = 0""",
		(pack.COMPANY, item))[0][0])


def _settle(item, year, month):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	engine = StdEngine(pack.COMPANY, item)
	period = frappe.db.get_value("Inventory Period",
		{"company": pack.COMPANY, "period_year": year, "period_month": month})
	last = get_last_day(f"{year}-{month:02d}-01")
	return engine.close_period(year=year, month=month, sc=_sc(item, last),
		source=("Inventory Period", period))


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _run():
	# the release day D needs earlier days in its month for the late entries
	real = getdate(nowdate())
	today = real if real.day >= 4 else add_days(get_first_day(real), -1)
	with _Today(today):
		_scenarios(today)

	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD release-day revaluation failures: " + "; ".join(c[0] for c in failed))


def _scenarios(today):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.shared.period_close import assert_event_gl_identity

	day1, last = get_first_day(today), get_last_day(today)
	early = add_days(day1, 1)
	prev = add_days(day1, -1)
	nxt = add_days(last, 1)
	frappe.db.savepoint("std_reval_release")
	try:
		wh, _wh2 = pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cur_period = pack.make_period(today.year, today.month, "OPEN")
		_set("Date of release")

		# ---- A: current month, 70 -> 90 ---------------------------------
		a = pack.std_item("_STD-RELDAY-A")
		pack.scv_release(a, prev.year, prev.month, 70)
		pack.make_pr(a, wh, 150, 70, posting_date=str(day1))
		pack.make_dn(a, wh, 40, posting_date=str(early))
		v90 = pack.scv_release(a, today.year, today.month, 90)
		v90.reload()
		check("A: the version switches at its release: effective from D, prices from this month",
			v90.switch_on_release and not v90.switch_at_period_end
			and getdate(v90.effective_from) == today and getdate(v90.revaluation_date) == today
			and (v90.price_from_year, v90.price_from_month) == (today.year, today.month),
			f"{v90.effective_from} {v90.price_from_year}-{v90.price_from_month}")
		rel = _events(v90.name)
		check("A: one Rev Rel = on hand 110 x 20 = 2,200 dated D, posted at release",
			len(rel) == 1 and rel[0].std_trans == "Rev Rel" and flt(rel[0].total_sc) == 2200
			and getdate(rel[0].posting_date) == today and v90.revaluation_posted, str(rel))
		check("A: its GL and stock-ledger row are dated D",
			set(frappe.get_all("GL Entry", filters={"voucher_no": v90.name, "is_cancelled": 0}, pluck="posting_date")) == {today}
			and set(frappe.get_all("Stock Ledger Entry", filters={"voucher_no": v90.name, "is_cancelled": 0},
				pluck="posting_date")) == {today})
		check("A: dates before D keep 70; D and next month resolve 90",
			_sc(a, early) == 70 and _sc(a, add_days(today, -1)) == 70 and _sc(a, today) == 90 and _sc(a, nxt) == 90)

		pr_now = pack.make_pr(a, wh, 10, 90, posting_date=str(today))
		check("A: a receipt dated D is valued at 90",
			[flt(e.standard_cost) for e in _events(pr_now.name)] == [90], str(_events(pr_now.name)))
		pr_late = pack.make_pr(a, wh, 5, 70, posting_date=str(early))
		ev = {e.std_trans: e for e in _events(pr_late.name)}
		check("A: a late receipt dated before D is at 70 and bridges 5 x 20 = 100 into D",
			flt(ev.get("Rec", {}).get("standard_cost")) == 70 and "Rev Rel" in ev
			and flt(ev["Rev Rel"].total_sc) == 100 and getdate(ev["Rev Rel"].posting_date) == today
			and ev["Rev Rel"].cost_version == v90.name, str(ev))
		dn_late = pack.make_dn(a, wh, 3, posting_date=str(early))
		ev = {e.std_trans: e for e in _events(dn_late.name)}
		check("A: a late issue dated before D is at 70 and bridges -3 x 20 = -60 into D",
			flt(ev.get("Iss", {}).get("standard_cost")) == 70 and flt(ev.get("Rev Rel", {}).get("total_sc")) == -60,
			str(ev))
		dn_now = pack.make_dn(a, wh, 20, posting_date=str(today))
		check("A: an issue dated D carries no bridge",
			[e.std_trans for e in _events(dn_now.name)] == ["Iss"], str(_events(dn_now.name)))
		check("A: stock on hand 102 is carried at 90",
			_stock_value(a) == 102 * 90, str(_stock_value(a)))

		sett = _settle(a, today.year, today.month)
		# release amount 2,240 (2,200 + 100 - 60) over ending 102 + consumed
		# since the switch 20; the receipt dated D at 70 vs 90 adds no Rev
		check("A: the settlement shares the release over ending stock and the consumption since it",
			flt(sett.rev_pool) == -2240 and abs(flt(sett.rev_es) - (-2240 * 102 / 122)) < 0.01
			and abs(flt(sett.rev_es) + flt(sett.rev_cons) + 2240) < 0.01,
			f"pool {sett.rev_pool} es {sett.rev_es} cons {sett.rev_cons}")
		check("A: inventory GL equals the valuation events for the month",
			assert_event_gl_identity(frappe.get_doc("Inventory Period", cur_period))["ok"])

		# ---- B: cancel a receipt that was on hand at the release ---------
		b = pack.std_item("_STD-RELDAY-B")
		pack.scv_release(b, prev.year, prev.month, 10)
		pr_b = pack.make_pr(b, wh, 50, 10, posting_date=str(early))
		v12 = pack.scv_release(b, today.year, today.month, 12)
		check("B: the release revalues the 50 on hand by 100",
			[flt(e.total_sc) for e in _events(v12.name)] == [100])
		cxl = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr_b.name))
		cxl.submit()
		ev = _events(cxl.name)
		check("B: the cancellation reverses at 10 and gives back the revaluation (-100, Rev Rel)",
			sorted((e.std_trans, flt(e.total_sc)) for e in ev) == [("Rec", -500.0), ("Rev Rel", -100.0)], str(ev))
		check("B: the item's stock value returns to zero", _stock_value(b) == 0, str(_stock_value(b)))
		pr_b2 = pack.make_pr(b, wh, 7, 10, posting_date=str(early))  # late: bridges 7 x 2 = 14
		cxl2 = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr_b2.name))
		cxl2.submit()
		ev = _events(cxl2.name)
		check("B: cancelling a late receipt reverses it and its bridge, nothing more",
			sorted((x.std_trans, flt(x.total_sc)) for x in ev) == [("Rec", -70.0), ("Rev Rel", -14.0)]
			and _stock_value(b) == 0, f"{ev} {_stock_value(b)}")

		# ---- C: backdated, valid from the previous month ------------------
		c = pack.std_item("_STD-RELDAY-C")
		pack.scv_release(c, prev.year, prev.month, 10)
		pack.make_pr(c, wh, 61, 10, posting_date=str(prev))
		vc = pack.scv_release(c, prev.year, prev.month, 12)
		vc.reload()
		check("C: a backdated change switches at its release; the previous month and the days before keep 10",
			vc.switch_on_release and getdate(vc.effective_from) == today
			and _sc(c, prev) == 10 and _sc(c, early) == 10 and _sc(c, today) == 12, str(vc.effective_from))
		check("C: it revalues the 61 on hand by 122 on D",
			[(e.std_trans, flt(e.total_sc), getdate(e.posting_date)) for e in _events(vc.name)]
			== [("Rev Rel", 122.0, today)], str(_events(vc.name)))
		_settle(c, prev.year, prev.month)
		sett_c = _settle(c, today.year, today.month)
		check("C: nothing consumed since the switch: the settlement gives it wholly to ending stock",
			abs(flt(sett_c.rev_es) + 122) < 0.01 and abs(flt(sett_c.rev_cons)) < 0.01,
			f"es {sett_c.rev_es} cons {sett_c.rev_cons}")

		# ---- D: first cost ----------------------------------------------
		d = pack.std_item("_STD-RELDAY-D")
		vd = pack.scv_release(d, today.year, today.month, 40)
		vd.reload()
		check("D: an item's first cost prices its whole month at once",
			not vd.switch_on_release and vd.revaluation_posted and _sc(d, day1) == 40)

		# ---- E: future month ----------------------------------------------
		e = pack.std_item("_STD-RELDAY-E")
		pack.scv_release(e, prev.year, prev.month, 20)
		pack.make_pr(e, wh, 5, 20, posting_date=str(today))
		ve = pack.scv_release(e, nxt.year, nxt.month, 25)
		ve.reload()
		check("E: a future change switches on day 1 of its month, nothing posted now",
			not ve.switch_on_release and getdate(ve.effective_from) == nxt and not _events(ve.name)
			and _sc(e, today) == 20, str(ve.effective_from))

		# ---- F: two releases on the same day -----------------------------
		f = pack.std_item("_STD-RELDAY-F")
		pack.scv_release(f, prev.year, prev.month, 50)
		pack.make_pr(f, wh, 8, 50, posting_date=str(early))
		vf1 = pack.scv_release(f, today.year, today.month, 55)
		vf2 = pack.scv_release(f, today.year, today.month, 60)
		check("F: the second same-day release revalues from the first (8 x 5 each)",
			[flt(x.total_sc) for x in _events(vf1.name)] == [40] and [flt(x.total_sc) for x in _events(vf2.name)] == [40]
			and _sc(f, today) == 60 and _sc(f, early) == 50
			and frappe.db.get_value(SCV, vf1.name, "status") == "RELEASED")

		# ---- G: re-stamp a pending period-end version ---------------------
		from periodic_valuation.periodic_standard_cost.doctype.item_standard_cost_version.item_standard_cost_version import (
			restamp_period_end_switches,
		)

		g = pack.std_item("_STD-RELDAY-G")
		vg0 = pack.scv_release(g, prev.year, prev.month, 30)
		pack.make_pr(g, wh, 10, 30, posting_date=str(early))
		vg = frappe.get_doc({"doctype": SCV, "company": pack.COMPANY, "item_code": g,
			"valid_from_year": prev.year, "valid_from_month": prev.month,
			"standard_cost": 35, "source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
		frappe.db.set_value(SCV, vg.name, {
			"status": "RELEASED", "switch_at_period_end": 1, "revaluation_posted": 0,
			"price_from_year": nxt.year, "price_from_month": nxt.month, "revaluation_date": last,
			"effective_from": nxt, "released_on": f"{today} {frappe.utils.nowtime()}", "supersedes_version": vg0.name,
		}, update_modified=False)
		pr_g = pack.make_pr(g, wh, 4, 30, posting_date=str(today))  # posted at the old cost before the re-stamp
		g2 = pack.std_item("_STD-RELDAY-G2")
		vg20 = pack.scv_release(g2, prev.year, prev.month, 20)
		pack.make_pr(g2, wh, 6, 20, posting_date=str(early))
		vg2 = frappe.get_doc({"doctype": SCV, "company": pack.COMPANY, "item_code": g2,
			"valid_from_year": nxt.year, "valid_from_month": nxt.month,
			"standard_cost": 30, "source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
		nxt_last = get_last_day(nxt)
		frappe.db.set_value(SCV, vg2.name, {
			"status": "RELEASED", "switch_at_period_end": 1, "revaluation_posted": 0,
			"price_from_year": add_days(nxt_last, 1).year, "price_from_month": add_days(nxt_last, 1).month,
			"revaluation_date": nxt_last, "effective_from": add_days(nxt_last, 1),
			"released_on": f"{today} {frappe.utils.nowtime()}", "supersedes_version": vg20.name,
		}, update_modified=False)
		restamp_period_end_switches()
		vg.reload()
		check("G: the pending version now switches at its release",
			vg.switch_on_release and not vg.switch_at_period_end and getdate(vg.effective_from) == today
			and vg.revaluation_posted and _sc(g, today) == 35 and _sc(g, early) == 30, str(vg.effective_from))
		check("G: it revalues the stock on hand now, 14 x 5 = 70, on its release day",
			[(x.std_trans, flt(x.total_sc), getdate(x.posting_date)) for x in _events(vg.name)]
			== [("Rev Rel", 70.0, today)], str(_events(vg.name)))
		check("G: the item is carried at 35", _stock_value(g) == 14 * 35, str(_stock_value(g)))
		cxl_g = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr_g.name))
		cxl_g.submit()
		check("G: cancelling the receipt entered before the re-stamp gives back its 4 x 5",
			sorted((x.std_trans, flt(x.total_sc)) for x in _events(cxl_g.name)) == [("Rec", -120.0), ("Rev Rel", -20.0)]
			and _stock_value(g) == 10 * 35, f"{_events(cxl_g.name)} {_stock_value(g)}")
		vg2.reload()
		check("G: a pending version for a later month switches on day 1 of it, nothing posted now",
			not vg2.switch_at_period_end and not vg2.switch_on_release and not vg2.revaluation_posted
			and getdate(vg2.effective_from) == nxt and not _events(vg2.name)
			and _sc(g2, today) == 20 and _sc(g2, nxt) == 30, str(vg2.effective_from))

		# ---- H: back on the first day ------------------------------------
		_set("First day of the period")
		h = pack.std_item("_STD-RELDAY-H")
		pack.scv_release(h, prev.year, prev.month, 20)
		pack.make_pr(h, wh, 5, 20, posting_date=str(today))
		v25 = pack.scv_release(h, today.year, today.month, 25)
		v25.reload()
		t = _events(v25.name)
		check("H: the first-day rule is unchanged - day-1 triplet, new cost all month",
			t and {getdate(x.posting_date) for x in t} == {day1} and _sc(h, day1) == 25
			and not v25.switch_on_release and getdate(v25.revaluation_date) == day1, str(t))

		# ---- I: YTD, release in the previous month ------------------------
		_set("Date of release")
		if prev.year == today.year:
			i = pack.std_item("_STD-RELDAY-I", view="YTD")
			p2, p3, p10 = (add_days(get_first_day(prev), n) for n in (1, 2, 9))
			with _Today(p10):
				pack.scv_release(i, prev.year, prev.month, 40)
				pack.make_pr(i, wh, 100, 40, posting_date=str(p2))
				pack.make_dn(i, wh, 10, posting_date=str(p3))        # before the switch
				vi = pack.scv_release(i, prev.year, prev.month, 50)  # 90 on hand -> 900
				pack.make_dn(i, wh, 20, posting_date=str(p10))       # after the switch
			check("I: the release revalues the 90 on hand by 900",
				[flt(x.total_sc) for x in _events(vi.name)] == [900], str(_events(vi.name)))
			_settle(i, prev.year, prev.month)
			pack.make_dn(i, wh, 30, posting_date=str(today))
			sett_i = _settle(i, today.year, today.month)
			check("I: the current month shares it by the consumption since the switch, 40 / 90",
				abs(flt(sett_i.rev_es) - (-900 * 40 / 90)) < 0.01, f"es {sett_i.rev_es} cons {sett_i.rev_cons}")
		else:
			print("SKIP I: the previous month is in the prior fiscal year")

		# ---- J: the migration patch --------------------------------------
		from periodic_valuation.patches.v1_0 import switch_cost_changes_at_release

		frappe.db.sql("""update `tabPeriodic Standard Cost Settings` set revaluation_posting_date = 'Last day of the period'
			where company = %s""", pack.COMPANY)
		switch_cost_changes_at_release.execute()
		check("J: the patch moves the setting to Date of release",
			frappe.db.get_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
				"revaluation_posting_date") == "Date of release")
	finally:
		frappe.db.rollback(save_point="std_reval_release")
