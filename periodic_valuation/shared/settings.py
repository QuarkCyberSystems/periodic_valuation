# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Single accessor for Periodic Moving Average configuration.

Primary home for cross-cutting settings is core Accounts Settings via upstream
PR; until that lands the per-company `Periodic Moving Average Settings` doctype in
this app is authoritative. All kernel code reads through here so the storage
location can change without touching callers. The reads themselves go through
qcs_platform's one settings accessor (registered in platform.py), which caches
and enforces one record per company.
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


SETTINGS = "Periodic Moving Average Settings"


def get_pma_settings_doc(company):
	from qcs_platform.settings import require_company_setting

	return require_company_setting(SETTINGS, company)


def get_pma_setting(company, key):
	from qcs_platform.settings import company_setting

	value = company_setting(SETTINGS, company, key)
	if value is not None:
		return value
	return _DEFAULTS.get(key)


def get_return_valuation(company, doctype):
	"""Per-doctype override -> company default -> 'With Reference'."""
	from qcs_platform.settings import settings_doc

	doc = settings_doc(SETTINGS, company)
	if doc is not None:
		for row in doc.return_valuation_overrides or []:
			if row.document_type == doctype:
				return row.default_return_valuation
		if doc.default_return_valuation:
			return doc.default_return_valuation
	return _DEFAULTS["default_return_valuation"]
