# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Periodic Standard Cost posting kernel — voucher routing layer.

Maps routed vouchers onto STD posting intents, drives StdEngine for the
event log + GL, and maintains the SLE-compatible rows / Bin / Inventory
Period Balance so reports and the reconciliation gate keep working (DR-02).

Intent classification:
- Purchase Receipt            -> Rec           (is_return -> PR)
- Delivery Note / SI stock    -> Iss           (is_return -> SR)
- Stock Entry receipt/issue   -> Rec / Iss     (transfers unsupported in v1)
- backdated cross-month       -> REC (BD) / Issue (BD) + companion (Rule B)
- backdated cross-FY          -> REC (BY) / Issue (BY) + companion
- Landed Cost Voucher         -> LC            (via the shared LCV hook)
- Purchase Invoice difference -> LC-shaped PPV event
- Stock Count                 -> SC+ / SC-
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from periodic_valuation.periodic_standard_cost.engine import (
	StdEngine,
	get_active_standard_cost,
	get_std_setting,
	r2,
)
from periodic_valuation.shared.immutable import KERNEL_FLAG
from periodic_valuation.shared.periods import assert_posting_allowed


def post_via_std_kernel(controller, sl_entries):
	if controller.docstatus == 2:
		frappe.throw(
			_("Direct cancellation is blocked for Periodic Standard Cost items. Use Create Cancellation."),
			title=_("Immutable Ledger"),
		)
	if controller.doctype == "Subcontracting Receipt":
		frappe.throw(
			_("Subcontracting is not supported for Periodic Standard Cost items in this release."),
			title=_("Not Supported"),
		)
	is_return = bool(controller.get("is_return"))
	is_cancellation = bool(controller.get("is_cancellation"))

	# DR-17 idempotency: a re-entered voucher must not double-post
	if frappe.db.exists("Inventory Valuation Event", {
		"source_doctype": controller.doctype, "source_docname": controller.name,
		"is_cancelled": 0,
	}):
		return

	from periodic_valuation.periodic_moving_average.kernel import _stamp_document_intent

	_stamp_document_intent(controller, is_cancellation, is_return)

	if controller.doctype == "Stock Reconciliation":
		for sle in sl_entries:
			_post_opening_std(controller, sle)
		return

	entries = sorted(
		sl_entries,
		key=lambda s: (s.get("item_code"), s.get("warehouse") or "", str(s.get("posting_date"))),
	)
	for sle in entries:
		if controller.doctype == "Stock Entry":
			row = next(
				(x for x in controller.get("items") or [] if x.name == sle.get("voucher_detail_no")),
				None,
			)
			if row is not None and row.get("s_warehouse") and row.get("t_warehouse"):
				frappe.throw(
					_("Warehouse transfers are not supported for Periodic Standard Cost items in this release."),
					title=_("Not Supported"),
				)
		_post_entry(controller, sle, is_return)


def _post_entry(controller, sle, is_return):
	company = controller.company
	item_code = sle.get("item_code")
	posting_date = getdate(sle.get("posting_date"))
	period = assert_posting_allowed(company, posting_date)
	engine = StdEngine(company, item_code, sle.get("warehouse"))

	if controller.get("is_cancellation"):
		_post_cancellation_std(controller, engine, sle, period)
		return

	today = getdate(frappe.utils.nowdate())
	cross_month, cross_fy = _backdate_class(engine, posting_date, today)

	scv = get_active_standard_cost(company, item_code, sle.get("warehouse"), posting_date)
	sc = flt(scv.standard_cost)
	qty = flt(sle.get("actual_qty"))
	source = (controller.doctype, controller.name, sle.get("voucher_detail_no"))

	if qty > 0 and not is_return:
		trans = "REC (BY)" if (cross_month and cross_fy) else ("REC (BD)" if cross_month else "Rec")
		ac = flt(sle.get("incoming_rate"))
		engine.post(trans=trans, posting_date=posting_date, qty=qty, sc=sc, ac=ac,
			source=source, cost_version=scv.name)
		value = r2(qty * sc)
		_post_companion_if_needed(engine, controller, sle, trans, qty, sc, source, today)
	elif qty < 0 and not is_return:
		block_setting = get_std_setting(company, "block_negative_stock_std")
		if flt(1 if block_setting is None else block_setting):
			_assert_stock_available(engine, -qty)
		trans = "Issue (BY)" if (cross_month and cross_fy) else ("Issue (BD)" if cross_month else "Iss")
		engine.post(trans=trans, posting_date=posting_date, qty=-qty, sc=sc,
			source=source, cost_version=scv.name)
		value = r2(qty * sc)
		_post_companion_if_needed(engine, controller, sle, trans, -qty, sc, source, today)
	elif is_return and qty < 0:
		# purchase return (PR intent): stock leg at SC; AC = the returned row's
		# actual rate (sle.incoming_rate is 0 for outgoing returns), so the
		# PPV pool gives back the original variance (workbook PR row)
		ac = flt(sle.get("incoming_rate"))
		if not ac:
			row = next((x for x in controller.get("items") or []
				if x.name == sle.get("voucher_detail_no")), None)
			ac = flt(row.base_net_rate) if row else 0.0
		engine.post(trans="PR", posting_date=posting_date, qty=-qty, sc=sc, ac=ac,
			source=source, cost_version=scv.name)
		value = r2(qty * sc)
	else:
		# sales return (SR intent): new movement at posting-date STD (phase-1 rule)
		engine.post(trans="SR", posting_date=posting_date, qty=qty, sc=sc, ac=sc,
			source=source, cost_version=scv.name)
		value = r2(qty * sc)

	_write_sle_and_state(controller, engine, sle, period, qty, sc, value, scv_name=scv.name)


