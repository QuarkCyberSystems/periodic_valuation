"""Valuation GL carries the voucher row's attribution (client PA-028) -
bench --site <site> execute periodic_valuation.tests.smoke_gl_attribution.run

Client PA-028: a Material Issue tagged on its rows (header untagged) posted
GL on the company cost centre with no dimensions. Every case here leaves the
header untagged and tags the rows:

G-01 receipt: the stock and received-not-billed legs carry the row's cost
     centre, dimension and row; the event snapshots it
G-02 issue of two rows tagged apart: each row's legs carry its own
G-03 warehouse-scope transfer: the posted legs carry the row's
G-04 landed cost: the legs carry the RECEIPT row's cost centre and
     dimension (not the landed-cost row's), on the voucher's own row
G-05 invoice difference: the legs carry the invoice row's
G-06 cancellation of the issue: each mirror leg carries the original's
     attribution on the cancellation's own row; issue + cancellation net
     to zero per account, cost centre and dimension
G-06b a cancellation with its first row removed: the mirror lands on the
      remaining row, not on the position
G-07 cancellation of the landed cost nets per attribution too
G-08 standard cost: receipt and issue legs carry the row's; the exact
     reversal of the issue nets per attribution
G-09 an event of no voucher row (settlement family) takes the company
     cost centre and no dimension
Rolled back unless commit=True.
"""

import json

import frappe
from frappe.utils import flt, getdate, nowdate

from periodic_valuation.tests.smoke_kernel import COMPANY, ensure_masters

CHECKS = []
ITEM = "_GLA-MAP"
WH_ITEM = "_GLA-MAP-WH"
STD_ITEM = "_GLA-STD"
WAREHOUSE_ACCOUNTS = "item_default_warehouse_accounts"


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ fixtures
def _cost_centres():
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	parent = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 1}, "name")
	names = []
	for suffix in ("A", "B"):
		name = f"_GLA CC {suffix} - {abbr}"
		if not frappe.db.exists("Cost Center", name):
			frappe.get_doc({"doctype": "Cost Center", "cost_center_name": f"_GLA CC {suffix}",
				"parent_cost_center": parent, "company": COMPANY, "is_group": 0}).insert(ignore_permissions=True)
		names.append(name)
	return names


def _dimension():
	"""An enabled accounting dimension and two values of it. With Project
	Accounting installed the suite makes its own two approved projects - the
	client's case; otherwise it takes a dimension the site already has two
	values of. Never runs on cost centres alone: the client defect is the
	project that did not reach the GL."""
	from qcs_platform.ledger.attribution import dimension_fields

	carried = [f for f in dimension_fields("Stock Entry Detail") if f in dimension_fields("Purchase Receipt Item")]
	if "project_accounting" in carried:
		return "project_accounting", [_project(n) for n in ("A", "B")]
	for field in carried:
		options = frappe.get_meta("GL Entry").get_field(field).options
		values = frappe.get_all(options, pluck="name", limit=2, order_by="creation desc",
			filters={"docstatus": 1} if frappe.get_meta(options).is_submittable else {})
		if len(values) == 2:
			return field, values
	raise AssertionError("no accounting dimension with two values on this site: the suite cannot prove "
		"that a row's dimension reaches the GL")


def _project(label):
	name = frappe.db.get_value("Project Accounting", {"project_name": f"_GLA Project {label}", "company": COMPANY,
		"docstatus": 1})
	if name:
		return name
	return frappe.get_doc({"doctype": "Project Accounting", "project_name": f"_GLA Project {label}",
		"company": COMPANY, "project_type": "Opex", "status": "Open"}).insert(ignore_permissions=True).submit().name


def _item(code, method="Periodic Moving Average", include_warehouse=0):
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": method, "valuation_includes_warehouse": include_warehouse,
			**({"settlement_view": "MTD"} if method == "Periodic Standard Cost" else {})}).insert(ignore_permissions=True)
	return code


def _tags(env, n):
	return {"cost_center": env.ccs[n], env.dim: env.dim_values[n]}


def _submit(doc):
	doc = frappe.get_doc(doc)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def _legs(voucher, detail=None):
	filters = {"voucher_no": voucher, "is_cancelled": 0}
	if detail:
		filters["voucher_detail_no"] = detail
	fields = ["account", "debit", "credit", "cost_center", "voucher_detail_no", "valuation_event_id"]
	return frappe.get_all("GL Entry", filters=filters, fields=[*fields, _ENV.dim])


def _carries(env, legs, n, row):
	return bool(legs) and all(g.cost_center == env.ccs[n] and g.voucher_detail_no == row
		and g.get(env.dim) == env.dim_values[n] for g in legs)


