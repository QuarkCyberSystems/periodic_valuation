# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""The basis correction owner (GA-0009-01 GAP-054, basis_correction.py):
the platform hands this app the economic amount D of a non-monetary
advance's basis correction and a clearing account.

- Periodic Moving Average: D = -111 on 3 of 10 units on hand moves the
  period balance by -111 (fully covered) and the clearing by +111; the
  reversal mirrors the event and restores both.
- Periodic Standard Cost: D enters the variance pool through the LC route,
  inventory stays at standard; the reversal mirrors it.
- Stock this app does not value, or a non-stock destination, is not its.

Rolled back. Run on a throwaway site:
    bench --site <site> execute periodic_valuation.tests.verify_basis_correction.run
"""

import frappe
from frappe.utils import flt, getdate, nowdate

RESULTS = []


def check(label, ok, detail=""):
	RESULTS.append((label, bool(ok), detail))
	print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" - {detail}" if detail and not ok else ""))


def _reconciliation(company, reversal_of=None):
	name = frappe.get_doc({"doctype": "ToDo", "description": "basis correction probe"}).insert(ignore_permissions=True).name
	return frappe._dict(doctype="ToDo", name=name, company=company, posting_date=nowdate(), origin_slice="PINV-PROBE:row",
		source_qty=3, reversal_of=reversal_of)


def _closing(company, item):
	row = frappe.get_all("Inventory Period Balance", filters={"company": company, "item_code": item},
		fields=["closing_value"], order_by="period_year desc, period_month desc", limit=1)
	return flt(row[0].closing_value) if row else 0.0


def _gl(account, voucher):
	return flt(frappe.db.sql("""select coalesce(sum(credit - debit), 0) from `tabGL Entry` where account = %s and voucher_no = %s
		and is_cancelled = 0""", (account, voucher))[0][0])


def _checks():
	from periodic_valuation.basis_correction import basis_correction
	from periodic_valuation.tests import smoke_kernel, smoke_std

	company = smoke_kernel.get_company()
	warehouse = smoke_kernel.ensure_masters()
	clearing = frappe.get_all("Account", filters={"company": company, "is_group": 0, "root_type": "Liability", "account_type": ""},
		limit=1, pluck="name")[0]

	check("a non-stock destination is not this app's", basis_correction("post", _reconciliation(company), {"kind": "Asset"}, -1, clearing)
		is None)

	# ---- Periodic Moving Average
	item = smoke_kernel.ITEM
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": company, "supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": item, "qty": 10, "rate": 100, "warehouse": warehouse}]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	destination = {"kind": "Stock", "item_code": item, "warehouse": warehouse, "stock_qty": 3, "principal": 3}
	before = _closing(company, item)
	pre = _reconciliation(company)
	made = basis_correction("post", pre, destination, -111, clearing)
	event = frappe.db.get_value("Inventory Valuation Event", made[1], ["value_delta", "reason_code", "source_doctype"], as_dict=True)
	check("MAP: the correction is a value-only invoice_diff event of -111 on the reconciliation",
		made[0] == "Inventory Valuation Event" and (flt(event.value_delta), event.reason_code, event.source_doctype) == (-111, "invoice_diff",
		"ToDo"), str(event))
	check("MAP: on hand covers it - the period balance moves by -111", abs(_closing(company, item) - (before - 111)) < 0.01,
		f"{before} -> {_closing(company, item)}")
	check("MAP: the owner's rows debit the clearing 111 - what the reconciliation credited", abs(_gl(clearing, pre.name) + 111) < 0.01,
		str(_gl(clearing, pre.name)))
	reversal = _reconciliation(company, reversal_of=pre.name)
	undone = basis_correction("reverse", reversal, destination, -111, clearing)
	check("MAP: the reversal mirrors the event and restores the balance",
		bool(undone and undone[1]) and abs(_closing(company, item) - before) < 0.01
		and frappe.db.get_value("Inventory Valuation Event", undone[1], "reversal_of") == made[1],
		f"{undone} closing {_closing(company, item)} want {before}")

	# ---- Periodic Standard Cost
	smoke_std.ensure_std_masters(company)
	today = getdate(nowdate())
	if not frappe.db.exists("Item Standard Cost Version", {"company": company, "item_code": smoke_std.ITEM, "status": "RELEASED",
			"valid_from_year": today.year, "valid_from_month": today.month}):
		scv = smoke_std.make_scv(company, today.year, today.month, 10)
		scv.flags.via_release_flow = True
		scv.status = "RELEASED"
		scv.save(ignore_permissions=True)
	std_destination = {"kind": "Stock", "item_code": smoke_std.ITEM, "warehouse": warehouse, "stock_qty": 3, "principal": 3}
	std_pre = _reconciliation(company)
	std_made = basis_correction("post", std_pre, std_destination, -50, clearing)
	std_event = frappe.db.get_value("Inventory Valuation Event", std_made[1], ["std_trans", "source_doctype"], as_dict=True)
	check("STD: the correction enters the variance pool by the LC route", std_event and std_event.std_trans == "LC"
		and std_event.source_doctype == "ToDo", str(std_event))
	std_undone = basis_correction("reverse", _reconciliation(company, reversal_of=std_pre.name), std_destination, -50, clearing)
	check("STD: the reversal mirrors it", bool(std_undone and std_undone[1])
		and frappe.db.get_value("Inventory Valuation Event", std_undone[1], "reversal_of") == std_made[1], str(std_undone))


def run():
	RESULTS.clear()
	frappe.set_user("Administrator")
	frappe.db.savepoint("pv_basis_correction")
	try:
		_checks()
	except Exception as exc:
		import traceback

		check("unexpected error", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")
	finally:
		frappe.db.rollback(save_point="pv_basis_correction")
	failed = [r for r in RESULTS if not r[1]]
	print(f"\nbasis correction owner: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
	if failed:
		raise AssertionError("; ".join(r[0] for r in failed))
	return True
