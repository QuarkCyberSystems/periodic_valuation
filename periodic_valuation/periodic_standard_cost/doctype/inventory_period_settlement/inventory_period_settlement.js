// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Inventory Period Settlement", {
	refresh(frm) {
		// the settlement's journal lines: its own rows, their carry into the
		// next month and, once reversed, the reversal (client ticket STD-005)
		const events = [frm.doc.sett_event, frm.doc.sett_rev_event]
			.concat((frm.doc.reversed_by_events || "").split(","))
			.filter(Boolean);
		if (events.length) {
			frm.add_custom_button(__("Ledger Entries"), () => {
				frappe.set_route("List", "GL Entry", {
					valuation_event_id: ["in", events],
					is_cancelled: 0,
				});
			}, __("View"));
		}
		if (frm.doc.settlement_run) {
			frm.add_custom_button(__("Settlement Run"), () => {
				frappe.set_route("Form", "Inventory Period Settlement Run", frm.doc.settlement_run);
			}, __("View"));
		}
		if (frm.doc.cancelled) return;
		frm.add_custom_button(__("Reverse Settlement"), () => {
			frappe.confirm(
				__("Reverse this settlement? The period reopens for corrections and must be re-settled. Allowed for the previous month only."),
				() => {
					frappe.call({
						method: "reverse", doc: frm.doc, freeze: true,
						callback() { frm.reload_doc(); },
					});
				}
			);
		});
	},
});