def _net_by_attribution(env, vouchers):
	key_fields = ["account", "cost_center", env.dim]
	net = {}
	for g in frappe.get_all("GL Entry", filters={"voucher_no": ("in", vouchers), "is_cancelled": 0},
			fields=[*key_fields, "debit", "credit"]):
		key = tuple(g[f] for f in key_fields)
		net[key] = flt(net.get(key, 0) + flt(g.debit) - flt(g.credit), 2)
	return net


_ENV = frappe._dict()


# --------------------------------------------------------------------- cases
def run(commit=False):
	wh = ensure_masters()
	env = _ENV
	env.update(wh=wh, ccs=_cost_centres())
	frappe.db.savepoint("gla_smoke")
	try:
		env.dim, env.dim_values = _dimension()
		print(f"dimension: {env.dim} = {env.dim_values}")
		# the invoice-difference case bills at another rate than received
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		pr = _receipt(env)
		issue = _issue(env)
		_transfer(env)
		lcv = _landed_cost(env, pr)
		_invoice_difference(env, pr)
		_cancellations(env, issue, lcv)
		_standard_cost(env)
		_no_row(env)
	finally:
		if commit:
			frappe.db.commit()
		else:
			frappe.db.rollback(save_point="gla_smoke")
	passed = sum(1 for _l, ok in CHECKS if ok)
	print(f"\ngl attribution smoke: {passed}/{len(CHECKS)} passed")
	if passed != len(CHECKS):
		raise AssertionError("gl attribution smoke failed")
	return CHECKS


def _receipt(env):
	_item(ITEM)
	pr = _submit({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": ITEM, "qty": 10, "rate": 10, "warehouse": env.wh, **_tags(env, 0)}]})
	row = pr.items[0].name
	legs = _legs(pr.name, row)
	check("G-01 receipt: stock and received-not-billed legs carry the row's cost centre, dimension and row",
		len(legs) >= 2 and _carries(env, legs, 0, row), legs)
	ive = frappe.db.get_value("Inventory Valuation Event", {"source_docname": pr.name}, ["gl_attribution"])
	snap = json.loads(ive or "{}")
	check("G-01 the event snapshots the attribution", snap.get("cost_center") == env.ccs[0]
		and snap.get("voucher_detail_no") == row, snap)
	return pr


def _issue(env):
	expense = frappe.get_cached_value("Company", COMPANY, "default_expense_account")
	issue = _submit({"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Issue",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": ITEM, "qty": q, "s_warehouse": env.wh, "expense_account": expense, **_tags(env, n)}
			for n, q in ((0, 2), (1, 3))]})
	ok = all(_carries(env, _legs(issue.name, r.name), n, r.name) and len(_legs(issue.name, r.name)) == 2
		for n, r in enumerate(issue.items))
	check("G-02 issue of two rows tagged apart: each row's two legs carry its own", ok,
		[_legs(issue.name, r.name) for r in issue.items])
	return issue


def _transfer(env):
	_item(WH_ITEM, include_warehouse=1)
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	dst = f"_GLA Second - {abbr}"
	if not frappe.db.exists("Warehouse", dst):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_GLA Second", "company": COMPANY}).insert(
			ignore_permissions=True)
	item = frappe.get_doc("Item", WH_ITEM)
	if not any(r.warehouse == dst for r in item.get(WAREHOUSE_ACCOUNTS) or []):
		item.append(WAREHOUSE_ACCOUNTS, {"company": COMPANY, "warehouse": dst,
			"default_inventory_account": _second_stock_account()})
		item.save(ignore_permissions=True)
	_submit({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": WH_ITEM, "qty": 5, "rate": 10, "warehouse": env.wh}]})
	se = _submit({"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Transfer",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": WH_ITEM, "qty": 2, "s_warehouse": env.wh, "t_warehouse": dst, **_tags(env, 1)}]})
	legs = _legs(se.name)
	check("G-03 warehouse-scope transfer between two stock accounts: its legs carry the row's", bool(legs)
		and _carries(env, legs, 1, se.items[0].name), legs)


def _second_stock_account():
	"""A stock account of its own for the second warehouse, so the transfer
	posts GL (between warehouses on one account it nets to nothing)."""
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	name = f"_GLA Stock Second - {abbr}"
	if not frappe.db.exists("Account", name):
		parent = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Stock", "is_group": 1}, "name") \
			or frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Asset", "is_group": 1}, "name")
		frappe.get_doc({"doctype": "Account", "account_name": "_GLA Stock Second", "company": COMPANY,
			"parent_account": parent, "account_type": "Stock", "root_type": "Asset"}).insert(ignore_permissions=True)
	return name


def _landed_cost(env, pr):
	exp = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0, "root_type": "Expense"},
		limit=1, pluck="name")[0]
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY, "posting_date": nowdate(),
		"distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name,
			"supplier": "_SMK Supplier", "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp, "description": "freight", "amount": 20}]})
	lcv.get_items_from_purchase_receipts()
	for row in lcv.items:  # the LCV's own row tagged otherwise: the receipt row decides, as in core
		row.cost_center = env.ccs[1]
		if row.meta.has_field(env.dim):
			row.set(env.dim, env.dim_values[1])
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	legs = _legs(lcv.name)
	check("G-04 landed cost: the legs carry the receipt row's cost centre and dimension, on the voucher's row",
		bool(legs) and _carries(env, legs, 0, lcv.items[0].name), legs)
	return lcv


