# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Item Cost Adjustment - Stage B of a Material allocation (GA-0002-02
GAP-029, D-079) -
bench --site <site> execute periodic_valuation.tests.smoke_item_cost_adjustment.run

Against a stub handover source standing in for whatever app allocates
(this app imports none): pending lines become document lines; a Moving
Average item takes the cost through the Cost Adjustment tree (fully
covered to inventory, partly covered split with price difference); a
Standard Cost item keeps its standard and the cost joins the variance
pool; every leg credits the line's allocation account on the line's cost
centre; a take beyond pending, a non-periodic item and a site with no
source are refused; native cancel is refused and the Cancellation mirrors
the events and releases the take. The quantity a Moving Average line
applies to defaults to the allocation month's receipts, and with none any
stock on hand carries the whole amount (CAR-29); an allocation from a month
whose inventory period has since closed is taken in the open month, never
into the closed one (TC-064). Rolled back.
"""

import contextlib

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from qcs_platform import registry
from qcs_platform.contracts import PendingLine

from periodic_valuation.tests.smoke_edges import make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company

ICA = "Item Cost Adjustment"
PATH = "periodic_valuation.periodic_valuation.doctype.item_cost_adjustment.item_cost_adjustment"
CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


class StubSource:
	"""Positions held in memory; records what was taken and released."""

	app = "_ica_stub"
	id = "stub.material"
	receiver_type = "MATERIAL"

	def __init__(self, company, account, cost_center):
		self.company, self.account, self.cost_center = company, account, cost_center
		self.positions, self.taken, self.released = {}, [], []

	def add(self, key, item, amount, warehouse=None, allocated_on=None):
		self.positions[key] = (item, warehouse, amount, allocated_on or nowdate())

	def pending(self, receiver_type, company, filters):
		return [
			PendingLine(receiver_type="MATERIAL", key=k, company=company, object_doctype="Item", object_name=item,
				amount=amount, account=self.account, cost_center=self.cost_center, bucket="POWER",
				context={"warehouse": wh, "last_posting_date": on})
			for k, (item, wh, amount, on) in self.positions.items()
			if not filters.get("object") or filters["object"] == item
		]

	def consume(self, document, lines):
		for line in lines:
			pending = self.positions.get(line.key, (None, None, 0, None))[2]
			if abs(line.amount) > abs(pending) + 0.005:
				frappe.throw(f"{line.key}: {line.amount} is beyond the pending {pending} (VR-040)")
		self.taken.append((document.name, tuple((l.key, l.amount) for l in lines)))

	def release(self, document):
		self.released.append(document.name)


@contextlib.contextmanager
def source(stub):
	"""The registry answers `stub` for MATERIAL for the body of a case."""
	original_one, original_all = registry.handover_source, registry.handover_sources
	registry.handover_source = lambda t: stub if t == "MATERIAL" else original_one(t)
	registry.handover_sources = lambda: {"MATERIAL": stub} if stub else {}
	try:
		yield stub
	finally:
		registry.handover_source, registry.handover_sources = original_one, original_all


def run():
	frappe.set_user("Administrator")
	frappe.db.savepoint("pv_ica")
	try:
		_run()
	finally:
		frappe.db.rollback(save_point="pv_ica")
	failed = [c for c in CHECKS if not c[1]]
	print(f"\nitem cost adjustment: {len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
	if failed:
		raise Exception("item cost adjustment failures: " + "; ".join(c[0] for c in failed))
	return True


def _account(company):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"_SMK ICA Allocation - {abbr}"
	if not frappe.db.exists("Account", name):
		parent = frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 1}, "name", order_by="lft asc")
		frappe.get_doc({"doctype": "Account", "account_name": "_SMK ICA Allocation", "company": company,
			"parent_account": parent, "root_type": "Expense"}).insert(ignore_permissions=True)
	return name


def _new(company, lines):
	r = frappe.get_attr(f"{PATH}.get_pending")(company)
	doc = frappe.get_doc({"doctype": ICA, "company": company, "posting_date": nowdate(), "items": [
		l for l in r["lines"] if l["handover_key"] in lines]})
	for row in doc.items:
		if lines[row.handover_key] is not None:
			row.amount = lines[row.handover_key]
	doc.insert(ignore_permissions=True)
	return doc


def _legs(doc, account):
	return frappe.get_all("GL Entry", filters={"voucher_no": doc.name, "account": account, "is_cancelled": 0},
		fields=["debit", "credit", "cost_center"])


def _run():
	from periodic_valuation.shared.accounts import get_inventory_account

	wh = ensure_masters()
	company = get_company()
	account = _account(company)
	cc = frappe.get_cached_value("Company", company, "cost_center")
	stub = StubSource(company, account, cc)

	# ---- no source on the site ---------------------------------------
	with source(None):
		r = frappe.get_attr(f"{PATH}.get_pending")(company)
		check("TC-050 no source: nothing pending and the form is told so", r == {"available": False, "lines": []}, str(r))

	full = make_item("_ICA-MAP-FULL")
	make_pr(full, wh, 100, 10)
	part = make_item("_ICA-MAP-PART")
	make_pr(part, wh, 100, 10)
	make_dn(part, wh, 60)
	stub.add("K-FULL", full, 500.0, wh)
	stub.add("K-PART", part, 1_000.0, wh)
	with source(stub):
		r = frappe.get_attr(f"{PATH}.get_pending")(company)
		check("pending lines become document lines with account and cost centre",
			{l["handover_key"] for l in r["lines"]} == {"K-FULL", "K-PART"}
			and all(l["allocation_account"] == account and l["cost_center"] == cc for l in r["lines"]), str(r))

		try:
			over = _new(company, {"K-FULL": 600.0})
			over.submit()
			check("TC-046 a take beyond pending is refused by the source", False, "submitted")
		except frappe.ValidationError as exc:
			check("TC-046 a take beyond pending is refused by the source", "VR-040" in str(exc), str(exc)[:150])

		doc = _new(company, {"K-FULL": None})
		doc.items[0].basis_qty = 100  # on hand 100 covers it
		doc.save(ignore_permissions=True)
		doc.submit()
		doc.reload()
		check("the take reached the source", stub.taken and stub.taken[-1] == (doc.name, (("K-FULL", 500.0),)), str(stub.taken))
		check("the document names its source", doc.handover_source == stub.app, doc.handover_source)
		inv = get_inventory_account(company, full, wh)
		alloc = _legs(doc, account)
		check("TC-044 fully covered: the whole amount to inventory (Dr stock / Cr allocation account)",
			flt(doc.items[0].inventory_amount) == 500.0 and sum(flt(x.debit) for x in _legs(doc, inv)) == 500.0
			and sum(flt(x.credit) for x in alloc) == 500.0, f"{doc.items[0].inventory_amount} {alloc}")
		check("TC-044 the credit is on the line's cost centre", alloc and all(x.cost_center == cc for x in alloc), str(alloc))
		map_now = frappe.db.get_value("Inventory Period Balance", {"company": company, "item_code": full}, "moving_avg_price",
			order_by="period_year desc, period_month desc")
		check("TC-044 the moving average rises by 5.00", flt(map_now, 4) == 15.0, map_now)

		doc2 = _new(company, {"K-PART": None})
		doc2.items[0].basis_qty = 100  # 40 on hand of 100 received since zero
		doc2.save(ignore_permissions=True)
		doc2.submit()
		doc2.reload()
		row = doc2.items[0]
		check("Cost Adjustment tree: 40 % to inventory, 60 % to price difference",
			flt(row.inventory_amount) == 400.0 and flt(row.expense_amount) == 600.0, f"{row.inventory_amount} {row.expense_amount}")
		check("the whole 1,000 credits the allocation account", sum(flt(x.credit) for x in _legs(doc2, account)) == 1_000.0)

		native = "_ICA-NATIVE"
		if not frappe.db.exists("Item", native):
			frappe.get_doc({"doctype": "Item", "item_code": native, "item_name": native, "is_stock_item": 1,
				"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
				"stock_uom": "Nos", "valuation_method": "FIFO"}).insert(ignore_permissions=True)
		stub.add("K-NATIVE", native, 50.0, wh)
		try:
			_new(company, {"K-NATIVE": None})
			check("an item valued outside periodic valuation is refused", False, "saved")
		except frappe.ValidationError as exc:
			check("an item valued outside periodic valuation is refused", "periodic-valuation item" in str(exc), str(exc)[:150])

		_std_case(company, wh, stub, account)
		_coverage_default(company, wh, stub, account)
		_closed_month(company, wh, stub, account)

		# ---- never cancelled; the Cancellation mirrors and releases -------
		frappe.db.savepoint("pv_ica_cancel")
		try:
			frappe.get_doc(ICA, doc.name).cancel()
			check("native cancel is refused", False, "cancelled")
		except frappe.ValidationError as exc:
			check("native cancel is refused", "Cancellation" in str(exc), str(exc)[:150])
		frappe.db.rollback(save_point="pv_ica_cancel")
		from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

		cx = frappe.get_doc(ICA, make_cancellation(ICA, doc.name))
		cx.submit()
		check("the Cancellation releases the take", stub.released == [doc.name], str(stub.released))
		net = sum(flt(x.debit) - flt(x.credit) for x in _legs(doc, account) + _legs(cx, account))
		check("the allocation account nets to zero across the pair", flt(net, 2) == 0, net)
		map_after = frappe.db.get_value("Inventory Period Balance", {"company": company, "item_code": full}, "moving_avg_price",
			order_by="period_year desc, period_month desc")
		check("the moving average is restored", flt(map_after, 4) == 10.0, map_after)

	with source(None):
		try:
			again = frappe.get_doc({"doctype": ICA, "company": company, "posting_date": nowdate(), "items": [
				{"handover_key": "K-PART", "item_code": part, "warehouse": wh, "amount": 1.0,
					"allocation_account": account, "cost_center": cc}]})
			again.insert(ignore_permissions=True)
			again.submit()
			check("TC-050 taking a line with no source is refused", False, "submitted")
		except frappe.ValidationError as exc:
			check("TC-050 taking a line with no source is refused", "No installed app" in str(exc), str(exc)[:150])


def _std_case(company, wh, stub, account):
	from periodic_valuation.tests.smoke_std import ensure_std_masters

	ensure_std_masters(company)
	item = "_ICA-STD"
	if not frappe.db.exists("Item", item):
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "is_stock_item": 1,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos", "valuation_method": "Periodic Standard Cost", "settlement_view": "MTD"}).insert(ignore_permissions=True)
	today = frappe.utils.getdate(nowdate())
	scv = frappe.get_doc({"doctype": "Item Standard Cost Version", "company": company, "item_code": item,
		"valid_from_year": today.year, "valid_from_month": today.month, "standard_cost": 10, "source_type": "MANUAL_OVERRIDE"})
	scv.insert(ignore_permissions=True)
	scv.flags.via_release_flow = True
	scv.status = "RELEASED"
	scv.save(ignore_permissions=True)
	make_pr(item, wh, 50, 10)
	stub.add("K-STD", item, 300.0, wh)
	doc = _new(company, {"K-STD": None})
	doc.submit()
	doc.reload()
	row = doc.items[0]
	events = frappe.get_all("Inventory Valuation Event", filters={"source_docname": doc.name, "is_cancelled": 0},
		fields=["reason_code", "std_trans", "total_ac", "total_sc"])
	check("Standard Cost: the standard stays, the cost joins the variance pool",
		flt(row.expense_amount) == 300.0 and not flt(row.inventory_amount) and events and events[0].std_trans == "LC",
		f"{row.expense_amount} {events}")
	check("Standard Cost: the allocation account is credited", sum(flt(x.credit) for x in _legs(doc, account)) == 300.0)


def _coverage_default(company, wh, stub, account):
	"""CAR-29: Get Pending fills the quantity with the allocation month's
	receipts of the item; a month with none leaves it blank, and blank
	means any stock on hand carries the whole amount."""
	item = make_item("_ICA-MAP-DEFAULT")
	make_pr(item, wh, 100, 10)
	make_dn(item, wh, 60)  # 40 on hand of 100 received this month
	stub.add("K-DEFAULT", item, 1_000.0, wh)
	line = next(l for l in frappe.get_attr(f"{PATH}.get_pending")(company)["lines"] if l["handover_key"] == "K-DEFAULT")
	check("CAR-29 the quantity defaults to the allocation month's receipts (100)", flt(line["basis_qty"]) == 100, line["basis_qty"])
	doc = _new(company, {"K-DEFAULT": None})
	doc.submit()
	doc.reload()
	row = doc.items[0]
	check("CAR-29 left as defaulted: 40 % to inventory, 60 % to price difference",
		flt(row.inventory_amount) == 400.0 and flt(row.expense_amount) == 600.0, f"{row.inventory_amount} {row.expense_amount}")

	quiet = make_item("_ICA-MAP-QUIET")
	make_pr(quiet, wh, 50, 10)
	stub.add("K-QUIET", quiet, 200.0, wh, allocated_on=str(add_months(get_first_day(nowdate()), -12)))
	line = next(l for l in frappe.get_attr(f"{PATH}.get_pending")(company)["lines"] if l["handover_key"] == "K-QUIET")
	check("CAR-29 a month with no receipt leaves the quantity blank", not flt(line["basis_qty"]), line["basis_qty"])
	doc = _new(company, {"K-QUIET": None})
	doc.submit()
	doc.reload()
	check("CAR-29 blank: the stock on hand carries the whole amount", flt(doc.items[0].inventory_amount) == 200.0,
		doc.items[0].inventory_amount)


def _closed_month(company, wh, stub, account):
	"""TC-064: the allocation was posted in a month whose inventory period
	has since closed. The Item Cost Adjustment is refused there and posts in
	the open month; the take clears the position."""
	from periodic_valuation.shared.periods import get_period

	last_month = add_months(get_first_day(nowdate()), -1)
	period = get_period(company, last_month)
	if period:
		frappe.db.set_value("Inventory Period", period.name, "status", "SETTLED_FROZEN", update_modified=False)
	else:
		period = frappe.get_doc({"doctype": "Inventory Period", "company": company, "start_date": str(last_month),
			"status": "SETTLED_FROZEN"})
		period.flags.ignore_validate = True
		period.insert(ignore_permissions=True)
		frappe.db.set_value("Inventory Period", period.name, "status", "SETTLED_FROZEN", update_modified=False)
	item = make_item("_ICA-MAP-CLOSED")
	make_pr(item, wh, 10, 10)
	stub.add("K-CLOSED", item, 100.0, wh, allocated_on=str(last_month))
	frappe.db.savepoint("pv_ica_closed")
	try:
		doc = _new(company, {"K-CLOSED": None})
		doc.posting_date = str(last_month)
		doc.save(ignore_permissions=True)
		doc.submit()
		check("TC-064 a take dated in the closed month is refused", False, "submitted")
	except frappe.ValidationError as exc:
		check("TC-064 a take dated in the closed month is refused", "no longer accepts postings" in str(exc), str(exc)[:160])
	frappe.db.rollback(save_point="pv_ica_closed")
	taken = len(stub.taken)
	doc = _new(company, {"K-CLOSED": None})
	doc.submit()
	doc.reload()
	check("TC-064 it posts in the open month", str(doc.posting_date) == nowdate() and flt(doc.items[0].inventory_amount) == 100.0,
		f"{doc.posting_date} {doc.items[0].inventory_amount}")
	check("TC-064 the take clears the position", len(stub.taken) == taken + 1 and stub.taken[-1][0] == doc.name, str(stub.taken[-1:]))
