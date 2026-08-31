# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Connections shown on core forms for the valuation records they produce.

Wired through hooks.override_doctype_dashboards: each function receives the
doctype's existing dashboard data and adds a "Valuation" group. The event
tables link to their source through source_docname, not through a field
named after the source doctype, hence the non-standard fieldnames."""

from frappe import _

ITEM_RECORDS = [
	"Item Standard Cost Version",
	"Standard Cost Estimate",
	"Item Settlement View Change",
	"Inventory Period Settlement",
	"Inventory Period Balance",
	"Inventory Valuation Event",
]

SOURCE_RECORDS = ["Inventory Valuation Event", "Stock Movement Event"]


def item(data):
	data.setdefault("transactions", []).append({"label": _("Valuation"), "items": ITEM_RECORDS})
	return data


def source_document(data):
	data.setdefault("non_standard_fieldnames", {}).update(
		{dt: "source_docname" for dt in SOURCE_RECORDS}
	)
	data.setdefault("transactions", []).append({"label": _("Valuation"), "items": SOURCE_RECORDS})
	return data
