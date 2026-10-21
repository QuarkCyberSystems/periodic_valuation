"""Mixed vouchers (D-030 term 2, as narrowed by D-095), raised by
qcs_platform's dispatcher from each owner's `posts_rows` answer -
bench --site <site> execute periodic_valuation.tests.smoke_mixed_voucher.run

A Purchase Receipt or Delivery Note may carry routed rows beside rows
another owner posts for itself: its Cancellation is ERPNext's own return
document (STD-014), on which every owner reverses its own rows - core its
FIFO, service and fixed-asset rows, project accounting its AuC row as a
negative row of its own, asset_enterprise the assets the receipt created
(the platform's `receipt_reversal_assets` socket, at the Cancellation's
submit). Proved here end to end: each pair nets to zero in the GL and the
stock ledger. Still refused at validate: a Stock Entry and a receipt that
is itself a return (their Cancellation is a copy - core would post its rows
again) and an invoice with a fixed-asset row (the debit note copies
asset_enterprise's allocation, probe (b)).
Rolled back.
"""

import frappe
from frappe.utils import flt, nowdate

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
		ok = "Mixed Voucher" in (getattr(e, "title", "") or "") or "mixes rows" in str(e)
		check(label, ok, str(e)[:120])
		return
	check(label, False, "inserted without refusal")


def _allowed(label, doc):
	try:
		doc.insert(ignore_permissions=True)
		check(label, True)
	except Exception as e:  # noqa
		check(label, False, str(e)[:160])


def _net(names):
	gl = frappe.db.sql(
		"""select account, sum(debit - credit) v from `tabGL Entry` where voucher_no in %s and is_cancelled = 0
		group by account having abs(sum(debit - credit)) > 0.005""", (tuple(names),), as_dict=True)
	sle = frappe.db.sql(
		"""select item_code, sum(actual_qty) q, sum(stock_value_difference) v from `tabStock Ledger Entry`
		where voucher_no in %s and is_cancelled = 0 group by item_code
		having abs(sum(actual_qty)) > 0.0001 or abs(sum(stock_value_difference)) > 0.005""", (tuple(names),), as_dict=True)
	return gl, sle


def _reverses(label, doc, after=None):
	"""Admitted at save; Create Cancellation is the return; it submits and the
	pair nets to zero in the GL and the stock ledger."""
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	frappe.db.savepoint("mix_rev")
	try:
		doc.insert(ignore_permissions=True)
		doc.submit()
		cancellation = frappe.get_doc(doc.doctype, make_cancellation(doc.doctype, doc.name))
		cancellation.submit()
		gl, sle = _net([doc.name, cancellation.name])
		check(f"{label}: admitted, reversed by its Cancellation (a return), every owner nets to zero",
			cancellation.is_return and not gl and not sle, f"gl {gl} sle {sle}")
		if after:
			after(doc, cancellation)
	except Exception as e:  # noqa
		check(f"{label}: admitted and reversed", False, str(e)[:200])
	finally:
		frappe.db.rollback(save_point="mix_rev")


def _in_stock(*items):
	for item in items:
		pr = _pr([_row(item, 20, 7)])
		pr.insert(ignore_permissions=True)
		pr.submit()


