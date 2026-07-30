// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Item Standard Cost Version", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "DRAFT") return;
		frm.add_custom_button(__("Release"), () =>
			frm.call({ doc: frm.doc, method: "release" }).then(() => frm.reload_doc())
		).addClass("btn-primary");
	},
});
