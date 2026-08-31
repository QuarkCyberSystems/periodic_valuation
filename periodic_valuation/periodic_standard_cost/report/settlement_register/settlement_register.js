// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Settlement Register"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_default("company"),
			reqd: 1,
		},
		{ fieldname: "period_year", label: __("Period Year"), fieldtype: "Int" },
		{ fieldname: "period_month", label: __("Period Month"), fieldtype: "Int" },
		{ fieldname: "item_code", label: __("Item"), fieldtype: "Link", options: "Item" },
		{
			fieldname: "settlement_view",
			label: __("View"),
			fieldtype: "Select",
			options: "\nMTD\nYTD",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nLive\nReversed",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data) {
			const color = data.status === __("Reversed") ? "var(--orange-600)" : "var(--green-600)";
			value = `<span style="color:${color};font-weight:600">${value}</span>`;
		}
		return value;
	},
};
