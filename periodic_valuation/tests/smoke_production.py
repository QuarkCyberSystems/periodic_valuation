"""Production-hardening suite — bench --site <site> execute periodic_valuation.tests.smoke_production.run

Goes beyond the signed 10-row Test Matrix: numeric boundaries, sequencing and
backdating edges, document variety, period machinery, scope isolation, and
global integrity invariants. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from periodic_valuation.tests.smoke_edges import ipb, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))


def settings_doc(company):
	return frappe.get_doc(
		"Periodic Moving Average Settings", {"company": company}
	)


def run(commit=False):
	wh = ensure_masters()
	company = get_company()
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	prior = get_first_day(add_months(nowdate(), -1))
	if not frappe.db.exists("Inventory Period", {"company": company, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": company,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)

	# ---------- P1 fractional quantities, repeating-decimal MAP stays exact
	it = make_item("_PRD-FRAC")
	make_pr(it, wh, 3, 10)
	rv = frappe.get_doc({"doctype": "Stock Revaluation", "company": company,
		"posting_date": nowdate(),
		"items": [{"item_code": it, "warehouse": wh,
			"new_valuation_rate": flt(31 / 3, 6)}]})
	rv.insert(ignore_permissions=True)
	rv.submit()
	make_dn(it, wh, 1)
	make_dn(it, wh, 1)
	make_dn(it, wh, 1)
	c = ipb(it)
	check("P1 repeating-decimal issue-out lands on exact zero",
		flt(c.closing_qty) == 0 and flt(c.closing_value, 2) == 0
		and flt(c.moving_avg_price) == 0 and flt(c.total_received_since_zero) == 0,
		f"{c.closing_qty}/{c.closing_value}")

	# ---------- P2 large magnitudes
	it = make_item("_PRD-BIG")
	make_pr(it, wh, 1000000, 950.12)
	make_dn(it, wh, 400000)
	c = ipb(it)
	check("P2 large magnitudes keep identity",
		flt(c.closing_qty) == 600000
		and flt(c.closing_value, 2) == flt(600000 * 950.12, 2),
		f"{c.closing_qty}/{c.closing_value}")

	# ---------- P3 zero-rate (free) receipt dilutes MAP deliberately
	it = make_item("_PRD-FREE")
	make_pr(it, wh, 10, 10)
	pr0 = frappe.get_doc({"doctype": "Purchase Receipt", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(), "set_posting_time": 1,
		"ignore_pricing_rule": 1,
		"items": [{"item_code": it, "qty": 10, "rate": 0, "price_list_rate": 0,
			"allow_zero_valuation_rate": 1, "warehouse": wh}]})
	pr0.insert(ignore_permissions=True)
	pr0.submit()
	c = ipb(it)
	check("P3 free receipt halves MAP", flt(c.moving_avg_price, 6) == 5
		and flt(c.closing_value, 2) == 100, f"{c.moving_avg_price}/{c.closing_value}")

	# ---------- P4 negative stock BLOCKED when setting is off
	it = make_item("_PRD-NEGBLOCK")
	make_pr(it, wh, 5, 10)
	s = settings_doc(company)
	frappe.db.set_value("Periodic Moving Average Settings", s.name, "negative_stock_allowed", 0)
	try:
		make_dn(it, wh, 8)
		check("P4 negative stock blocked when disabled", False, "DN submitted")
	except frappe.ValidationError:
		check("P4 negative stock blocked when disabled", True)
	finally:
		frappe.db.set_value("Periodic Moving Average Settings", s.name, "negative_stock_allowed", 1)

	# ---------- P5 multi-row voucher: same item twice, sequential blending
	it = make_item("_PRD-2ROW")
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(), "set_posting_time": 1,
		"items": [
			{"item_code": it, "qty": 10, "rate": 10, "warehouse": wh},
			{"item_code": it, "qty": 20, "rate": 13, "warehouse": wh},
		]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	c = ipb(it)
	check("P5 two rows same item blend sequentially",
		flt(c.closing_qty) == 30 and flt(c.closing_value, 2) == 360
		and flt(c.moving_avg_price, 6) == 12, f"{c.closing_qty}/{c.closing_value}")

	# ---------- P6 mixed voucher: routed + FIFO item coexist
	fifo_item = "_PRD-FIFO"
	if not frappe.db.exists("Item", fifo_item):
		frappe.get_doc({"doctype": "Item", "item_code": fifo_item, "item_name": fifo_item,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "FIFO"}).insert(ignore_permissions=True)
	it = make_item("_PRD-MIX")
	pr = frappe.get_doc({"doctype": "Purchase Receipt", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(), "set_posting_time": 1,
		"items": [
			{"item_code": it, "qty": 10, "rate": 10, "warehouse": wh},
			{"item_code": fifo_item, "qty": 5, "rate": 8, "warehouse": wh},
		]})
	pr.insert(ignore_permissions=True)
	pr.submit()
	gl = frappe.get_all("GL Entry", filters={"voucher_no": pr.name, "is_cancelled": 0},
		fields=["account", "debit", "credit", "valuation_event_id"])
	dr = flt(sum(g.debit for g in gl), 2)
	cr = flt(sum(g.credit for g in gl), 2)
	tagged_dr = flt(sum(g.debit for g in gl if g.valuation_event_id), 2)
	fifo_sle = frappe.get_all("Stock Ledger Entry",
		filters={"voucher_no": pr.name, "item_code": fifo_item, "is_cancelled": 0},
		fields=["posted_via_valuation_kernel", "stock_value_difference"])
	check("P6 mixed voucher balanced; kernel GL tagged; FIFO row on core path",
		dr == cr == 140 and tagged_dr == 100
		and fifo_sle and fifo_sle[0].posted_via_valuation_kernel == 0
		and flt(fifo_sle[0].stock_value_difference) == 40,
		f"dr {dr} cr {cr} tagged {tagged_dr} fifo {fifo_sle}")

	# ---------- P7 rejected qty on routed item is refused
	it = make_item("_PRD-REJ")
	rej_wh = wh
	try:
		prr = frappe.get_doc({"doctype": "Purchase Receipt", "company": company,
			"supplier": "_SMK Supplier", "posting_date": nowdate(), "set_posting_time": 1,
			"items": [{"item_code": it, "qty": 8, "received_qty": 10, "rejected_qty": 2,
				"rate": 10, "warehouse": wh, "rejected_warehouse": rej_wh}]})
		prr.insert(ignore_permissions=True)
		prr.submit()
		check("P7 rejected qty refused", False, "submitted")
	except frappe.ValidationError:
		check("P7 rejected qty refused", True)

	# ---------- P8 backdated ISSUE into previous period
	it = make_item("_PRD-BDISS")
	make_pr(it, wh, 50, 10, posting_date=str(prior))
	make_pr(it, wh, 50, 14)  # current MAP now 12 across 100
	dn = make_dn(it, wh, 20, posting_date=str(prior))
	p = ipb_period(it, prior.year, prior.month)
	c = ipb(it)
	# first PR (backdated) contributed +50/+500 carryover; the backdated DN adds -20/-200
	check("P8 backdated issue valued at PRIOR MAP (10), carryover flows",
		flt(p.issue_value, 2) == 200 and flt(c.carryover_qty) == 30
		and flt(c.carryover_value, 2) == 300 and flt(c.closing_qty) == 80,
		f"prior issue {p.issue_value} carry {c.carryover_qty}/{c.carryover_value}")
	gl = frappe.get_all("GL Entry", filters={"voucher_no": dn.name, "is_cancelled": 0},
		fields=["posting_date"])
	check("P8 backdated issue GL on prior date",
		gl and all(str(g.posting_date) == str(prior) for g in gl))

	# ---------- P9 first-ever posting is backdated (no balances exist yet)
	it = make_item("_PRD-FIRSTBD")
	make_pr(it, wh, 30, 7, posting_date=str(prior))
	p = ipb_period(it, prior.year, prior.month)
	c = ipb(it)
	check("P9 first-ever backdated receipt: prior 30/210, current effective 30/210",
		p and flt(p.closing_qty) == 30 and flt(p.closing_value, 2) == 210
		and flt(c.opening_qty) + flt(c.carryover_qty) == 30
		and flt(c.opening_value) + flt(c.carryover_value, 2) == 210
		and flt(c.closing_qty) == 30,
		f"prior {p and p.closing_qty} cur {c.closing_qty}/{c.closing_value}")

	# ---------- P10 cancel an ISSUE; and cancel-of-cancellation refused
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	it = make_item("_PRD-CXLISS")
	make_pr(it, wh, 10, 10)
	dn = make_dn(it, wh, 4)
	cxl = frappe.get_doc("Delivery Note", make_cancellation("Delivery Note", dn.name))
	cxl.submit()
	c = ipb(it)
	check("P10 issue cancellation restores 10/100",
		flt(c.closing_qty) == 10 and flt(c.closing_value, 2) == 100,
		f"{c.closing_qty}/{c.closing_value}")
	try:
		make_cancellation("Delivery Note", cxl.name)
		check("P10 cancel-of-cancellation refused", False)
	except frappe.ValidationError:
		check("P10 cancel-of-cancellation refused", True)

	# ---------- P11 cancel a receipt AFTER consumption at blended MAP (exact mirror)
	it = make_item("_PRD-CXLLATE")
	make_pr(it, wh, 100, 10)
	pr2 = make_pr(it, wh, 50, 20)
	make_dn(it, wh, 30)  # at 14.1667 -> 425.00
	cxl = frappe.get_doc("Purchase Receipt", make_cancellation("Purchase Receipt", pr2.name))
	cxl.submit()
	c = ipb(it)
	# sequence is PR->PR->DN: the DN issued at blended MAP 13.3333 (400), so the
	# exact mirror of PR2 leaves 70 units at 600.00
	check("P11 late receipt cancellation mirrors exactly (70/600)",
		flt(c.closing_qty) == 70 and flt(c.closing_value, 2) == 600.00,
		f"{c.closing_qty}/{c.closing_value}")

	# ---------- P12 posting into a month with no Inventory Period
	it = make_item("_PRD-NOPERIOD")
	future = get_first_day(add_months(nowdate(), 2))
	try:
		make_pr(it, wh, 1, 10, posting_date=str(future))
		check("P12 posting without a period refused", False, "submitted")
	except frappe.ValidationError:
		check("P12 posting without a period refused", True)

	# ---------- P13 invoice diff: negative diff and fully-consumed stock
	it = make_item("_PRD-PIDN")
	pr = make_pr(it, wh, 100, 10)
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": it, "qty": 100, "rate": 9, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	c = ipb(it)
	check("P13a credit-side invoice diff lowers MAP to 9",
		flt(c.moving_avg_price, 4) == 9 and flt(c.closing_value, 2) == 900,
		f"{c.moving_avg_price}/{c.closing_value}")

	it = make_item("_PRD-PICONS")
	pr = make_pr(it, wh, 10, 10)
	make_dn(it, wh, 10)  # fully consumed; counter reset
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": it, "qty": 10, "rate": 12, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	ive = frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": pi.name, "reason_code": "invoice_diff"},
		fields=["value_delta", "expense_portion"])
	c = ipb(it)
	check("P13b fully-consumed diff goes 100% to expense",
		ive and flt(ive[0].value_delta, 2) == 0 and flt(ive[0].expense_portion, 2) == 20
		and flt(c.closing_value, 2) == 0, str(ive))

	# ---------- P14 partial billing diff uses billed qty
	it = make_item("_PRD-PIPART")
	pr = make_pr(it, wh, 100, 10)
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": company,
		"supplier": "_SMK Supplier", "posting_date": nowdate(),
		"items": [{"item_code": it, "qty": 40, "rate": 11, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	c = ipb(it)
	check("P14 partial billing: diff 40, MAP 10.40",
		flt(c.closing_value, 2) == 1040 and flt(c.moving_avg_price, 4) == 10.40,
		f"{c.closing_value}/{c.moving_avg_price}")

	# ---------- P15 per-warehouse scope: independent negative states
	it = make_item("_PRD-WHNEG", include_warehouse=1)
	abbr = frappe.db.get_value("Company", company, "abbr")
	wh2 = f"_SMK Stores 2 - {abbr}"
	if not frappe.db.exists("Warehouse", wh2):
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "_SMK Stores 2",
			"company": company}).insert(ignore_permissions=True)
	make_pr(it, wh, 10, 10)
	make_pr(it, wh2, 10, 20)
	make_dn(it, wh, 15)  # wh scope goes negative, wh2 untouched
	a = ipb(it, warehouse=wh)
	b = ipb(it, warehouse=wh2)
	check("P15 negative state isolated per warehouse scope",
		a.is_negative == 1 and flt(a.frozen_map) == 10
		and b.is_negative == 0 and flt(b.moving_avg_price) == 20,
		f"A {a.is_negative}/{a.frozen_map} B {b.is_negative}/{b.moving_avg_price}")

	# ---------- P16 scope flag locked after transactions
	locked = frappe.get_doc("Item", "_PRD-WHNEG")
	locked.valuation_includes_warehouse = 0
	try:
		locked.save(ignore_permissions=True)
		check("P16 scope flag locked after transactions", False, "saved")
	except frappe.ValidationError:
		check("P16 scope flag locked after transactions", True)

	# ---------- P17 immutability: persisted event rejects modification
	ive_name = frappe.get_all("Inventory Valuation Event", limit=1, pluck="name")[0]
	doc = frappe.get_doc("Inventory Valuation Event", ive_name)
	doc.value_delta = 999999
	try:
		doc.save(ignore_permissions=True)
		check("P17 valuation event immutable", False, "saved")
	except frappe.ValidationError:
		check("P17 valuation event immutable", True)

	# ---------- P18 GLOBAL INVARIANTS across everything this suite posted
	orphans = frappe.db.sql("""
		SELECT COUNT(*) FROM `tabInventory Valuation Event` ive
		LEFT JOIN `tabGL Entry` gle ON gle.valuation_event_id = ive.name AND gle.is_cancelled = 0
		WHERE ive.is_cancelled = 0 AND ive.value_delta != 0 AND gle.name IS NULL""")[0][0]
	check("P18a zero orphan valuation events (all value hits GL)", orphans == 0, str(orphans))

	unbalanced = frappe.db.sql("""
		SELECT COUNT(*) FROM (
			SELECT valuation_event_id, SUM(debit) d, SUM(credit) c FROM `tabGL Entry`
			WHERE COALESCE(valuation_event_id,'') != '' AND is_cancelled = 0
			GROUP BY valuation_event_id HAVING ROUND(SUM(debit) - SUM(credit), 2) != 0
		) x""")[0][0]
	check("P18b every valuation event's GL is balanced", unbalanced == 0, str(unbalanced))

	from periodic_valuation.shared.accounts import get_inventory_account
	total_ipb = 0.0
	accounts = set()
	scopes = frappe.db.sql("""
		SELECT company, item_code, warehouse, MAX(period_year * 100 + period_month) mp
		FROM `tabInventory Period Balance` GROUP BY company, item_code, warehouse""", as_dict=True)
	for s_ in scopes:
		row = frappe.db.get_value("Inventory Period Balance",
			{"company": s_.company, "item_code": s_.item_code, "warehouse": s_.warehouse,
			 "period_year": s_.mp // 100, "period_month": s_.mp % 100},
			["closing_value"])
		total_ipb += flt(row)
		acct = get_inventory_account(s_.company, s_.item_code, s_.warehouse or None)
		if acct:
			accounts.add(acct)
	gl_net = 0.0
	if accounts:
		gl_net = flt(frappe.db.sql("""
			SELECT COALESCE(SUM(debit - credit), 0) FROM `tabGL Entry`
			WHERE account IN %s AND is_cancelled = 0 AND COALESCE(valuation_event_id,'') != ''""",
			(tuple(accounts),))[0][0], 2)
	check("P18c global GL == global period-balance value",
		flt(total_ipb, 2) == gl_net, f"ipb {flt(total_ipb, 2)} gl {gl_net}")

	# ---------- P20 manual-drift guard: JE blocked on kernel inventory accounts
	from periodic_valuation.overrides.journal_guard import CACHE_KEY
	frappe.cache.delete_value(f"{CACHE_KEY}:{company}")
	from periodic_valuation.shared.accounts import get_inventory_account as _gia
	protected_acct = _gia(company, "_PRD-2ROW", wh)
	cogs = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"root_type": "Expense"}, limit=1, pluck="name")[0]
	cash = frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"account_type": "Cash"}, limit=1, pluck="name")[0]
	try:
		je = frappe.get_doc({"doctype": "Journal Entry", "company": company,
			"posting_date": nowdate(), "accounts": [
				{"account": protected_acct, "debit_in_account_currency": 100},
				{"account": cash, "credit_in_account_currency": 100},
			]})
		je.insert(ignore_permissions=True)
		check("P20a manual JE on kernel inventory account blocked", False, "inserted")
	except frappe.ValidationError:
		check("P20a manual JE on kernel inventory account blocked", True)
	je2 = frappe.get_doc({"doctype": "Journal Entry", "company": company,
		"posting_date": nowdate(), "accounts": [
			{"account": cogs, "debit_in_account_currency": 50},
			{"account": cash, "credit_in_account_currency": 50},
		]})
	je2.insert(ignore_permissions=True)
	check("P20b JE on expense accounts still allowed", je2.name is not None)

	# ---------- P19 double period close: activity close, then empty close
	open_p = frappe.get_all("Inventory Period", filters={"company": company, "status": "OPEN"}, pluck="name")[0]
	ipc1 = frappe.get_doc({"doctype": "Inventory Period Close", "company": company,
		"inventory_period": open_p, "posting_date": nowdate()})
	ipc1.insert(ignore_permissions=True)
	ipc1.submit()
	ipc1.reload()
	check("P19a close with activity passes gates", ipc1.reconciliation_passed == 1,
		f"disc {ipc1.discrepancy}")
	open_p2 = frappe.get_all("Inventory Period", filters={"company": company, "status": "OPEN"}, pluck="name")[0]
	ipc2 = frappe.get_doc({"doctype": "Inventory Period Close", "company": company,
		"inventory_period": open_p2, "posting_date": nowdate()})
	ipc2.insert(ignore_permissions=True)
	ipc2.submit()
	ipc2.reload()
	third = frappe.get_all("Inventory Period", filters={"company": company, "status": "OPEN"}, pluck="name")
	check("P19b empty-period close passes vacuously and opens the next",
		ipc2.reconciliation_passed == 1 and bool(third), str(third))
	neg = ipb("_PRD-WHNEG", warehouse=wh)
	check("P19c negative/frozen state survives period close",
		neg.is_negative == 1 and flt(neg.frozen_map) == 10,
		f"{neg.is_negative}/{neg.frozen_map}")

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("production smoke failures: " + "; ".join(x[0] for x in failed))
