# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Single accessor for Periodic Moving Average configuration.

Primary home for cross-cutting settings is core Accounts Settings via upstream
PR; until that lands the per-company `Periodic Moving Average Settings` doctype in
this app is authoritative. All kernel code reads through here so the storage
location can change without touching callers.
"""

import frappe
from frappe import _

_DEFAULTS = {
	"negative_stock_allowed": 0,
	"default_return_valuation": "With Reference",
	"enable_period_balance_audit_log": 0,
	"rounding_tolerance": 0.01,
	"reconciliation_tolerance": 0.0,
}


def get_pma_settings_doc(company):
	name = frappe.db.get_value("Periodic Moving Average Settings", {"company": company})
	if not name:
		frappe.throw(
			_("Periodic Moving Average Settings not configured for company {0}.").format(company),
			title=_("Missing Configuration"),
		)
	return frappe.get_cached_doc("Periodic Moving Average Settings", name)


def get_pma_setting(company, key):
	name = frappe.db.get_value("Periodic Moving Average Settings", {"company": company})
	if name:
		value = frappe.db.get_value("Periodic Moving Average Settings", name, key)
		if value is not None:
			return value
	if key in _DEFAULTS:
		return _DEFAULTS[key]
	return None


def get_return_valuation(company, doctype):
	"""Per-doctype override -> company default -> 'With Reference'."""
	name = frappe.db.get_value("Periodic Moving Average Settings", {"company": company})
	if name:
		doc = frappe.get_cached_doc("Periodic Moving Average Settings", name)
		for row in doc.return_valuation_overrides or []:
			if row.document_type == doctype:
				return row.default_return_valuation
		if doc.default_return_valuation:
			return doc.default_return_valuation
	return _DEFAULTS["default_return_valuation"]
