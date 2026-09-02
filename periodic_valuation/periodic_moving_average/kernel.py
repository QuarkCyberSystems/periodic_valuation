# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Periodic Moving Average posting kernel.

Flow per posting (single DB transaction with the voucher):
Normalize -> Validate (period open) -> Lock (IPB prev->current) -> Compute ->
Write (SME + IVE + IPB + SLE-compatible row + GL).

This kernel is the behavioral spec. The pure-Python simulator in
periodic_valuation.periodic_moving_average.reference reproduces the approved
sample-entries workbook and covers the receipt, issue, return, revaluation and
negative-stock paths; it does not carry transfers, the Stock Reconciliation
decomposition, or the rules ruled after it was written, so where the two
differ this file governs.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime

from periodic_valuation.shared.accounts import get_inventory_account, get_offset_account
from periodic_valuation.shared.immutable import KERNEL_FLAG
from periodic_valuation.shared.periods import assert_posting_allowed, get_open_period
from periodic_valuation.shared.settings import get_pma_setting, get_return_valuation

R = 6  # internal precision


def r6(x):
	return flt(x, R)


def r2(x):
	return flt(x, 2)


# --------------------------------------------------------------------- state
class ScopeState:
	"""Loads, locks and mutates the Inventory Period Balance rows for one
	valuation scope (company, item [, warehouse])."""

	def __init__(self, company, item_code, warehouse):
		self.company = company
		self.item_code = item_code
		self.include_warehouse = frappe.get_cached_value(
			"Item", item_code, "valuation_includes_warehouse"
		)
		self.warehouse = warehouse if self.include_warehouse else ""
		self.physical_warehouse = warehouse

	def _filters(self, year, month):
		return {
			"company": self.company,
			"item_code": self.item_code,
			"warehouse": self.warehouse or "",
			"period_year": year,
			"period_month": month,
		}

	def load(self, period, lock=True):
		"""Fetch-or-create the IPB row for an Inventory Period; row-lock it."""
		filters = self._filters(period.period_year, period.period_month)
		name = frappe.db.get_value("Inventory Period Balance", filters, for_update=lock)
		if not name:
			prev = self._previous_closing(period)
			frappe.flags[KERNEL_FLAG] = True
			try:
				doc = frappe.get_doc(
					{
						"doctype": "Inventory Period Balance",
						**filters,
						"opening_qty": prev["qty"],
						"opening_value": prev["value"],
						"closing_qty": prev["qty"],
						"closing_value": prev["value"],
						"moving_avg_price": prev["map"],
						"total_received_since_zero": prev["counter"],
						"is_negative": prev["is_negative"],
						"frozen_map": prev["frozen_map"],
					}
				).insert(ignore_permissions=True)
			finally:
				frappe.flags[KERNEL_FLAG] = False
			name = doc.name
			frappe.db.get_value("Inventory Period Balance", name, for_update=lock)
		return frappe.get_doc("Inventory Period Balance", name)

	def _previous_closing(self, period):
		row = frappe.get_all(
			"Inventory Period Balance",
			filters={
				"company": self.company,
				"item_code": self.item_code,
				"warehouse": self.warehouse or "",
			},
			fields=[
				"period_year", "period_month", "closing_qty", "closing_value",
				"moving_avg_price", "total_received_since_zero", "is_negative", "frozen_map",
			],
			order_by="period_year desc, period_month desc",
			limit=1,
		)
		row = [
			x for x in row
			if (x.period_year, x.period_month) < (period.period_year, period.period_month)
		]
		if not row:
			return {"qty": 0, "value": 0, "map": 0, "counter": 0, "is_negative": 0, "frozen_map": 0}
		x = row[0]
		return {
			"qty": x.closing_qty, "value": x.closing_value, "map": x.moving_avg_price,
			"counter": x.total_received_since_zero, "is_negative": x.is_negative,
			"frozen_map": x.frozen_map,
		}

	def save(self, ipb, caused_by=None, movement_event=None, source=None):
		before = None
		if get_pma_setting(self.company, "enable_period_balance_audit_log"):
			before = frappe.get_doc("Inventory Period Balance", ipb.name).as_dict()
		frappe.flags[KERNEL_FLAG] = True
		try:
			ipb._kernel_allowed_update = True
			ipb.save(ignore_permissions=True)
			if before is not None:
				self._snapshot(before, ipb.as_dict(), caused_by, movement_event, source)
		finally:
			frappe.flags[KERNEL_FLAG] = False

	def _snapshot(self, before, after, caused_by, movement_event, source):
		group = frappe.generate_hash(length=12)
		bucket_fields = [
			"company", "item_code", "warehouse", "period_year", "period_month",
			"opening_qty", "opening_value", "carryover_qty", "carryover_value",
			"receipt_qty", "receipt_value", "issue_qty", "issue_value",
			"adjust_qty", "adjust_value", "reval_value", "prd_value",
			"closing_qty", "closing_value", "moving_avg_price",
			"total_received_since_zero", "is_negative", "frozen_map",
		]
		for image, data in (("Before", before), ("After", after)):
			frappe.get_doc(
				{
					"doctype": "Inventory Period Balance Snapshot",
					"snapshot_event_group": group,
					"image": image,
					"caused_by_event_id": caused_by,
					"movement_event_id": movement_event,
					"source_doctype": source and source[0],
					"source_docname": source and source[1],
					**{f: data.get(f) for f in bucket_fields},
				}
			).insert(ignore_permissions=True)


def recompute_closing(ipb):
	ipb.closing_qty = r6(
		flt(ipb.opening_qty) + flt(ipb.carryover_qty) + flt(ipb.receipt_qty)
		- flt(ipb.issue_qty) + flt(ipb.adjust_qty)
	)
	ipb.closing_value = r6(
		flt(ipb.opening_value) + flt(ipb.carryover_value) + flt(ipb.receipt_value)
		- flt(ipb.issue_value) + flt(ipb.adjust_value) + flt(ipb.reval_value) + flt(ipb.prd_value)
	)
	if flt(ipb.closing_qty) > 0:
		ipb.moving_avg_price = r6(flt(ipb.closing_value) / flt(ipb.closing_qty))


# ------------------------------------------------------------------- writers
SYSTEM_REASONS = {"prd_split", "rounding_cleanup", "settlement", "settlement_reverse", "stranded_sweep"}


def _derive_intent(reason):
	if reason == "cancellation":
		return "EXACT_REVERSAL_WITH_REFERENCE"
	if reason == "return_with_ref":
		return "RETURN_WITH_REFERENCE"
	if reason in SYSTEM_REASONS:
		return "SYSTEM_GENERATED"
	return "NEW_CURRENT_STD_MOVEMENT"


def _receipt_fx(controller):
	"""FX traceability (DR-28 m1): stamp the document conversion rate on receipt
	events, so the receipt-rate basis of a later IFRS split is recorded on the
	event itself. The invoice-difference path still reads the rate from the
	receipt when it posts; this stamp is the audit record of it."""
	rate = flt(controller.get("conversion_rate"))
	return rate if rate and rate != 1 else None


def _derive_expense_portion(reason, movement_type, value_delta, prd_amount):
	"""Signed P&L side of an event (positive = net debit to P&L).

	Derived from the same double-entry the GL legs follow: events whose offset
	is a P&L account mirror the inventory move; events offset to the balance
	sheet (GR/IR) carry no P&L side; negative-stock receipts put exactly the
	PRD component into P&L.
	"""
	if reason in ("receipt", "receipt_neg", "receipt_cross_zero"):
		return prd_amount                      # plain receipt: 0 (GR/IR)
	if reason in ("issue", "count_diff", "revaluation", "rounding_cleanup", "prd_split"):
		return -flt(value_delta)               # offset account is P&L
	if reason in ("return_with_ref", "return_no_ref"):
		# sales return credits COGS; purchase return offsets GR/IR
		return -flt(value_delta) if movement_type == "return_in" else 0
	return 0                                   # transfers, settlement family


def write_events(scope, ipb, *, source, posting_date, movement_type, reason, qty_delta,
		value_delta, map_before, prd_amount=0, inventory_portion=None, expense_portion=None,
		fx_variance=0, reference_event=None, reversal_of=None, movement_reversal_of=None,
		caused_by=None, affects_map=0, stock_uom=None, exchange_rate_at_receipt=None):
	"""Insert the SME (when qty moves) + IVE pair; returns (sme_name, ive_name)."""
	# Every event row carries its inventory / P&L decomposition (client
	# approval comment 2, 2026-08-18: "populated in each relevant transaction,
	# not only in the LC transaction"). The two columns were designed as the
	# late-cost split's audit fields and stayed 0/0 on every other row, so a
	# finance reader saw "no split recorded" on issues, counts and
	# cancellations. inventory_portion is definitionally the event's net
	# inventory effect; expense_portion is derived from the event's own GL
	# shape unless the caller computed (LC/invoice-diff) or mirrored
	# (cancellation) a specific value. Display-only - no GL or balance change.
	if inventory_portion is None:
		inventory_portion = value_delta
	if expense_portion is None:
		expense_portion = _derive_expense_portion(reason, movement_type, value_delta, prd_amount)
	d = getdate(posting_date)
	frappe.flags[KERNEL_FLAG] = True
	try:
		sme_name = None
		if movement_type:
			sme = frappe.get_doc(
				{
					"doctype": "Stock Movement Event",
					"company": scope.company,
					"item_code": scope.item_code,
					"warehouse": scope.physical_warehouse,
					"period_year": d.year,
					"period_month": d.month,
					"posting_date": posting_date,
					"entry_date": now_datetime(),
					"source_doctype": source[0],
					"source_docname": source[1],
					"source_detail_name": source[2] if len(source) > 2 else None,
					"movement_type": movement_type,
					"qty_delta": qty_delta,
					"stock_uom": stock_uom,
					"reversal_of": movement_reversal_of if movement_type == "cancellation" else None,
				}
			).insert(ignore_permissions=True)
			sme_name = sme.name

		ive = frappe.get_doc(
			{
				"doctype": "Inventory Valuation Event",
				"company": scope.company,
				"item_code": scope.item_code,
				"warehouse": scope.physical_warehouse,
				"period_year": d.year,
				"period_month": d.month,
				"posting_date": posting_date,
				"entry_date": now_datetime(),
				"source_doctype": source[0],
				"source_docname": source[1],
				"source_detail_name": source[2] if len(source) > 2 else None,
				"reason_code": reason,
				"posting_intent": _derive_intent(reason),
				"qty_basis": abs(qty_delta) if qty_delta else 0,
				"value_delta": r2(value_delta),
				"inventory_portion": r2(inventory_portion),
				"expense_portion": r2(expense_portion),
				"prd_amount": r2(prd_amount),
				"fx_variance": r2(fx_variance),
				"exchange_rate_at_receipt": exchange_rate_at_receipt,
				"affects_map": affects_map,
				"map_before": map_before,
				"map_after": ipb.moving_avg_price,
				"map_frozen": ipb.is_negative,
				"movement_event_id": sme_name,
				"reference_receipt_event": reference_event,
				"reversal_of": reversal_of if reason == "cancellation" else None,
				"caused_by_event_id": caused_by,
			}
		).insert(ignore_permissions=True)
	finally:
		frappe.flags[KERNEL_FLAG] = False
	return sme_name, ive.name


