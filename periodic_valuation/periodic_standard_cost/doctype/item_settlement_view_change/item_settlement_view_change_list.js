// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.listview_settings["Item Settlement View Change"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = { Draft: "grey", Approved: "blue", Posted: "green" };
		return [__(doc.status), colors[doc.status] || "grey", "status,=," + doc.status];
	},
};
