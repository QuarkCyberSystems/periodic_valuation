# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""DR-69 data repair: inventory GL and the period balances agree again
(UAT badiav16, 15/10/2026: the reconciliation gate failed in both open
months). Per company, in this order, with the reconciliation gate run for
each open month before and after:

1. settlements of the OPEN month are reversed - they posted their carry into
   a month with no period (a month is settled once the next is open);
2. stock legs posted to an account that is not the item's inventory account
   (the account setup was changed while the item held stock) are moved to it
   by a kernel-tagged transfer (reason account_transfer), dated in the month
   they belong to;
3. each standard-cost balance of an open month is rebuilt from its events -
   Opening as at the period's opening, Carryover, Receipts / Issues by
   transaction, the rest Revaluation - so it equals the event ledger, which
   equals the GL.

Rolled back for the company if any month's discrepancy grows. Every change
is listed in a comment on its Inventory Period."""

import calendar
from datetime import date

import frappe
from frappe.utils import flt, getdate, now_datetime, nowdate


def execute():
	from periodic_valuation.shared.period_close import run_reconciliation_gate

	frappe.reload_doc("periodic_valuation", "doctype", "inventory_valuation_event")
	for company in frappe.get_all("Inventory Period", filters={"status": "OPEN"}, pluck="company", distinct=True):
		periods = [frappe.get_doc("Inventory Period", n) for n in frappe.get_all("Inventory Period",
			filters={"company": company, "status": ("in", ("OPEN", "PREV_OPEN_UNSETTLED"))},
			order_by="period_year asc, period_month asc", pluck="name")]
		before = {p.name: run_reconciliation_gate(p)["discrepancy"] for p in periods}
		frappe.db.savepoint("dr69_repair")
		log = {p.name: [] for p in periods}
		_reverse_open_month_settlements(company, periods, log)
		_transfer_misrouted_stock(company, periods, log)
		_rebuild_std_balances(company, periods, log)
		after = {p.name: run_reconciliation_gate(p)["discrepancy"] for p in periods}
		if any(abs(flt(after[n])) > abs(flt(before[n])) + 0.005 for n in before):
			frappe.db.rollback(save_point="dr69_repair")
			print(f"DR-69 {company}: a discrepancy grew {before} -> {after}; rolled back, nothing written")
			continue
		for p in periods:
			if log[p.name]:
				frappe.get_doc("Inventory Period", p.name).add_comment("Comment",
					f"<p><b>DR-69 reconciliation repair</b>: discrepancy before {flt(before[p.name], 2)}, "
					f"after {flt(after[p.name], 2)}.</p><ul>" + "".join(f"<li>{x}</li>" for x in log[p.name]) + "</ul>")
		print(f"DR-69 {company}: discrepancy {before} -> {after}; {sum(len(v) for v in log.values())} changes")


def _month_end(p):
	return date(p.period_year, p.period_month, calendar.monthrange(p.period_year, p.period_month)[1])


def _reverse_open_month_settlements(company, periods, log):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	open_p = next(p for p in periods if p.status == "OPEN")
	for s in frappe.get_all("Inventory Period Settlement", filters={"company": company, "cancelled": 0,
			"period_year": open_p.period_year, "period_month": open_p.period_month},
			fields=["name", "item_code", "warehouse"]):
		StdEngine(company, s.item_code, s.warehouse or None).sett_reverse(
			s.name, source=("Inventory Period Settlement", s.name))
		log[open_p.name].append(f"{s.item_code}: settlement {s.name} of the open month reversed - settle it once the next month is open")


def _transfer_misrouted_stock(company, periods, log):
	"""A standard-cost event's stock leg is the GL line equal to its value; if
	that line is on an account other than the item's inventory account, the
	value is moved to the inventory account."""
	from erpnext.accounts.general_ledger import make_gl_entries

	from periodic_valuation.shared.accounts import get_inventory_account
	from periodic_valuation.shared.immutable import KERNEL_FLAG

	moved = {}
	for e in frappe.db.sql("""SELECT e.name, e.item_code, e.warehouse, e.total_sc, e.period_year, e.period_month
			FROM `tabInventory Valuation Event` e JOIN `tabItem` i ON i.name = e.item_code
			WHERE e.company = %s AND e.is_cancelled = 0 AND COALESCE(e.std_trans, '') != ''
				AND ABS(e.total_sc) > 0.005 AND i.valuation_method = 'Periodic Standard Cost'""", company, as_dict=True):
		inv = get_inventory_account(company, e.item_code, e.warehouse)
		lines = frappe.db.sql("""SELECT account, SUM(debit - credit) v FROM `tabGL Entry`
			WHERE valuation_event_id = %s AND is_cancelled = 0 GROUP BY account""", e.name, as_dict=True)
		if any(l.account == inv and abs(flt(l.v) - flt(e.total_sc)) < 0.01 for l in lines):
			continue
		other = next((l for l in lines if l.account != inv and abs(flt(l.v) - flt(e.total_sc)) < 0.01
			and frappe.get_cached_value("Account", l.account, "root_type") == "Asset"), None)
		if not other:
			continue
		key = (e.item_code, e.warehouse, inv, other.account, e.period_year, e.period_month)
		moved[key] = flt(moved.get(key, 0) + flt(e.total_sc), 2)

	open_p = next(p for p in periods if p.status == "OPEN")
	cost_center = frappe.get_cached_value("Company", company, "cost_center")
	for (item, wh, inv, wrong, y, m), amount in moved.items():
		if not amount:
			continue
		target = next((p for p in periods if (p.period_year, p.period_month) == (y, m)), open_p)
		day = getdate(nowdate()) if target.status == "OPEN" else _month_end(target)
		frappe.flags[KERNEL_FLAG] = True
		try:
			ive = frappe.get_doc({"doctype": "Inventory Valuation Event", "company": company, "item_code": item,
				"warehouse": wh, "period_year": day.year, "period_month": day.month, "posting_date": day,
				"entry_date": now_datetime(), "source_doctype": "Inventory Period", "source_docname": target.name,
				"reason_code": "account_transfer", "posting_intent": "SYSTEM_GENERATED", "std_trans": "",
				"qty_adj": 0, "qty_basis": 0, "total_sc": 0, "total_ac": 0, "value_delta": 0}).insert(ignore_permissions=True)
		finally:
			frappe.flags[KERNEL_FLAG] = False
		common = {"company": company, "posting_date": day, "voucher_type": "Inventory Period",
			"voucher_no": target.name, "cost_center": cost_center, "valuation_event_id": ive.name,
			"remarks": f"DR-69: {item} stock value posted to {wrong} moved to {inv}"}
		gl = [frappe._dict(account=inv, debit=amount if amount > 0 else 0, credit=-amount if amount < 0 else 0,
				debit_in_account_currency=amount if amount > 0 else 0, credit_in_account_currency=-amount if amount < 0 else 0, **common),
			frappe._dict(account=wrong, debit=-amount if amount < 0 else 0, credit=amount if amount > 0 else 0,
				debit_in_account_currency=-amount if amount < 0 else 0, credit_in_account_currency=amount if amount > 0 else 0, **common)]
		make_gl_entries(gl, merge_entries=False)
		log[target.name].append(f"{item}: {amount} posted to {wrong} moved to {inv} ({ive.name}, {day})")


def _rebuild_std_balances(company, periods, log):
	from periodic_valuation.periodic_moving_average.kernel import ScopeState
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.periodic_standard_cost.kernel import bucket_of

	scopes = frappe.db.sql("""SELECT DISTINCT b.item_code, b.warehouse FROM `tabInventory Period Balance` b
		JOIN `tabItem` i ON i.name = b.item_code
		WHERE b.company = %s AND i.valuation_method = 'Periodic Standard Cost'""", company, as_dict=True)
	for s in scopes:
		engine = StdEngine(company, s.item_code, s.warehouse or None)
		events = frappe.get_all("Inventory Valuation Event",
			filters={**engine._scope_filters(), "std_trans": ("!=", "")},
			fields=["std_trans", "period_year", "period_month", "qty_adj", "total_sc", "reversal_of", "creation", "source_docname"])
		moved_months = {(e.source_docname, e.period_year, e.period_month) for e in events
			if e.reversal_of and bucket_of(e.std_trans)}
		scope = ScopeState(company, s.item_code, s.warehouse or None)
		for p in periods:
			key = (p.period_year, p.period_month)
			name = frappe.db.get_value("Inventory Period Balance", scope._filters(*key))
			if not name:
				continue
			row = frappe.get_doc("Inventory Period Balance", name)
			earlier = [e for e in events if (e.period_year, e.period_month) < key]
			late = [e for e in earlier if e.creation > p.creation]
			cq, cv = sum(flt(e.qty_adj) for e in late), sum(flt(e.total_sc) for e in late)
			oq, ov = sum(flt(e.qty_adj) for e in earlier) - cq, sum(flt(e.total_sc) for e in earlier) - cv
			rq = rv = iq = iv = 0.0
			month = [e for e in events if (e.period_year, e.period_month) == key]
			for e in month:
				b = bucket_of(e.std_trans)
				if b is None and e.reversal_of and e.std_trans.endswith(" - Rev") \
						and (e.source_docname, e.period_year, e.period_month) in moved_months:
					b = bucket_of(e.std_trans[: -len(" - Rev")])
					if b == "receipt":
						rv += flt(e.total_sc)
					elif b == "issue":
						iv -= flt(e.total_sc)
					continue
				if b == "receipt":
					rq += flt(e.qty_adj); rv += flt(e.total_sc)
				elif b == "issue":
					iq -= flt(e.qty_adj); iv -= flt(e.total_sc)
			total_q = oq + cq + sum(flt(e.qty_adj) for e in month)
			total_v = ov + cv + sum(flt(e.total_sc) for e in month)
			adj_q, adj_v = flt(row.adjust_qty), flt(row.adjust_value)
			if abs((oq + cq + rq - iq + adj_q) - total_q) > 0.000001:
				log[p.name].append(f"{s.item_code}: quantity does not rebuild from events - left as it was")
				continue
			reval = total_v - ov - cv - rv + iv - adj_v - flt(row.prd_value)
			new = {"opening_qty": oq, "opening_value": flt(ov, 2), "carryover_qty": cq, "carryover_value": flt(cv, 2),
				"receipt_qty": rq, "receipt_value": flt(rv, 2), "issue_qty": iq, "issue_value": flt(iv, 2),
				"reval_value": flt(reval, 2), "closing_qty": total_q, "closing_value": flt(total_v, 2)}
			old = {k: flt(row.get(k), 6) for k in new}
			if all(abs(flt(new[k], 6) - old[k]) < 0.005 for k in new):
				continue
			frappe.db.set_value("Inventory Period Balance", name, new, update_modified=False)
			log[p.name].append(f"{s.item_code}: closing {flt(old['closing_qty'])} / {flt(old['closing_value'], 2)} "
				f"-> {flt(total_q)} / {flt(total_v, 2)} (rebuilt from its events)")
		if engine.view == "YTD":
			for p in periods:
				engine.refresh_ipb_pools(p.period_year, p.period_month)