def write_sle(controller, sle_dict, scope, ipb, value_delta, bin_absolute=None):
	"""SLE-compatible row so Bin, reports and reconciliations keep working."""
	args = dict(sle_dict)
	args.update(
		{
			"doctype": "Stock Ledger Entry",
			"posted_via_valuation_kernel": 1,
			"actual_qty": sle_dict.get("actual_qty"),
			"incoming_rate": sle_dict.get("incoming_rate") or 0,
			"valuation_rate": flt(ipb.moving_avg_price),
			"qty_after_transaction": flt(ipb.closing_qty),
			"stock_value": flt(ipb.closing_value),
			"stock_value_difference": r2(value_delta),
			"stock_queue": "[]",
		}
	)
	sle = frappe.get_doc(args)
	sle.flags.ignore_permissions = True
	sle.allow_negative_stock = True
	sle.via_landed_cost_voucher = False
	sle.insert()
	sle.submit()
	update_bin(scope, ipb, sle_dict.get("actual_qty") or 0, absolute=bin_absolute)
	return sle.name


def ensure_physical_warehouse(scope):
	"""Company-scope callers (SCV boundary restate, settlement absorb) hold no
	physical warehouse, so their value-only SLE mirror had nowhere to land.
	When the item's whole stock-ledger history sits in ONE warehouse - the
	normal single-store case - resolve it; otherwise leave the scope as-is
	(the mirror is skipped and align_stock_ledger_value reports the drift for
	a per-warehouse decision)."""
	if scope.physical_warehouse:
		return scope
	whs = frappe.get_all("Stock Ledger Entry",
		filters={"company": scope.company, "item_code": scope.item_code, "is_cancelled": 0},
		pluck="warehouse", distinct=True)
	if len(whs) == 1:
		scope.physical_warehouse = whs[0]
	return scope


def write_value_sle(scope, ipb, *, source, posting_date, value_delta, qty_delta=0.0,
		stock_uom=None):
	"""Zero-quantity SLE carrying a value-only movement (DR-02).

	Value events - invoice difference, landed cost, revaluation, count difference,
	FX - move inventory value without a receipt or issue. They already write the
	IVE, the IPB, the GL and the Bin, but without an SLE the ~30 core consumers
	that read the Stock Ledger (Stock Balance, Stock Ledger, Stock Analytics,
	Gross Profit, valuation-rate lookups) never see the value and under-report
	inventory by exactly the event amount.

	Mirrors core's own zero-qty convention (Landed Cost Voucher, Stock
	Reconciliation). Flagged `posted_via_valuation_kernel` so `process_sle()`
	early-exits and core never recomputes what the kernel already decided.
	"""
	if not scope.physical_warehouse:
		return None          # nothing to post against when no warehouse was supplied
	if not flt(value_delta) and not flt(qty_delta):
		return None

	voucher_type, voucher_no, voucher_detail_no = source
	args = {
		"doctype": "Stock Ledger Entry",
		"item_code": scope.item_code,
		"warehouse": scope.physical_warehouse,
		"company": scope.company,
		"posting_date": posting_date,
		"posting_time": frappe.utils.nowtime(),
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"voucher_detail_no": voucher_detail_no,
		"stock_uom": stock_uom or frappe.get_cached_value("Item", scope.item_code, "stock_uom"),
		"posted_via_valuation_kernel": 1,
		"is_adjustment_entry": 1,
		"actual_qty": flt(qty_delta),
		"incoming_rate": 0,
		"valuation_rate": flt(ipb.moving_avg_price),
		"qty_after_transaction": flt(ipb.closing_qty),
		"stock_value": flt(ipb.closing_value),
		"stock_value_difference": r2(value_delta),
		"stock_queue": "[]",
	}
	sle = frappe.get_doc(args)
	sle.flags.ignore_permissions = True
	sle.allow_negative_stock = True
	sle.via_landed_cost_voucher = False
	sle.insert()
	sle.submit()
	return sle.name


def _sync_bin_to_ledger(scope, qty_delta):
	"""Shift the Bin by qty_delta and refresh its value/rate from the LATEST
	IPB (so a backdated value event still reflects the current balance). Used
	by value-only postings (count / revaluation / landed cost / invoice-diff),
	which otherwise never touch the Bin and cause Bin<->IPB drift."""
	from erpnext.stock.utils import get_or_make_bin

	if not scope.physical_warehouse:
		return
	latest = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": scope.company, "item_code": scope.item_code,
			"warehouse": scope.warehouse or ""},
		fields=["closing_value", "moving_avg_price"],
		order_by="period_year desc, period_month desc", limit=1,
	)
	if not latest:
		return
	bin_name = get_or_make_bin(scope.item_code, scope.physical_warehouse)
	new_qty = flt(frappe.db.get_value("Bin", bin_name, "actual_qty")) + flt(qty_delta)
	frappe.db.set_value("Bin", bin_name, {
		"actual_qty": new_qty,
		"valuation_rate": flt(latest[0].moving_avg_price),
		"stock_value": flt(latest[0].closing_value) if scope.include_warehouse
			else flt(new_qty * flt(latest[0].moving_avg_price), 2),
	}, update_modified=True)


def update_bin(scope, ipb, qty_delta, absolute=None):
	from erpnext.stock.utils import get_or_make_bin

	if not scope.physical_warehouse:
		return
	bin_name = get_or_make_bin(scope.item_code, scope.physical_warehouse)
	# `absolute` is the counted physical quantity for this warehouse - used by
	# Stock Reconciliation to SET the Bin (correcting any prior Bin<->IPB drift)
	# rather than applying a delta. Every other posting shifts the Bin by the
	# movement quantity, keeping the shadow row in step with the ledger.
	if absolute is not None:
		qty = flt(absolute)
	else:
		qty = flt(frappe.db.get_value("Bin", bin_name, "actual_qty")) + flt(qty_delta)
	values = {
		"actual_qty": qty,
		"valuation_rate": flt(ipb.moving_avg_price),
		# per-warehouse scope: bin value is exact; company scope: approximate
		# the shelf value at scope MAP (design: Bin reads from IPB)
		"stock_value": flt(ipb.closing_value) if scope.include_warehouse else flt(qty * flt(ipb.moving_avg_price), 2),
	}
	frappe.db.set_value("Bin", bin_name, values, update_modified=True)


def post_gl(controller, posting_date, legs, ive_name, remarks=None):
	"""legs: (account, signed_amount, against) - positive Dr, negative Cr."""
	from erpnext.accounts.general_ledger import make_gl_entries

	gl_map = []
	for account, amount, against in legs:
		amount = r2(amount)
		if not amount:
			continue
		gl_map.append(
			controller.get_gl_dict(
				{
					"account": account,
					"against": against,
					"debit": amount if amount > 0 else 0,
					"credit": -amount if amount < 0 else 0,
					# Inventory and its valuation offsets are always company
					# currency (design: "inventory always base currency"), so
					# the account-currency figures are the same numbers. The
					# General Ledger report renders THESE fields - leaving them
					# unset stores 0 and every kernel leg displays as $0.00,
					# which finance reads as "no accounting entries" even
					# though the base debit/credit are correct and balanced
					# (client meeting 2026-08-12, screenshot of ACC-PINV GL).
					"debit_in_account_currency": amount if amount > 0 else 0,
					"credit_in_account_currency": -amount if amount < 0 else 0,
					"posting_date": posting_date,
					"remarks": remarks or _("PMA valuation event {0}").format(ive_name),
					"valuation_event_id": ive_name,
					"cost_center": controller.get("cost_center")
					or frappe.get_cached_value("Company", controller.company, "cost_center"),
				}
			)
		)
	if gl_map:
		make_gl_entries(gl_map, merge_entries=False)


