// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Bulk Item Settlement View Change", {
	refresh(frm) {
		const colors = { Draft: "grey", Previewed: "blue", Created: "green" };
		frm.page.set_indicator(__(frm.doc.status || "Draft"), colors[frm.doc.status] || "grey");
		if (frm.is_new()) {
			frm.dashboard.set_headline(__("Save, then Preview to see which items in the group would change."), "blue");
			return;
		}
		if (frm.doc.status !== "Created") {
			frm.add_custom_button(__("Preview"), () => {
				frm.call({ method: "preview", doc: frm.doc, freeze: true }).then((r) => {
					frm.reload_doc();
					const m = r.message || {};
					frappe.show_alert({ message: __("{0} of {1} items eligible", [m.eligible, m.total]), indicator: "blue" });
				});
			});
		}
		if (frm.doc.status === "Previewed") {
			const eligible = (frm.doc.items || []).filter((d) => d.eligible).length;
			frm.add_custom_button(__("Create {0} Draft Requests", [eligible]), () => {
				frappe.confirm(
					__("Create one Draft Item Settlement View Change for each of the {0} eligible items? Each request still needs approval by a different user before it can be posted.", [eligible]),
					() => frm.call({ method: "create_drafts", doc: frm.doc, freeze: true }).then(() => frm.reload_doc())
				);
			});
			frm.change_custom_button_type(__("Create {0} Draft Requests", [eligible]), null, "primary");
			frm.dashboard.set_headline(__("{0} eligible item(s). Items already on the target view, disabled, or with an open request are skipped.", [eligible]), "blue");
		}
		if (frm.doc.status === "Created") {
			frm.dashboard.set_headline(__("{0} draft request(s) created - open Item Settlement View Change to approve and post them.", [frm.doc.created_count]), "green");
		}
	},
});
