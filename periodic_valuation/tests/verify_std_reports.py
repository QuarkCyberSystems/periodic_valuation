# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Module reports and form connections - bench --site <site> execute periodic_valuation.tests.verify_std_reports.run

Builds a small known scenario, then runs Inventory Book vs Reference,
Settlement Register and Cross Fiscal Year Late Entries through
frappe.desk.query_report.run and asserts the numbers; also asserts the
dashboard Connections added on Item and source documents. Rolled back
unless commit=True."""

import frappe
from frappe.utils import flt, getdate, nowdate

from periodic_valuation.tests.smoke_edges import make_dn, make_pr
from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company
from periodic_valuation.tests.smoke_std import ensure_std_masters
from periodic_valuation.tests.verify_std_design_tcs import scv_release, std_item

CHECKS = []


def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


def run_report(name, filters):
	from frappe.desk.query_report import run as qr_run
	res = qr_run(name, filters=filters, ignore_prepared_report=True)
	return [r for r in (res.get("result") or []) if isinstance(r, dict)]


def run(commit=False):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	wh = ensure_masters()
	company = get_company()
	ensure_std_masters(company)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	today = getdate(nowdate())

	# ---- scenario: STD item, receipt 100 @ 12 (SC 10), issue 20, month closed
	item = std_item("_RPT-STD-1")
	scv_release(company, item, today.year, today.month, 10)
	pr = make_pr(item, wh, 100, 12)
	make_dn(item, wh, 20)
	eng = StdEngine(company, item, wh)
	sett = eng.close_period(year=today.year, month=today.month, sc=10, source=("Item", item))

	# ---- back-year pair for the cross-FY report
	item2 = std_item("_RPT-STD-2")
	e2 = StdEngine(company, item2, wh)
	e2.post(trans="REC (BY)", posting_date="2025-12-10", qty=10, sc=8, ac=8,
		source=("Item", item2), entry_date=str(today))
	e2.post(trans="REC (BY) - Rev", posting_date=str(today), qty=10, sc=11,
		t_sc_override=30, source=("Item", item2))

	# ---- R1: Inventory Book vs Reference
	rows = run_report("Inventory Book vs Reference",
		{"company": company, "period_year": today.year, "period_month": today.month,
			"item_code": item})
	r = next((x for x in rows if x.get("item_code") == item), None)
	check("R1 row: 80 @ 10 -> reference 800, capitalized 160, book 960, check 0",
		r and flt(r["closing_qty"]) == 80 and flt(r["unit_cost"]) == 10
		and flt(r["reference_value"], 2) == 800 and flt(r["capitalized_variance"], 2) == 160
		and flt(r["book_value"], 2) == 960 and flt(r["check"], 2) == 0, str(r))
	check("R1 stock ledger value shows the reference figure (800)",
		r and flt(r["stock_ledger_value"], 2) == 800, str(r and r["stock_ledger_value"]))

	# ---- R2: Settlement Register, live then reversed
	rows = run_report("Settlement Register", {"company": company, "item_code": item})
	r = next((x for x in rows if x.get("settlement") == sett.name), None)
	totals = rows[-1] if rows else {}
	check("R2 live row: pool 200, split 160/40, status Live, run-linked events shown",
		r and flt(r["variance"], 2) == 200 and flt(r["es_var"], 2) == 160
		and flt(r["out_var"], 2) == 40 and r["status"] == "Live"
		and r["sett_event"] and r["sett_rev_event"], str(r))
	check("R2 live totals include the settlement",
		flt(totals.get("es_var"), 2) == 160 and flt(totals.get("out_var"), 2) == 40, str(totals))

	eng.sett_reverse(sett.name, source=("Item", item))
	rows = run_report("Settlement Register", {"company": company, "item_code": item})
	r = next((x for x in rows if x.get("settlement") == sett.name), None)
	totals = rows[-1] if rows else {}
	check("R2 after Reverse: row stays on the register flagged Reversed",
		r and r["status"] == "Reversed" and r["reversed_by_events"], str(r))
	check("R2 live totals now exclude it",
		flt(totals.get("es_var"), 2) == 0 and flt(totals.get("out_var"), 2) == 0, str(totals))
	rows = run_report("Settlement Register",
		{"company": company, "item_code": item, "status": "Live"})
	check("R2 status filter: no live rows for the item",
		not [x for x in rows if x.get("settlement") == sett.name], str(len(rows)))

	# ---- R3: Cross Fiscal Year Late Entries
	rows = run_report("Cross Fiscal Year Late Entries",
		{"company": company, "fiscal_year": 2025, "item_code": item2})
	check("R3 one row: REC (BY) 10 @ prior cost 8 (80) with bridge +30 this period",
		len(rows) == 1 and rows[0]["std_trans"] == "REC (BY)" and flt(rows[0]["qty_adj"]) == 10
		and flt(rows[0]["prior_sc"], 2) == 8 and flt(rows[0]["total_sc"], 2) == 80
		and flt(rows[0]["bridge_amount"], 2) == 30
		and rows[0]["bridge_period"] == f"{today.year}-{today.month:02d}", str(rows))

	# ---- Connections (dashboard links)
	from frappe.desk.notifications import get_open_count
	found = {d.get("doctype") for d in
		(get_open_count("Item", item).get("count") or {}).get("external_links_found", [])}
	expected = {"Item Standard Cost Version", "Inventory Period Settlement",
		"Inventory Period Balance", "Inventory Valuation Event"}
	check("Item form Connections list the valuation records",
		expected.issubset(found), str(sorted(found - expected))[:120] or str(sorted(found)))
	found = {d.get("doctype") for d in
		(get_open_count("Purchase Receipt", pr.name).get("count") or {}).get("external_links_found", [])}
	check("Purchase Receipt Connections list the valuation events",
		{"Inventory Valuation Event", "Stock Movement Event"}.issubset(found), str(sorted(found)))

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if failed:
		print("FAILED: " + "; ".join(x[0] for x in failed))
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