# ----------------------------------------------------------------- main entry
def post_via_pma_kernel(controller, sl_entries):
	"""Entry point for the fork's routing dispatch."""
	if controller.docstatus == 2:
		frappe.throw(
			_("Direct cancellation is blocked for Periodic Moving Average items. Use Create Cancellation."),
			title=_("Immutable Ledger"),
		)

	company = controller.company
	if controller.doctype == "Subcontracting Receipt":
		frappe.throw(
			_("Subcontracting is not supported for Periodic Moving Average items in this release."),
			title=_("Not Supported"),
		)
	if controller.doctype == "Stock Entry" and controller.get("add_to_transit"):
		frappe.throw(
			_("Stock-in-transit is not supported for periodic-valuation items in this release."),
			title=_("Not Supported"),
		)

	is_cancellation = bool(controller.get("is_cancellation"))
	is_return = bool(controller.get("is_return"))

	# deterministic lock order: sort by scope key then posting date
	entries = sorted(
		sl_entries, key=lambda s: (s.get("item_code"), s.get("warehouse") or "", str(s.get("posting_date")))
	)

	_guard_rejected_qty(controller)

	if controller.doctype == "Stock Reconciliation":
		for sle in entries:
			scope = ScopeState(company, sle.get("item_code"), sle.get("warehouse"))
			period = assert_posting_allowed(company, sle.get("posting_date"))
			_post_reconciliation(controller, scope, period, sle)
		return

	# DR-17 idempotency: a re-entered voucher must not double-post
	if frappe.db.exists("Inventory Valuation Event", {
		"source_doctype": controller.doctype, "source_docname": controller.name,
		"is_cancelled": 0,
	}):
		return

	_stamp_document_intent(controller, is_cancellation, is_return)
	transfer_pairs, entries = _pair_transfers(controller, entries)
	for out_sle, in_sle in transfer_pairs:
		_post_transfer(controller, out_sle, in_sle)

	for sle in entries:
		scope = ScopeState(company, sle.get("item_code"), sle.get("warehouse"))
		posting_date = sle.get("posting_date")
		period = assert_posting_allowed(company, posting_date)
		open_period = get_open_period(company)

		if period.status == "OPEN" or not open_period or period.name == open_period.name:
			_post_current(controller, scope, period, sle, is_cancellation, is_return)
		else:
			_post_backdated(controller, scope, period, open_period, sle, is_return)


def _guard_rejected_qty(controller):
	"""Rejected-warehouse receipts are unsupported for kernel items in this
	release: a zero-rate rejected receipt entering a company-level scope would
	dilute MAP. Quality-inspection stock is treated as regular stock (signed
	plan scope declaration)."""
	kernel_map = frappe.get_hooks("valuation_kernels")
	from erpnext.stock.utils import get_valuation_method

	for row in controller.get("items") or []:
		if (
			flt(row.get("rejected_qty"))
			and row.get("item_code")
			and get_valuation_method(row.item_code, controller.get("company")) in kernel_map
		):
			frappe.throw(
				_(
					"Row {0}: rejected quantity is not supported for periodic-valuation item {1} in this "
					"release. Receive the full quantity and post a return or count for rejects."
				).format(row.idx, row.item_code),
				title=_("Rejected Qty Not Supported"),
			)


def _post_reconciliation(controller, scope, period, sle):
	"""Stock Reconciliation = the cutover / correction lever (signed plan).

	SR rows carry ABSOLUTE targets (qty_after_transaction, valuation_rate),
	not deltas. The kernel decomposes them into up to two immutable events:
	a quantity adjustment at the target rate (count_gain / count_loss) and a
	residual revaluation so closing value lands exactly on qty x rate.
	Offset account: the reconciliation's expense/difference account.
	"""
	ipb = scope.load(period)
	source = (controller.doctype, controller.name, sle.get("voucher_detail_no"))
	posting_date = sle.get("posting_date")
	map_before = flt(ipb.moving_avg_price)

	current_qty = flt(ipb.closing_qty)
	current_value = flt(ipb.closing_value)
	# A reconciliation row targets ONE warehouse's PHYSICAL quantity. For a
	# company-scope item with stock in several warehouses, comparing the target
	# against the SCOPE total posted garbage: setting a warehouse holding 60 to
	# a counted 55 (with 40 elsewhere) read as a -45 adjustment for a real loss
	# of 5 (client approval comment 1, 2026-08-18). Valuation of the delta
	# stays at the scope rate - quantity is physical, value is scope.
	from periodic_valuation.periodic_moving_average.api import get_physical_qty

	physical_qty = get_physical_qty(scope.item_code, scope.physical_warehouse, posting_date)
	target_qty = flt(sle.get("qty_after_transaction")) if sle.get("qty_after_transaction") is not None else physical_qty
	has_rate = sle.get("valuation_rate") not in (None, "")
	rate = flt(sle.get("valuation_rate")) if has_rate else (map_before or 0)
	# Core SR auto-fills valuation_rate from the prefill on every row, so
	# has_rate alone cannot mean "the user wants a re-price" - only a rate that
	# DIFFERS from the scope's current rate does. The prefill rate equals the
	# scope rate by construction, so an untouched row reconciles quantity only.
	scope_rate = flt(ipb.frozen_map) if ipb.is_negative else map_before
	reprice = has_rate and abs(flt(rate) - scope_rate) > 1e-4
	scope_spans_other_warehouses = abs(flt(current_qty) - flt(physical_qty)) > 1e-6
	if reprice and scope_spans_other_warehouses:
		# a rate re-prices the WHOLE valuation scope; forcing scope value to
		# one warehouse's qty x rate while other warehouses hold stock would
		# corrupt the shared MAP. Value corrections on a multi-warehouse
		# company scope go through Stock Revaluation (scope-level by design).
		frappe.throw(
			_(
				"Row for {0}: a valuation rate cannot be set here - the item is valued at "
				"company scope and other warehouses hold {1} of its {2} on hand. Reconcile "
				"the quantity only, and use Stock Revaluation for value corrections."
			).format(scope.item_code, flt(current_qty - physical_qty), flt(current_qty)),
			title=_("Scope-Level Value"),
		)
	target_value = r2(target_qty * rate) if reprice else r2(current_value + (target_qty - physical_qty) * rate)

	qty_delta = r6(target_qty - physical_qty)
	offset = _reconciliation_offset_account(controller, sle)
	inventory_account = get_inventory_account(scope.company, scope.item_code, scope.physical_warehouse)
	total_delta = 0.0
	last_ive = None

	if qty_delta:
		# A count difference is valued at the CURRENT period MAP (frozen MAP
		# while negative) and never moves MAP - the rate change, if any, is
		# entirely the revaluation residual below. Valuing the count at the
		# TARGET rate overstated the count leg and understated the
		# revaluation by qty x (target - MAP), and moved MAP on a count
		# event, which the design forbids (client meeting 2026-08-12,
		# MAT-RECO-2026-00010: 500 x 1,000 posted where 500 x 996 belongs).
		count_rate = flt(ipb.frozen_map) if ipb.is_negative else map_before
		qty_value = r2(qty_delta * count_rate)
		ipb.adjust_qty = r6(flt(ipb.adjust_qty) + qty_delta)
		ipb.adjust_value = r6(flt(ipb.adjust_value) + qty_value)
		recompute_closing(ipb)
		if not ipb.is_negative and flt(ipb.closing_qty) > 0:
			ipb.moving_avg_price = map_before  # count never moves MAP
		sme, last_ive = write_events(
			scope, ipb, source=source, posting_date=posting_date,
			movement_type="count_gain" if qty_delta > 0 else "count_loss",
			reason="count_diff", qty_delta=qty_delta, value_delta=qty_value,
			map_before=map_before, affects_map=0,
			stock_uom=sle.get("stock_uom"),
		)
		post_gl(
			controller, posting_date,
			[(inventory_account, qty_value, offset), (offset, -qty_value, inventory_account)],
			last_ive,
		)
		total_delta += qty_value

	residual = r2(target_value - flt(ipb.closing_value))
	if residual:
		if flt(ipb.closing_qty) <= 0:
			frappe.throw(
				_("Row for {0}: cannot set a valuation rate on zero/negative stock.").format(scope.item_code)
			)
		ipb.reval_value = r6(flt(ipb.reval_value) + residual)
		recompute_closing(ipb)
		__, last_ive = write_events(
			scope, ipb, source=source, posting_date=posting_date,
			movement_type=None, reason="revaluation", qty_delta=0,
			value_delta=residual, map_before=map_before, affects_map=1,
		)
		post_gl(
			controller, posting_date,
			[(inventory_account, residual, offset), (offset, -residual, inventory_account)],
			last_ive,
		)
		total_delta += residual

	if reprice and flt(ipb.closing_qty) > 0:
		ipb.moving_avg_price = rate
	_freeze_check(ipb)
	scope.save(ipb, caused_by=last_ive, source=source)

	# Backdated reconciliation: every LATER period's carryover must absorb the
	# same qty/value delta, exactly as receipts, issues and value events do -
	# otherwise the next period's opening no longer chains from this period's
	# closing and period close refuses (client meeting 2026-08-12; MH #9:
	# July closed 3,000/2,988,930 while August still carried 1,490/1,370,800).
	_cascade_value_carryover(
		scope, period, qty_delta=qty_delta, value_delta=total_delta, source=source,
	)

	sle_row = dict(sle)
	sle_row["actual_qty"] = qty_delta
	# a reconciliation SETS the counted physical quantity for this warehouse,
	# so the Bin is written to the absolute target (not shifted by the delta) -
	# this is the lever that erases any prior Bin<->IPB drift.
	write_sle(controller, sle_row, scope, ipb, total_delta, bin_absolute=target_qty)
	maybe_rounding_cleanup(controller, scope, ipb, source, posting_date, qty_scale=qty_delta)


def _reconciliation_offset_account(controller, sle):
	detail = sle.get("voucher_detail_no")
	for row in controller.get("items") or []:
		if row.name == detail and row.get("expense_account"):
			return row.get("expense_account")
	return controller.get("expense_account") or frappe.get_cached_value(
		"Company", controller.company, "stock_adjustment_account"
	)


