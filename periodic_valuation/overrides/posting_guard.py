# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Refuse at Save what the kernel would refuse at Submit (client ticket
STD-016, reopened 11/10/2026: "the validation ... should be on Save not the
submit ... our team settled October, then made a return in October: the
system allowed the draft and refused the submit").

Two checks, for a document carrying periodic-valuation items:
- the posting calendars, asked through the platform (Inventory Period and
  Accounting Period): a month that no longer takes postings;
- for a standard-cost item, its own settlement: a month already settled for
  the item takes no posting until the settlement is reversed.
"""

import frappe
from frappe import _
from frappe.utils import getdate


def validate_posting_open(doc, method=None):
	if doc.docstatus == 2 or frappe.flags.in_patch or frappe.flags.in_migrate:
		return
	posting_date = doc.get("posting_date")
	if not doc.get("company") or not posting_date:
		return
	from qcs_platform.core.valuation_sockets import routed_items

	items = routed_items(doc)
	if not items:
		return
	from qcs_platform.period_authority import check

	check(doc, posting_date)
	_refuse_settled_std_scopes(doc, items, getdate(posting_date))


def _refuse_settled_std_scopes(doc, items, day):
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	std = {i for i in items if frappe.get_cached_value("Item", i, "valuation_method") == "Periodic Standard Cost"}
	if not std:
		return
	settled, seen = [], set()
	for row in doc.get("items") or []:
		if row.get("item_code") not in std:
			continue
		wh = row.get("warehouse") or row.get("t_warehouse") or row.get("s_warehouse")
		engine = StdEngine(doc.company, row.item_code, wh)
		key = (row.item_code, engine.warehouse or "")
		if key in seen:
			continue
		seen.add(key)
		if engine.is_period_locked(day.year, day.month):
			sett = frappe.db.get_value("Inventory Period Settlement", {
				"company": doc.company, "item_code": row.item_code, "warehouse": engine.warehouse or "",
				"period_year": day.year, "period_month": day.month, "cancelled": 0})
			settled.append(_("{0} ({1})").format(row.item_code, sett) if sett else row.item_code)
	if settled:
		frappe.throw(
			_("{0}-{1:02d} is already settled for: {2}. Reverse the settlement first, or date this document in the next open month.").format(
				day.year, day.month, ", ".join(settled)),
			title=_("Period Settled"),
		)
