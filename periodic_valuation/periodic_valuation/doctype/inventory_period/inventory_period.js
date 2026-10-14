// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Inventory Period", {
	refresh(frm) {
		// the controlled period shift (DR-68): the only way a later period opens
		if (frm.is_new() || frm.doc.status !== "OPEN") return;
		frm.add_custom_button(__("Open Next Period"), () => {
			frappe.confirm(
				__("Open the next month? {0} becomes previous-open and every item's balance is carried into the new month. The month before {0} must already be closed.", [frm.doc.period_name]),
				() => frappe.call({
					method: "open_next", doc: frm.doc, freeze: true,
					freeze_message: __("Opening the next period..."),
					callback(r) {
						if (r.message) frappe.set_route("Form", "Inventory Period", r.message);
					},
				})
			);
		});
	},
});
