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
		frm.add_custom_button(
			__("Revaluation Events"),
			() =>
				frappe.set_route("List", "Inventory Valuation Event", {
					source_doctype: frm.doc.doctype,
					source_docname: frm.doc.name,
				}),
			__("View")
		);
		return;
	}
	const period = `${String(frm.doc.valid_from_month).padStart(2, "0")}-${frm.doc.valid_from_year}`;
	const why = {
		pending: __("Revaluation pending: it posts when {0} begins.", [period]),
		first_version: __("No revaluation entry: this is the item's first standard cost, so there was no earlier cost to revalue from."),
		nothing_to_revalue: __("No revaluation entry: the cost did not change, or no stock was on hand or moved in the period."),
	}[rev.reason];
	if (why) frm.dashboard.set_headline(why, rev.reason === "pending" ? "orange" : "blue");
}