def _stamp_document_intent(controller, is_cancellation, is_return):
	"""Visible, immutable classification on the source document - derived from
	the action taken (Create Cancellation / Return / plain), never user-picked."""
	intent = (
		"EXACT_REVERSAL_WITH_REFERENCE" if is_cancellation
		else "RETURN_WITH_REFERENCE" if (is_return and controller.get("return_against"))
		else "NEW_CURRENT_STD_MOVEMENT"
	)
	if controller.meta.has_field("posting_intent") and controller.get("posting_intent") != intent:
		frappe.db.set_value(controller.doctype, controller.name, "posting_intent", intent,
			update_modified=False)


def _pair_transfers(controller, entries):
	"""Detect two-leg transfers (same voucher row, one negative + one positive
	SLE) and split them from the normal flow."""
	if controller.doctype != "Stock Entry" or controller.get("is_cancellation"):
		return [], entries

	by_detail = {}
	for sle in entries:
		by_detail.setdefault(sle.get("voucher_detail_no"), []).append(sle)

	pairs, singles = [], []
	for group in by_detail.values():
		if len(group) == 2 and flt(group[0].get("actual_qty")) * flt(group[1].get("actual_qty")) < 0:
			out_sle = min(group, key=lambda s: flt(s.get("actual_qty")))
			in_sle = max(group, key=lambda s: flt(s.get("actual_qty")))
			pairs.append((out_sle, in_sle))
		else:
			singles.extend(group)
	return pairs, singles


def _post_transfer(controller, out_sle, in_sle):
	"""Warehouse transfer of a kernel item.

	Company-scope items (valuation_includes_warehouse OFF): physical-only -
	movement events on both legs, one value-neutral IVE, no GL (signed plan).
	Warehouse-scope items (ON): issue at the source scope's MAP, receipt into
	the destination scope at that unit cost; GL moves value between the two
	inventory accounts.
	"""
	company = controller.company
	item_code = out_sle.get("item_code")
	qty = flt(in_sle.get("actual_qty"))
	posting_date = out_sle.get("posting_date")
	period = assert_posting_allowed(company, posting_date)
	source = (controller.doctype, controller.name, out_sle.get("voucher_detail_no"))
	include_wh = frappe.get_cached_value("Item", item_code, "valuation_includes_warehouse")

	if not include_wh:
		scope = ScopeState(company, item_code, out_sle.get("warehouse"))
		ipb = scope.load(period)
		map_before = flt(ipb.moving_avg_price)
		# quantity is scope-neutral; record both physical legs for audit
		for sle, movement in ((out_sle, "transfer_out"), (in_sle, "transfer_in")):
			scope.physical_warehouse = sle.get("warehouse")
			sme, ive = write_events(
				scope, ipb, source=source, posting_date=posting_date,
				movement_type=movement, reason="transfer",
				qty_delta=flt(sle.get("actual_qty")), value_delta=0,
				map_before=map_before, stock_uom=sle.get("stock_uom"),
			)
			write_sle(controller, sle, scope, ipb, 0)
		scope.save(ipb, caused_by=ive, movement_event=sme, source=source)
		return

	# warehouse-scope: two independent scopes, value moves at source MAP
	out_scope = ScopeState(company, item_code, out_sle.get("warehouse"))
	in_scope = ScopeState(company, item_code, in_sle.get("warehouse"))
	ipb_out = out_scope.load(period)
	ipb_in = in_scope.load(period)

	rate = flt(ipb_out.frozen_map) if ipb_out.is_negative else flt(ipb_out.moving_avg_price)
	value = r2(qty * rate)

	map_before_out = flt(ipb_out.moving_avg_price)
	ipb_out.issue_qty = r6(flt(ipb_out.issue_qty) + qty)
	ipb_out.issue_value = r6(flt(ipb_out.issue_value) + value)
	recompute_closing(ipb_out)
	_pin_map(ipb_out, map_before_out)  # the outbound leg is an issue at source MAP
	_freeze_check(ipb_out)
	sme_out, ive_out = write_events(
		out_scope, ipb_out, source=source, posting_date=posting_date,
		movement_type="transfer_out", reason="transfer", qty_delta=-qty,
		value_delta=-value, map_before=map_before_out, stock_uom=out_sle.get("stock_uom"),
	)
	out_scope.save(ipb_out, caused_by=ive_out, movement_event=sme_out, source=source)
	write_sle(controller, out_sle, out_scope, ipb_out, -value)

	map_before_in = flt(ipb_in.moving_avg_price)
	result = _apply_receipt(ipb_in, qty, rate)
	sme_in, ive_in = write_events(
		in_scope, ipb_in, source=source, posting_date=posting_date,
		movement_type="transfer_in", reason="transfer", qty_delta=qty,
		value_delta=result["net_to_inventory"], map_before=map_before_in,
		prd_amount=result["prd"], affects_map=1, stock_uom=in_sle.get("stock_uom"),
	)
	in_scope.save(ipb_in, caused_by=ive_in, movement_event=sme_in, source=source)
	write_sle(controller, in_sle, in_scope, ipb_in, result["net_to_inventory"])

	source_account = get_inventory_account(company, item_code, out_sle.get("warehouse"))
	dest_account = get_inventory_account(company, item_code, in_sle.get("warehouse"))
	if source_account != dest_account:
		post_gl(
			controller, posting_date,
			[(dest_account, value, source_account), (source_account, -value, dest_account)],
			ive_in,
		)

	maybe_rounding_cleanup(controller, out_scope, ipb_out, source, posting_date, qty_scale=qty)


def _classify(controller, sle, is_cancellation, is_return):
	qty = flt(sle.get("actual_qty"))
	if is_cancellation:
		return "cancellation"
	if is_return:
		return "return_in" if qty > 0 else "return_out"
	return "receipt" if qty > 0 else "issue"


def _post_current(controller, scope, period, sle, is_cancellation, is_return):
	ipb = scope.load(period)
	kind = _classify(controller, sle, is_cancellation, is_return)
	qty = flt(sle.get("actual_qty"))
	source = (controller.doctype, controller.name, sle.get("voucher_detail_no"))
	posting_date = sle.get("posting_date")
	map_before = flt(ipb.moving_avg_price)

	inventory_account = get_inventory_account(scope.company, scope.item_code, scope.physical_warehouse)
	srbnb = frappe.get_cached_value("Company", scope.company, "stock_received_but_not_billed")
	expense = _voucher_expense_account(controller, sle) or srbnb

	if kind == "receipt":
		rate = flt(sle.get("incoming_rate"))
		if controller.doctype == "Stock Entry":
			# SE receipts offset the row/item expense chain, not GR/IR
			srbnb = _voucher_expense_account(controller, sle) or srbnb
		result = _apply_receipt(ipb, qty, rate)
		sme, ive = write_events(
			scope, ipb, source=source, posting_date=posting_date,
			movement_type="receipt", reason=result["reason"], qty_delta=qty,
			value_delta=result["net_to_inventory"], map_before=map_before,
			prd_amount=result["prd"], affects_map=1, stock_uom=sle.get("stock_uom"),
			exchange_rate_at_receipt=_receipt_fx(controller),
		)
		legs = [
			(inventory_account, result["receipt_value"], srbnb),
			(srbnb, -result["receipt_value"], inventory_account),
		]
		if result["prd"]:
			prd_account = get_offset_account(scope.company, scope.item_code, scope.physical_warehouse, "prd")
			legs += [(prd_account, result["prd"], inventory_account),
				(inventory_account, -result["prd"], prd_account)]
		scope.save(ipb, caused_by=ive, movement_event=sme, source=source)
		write_sle(controller, sle, scope, ipb, result["net_to_inventory"])
		post_gl(controller, posting_date, legs, ive)

	elif kind == "issue":
		rate = flt(ipb.frozen_map) if ipb.is_negative else flt(ipb.moving_avg_price)
		if not get_pma_setting(scope.company, "negative_stock_allowed") and flt(ipb.closing_qty) + qty < 0:
			frappe.throw(
				_("Insufficient stock for {0}: negative stock is not allowed.").format(scope.item_code),
				title=_("Negative Stock Blocked"),
			)
		issue_value = r2(-qty * rate)  # qty is negative
		ipb.issue_qty = r6(flt(ipb.issue_qty) - qty)
		ipb.issue_value = r6(flt(ipb.issue_value) + issue_value)
		recompute_closing(ipb)
		_pin_map(ipb, map_before)  # an issue never moves the MAP
		_freeze_check(ipb)
		sme, ive = write_events(
			scope, ipb, source=source, posting_date=posting_date,
			movement_type="issue", reason="issue", qty_delta=qty,
			value_delta=-issue_value, map_before=map_before, stock_uom=sle.get("stock_uom"),
		)
		scope.save(ipb, caused_by=ive, movement_event=sme, source=source)
		write_sle(controller, sle, scope, ipb, -issue_value)
		post_gl(
			controller, posting_date,
			[(expense, issue_value, inventory_account), (inventory_account, -issue_value, expense)],
			ive,
		)

	elif kind in ("return_in", "return_out"):
		policy = get_return_valuation(scope.company, controller.doctype)
		# Whether the return is valued at the ORIGINAL cost is decided by the
		# policy, not by whether the original event row could be located:
		# _original_rate falls back to the return row's own rate (copied from the
		# original document) when the event lookup misses, and that is still a
		# with-reference valuation. The MAP rule below keys on this, so it must
		# not be conflated with `reference_event` being found.
		valued_at_reference = policy == "With Reference" and bool(controller.get("return_against"))
		if valued_at_reference:
			rate, reference_event = _original_rate(controller, sle)
		else:
			rate, reference_event = (flt(ipb.frozen_map) if ipb.is_negative else flt(ipb.moving_avg_price)), None
		value = r2(qty * rate)  # signed with qty
		# Net returns against their ORIGIN bucket (WA-0003-01 item 6, "same
		# behaviour STD"): a purchase return reverses a receipt, so it nets
		# down the In/receipt side; a sales return reverses an issue, so it
		# nets down the Out/issue side. This mirrors STD's PR in_flag / SR
		# out_flag. Closing qty/value, MAP and GL are unchanged - only the
		# period-balance In/Out breakdown differs (previously returns were
		# bucketed by physical direction, inflating the wrong side).
		if qty > 0:
			# sales return (goods in) -> reduce net issues (Out)
			ipb.issue_qty = r6(flt(ipb.issue_qty) - qty)
			ipb.issue_value = r6(flt(ipb.issue_value) - value)
		else:
			# purchase return (goods out) -> reduce net receipts (In)
			ipb.receipt_qty = r6(flt(ipb.receipt_qty) + qty)
			ipb.receipt_value = r6(flt(ipb.receipt_value) + value)
			# ...and the since-zero counter, which measures the SAME receipts
			# (WA-0003-01 item 7). Once a purchase return nets down the receipt
			# bucket, leaving the counter untouched makes the Period Balance
			# contradict itself: receipt_qty says 800 received while
			# total_received_since_zero says 1,000 for the same movements, so
			# the audited receiving history would include goods that went back
			# to the supplier. Floored at zero - a return can
			# never imply negative receipts. A SALES return is deliberately not
			# counted here: it reverses an issue, so it never entered this
			# counter in the first place.
			ipb.total_received_since_zero = r6(
				max(0.0, flt(ipb.total_received_since_zero) + qty)
			)
		recompute_closing(ipb)
		if not valued_at_reference:
			# valued at the current MAP, so it cannot move it (signed plan,
			# Return without Reference); a return
			# at the original cost re-blends like a receipt and does move it
			_pin_map(ipb, map_before)
		_freeze_check(ipb)
		reason = "return_with_ref" if reference_event else "return_no_ref"
		movement = "return_in" if qty > 0 else "return_out"
		offset = expense if movement == "return_in" else srbnb
		sme, ive = write_events(
			scope, ipb, source=source, posting_date=posting_date,
			movement_type=movement, reason=reason, qty_delta=qty, value_delta=value,
			map_before=map_before, reference_event=reference_event,
			affects_map=1 if reference_event else 0, stock_uom=sle.get("stock_uom"),
		)
		scope.save(ipb, caused_by=ive, movement_event=sme, source=source)
		write_sle(controller, sle, scope, ipb, value)
		post_gl(
			controller, posting_date,
			[(inventory_account, value, offset), (offset, -value, inventory_account)],
			ive,
		)

	elif kind == "cancellation":
		_post_cancellation(controller, scope, period, ipb, sle, source, inventory_account, expense)

	maybe_rounding_cleanup(controller, scope, ipb, source, posting_date, qty_scale=qty)