def run():
	frappe.set_user("Administrator")
	ensure_masters()
	fifo, svc, fa = _fifo_item(), _service_item(), _asset_item()
	loc = frappe.db.get_value("Location", {}, "name") or frappe.get_doc(
		{"doctype": "Location", "location_name": f"{TAG} Location"}).insert(ignore_permissions=True).name
	co = _company()

	# --- still refused: the copy-reversed Stock Entry, the invoice with an asset row
	se = frappe.get_doc({
		"doctype": "Stock Entry", "company": co, "stock_entry_type": "Material Receipt",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [
			{"item_code": MAP_ITEM, "qty": 1, "basic_rate": 5, "t_warehouse": _wh()},
			{"item_code": fifo, "qty": 1, "basic_rate": 8, "t_warehouse": _wh()},
		],
	})
	_refused("SE routed + FIFO row refused (its Cancellation is a copy)", se)
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": co, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1, "bill_no": f"{TAG}-1", "bill_date": nowdate(),
		"items": [_row(MAP_ITEM, 10, 5), _row(fa, 1, 1000, asset_location=loc)],
	})
	pi.set_missing_values()
	_refused("PI routed + fixed-asset row refused (probe b)", pi)

	# --- admitted and reversed per owner (D-095)
	_reverses("PR routed + FIFO row (probe d)", _pr([_row(MAP_ITEM, 10, 5), _row(fifo, 5, 8)]))
	_reverses("PR routed + fixed-asset row, no asset created",
		_pr([_row(MAP_ITEM, 10, 5), _row(fa, 1, 1000, asset_location=loc)]))
	prov_acc_before = frappe.db.get_value("Company", co, "default_provisional_account")
	frappe.db.set_value("Company", co, {
		"enable_provisional_accounting_for_non_stock_items": 1,
		"default_provisional_account": prov_acc_before or frappe.db.get_value(
			"Account", {"company": co, "root_type": "Liability", "is_group": 0, "account_type": ("in", ("", None))}, "name"),
	}, update_modified=False)
	frappe.clear_cache(doctype="Company")
	_reverses("PR routed + service row under provisional accounting",
		_pr([_row(MAP_ITEM, 10, 5), _row(svc, 1, 50)]))
	frappe.db.set_value("Company", co, {
		"enable_provisional_accounting_for_non_stock_items": 0, "default_provisional_account": prov_acc_before,
	}, update_modified=False)
	frappe.clear_cache(doctype="Company")

	frappe.db.savepoint("mix_dn")
	_in_stock(MAP_ITEM, fifo)
	dn = frappe.get_doc({
		"doctype": "Delivery Note", "company": co, "customer": "_SMK Customer",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [_row(MAP_ITEM, 1, 20), _row(fifo, 1, 20)],
	})
	dn.set_missing_values()
	_reverses("DN routed + FIFO row", dn)
	frappe.db.rollback(save_point="mix_dn")

	installed = frappe.get_installed_apps()
	if "asset_enterprise" in installed:
		from asset_enterprise.setup import verify_tc as tc

		cat = tc._category(co, "TC IT Equipment", suspense=tc._plain(co, "Liability"))
		asset_item = tc._item(cat, "TC-IT-ITEM")

		def assets_reversed(doc, cancellation):
			assets = frappe.get_all("Asset", filters={"purchase_receipt": doc.name}, fields=["name", "docstatus"])
			check("PR routed + auto-created asset: the Cancellation's submit reversed the receipt's asset",
				bool(assets) and all(a.docstatus == 2 for a in assets), str(assets))

		_reverses("PR routed + auto-created asset",
			_pr([_row(MAP_ITEM, 10, 5), _row(asset_item, 1, 5000, asset_location=tc._location())]), assets_reversed)

		# an asset row is reversed whole: a Cancellation edited to give back
		# part of it is refused, and the receipt's assets stand
		from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

		frappe.db.savepoint("mix_part")
		try:
			pr = _pr([_row(MAP_ITEM, 10, 5), _row(asset_item, 2, 5000, asset_location=tc._location())])
			pr.insert(ignore_permissions=True)
			pr.submit()
			part = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr.name))
			for row in part.items:
				if row.item_code == asset_item:
					row.qty = row.received_qty = -1
			part.save(ignore_permissions=True)
			try:
				part.submit()
				check("PR routed + asset row: a partial reversal of the asset row is refused", False, "submitted")
			except frappe.ValidationError as e:
				standing = frappe.db.count("Asset", {"purchase_receipt": pr.name, "docstatus": 1})
				check("PR routed + asset row: a partial reversal of the asset row is refused, the assets stand",
					"reversal must carry the whole row" in str(e) and standing == 2, f"{standing} standing; {str(e)[:120]}")
		except Exception as e:  # noqa
			check("PR routed + asset row: partial reversal case ran", False, str(e)[:200])
		finally:
			frappe.db.rollback(save_point="mix_part")
	else:
		check("PR routed + auto-created asset (asset_enterprise not installed) - skipped", True)

	if "project_accounting" in installed:
		from project_accounting.tests.smoke import phase28_auc_object_routing as phase28

		env28 = phase28._env(co)

		def auc_negative_row(doc, cancellation):
			rows = frappe.get_all("AuC Source Detail", filters={"source_document": ("in", [doc.name, cancellation.name]),
				"docstatus": 1}, fields=["source_document", "amount_added_to_auc", "auc_object", "project_accounting"])
			by = {r.source_document: r for r in rows}
			ok = (doc.name in by and cancellation.name in by
				and flt(by[cancellation.name].amount_added_to_auc) == -flt(by[doc.name].amount_added_to_auc)
				and by[cancellation.name].auc_object == by[doc.name].auc_object
				and by[cancellation.name].project_accounting == by[doc.name].project_accounting)
			check("PR routed + AuC row: project accounting books its own negative row, named by the Cancellation", ok, str(rows))

		_reverses("PR routed + AuC-intent row", frappe.get_doc({
			"doctype": "Purchase Receipt", "company": co, "supplier": env28.supplier,
			"posting_date": nowdate(), "set_posting_time": 1,
			"items": [
				_row(MAP_ITEM, 1, 10),
				{"item_code": env28.item, "qty": 1, "rate": 700, "asset_purchase_intent": "Component of Larger Asset",
					"auc_object": env28.machinery_object, "project_accounting": env28.project, "cost_center": env28.cost_center},
			],
		}), auc_negative_row)
		if "asset_enterprise" in installed:
			def fasd_follows_asset(doc, cancellation):
				assets = frappe.get_all("Asset", filters={"purchase_receipt": doc.name}, fields=["name", "docstatus"])
				fasd = frappe.get_all("Final Asset Source Detail", filters={"source_document": doc.name},
					fields=["name", "docstatus", "final_asset"])
				check("PR routed + Direct Standalone asset: the asset and PA's Final Asset Source Detail are both reversed",
					bool(assets) and all(a.docstatus == 2 for a in assets) and bool(fasd) and all(f.docstatus == 2 for f in fasd),
					f"assets {assets} fasd {fasd}")

			_reverses("PR routed + Direct Standalone asset row", frappe.get_doc({
				"doctype": "Purchase Receipt", "company": co, "supplier": env28.supplier,
				"posting_date": nowdate(), "set_posting_time": 1,
				"items": [
					_row(MAP_ITEM, 1, 10),
					{"item_code": asset_item, "qty": 1, "rate": 5000, "asset_location": tc._location(),
						"asset_purchase_intent": "Direct Standalone Asset – Ready for Use",
						"project_accounting": env28.project, "cost_center": env28.cost_center},
				],
			}), fasd_follows_asset)
	else:
		check("PR routed + AuC-intent row (project_accounting not installed) - skipped", True)

	# --- a receipt that is itself a return has no return of its own: its
	# Cancellation is a copy core would post again on (D-095 review, BLOCK 1).
	# A mixed return is refused at save; one built before the rule cannot be
	# reversed by hand
	from erpnext.controllers.sales_and_purchase_return import make_return_doc
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	frappe.db.savepoint("mix_ret")
	try:
		pr = _pr([_row(MAP_ITEM, 10, 5), _row(fifo, 5, 8)])
		pr.insert(ignore_permissions=True)
		pr.submit()
		ret = make_return_doc("Purchase Receipt", pr.name)
		for row in ret.items:
			row.qty = row.received_qty = -1
		_refused("a mixed return of a mixed receipt is refused at save (its Cancellation would be a copy)", ret)
		frappe.flags.qcs_allow_mixed_voucher = True
		try:
			old = make_return_doc("Purchase Receipt", pr.name)
			for row in old.items:
				row.qty = row.received_qty = -1
			old.insert(ignore_permissions=True)
			old.submit()
		finally:
			frappe.flags.qcs_allow_mixed_voucher = False
		try:
			make_cancellation("Purchase Receipt", old.name)
			check("Create Cancellation of a pre-rule mixed return refused", False, "cancellation created")
		except frappe.ValidationError as e:
			check("Create Cancellation of a pre-rule mixed return refused with the reversal text",
				"predates this rule" in str(e), str(e)[:160])
	except Exception as e:  # noqa
		check("mixed return cases ran", False, str(e)[:200])
	finally:
		frappe.flags.qcs_allow_mixed_voucher = False
		frappe.db.rollback(save_point="mix_ret")

	# --- untouched shapes
	_allowed("PR routed-only allowed", _pr([_row(MAP_ITEM, 10, 5)]))
	_allowed("PR FIFO-only allowed", _pr([_row(fifo, 5, 8)]))
	si = frappe.get_doc({
		"doctype": "Sales Invoice", "company": co, "customer": "_SMK Customer",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [_row(MAP_ITEM, 1, 20), _row(fifo, 1, 20)],
	})
	si.set_missing_values()
	_allowed("SI routed + FIFO row allowed (return-reversed: core rows reverse natively)", si)

	# --- a Stock Entry built under the fixture flag predates the rule; its
	# Cancellation is refused with the reversal text, not "separate documents"
	frappe.flags.qcs_allow_mixed_voucher = True
	try:
		mixed = frappe.get_doc({
			"doctype": "Stock Entry", "company": co, "stock_entry_type": "Material Receipt",
			"posting_date": nowdate(), "set_posting_time": 1,
			"items": [
				{"item_code": MAP_ITEM, "qty": 1, "basic_rate": 5, "t_warehouse": _wh()},
				{"item_code": fifo, "qty": 1, "basic_rate": 8, "t_warehouse": _wh()},
			],
		})
		_allowed("flag qcs_allow_mixed_voucher builds routed + FIFO Stock Entry", mixed)
		mixed.submit()
	finally:
		frappe.flags.qcs_allow_mixed_voucher = False
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	try:
		make_cancellation("Stock Entry", mixed.name)
		check("Create Cancellation of a pre-rule mixed Stock Entry refused", False, "cancellation created")
	except frappe.ValidationError as e:
		check("Create Cancellation of a pre-rule mixed Stock Entry refused with the reversal text",
			"predates this rule" in str(e) and "do not reverse it by hand" in str(e), str(e)[:160])

	from periodic_valuation.tests import smoke_kernel

	failed = [c for c in smoke_kernel.CHECKS if not c[1]]
	print(f"\nmixed-voucher smoke: {len(smoke_kernel.CHECKS) - len(failed)}/{len(smoke_kernel.CHECKS)} passed")
	# the company flag was restored in-transaction above; nothing here persists
	frappe.db.rollback()
	return not failed
