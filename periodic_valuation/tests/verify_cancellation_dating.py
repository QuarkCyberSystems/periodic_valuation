"""A cancellation posts on its own date, never before the original, and
shows negative totals (client ticket STD-014, 08/10/2026). Run:
bench --site <site> execute periodic_valuation.tests.verify_cancellation_dating.run

  1  a Purchase Receipt cancellation is the native return: negative
     quantity and grand total
  2  dated before the receipt it cancels: refused
  3  Moving Average: dated after it, the reversal posts on the
     cancellation's date and the stock goes back to what it was
  4  a Delivery Note cancellation is negative too and puts the stock back
  5  Standard Cost: a receipt in the previous open month cancelled in the
     current month reverses on the cancellation's date (not the receipt's),
     at the receipt's own standard cost, companion included
  6  Standard Cost: dated before the receipt: refused

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack
from periodic_valuation.tests.smoke_edges import make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company
from periodic_valuation.tests.smoke_std import ensure_std_masters

CHECKS = []


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
		raise Exception("Cancellation dating failures: " + "; ".join(c[0] for c in failed))


def _cancellation(doc, posting_date, posting_time="23:00:00"):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cxl = frappe.get_doc(doc.doctype, make_cancellation(doc.doctype, doc.name))
	cxl.posting_date = str(posting_date)
	cxl.posting_time = posting_time
	return cxl


def _submit_on(doc, posting_date):
	doc.posting_date = str(posting_date)
	doc.save()
	doc.submit()


def _refused(fn):
	frappe.db.savepoint("cxl_refusal")
	try:
		fn()
		return ""
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="cxl_refusal")


def _event_dates(doc):
	return sorted({str(d) for d in frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": doc.name, "is_cancelled": 0}, pluck="posting_date")})


def _gl_dates(doc):
	return sorted({str(d) for d in frappe.get_all("GL Entry",
		filters={"voucher_no": doc.name, "is_cancelled": 0}, pluck="posting_date")})


def _closing(company, item, d):
	d = getdate(d)
	row = frappe.get_all("Inventory Period Balance", filters={"company": company, "item_code": item,
		"period_year": d.year, "period_month": d.month}, fields=["closing_qty", "closing_value"])
	return (flt(row[0].closing_qty), flt(row[0].closing_value, 2)) if row else (0.0, 0.0)


def _run():
	today = getdate(nowdate())
	if today.day < 3:
		print("SKIP: needs two days of the current month before today")
		return
	day1 = get_first_day(today)
	o2 = add_days(day1, 1)
	prev = add_months(day1, -1)
	p2 = add_days(prev, 1)
	frappe.db.savepoint("cxl_dating")
	try:
		wh = ensure_masters()
		company = get_company()
		ensure_std_masters(company)
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		for when, status in ((prev, "PREV_OPEN_UNSETTLED"), (day1, "OPEN")):
			name = frappe.db.get_value("Inventory Period",
				{"company": company, "period_year": when.year, "period_month": when.month})
			if not name:
				d = frappe.get_doc({"doctype": "Inventory Period", "company": company, "start_date": str(when)})
				d.flags.ignore_validate = True
				name = d.insert(ignore_permissions=True).name
			frappe.db.set_value("Inventory Period", name, "status", status, update_modified=False)

		# ---- Moving Average -------------------------------------------------
		m = make_item("_CXL-DATE-MAP")
		make_pr(m, wh, 50, 100, posting_date=str(day1))
		before = _closing(company, m, today)
		pr = make_pr(m, wh, 10, 120, posting_date=str(o2))
		cxl = _cancellation(pr, today)
		check("1: a receipt cancellation is a return with negative quantity and grand total",
			cxl.is_return == 1 and flt(cxl.items[0].qty) < 0 and flt(cxl.grand_total) < 0,
			f"is_return {cxl.is_return} qty {cxl.items[0].qty} total {cxl.grand_total}")
		check("2: a cancellation dated before the receipt is refused",
			_refused(lambda: _submit_on(cxl, day1)))
		cxl.reload()
		cxl.posting_date, cxl.posting_time = str(today), "23:00:00"
		cxl.save()
		cxl.submit()
		check("3: MAP - the reversal posts on the cancellation's date",
			_event_dates(cxl) == [str(today)] and _gl_dates(cxl) == [str(today)],
			f"{_event_dates(cxl)} / {_gl_dates(cxl)}")
		check("3: MAP - the stock is back to what it was before the receipt",
			_closing(company, m, today) == before, f"{_closing(company, m, today)} vs {before}")

		dn = make_dn(m, wh, 5, posting_date=str(o2))
		dcx = _cancellation(dn, today)
		check("4: a delivery cancellation is negative too",
			dcx.is_return == 1 and flt(dcx.items[0].qty) < 0 and flt(dcx.grand_total) < 0,
			f"qty {dcx.items[0].qty} total {dcx.grand_total}")
		dcx.save()
		dcx.submit()
		check("4: the delivery cancellation puts the stock back on its own date",
			_closing(company, m, today) == before and _event_dates(dcx) == [str(today)],
			f"{_closing(company, m, today)} vs {before}; {_event_dates(dcx)}")

		# ---- Standard Cost --------------------------------------------------
		s = pack.std_item("_CXL-DATE-STD")
		for when, sc in ((prev, 100), (day1, 110)):
			v = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company, "item_code": s,
				"valid_from_year": when.year, "valid_from_month": when.month, "standard_cost": sc,
				"source_type": "MANUAL_OVERRIDE"}).insert(ignore_permissions=True)
			v.release()
		spr = make_pr(s, wh, 20, 120, posting_date=str(p2))
		scx = _cancellation(spr, today)
		check("6: STD - a cancellation dated before the receipt is refused",
			_refused(lambda: _submit_on(scx, prev)))
		scx.reload()
		scx.posting_date, scx.posting_time = str(today), "23:00:00"
		scx.save()
		scx.submit()
		mirror = frappe.get_all("Inventory Valuation Event", filters={"source_docname": scx.name, "is_cancelled": 0},
			fields=["std_trans", "total_sc", "posting_date", "reversal_of"])
		check("5: STD - every reversal row is dated the cancellation's date, the receipt's companion included",
			len(mirror) == 2 and all(str(x.posting_date) == str(today) and x.reversal_of for x in mirror)
			and _gl_dates(scx) == [str(today)], str(mirror))
		check("5: STD - reversed at the receipt's own standard cost (20 x 100), the companion's 200 with it",
			sorted(flt(x.total_sc) for x in mirror) == [-2000.0, -200.0], str(mirror))
		net = flt(frappe.db.sql("""select coalesce(sum(total_sc), 0) from `tabInventory Valuation Event`
			where item_code = %s and is_cancelled = 0 and std_trans like %s""", (s, "REC%"))[0][0], 2)
		check("5: STD - nothing is left of the receipt (its rows and their reversals net to zero)",
			net == 0, str(net))
	finally:
		frappe.db.rollback(save_point="cxl_dating")
