"""DR-68: one way to open a period; a balance for every scope in every open
month; the opening fixed (client ticket STD-021, 14/10/2026; Vivek: the SAP
model). Run:
bench --site <site> execute periodic_valuation.tests.verify_period_machine.run

  1  through the form / API, a later period cannot be created and a status
     cannot be changed; a company's first period can
  2  Open Next Period is refused without a period role
  3  the opening of a balance cannot be changed once created
  4  a cost-only standard-cost item has a balance in every open month
  5  Inventory Period Close's gate reports an item without a balance in the
     month

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_months, flt, get_first_day, getdate, nowdate

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
		raise Exception("Period machine failures: " + "; ".join(c[0] for c in failed))


def _refused(fn):
	frappe.db.savepoint("pm_refusal")
	try:
		fn()
		return ""
	except (frappe.ValidationError, frappe.PermissionError) as e:
		frappe.clear_last_message()
		return str(e) or "refused"
	finally:
		frappe.db.rollback(save_point="pm_refusal")


class _AsRequest:
	"""What a desk form save or an API call looks like to the controller."""

	def __enter__(self):
		self.old = getattr(frappe.local, "request", None)
		frappe.local.request = object()

	def __exit__(self, *exc):
		frappe.local.request = self.old


def _run():
	from periodic_valuation.shared.period_close import assert_scope_rows_complete

	today = getdate(nowdate())
	day1 = get_first_day(today)
	prev = add_months(day1, -1)
	nxt = add_months(day1, 1)
	frappe.db.savepoint("period_machine")
	try:
		pack.ensure_company()
		pp = pack.make_period(prev.year, prev.month, "PREV_OPEN_UNSETTLED")
		cp = pack.make_period(today.year, today.month, "OPEN")

		# ---- 1 ------------------------------------------------------------------
		with _AsRequest():
			msg = _refused(lambda: frappe.get_doc({"doctype": "Inventory Period", "company": pack.COMPANY,
				"start_date": str(nxt), "status": "OPEN"}).insert(ignore_permissions=True))
			check("1: a later period cannot be created through the form / API", "Open Next Period" in msg, msg)
			def edit_status():
				d = frappe.get_doc("Inventory Period", cp)
				d.status = "PREV_OPEN_UNSETTLED"
				d.save(ignore_permissions=True)
			msg = _refused(edit_status)
			check("1: a period's status cannot be changed through the form / API", "period actions" in msg.lower()
				or "Open Next Period" in msg, msg)
			co = "_DR68 First Period Co"
			if not frappe.db.exists("Company", co):
				frappe.get_doc({"doctype": "Company", "company_name": co, "abbr": "D68F",
					"default_currency": frappe.db.get_value("Company", pack.COMPANY, "default_currency"),
					"country": frappe.db.get_value("Company", pack.COMPANY, "country")}).insert(ignore_permissions=True)
			msg = _refused(lambda: frappe.get_doc({"doctype": "Inventory Period", "company": co,
				"start_date": str(day1), "status": "OPEN"}).insert(ignore_permissions=True))
			check("1: a company's first period can be created (go-live)", not msg, msg)

		# ---- 2 ------------------------------------------------------------------
		user = "dr68.nobody@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "DR68", "send_welcome_email": 0,
				"roles": [{"role": "Stock User"}]}).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			msg = _refused(lambda: frappe.get_doc("Inventory Period", cp).open_next())
		finally:
			frappe.set_user("Administrator")
		check("2: Open Next Period is refused without a period role", "needs one of these roles" in msg, msg)

		# ---- 3, 4 -----------------------------------------------------------------
		it = pack.std_item("_DR68-COST-ONLY")
		pack.scv_release(it, prev.year, prev.month, 50)
		rows = frappe.get_all("Inventory Period Balance", filters={"company": pack.COMPANY, "item_code": it},
			fields=["name", "period_month", "opening_qty", "closing_qty", "period_standard_cost"], order_by="period_year, period_month")
		check("4: a cost-only item has a balance in every open month, zero, at its standard",
			[(r.period_month, flt(r.closing_qty), flt(r.period_standard_cost)) for r in rows]
			== [(prev.month, 0.0, 50.0), (today.month, 0.0, 50.0)], str(rows))
		from periodic_valuation.shared.immutable import KERNEL_FLAG

		def change_opening_kernel():
			frappe.flags[KERNEL_FLAG] = True
			try:
				d = frappe.get_doc("Inventory Period Balance", rows[-1].name)
				d.opening_qty = 5
				d.save(ignore_permissions=True)
			finally:
				frappe.flags[KERNEL_FLAG] = False
		msg = _refused(change_opening_kernel)
		check("3: the opening cannot be changed once created, not even by the kernel", "fixed" in msg, msg)

		# ---- 5 ------------------------------------------------------------------
		frappe.db.delete("Inventory Period Balance", {"name": rows[-1].name})
		gate = assert_scope_rows_complete(frappe.get_doc("Inventory Period", cp))
		check("5: the close gate reports an item without a balance in the month",
			not gate["ok"] and any(m["item_code"] == it for m in gate["missing"]), str(gate)[:200])
		del pp
	finally:
		frappe.db.rollback(save_point="period_machine")