def _invoice_difference(env, pr):
	pi = _submit({"doctype": "Purchase Invoice", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": ITEM, "qty": 10, "rate": 11, "warehouse": env.wh, "purchase_receipt": pr.name,
			"pr_detail": pr.items[0].name, **_tags(env, 1)}]})
	legs = [g for g in _legs(pi.name) if g.valuation_event_id]
	check("G-05 invoice difference: the legs carry the invoice row's", bool(legs)
		and _carries(env, legs, 1, pi.items[0].name), legs)


def _cancellations(env, issue, lcv):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cx = frappe.get_doc("Stock Entry", make_cancellation("Stock Entry", issue.name))
	cx.submit()
	own = all(_carries(env, _legs(cx.name, r.name), n, r.name) for n, r in enumerate(cx.items))
	check("G-06 cancellation: each mirror leg keeps the original's attribution on its own row", own,
		[_legs(cx.name, r.name) for r in cx.items])
	net = _net_by_attribution(env, [issue.name, cx.name])
	check("G-06 issue + cancellation net to zero per account, cost centre and dimension",
		bool(net) and not any(net.values()), net)
	_partial_cancellation(env, make_cancellation)
	lx = frappe.get_doc("Landed Cost Voucher", make_cancellation("Landed Cost Voucher", lcv.name))
	lx.submit()
	net = _net_by_attribution(env, [lcv.name, lx.name])
	check("G-07 landed cost + its cancellation net per attribution", bool(net) and not any(net.values()), net)


def _partial_cancellation(env, make_cancellation):
	"""A Cancellation whose first row was removed before submit: the
	remaining row's mirror lands on that row - paired by the row being
	posted, never by position."""
	expense = frappe.get_cached_value("Company", COMPANY, "default_expense_account")
	issue = _submit({"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Issue",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": ITEM, "qty": q, "s_warehouse": env.wh, "expense_account": expense, **_tags(env, n)}
			for n, q in ((0, 1), (1, 2))]})
	cx = frappe.get_doc("Stock Entry", make_cancellation("Stock Entry", issue.name))
	cx.items = [cx.items[1]]
	cx.items[0].idx = 1
	cx.save(ignore_permissions=True)
	cx.submit()
	legs = _legs(cx.name)
	check("G-06b a Cancellation with its first row removed: the mirror carries the second row's attribution "
		"on its own row", _carries(env, legs, 1, cx.items[0].name), legs)


def _standard_cost(env):
	from periodic_valuation.tests.smoke_std import ensure_std_masters
	from periodic_valuation.tests.uat_std_pack import release_or_pend

	ensure_std_masters(COMPANY)
	_item(STD_ITEM, "Periodic Standard Cost")
	today = getdate(nowdate())
	if not frappe.db.exists("Item Standard Cost Version", {"company": COMPANY, "item_code": STD_ITEM,
			"status": "RELEASED"}):
		scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": COMPANY, "item_code": STD_ITEM,
			"valid_from_year": today.year, "valid_from_month": today.month, "standard_cost": 10,
			"source_type": "MANUAL_OVERRIDE"})
		scv.insert(ignore_permissions=True)
		release_or_pend(scv)
	pr = _submit({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": STD_ITEM, "qty": 10, "rate": 12, "warehouse": env.wh, **_tags(env, 0)}]})
	check("G-08 standard cost receipt: stock, PPV and GR/IR legs carry the row's",
		_carries(env, _legs(pr.name, pr.items[0].name), 0, pr.items[0].name), _legs(pr.name))
	issue = _submit({"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Issue",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": STD_ITEM, "qty": 4, "s_warehouse": env.wh, **_tags(env, 1)}]})
	check("G-08 standard cost issue: its legs carry the row's",
		_carries(env, _legs(issue.name, issue.items[0].name), 1, issue.items[0].name), _legs(issue.name))
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cx = frappe.get_doc("Stock Entry", make_cancellation("Stock Entry", issue.name))
	cx.submit()
	net = _net_by_attribution(env, [issue.name, cx.name])
	check("G-08 the exact reversal nets per attribution", bool(net) and not any(net.values()), net)


def _no_row(env):
	from periodic_valuation.shared.event_gl import source_attribution

	a = source_attribution(COMPANY, ("Inventory Period Settlement Run", "_any"))
	check("G-09 an event of no voucher row: company cost centre, no dimension",
		a.cost_center == frappe.get_cached_value("Company", COMPANY, "cost_center")
		and not any(v for k, v in a.items() if k != "cost_center"), a)
