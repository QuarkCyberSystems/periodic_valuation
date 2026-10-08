// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Item Standard Cost Version", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.status === "DRAFT") {
			frm.add_custom_button(__("Release"), () =>
				frm.call({ doc: frm.doc, method: "release" }).then(() => frm.reload_doc())
			).addClass("btn-primary");
			return;
		}
		show_revaluation(frm);
	},
});

// Client ticket STD-002 (28/09): the revaluation a release posts was not
// reachable from the version. It is booked as GL rows under this document
// (no separate Journal Entry), like Stock Revaluation and Stock Count, so
// the same View shortcuts lead to it — or the form says why nothing posted.
function show_revaluation(frm) {
	const rev = (frm.doc.__onload && frm.doc.__onload.revaluation) || {};
	show_replaced_by(frm);
	if (rev.events && !rev.posted) {
		// STD-010: a change with nothing to revalue still logs a zero event
		frm.add_custom_button(__("Revaluation Events"), () => route_to_events(frm), __("View"));
	}
	if (rev.posted) {
		frm.add_custom_button(
			__("Accounting Ledger"),
			() => {
				frappe.route_options = {
					voucher_no: frm.doc.name,
					from_date: rev.from_date,
					to_date: rev.to_date,
					company: frm.doc.company,
					categorize_by: "Categorize by Voucher (Consolidated)",
					ignore_prepared_report: true,
				};
				frappe.set_route("query-report", "General Ledger Enterprise");
			},
			__("View")
		);
		frm.add_custom_button(__("Revaluation Events"), () => route_to_events(frm), __("View"));
		show_release_switch(frm);
		return;
	}
	const period = `${String(frm.doc.valid_from_month).padStart(2, "0")}-${frm.doc.valid_from_year}`;
	const fmt = (d) => frappe.datetime.str_to_user(d);
	const why = {
		pending: __("Revaluation pending: it posts when {0} begins.", [period]),
		// versions released under the "Last day of the period" rule before the
		// DR-50 amendment (05/10/2026) and not re-stamped
		switch_pending: __(
			"Switches at period end: the current standard cost stays in force until {0}. The stock on hand on {0} is revalued on that day, and this cost applies from {1}.",
			[fmt(rev.revaluation_date), fmt(rev.effective_from)]
		),
		first_version: rev.events
			? __("The item's first standard cost: recorded as a zero-value revaluation event, with no ledger entry.")
			: __("No revaluation entry: this is the item's first standard cost, so there was no earlier cost to revalue from."),
		nothing_to_revalue: rev.events
			? __("Revaluation recorded with zero value: no stock was on hand or moved, so nothing was posted to the ledger.")
			: __("No revaluation entry: the cost did not change, or no stock was on hand or moved in the period."),
	}[rev.reason];
	const pending = ["pending", "switch_pending"].includes(rev.reason);
	if (why) frm.dashboard.set_headline(why, pending ? "orange" : "blue");
	else show_release_switch(frm);
}

// DR-57: a backdated change corrects its own month only - say so, and that
// the month after keeps its own standard cost
function show_correction(frm) {
	if (!frm.doc.is_correction) return false;
	const to = frappe.datetime.str_to_user(frm.doc.effective_to);
	const next = frappe.datetime.str_to_user(frappe.datetime.add_days(frm.doc.effective_to, 1));
	frm.dashboard.set_headline(
		__("Corrects its month only (to {0}): the revaluation reverses on {1}, and the month from {1} keeps its own standard cost.",
			[to, next]),
		"blue"
	);
	return true;
}

// DR-50 as amended: say where the switch fell, so the client sees why a
// movement dated earlier in the month still carries the earlier cost
function show_release_switch(frm) {
	if (show_correction(frm)) return;
	if (!frm.doc.switch_on_release) return;
	frm.dashboard.set_headline(
		__("Switched at release on {0}: movements dated before it keep the earlier cost, and the stock on hand was revalued that day.",
			[frappe.datetime.str_to_user(frm.doc.effective_from)]),
		"blue"
	);
}

function show_replaced_by(frm) {
	const next = frm.doc.__onload && frm.doc.__onload.replaced_by;
	if (!next) return;
	const until = frappe.datetime.str_to_user(next.in_force_until);
	const link = frappe.utils.get_form_link(frm.doc.doctype, next.name, true);
	const text = next.switch_at_period_end && !next.revaluation_posted
		? __("In force until {0}: replaced by {1} at period end.", [until, link])
		: __("Replaced by {0} from {1}.", [link, frappe.datetime.str_to_user(next.effective_from)]);
	frm.dashboard.add_comment(text, "blue", true);
}

function route_to_events(frm) {
	frappe.set_route("List", "Inventory Valuation Event", {
		source_doctype: frm.doc.doctype,
		source_docname: frm.doc.name,
	});
}
