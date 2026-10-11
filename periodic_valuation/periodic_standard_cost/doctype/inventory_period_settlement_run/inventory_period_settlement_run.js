// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Inventory Period Settlement Run", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;
		// the settlement journal posts under the run: the month's last day and
		// the carry into the next month's first day (client ticket STD-005)
		frm.add_custom_button(__("Accounting Ledger"), () => {
			const from = `${frm.doc.period_year}-${String(frm.doc.period_month).padStart(2, "0")}-01`;
			frappe.route_options = {
				voucher_no: frm.doc.name,
				from_date: from,
				// the last day of the month after: the day before the 1st two months on
				to_date: frappe.datetime.add_days(frappe.datetime.add_months(from, 2), -1),
				company: frm.doc.company,
				group_by: "Group by Voucher (Consolidated)",
			};
			frappe.set_route("query-report", "General Ledger");
		}, __("View"));
		frm.add_custom_button(__("Settlements"), () => {
			frappe.set_route("List", "Inventory Period Settlement", { settlement_run: frm.doc.name });
		}, __("View"));
	},
});
