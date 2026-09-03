# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Gated reopen of a closed Inventory Period (DR-45, the period-machine
counterpart of DR-08).

DR-08 promises that the immediately-previous month can be reopened - Dec
from Jan, never from Feb - and anything older takes the forward-correction
path. The period machine (DR-37) froze a month through Inventory Period
Close with no way back, which foreclosed that promise. This document is the
way back, under the same window:

- only a SETTLED_FROZEN period,
- only the month immediately before the company's OPEN period (so the
  machine is back to its legal two-open-months shape: reopened month
  PREV_OPEN_UNSETTLED, current month OPEN - never three),
- a mandatory reason, stamped on the period with who/when and a running
  reopen count,
- forward-only: the reopen document cannot be cancelled; the month is closed
  again through Inventory Period Close, which re-runs every gate. While the
  month stays reopened, the machine cannot roll into the month after the
  current one (the two-open-months rule), so a reopen is self-limiting.

Scope settlements (STD) are untouched: each stays locked until its own
Sett-Reverse, exactly as DR-08 describes; MAP scopes simply become postable
for backdated entries again."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class InventoryPeriodReopen(Document):
	def validate(self):
		period = frappe.get_doc("Inventory Period", self.inventory_period)
		if period.company != self.company:
			frappe.throw(_("Inventory Period {0} does not belong to {1}.").format(period.name, self.company))
		if period.status != "SETTLED_FROZEN":
			frappe.throw(
				_("Only a closed period (SETTLED_FROZEN) can be reopened; {0} is {1}.").format(
					period.period_name, period.status
				),
				title=_("Period Not Closed"),
			)
		open_name = frappe.db.get_value("Inventory Period", {"company": self.company, "status": "OPEN"})
		if not open_name:
			frappe.throw(_("{0} has no OPEN Inventory Period; nothing to reopen against.").format(self.company))
		open_period = frappe.get_doc("Inventory Period", open_name)
		other_prev = frappe.db.get_value(
			"Inventory Period", {"company": self.company, "status": "PREV_OPEN_UNSETTLED"}, "period_name"
		)
		if other_prev:
			frappe.throw(
				_(
					"{0} is still open as the previous period. At most two periods may be open - close {0} "
					"(Inventory Period Close) before reopening {1}."
				).format(other_prev, period.period_name),
				title=_("Close the Previous Period First"),
			)
		py, pm = (
			(open_period.period_year - 1, 12) if open_period.period_month == 1
			else (open_period.period_year, open_period.period_month - 1)
		)
		if (period.period_year, period.period_month) != (py, pm):
			frappe.throw(
				_(
					"Only the immediately-previous month can be reopened (DR-08): the open period is {0}, "
					"so only {1}-{2:02d} qualifies. {3} is older and stays closed - post a current-dated "
					"correction in the open period instead."
				).format(open_period.period_name, py, pm, period.period_name),
				title=_("Outside the Reopen Window"),
			)
		self.status_before = period.status
		self.closed_by_before = period.closed_by
		self.closed_on_before = period.closed_on
		self.reopen_sequence = int(period.get("reopen_count") or 0) + 1

	def on_submit(self):
		period = frappe.get_doc("Inventory Period", self.inventory_period)
		# the same direct write Inventory Period Close uses for the freeze: the
		# state machine's own transition table stays closed to ad-hoc edits
		frappe.db.set_value("Inventory Period", period.name, {
			"status": "PREV_OPEN_UNSETTLED",
			"reopened_by": frappe.session.user,
			"reopened_on": now_datetime(),
			"reopen_count": self.reopen_sequence,
			"last_reopen": self.name,
		}, update_modified=False)
		self.db_set("remarks", _(
			"{0} reopened (reopen no. {1}) - it is postable as the previous-open period again and must be "
			"closed through Inventory Period Close, which re-runs every gate. The period machine will not "
			"roll past {2} until then."
		).format(period.period_name, self.reopen_sequence,
			frappe.db.get_value("Inventory Period", {"company": self.company, "status": "OPEN"}, "period_name")))

	def before_cancel(self):
		frappe.throw(
			_("Inventory Period Reopen documents cannot be cancelled. Close the period again with Inventory Period Close."),
			title=_("Immutable Ledger"),
		)
