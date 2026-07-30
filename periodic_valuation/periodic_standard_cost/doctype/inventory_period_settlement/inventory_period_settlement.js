// Copyright (c) 2026, Quark Cyber Systems

frappe.ui.form.on("Inventory Period Settlement", {
	refresh(frm) {
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
