# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""What periodic_valuation registers with qcs_platform (Build 0.1 §8).

One adapter answers for every document this app posts rows on - the
eight doctypes its cancel guard used to hook - and for its three
append-only event-log doctypes; the Inventory Period is offered as a
posting calendar; the per-company settings doctype is registered so the
platform's one accessor serves it. The rules stay here (which items are
routed, what a Cancellation document is, what a period status means);
the platform only asks.
"""

import frappe
from frappe import _

from qcs_platform.contracts import API_VERSION, Action, Dependent, Refusal, Registration, View
from qcs_platform.core.reversal import reversed_by
from qcs_platform.ledger.policies import PostedNeverDeleted

from periodic_valuation.overrides.cancel_guard import has_routed_items
from periodic_valuation.shared.immutable import KERNEL_FLAG

# the doctypes make_cancellation reverses by copy or return (cancellation.CANCELLABLE)
# plus Stock Reconciliation, which is refused without a route
GOVERNED = (
	"Purchase Receipt",
	"Delivery Note",
	"Stock Entry",
	"Purchase Invoice",
	"Sales Invoice",
	"Subcontracting Receipt",
	"Landed Cost Voucher",
	"Stock Reconciliation",
)
# append-only event rows, and the kernel-maintained period balance
EVENT_ROWS = ("Stock Movement Event", "Inventory Valuation Event", "Inventory Period Balance Snapshot")
KERNEL_MAINTAINED = ("Inventory Period Balance",)
LEDGER_ROWS = EVENT_ROWS + KERNEL_MAINTAINED
NOT_REVERSIBLE = {
	"Stock Reconciliation": _("Opening balances are not reversible; correct quantities with a Stock Count."),
}
# on a Cancellation document everything but the posting date is display-only
REVERSAL_EDITABLE = ("posting_date", "set_posting_time", "posting_time")


class ValuationLedgerAdapter(PostedNeverDeleted):
	app = "periodic_valuation"
	id = "valuation.ledger"
	doctypes = GOVERNED
	ledger_doctypes = LEDGER_ROWS
	# where this app writes is_cancellation / cancellation_against: the
	# doctypes make_cancellation reverses; the platform installs the pair there
	reversal_doctypes = tuple(dt for dt in GOVERNED if dt not in NOT_REVERSIBLE)

	def governs(self, doc):
		return has_routed_items(doc)

	def _offers_cancellation(self, doc):
		# never on a Cancellation document, never while one stands against the
		# original - a draft included, since make_cancellation refuses a second
		return (
			doc.doctype not in NOT_REVERSIBLE
			and not doc.get("is_cancellation")
			and not reversed_by(doc.doctype, doc.name, include_drafts=True)
		)

	def can_cancel(self, doc):
		"""Direct cancellation (docstatus 1 -> 2) is never allowed for a
		document that contains routed items - even same-period with no
		downstream consumption, because an intervening revaluation could
		have landed (May 6 decision). A Cancellation document is the way."""
		if doc.doctype in NOT_REVERSIBLE:
			return Refusal(
				title=_("Cancellation Blocked"),
				message=_("{0} contains periodic-valuation items. {1}").format(doc.name, NOT_REVERSIBLE[doc.doctype]),
				owner=self.app,
			)
		actions = self.actions(doc)
		return Refusal(
			title=_("Cancellation Blocked"),
			message=_(
				"{0} contains periodic-valuation items. Direct cancellation would mutate the immutable ledger."
			).format(doc.name),
			owner=self.app,
			route=actions[0] if actions else None,
		)

	def dependents(self, doc):
		from periodic_valuation.periodic_moving_average.cancellation import standing_dependents

		return tuple(
			Dependent(doctype=dt, name=name, how_to_undo=why)
			for dt, name, why in standing_dependents(doc.doctype, doc.name, doc)
		)

	def actions(self, doc):
		if not self._offers_cancellation(doc):
			return ()
		return (
			Action(
				label=_("Create Cancellation"),
				method="periodic_valuation.periodic_moving_average.cancellation.make_cancellation",
			),
		)

	def locked_fields(self, doc):
		if not doc.get("is_cancellation"):
			return ()
		return tuple(f.fieldname for f in doc.meta.fields if f.fieldname not in REVERSAL_EDITABLE)

	def views(self, doc):
		views = [
			View(
				label=_("Valuation Events"),
				route=f"/app/inventory-valuation-event?source_doctype={doc.doctype}&source_docname={doc.name}",
			)
		]
		if doc.doctype == "Landed Cost Voucher":
			# core LCV never posts GL of its own, so its form has no ledger
			# shortcuts; for kernel items it does (client review 2026-08-18)
			views.insert(
				0,
				View(
					label=_("Stock Ledger"),
					report="Stock Ledger Enterprise",
					filters={"voucher_no": doc.name, "from_date": str(doc.posting_date), "company": doc.company},
				),
			)
			views.insert(
				1,
				View(
					label=_("Accounting Ledger"),
					report="General Ledger Enterprise",
					filters={"voucher_no": doc.name, "from_date": str(doc.posting_date), "company": doc.company},
				),
			)
		return tuple(views)

	def rows_may_change(self, row_doc):
		if not frappe.flags.get(KERNEL_FLAG):
			return False
		if row_doc.doctype in KERNEL_MAINTAINED:
			return True  # the kernel keeps the period balance current
		# an event row: the kernel may flip is_cancelled as part of a reversal pairing; nothing else
		return bool(getattr(row_doc, "_kernel_allowed_update", False))

	def posts_rows(self, doc):
		return has_routed_items(doc)


class InventoryPeriodAuthority:
	"""The Inventory Period as a posting calendar (§4.3): a posting into a
	period that is neither OPEN nor the still-open previous month is
	refused, in this app's words. A month with no period yet is not
	refused here - the kernel opens it on demand (DR-37)."""

	app = "periodic_valuation"
	id = "valuation.inventory_period"

	def applies_to(self, company, doctype):
		return doctype in GOVERNED and bool(frappe.db.exists("Inventory Period", {"company": company}))

	def can_post(self, company, doctype, posting_date):
		from periodic_valuation.shared.periods import get_period, period_refusal

		period = get_period(company, posting_date)
		refusal = period_refusal(period) if period is not None else None
		if not refusal:
			return None
		return Refusal(title=refusal[0], message=refusal[1], owner=self.app)

	def status(self, company, posting_date):
		from periodic_valuation.shared.periods import get_period

		period = get_period(company, posting_date)
		return period.status if period else "NO PERIOD"


def get_registration():
	return Registration(
		app="periodic_valuation",
		api_version=API_VERSION,
		ledger_adapters=(ValuationLedgerAdapter(),),
		settings_doctypes=("Periodic Moving Average Settings",),
		period_authorities=(InventoryPeriodAuthority(),),
	)
