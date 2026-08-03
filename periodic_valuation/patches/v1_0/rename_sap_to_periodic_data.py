# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Data half of the SAP -> Periodic rename (post_model_sync: needs the synced
schema, in particular SLE.posted_via_valuation_kernel from the erpnext fork).
Idempotent; a no-op on sites that never carried the old names."""

import frappe

METHODS = {
	"SAP Moving Average": "Periodic Moving Average",
	"SAP Standard Cost": "Periodic Standard Cost",
}


def execute():
	for old, new in METHODS.items():
		frappe.db.sql("UPDATE tabItem SET valuation_method=%s WHERE valuation_method=%s", (new, old))
		frappe.db.sql(
			"""UPDATE tabSingles SET value=%s WHERE doctype='Stock Settings'
			AND field='valuation_method' AND value=%s""",
			(new, old),
		)

	# Carry the kernel flag over from the old SLE column, then drop it.
	if frappe.db.has_column("Stock Ledger Entry", "posted_via_sap_kernel"):
		if frappe.db.has_column("Stock Ledger Entry", "posted_via_valuation_kernel"):
			frappe.db.sql(
				"""UPDATE `tabStock Ledger Entry`
				SET posted_via_valuation_kernel=1 WHERE posted_via_sap_kernel=1"""
			)
		frappe.db.sql_ddl("ALTER TABLE `tabStock Ledger Entry` DROP COLUMN posted_via_sap_kernel")

	# Settings documents keep their autoname-derived names unless renamed.
	for dt, old_prefix, new_prefix in (
		("Periodic Moving Average Settings", "SAP-MA-SET-", "PMA-SET-"),
		("Periodic Standard Cost Settings", "SAP-STD-SET-", "STD-SET-"),
	):
		if not frappe.db.table_exists(dt):
			continue
		for name in frappe.get_all(dt, filters={"name": ("like", old_prefix + "%")}, pluck="name"):
			frappe.rename_doc(dt, name, new_prefix + name[len(old_prefix):], force=True, show_alert=False)

	# Superseded section-break carrier on Stock Entry Type.
	frappe.db.delete("Custom Field", {"fieldname": "sap_valuation_section"})
