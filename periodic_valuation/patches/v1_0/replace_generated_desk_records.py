"""Before the app's own sidebar and desktop icon are imported, remove the
ones the desk generated for the workspace while the app shipped none
(standard 0, no app). The import replaces a record only while the file is
newer than the database copy, so a generated record touched after the
file's timestamp would otherwise be kept for good."""

import frappe

TITLE = "Periodic Valuation"


def execute():
	for doctype in ("Workspace Sidebar", "Desktop Icon"):
		generated = frappe.db.get_value(doctype, TITLE, ["standard", "app"], as_dict=True)
		if generated and not generated.standard and not generated.app:
			frappe.delete_doc(doctype, TITLE, ignore_permissions=True, force=True)
