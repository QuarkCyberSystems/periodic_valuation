// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Cross Fiscal Year Late Entries"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_default("company"),
			reqd: 1,
		},
		{
			fieldname: "fiscal_year",
			label: __("Original Fiscal Year"),
			fieldtype: "Int",
			description: __("The prior year the entries were dated into; leave empty for all"),
		},
		{ fieldname: "item_code", label: __("Item"), fieldtype: "Link", options: "Item" },
	],
};
