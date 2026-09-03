# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Period machine - bench --site <site> execute periodic_valuation.tests.smoke_periods.run

DR-37: two months may be open at once (current OPEN, previous
PREV_OPEN_UNSETTLED), never three. A posting dated in the month after the
OPEN one rolls the machine forward on demand; the roll is refused while the
older previous-open month is unclosed; Inventory Period Close of a
previous-open month freezes it after the gates; closing the OPEN month
rolls instead of freezing; a closed month is reopenable only through the
gated Inventory Period Reopen and only while it is the immediately-previous
month (DR-45 / DR-08). Runs in its own company so the site's real
periods are never touched. Rolled back unless commit=True."""

import frappe
from frappe.utils import add_months, flt, get_first_day, getdate, nowdate

COMPANY = "Period Test Co"
ABBR = "PTC"
CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def ensure_company():
	if not frappe.db.exists("Company", COMPANY):
		base = frappe.get_all("Company", filters={"name": ("not like", "%Test Co%")},
			limit=1, fields=["default_currency", "country"])[0]
		frappe.get_doc({"doctype": "Company", "company_name": COMPANY, "abbr": ABBR,
			"default_currency": base.default_currency, "country": base.country,
			"create_chart_of_accounts_based_on": "Standard Template"}).insert(ignore_permissions=True)
	for fy in frappe.get_all("Fiscal Year", fields=["name", "year_start_date", "year_end_date"]):
		if fy.year_start_date.year <= getdate(nowdate()).year <= fy.year_end_date.year:
			if frappe.db.exists("Fiscal Year Company", {"parent": fy.name}) and not frappe.db.exists(
					"Fiscal Year Company", {"parent": fy.name, "company": COMPANY}):
				frappe.get_doc({"doctype": "Fiscal Year Company", "parent": fy.name, "parenttype": "Fiscal Year",
					"parentfield": "companies", "company": COMPANY}).insert(ignore_permissions=True)
	if not frappe.db.exists("Periodic Moving Average Settings", {"company": COMPANY}):
		def acc(hint, root="Expense"):
			rows = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0,
				"account_name": ("like", f"%{hint}%")}, limit=1, pluck="name")
			return rows[0] if rows else frappe.get_all("Account", filters={"company": COMPANY,
				"is_group": 0, "root_type": root}, limit=1, pluck="name")[0]
		frappe.get_doc({"doctype": "Periodic Moving Average Settings", "company": COMPANY,
			"prd_account": acc("Cost of Goods Sold"), "fx_gain_loss_account": acc("Exchange Gain/Loss"),
			"stock_rounding_adjustment_account": acc("Stock Adjustment"),
			"price_difference_account": acc("Cost of Goods Sold"),
			"inventory_variance_account": acc("Stock Adjustment"),
			"stock_revaluation_account": acc("Stock Adjustment"),
			"negative_stock_allowed": 1}).insert(ignore_permissions=True)
	wh = f"Stores - {ABBR}"
	if not frappe.db.exists("Warehouse", wh):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores", "company": COMPANY}).insert(ignore_permissions=True)
	if not frappe.db.exists("Supplier", "_PTC Supplier"):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": "_PTC Supplier",
			"supplier_group": frappe.get_all("Supplier Group", limit=1, pluck="name")[0]}).insert(ignore_permissions=True)
	item = "_PTC-ROLL"
	if not frappe.db.exists("Item", item):
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Moving Average"}).insert(ignore_permissions=True)
	return wh, item


def make_pr(item, wh, qty, rate, posting_date):
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": COMPANY, "supplier": "_PTC Supplier",
		"posting_date": posting_date, "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh}]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	return pr


def period(y, m):
	name = frappe.db.get_value("Inventory Period", {"company": COMPANY, "period_year": y, "period_month": m})
	return frappe.get_doc("Inventory Period", name) if name else None


def status_of(y, m):
	p = period(y, m)
	return p.status if p else None


def make_period(y, m, status):
	doc = frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
		"start_date": f"{y}-{m:02d}-01", "status": status})
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Inventory Period", doc.name, "status", status, update_modified=False)


def ipb(item, y, m):
	rows = frappe.get_all("Inventory Period Balance", filters={"company": COMPANY, "item_code": item,
		"period_year": y, "period_month": m}, fields=["opening_qty", "closing_qty", "closing_value"])
	return rows[0] if rows else None


def ym(d):
	return d.year, d.month


def run(commit=False):
	from periodic_valuation.shared.period_close import open_next_period, roll_periods_due
	from periodic_valuation.shared.periods import assert_posting_allowed

	wh, item = ensure_company()
	today = getdate(nowdate())
	m0 = get_first_day(today)                 # this calendar month
	m1 = get_first_day(add_months(m0, -1))     # last month
	m2 = get_first_day(add_months(m0, -2))
	m3 = get_first_day(add_months(m0, -3))

	# machine two months back: m3 previous-open (unclosed), m2 OPEN
	make_period(*ym(m3), "PREV_OPEN_UNSETTLED")
	make_period(*ym(m2), "OPEN")
	make_pr(item, wh, 10, 15, str(m2.replace(day=5)))   # balance in the OPEN month

	# ---- 1. a third month is refused while the oldest is unclosed
	try:
		make_pr(item, wh, 1, 15, str(m1.replace(day=3)))
		check("posting into the next month is refused while the older month is unclosed", False, "posted")
	except frappe.ValidationError as e:
		check("posting into the next month is refused while the older month is unclosed",
			"still open" in str(e), str(e)[:140])
	check("machine untouched by the refusal", status_of(*ym(m2)) == "OPEN" and status_of(*ym(m1)) is None,
		f"{status_of(*ym(m2))} / {status_of(*ym(m1))}")

	# ---- 2. once the oldest is frozen, the same posting rolls the machine on demand
	frappe.db.set_value("Inventory Period", period(*ym(m3)).name, "status", "SETTLED_FROZEN", update_modified=False)
	pr = make_pr(item, wh, 1, 15, str(m1.replace(day=3)))
	check("posting rolls: OPEN -> previous-open, next month OPEN",
		status_of(*ym(m2)) == "PREV_OPEN_UNSETTLED" and status_of(*ym(m1)) == "OPEN",
		f"{status_of(*ym(m2))} / {status_of(*ym(m1))}")
	b = ipb(item, *ym(m1))
	check("new month seeded from the previous closing (opening 10 -> closing 11 @ 15)",
		b and flt(b.opening_qty) == 10 and flt(b.closing_qty) == 11 and flt(b.closing_value) == 165,
		str(b and (b.opening_qty, b.closing_qty, b.closing_value)))
	check("the posting landed in the new month's period",
		frappe.db.get_value("Inventory Valuation Event", {"source_docname": pr.name}, "period_month") == m1.month, "")

	# ---- 3. backdating into the previous-open month carries forward
	make_pr(item, wh, 2, 15, str(m2.replace(day=20)))
	b = ipb(item, *ym(m1))
	check("backdated receipt into previous-open carries into the open month (13 @ 15)",
		b and flt(b.closing_qty) == 13 and flt(b.closing_value) == 195, str(b and (b.closing_qty, b.closing_value)))

	# ---- 4. two months ahead is not a roll (resolver-level: ERPNext refuses
	# future posting dates before the kernel is reached)
	try:
		assert_posting_allowed(COMPANY, str(add_months(m1, 2).replace(day=2)))
		check("a date two months ahead is refused (no silent double roll)", False, "allowed")
	except frappe.ValidationError as e:
		check("a date two months ahead is refused (no silent double roll)", "No Inventory Period covers" in str(e), str(e)[:120])

	# ---- 5. scheduler: refused while m2 unclosed, rolls once frozen
	try:
		open_next_period(period(*ym(m1)))
		check("scheduler roll refused while the previous month is unclosed", False, "rolled")
	except frappe.ValidationError:
		check("scheduler roll refused while the previous month is unclosed", True)
	frappe.db.set_value("Inventory Period", period(*ym(m2)).name, "status", "SETTLED_FROZEN", update_modified=False)
	rolled = roll_periods_due(commit=False, company=COMPANY)
	check("roll_periods_due opens the current calendar month once the rule allows",
		COMPANY in rolled and status_of(*ym(m1)) == "PREV_OPEN_UNSETTLED" and status_of(*ym(m0)) == "OPEN",
		f"{rolled} {status_of(*ym(m1))} / {status_of(*ym(m0))}")

	# ---- 6. Inventory Period Close of the previous-open month freezes it
	close = frappe.get_doc({"doctype": "Inventory Period Close", "company": COMPANY,
		"inventory_period": period(*ym(m1)).name, "posting_date": nowdate()})
	close.insert(ignore_permissions=True)
	close.submit()
	check("Close of the previous-open month freezes it after the gates", status_of(*ym(m1)) == "SETTLED_FROZEN", status_of(*ym(m1)))

	# ---- 7. Close of the OPEN month rolls (does not freeze)
	make_pr(item, wh, 1, 15, str(today))
	close2 = frappe.get_doc({"doctype": "Inventory Period Close", "company": COMPANY,
		"inventory_period": period(*ym(m0)).name, "posting_date": nowdate()})
	close2.insert(ignore_permissions=True)
	close2.submit()
	nxt = get_first_day(add_months(m0, 1))
	check("Close of the OPEN month rolls forward instead of freezing",
		status_of(*ym(m0)) == "PREV_OPEN_UNSETTLED" and status_of(*ym(nxt)) == "OPEN", f"{status_of(*ym(m0))} / {status_of(*ym(nxt))}")

	# ---- 8. gated reopen (DR-45 / DR-08 window)
	# state: m1 frozen, m0 previous-open, nxt OPEN
	def try_reopen(y, m, reason="test reopen"):
		doc = frappe.get_doc({"doctype": "Inventory Period Reopen", "company": COMPANY,
			"inventory_period": period(y, m).name, "posting_date": nowdate(), "reason": reason})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc
	try:
		try_reopen(*ym(m1))
		check("reopen refused while another month is still previous-open", False, "reopened")
	except frappe.ValidationError as e:
		check("reopen refused while another month is still previous-open", "still open as the previous period" in str(e), str(e)[:140])
	close3 = frappe.get_doc({"doctype": "Inventory Period Close", "company": COMPANY,
		"inventory_period": period(*ym(m0)).name, "posting_date": nowdate()})
	close3.insert(ignore_permissions=True)
	close3.submit()
	check("m0 frozen by its own close (setup)", status_of(*ym(m0)) == "SETTLED_FROZEN", status_of(*ym(m0)))
	try:
		try_reopen(*ym(m1))
		check("reopen refused for a month older than the immediately-previous one", False, "reopened")
	except frappe.ValidationError as e:
		check("reopen refused for a month older than the immediately-previous one", "Outside the Reopen Window" in str(e) or "immediately-previous" in str(e), str(e)[:140])
	try:
		try_reopen(*ym(nxt))
		check("reopen refused for a period that is not closed", False, "reopened")
	except frappe.ValidationError as e:
		check("reopen refused for a period that is not closed", "Only a closed period" in str(e), str(e)[:140])
	ro = try_reopen(*ym(m0), "UAT needs backdated August tests")
	p0 = period(*ym(m0))
	check("reopen of the immediately-previous frozen month: PREV_OPEN_UNSETTLED again, audit stamped",
		p0.status == "PREV_OPEN_UNSETTLED" and p0.reopened_by == frappe.session.user and p0.reopen_count == 1
		and p0.last_reopen == ro.name and ro.status_before == "SETTLED_FROZEN" and ro.reopen_sequence == 1,
		f"{p0.status} by {p0.reopened_by} count {p0.reopen_count} doc {ro.name}")
	before = ipb(item, *ym(nxt))
	make_pr(item, wh, 3, 15, str(m0.replace(day=1)))   # m0 is the current calendar month: stay in the past
	after = ipb(item, *ym(nxt))
	check("backdated posting into the reopened month is accepted and carries into the open month (+3)",
		flt(after.closing_qty) == flt(before.closing_qty) + 3, f"{before.closing_qty} -> {after.closing_qty}")
	try:
		open_next_period(period(*ym(nxt)))
		check("machine cannot roll past the open month while the reopened month is unclosed", False, "rolled")
	except frappe.ValidationError as e:
		check("machine cannot roll past the open month while the reopened month is unclosed", "still open" in str(e), str(e)[:140])
	try:
		ro.cancel()
		check("reopen document cannot be cancelled (forward-only)", False, "cancelled")
	except frappe.ValidationError as e:
		check("reopen document cannot be cancelled (forward-only)", "cannot be cancelled" in str(e), str(e)[:140])
	close4 = frappe.get_doc({"doctype": "Inventory Period Close", "company": COMPANY,
		"inventory_period": period(*ym(m0)).name, "posting_date": nowdate()})
	close4.insert(ignore_permissions=True)
	close4.submit()
	check("re-close through Inventory Period Close freezes the reopened month again (gates re-run)",
		status_of(*ym(m0)) == "SETTLED_FROZEN", status_of(*ym(m0)))
	try:
		try_reopen(*ym(m0), "second time")
		p0 = period(*ym(m0))
		check("a second reopen counts up (reopen no. 2) and keeps the first document in history",
			p0.reopen_count == 2 and p0.status == "PREV_OPEN_UNSETTLED" and p0.last_reopen != ro.name, f"{p0.reopen_count} {p0.last_reopen}")
	except frappe.ValidationError as e:
		check("a second reopen counts up (reopen no. 2) and keeps the first document in history", False, str(e)[:140])

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