def _post_opening_std(controller, sle):
	"""Beg producer (M12): Stock Reconciliation is the go-live opening lever
	for a scope with NO valuation history. Per the workbook's Beg row: qty at
	the active standard cost, opening variance (AC - SC) into both PPV pools,
	offset FY Carry Forward (DR-06). All later corrections stay blocked —
	Stock Count moves quantity, a cost version release moves value."""
	company = controller.company
	item_code = sle.get("item_code")
	posting_date = getdate(sle.get("posting_date"))
	period = assert_posting_allowed(company, posting_date)
	engine = StdEngine(company, item_code, sle.get("warehouse"))

	if engine.events():
		frappe.throw(
			_(
				"Stock Reconciliation for Periodic Standard Cost items is limited to the opening "
				"entry of a scope with no valuation history. Use Stock Count (quantity) or "
				"release a new cost version (value) for corrections."
			),
			title=_("Not Supported"),
		)

	target_qty = flt(
		sle.get("qty_after_transaction")
		if sle.get("qty_after_transaction") is not None
		else sle.get("actual_qty")
	)
	if target_qty <= 0:
		frappe.throw(_("Opening quantity must be positive for {0}.").format(item_code))

	scv = get_active_standard_cost(company, item_code, sle.get("warehouse"), posting_date)
	sc = flt(scv.standard_cost)
	ac = flt(sle.get("valuation_rate")) if sle.get("valuation_rate") not in (None, "") else sc
	source = (controller.doctype, controller.name, sle.get("voucher_detail_no"))
	engine.post(trans="Beg", posting_date=posting_date, qty=target_qty, sc=sc, ac=ac,
		source=source, cost_version=scv.name)
	_write_sle_and_state(controller, engine, sle, period, target_qty, sc, r2(target_qty * sc), scv_name=scv.name)


def _post_cancellation_std(controller, engine, sle, period):
	"""Exact reversal with reference for a same-doctype Cancellation document."""
	original = controller.get("cancellation_against")
	if not original:
		frappe.throw(_("Cancellation document must reference the original via Cancellation Against."))
	detail = sle.get("voucher_detail_no")
	row = next((x for x in controller.get("items") or [] if x.name == detail), None)
	orig_detail = row and (
		row.get("purchase_receipt_item") or row.get("dn_detail") or row.get("delivery_note_item")
	)
	filters = {
		"source_doctype": controller.doctype, "source_docname": original,
		"item_code": engine.item_code, "is_cancelled": 0,
		"std_trans": ("!=", ""),
	}
	if orig_detail:
		filters["source_detail_name"] = orig_detail
	originals = frappe.get_all("Inventory Valuation Event", filters=filters, pluck="name")
	if not originals:
		frappe.throw(_("No STD valuation events found for {0} to reverse.").format(original))

	source = (controller.doctype, controller.name, detail)
	mirror = None
	for name in originals:
		mirror = engine.reverse_event(name, source=source,
			posting_date=sle.get("posting_date"))

	# SLE-compatible row + state at the ORIGINAL standard cost
	orig_ive = frappe.get_doc("Inventory Valuation Event", originals[0])
	qty = -flt(orig_ive.qty_adj)
	value = -flt(orig_ive.total_sc)
	from periodic_valuation.periodic_moving_average.kernel import ScopeState, recompute_closing, write_sle

	scope = ScopeState(engine.company, engine.item_code, sle.get("warehouse"))
	ipb = scope.load(period)
	if qty > 0:
		ipb.receipt_qty = flt(ipb.receipt_qty) + qty
		ipb.receipt_value = r2(flt(ipb.receipt_value) + value)
	elif qty < 0:
		ipb.issue_qty = flt(ipb.issue_qty) - qty
		ipb.issue_value = r2(flt(ipb.issue_value) - value)
	recompute_closing(ipb)
	scope.save(ipb, source=(controller.doctype, controller.name))
	mirrored = dict(sle)
	mirrored["actual_qty"] = qty
	write_sle(controller, mirrored, scope, ipb, value)