def _apply_receipt(ipb, qty, rate):
	"""Receipt math on the IPB row - mirrors reference kernel receipt()."""
	receipt_value = r2(qty * rate)
	closing = flt(ipb.closing_qty)

	if closing >= 0:
		ipb.receipt_qty = r6(flt(ipb.receipt_qty) + qty)
		ipb.receipt_value = r6(flt(ipb.receipt_value) + receipt_value)
		ipb.total_received_since_zero = r6(flt(ipb.total_received_since_zero) + qty)
		recompute_closing(ipb)
		return {"reason": "receipt", "receipt_value": receipt_value, "net_to_inventory": receipt_value, "prd": 0}

	frozen = flt(ipb.frozen_map)
	if closing + qty <= 0:
		prd = r2((rate - frozen) * qty)
		net = r2(qty * frozen)
		ipb.receipt_qty = r6(flt(ipb.receipt_qty) + qty)
		ipb.receipt_value = r6(flt(ipb.receipt_value) + receipt_value)
		ipb.prd_value = r6(flt(ipb.prd_value) - prd)
		ipb.total_received_since_zero = r6(flt(ipb.total_received_since_zero) + qty)
		recompute_closing(ipb)
		# Landing exactly on zero ends the negative excursion: clear the frozen
		# state and reset the since-zero counter. MAP is deliberately NOT touched
		# - it stays at the previous (frozen) value until a real crossing receipt
		# re-prices the pool (WA-0003-01 item 22).
		_freeze_check(ipb)
		return {"reason": "receipt_neg", "receipt_value": receipt_value, "net_to_inventory": net, "prd": prd}

	clearing = r6(-closing)
	excess = r6(qty - clearing)
	prd = r2((rate - frozen) * clearing)
	net = r2(clearing * frozen + excess * rate)
	ipb.receipt_qty = r6(flt(ipb.receipt_qty) + qty)
	ipb.receipt_value = r6(flt(ipb.receipt_value) + receipt_value)
	ipb.prd_value = r6(flt(ipb.prd_value) - prd)
	ipb.total_received_since_zero = excess  # fresh cycle
	ipb.is_negative = 0
	ipb.frozen_map = 0
	recompute_closing(ipb)
	ipb.moving_avg_price = rate  # RESET to the crossing receipt's price
	return {"reason": "receipt_cross_zero", "receipt_value": receipt_value, "net_to_inventory": net, "prd": prd}


def _cascade_value_carryover(scope, period, *, qty_delta, value_delta, source):
	"""Propagate a backdated value/count adjustment into every later period's
	carryover buckets so the materialized balance chain stays consistent
	(WA-0003-01 item 8). No-op when the event is in the latest period."""
	if not (flt(qty_delta) or flt(value_delta)):
		return
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
		ipb.carryover_qty = r6(flt(ipb.carryover_qty) + flt(qty_delta))
		ipb.carryover_value = r6(flt(ipb.carryover_value) + flt(value_delta))
		recompute_closing(ipb)
		_freeze_check(ipb)
		scope.save(ipb, source=source)


def _guard_positive_value(ipb, item_code, reason):
	"""Block a value-only posting that would drive inventory value negative
	while positive stock remains - that produces a negative moving average,
	which is never valid (WA-0003-01 item 14). Genuine negative *stock* (qty
	< 0 with frozen MAP) is a separate, supported case and is not caught here.
	"""
	if flt(ipb.closing_qty) > 0 and flt(ipb.closing_value) < 0:
		frappe.throw(
			_(
				"This {0} would make {1}'s inventory value negative ({2}) while "
				"{3} units are still in stock, producing a negative moving average. "
				"Posting is blocked - correct the cost or quantity first."
			).format(
				reason.replace("_", " "), item_code,
				flt(ipb.closing_value, 2), flt(ipb.closing_qty),
			),
			title=_("Negative Valuation Blocked"),
		)


def _pin_map(ipb, map_before):
	"""Hold the MAP for an event the design says never moves it.

	`recompute_closing` derives MAP = closing_value / closing_qty, but
	closing_value is carried at GL precision (the issue leg is rounded to 2dp so
	it equals the posted amount). Re-deriving the MAP from that rounded figure
	nudges it: an issue of 20 against a MAP of 13.333333 leaves 13.333308, and 7
	of 11 issue quantities moved a MAP that the signed plan fixes as unchanged
	on issues and on returns without reference. The drift accumulates over
	every issue and makes the MAP
	irreproducible from the documented formula.

	Counts and reconciliations already pin the MAP this way; issues and
	returns-without-reference now follow the same rule.
	"""
	if flt(ipb.closing_qty) > 0:
		ipb.moving_avg_price = r6(map_before)


def _freeze_check(ipb):
	if flt(ipb.closing_qty) < 0 and not ipb.is_negative:
		ipb.is_negative = 1
		ipb.frozen_map = flt(ipb.moving_avg_price)
	elif flt(ipb.closing_qty) >= 0 and ipb.is_negative:
		ipb.is_negative = 0
		ipb.frozen_map = 0
	if flt(ipb.closing_qty) == 0:
		ipb.total_received_since_zero = 0


def maybe_rounding_cleanup(controller, scope, ipb, source, posting_date, qty_scale=0):
	"""Mandatory zero-qty cleanup (signed plan): when closing_qty hits 0 with a
	residual value within tolerance, clear it to Stock Rounding Adjustment. The
	MAP is retained (see below). Called from the receipt, issue, transfer and
	reconciliation paths.

	Residuals LARGER than that are deliberately not swept here: they are real
	amounts, not rounding, and the Apr-22 rule forbids writing anything off
	automatically. `period_close.assert_no_stranded_value` refuses to close the
	period while one exists, so it is surfaced instead of buried.
	"""
	if flt(ipb.closing_qty) != 0:
		return
	# WA-0003-01 #17: measure the residual at FULL stored precision (r6). The
	# earlier r2() here rounded sub-cent residuals (e.g. -0.0015 left by an
	# r6 issue at a repeating-decimal MAP) to 0.00, so they were never
	# cleaned; the next receipt then blended against the leftover and the
	# fresh MAP came out 399.9995 instead of the receipt price 400 (client's
	# IPB co9dsrrq3p). GL legs are only posted when the residual is
	# representable at GL precision - a sub-cent cleanup is value-side only
	# (the IVE keeps the audit trail; a 0.00 GL row is meaningless).
	residual = r6(flt(ipb.closing_value))
	tolerance = flt(get_pma_setting(scope.company, "rounding_tolerance")) or 0.01
	# The arithmetic residual scales with the movement: an r6 MAP multiplied by
	# a quantity can only be wrong by qty x 5e-7, so a 3,000,000-unit issue
	# legitimately leaves up to 1.50 of pure rounding. Measuring that against a
	# flat 0.01 would classify ordinary bulk movements as stranded value and
	# refuse every month-end close - cement quantities are in tonnes and kg.
	limit = max(tolerance, abs(flt(qty_scale)) * 5e-7)
	map_before = flt(ipb.moving_avg_price)
	# The MAP is RETAINED at zero quantity (client behaviour review 2026-08-18,
	# MAT-STE-2026-00150; ruled 2026-08-18). It is the SAP material-master
	# behaviour, and zeroing it was not cosmetic: frozen_map is captured FROM
	# the MAP, so an issue taken from an exactly-zero balance froze at 0 and
	# left stock at no cost at all - 100 in, 100 out, then 50 more out booked
	# ZERO COGS, while the same 150 issued in one movement booked 1,500. The
	# next receipt is unaffected either way (value is 0, so it re-prices to its
	# own rate). Supersedes plan §725's "MAP -> 0" and the workbook's
	# Zero Qty Reset sheet on this point. The since-zero counter is still reset
	# here: it is a per-cycle receiving history kept for audit, and no longer
	# drives any value split (DR-32).
	if residual and abs(residual) <= limit:
		ipb.reval_value = r6(flt(ipb.reval_value) - residual)
		recompute_closing(ipb)
		__, ive = write_events(
			scope, ipb, source=source, posting_date=posting_date,
			movement_type=None, reason="rounding_cleanup", qty_delta=0,
			value_delta=-residual, map_before=map_before,
		)
		scope.save(ipb, caused_by=ive, source=source)
		gl_residual = r2(residual)
		if gl_residual:
			inventory_account = get_inventory_account(scope.company, scope.item_code, scope.physical_warehouse)
			rounding_account = get_offset_account(
				scope.company, scope.item_code, scope.physical_warehouse, "rounding_cleanup"
			)
			post_gl(
				controller, posting_date,
				[(rounding_account, gl_residual, inventory_account), (inventory_account, -gl_residual, rounding_account)],
				ive,
			)


