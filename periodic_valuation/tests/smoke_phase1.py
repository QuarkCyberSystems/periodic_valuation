"""Phase-1 smoke test - run via: bench --site <site> execute smoke_phase1.run

Verifies (read-only where possible, all writes rolled back):
1. Shared doctypes exist with expected schema.
2. Custom fields landed (GL Entry.valuation_event_id, cancellation fields, Item tables).
3. Item accepts 'Periodic Moving Average'; enum extended; core defaults NOT extended.
4. Routing dispatch reaches the stub kernel (expected throw).
5. Immutability guards block manual event inserts.
6. Unknown-method guard exists on update_entries_after.
"""

import frappe

RESULTS = []


def check(label, ok, detail=""):
	RESULTS.append((label, bool(ok), detail))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def run():
	frappe.flags.in_test = False

	# 1. doctypes
	for dt in (
		"Stock Movement Event",
		"Inventory Valuation Event",
		"Inventory Period Balance",
		"Inventory Period",
		"Inventory Period Close",
		"Inventory Period Balance Snapshot",
		"Periodic Moving Average Settings",
		"Item Default Warehouse Account",
		"Stock Entry Type Account",
	):
		check(f"doctype {dt}", frappe.db.exists("DocType", dt))

	# 2. custom fields
	check(
		"GL Entry.valuation_event_id",
		frappe.db.exists("Custom Field", {"dt": "GL Entry", "fieldname": "valuation_event_id"}),
	)
	for dt in ("Purchase Receipt", "Delivery Note", "Stock Entry", "Landed Cost Voucher"):
		check(
			f"{dt}.is_cancellation",
			frappe.db.exists("Custom Field", {"dt": dt, "fieldname": "is_cancellation"}),
		)

	# 3. enum
	item_meta = frappe.get_meta("Item")
	options = (item_meta.get_field("valuation_method").options or "").split("\n")
	check("Item enum has Periodic Moving Average", "Periodic Moving Average" in options)
	check("Item enum has Periodic Standard Cost", "Periodic Standard Cost" in options)
	check(
		"Item has valuation_includes_warehouse",
		item_meta.get_field("valuation_includes_warehouse") is not None,
	)
	ss_options = (frappe.get_meta("Stock Settings").get_field("valuation_method").options or "").split("\n")
	check("Stock Settings enum NOT extended (no global toggle)", "Periodic Moving Average" not in ss_options)

	# SLE flag
	check(
		"SLE.posted_via_valuation_kernel",
		frappe.get_meta("Stock Ledger Entry").get_field("posted_via_valuation_kernel") is not None,
	)

	# hooks registry
	kernels = frappe.get_hooks("valuation_kernels")
	check("kernel registry hook", kernels.get("Periodic Moving Average"))

	# 4. immutability guard - manual SME insert must be blocked
	try:
		frappe.get_doc(
			{
				"doctype": "Stock Movement Event",
				"company": frappe.get_all("Company", limit=1, pluck="name")[0],
				"item_code": "x",
				"posting_date": "2026-07-06",
				"movement_type": "receipt",
				"qty_delta": 1,
			}
		).insert()
		check("SME manual insert blocked", False, "insert unexpectedly succeeded")
	except frappe.ValidationError:
		check("SME manual insert blocked", True)
	finally:
		frappe.db.rollback()

	# 5. routing dispatch: build a fake controller-ish object and call the
	# StockController method unbound with crafted entries.
	from erpnext.controllers.stock_controller import StockController

	company = frappe.get_all("Company", limit=1, pluck="name")[0]

	item_code = frappe.get_all("Item", filters={"is_stock_item": 1}, limit=1, pluck="name")
	if item_code:
		item_code = item_code[0]
		original = frappe.db.get_value("Item", item_code, "valuation_method")
		frappe.db.set_value("Item", item_code, "valuation_method", "Periodic Moving Average")
		frappe.clear_cache(doctype="Item")
		frappe.local.request_cache = None  # bust @request_cache on get_valuation_method

		fake = frappe._dict({"doctype": "Stock Entry", "name": "SMOKE-TEST", "company": company})
		fake.get = fake.__getitem__ if False else lambda k, d=None: frappe._dict.get(fake, k, d)

		class FakeController:
			doctype = "Stock Entry"
			name = "SMOKE-TEST"

			def get(self, key, default=None):
				return {"company": company}.get(key, default)

		try:
			StockController.route_periodic_valuation_entries(
				FakeController(), [frappe._dict({"item_code": item_code})]
			)
			check("routing dispatch reaches stub kernel", False, "no throw")
		except frappe.ValidationError as e:
			check("routing dispatch reaches stub kernel", "Kernel Not Enabled" in str(e) or "not yet enabled" in str(e), str(e)[:120])
		finally:
			frappe.db.set_value("Item", item_code, "valuation_method", original or "")
			frappe.db.rollback()

	# 6. unknown-method guard present
	from erpnext.stock.stock_ledger import update_entries_after

	check(
		"unknown-method throw guard on update_entries_after",
		hasattr(update_entries_after, "validate_known_valuation_method"),
	)

	failed = [r for r in RESULTS if not r[1]]
	print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
	if failed:
		raise Exception("Smoke test failures: " + "; ".join(r[0] for r in failed))
