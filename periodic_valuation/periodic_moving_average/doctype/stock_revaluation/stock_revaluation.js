// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Stock Revaluation", {
	refresh(frm) {
		// After submit, offer the same View > Stock Ledger / Accounting Ledger
		// shortcuts every core stock document has. The revaluation posts GL
		// and SLE rows with no way to reach them from the document, so the
		// entries read as absent — the same "doesn't create a JV" blind spot
		// the client hit on Landed Cost and Stock Count (behaviour review
		// 2026-08-18; Stock Count got these buttons after the 2026-08-12
		// meeting).
		if (frm.doc.docstatus === 0) return;
		const to_date = moment(frm.doc.modified).format("YYYY-MM-DD");
		const route_args = (extra) => ({
			voucher_no: frm.doc.name,
			from_date: frm.doc.posting_date,
			to_date: to_date,
			company: frm.doc.company,
			show_cancelled_entries: frm.doc.docstatus === 2,
			ignore_prepared_report: true,
			...extra,
		});
		frm.add_custom_button(
			__("Stock Ledger"),
			() => {
				frappe.route_options = route_args({});
				frappe.set_route("query-report", "Stock Ledger");
			},
			__("View")
		);
		if (erpnext.is_perpetual_inventory_enabled(frm.doc.company)) {
			frm.add_custom_button(
				__("Accounting Ledger"),
				() => {
					frappe.route_options = route_args({
						categorize_by: "Categorize by Voucher (Consolidated)",
					});
					frappe.set_route("query-report", "General Ledger");
				},
				__("View")
			);
		}
		// the valuation events this revaluation produced — the kernel's own audit trail
		frm.add_custom_button(
			__("Valuation Events"),
			() => {
				frappe.set_route("List", "Inventory Valuation Event", {
					source_doctype: "Stock Revaluation",
					source_docname: frm.doc.name,
				});
			},
			__("View")
		);
	},
});

frappe.ui.form.on("Stock Revaluation Item", {
	item_code(frm, cdt, cdn) { fetch_current_state(frm, cdt, cdn); },
	warehouse(frm, cdt, cdn) { fetch_current_state(frm, cdt, cdn); },
	new_valuation_rate(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		row.difference_amount = flt(row.current_qty) * (flt(row.new_valuation_rate) - flt(row.current_valuation_rate));
		frm.refresh_field("items");
	},
});

function fetch_current_state(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row.item_code || !frm.doc.company) return;
	frappe.call({
		method: "periodic_valuation.periodic_moving_average.api.get_current_state",
		args: { company: frm.doc.company, item_code: row.item_code, warehouse: row.warehouse },
		callback(r) {
			if (!r.message) return;
			frappe.model.set_value(cdt, cdn, {
				current_qty: r.message.closing_qty,
				current_valuation_rate: r.message.moving_avg_price,
				current_stock_value: r.message.closing_value,
			});
		},
	});
}
