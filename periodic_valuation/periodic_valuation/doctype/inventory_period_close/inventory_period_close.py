# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class InventoryPeriodClose(Document):
	"""Period close ceremony.

	On submit: run the consistency checks and the GL reconciliation gate
	(Apr 22 decision - strict tolerance, manual resolution only, no automatic
	write-off ever). Only when every gate passes does the period advance and
	the next period's opening balances get seeded.
	"""

	def validate(self):
		period = frappe.get_doc("Inventory Period", self.inventory_period)
		if period.company != self.company:
			frappe.throw(_("Inventory Period {0} does not belong to {1}.").format(period.name, self.company))
		if period.status not in ("OPEN", "PREV_OPEN_UNSETTLED"):
			frappe.throw(
				_("Inventory Period {0} is {1}; only an open period (current or previous) can be closed.").format(
					period.name, period.status
				),
				title=_("Invalid Period State"),
			)

	def on_submit(self):
		self.run_close()

	def run_close(self):
		from periodic_valuation.shared.period_close import (
			assert_continuity,
			assert_event_gl_identity,
			assert_no_orphans,
			run_reconciliation_gate,
			seed_next_period_openings,
		)

		period = frappe.get_doc("Inventory Period", self.inventory_period)

		from periodic_valuation.shared.period_close import (
			assert_bin_ledger_consistency,
			assert_no_stranded_value,
		)

		continuity = assert_continuity(period)
		identity = assert_event_gl_identity(period)
		orphans = assert_no_orphans(period)
		recon = run_reconciliation_gate(period)
		bin_ledger = assert_bin_ledger_consistency(period)
		stranded = assert_no_stranded_value(period)

		self.db_set(
			{
				"continuity_ok": 1 if continuity["ok"] else 0,
				"identity_ok": 1 if identity["ok"] else 0,
				"no_orphan_gl": 1 if orphans["no_orphan_gl"] else 0,
				"no_orphan_events": 1 if orphans["no_orphan_events"] else 0,
				"gl_inventory_balance": flt(recon["gl_inventory_balance"]),
				"movement_table_total": flt(recon["movement_table_total"]),
				"discrepancy": flt(recon["discrepancy"]),
				"reconciliation_tolerance": flt(recon["tolerance"]),
				"reconciliation_passed": 1 if recon["passed"] else 0,
			}
		)

		failures = []
		if not continuity["ok"]:
			failures.append(
				_("Opening/closing continuity broken for: {0}").format(", ".join(continuity["detail"][:10]))
			)
		if not identity["ok"]:
			failures.append(
				_("Inventory-to-GL identity mismatch (events {0} vs GL {1}).").format(
					identity["detail"]["ive_total"], identity["detail"]["gl_total"]
				)
			)
		if not orphans["no_orphan_events"]:
			failures.append(
				_("Valuation events without GL output: {0}").format(", ".join(orphans["detail"][:10]))
			)
		if not recon["passed"]:
			failures.append(
				_(
					"Reconciliation gate: discrepancy of {0} between GL inventory balance and the movement "
					"table (tolerance {1}). Investigate via Inventory Period Balance Snapshot and post a "
					"manual reconciliation adjustment; automatic write-off is not permitted."
				).format(recon["discrepancy"], recon["tolerance"])
			)
		if bin_ledger["drifts"]:
			items = ", ".join(sorted({d["item_code"] for d in bin_ledger["drifts"]})[:10])
			failures.append(
				_(
					"Stock balance (Bin) disagrees with the valuation ledger (IPB) for: {0}. "
					"Run a Stock Reconciliation on these items to realign before closing."
				).format(items)
			)
		if stranded["stranded"]:
			detail = ", ".join(
				"{0} {1} carrying {2} on zero stock (since {3})".format(
					d["item_code"], d["warehouse"], flt(d["closing_value"], 2), d["period"]
				)
				for d in stranded["stranded"][:10]
			)
			failures.append(
				_(
					"Inventory value is held against zero quantity for: {0}. Residuals within "
					"{1} clear automatically; these are larger, so they are a real cost sitting "
					"on stock that is gone - most often a late landed cost or invoice difference "
					"allocated to a period where the goods were still on hand. Post a Stock "
					"Revaluation or a correcting entry to move it to the right account; automatic "
					"write-off is not permitted."
				).format(detail, flt(stranded["tolerance"], 2))
			)
		if bin_ledger.get("value_drifts"):
			detail = ", ".join(
				"{0} (ledger {1} vs stock ledger {2})".format(
					d["item_code"], flt(d["ipb_value"], 2), flt(d["sle_value"], 2)
				)
				for d in bin_ledger["value_drifts"][:10]
			)
			failures.append(
				_(
					"Stock Ledger value disagrees with the valuation ledger (IPB) for: {0}. "
					"A value event did not write its SLE row, so core stock reports "
					"under-state inventory by the difference."
				).format(detail)
			)

		if failures:
			frappe.throw(
				_("Period {0} close blocked:").format(period.period_name)
				+ "<br><br>" + "<br>".join(failures),
				title=_("Period Close Blocked"),
			)

		if period.status == "PREV_OPEN_UNSETTLED":
			# the real close: every gate passed on the previous month - freeze it
			frappe.db.set_value("Inventory Period", period.name, "status", "SETTLED_FROZEN")
			period.db_set({"closed_by": frappe.session.user, "closed_on": now_datetime()})
		else:
			# closing the CURRENT month rolls the machine forward; the month stays
			# postable as previous-open and is frozen by a later close of its own
			seed_next_period_openings(period)
			self.db_set("remarks", _("{0} rolled forward: it is now the previous-open period and stays postable; close it again after settlement to freeze it.").format(period.period_name))

	def before_cancel(self):
		frappe.throw(
			_("Inventory Period Close documents cannot be cancelled. Period state moves forward only."),
			title=_("Immutable Ledger"),
		)
