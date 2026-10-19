# Copyright (c) 2026, Quark Cyber Systems and contributors
# For license information, please see license.txt

"""The one GL writer of both valuation kernels.

Core posts stock GL per voucher row and gives every leg that row's cost
centre, project, accounting dimensions and row (voucher_detail_no). The
platform routes periodic-valuation rows here instead of to core, so these
legs must carry the same attribution - client PA-028: a Material Issue
tagged with a project on its rows posted GL on the company cost centre
with no project, and the cost never reached the project.

- an event's legs carry the attribution of the voucher row the event comes
  from (`source_detail_name`), snapshotted on the event the first time it
  posts (`gl_attribution`);
- a landed cost belongs to the receipt row it lands on: that row's
  attribution, on the voucher's own row;
- an event of no voucher row - standard-cost settlement and its variances,
  cost-version release, scope-level sweeps - posts on the company cost
  centre with no dimensions (Vivek, 19/10/2026: settlement variances stay
  where they post today);
- a mirror reverses what the original posted, attribution included: the
  original's live GL netted by account and attribution, each leg on the
  reversing document's row that undoes the original's row (the row the
  kernel is posting, or the reversing row's own link to it - never its
  position).

The attribution rules themselves are the platform's
(`qcs_platform.ledger.attribution`); this module only decides which row
an event belongs to.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt
from qcs_platform.ledger import attribution as attr

from periodic_valuation.periodic_moving_average.kernel import r2

IVE = "Inventory Valuation Event"
GL_ENTRY = "GL Entry"
IVE_FIELDS = ("name", "company", "source_doctype", "source_docname", "source_detail_name", "reversal_of",
	"gl_attribution")
# the link a reversing row carries to the row it undoes when the reversing
# document is a native return of the original (core's return links)
RETURN_ROW_LINKS = ("purchase_receipt_item", "purchase_invoice_item", "dn_detail", "sales_invoice_item")


class Resolver:
	"""Documents and events read once for one posting or one repair run.
	Lives as long as the call that made it: nothing is cached across
	requests, so a voucher is never read as it was before a rollback."""

	def __init__(self, controller=None):
		self.controller = controller
		self.docs = {}
		self.ives = {}

	def doc(self, doctype, name):
		c = self.controller
		if c is not None and (c.doctype, c.name) == (doctype, name):
			return c
		if (doctype, name) not in self.docs:
			if not (doctype and name and frappe.db.exists(doctype, name)):
				return None  # a miss is never kept
			self.docs[(doctype, name)] = frappe.get_doc(doctype, name)
		return self.docs[(doctype, name)]

	def ive(self, ive):
		if not isinstance(ive, str):
			return ive
		if ive not in self.ives:
			self.ives[ive] = frappe.db.get_value(IVE, ive, list(IVE_FIELDS), as_dict=True)
		return self.ives[ive]

	def preload_ives(self, names):
		"""Every event named, and the originals they reverse, in a few reads."""
		names = [n for n in set(names) if n and n not in self.ives]
		while names:
			for e in frappe.get_all(IVE, filters={"name": ("in", names)}, fields=list(IVE_FIELDS)):
				self.ives[e.name] = e
			names = list({e.reversal_of for e in self.ives.values()
				if e.reversal_of and e.reversal_of not in self.ives} - set(names))


def _row(doc, detail):
	for df in doc.meta.get_table_fields():
		for row in doc.get(df.fieldname) or []:
			if row.name == detail:
				return row
	return None


# ------------------------------------------------------------- attribution
def company_attribution(company):
	"""The attribution of a posting that belongs to no voucher row."""
	return attr.row_attribution(frappe._dict(company=company))


def source_attribution(company, source, controller=None, resolver=None):
	"""The attribution of the voucher row `source` names:
	(doctype, name[, row])."""
	resolver = resolver or Resolver(controller)
	doctype, name, detail = (tuple(source or ()) + (None, None, None))[:3]
	doc = resolver.doc(doctype, name) if detail else None
	row = _row(doc, detail) if doc is not None else None
	if row is None:
		return company_attribution(company)
	if doctype == "Landed Cost Voucher" and row.get("receipt_document") and row.get("purchase_receipt_item"):
		receipt = resolver.doc(row.receipt_document_type, row.receipt_document)
		receipt_row = _row(receipt, row.purchase_receipt_item) if receipt is not None else None
		if receipt_row is not None:
			return frappe._dict(attr.row_attribution(receipt, receipt_row), voucher_detail_no=row.name)
	return attr.row_attribution(doc, row)


def expected_attribution(ive, controller=None, resolver=None):
	"""The attribution `ive`'s legs should carry, derived (never the
	snapshot): its source row's; for the reversal of another document's
	event, the original's on the reversing document's row that undoes it -
	a landed-cost or invoice-difference mirror names the ORIGINAL's row,
	which its own voucher does not hold."""
	resolver = resolver or Resolver(controller)
	ive = resolver.ive(ive)
	if ive.get("reversal_of"):
		original = resolver.ive(ive.reversal_of)
		if original and (original.source_doctype, original.source_docname) != (ive.source_doctype, ive.source_docname):
			value = frappe._dict(expected_attribution(original, resolver=resolver))
			if value.voucher_detail_no:
				reversing = resolver.doc(ive.source_doctype, ive.source_docname)
				own = ive.source_detail_name if reversing is not None and ive.source_detail_name \
					and _row(reversing, ive.source_detail_name) is not None else None
				value.voucher_detail_no = own or undoing_row(original, value.voucher_detail_no, reversing, resolver)
			return value
	return source_attribution(
		ive.company, (ive.source_doctype, ive.source_docname, ive.source_detail_name), resolver=resolver
	)


def event_attribution(ive, controller=None, resolver=None):
	"""What `ive`'s legs carry: its snapshot, else `expected_attribution` -
	stored on the event, so every later leg of the same event (a rounding
	residue, a repair) carries what the first did."""
	resolver = resolver or Resolver(controller)
	ive = resolver.ive(ive)
	if ive.get("gl_attribution"):
		return frappe._dict(json.loads(ive.gl_attribution))
	value = expected_attribution(ive, resolver=resolver)
	snapshot = json.dumps(value, sort_keys=True, default=str)
	frappe.db.set_value(IVE, ive.name, "gl_attribution", snapshot, update_modified=False)
	ive.gl_attribution = snapshot
	return value


# -------------------------------------------------------------- the writer
def post_event_gl(ive, legs, *, posting_date, remarks, controller=None, voucher=None, company=None):
	"""Post `legs` - (account, signed amount, against[, attribution]),
	positive Dr - for valuation event `ive`, merge off. A leg without its
	own attribution carries the event's. With `controller` the rows are
	built by its get_gl_dict (fiscal year, currencies) and the attribution
	stamped over the header defaults it fills in."""
	from erpnext.accounts.general_ledger import make_gl_entries

	resolver = Resolver(controller)
	ive = resolver.ive(ive)
	company = company or ive.company
	voucher_type, voucher_no = voucher or (ive.source_doctype, ive.source_docname)
	use_controller = controller is not None and hasattr(controller, "get_gl_dict") and (
		controller.doctype, controller.name) == (voucher_type, voucher_no)
	base = None
	gl_map = []
	for leg in legs:
		account, amount, against = leg[:3]
		attribution = leg[3] if len(leg) > 3 else None
		amount = r2(amount)
		if not amount or not account:
			continue
		if attribution is None:
			base = base if base is not None else event_attribution(ive, resolver=resolver)
			attribution = base
		args = {
			"account": account,
			"against": against or "",
			"debit": amount if amount > 0 else 0,
			"credit": -amount if amount < 0 else 0,
			# inventory and its offsets are company currency: the account-
			# currency figures are the same numbers (the General Ledger report
			# renders these; unset they show 0.00)
			"debit_in_account_currency": amount if amount > 0 else 0,
			"credit_in_account_currency": -amount if amount < 0 else 0,
			"posting_date": posting_date,
			"remarks": remarks,
			"valuation_event_id": ive.name,
		}
		if use_controller:
			gl = controller.get_gl_dict(args)
		else:
			gl = frappe._dict(args, voucher_type=voucher_type, voucher_no=voucher_no, company=company)
		gl_map.append(attr.stamp(gl, attribution))
	if gl_map:
		make_gl_entries(gl_map, merge_entries=False)
	return gl_map


# ----------------------------------------------------------------- mirrors
def mirror_legs(original_event, share=1.0, against="", reversing_doc=None, row=None):
	"""The legs that reverse what `original_event` posted, scaled by
	`share`: its live GL netted by account and attribution (a repaired
	event's unattributed row and its reclass reversal cancel out), sides
	swapped. Each leg keeps the original's attribution; a leg the original
	posted on a row is posted on `row` - the reversing row the kernel is
	posting - or, without one, on the reversing document's row that undoes
	the original's (`undoing_row`). `reversing_doc` is the document or its
	(doctype, name)."""
	resolver = Resolver()
	if isinstance(reversing_doc, (tuple, list)):
		reversing_doc = resolver.doc(reversing_doc[0], reversing_doc[1])
	fields = ["account", "debit", "credit", *attr.attribution_fields()]
	net = {}
	for g in frappe.get_all(GL_ENTRY, filters={"valuation_event_id": original_event, "is_cancelled": 0},
			fields=fields, order_by="creation, name"):
		attribution = attr.gl_attribution(g)
		key = (g.account, *(attribution[f] for f in attr.attribution_fields()))
		held = net.setdefault(key, [attribution, 0.0])
		held[1] += flt(g.credit) - flt(g.debit)
	original = resolver.ive(original_event)
	legs = []
	for (account, *_key), (attribution, amount) in net.items():
		amount = r2(amount * share)
		if not amount:
			continue
		attribution = frappe._dict(attribution)
		if attribution.voucher_detail_no:
			attribution.voucher_detail_no = row or undoing_row(
				original, attribution.voucher_detail_no, reversing_doc, resolver)
		legs.append((account, amount, against, attribution))
	return legs


def undoing_row(original, detail, reversing_doc, resolver=None):
	"""The reversing document's row that undoes `detail` on the original
	voucher, or None - by a link, never by position: the same voucher keeps
	its row; a native return's row names the row it returns (core's return
	links); a copied Landed Cost Voucher's row lands on the same receipt row
	as the original's."""
	if reversing_doc is None or not original or not detail:
		return None
	if (reversing_doc.doctype, reversing_doc.name) == (original.source_doctype, original.source_docname):
		return detail
	rows = [r for df in reversing_doc.meta.get_table_fields() for r in reversing_doc.get(df.fieldname) or []]
	if reversing_doc.doctype == "Landed Cost Voucher":
		# an LCV row's purchase_receipt_item names the RECEIPT row it lands on
		source = (resolver or Resolver()).doc(original.source_doctype, original.source_docname)
		landed = _row(source, detail) if source is not None else None
		if landed is None:
			return None
		target = (landed.get("receipt_document"), landed.get("purchase_receipt_item"))
		return next((r.name for r in rows
			if (r.get("receipt_document"), r.get("purchase_receipt_item")) == target), None)
	return next((r.name for r in rows if any(r.get(link) == detail for link in RETURN_ROW_LINKS)), None)


