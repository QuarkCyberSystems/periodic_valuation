// Copyright (c) 2026, Quark Cyber Systems

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
