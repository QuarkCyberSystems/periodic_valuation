# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Period resolution and posting-eligibility checks for the periodic valuation kernels.

Posting is allowed only to the current OPEN period and — while it is still
PREV_OPEN_UNSETTLED — the previous period. Anything older is rejected: a closed
period is not reopened (DR-21), so the correction is posted in the current open
period at current valuation. (The ``original_period`` reach-back from the pre-
DR-21 plan is not used — see DR-21.)
"""

import frappe
from frappe import _
from frappe.utils import getdate

POSTING_ALLOWED_STATES = ("OPEN", "PREV_OPEN_UNSETTLED")


def get_period(company, posting_date):
	"""Return the Inventory Period row covering posting_date, or None."""
	d = getdate(posting_date)
	name = frappe.db.get_value(
		"Inventory Period",
		{"company": company, "period_year": d.year, "period_month": d.month},
	)
	return frappe.get_doc("Inventory Period", name) if name else None


def get_open_period(company):
	name = frappe.db.get_value("Inventory Period", {"company": company, "status": "OPEN"})
	return frappe.get_doc("Inventory Period", name) if name else None


def assert_posting_allowed(company, posting_date):
	"""Throw unless posting_date falls in an OPEN or PREV_OPEN_UNSETTLED period.

	Returns the Inventory Period document on success.
	"""
	period = get_period(company, posting_date)
	if not period:
		frappe.throw(
			_("No Inventory Period covers {0} for {1}. Create the period before posting periodic-valuation items.").format(
				posting_date, company
			),
			title=_("No Inventory Period"),
		)
	if period.status not in POSTING_ALLOWED_STATES:
		frappe.throw(
			_(
				"Inventory Period {0} is {1} and no longer accepts postings — a closed period "
				"is not reopened. Post the correction in the current open period; only the "
				"immediately-previous period stays open for backdated entries."
			).format(period.period_name, period.status),
			title=_("Period Locked"),
		)
	return period


def period_key(posting_date):
	d = getdate(posting_date)
	return d.year, d.month
