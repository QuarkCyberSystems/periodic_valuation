// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Stock Count Item", {
	item_code(frm, cdt, cdn) { fetch_current_state(frm, cdt, cdn); },
	warehouse(frm, cdt, cdn) { fetch_current_state(frm, cdt, cdn); },
	counted_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		row.quantity_difference = flt(row.counted_qty) - flt(row.current_qty);
		row.difference_amount = flt(row.quantity_difference) * flt(row.valuation_rate);
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
				valuation_rate: r.message.is_negative ? r.message.frozen_map : r.message.moving_avg_price,
			});
		},
	});
}
