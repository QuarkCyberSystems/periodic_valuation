// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Inventory Book vs Reference"] = {
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
			fieldname: "period_year",
			label: __("Period Year"),
			fieldtype: "Int",
			default: new Date().getFullYear(),
			reqd: 1,
		},
		{
			fieldname: "period_month",
			label: __("Period Month"),
			fieldtype: "Int",
			default: new Date().getMonth() + 1,
			reqd: 1,
		},
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: () => ({ filters: { company: frappe.query_report.get_filter_value("company") } }),
		},
		{
			fieldname: "valuation_method",
			label: __("Valuation Method"),
			fieldtype: "Select",
			options: "\nPeriodic Standard Cost\nPeriodic Moving Average",
		},
		{
			fieldname: "show_zero",
			label: __("Show Zero Balances"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "check" && data && Math.abs(data.check) >= 0.01) {
			value = `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
