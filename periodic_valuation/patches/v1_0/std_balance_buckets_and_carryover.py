# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Client tickets STD-013 (reopened 08/10/2026) and STD-015 (08/10/2026):
standard-cost period balances, rebuilt from the valuation events.

- Receipts / Issues by transaction, not by quantity sign (DR-31, as MAP): a
  purchase return nets Receipts, a sales return nets Issues, a cancellation
  nets its original's bucket.
- Opening fixed at period creation: what backdated postings into earlier
  months added to it after the row was created moves to Carryover.

Every row keeps its closing exactly; a row whose closing would change is
left as it is and reported. Display buckets only - no event or GL changes."""

import frappe
from frappe.utils import flt


def execute():
	from periodic_valuation.periodic_moving_average.kernel import recompute_closing
	from periodic_valuation.periodic_standard_cost.engine import StdEngine
	from periodic_valuation.periodic_standard_cost.kernel import bucket_of

	scopes = frappe.db.sql("""SELECT DISTINCT b.company, b.item_code, b.warehouse
		FROM `tabInventory Period Balance` b JOIN `tabItem` i ON i.name = b.item_code
		WHERE i.valuation_method = 'Periodic Standard Cost'""", as_dict=True)
	fixed, skipped = 0, []
	for s in scopes:
		engine = StdEngine(s.company, s.item_code, s.warehouse or None)
		events = frappe.get_all("Inventory Valuation Event",
			filters={**engine._scope_filters(), "std_trans": ("!=", "")},
			fields=["std_trans", "period_year", "period_month", "qty_adj", "total_sc", "reversal_of", "creation"])
		for row in frappe.get_all("Inventory Period Balance", filters={"company": s.company,
				"item_code": s.item_code, "warehouse": s.warehouse or ""}, fields=["name"]):
			ipb = frappe.get_doc("Inventory Period Balance", row.name)
			key = (ipb.period_year, ipb.period_month)
			before = (flt(ipb.closing_qty, 6), flt(ipb.closing_value, 2))
			rq = rv = iq = iv = 0.0
			for e in events:
				if (e.period_year, e.period_month) != key:
					continue
				b = bucket_of(e.std_trans)
				if b is None and e.reversal_of and e.std_trans.endswith(" - Rev"):
					# a cancellation books its companion's reversal with the
					# movement it belongs to (kernel._post_cancellation_std)
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
			later = [e for e in events if (e.period_year, e.period_month) < key and e.creation > ipb.creation]
			cq = sum(flt(e.qty_adj) for e in later)
			cv = sum(flt(e.total_sc) for e in later)
			new = frappe._dict(ipb.as_dict())
			new.update(receipt_qty=rq, receipt_value=flt(rv, 2), issue_qty=iq, issue_value=flt(iv, 2),
				opening_qty=flt(ipb.opening_qty) + flt(ipb.carryover_qty) - cq,
				opening_value=flt(flt(ipb.opening_value) + flt(ipb.carryover_value) - cv, 2),
				carryover_qty=cq, carryover_value=flt(cv, 2))
			recompute_closing(new)
			if (flt(new.closing_qty, 6), flt(new.closing_value, 2)) != before:
				skipped.append(f"{s.company} / {s.item_code} {key}: closing {before} -> "
					f"{(flt(new.closing_qty, 6), flt(new.closing_value, 2))}")
				continue
			fields = ("receipt_qty", "receipt_value", "issue_qty", "issue_value",
				"opening_qty", "opening_value", "carryover_qty", "carryover_value")
			if any(flt(new[f], 6) != flt(ipb.get(f), 6) for f in fields):
				frappe.db.set_value("Inventory Period Balance", ipb.name, {f: new[f] for f in fields},
					update_modified=False)
				fixed += 1
		if engine.view == "YTD":
			for y in {e.period_year for e in events}:
				engine.refresh_ipb_pools(y, 1)
	print(f"STD-013/015: {fixed} standard-cost period balances rebuilt; {len(skipped)} left as they were")
	for x in skipped:
		print("  left", x)
