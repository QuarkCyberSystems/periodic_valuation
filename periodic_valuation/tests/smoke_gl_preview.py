# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Draft Accounting Ledger preview shows the final GL (client, 06/10/2026) -
bench --site <site> execute periodic_valuation.tests.smoke_gl_preview.run

ERPNext's draft preview runs only the document's own make_gl_entries and
rolls back. The invoice-difference legs post from on_submit, so the preview
of a Purchase Invoice left them out; `before_gl_preview` now posts them
inside the preview's transaction.

  1  same-period price difference: the preview carries the Stock In Hand /
     Stock Received But Not Billed legs and equals the GL posted on submit
  2  invoice dated in the previous period, current period negative: the
     preview also carries the next-period revaluation leg (DR-53) and
     equals the submitted GL
  3  the preview leaves nothing behind: no valuation event, GL or balance
     change for the draft
  4  a draft Purchase Receipt's preview carries the kernel's receipt legs

Rolled back unless commit=True."""

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from periodic_valuation.tests.smoke_edges import COMPANY, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def preview(doc):
	"""What the draft's Accounting Ledger preview shows, without the request
	wrapper's full rollback (which would discard this suite's fixtures)."""
	from erpnext.controllers.stock_controller import get_accounting_ledger_preview

	frappe.db.savepoint("gl_preview")
	try:
		d = frappe.get_doc(doc.doctype, doc.name)
		d.run_method("before_gl_preview")
		_cols, data = get_accounting_ledger_preview(d, frappe._dict(company=COMPANY, include_dimensions=1))
	finally:
		frappe.db.rollback(save_point="gl_preview")
	return lines(data)


def lines(rows):
	out = {}
	for r in rows:
		if isinstance(r, (list, tuple)):
			# the preview's datatable rows: posting_date, account, debit, credit, ...
			r = {"posting_date": r[0], "account": r[1], "debit": r[2], "credit": r[3]}
		key = (str(r.get("posting_date")), r.get("account"))
		dr, cr = out.get(key, (0.0, 0.0))
		out[key] = (round(dr + flt(r.get("debit")), 2), round(cr + flt(r.get("credit")), 2))
	return {k: v for k, v in out.items() if v != (0.0, 0.0)}


def posted(voucher):
	return lines(frappe.get_all("GL Entry", filters={"voucher_no": voucher, "is_cancelled": 0},
		fields=["posting_date", "account", "debit", "credit"]))


def draft_pi(item, wh, pr, qty, rate, posting_date):
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": posting_date, "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	return pi


def run(commit=False):
	frappe.db.savepoint("gl_preview_suite")
	try:
		_run()
	finally:
		if commit:
			frappe.db.commit()
		else:
			frappe.db.rollback(save_point="gl_preview_suite")
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("GL preview failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.shared.accounts import get_inventory_account, get_offset_account

	wh = ensure_masters()
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	prior = get_first_day(add_months(nowdate(), -1))
	cur = get_first_day(nowdate())
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	srbnb = frappe.get_cached_value("Company", COMPANY, "stock_received_but_not_billed")

	# ---- 1: same-period price difference ----------------------------------
	it = make_item("_GLP-1")
	stock = get_inventory_account(COMPANY, it, wh)
	pr = make_pr(it, wh, 100, 10)
	pi = draft_pi(it, wh, pr, 100, 8.5, nowdate())
	pv = preview(pi)
	check("1: the draft preview carries the invoice difference (Cr Stock In Hand 150)",
		pv.get((nowdate(), stock)) == (0.0, 150.0), str(pv))
	# ---- 3: nothing left behind --------------------------------------------
	check("3: the preview leaves no valuation event, GL or balance change for the draft",
		not frappe.db.exists("Inventory Valuation Event", {"source_docname": pi.name})
		and not frappe.db.exists("GL Entry", {"voucher_no": pi.name})
		and flt(ipb_period(it, cur.year, cur.month).closing_value, 2) == 1000)
	pi.submit()
	check("1: the preview equals the GL posted on submit", pv == posted(pi.name), f"{pv} vs {posted(pi.name)}")

	# ---- 2: backdated invoice into a negative current period ----------------
	it = make_item("_GLP-2")
	stock = get_inventory_account(COMPANY, it, wh)
	reval = get_offset_account(COMPANY, it, wh, "revaluation")
	pr = make_pr(it, wh, 100, 10, posting_date=str(prior.replace(day=5)))
	make_dn(it, wh, 120)
	pi = draft_pi(it, wh, pr, 100, 8.5, str(prior.replace(day=7)))
	pv = preview(pi)
	check("2: the preview carries the next-period revaluation leg (Cr revaluation 150 on day 1)",
		pv.get((str(cur), reval)) == (0.0, 150.0) and pv.get((str(cur), stock)) == (150.0, 0.0), str(pv))
	pi.submit()
	check("2: the preview equals the GL posted on submit", pv == posted(pi.name), f"{pv} vs {posted(pi.name)}")

	# ---- 4: draft Purchase Receipt ------------------------------------------
	it = make_item("_GLP-4")
	stock = get_inventory_account(COMPANY, it, wh)
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_SMK Supplier",
		"posting_date": nowdate(), "set_posting_time": 1,
		"items": [{"item_code": it, "qty": 10, "rate": 12, "warehouse": wh}]})
	pr.insert(ignore_permissions=True)
	pv = preview(pr)
	pr.submit()
	check("4: a draft receipt's preview carries the kernel's legs and equals the submitted GL",
		pv.get((nowdate(), stock)) == (120.0, 0.0) and pv == posted(pr.name), f"{pv} vs {posted(pr.name)}")
