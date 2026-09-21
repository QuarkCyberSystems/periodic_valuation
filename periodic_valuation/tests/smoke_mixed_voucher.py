"""Interim mixed-voucher refusal (D-030 term 2) -
bench --site <site> execute periodic_valuation.tests.smoke_mixed_voucher.run

Proves the four shapes the 2026-09-17 probes showed cannot be reversed are
refused at validate, that routed-only and core-only vouchers are untouched,
that a project dimension alone is not "mixed", and that the fixture flag
builds the refused shape. Rolled back.
"""

import frappe
from frappe.utils import nowdate

from periodic_valuation.tests.smoke_kernel import ITEM as MAP_ITEM, check, ensure_masters, get_company

TAG = "_SMK-MIX"


def _company():
	return get_company()


def _item(code, **kw):
	if not frappe.db.exists("Item", code):
		doc = {
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos", "is_stock_item": 1, "valuation_method": "FIFO",
		}
		doc.update(kw)
		frappe.get_doc(doc).insert(ignore_permissions=True)
	return code


def _fifo_item():
	return _item(f"{TAG}-FIFO")


def _service_item():
	return _item(f"{TAG}-SVC", is_stock_item=0)


def _asset_item():
	code = f"{TAG}-FA"
	if not frappe.db.exists("Item", code):
		cat = f"{TAG} Category"
		if not frappe.db.exists("Asset Category", cat):
			co = _company()
			acc = lambda t: frappe.db.get_value("Account", {"company": co, "account_type": t, "is_group": 0}, "name")
			frappe.get_doc({
				"doctype": "Asset Category", "asset_category_name": cat,
				"accounts": [{
					"company_name": co, "fixed_asset_account": acc("Fixed Asset"),
					"accumulated_depreciation_account": acc("Accumulated Depreciation"),
					"depreciation_expense_account": acc("Depreciation")
					or frappe.db.get_value("Account", {"company": co, "root_type": "Expense", "is_group": 0}, "name"),
				}],
			}).insert(ignore_permissions=True)
		_item(code, is_stock_item=0, is_fixed_asset=1, asset_category=cat, auto_create_assets=0)
	return code


def _wh():
	abbr = frappe.db.get_value("Company", _company(), "abbr")
	return f"_SMK Stores - {abbr}"


def _pr(rows):
	pr = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": _company(), "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1, "items": rows,
	})
	pr.set_missing_values()
	return pr


def _row(item, qty=1, rate=10, **kw):
	r = {"item_code": item, "qty": qty, "rate": rate, "warehouse": _wh()}
	r.update(kw)
	return r


def _refused(label, doc):
	try:
		doc.insert(ignore_permissions=True)
	except frappe.ValidationError as e:
		ok = "Mixed Voucher" in (getattr(e, "title", "") or "") or "mixes periodic-valuation items" in str(e)
		check(label, ok, str(e)[:120])
		return
	check(label, False, "inserted without refusal")


def _allowed(label, doc):
	try:
		doc.insert(ignore_permissions=True)
		check(label, True)
	except Exception as e:  # noqa
		check(label, False, str(e)[:160])


