# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Cost Adjustment tree v5, "in next period" (client 06/10/2026; DR-53) -
bench --site <site> execute periodic_valuation.tests.smoke_cost_adjustment_carry.run

A cost adjustment dated in the previous period carries only its inventory
portion into the current period, which keeps it only as far as its own stock
can; the rest posts as revaluation (Dr/Cr Inventory against the revaluation
account) on day 1 of the current period:

  A   current negative before and after: the whole carried -150 goes to
      revaluation, the period keeps -20 / -200 at its frozen MAP
  A+  the same with a +150 increase
  B   current positive before (400), negative after (-300): inventory lands
      on zero, -300 to revaluation
  C   current positive before and after: plain carry, nothing revalued
  Z   no stock in the current period: the whole carry to revaluation
  S   a same-period adjustment carries nothing
  L   a landed cost into the previous period, current negative: revalued
  R   cancellations restore every case exactly - dated today and dated in
      the previous period; the revaluation leg reverses with them
  every case: inventory GL equals the valuation events per period and the
  stock ledger equals the current balance

Rolled back unless commit=True."""

import frappe
from frappe.utils import add_months, flt, get_first_day, nowdate

from periodic_valuation.periodic_moving_average.kernel import get_offset_account
from periodic_valuation.shared.accounts import get_inventory_account
from periodic_valuation.tests.smoke_edges import COMPANY, ipb_period, make_dn, make_item, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def make_pi(item, wh, pr, qty, rate, posting_date):
	pi = frappe.get_doc({"doctype": "Purchase Invoice", "company": COMPANY,
		"supplier": "_SMK Supplier", "posting_date": posting_date, "set_posting_time": 1,
		"items": [{"item_code": item, "qty": qty, "rate": rate, "warehouse": wh,
			"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}]})
	pi.insert(ignore_permissions=True)
	pi.submit()
	return pi


def cancel(doctype, name, posting_date=None):
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation

	cx = frappe.get_doc(doctype, make_cancellation(doctype, name))
	if posting_date:
		cx.posting_date = posting_date
		cx.set_posting_time = 1
		cx.save(ignore_permissions=True)
	cx.submit()
	return cx


def carry_events(source_docname):
	return frappe.get_all("Inventory Valuation Event",
		filters={"source_docname": source_docname, "reason_code": "carry_revaluation", "is_cancelled": 0},
		fields=["name", "value_delta", "posting_date", "period_month", "caused_by_event_id"])


def gl_net(item, account):
	return flt(frappe.db.sql(
		"""select coalesce(sum(g.debit - g.credit), 0) from `tabGL Entry` g
		join `tabInventory Valuation Event` e on e.name = g.valuation_event_id
		where e.item_code = %s and g.account = %s and g.is_cancelled = 0""", (item, account))[0][0], 2)


def sle_value(item):
	return flt(frappe.db.sql(
		"select coalesce(sum(stock_value_difference), 0) from `tabStock Ledger Entry` "
		"where item_code = %s and is_cancelled = 0", item)[0][0], 2)


def run(commit=False):
	frappe.db.savepoint("ca_carry")
	try:
		_run()
	finally:
		if commit:
			frappe.db.commit()
		else:
			frappe.db.rollback(save_point="ca_carry")
	failed = [c for c in CHECKS if not c[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		raise Exception("Cost adjustment carry failures: " + "; ".join(c[0] for c in failed))


def _run():
	from periodic_valuation.shared.period_close import assert_event_gl_identity

	wh = ensure_masters()
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	prior = get_first_day(add_months(nowdate(), -1))
	cur = get_first_day(nowdate())
	if not frappe.db.exists("Inventory Period", {"company": COMPANY, "period_name": prior.strftime("%Y-%m")}):
		frappe.get_doc({"doctype": "Inventory Period", "company": COMPANY,
			"start_date": prior, "status": "PREV_OPEN_UNSETTLED"}).insert(ignore_permissions=True)
	py, pm, cy, cm = prior.year, prior.month, cur.year, cur.month
	d5, d7, d9 = (str(prior.replace(day=n)) for n in (5, 7, 9))

	def state(it):
		p, c = ipb_period(it, py, pm), ipb_period(it, cy, cm)
		return p, c

	def consistent(label, it):
		c = ipb_period(it, cy, cm)
		periods = [frappe.get_doc("Inventory Period", n) for n in frappe.get_all("Inventory Period",
			filters={"company": COMPANY, "period_year": ("in", (py, cy)), "period_month": ("in", (pm, cm))},
			pluck="name")]
		ident = all(assert_event_gl_identity(p)["ok"] for p in periods)
		stock = get_inventory_account(COMPANY, it, wh)
		check(f"{label}: GL = valuation events per period; stock ledger and inventory GL = current balance",
			ident and sle_value(it) == flt(c.closing_value, 2) and gl_net(it, stock) == flt(c.closing_value, 2),
			f"ident {ident} sle {sle_value(it)} gl {gl_net(it, stock)} closing {c.closing_value}")

	# ---- A: current negative before and after, -150 -------------------
	a = make_item("_CA-A")
	reval_acct = get_offset_account(COMPANY, a, wh, "revaluation")
	pr = make_pr(a, wh, 100, 10, posting_date=d5)            # prior 100 / 1,000
	make_dn(a, wh, 120)                                       # current -20 / -200, frozen 10
	pi_a = make_pi(a, wh, pr, 100, 8.5, d7)                   # backdated diff -150, fully covered in the prior period
	p, c = state(a)
	ev = carry_events(pi_a.name)
	check("A: the prior period takes the -150 (1,000 -> 850)", flt(p.closing_value, 2) == 850, str(p.closing_value))
	check("A: the current period keeps -20 / -200 at frozen MAP 10",
		flt(c.closing_qty) == -20 and flt(c.closing_value, 2) == -200 and flt(c.frozen_map, 4) == 10,
		f"{c.closing_qty}/{c.closing_value}/{c.frozen_map}")
	check("A: one revaluation leg +150 on day 1 of the current period, linked to the carried event",
		len(ev) == 1 and flt(ev[0].value_delta, 2) == 150 and str(ev[0].posting_date) == str(cur)
		and ev[0].caused_by_event_id, str(ev))
	check("A: Dr Stock 150 / Cr Revaluation 150", gl_net(a, reval_acct) == -150, str(gl_net(a, reval_acct)))
	consistent("A", a)

	# ---- A+: current negative, +150 --------------------------------------
	ap = make_item("_CA-AP")
	pr = make_pr(ap, wh, 100, 10, posting_date=d5)
	make_dn(ap, wh, 120)
	pi_ap = make_pi(ap, wh, pr, 100, 11.5, d7)
	p, c = state(ap)
	check("A+: a +150 increase into a negative period goes to revaluation too; current stays -200",
		flt(c.closing_value, 2) == -200 and [flt(e.value_delta, 2) for e in carry_events(pi_ap.name)] == [-150]
		and gl_net(ap, reval_acct) == 150, f"{c.closing_value} {carry_events(pi_ap.name)}")
	consistent("A+", ap)

	# ---- B: positive before, negative after --------------------------------
	b = make_item("_CA-B")
	pr = make_pr(b, wh, 100, 20, posting_date=d5)             # prior 100 / 2,000
	make_dn(b, wh, 80)                                        # current 20 / 400
	pi_b = make_pi(b, wh, pr, 100, 13, d7)                    # diff -700, prior 2,000 -> 1,300
	p, c = state(b)
	check("B: prior 1,300; current lands on zero (400 - 700 + 300), 20 units",
		flt(p.closing_value, 2) == 1300 and flt(c.closing_value, 2) == 0 and flt(c.closing_qty) == 20,
		f"{p.closing_value} {c.closing_value} {c.closing_qty}")
	check("B: the part below zero, -300, goes to revaluation (Dr Stock 300 / Cr Revaluation 300)",
		[flt(e.value_delta, 2) for e in carry_events(pi_b.name)] == [300] and gl_net(b, reval_acct) == -300,
		str(carry_events(pi_b.name)))
	consistent("B", b)

	# ---- C: positive before and after --------------------------------------
	cc = make_item("_CA-C")
	pr = make_pr(cc, wh, 100, 20, posting_date=d5)
	make_dn(cc, wh, 50)                                       # current 50 / 1,000
	pi_c = make_pi(cc, wh, pr, 100, 13, d7)
	p, c = state(cc)
	check("C: a carry the current stock can hold stays in inventory (1,000 - 700 = 300), nothing revalued",
		flt(c.closing_value, 2) == 300 and not carry_events(pi_c.name), f"{c.closing_value}")
	consistent("C", cc)

	# ---- Z: no stock in the current period -----------------------------------
	z = make_item("_CA-Z")
	pr = make_pr(z, wh, 100, 20, posting_date=d5)
	make_dn(z, wh, 100)                                       # current 0 / 0
	pi_z = make_pi(z, wh, pr, 100, 13, d7)
	p, c = state(z)
	check("Z: no stock now - the whole -700 to revaluation, current stays 0 / 0",
		flt(c.closing_value, 2) == 0 and flt(c.closing_qty) == 0
		and [flt(e.value_delta, 2) for e in carry_events(pi_z.name)] == [700], f"{c.closing_value}")
	consistent("Z", z)

	# ---- S: same-period adjustment ----------------------------------------------
	s = make_item("_CA-S")
	pr = make_pr(s, wh, 100, 10)
	make_dn(s, wh, 120)
	pi_s = make_pi(s, wh, pr, 100, 8.5, nowdate())
	check("S: an adjustment dated in the current period carries nothing", not carry_events(pi_s.name))

	# ---- L: landed cost into the previous period, current negative --------------
	el = make_item("_CA-L")
	pr = make_pr(el, wh, 100, 10, posting_date=d5)
	make_dn(el, wh, 120)                                      # current -20 / -200
	exp_acct = frappe.get_all("Account", filters={"company": COMPANY, "is_group": 0, "root_type": "Expense"},
		limit=1, pluck="name")[0]
	lcv = frappe.get_doc({"doctype": "Landed Cost Voucher", "company": COMPANY,
		"posting_date": d7, "distribute_charges_based_on": "Amount",
		"purchase_receipts": [{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name,
			"supplier": "_SMK Supplier", "grand_total": pr.grand_total}],
		"taxes": [{"expense_account": exp_acct, "description": "freight", "amount": 200}]})
	lcv.get_items_from_purchase_receipts()
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	p, c = state(el)
	check("L: a landed cost of 200 into the previous period; the negative current period revalues it",
		flt(p.closing_value, 2) == 1200 and flt(c.closing_value, 2) == -200
		and [flt(e.value_delta, 2) for e in carry_events(lcv.name)] == [-200], f"{p.closing_value} {c.closing_value}")
	consistent("L", el)

	# ---- R: cancellations restore exactly -------------------------------------
	cancel("Purchase Invoice", pi_b.name)                     # dated today
	p, c = state(b)
	check("R: B cancelled today - current back to 20 / 400, revaluation account nets to zero",
		flt(c.closing_value, 2) == 400 and gl_net(b, reval_acct) == 0, f"{c.closing_value} {gl_net(b, reval_acct)}")
	consistent("R-B", b)
	cancel("Purchase Invoice", pi_a.name, posting_date=d9)    # dated in the previous period
	p, c = state(a)
	check("R: A cancelled in the previous period - prior back to 1,000, current -200, revaluation nets to zero",
		flt(p.closing_value, 2) == 1000 and flt(c.closing_value, 2) == -200 and gl_net(a, reval_acct) == 0,
		f"{p.closing_value} {c.closing_value} {gl_net(a, reval_acct)}")
	consistent("R-A", a)
	cancel("Purchase Invoice", pi_ap.name)
	p, c = state(ap)
	check("R: A+ cancelled - current -200, revaluation nets to zero",
		flt(c.closing_value, 2) == -200 and gl_net(ap, reval_acct) == 0, f"{c.closing_value} {gl_net(ap, reval_acct)}")
	consistent("R-A+", ap)
	cancel("Landed Cost Voucher", lcv.name)
	p, c = state(el)
	check("R: L cancelled - current -200, revaluation nets to zero",
		flt(c.closing_value, 2) == -200 and gl_net(el, reval_acct) == 0, f"{c.closing_value} {gl_net(el, reval_acct)}")
	consistent("R-L", el)
