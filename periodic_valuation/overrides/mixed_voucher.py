# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Interim mixed-voucher refusal (D-030 term 2).

A document that carries kernel-routed items is reversed by a dated Cancellation
document (cancellation.py). That copy is understood by the valuation kernel only:
every other owner of rows on the same document - ERPNext core for its own stock
and asset rows, project accounting for AuC rows - reads the copy as a NEW posting
(probes 2026-09-17: a routed + FIFO receipt doubled the FIFO row; a routed +
asset receipt created a second asset and posted ARBNB twice; a routed + AuC
receipt debited AuC twice). Until the platform coordinator reverses every owner's
rows together, such a document must not exist: it is refused at validate, with
the split named, and the runbook says to raise the rows on separate vouchers.

Who posts is asked, never guessed from another app's fieldnames:
- core's answer is computed here from core semantics alone (is_stock_item,
  is_fixed_asset, the company's provisional-accounting flag);
- any other app registers `mixed_voucher_posters = ["dotted.path"]` in its hooks;
  each path is `fn(doc) -> bool` ("I post my own ledger rows from this document").

The refusal is bypassed only under `frappe.flags.qcs_allow_mixed_voucher`, which
smoke fixtures set to build the very documents the reversal fixes are proven on.
Lifecycle: at qcs_platform 0.1 this module and the hook are replaced by the
LedgerAdapter.posts_rows answers behind the platform dispatcher (Build 0.1 §5.3);
at 0.3 the coordinator reverses every owner together and the refusal is deleted.
"""

import frappe
from frappe import _

from periodic_valuation.overrides.cancel_guard import get_kernel_methods

# reversed by a Cancellation COPY (cancellation.py `copy_doc`): core re-posts
# every row it owns on the copy
COPY_REVERSED = ("Purchase Receipt", "Delivery Note", "Stock Entry", "Subcontracting Receipt", "Landed Cost Voucher")
# reversed by core's own Return document (debit / credit note): core rows reverse
# natively, only asset allocation is re-read (probe (b))
RETURN_REVERSED = ("Purchase Invoice", "Sales Invoice")

POSTERS_HOOK = "mixed_voucher_posters"


def _is_routed(item_code, company):
	# the one routing test of the app (cancel_guard.has_routed_items uses the same)
	from erpnext.stock.utils import get_valuation_method

	return get_valuation_method(item_code, company) in get_kernel_methods()


def _item_facts(item_code):
	is_stock, is_fa = frappe.get_cached_value("Item", item_code, ["is_stock_item", "is_fixed_asset"]) or (0, 0)
	return bool(is_stock), bool(is_fa)


def _rows(doc):
	return [r for r in (doc.get("items") or []) if r.get("item_code")]


def valuation_posts(doc):
	"""Rows the periodic kernel posts: items on a kernel valuation method."""
	return [r for r in _rows(doc) if _is_routed(r.item_code, doc.company)]


def core_posts(doc):
	"""Rows ERPNext core posts SLE / GL for itself and would re-post on a
	Cancellation copy - per reversal path (D-030 term 2)."""
	provisional = bool(
		frappe.get_cached_value("Company", doc.company, "enable_provisional_accounting_for_non_stock_items")
	)
	out = []
	for r in _rows(doc):
		is_stock, is_fa = _item_facts(r.item_code)
		if is_fa or r.get("is_fixed_asset"):
			out.append(r)  # both paths: core creates the asset / re-reads the allocation
		elif doc.doctype in COPY_REVERSED:
			if is_stock and not _is_routed(r.item_code, doc.company):
				out.append(r)  # native SLE + stock GL
			elif not is_stock and provisional and doc.doctype in ("Purchase Receipt", "Subcontracting Receipt"):
				out.append(r)  # provisional expense GL pair
	return out


def other_posters(doc):
	"""Apps that declare they post their own ledger rows from this document."""
	names = []
	for path in frappe.get_hooks(POSTERS_HOOK) or []:
		if frappe.get_attr(path)(doc):
			names.append(path.split(".")[0])
	return names


def refuse_mixed_voucher(doc, method=None):
	if frappe.flags.qcs_allow_mixed_voucher or frappe.flags.in_install or frappe.flags.in_patch:
		return
	if doc.doctype not in COPY_REVERSED + RETURN_REVERSED:
		return
	routed = valuation_posts(doc)
	if not routed:
		return
	core = core_posts(doc)
	others = other_posters(doc)
	if not core and not others:
		return

	owners = []
	if core:
		owners.append(_("ERPNext core (rows {0})").format(", ".join(f"#{r.idx}" for r in core)))
	for app in others:
		owners.append(app.replace("_", " ").title())
	if doc.get("is_cancellation") or doc.get("is_return"):
		# a reversal of a mixed original that predates this rule: the original's
		# rows cannot be split after the fact; that document is reversed only by
		# the owners together (D-030 term 1 fixes / platform coordinator)
		next_action = _(
			"The original document predates this rule; its reversal needs every owner's rows reversed "
			"together and is not available yet - do not reverse it by hand."
		)
	else:
		next_action = _("Raise the periodic-valuation items on a separate {0}.").format(_(doc.doctype))
	frappe.throw(
		_(
			"This {0} mixes periodic-valuation items (rows {1}) with rows that another owner posts "
			"for itself: {2}. Such a document cannot be reversed consistently yet - the Cancellation "
			"document would re-post the other owner's rows. {3}"
		).format(_(doc.doctype), ", ".join(f"#{r.idx}" for r in routed), "; ".join(owners), next_action),
		title=_("Mixed Voucher Refused"),
	)
