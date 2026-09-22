// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Inventory Period Balance", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.item_code) return;
		// the server decides (DR-47 / D-035): is this a standard-cost scope, is
		// the month postable, does a LIVE settlement lock it - never re-derived
		// here from stamps (the Settlement link stays as history after a reverse)
		frm.call({ method: "settlement_state", doc: frm.doc }).then((r) => {
			const st = r.message || {};
			if (!st.is_std || !st.postable || st.settled) return;
			frm.add_custom_button(__("Settle This Item"), () => {
				frappe.new_doc("Inventory Period Settlement Run", {
					company: frm.doc.company,
					period_year: frm.doc.period_year,
					period_month: frm.doc.period_month,
					run_type: "INITIAL_CLOSE",
					item_code: frm.doc.item_code,
					warehouse: frm.doc.warehouse || undefined,
				});
			});
			frm.dashboard.set_headline(
				__("No live settlement for {0}-{1}. Settle This Item runs the settlement for this scope only.",
					[frm.doc.period_year, String(frm.doc.period_month).padStart(2, "0")]), "orange");
		});
	},
});