def _backdate_class(engine, posting_date, today):
	"""DR-09 label rules (m11): decide whether a backdated posting carries a
	(BD)/(BY) label or posts plain.

	- Post-reopen late entries post PLAIN: the target period was settled and
	  Sett-Reversed, so the coming re-settlement absorbs them — no bridge.
	- (BY) exists only inside the prior-FY soft-close window: the immediately
	  previous fiscal year, before its December is live-settled. Outside the
	  window the backdate is refused (post a current-dated correction).
	"""
	cross_month = (posting_date.year, posting_date.month) != (today.year, today.month)
	if not cross_month:
		return False, False

	# reopened period: a cancelled settlement exists and no live one
	reopened = not engine.is_period_locked(posting_date.year, posting_date.month) \
		and frappe.db.exists("Inventory Period Settlement", {
			"company": engine.company, "item_code": engine.item_code,
			"warehouse": engine.warehouse or "",
			"period_year": posting_date.year, "period_month": posting_date.month,
			"cancelled": 1,
		})
	if reopened:
		return False, False

	cross_fy = posting_date.year != today.year
	if cross_fy:
		if posting_date.year != today.year - 1 or engine.is_period_locked(today.year - 1, 12):
			frappe.throw(
				_(
					"Backdating into {0} is outside the prior-year soft-close window "
					"((BY) covers only the previous fiscal year before its December is "
					"settled). Post a current-dated correction instead."
				).format(posting_date),
				title=_("Backdate Window Closed"),
			)
	return cross_month, cross_fy


def _post_companion_if_needed(engine, controller, sle, trans, qty, sc_original, source, today):
	"""Cross-month/FY backdate companion: bridges qty x (current SC - original SC)
	in the current period (audit-only when the SC is unchanged)."""
	if trans not in ("REC (BD)", "Issue (BD)", "REC (BY)", "Issue (BY)"):
		return
	current = get_active_standard_cost(
		engine.company, engine.item_code, engine.physical_warehouse, today
	)
	delta = flt(current.standard_cost) - flt(sc_original)
	companion_value = r2(qty * delta)
	if trans.startswith("Issue"):
		companion_value = -companion_value
	# zero-delta companions still post (simulators' zero-bridging rows): the
	# audit chain must show the bridge was evaluated, not skipped
	engine.post(
		trans=f"{trans} - Rev", posting_date=today, qty=qty,
		sc=current.standard_cost, source=source, ref=source[1],
		t_sc_override=companion_value, cost_version=current.name,
	)
	if companion_value:
		# the companion's stock leg must land in today's balance too, or the
		# GL == movement-table identity breaks at period close
		from periodic_valuation.periodic_moving_average.kernel import ScopeState, recompute_closing
		from periodic_valuation.shared.periods import get_period

		period = get_period(engine.company, today)
		if period:
			scope = ScopeState(engine.company, engine.item_code, engine.physical_warehouse)
			ipb = scope.load(period)
			ipb.reval_value = flt(ipb.reval_value) + companion_value
			recompute_closing(ipb)
			if flt(ipb.period_standard_cost):
				ipb.closing_reference_value = r2(flt(ipb.closing_qty) * flt(ipb.period_standard_cost))
			scope.save(ipb, source=source)


def _assert_stock_available(engine, qty_needed):
	from periodic_valuation.periodic_standard_cost.engine import flt as _flt

	periods = engine._periods_present()
	on_hand = 0.0
	if periods:
		y, m = periods[-1]
		on_hand = engine.end_qty_mtd(y, m)
	if on_hand - qty_needed < 0:
		frappe.throw(
			_("Insufficient stock for {0}: negative stock is blocked for Periodic Standard Cost items.").format(
				engine.item_code
			),
			title=_("Negative Stock Blocked"),
		)


