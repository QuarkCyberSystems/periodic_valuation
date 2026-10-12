"""A standard-cost issue raised by an Asset Capitalization or an Asset Repair
credits stock against its row's account, not Cost of Goods Sold
(TSK-2026-00902, Vivek 12/10/2026, the narrow version). Run:
bench --site <site> execute periodic_valuation.tests.verify_std_asset_issue_account.run

  1  a Material Issue linked to an Asset Capitalization: Dr the row's
     account / Cr Stock In Hand, at standard cost; nothing to COGS
  2  the same for an Asset Repair
  3  an ordinary Material Issue still debits COGS
  4  the capitalization issue's cancellation mirrors the row's account, so
     the account nets to zero
  5  moving average reads the same helper (row account wins)
  6  the asset side's reversal returns the materials with a Material
     Receipt: Dr Stock / Cr the row's account, not SRBNB
  7  an ordinary receipt still credits SRBNB

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import flt, getdate, nowdate

from periodic_valuation.tests import uat_std_pack as pack

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
		raise Exception("STD asset issue account failures: " + "; ".join(c[0] for c in failed))


def _gl(voucher):
	out = {}
	for g in frappe.get_all("GL Entry", filters={"voucher_no": voucher, "is_cancelled": 0},
			fields=["account", "debit", "credit"]):
		out[g.account] = flt(out.get(g.account, 0) + flt(g.debit) - flt(g.credit), 2)
	return out


def _issue(item, wh, qty, account, link=None):
	se = frappe.get_doc({"doctype": "Stock Entry", "company": pack.COMPANY, "stock_entry_type": "Material Issue",
		"purpose": "Material Issue", "posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "s_warehouse": wh, "expense_account": account}]})
	if link:
		se.set(link[0], link[1])
		se.flags.ignore_links = True
	se.insert(ignore_permissions=True)
	se.submit()
	return se


def _run():
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	today = getdate(nowdate())
	frappe.db.savepoint("std_asset_issue")
	try:
		wh, _ = pack.ensure_company()
		pack.make_period(today.year, today.month, "OPEN")
		a = pack.acct()
		clearing = pack.make_acc("_Capitalization Clearing", "Asset")
		it = pack.std_item("_STD-ASSET-ISSUE")
		pack.scv_release(it, today.year, today.month, 100)
		pack.make_pr(it, wh, 50, 100)

		cap = _issue(it, wh, 10, clearing, ("asset_capitalization", "ACC-ASC-TEST-0001"))
		check("1: capitalization issue - Dr the row's account / Cr Stock 1,000; nothing to COGS",
			_gl(cap.name) == {clearing: 1000.0, a.stock: -1000.0}, str(_gl(cap.name)))
		rep = _issue(it, wh, 2, clearing, ("asset_repair", "ACC-ASR-TEST-0001"))
		check("2: repair issue - Dr the row's account / Cr Stock 200",
			_gl(rep.name) == {clearing: 200.0, a.stock: -200.0}, str(_gl(rep.name)))
		plain = _issue(it, wh, 3, clearing)
		check("3: an ordinary issue still debits COGS (the row's account is not read)",
			_gl(plain.name) == {a.cogs: 300.0, a.stock: -300.0}, str(_gl(plain.name)))

		# the test's capitalization is a placeholder name; the reversal mirrors
		# the original's GL rows and never reads the link
		frappe.db.set_value("Stock Entry", cap.name, "asset_capitalization", None, update_modified=False)
		cx = frappe.get_doc("Stock Entry", make_cancellation("Stock Entry", cap.name))
		cx.posting_time = "23:59:00"
		cx.save(ignore_permissions=True)
		cx.submit()
		net = flt(_gl(cap.name).get(clearing, 0) + _gl(cx.name).get(clearing, 0), 2)
		check("4: the cancellation mirrors the row's account; the account nets to zero",
			_gl(cx.name) == {clearing: -1000.0, a.stock: 1000.0} and net == 0, str(_gl(cx.name)))

		# the asset side's reversal returns the materials with a Material Receipt
		# whose row carries the clearing account, at the issued cost
		ret = frappe.get_doc({"doctype": "Stock Entry", "company": pack.COMPANY,
			"stock_entry_type": "Material Receipt", "purpose": "Material Receipt",
			"posting_date": nowdate(), "set_posting_time": 1, "asset_capitalization": "ACC-ASC-TEST-0002",
			"items": [{"item_code": it, "qty": 4, "t_warehouse": wh, "basic_rate": 100,
				"expense_account": clearing}]})
		ret.flags.ignore_links = True
		ret.insert(ignore_permissions=True)
		ret.submit()
		check("6: the reversal's Material Receipt - Dr Stock / Cr the row's account 400; nothing to SRBNB or PPV",
			_gl(ret.name) == {a.stock: 400.0, clearing: -400.0}, str(_gl(ret.name)))
		rec = pack.make_pr(it, wh, 1, 100)
		check("7: an ordinary receipt still credits Stock Received But Not Billed",
			_gl(rec.name).get(a.grir) == -100.0, str(_gl(rec.name)))

		from periodic_valuation.shared.accounts import voucher_expense_account

		check("5: one helper - the row's account wins",
			voucher_expense_account(cap, {"voucher_detail_no": cap.items[0].name, "item_code": it,
				"warehouse": wh}) == clearing)
	finally:
		frappe.db.rollback(save_point="std_asset_issue")
