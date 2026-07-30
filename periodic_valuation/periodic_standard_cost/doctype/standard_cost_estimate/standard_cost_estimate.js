// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Standard Cost Estimate", {
	refresh(frm) {
		if (frm.is_new()) return;
		const call = (method) =>
			frm.call({ doc: frm.doc, method }).then(() => frm.reload_doc());

		if (["DRAFT", "CALCULATED"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Calculate"), () => call("calculate"));
		}
		if (frm.doc.status === "CALCULATED") {
			frm.add_custom_button(__("Mark"), () => call("mark")).addClass("btn-primary");
		}
		if (frm.doc.status === "MARKED") {
			frm.add_custom_button(__("Release"), () => call("release")).addClass("btn-primary");
		}
	},
});
