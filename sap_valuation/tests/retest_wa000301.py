# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""WA-0003-01 UAT-01 RE-ENTRY — recreate every client-reported issue on the
LIVE site with fresh items and REAL vouchers, so the fixes can be inspected
on the actual documents.

  bench --site <site> execute sap_valuation.tests.retest_wa000301.run --kwargs '{"commit": true}'

Unlike verify_wa000301 (which rolls back), this COMMITS when commit=True so the
documents persist for review. Every item created is prefixed `UAT-RT-` and a
run tag is printed for easy filtering. Idempotent-ish: item codes carry the run
tag, so re-running makes a fresh set.
"""

import frappe
from frappe.utils import add_days, flt, nowdate

CO = "Badia Cement"
WH = "UAT Stores - BC"
SUP = "QCS"
CUST = "UAT Customer"
RESULTS = []


def _tag():
	# stable per-run suffix without Date.now(): use max existing run index + 1
	existing = frappe.get_all("Item", filters={"item_code": ("like", "UAT-RT-%-R%")}, pluck="item_code")
	idx = 0
	for c in existing:
		m = c.rsplit("-R", 1)
		if len(m) == 2 and m[1].isdigit():
			idx = max(idx, int(m[1]))
	return f"R{idx + 1}"


def result(issue, label, ok, detail=""):
	RESULTS.append((issue, label, bool(ok), detail))
	print(f"{'PASS' if ok else 'FAIL'} [{issue}] {label}" + (f" — {detail}" if detail else ""))


def item(code):
	if not frappe.db.exists("Item", code):
		ig = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		uom = "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", limit=1, pluck="name")[0]
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code, "item_group": ig,
			"stock_uom": uom, "is_stock_item": 1, "valuation_method": "SAP Moving Average",
		}).insert(ignore_permissions=True)
	return code


def pr(it, qty, rate, date=None, is_return=False, against=None, against_detail=None):
	row = {"item_code": it, "warehouse": WH, "qty": qty, "rate": rate}
	if against_detail:
		row["purchase_receipt"] = against
		row["purchase_receipt_item"] = against_detail
	doc = frappe.get_doc({"doctype": "Purchase Receipt", "company": CO, "supplier": SUP,
		"posting_date": date or nowdate(), "set_posting_time": 1,
		"is_return": 1 if is_return else 0, "return_against": against, "items": [row]})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def dn(it, qty, date=None, is_return=False, against=None):
	doc = frappe.get_doc({"doctype": "Delivery Note", "company": CO, "customer": CUST,
		"posting_date": date or nowdate(), "set_posting_time": 1,
		"is_return": 1 if is_return else 0, "return_against": against,
		"items": [{"item_code": it, "warehouse": WH, "qty": qty, "rate": 100}]})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def pi(it, prd, qty, rate, date=None, **extra):
	doc = frappe.get_doc({"doctype": "Purchase Invoice", "company": CO, "supplier": SUP,
		"posting_date": date or nowdate(),
		"items": [{"item_code": it, "qty": qty, "rate": rate, "warehouse": WH,
			"purchase_receipt": prd.name, "pr_detail": prd.items[0].name}], **extra})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def lcv(prd, amount, date=None):
	freight = frappe.get_all("Account", filters={"company": CO, "is_group": 0,
		"account_name": ("like", "%Freight%")}, limit=1, pluck="name")
	doc = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": CO,
		"posting_date": date or nowdate(),
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt",
			"receipt_document": prd.name, "supplier": prd.supplier, "grand_total": prd.grand_total}],
		"taxes": [{"expense_account": freight[0], "description": "Freight", "amount": amount}],
		"distribute_charges_based_on": "Amount"})
	doc.get_items_from_purchase_receipts()
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def count(it, counted, date=None):
	doc = frappe.get_doc({"doctype": "Stock Count", "company": CO, "posting_date": date or nowdate(),
		"set_posting_time": 1, "items": [{"item_code": it, "warehouse": WH, "counted_qty": counted}]})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def reval(it, new_rate, date=None):
	doc = frappe.get_doc({"doctype": "Stock Revaluation", "company": CO, "posting_date": date or nowdate(),
		"items": [{"item_code": it, "warehouse": WH, "new_valuation_rate": new_rate}]})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def ipb(it, year=None, month=None):
	f = {"company": CO, "item_code": it}
	if year:
		f.update({"period_year": year, "period_month": month})
	r = frappe.get_all("Inventory Period Balance", filters=f,
		fields=["closing_qty", "closing_value", "moving_avg_price", "receipt_qty", "carryover_qty", "carryover_value"],
		order_by="period_year desc, period_month desc", limit=1)
	return r[0] if r else None


def cancel(dt, name):
	from sap_valuation.sap_moving_average.cancellation import make_cancellation
	cx = frappe.get_doc(dt, make_cancellation(dt, name))
	cx.submit()
	return cx


def run(commit=False):
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 50)
	frappe.db.set_single_value("Selling Settings", "maintain_same_sales_rate", 0)
	T = _tag()
	today = nowdate()
	print(f"\n===== WA-0003-01 RE-ENTRY on {CO} — run tag {T} (items prefixed UAT-RT-*-{T}) =====\n")

	def sc(n, tag):
		return item(f"UAT-RT-{n:02d}-{tag}-{T}")

	# --- Items 1/2/3: PI price difference + stock-ratio split
	try:
		it = sc(1, "PIDIFF"); p = pr(it, 100, 10, today); dn(it, 40, today)   # hold 60/100 -> ratio 0.6
		inv = pi(it, p, 100, 13, today)                                        # diff 300 @ ratio 0.6
		ive = frappe.get_all("Inventory Valuation Event",
			filters={"source_docname": inv.name, "reason_code": "invoice_diff"},
			fields=["value_delta", "expense_portion"])
		result("1/2/3", f"PI {inv.name}: diff 300 splits 180 stock / 120 exp (ratio 0.6)",
			ive and flt(ive[0].value_delta, 2) == 180 and flt(ive[0].expense_portion, 2) == 120,
			str(ive))
	except Exception as e:
		result("1/2/3", "PI price-difference split", False, str(e)[:120])

	# --- Item 4: Stock Revaluation posts linked GL, correct MAP
	try:
		it = sc(4, "REVAL"); pr(it, 100, 10, today)
		rv = reval(it, 15, today)
		gl = frappe.get_all("GL Entry", filters={"voucher_no": rv.name, "is_cancelled": 0},
			fields=["valuation_event_id", "debit"])
		result("4", f"Stock Revaluation {rv.name}: 2 GL rows tagged, MAP -> 15",
			len(gl) == 2 and all(g.valuation_event_id for g in gl)
			and flt(ipb(it).moving_avg_price, 2) == 15, f"MAP {ipb(it).moving_avg_price}")
	except Exception as e:
		result("4", "Stock Revaluation GL", False, str(e)[:120])

	# --- Item 5 + 9: reverse an issue, and reverse a purchase return
	try:
		it = sc(5, "REVISS"); pr(it, 100, 10, today)
		se = dn(it, 30, today)
		cx = cancel("Delivery Note", se.name)
		result("5", f"reverse issue {se.name} -> {cx.name}, stock back to 100",
			flt(ipb(it).closing_qty) == 100, f"qty {ipb(it).closing_qty}")
	except Exception as e:
		result("5", "reverse of issue", False, str(e)[:120])
	try:
		it = sc(9, "REVRET"); p = pr(it, 100, 10, today)
		ret = pr(it, -40, 10, today, is_return=True, against=p.name, against_detail=p.items[0].name)
		cx = cancel("Purchase Receipt", ret.name)
		result("9", f"reverse purchase-return {ret.name} -> {cx.name} (was 'original not found')",
			flt(ipb(it).closing_qty) == 100, f"qty {ipb(it).closing_qty}")
	except Exception as e:
		result("9", "reverse of purchase return", False, str(e)[:120])

	# --- Item 6: return nets against origin bucket
	try:
		it = sc(6, "RETNET"); p = pr(it, 100, 10, today); dn(it, 40, today)
		pr(it, -20, 10, today, is_return=True, against=p.name, against_detail=p.items[0].name)
		b = ipb(it)
		result("6", "purchase return nets down In (receipt 80, issue 40)",
			flt(b.receipt_qty) == 80, f"receipt {b.receipt_qty}")
	except Exception as e:
		result("6", "return netting", False, str(e)[:120])

	# --- Item 8: backdated Stock Count uses prior-period on-hand + cascades
	try:
		it = sc(8, "BDCOUNT")
		pr(it, 100, 10, "2026-06-15")     # prior period (June)
		pr(it, 10, 10, today)             # current (July)
		scb = count(it, 90, "2026-06-20")  # count in June: 90 vs 100 -> -10
		jun = ipb(it, 2026, 6); cur = ipb(it)
		result("8", f"backdated count {scb.name}: June 90/900, current cascades to 100/1000",
			flt(jun.closing_qty) == 90 and flt(cur.closing_qty) == 100,
			f"Jun {jun.closing_qty} cur {cur.closing_qty}")
	except Exception as e:
		result("8", "backdated count carryover", False, str(e)[:120])

	# --- Item 10 + 13: reverse an invoice (debit note): un-bills, party GL reversed, no due-date error
	try:
		it = sc(10, "REVINV"); p = pr(it, 100, 10, today)
		inv = pi(it, p, 100, 12, today, bill_no=f"SUP-{T}", bill_date=today)  # supplier inv date -> #13 trap
		cx = cancel("Purchase Invoice", inv.name)
		pb = frappe.db.get_value("Purchase Receipt", p.name, ["per_billed", "status"])
		result("10/13", f"reverse invoice {inv.name} -> {cx.name}: receipt To Bill, MAP 10, no due-date error",
			flt(pb[0]) == 0 and pb[1] == "To Bill" and flt(ipb(it).moving_avg_price, 2) == 10, str(pb))
	except Exception as e:
		result("10/13", "reverse of invoice", False, str(e)[:120])

	# --- Item 11: reversing a receipt that has a return is blocked
	try:
		it = sc(11, "PRRET"); p = pr(it, 100, 10, today)
		pr(it, -30, 10, today, is_return=True, against=p.name, against_detail=p.items[0].name)
		try:
			cancel("Purchase Receipt", p.name)
			result("11", "reverse receipt-with-return", False, "NOT blocked")
		except frappe.ValidationError:
			result("11", f"reversing receipt {p.name} with a return is blocked (warn-first)", True)
	except Exception as e:
		result("11", "reverse receipt with return", False, str(e)[:120])

	# --- Item 12: reversing an invoiced receipt is blocked
	try:
		it = sc(12, "INVPR"); p = pr(it, 100, 10, today); pi(it, p, 100, 10, today)
		try:
			cancel("Purchase Receipt", p.name)
			result("12", "reverse invoiced receipt", False, "NOT blocked")
		except frappe.ValidationError:
			result("12", f"reversing invoiced receipt {p.name} is blocked (reverse invoice first)", True)
	except Exception as e:
		result("12", "reverse invoiced receipt", False, str(e)[:120])

	# --- Item 14: MAP cannot go negative (guard) — verified via a value event
	try:
		from sap_valuation.sap_moving_average.kernel import post_value_event
		it = sc(14, "NEGMAP"); pr(it, 100, 10, today); dn(it, 90, today)  # hold 10 @ 100
		srbnb = frappe.get_cached_value("Company", CO, "stock_received_but_not_billed")
		try:
			post_value_event(CO, it, WH, source=("Purchase Invoice", "_RT14", "x"),
				posting_date=today, reason="invoice_diff", value_delta=-300, offset_account=srbnb)
			result("14", "negative-MAP guard", False, "NOT blocked")
		except frappe.ValidationError:
			result("14", "a value event that would make MAP negative is blocked", True)
	except Exception as e:
		result("14", "negative-MAP guard", False, str(e)[:120])

	# --- Item 15: partial-qty invoice at a higher rate (qty-based billing)
	try:
		it = sc(15, "PARTBILL"); p = pr(it, 5000, 400, today)
		i1 = pi(it, p, 3000, 800, today)      # amount 2.4M > 2.0M receipt — used to block
		i2 = pi(it, p, 2000, 900, today)      # remaining qty at any rate
		blocked = False
		try:
			pi(it, p, 100, 500, today)        # 5100 > 5000 received
		except frappe.ValidationError:
			blocked = True
		result("15", f"partial bill {i1.name} @800 + remaining {i2.name} @900 post; over-qty blocked={blocked}",
			i1.docstatus == 1 and i2.docstatus == 1 and blocked)
	except Exception as e:
		result("15", "partial invoicing", False, str(e)[:120])

	# ---------------- summary
	npass = sum(1 for r in RESULTS if r[2])
	print(f"\n===== {npass}/{len(RESULTS)} re-entries behaved correctly (run tag {T}) =====")
	for issue, label, ok, detail in RESULTS:
		if not ok:
			print(f"  FAIL [{issue}] {label}: {detail}")
	if commit:
		frappe.db.commit()
		print(f"\nCOMMITTED. Filter items by code 'UAT-RT-%-{T}' to inspect the documents.")
	else:
		frappe.db.rollback()
		print("\n(dry run — rolled back; pass commit=True to persist)")