# ------------------------------------------------------------------ repair
def repair_gl_attribution(company, write=False, vouchers=None, force=False):
	"""Reclassify GL the kernels posted before they stamped the voucher
	row's attribution (client PA-028; Vivek, 19/10/2026). Census first:
	with write=False it only reports.

	For every valuation event, its live GL is netted by voucher, account and
	attribution; a group whose cost centre, project or dimensions differ
	from what the event should carry (`expected_attribution`) is
	reclassified ON THE ORIGINAL VOUCHER, tagged with the same event:
	reversed as it stands, re-posted with the expected attribution. Each
	pair nets to zero on its account, so inventory, the event's value and
	the period-close gates are untouched; it is inserted through
	make_gl_entries, so Project Accounting stamps the re-posted rows as
	source lines. Nothing is updated or deleted.

	Dated on the original date while the period authority still accepts
	postings there, else in the current open period. A row that differs
	only by its missing voucher row is left: it moves no balance.

	A group already on a Project Accounting source line (its row carried
	the project through the header) is listed with that line and what has
	been settled from it. Its reversal nets that line and the re-post opens
	a new one per row; on a settled line that leaves a negative amount to
	settle, so such an event - every leg of it - is skipped unless force=1.

	bench --site <site> execute periodic_valuation.shared.event_gl.repair_gl_attribution \\
		--kwargs '{"company": "Badia Cement"}'          # census
		--kwargs '{"company": "Badia Cement", "write": 1}'
	"""
	from periodic_valuation.shared.periods import current_period_day, get_period, period_refusal

	fields = attr.attribution_fields()
	compared = [f for f in fields if f != "voucher_detail_no"]
	line_key = frappe.get_meta(GL_ENTRY).has_field("pa_line_key")
	conditions = {"company": company, "is_cancelled": 0, "valuation_event_id": ("is", "set")}
	if vouchers:
		conditions["voucher_no"] = ("in", list(vouchers))
	rows = frappe.get_all(GL_ENTRY, filters=conditions,
		fields=["valuation_event_id", "voucher_type", "voucher_no", "account", "debit", "credit", "posting_date",
			*fields, *(["pa_line_key"] if line_key else [])], order_by="creation, name")
	groups = {}
	for g in rows:
		a = attr.gl_attribution(g)
		key = (g.valuation_event_id, g.voucher_type, g.voucher_no, g.account, *(a[f] for f in fields))
		held = groups.setdefault(key, frappe._dict(attribution=a, amount=0.0, posting_date=g.posting_date,
			line=g.get("pa_line_key")))
		held.amount += flt(g.debit) - flt(g.credit)

	resolver = Resolver()
	resolver.preload_ives(k[0] for k in groups)
	settled = _settled_by_line({g.line for g in groups.values() if g.line})
	today = current_period_day(company)
	periods = {}

	def repair_date(day):
		month = (day.year, day.month)
		if month not in periods:
			period = get_period(company, day)
			periods[month] = bool(period) and period_refusal(period) is None
		return day if periods[month] else today

	expected, plan = {}, []
	for (event, voucher_type, voucher_no, account, *_rest), group in groups.items():
		amount = r2(group.amount)
		if not amount:
			continue
		if event not in expected:
			expected[event] = expected_attribution(event, resolver=resolver)
		want = expected[event]
		if all((group.attribution[f] or None) == (want.get(f) or None) for f in compared):
			continue
		line = settled.get(group.line) if group.line else None
		plan.append(frappe._dict(event=event, voucher_type=voucher_type, voucher_no=voucher_no, account=account,
			amount=amount, was=group.attribution, now=want, posting_date=repair_date(group.posting_date),
			source_line=line.name if line else None, settled=line.settled if line else 0.0,
			skipped=bool(line and line.settled and not force)))

	# an event is reclassified whole or not at all: its legs never end on
	# different cost centres
	held_back = {p.event for p in plan if p.skipped}
	for p in plan:
		p.skipped = p.event in held_back

	if write:
		by_event = {}
		for p in plan:
			if not p.skipped:
				by_event.setdefault((p.event, p.voucher_type, p.voucher_no, p.posting_date), []).append(p)
		for (event, voucher_type, voucher_no, day), items in by_event.items():
			legs = []
			for p in items:
				legs.append((p.account, -p.amount, "", p.was))
				legs.append((p.account, p.amount, "", p.now))
			post_event_gl(event, legs, posting_date=day, voucher=(voucher_type, voucher_no), company=company,
				remarks=_("Reclassified to its voucher row's cost centre and dimensions (PA-028)"))
		for event, value in expected.items():
			frappe.db.set_value(IVE, event, "gl_attribution", json.dumps(value, sort_keys=True, default=str),
				update_modified=False)
		# a finance-run tool, census first: the whole plan is one unit of work
		# and is committed once, after every pair has posted
		frappe.db.commit()

	return {
		"groups": len(plan),
		"vouchers": len({p.voucher_no for p in plan}),
		"skipped_settled": len([p for p in plan if p.skipped]),
		"written": bool(write),
		"plan": [{
			"voucher": p.voucher_no, "event": p.event, "account": p.account, "amount": p.amount,
			"posting_date": str(p.posting_date),
			"from": {f: p.was[f] for f in compared if p.was[f]},
			"to": {f: p.now.get(f) for f in compared if p.now.get(f)},
			**({"source_line": p.source_line, "settled": p.settled} if p.source_line else {}),
			**({"skipped": "the event has a leg on a settled source line - pass force=1 to reclassify"}
				if p.skipped else {}),
		} for p in plan],
	}


def _settled_by_line(line_keys):
	"""Project Accounting source lines by line key, with what has been
	settled from each - read only when that app is installed; this app
	never depends on it."""
	if not line_keys or not frappe.db.exists("DocType", "Project Source Line"):
		return {}
	lines = frappe.get_all("Project Source Line", filters={"line_key": ("in", list(line_keys))},
		fields=["name", "line_key", "source_amount", "available_to_settle"])
	return {
		l.line_key: frappe._dict(name=l.name, settled=r2(max(flt(l.source_amount) - flt(l.available_to_settle), 0)))
		for l in lines
	}