def run():
	frappe.set_user("Administrator")
	ensure_masters()
	fifo, svc, fa = _fifo_item(), _service_item(), _asset_item()
	loc = frappe.db.get_value("Location", {}, "name") or frappe.get_doc(
		{"doctype": "Location", "location_name": f"{TAG} Location"}).insert(ignore_permissions=True).name
	co = _company()

	# --- refused shapes (probes (d), (a), (b))
	_refused("PR routed + FIFO row refused (probe d)", _pr([_row(MAP_ITEM, 10, 5), _row(fifo, 5, 8)]))
	_refused("PR routed + fixed-asset row refused (probe a)",
		_pr([_row(MAP_ITEM, 10, 5), _row(fa, 1, 1000, asset_location=loc)]))
	dn = frappe.get_doc({
		"doctype": "Delivery Note", "company": co, "customer": "_SMK Customer",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [_row(MAP_ITEM, 1, 20), _row(fifo, 1, 20)],
	})
	dn.set_missing_values()
	_refused("DN routed + FIFO row refused", dn)
	se = frappe.get_doc({
		"doctype": "Stock Entry", "company": co, "stock_entry_type": "Material Receipt",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [
			{"item_code": MAP_ITEM, "qty": 1, "basic_rate": 5, "t_warehouse": _wh()},
			{"item_code": fifo, "qty": 1, "basic_rate": 8, "t_warehouse": _wh()},
		],
	})
	_refused("SE routed + FIFO row refused", se)
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": co, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1, "bill_no": f"{TAG}-1", "bill_date": nowdate(),
		"items": [_row(MAP_ITEM, 10, 5), _row(fa, 1, 1000, asset_location=loc)],
	})
	pi.set_missing_values()
	_refused("PI routed + fixed-asset row refused (probe b)", pi)

	# --- provisional accounting: a service row becomes core-posted
	prov_acc_before = frappe.db.get_value("Company", co, "default_provisional_account")
	frappe.db.set_value("Company", co, {
		"enable_provisional_accounting_for_non_stock_items": 1,
		"default_provisional_account": prov_acc_before or frappe.db.get_value(
			"Account", {"company": co, "root_type": "Liability", "is_group": 0, "account_type": ("in", ("", None))}, "name"),
	}, update_modified=False)
	frappe.clear_cache(doctype="Company")
	_refused("PR routed + service row refused when provisional accounting is on",
		_pr([_row(MAP_ITEM, 10, 5), _row(svc, 1, 50)]))
	frappe.db.set_value("Company", co, {
		"enable_provisional_accounting_for_non_stock_items": 0, "default_provisional_account": prov_acc_before,
	}, update_modified=False)
	frappe.clear_cache(doctype="Company")
	_allowed("PR routed + service row allowed when provisional accounting is off",
		_pr([_row(MAP_ITEM, 10, 5), _row(svc, 1, 50)]))

	# --- other owners answer through the hook
	if "project_accounting" in frappe.get_installed_apps():
		project = frappe.db.get_value("Project Accounting", {"company": co, "project_type": "Capex"}, "name")
		if project:
			_refused("PR routed + AuC-intent row refused (probe a, PA answers)",
				_pr([_row(MAP_ITEM, 10, 5), _row(svc, 1, 700,
					asset_purchase_intent="Standalone Asset Under Construction", project_accounting=project)]))
		else:
			check("PR routed + AuC-intent row (no Capex project on site) - skipped", True)
	else:
		check("PR routed + AuC-intent row (project_accounting not installed) - skipped", True)

	# --- untouched shapes
	_allowed("PR routed-only allowed", _pr([_row(MAP_ITEM, 10, 5)]))
	_allowed("PR FIFO-only allowed", _pr([_row(fifo, 5, 8)]))
	_allowed("PR FIFO + fixed-asset (no routed row) allowed",
		_pr([_row(fifo, 5, 8), _row(fa, 1, 1000, asset_location=loc)]))
	if frappe.get_meta("Purchase Receipt Item").has_field("project_accounting"):
		# a project may enforce its own cost centre / warehouse (PA policy) -
		# pick one that does not, so the only rule under test is the mixed-voucher one
		project = frappe.db.get_value(
			"Project Accounting",
			{"company": co, "enforce_project_cost_center": 0, "enforce_project_warehouse": 0},
			"name",
		)
		if project:
			_allowed("PR routed + project dimension (no own posting) allowed",
				_pr([_row(MAP_ITEM, 10, 5, project_accounting=project)]))
		else:
			check("PR routed + project dimension (no unenforced project on site) - skipped", True)
	si = frappe.get_doc({
		"doctype": "Sales Invoice", "company": co, "customer": "_SMK Customer",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [_row(MAP_ITEM, 1, 20), _row(fifo, 1, 20)],
	})
	si.set_missing_values()
	_allowed("SI routed + FIFO row allowed (return-reversed: core rows reverse natively)", si)

	# --- the fixture flag builds the refused shape; reversing it is then refused
	# with the "predates this rule" text, not "raise a separate voucher"
	frappe.flags.qcs_allow_mixed_voucher = True
	try:
		mixed = _pr([_row(MAP_ITEM, 10, 5), _row(fifo, 5, 8)])
		_allowed("flag qcs_allow_mixed_voucher builds routed + FIFO PR", mixed)
		mixed.submit()
	finally:
		frappe.flags.qcs_allow_mixed_voucher = False
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	try:
		make_cancellation("Purchase Receipt", mixed.name)
		check("Create Cancellation of a pre-rule mixed PR refused", False, "cancellation created")
	except frappe.ValidationError as e:
		check("Create Cancellation of a pre-rule mixed PR refused with the reversal text",
			"predates this rule" in str(e) and "separate" not in str(e), str(e)[:160])

	from periodic_valuation.tests import smoke_kernel

	failed = [c for c in smoke_kernel.CHECKS if not c[1]]
	print(f"\nmixed-voucher smoke: {len(smoke_kernel.CHECKS) - len(failed)}/{len(smoke_kernel.CHECKS)} passed")
	# the company flag was restored in-transaction above; nothing here persists
	frappe.db.rollback()
	return not failed