def _voucher_expense_account(controller, sle):
	"""Expense/offset for issue-side GL: row override -> Item/Item Group
	default expense -> Company default expense account. Never SRBNB."""
	detail = sle.get("voucher_detail_no")
	for row in controller.get("items") or []:
		if row.name == detail and row.get("expense_account"):
			return row.get("expense_account")
	return (
		get_offset_account(
			controller.company, sle.get("item_code"), sle.get("warehouse"), "expense"
		)
		or frappe.get_cached_value("Company", controller.company, "default_expense_account")
	)


def _original_rate(controller, sle):
	"""Rate + valuation event of the original document row a return references."""
	detail = sle.get("voucher_detail_no")
	original_detail = None
	for row in controller.get("items") or []:
		if row.name == detail:
			original_detail = (
				row.get("purchase_receipt_item") or row.get("dn_detail")
				or row.get("delivery_note_item") or row.get("purchase_invoice_item")
				or row.get("sales_invoice_item")
			)
			break
	ive = None
	if original_detail:
		ive = frappe.db.get_value(
			"Inventory Valuation Event",
			{"source_detail_name": original_detail, "is_cancelled": 0},
			["name", "value_delta", "qty_basis"],
			as_dict=True,
		)
	if ive and flt(ive.qty_basis):
		return abs(flt(ive.value_delta) / flt(ive.qty_basis)), ive.name
	# fallback: the return row's own rate (copied from the original document)
	return flt(sle.get("incoming_rate")) or flt(sle.get("outgoing_rate")), None


