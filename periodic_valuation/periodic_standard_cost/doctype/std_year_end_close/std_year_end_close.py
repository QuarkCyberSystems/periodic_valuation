# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Year-end machinery (M12, design §5.4 CARRY_PER_VIEW):

- force-settles December for every STD scope with activity in the fiscal
  year (the December Sett-Rev — inventory-share only — lands on Jan 1 and
  IS the new year's opening variance seed; MTD items start clean),
- asserts the settlement identities before the fiscal-year flip:
  es_var + out_var = variance, ppv_es + ppv_cons = ppv_pool,
  rev_es + rev_cons = rev_pool, and IPB December closing qty = es_qty.

All-or-nothing: any scope failure rolls the whole close back with a log.
The engine's `_assert_prior_fy_closed` gate refuses new-year settlements
until this has run (or December is otherwise live-settled per scope).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

from periodic_valuation.periodic_standard_cost.engine import (
	StdEngine,
	get_active_standard_cost,
	r2,
)


class STDYearEndClose(Document):
	def validate(self):
		today = getdate(nowdate())
		if flt(self.fiscal_year) >= today.year:
			frappe.throw(
				_("Only a finished fiscal year can be closed (today is in {0}).").format(today.year)
			)

	def on_submit(self):
		fy = int(self.fiscal_year)
		scopes = frappe.db.sql(
			"""SELECT DISTINCT ive.item_code, ive.warehouse
			FROM `tabInventory Valuation Event` ive
			JOIN `tabItem` i ON i.name = ive.item_code
			WHERE ive.company = %s AND ive.is_cancelled = 0 AND ive.period_year = %s
				AND COALESCE(ive.std_trans, '') != ''
				AND i.valuation_method = 'Periodic Standard Cost'""",
			(self.company, fy), as_dict=True,
		)
		log, settled, verified, failures = [], 0, 0, []
		for s in scopes:
			key = f"{s.item_code}" + (f" @ {s.warehouse}" if s.warehouse else "")
			try:
				engine = StdEngine(self.company, s.item_code, s.warehouse)
				if not engine.is_period_locked(fy, 12):
					if engine.end_qty_mtd(fy, 12) <= 0 and not flt(engine.pool_ppv(fy, 12)) \
							and not flt(engine.pool_rev(fy, 12)):
						log.append(f"{key}: nothing to settle in Dec {fy} (no stock, no pool)")
						verified += 1
						continue
					scv = get_active_standard_cost(
						self.company, s.item_code, s.warehouse, f"{fy}-12-31"
					)
					engine.close_period(
						year=fy, month=12, sc=flt(scv.standard_cost),
						source=(self.doctype, self.name),
					)
					settled += 1
					log.append(f"{key}: December {fy} force-settled")
				self._verify_scope(engine, s, fy, log)
				verified += 1
			except Exception as e:
				failures.append(f"{key}: {e}")

		self.db_set({
			"status": "Completed", "scopes_total": len(scopes),
			"scopes_settled": settled, "scopes_verified": verified,
			"log": "\n".join(log + failures),
		})
		if failures:
			frappe.throw(
				_("Year End Close {0} failed for {1} scope(s):<br>{2}").format(
					fy, len(failures), "<br>".join(frappe.utils.escape_html(f) for f in failures)
				),
				title=_("Year End Close Failed"),
			)

	def _verify_scope(self, engine, s, fy, log):
		sett = frappe.get_all(
			"Inventory Period Settlement",
			filters={"company": self.company, "item_code": s.item_code,
				"warehouse": ("in", (s.warehouse or "", None)), "period_year": fy,
				"period_month": 12, "cancelled": 0},
			fields=["name", "variance", "es_var", "out_var", "ppv_pool", "rev_pool",
				"ppv_es", "ppv_cons", "rev_es", "rev_cons", "es_qty", "es_qty_override"],
		)
		if not sett:
			frappe.throw(_("no live December settlement found after close"))
		x = sett[0]
		checks = [
			("es_var+out_var=variance", r2(flt(x.es_var) + flt(x.out_var)), r2(flt(x.variance))),
			("ppv shares", r2(flt(x.ppv_es) + flt(x.ppv_cons)), r2(flt(x.ppv_pool))),
			("rev shares", r2(flt(x.rev_es) + flt(x.rev_cons)), r2(flt(x.rev_pool))),
		]
		for label, a, b in checks:
			if abs(a - b) > 0.01:
				frappe.throw(_("identity {0} broken: {1} != {2}").format(label, a, b))
		if not flt(x.es_qty_override):
			ipb_qty = frappe.db.get_value(
				"Inventory Period Balance",
				{"company": self.company, "item_code": s.item_code,
					"warehouse": ("in", (s.warehouse or "", None)), "period_year": fy, "period_month": 12},
				"closing_qty",
			)
			if ipb_qty is not None and abs(flt(ipb_qty) - flt(x.es_qty)) > 0.000001:
				frappe.throw(
					_("IPB December closing qty {0} != settlement end qty {1}").format(
						ipb_qty, x.es_qty
					)
				)

	def before_cancel(self):
		frappe.throw(
			_("Year End Close records are immutable. Reverse individual settlements instead."),
			title=_("Immutable Ledger"),
		)