def _write_sle_and_state(controller, engine, sle, period, qty, sc, value, scv_name=None):
	"""SLE-compatible row + Bin + Inventory Period Balance at standard cost."""
	from periodic_valuation.periodic_moving_average.kernel import ScopeState, recompute_closing, write_sle

	scope = ScopeState(engine.company, engine.item_code, sle.get("warehouse"))
	ipb = scope.load(period)
	if qty > 0:
		ipb.receipt_qty = flt(ipb.receipt_qty) + qty
		ipb.receipt_value = r2(flt(ipb.receipt_value) + value)
	else:
		ipb.issue_qty = flt(ipb.issue_qty) - qty
		ipb.issue_value = r2(flt(ipb.issue_value) - value)
	recompute_closing(ipb)
	ipb.moving_avg_price = sc  # for STD scopes this column carries the active SC
	ipb.period_standard_cost = sc
	ipb.resolved_settlement_view = engine.view
	ipb.active_cost_version = scv_name
	ipb.closing_reference_value = r2(flt(ipb.closing_qty) * flt(sc))
	scope.save(ipb, source=(controller.doctype, controller.name))
	_cascade_backdated_ipb(scope, period, qty, value, source=(controller.doctype, controller.name))
	write_sle(controller, sle, scope, ipb, value)


def _cascade_backdated_ipb(scope, period, qty, value, source):
	"""A backdated posting lands in ITS period's balance row; every later
	period's opening (and thus closing) must shift by the same delta or the
	materialized chain goes stale (workbook period grid restates Beg)."""
	from periodic_valuation.periodic_moving_average.kernel import recompute_closing

	later = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": scope.company, "item_code": scope.item_code,
			"warehouse": scope.warehouse or ""},
		fields=["name", "period_year", "period_month"],
	)
	later = sorted(
		(x for x in later
			if (x.period_year, x.period_month) > (period.period_year, period.period_month)),
		key=lambda x: (x.period_year, x.period_month),
	)
	for row in later:
		ipb = frappe.get_doc("Inventory Period Balance", row.name)
		ipb.opening_qty = flt(ipb.opening_qty) + qty
		ipb.opening_value = r2(flt(ipb.opening_value) + value)
		recompute_closing(ipb)
		if flt(ipb.period_standard_cost):
			ipb.closing_reference_value = r2(flt(ipb.closing_qty) * flt(ipb.period_standard_cost))
		scope.save(ipb, source=source)


# ------------------------------------------------------------ value events
def post_std_value_event(company, item_code, warehouse, *, trans, amount, source,
		posting_date, offset_account=None):
	"""LC / invoice-diff / count events from transaction-layer documents."""
	engine = StdEngine(company, item_code, warehouse)
	assert_posting_allowed(company, posting_date)
	scv = get_active_standard_cost(company, item_code, warehouse, posting_date)
	if trans == "LC":
		return engine.post(trans="LC", posting_date=posting_date, source=source,
			t_sc_override=0, t_ac_override=r2(amount), cost_version=scv.name)
	frappe.throw(_("Unsupported STD value event {0}").format(trans))


def handle_std_landed_cost(lcv, routed_rows):
	"""Called from the shared LCV handler for STD items: charge -> PPV pool."""
	total_charges = sum(flt(t.base_amount or t.amount) for t in lcv.get("taxes") or [])
	for row in routed_rows:
		amount = flt(row.applicable_charges)
		if not amount:
			continue
		warehouse = frappe.db.get_value(
			row.receipt_document_type + " Item", row.purchase_receipt_item, "warehouse"
		) if row.get("purchase_receipt_item") else None
		for tax in lcv.get("taxes") or []:
			share = flt(tax.base_amount or tax.amount) / total_charges if total_charges else 0
			if not share:
				continue
			engine = StdEngine(lcv.company, row.item_code, warehouse)
			scv = get_active_standard_cost(lcv.company, row.item_code, warehouse, lcv.posting_date)
			ive = engine.post(trans="LC", posting_date=lcv.posting_date,
				source=("Landed Cost Voucher", lcv.name, row.name),
				t_sc_override=0, t_ac_override=r2(amount * share), cost_version=scv.name,
				post_gl=False)
			engine._post_gl(ive, "LC", 0, r2(amount * share),
				offset_override=tax.expense_account)
	return True


def on_purchase_invoice_submit_std(doc, item, pr_row):
	"""PI price difference for an STD item -> PPV pool (invoice diff)."""
	qty = flt(item.qty)
	base_diff = r2((flt(item.base_net_rate) - flt(pr_row.base_net_rate)) * qty)
	if not base_diff:
		return
	engine = StdEngine(doc.company, item.item_code, pr_row.warehouse)
	scv = get_active_standard_cost(doc.company, item.item_code, pr_row.warehouse, doc.posting_date)
	srbnb = frappe.get_cached_value("Company", doc.company, "stock_received_but_not_billed")
	ive = engine.post(trans="LC", posting_date=doc.posting_date,
		source=("Purchase Invoice", doc.name, item.name),
		t_sc_override=0, t_ac_override=base_diff, cost_version=scv.name, post_gl=False)
	engine._post_gl(ive, "LC", 0, base_diff, offset_override=srbnb)
