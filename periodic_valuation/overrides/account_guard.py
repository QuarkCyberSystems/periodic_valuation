# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""An inventory account cannot change while the items it values hold stock
(DR-69; SAP: a material's valuation class - its account determination -
cannot change while it has stock). On 04/10/2026 the account setup of two
standard-cost items was changed for a few hours while they held stock: their
stock postings went to a fixed-asset account, the setup was then put back,
and the value stayed where the period balances and the reconciliation gate
no longer look for it."""

import frappe
from frappe import _

DEFAULT_TABLES = {"Item": "item_defaults", "Item Group": "item_group_defaults", "Brand": "brand_defaults"}
WAREHOUSE_TABLES = {"Item": "item_default_warehouse_accounts",
	"Item Group": "item_group_default_warehouse_accounts"}


def guard_inventory_account(doc, method=None):
	if doc.is_new() or frappe.flags.in_patch or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	companies = _changed_companies(doc, before)
	if not companies:
		return
	held = []
	for company in companies:
		held += _items_holding_stock(company, _covered_items(doc))
	if held:
		frappe.throw(
			_("The inventory account cannot change while these items hold stock or value: {0}. Their stock is valued on the current account; move it out first (issue or count to zero) or keep the account.").format(
				", ".join(sorted(set(held))[:10]) + (" ..." if len(set(held)) > 10 else "")),
			title=_("Inventory Account In Use"),
		)


def _changed_companies(doc, before):
	if doc.doctype == "Company":
		return {doc.name} if (before.get("default_inventory_account") or None) != (doc.get("default_inventory_account") or None) else set()
	changed = set()
	table = DEFAULT_TABLES.get(doc.doctype)
	if table:
		old = {r.company: r.get("default_inventory_account") or None for r in before.get(table) or []}
		new = {r.company: r.get("default_inventory_account") or None for r in doc.get(table) or []}
		changed |= {c for c in set(old) | set(new) if old.get(c) != new.get(c)}
	wtable = WAREHOUSE_TABLES.get(doc.doctype)
	if wtable:
		old = {(r.company, r.warehouse): r.get("default_inventory_account") or None for r in before.get(wtable) or []}
		new = {(r.company, r.warehouse): r.get("default_inventory_account") or None for r in doc.get(wtable) or []}
		changed |= {k[0] for k in set(old) | set(new) if old.get(k) != new.get(k)}
	return changed


def _covered_items(doc):
	"""Items whose inventory account this record can decide (conservative: an
	item-level override below a group or company default is not excluded)."""
	if doc.doctype == "Item":
		return [doc.name]
	if doc.doctype == "Item Group":
		return frappe.get_all("Item", filters={"item_group": doc.name}, pluck="name")
	if doc.doctype == "Brand":
		return frappe.get_all("Item", filters={"brand": doc.name}, pluck="name")
	return None  # Company: every item


def _items_holding_stock(company, items):
	if items is not None and not items:
		return []
	cond = "AND b.item_code IN %(items)s" if items is not None else ""
	return frappe.db.sql(f"""
		SELECT DISTINCT b.item_code FROM `tabInventory Period Balance` b
		JOIN `tabInventory Period` p ON p.company = b.company AND p.period_year = b.period_year
			AND p.period_month = b.period_month AND p.status IN ('OPEN', 'PREV_OPEN_UNSETTLED')
		WHERE b.company = %(company)s {cond}
			AND (ABS(b.closing_qty) > 0.000001 OR ABS(b.closing_value) > 0.005)
		""", {"company": company, "items": tuple(items or [""])}, pluck=True)
