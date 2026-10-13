"""A cancellation ends at the standard costs in force now, as a fresh posting
would (client ticket STD-017, 12/10/2026; DR-66). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_cancel_corrected_month.run

Previous month P (PREV_OPEN_UNSETTLED), current month C (OPEN). The
client's case: P 3,000 (C inherits 3,000); receipt 100 and issue 50 in P;
P corrected to 3,500; C changed to 5,000. Under each Revaluation Posting
Date option:

  1  the issue cancelled, dated in P: P ends 100 x 3,500, C 100 x 5,000;
     the stock GL equals C's closing
  2  a receipt cancelled, dated in P: P and C end at their standards
  3  the issue cancelled, dated in C: P stays 50 x 3,500, C 100 x 5,000;
     COGS Adjustment gives back the correction's 25,000 and the rest is
     revaluation
  4  a backdated original whose companion bridged it into C at the time:
     the companion's reversal and the new bridge are not counted twice
  5  STD-015: a balance row that appears after the period opened shows what
     earlier months received since then as Carryover

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

CHECKS = []
MODES = (("First", "First day of the period"), ("Latest", "Latest day of the period"))


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("STD cancel-in-corrected-month failures: " + "; ".join(c[0] for c in failed))


def _bal(item, d):
	d = getdate(d)
	r = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": item,
		"period_year": d.year, "period_month": d.month},
		fields=["opening_qty", "carryover_qty", "closing_qty", "closing_value"])
	return (flt(r[0].closing_qty), flt(r[0].closing_value, 2)) if r else (0.0, 0.0)


def _stock_gl(item, account):
	return flt(frappe.db.sql("""select coalesce(sum(g.debit - g.credit), 0) from `tabGL Entry` g
		join `tabInventory Valuation Event` e on e.name = g.valuation_event_id
		where e.item_code = %s and g.account = %s and g.is_cancelled = 0""", (item, account))[0][0], 2)


def _doc_gl(name, account):
	return flt(frappe.db.sql("""select coalesce(sum(debit - credit), 0) from `tabGL Entry`
		where voucher_no = %s and account = %s and is_cancelled = 0""", (name, account))[0][0], 2)


def _issue(item, wh, qty, date):
	se = frappe.get_doc({"doctype": "Stock Entry", "company": pack.COMPANY, "stock_entry_type": "Material Issue",
		"purpose": "Material Issue", "posting_date": str(date), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "s_warehouse": wh}]})
	se.insert(ignore_permissions=True)
	se.submit()
	return se


def _cancel(doc, date):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cx = frappe.get_doc(doc.doctype, make_cancellation(doc.doctype, doc.name))
	cx.posting_date, cx.posting_time = str(date), "23:00:00"
	cx.save(ignore_permissions=True)
	cx.submit()
	return cx


def _run():
	today = getdate(nowdate())
	if today.day < 2:
		print("SKIP: needs a day of the current month before today")
		return
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	p2, p3, p5 = add_days(prev, 1), add_days(prev, 2), add_days(prev, 4)
	frappe.db.savepoint("std_cancel_corr")
	try:
		wh, _ = pack.ensure_company()
		pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		pack.make_period(today.year, today.month, "OPEN")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		a = pack.acct()

		def client_case(code):
			it = pack.std_item(code)
			pack.scv_release(it, prev.year, prev.month, 3000)
			rec = pack.make_pr(it, wh, 100, 3000, posting_date=str(p2))
			iss = _issue(it, wh, 50, p3)
			pack.scv_release(it, prev.year, prev.month, 3500)
			pack.scv_release(it, today.year, today.month, 5000)
			return it, rec, iss

		for tag, mode in MODES:
			frappe.db.set_value("Periodic Standard Cost Settings", {"company": pack.COMPANY},
				"revaluation_posting_date", mode)

			it, _rec, iss = client_case(f"_STD-CXC-ISS-{tag}")
			_cancel(iss, p5)
			check(f"{tag} 1: issue cancelled in P - P 100 x 3,500, C 100 x 5,000, stock GL = C",
				_bal(it, prev) == (100.0, 350000.0) and _bal(it, today) == (100.0, 500000.0)
				and _stock_gl(it, a.stock) == 500000.0,
				f"P {_bal(it, prev)} C {_bal(it, today)} GL {_stock_gl(it, a.stock)}")

			it, _rec, _iss = client_case(f"_STD-CXC-REC-{tag}")
			extra = pack.make_pr(it, wh, 30, 3000, posting_date=str(p3))   # before the correction
			frappe.db.sql("select 1")
			_cancel(extra, p5)
			check(f"{tag} 2: receipt cancelled in P - P 50 x 3,500, C 50 x 5,000, stock GL = C",
				_bal(it, prev) == (50.0, 175000.0) and _bal(it, today) == (50.0, 250000.0)
				and _stock_gl(it, a.stock) == 250000.0,
				f"P {_bal(it, prev)} C {_bal(it, today)} GL {_stock_gl(it, a.stock)}")

			it, _rec, iss = client_case(f"_STD-CXC-CUR-{tag}")
			cx = _cancel(iss, today)
			check(f"{tag} 3: issue cancelled in C - P stays 50 x 3,500, C 100 x 5,000; COGS Adjustment gives back 25,000",
				_bal(it, prev) == (50.0, 175000.0) and _bal(it, today) == (100.0, 500000.0)
				and _stock_gl(it, a.stock) == 500000.0 and _doc_gl(cx.name, a.cogs_adj) == -25000.0,
				f"P {_bal(it, prev)} C {_bal(it, today)} GL {_stock_gl(it, a.stock)} cogs_adj {_doc_gl(cx.name, a.cogs_adj)}")

			# 4: the companion of a backdated original carried value at the time
			it = pack.std_item(f"_STD-CXC-BRIDGE-{tag}")
			pack.scv_release(it, prev.year, prev.month, 3000)
			pack.make_pr(it, wh, 100, 3000, posting_date=str(p2))
			pack.scv_release(it, today.year, today.month, 3500)
			iss = _issue(it, wh, 50, p3)                                    # companion -50 x 500 in C
			_cancel(iss, p5)
			check(f"{tag} 4: backdated original's companion and the new bridge are not counted twice",
				_bal(it, prev) == (100.0, 300000.0) and _bal(it, today) == (100.0, 350000.0)
				and _stock_gl(it, a.stock) == 350000.0,
				f"P {_bal(it, prev)} C {_bal(it, today)} GL {_stock_gl(it, a.stock)}")

		# ---- 5: STD-015 opening at period open ------------------------------------
		frappe.db.sql("UPDATE `tabInventory Period` SET creation = %s WHERE company = %s AND period_year = %s "
			"AND period_month = %s", (f"{day1} 00:00:01", pack.COMPANY, today.year, today.month))
		it = pack.std_item("_STD-CXC-OPEN")
		pack.scv_release(it, prev.year, prev.month, 200)
		pack.make_pr(it, wh, 500, 250, posting_date=str(p2))       # previous month, after C opened
		pack.make_pr(it, wh, 700, 225, posting_date=str(day1))     # creates C's row
		r = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": it,
			"period_year": today.year, "period_month": today.month},
			fields=["opening_qty", "carryover_qty", "closing_qty"])[0]
		check("5: a row created after the period opened - Opening 0, Carryover 500, Closing 1,200",
			(flt(r.opening_qty), flt(r.carryover_qty), flt(r.closing_qty)) == (0.0, 500.0, 1200.0), str(r))
	finally:
		frappe.db.rollback(save_point="std_cancel_corr")
