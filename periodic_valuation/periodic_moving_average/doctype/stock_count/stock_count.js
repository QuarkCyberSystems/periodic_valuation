// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Stock Count", {
	setup(frm) {
		// Company-scope the child links. Without these the pickers offer every
		// company's accounts and warehouses -- the client was shown
		// "Stock Adjustment - WP" on a Badia Cement count (meeting 2026-08-12).
		frm.set_query("variance_account", "items", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
				root_type: ["in", ["Expense", "Income"]],
			},
		}));
		frm.set_query("warehouse", "items", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
	},

	company(frm) {
		// company changed -> previously chosen rows may now be out of scope
		(frm.doc.items || []).forEach((row) => {
			if (row.variance_account || row.warehouse) {
				frappe.model.set_value(row.doctype, row.name, {
					variance_account: null,
					warehouse: null,
				});
			}
		});
	},

	// A count dated in a prior period must show THAT period's on-hand. Changing
	// the posting date therefore has to re-fetch every row, otherwise the form
	// keeps showing the balance for whichever date was set when the item was
	// picked (WA-0003-01 item 8).
	posting_date(frm) {
		(frm.doc.items || []).forEach((row) => {
			fetch_current_state(frm, row.doctype, row.name);
		});
	},
});

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
	if (!row || !row.item_code || !frm.doc.company) return;
	frappe.call({
		method: "periodic_valuation.periodic_moving_average.api.get_current_state",
		args: {
			company: frm.doc.company,
			item_code: row.item_code,
			warehouse: row.warehouse,
			// resolve the balance as of the period being posted into, which is
			// what the server uses to value the difference
			posting_date: frm.doc.posting_date,
		},
		callback(r) {
			if (!r.message) return;
			frappe.model.set_value(cdt, cdn, {
				current_qty: r.message.closing_qty,
				valuation_rate: r.message.is_negative ? r.message.frozen_map : r.message.moving_avg_price,
			});
			// keep the derived columns in step with the refreshed system qty
			const cur = locals[cdt][cdn];
			if (cur && flt(cur.counted_qty)) {
				frappe.model.set_value(cdt, cdn, {
					quantity_difference: flt(cur.counted_qty) - flt(r.message.closing_qty),
					difference_amount:
						(flt(cur.counted_qty) - flt(r.message.closing_qty)) *
						flt(r.message.is_negative ? r.message.frozen_map : r.message.moving_avg_price),
				});
			}
		},
	});
}
