# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from periodic_valuation.periodic_standard_cost.engine import StdEngine, get_active_standard_cost


class InventoryPeriodSettlementRun(Document):
	"""Batch STD settlement for one period across every scope with activity."""

	def validate(self):
		if not (1 <= (self.period_month or 0) <= 12):
			frappe.throw(_("Period Month must be 1-12."))

	def on_submit(self):
		scopes = frappe.db.sql(
			"""SELECT DISTINCT ive.item_code, ive.warehouse
			FROM `tabInventory Valuation Event` ive
			JOIN `tabItem` i ON i.name = ive.item_code
			WHERE ive.company = %s AND ive.is_cancelled = 0
				AND COALESCE(ive.std_trans, '') != '' AND i.valuation_method = 'Periodic Standard Cost'""",
			(self.company,), as_dict=True,
		)
		settled, total_es, total_out = 0, 0.0, 0.0
		notes = []
		# per-scope failure isolation (m3): one scope's error must not abort
		# the whole monthly run — its writes roll back to a savepoint and the
		# remaining scopes still settle; failures land in Remarks.
		for i, s in enumerate(scopes):
			key = f"{s.item_code}" + (f" @ {s.warehouse}" if s.warehouse else "")
			sp = f"sett_run_{i}"
			frappe.db.savepoint(sp)
			try:
				engine = StdEngine(self.company, s.item_code, s.warehouse)
				if engine.is_period_locked(self.period_year, self.period_month):
					continue
				scv = get_active_standard_cost(
					self.company, s.item_code, s.warehouse,
					f"{self.period_year}-{self.period_month:02d}-01",
				)
				sett = engine.close_period(
					year=self.period_year, month=self.period_month,
					sc=flt(scv.standard_cost), source=(self.doctype, self.name),
					settlement_run=self.name,
				)
				settled += 1
				total_es += flt(sett.es_var)
				total_out += flt(sett.out_var)
			except Exception as e:
				frappe.db.rollback(save_point=sp)
				msg = frappe.utils.strip_html(str(e))
				if "Nothing to settle" in msg:
					notes.append(f"{key}: skipped — {msg}")
				else:
					notes.append(f"{key}: FAILED — {msg}")
		self.db_set({
			"status": "Completed", "scopes_settled": settled,
			"total_es_var": flt(total_es, 2), "total_out_var": flt(total_out, 2),
			"remarks": "\n".join(notes) if notes else "",
		})
		failures = [n for n in notes if "FAILED" in n]
		if failures:
			frappe.msgprint(
				_("Settlement Run completed with {0} failed scope(s) — see Remarks. "
				"Fix and re-submit a new run for the failed scopes.").format(len(failures)),
				indicator="orange",
			)

	def before_cancel(self):
		frappe.throw(
			_("Settlement runs cannot be cancelled. Use Sett-Reverse on individual settlements (previous month only)."),
			title=_("Immutable Ledger"),
		)