def _post_cancellation(controller, scope, period, ipb, sle, source, inventory_account, expense):
	"""Dated mirror of the original document's events; original stays intact."""
	original = controller.get("cancellation_against")
	if not original:
		frappe.throw(_("Cancellation document must reference the original via Cancellation Against."))
	# Match the original event(s) by item within the original document. We do
	# NOT filter by source_detail_name: on a copied Cancellation document the
	# item rows are new, and a return's `purchase_receipt_item` points at the
	# grand-original receipt, not the return's own event - so a detail-name
	# filter wrongly finds nothing (WA-0003-01 item 9). Instead we take the
	# original doc's events for this item, drop any already reversed by a live
	# cancellation, and pair the line to one by matching quantity.
	candidates = frappe.get_all(
		"Inventory Valuation Event",
		filters={"source_doctype": controller.doctype, "source_docname": original,
			"item_code": scope.item_code, "is_cancelled": 0},
		fields=["name", "value_delta", "qty_basis", "reason_code", "prd_amount",
			"inventory_portion", "expense_portion"],
		order_by="creation",
	)
	unreversed = [
		e for e in candidates
		if not frappe.db.exists(
			"Inventory Valuation Event", {"reversal_of": e.name, "is_cancelled": 0}
		)
	]
	if not unreversed:
		if candidates:
			frappe.throw(
				_("{0} is already reversed.").format(original),
				title=_("Double Reversal Blocked"),
			)
		frappe.throw(_("No valuation events found for {0} to cancel.").format(original))

	posting_date = sle.get("posting_date")
	map_before = flt(ipb.moving_avg_price)
	# the cancellation document carries the original's positive quantities;
	# the kernel posts the mirror
	qty = -flt(sle.get("actual_qty"))

	want = abs(flt(sle.get("actual_qty")))
	orig = next((e for e in unreversed if flt(e.qty_basis) == want), unreversed[0])

	# A Create Cancellation is only eligible while the original's period is
	# still OPEN or PREV_OPEN_UNSETTLED. Once the period is closed it stays
	# closed (DR-21) - there is no settlement to reverse in Moving Average, so
	# the correction is a fresh current-period entry, not a reach-back.
	orig_period = frappe.db.get_value(
		"Inventory Valuation Event", orig.name, ["period_year", "period_month"], as_dict=True
	)
	orig_period_status = frappe.db.get_value(
		"Inventory Period",
		{"company": scope.company, "period_year": orig_period.period_year,
			"period_month": orig_period.period_month},
		"status",
	)
	if orig_period_status not in ("OPEN", "PREV_OPEN_UNSETTLED"):
		frappe.throw(
			_(
				"The original posting's period {0}-{1:02d} is {2}, so it can no longer be cancelled "
				"- a closed period is not reopened. To correct its effect, post a new entry (e.g. "
				"Stock Count or Stock Reconciliation) in the current open period; it is valued at "
				"the current moving average."
			).format(orig_period.period_year, orig_period.period_month, orig_period_status or _("closed")),
			title=_("Cancellation Not Eligible"),
		)

	# A Cancellation draft carries the original's quantities, but the user can
	# edit them down before submitting, so the reversal may cover only part of
	# the original event. The ledger value is pro-rated on that share - and the
	# mirrored GL below has to be pro-rated by the SAME share. Mirroring the
	# original's full GL amounts against a pro-rated ledger value left inventory
	# overstated by the difference, permanently, with nothing to point at: on the
	# client's bench a 60%-quantity cancellation of a 5,000 issue wrote 3,000 to
	# the ledger and 5,000 to GL, and the resulting 2,000 gap made the
	# reconciliation gate unpassable (MAT-STE-2026-00023).
	share = (abs(qty) / flt(orig.qty_basis)) if flt(orig.qty_basis) else 1.0
	value = r6(-flt(orig.value_delta) * share)
	orig_qty = -qty  # the cancelled share of the original's SIGNED quantity

	# A cancellation nets the bucket the ORIGINAL event filled - it is never
	# re-classified by its physical direction (OI-5 bucket mapping, confirmed
	# by the client in the 2026-08-18 behaviour review, MAT-PRE-2026-00375:
	# "Purchase return and Sales return same behavior ... Netting the OUT/IN";
	# recorded as DR-31). Bucketing by direction painted a different period
	# than the equivalent return: cancelling a 1,500 receipt left In at 1,500
	# AND pushed Out up by 1,500, so the balance read "received 2,000, issued
	# 2,000" where the truth was 500/500 - and left the since-zero counter
	# carrying cancelled goods, overstating the audited receiving history with
	# stock that was never kept (the same defect returns had, WA-0003-01
	# item 7). A cancellation shrinks the receipt statistics; it is not a
	# goods issue.
	# Closing qty/value, MAP, GL, SLE and the immutable events are unchanged -
	# every branch below moves closing_value by exactly `value`, matching the
	# scaled GL mirror; gross movement stays visible in the Stock Movement
	# Event log.
	RETURN_REASONS = ("return_with_ref", "return_no_ref")
	# DR-44: a reversal is exact on quantity and reference, but its inventory
	# value leg is FLOORED at the value the scope actually carries. Issues in
	# between consumed part of the cancelled receipt's value at the blended
	# MAP, so an exact value mirror would drive the scope's value negative -
	# stranded value the period-close gate refuses and the Apr-22 rule forbids
	# writing off. The shortfall posts to PRD (the Cost Adjustment tree's
	# account for value beyond zero, DR-34), mirroring the over-blended
	# consumption already booked. Additive reversals (issue cancellations)
	# stay exact: they cannot strand.
	is_receipt_family = orig.reason_code in ("receipt", "receipt_neg", "receipt_cross_zero") or (
		orig.reason_code in RETURN_REASONS and orig_qty < 0
	)
	prd_floor = 0.0
	# the floor guards the STRANDING case only (resulting qty stays >= 0 while
	# value would go negative). A cancellation that takes the quantity itself
	# negative is a legitimate negative-stock excursion (OI-5): its value
	# follows the frozen-MAP machinery exactly and must not be floored.
	if is_receipt_family and value < 0 and flt(ipb.closing_qty) + qty >= 0:
		available = max(flt(ipb.closing_value), 0.0)
		if r6(available + value) < 0:
			prd_floor = r2(available + value)  # negative: the part inventory cannot cover
			value = r6(-available)
	if is_receipt_family:
		# the original filled (or, for a purchase return, netted) the In side:
		# take the cancelled share back out of it. receipt_value carried the
		# GROSS receipt value for negative-stock receipts, with the PRD offset
		# in prd_value - mirror both so the breakdown reverses exactly.
		prd_share = r6(flt(orig.prd_amount) * share)
		bucket_value = r6(flt(orig.value_delta) * share + prd_share)
		ipb.receipt_qty = r6(flt(ipb.receipt_qty) - orig_qty)
		ipb.receipt_value = r6(flt(ipb.receipt_value) - bucket_value)
		if prd_share:
			ipb.prd_value = r6(flt(ipb.prd_value) + prd_share)
		if prd_floor:
			# the receipt bucket still shrinks by the gross cancelled value;
			# the uncovered part sits in the PRD bucket so the movement table
			# mirrors the PRD GL leg (same convention as receipt_neg)
			ipb.prd_value = r6(flt(ipb.prd_value) - prd_floor)
		# the counter measures the same receipts the bucket does (WA-0003-01
		# item 7): net it with the bucket, floored - a cancellation can never
		# imply negative receipts. (For a cancelled purchase return orig_qty is
		# negative, so this re-adds what the return had netted off.)
		ipb.total_received_since_zero = r6(
			max(0.0, flt(ipb.total_received_since_zero) - orig_qty)
		)
	elif orig.reason_code == "issue" or orig.reason_code in RETURN_REASONS:
		# the original filled (or, for a sales return, netted) the Out side
		ipb.issue_qty = r6(flt(ipb.issue_qty) + orig_qty)
		ipb.issue_value = r6(flt(ipb.issue_value) + r6(flt(orig.value_delta) * share))
	elif qty > 0:
		# transfers and other reasons keep the physical-direction fallback:
		# their forward legs either touch no In/Out bucket (company-scope
		# transfer) or a leg-specific one, and a mirrored pair nets to zero
		ipb.receipt_qty = r6(flt(ipb.receipt_qty) + qty)
		ipb.receipt_value = r6(flt(ipb.receipt_value) + value)
	elif qty < 0:
		ipb.issue_qty = r6(flt(ipb.issue_qty) - qty)
		ipb.issue_value = r6(flt(ipb.issue_value) - value)
	else:
		ipb.reval_value = r6(flt(ipb.reval_value) + value)
	recompute_closing(ipb)
	_freeze_check(ipb)

	orig_sme = frappe.db.get_value("Inventory Valuation Event", orig.name, "movement_event_id")
	sme, ive = write_events(
		scope, ipb, source=source, posting_date=posting_date,
		movement_type="cancellation", reason="cancellation", qty_delta=qty,
		value_delta=value, map_before=map_before, reversal_of=orig.name,
		movement_reversal_of=orig_sme, stock_uom=sle.get("stock_uom"),
		# the mirror's P&L side is the original's, reversed and scaled - the
		# derivation cannot know it (reason is just "cancellation")
		expense_portion=r2(-flt(orig.expense_portion) * share),
		prd_amount=r2(prd_floor),
	)
	scope.save(ipb, caused_by=ive, movement_event=sme, source=source)
	mirrored_sle = dict(sle)
	mirrored_sle["actual_qty"] = qty
	write_sle(controller, mirrored_sle, scope, ipb, value)
	# mirror the original event's GL with swapped sides on the cancellation date,
	# scaled to the share of the original this cancellation actually reverses
	legs = []
	for g in frappe.get_all(
		"GL Entry",
		filters={"valuation_event_id": orig.name, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		legs.append((g.account, r2((flt(g.credit) - flt(g.debit)) * share), inventory_account))
	if prd_floor:
		# cap the inventory credit at the covered value; the shortfall credits
		# PRD (DR-44). The offset side keeps the original's full amount.
		for i, (acct, amt, against) in enumerate(legs):
			if acct == inventory_account:
				legs[i] = (acct, r2(amt - prd_floor), against)
				break
		prd_account = get_offset_account(scope.company, scope.item_code, scope.physical_warehouse, "prd")
		legs.append((prd_account, r2(prd_floor), inventory_account))
	post_gl(controller, posting_date, legs, ive)


# ------------------------------------------------------------ backdated flow
def _post_backdated(controller, scope, prior_period, open_period, sle, is_return):
	"""Backdated posting into the PREV_OPEN_UNSETTLED period.

	Receipt into positive prior: plain math in prior + carryover to current.
	Receipt into negative prior: PRD math (Case A/B) in prior + carryover +
	cross-period absorb in the current period (C1/C2). Issues: plain prior
	math + carryover.
	"""
	qty = flt(sle.get("actual_qty"))
	if is_return or controller.get("is_cancellation"):
		frappe.throw(
			_("Backdated returns/cancellations into a previous period are not supported; post into the current period."),
		)

	# lock order: previous then current
	ipb_prior = scope.load(prior_period)
	ipb_cur = scope.load(open_period)
	source = (controller.doctype, controller.name, sle.get("voucher_detail_no"))
	posting_date = sle.get("posting_date")
	map_before_prior = flt(ipb_prior.moving_avg_price)
	inventory_account = get_inventory_account(scope.company, scope.item_code, scope.physical_warehouse)
	srbnb = frappe.get_cached_value("Company", scope.company, "stock_received_but_not_billed")

	if qty <= 0:
		# backdated issue at the prior period's MAP
		rate = flt(ipb_prior.frozen_map) if ipb_prior.is_negative else flt(ipb_prior.moving_avg_price)
		issue_value = r2(-qty * rate)
		ipb_prior.issue_qty = r6(flt(ipb_prior.issue_qty) - qty)
		ipb_prior.issue_value = r6(flt(ipb_prior.issue_value) + issue_value)
		recompute_closing(ipb_prior)
		_pin_map(ipb_prior, map_before_prior)  # an issue never moves the MAP
		_freeze_check(ipb_prior)
		sme, ive = write_events(
			scope, ipb_prior, source=source, posting_date=posting_date,
			movement_type="issue", reason="issue", qty_delta=qty,
			value_delta=-issue_value, map_before=map_before_prior, stock_uom=sle.get("stock_uom"),
		)
		scope.save(ipb_prior, caused_by=ive, movement_event=sme, source=source)
		map_before_cur = flt(ipb_cur.moving_avg_price)
		ipb_cur.carryover_qty = r6(flt(ipb_cur.carryover_qty) + qty)
		ipb_cur.carryover_value = r6(flt(ipb_cur.carryover_value) - issue_value)
		recompute_closing(ipb_cur)
		# An issue never moves the MAP - but the carry arrived at the PRIOR
		# period's rate, which would move it. Re-price the carried units at the
		# current period's own MAP and send the difference to the issue's
		# expense account: -(rate - MAP) x qty (MAP Rule, 31 Aug 2026; confirmed
		# 1 Sep). A frozen (negative) period re-prices at its frozen MAP.
		expense = _voucher_expense_account(controller, sle) or srbnb
		target_rate = flt(ipb_cur.frozen_map) if ipb_cur.is_negative else map_before_cur
		keep_map = r2(flt(ipb_cur.closing_qty) * target_rate - flt(ipb_cur.closing_value))
		if keep_map:
			ipb_cur.adjust_value = r6(flt(ipb_cur.adjust_value) + keep_map)
			recompute_closing(ipb_cur)
		_pin_map(ipb_cur, map_before_cur)  # still an issue in the current period's eyes
		_freeze_check(ipb_cur)
		scope.save(ipb_cur, caused_by=ive, source=source)
		write_sle(controller, sle, scope, ipb_cur, -issue_value + keep_map)
		post_gl(
			controller, posting_date,
			[(expense, issue_value, inventory_account), (inventory_account, -issue_value, expense)],
			ive,
		)
		if keep_map:
			first_of_open = f"{open_period.period_year}-{open_period.period_month:02d}-01"
			__, keep_ive = write_events(
				scope, ipb_cur, source=source, posting_date=first_of_open,
				movement_type=None, reason="revaluation", qty_delta=0, value_delta=keep_map,
				map_before=map_before_cur, caused_by=ive,
			)
			post_gl(
				controller, first_of_open,
				[(inventory_account, keep_map, expense), (expense, -keep_map, inventory_account)],
				keep_ive,
			)
		return

	rate = flt(sle.get("incoming_rate"))
	prior_was_negative = bool(ipb_prior.is_negative)
	result = _apply_receipt(ipb_prior, qty, rate)
	sme, ive = write_events(
		scope, ipb_prior, source=source, posting_date=posting_date,
		movement_type="receipt", reason=result["reason"], qty_delta=qty,
		value_delta=result["net_to_inventory"], map_before=map_before_prior,
		prd_amount=result["prd"], affects_map=1, stock_uom=sle.get("stock_uom"),
		exchange_rate_at_receipt=_receipt_fx(controller),
	)
	scope.save(ipb_prior, caused_by=ive, movement_event=sme, source=source)

	legs = [
		(inventory_account, result["receipt_value"], srbnb),
		(srbnb, -result["receipt_value"], inventory_account),
	]
	if result["prd"]:
		prd_account = get_offset_account(scope.company, scope.item_code, scope.physical_warehouse, "prd")
		legs += [(prd_account, result["prd"], inventory_account),
			(inventory_account, -result["prd"], prd_account)]
	post_gl(controller, posting_date, legs, ive)

	# carryover into the open period
	ipb_cur.carryover_qty = r6(flt(ipb_cur.carryover_qty) + qty)
	ipb_cur.carryover_value = r6(flt(ipb_cur.carryover_value) + result["net_to_inventory"])

	# The current period's "should-have" inventory effect of this receipt,
	# measured against the CURRENT period's stock state (MAP Rule, 31 Aug 2026:
	# universal forward cascade). Whatever the prior period booked to inventory
	# is corrected here and the difference is a price difference in the
	# current period:
	#   current negative before the carry -> deficit at the current frozen MAP,
	#     excess at the receipt rate (C2, and the client's Special Cases #1/#2
	#     where the prior period was positive);
	#   current positive, prior negative   -> the full receipt value (C1);
	#   both positive                      -> nothing to correct.
	absorb = 0.0
	cur_qty_before = flt(ipb_cur.closing_qty)
	if cur_qty_before < 0:
		clearing_c = min(qty, -cur_qty_before)
		excess_c = r6(qty - clearing_c)
		should_have = r6(clearing_c * flt(ipb_cur.frozen_map) + excess_c * rate)
		absorb = r2(should_have - result["net_to_inventory"])
	elif prior_was_negative:
		absorb = r2(result["receipt_value"] - result["net_to_inventory"])

	# Strictly ABOVE zero is a crossing; landing exactly on zero is not. This has
	# to mirror _apply_receipt's `closing + qty <= 0` boundary - when the two
	# disagree, a backdated receipt that lands the current period on exactly zero
	# re-prices MAP to its own rate instead of keeping the frozen MAP
	# (WA-0003-01 item 22). _freeze_check below still clears the frozen state.
	crossed_current_zero = ipb_cur.is_negative and flt(ipb_cur.closing_qty) + qty > 0
	if crossed_current_zero:
		# Fresh cycle on zero-crossing (signed rule; mirrors _apply_receipt):
		# only the excess above the deficit counts as "received since zero".
		ipb_cur.total_received_since_zero = r6(qty + flt(ipb_cur.closing_qty))
	else:
		ipb_cur.total_received_since_zero = r6(flt(ipb_cur.total_received_since_zero) + qty)

	if absorb:
		ipb_cur.adjust_value = r6(flt(ipb_cur.adjust_value) + absorb)
	recompute_closing(ipb_cur)
	if crossed_current_zero:
		ipb_cur.is_negative = 0
		ipb_cur.frozen_map = 0
		ipb_cur.moving_avg_price = rate
	_freeze_check(ipb_cur)

	absorb_ive = None
	if absorb:
		first_of_open = f"{open_period.period_year}-{open_period.period_month:02d}-01"
		__, absorb_ive = write_events(
			scope, ipb_cur, source=source, posting_date=first_of_open,
			movement_type=None, reason="prd_split", qty_delta=0, value_delta=absorb,
			prd_amount=r2(-absorb), map_before=flt(ipb_cur.moving_avg_price), caused_by=ive,
		)
		# the offset is the price-difference account - the same account the
		# prior-period leg used: receipt price vs frozen MAP is one economic
		# thing wherever it lands (client convention, 1 Sep 2026)
		prd_account = get_offset_account(scope.company, scope.item_code, scope.physical_warehouse, "prd")
		post_gl(
			controller, first_of_open,
			[(inventory_account, absorb, prd_account), (prd_account, -absorb, inventory_account)],
			absorb_ive,
		)

	scope.save(ipb_cur, caused_by=absorb_ive or ive, source=source)
	write_sle(controller, sle, scope, ipb_cur, result["net_to_inventory"] + absorb)


# ------------------------------------------------------- value-only postings
def post_value_event(company, item_code, warehouse, *, source, posting_date, reason,
		value_delta, offset_account, qty_delta=0.0, movement_type=None,
		expense_portion=None, fx_variance=0.0, offset_is_credit=True):
	"""Shared writer for MR21 revaluation / stock count / LCV / invoice-diff
	events posted by the transaction-layer doctypes."""
	scope = ScopeState(company, item_code, warehouse)
	period = assert_posting_allowed(company, posting_date)
	ipb = scope.load(period)
	map_before = flt(ipb.moving_avg_price)
	inventory_account = get_inventory_account(company, item_code, warehouse)

	# Cost Adjustment tree (client, 23 Aug 2026): a cost adjustment may take the
	# inventory value down to exactly zero and no further - anything beyond that
	# is a price difference, not negative inventory. Split here so the posting
	# succeeds with the right accounts instead of being refused.
	if reason in ("landed_cost", "invoice_diff", "fx_adjust", "revaluation") and flt(value_delta) < 0 \
			and flt(ipb.closing_qty) > 0 and flt(ipb.closing_value) + flt(value_delta) < 0:
		excess = r2(flt(ipb.closing_value) + flt(value_delta))   # negative: the part below zero
		value_delta = r2(-flt(ipb.closing_value))
		expense_portion = r2(flt(expense_portion or 0) + excess)
	if reason == "revaluation":
		if flt(ipb.closing_qty) <= 0:
			frappe.throw(_("Revaluation requires positive on-hand quantity for {0}.").format(item_code))
		ipb.reval_value = r6(flt(ipb.reval_value) + value_delta)
	elif reason == "count_diff":
		rate = flt(ipb.frozen_map) if ipb.is_negative else flt(ipb.moving_avg_price)
		value_delta = r2(qty_delta * rate)
		ipb.adjust_qty = r6(flt(ipb.adjust_qty) + qty_delta)
		ipb.adjust_value = r6(flt(ipb.adjust_value) + value_delta)
	elif reason in ("landed_cost", "invoice_diff", "fx_adjust"):
		ipb.reval_value = r6(flt(ipb.reval_value) + value_delta)
	else:
		frappe.throw(_("Unsupported value event {0}").format(reason))

	recompute_closing(ipb)
	if reason == "count_diff":
		# count difference never moves MAP
		ipb.moving_avg_price = map_before if not ipb.is_negative else ipb.moving_avg_price
	_guard_positive_value(ipb, item_code, reason)
	_freeze_check(ipb)

	sme, ive = write_events(
		scope, ipb, source=source, posting_date=posting_date,
		movement_type=movement_type, reason=reason, qty_delta=qty_delta,
		value_delta=value_delta, map_before=map_before,
		expense_portion=expense_portion, fx_variance=fx_variance,
		affects_map=0 if reason == "count_diff" else 1,
	)
	scope.save(ipb, caused_by=ive, movement_event=sme, source=source)

	# Backdated value event (e.g. a Stock Count dated in the previous period):
	# the event lands in its own period correctly, but every LATER period's
	# carryover must absorb the same qty/value delta or the current balance
	# and MAP go stale (WA-0003-01 item 8). No extra GL - carryover is a pure
	# balance-propagation bucket, as in the backdated receipt/issue path.
	_cascade_value_carryover(
		scope, period,
		qty_delta=qty_delta if reason == "count_diff" else 0.0,
		value_delta=value_delta,
		source=source,
	)

	# Keep the Bin (physical shadow) in step with the ledger. A Stock Count
	# moves quantity; revaluation / landed cost / invoice-diff move only value.
	# Without this the Bin drifts from the IPB and Stock Balance disagrees with
	# the valuation ledger (surfaced by shared.integrity.check_bin_ipb_drift).
	_sync_bin_to_ledger(scope, qty_delta if reason == "count_diff" else 0.0)

	# ...and keep the Stock Ledger in step too (DR-02). The Bin alone is not
	# enough: Stock Balance and the other core reports sum SLE
	# stock_value_difference, so without this row they under-report inventory
	# value by exactly this event's amount.
	write_value_sle(
		scope, ipb, source=source, posting_date=posting_date,
		value_delta=value_delta,
		qty_delta=qty_delta if reason == "count_diff" else 0.0,
	)

	legs = [(inventory_account, value_delta, offset_account)]
	total_offset = value_delta + (expense_portion or 0) + (fx_variance or 0)
	if expense_portion:
		price_diff = get_offset_account(company, item_code, warehouse, "price_difference")
		legs.append((price_diff, expense_portion, offset_account))
	if fx_variance:
		fx_account = get_offset_account(company, item_code, warehouse, "fx_gain_loss")
		legs.append((fx_account, fx_variance, offset_account))
	legs.append((offset_account, -total_offset, inventory_account))

	class _Ctl:  # minimal gl_dict context for standalone value events
		def get_gl_dict(self, row):
			row.setdefault("company", company)
			row.setdefault("posting_date", posting_date)
			row.setdefault("voucher_type", source[0])
			row.setdefault("voucher_no", source[1])
			return frappe._dict(row)

		def get(self, key, default=None):
			return default

	ctl = _Ctl()
	ctl.company = company
	post_gl(ctl, posting_date, legs, ive)
	return ive


def get_coverage_ratio(company, item_code, warehouse, basis_qty, as_of=None):
	"""Inventory share of a value-only late cost = STOCK COVERAGE, clamped
	[0, 1]: the scope's on-hand quantity against the quantity the charge
	applies to - the invoiced quantity for a purchase-invoice difference, the
	receipt row's quantity for a landed cost charge (DR-32).

	Fully covered -> the whole difference capitalises; partially covered ->
	proportional; each document is judged ON ITS OWN against on-hand as of its
	posting period, so two invoices billing the same receipt each check
	coverage independently. This replaces the earlier pool ratio
	(closing_qty / total_received_since_zero), whose denominator carried the
	whole receiving history and therefore charged a documentless share of
	every late cost to price difference once a scope had cycled through
	stock. The since-zero counter stays on the balance for audit but no longer
	drives valuation. On-hand resolves as of the posting period, not the
	latest one - a backdated invoice or landed cost must not read a future
	state (WA-0003-01 item 8 rule).
	"""
	if flt(basis_qty) <= 0:
		return 0.0
	scope = ScopeState(company, item_code, warehouse)
	rows = frappe.get_all(
		"Inventory Period Balance",
		filters={"company": company, "item_code": item_code, "warehouse": scope.warehouse or ""},
		fields=["closing_qty", "total_received_since_zero", "period_year", "period_month"],
		order_by="period_year desc, period_month desc",
		limit=60,
	)
	if as_of:
		d = getdate(as_of)
		rows = [r for r in rows if (r.period_year, r.period_month) <= (d.year, d.month)]
	on_hand = flt(rows[0].closing_qty) if rows else 0.0
	if on_hand <= 0:
		return 0.0
	# Cost Adjustment tree (client, 23 Aug 2026; confirmed 1 Sep): on hand covers
	# the adjusted quantity -> all to inventory; otherwise the inventory share is
	# on hand over TOTAL RECEIVED SINCE THE LAST ZERO (the pool ratio), not over
	# the adjusted quantity. Equal counts as covered.
	if on_hand >= flt(basis_qty):
		return 1.0
	since_zero = flt(rows[0].total_received_since_zero)
	if since_zero <= 0:
		return 1.0
	return min(max(on_hand / since_zero, 0.0), 1.0)
