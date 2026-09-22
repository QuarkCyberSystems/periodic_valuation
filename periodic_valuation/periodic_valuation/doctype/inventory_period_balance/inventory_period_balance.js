// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Inventory Period Balance", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.settlement || !frm.doc.item_code) return;
		frappe.db.get_value("Item", frm.doc.item_code, "valuation_method").then((r) => {
			if ((r.message || {}).valuation_method !== "Periodic Standard Cost") return;
			frappe.db.get_value("Inventory Period",
				{ company: frm.doc.company, period_year: frm.doc.period_year, period_month: frm.doc.period_month },
				"status").then((p) => {
				const status = (p.message || {}).status;
				if (status !== "OPEN" && status !== "PREV_OPEN_UNSETTLED") return;
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
					__("Not settled for {0}-{1}. Settle This Item runs the settlement for this scope only.",
						[frm.doc.period_year, String(frm.doc.period_month).padStart(2, "0")]), "orange");
			});
		});
	},
});
