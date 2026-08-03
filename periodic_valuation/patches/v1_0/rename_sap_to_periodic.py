# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""In-place rename of the SAP-branded schema to Periodic Valuation naming
(pre_model_sync: must run before doctype sync so the renamed doctypes are
updated instead of duplicated).

NOTE: sites installed under the old app name need the app-identity rows fixed
via SQL BEFORE `bench migrate` can even start (hooks of every installed app are
imported at startup, and `sap_valuation` is no longer importable):

    UPDATE tabDefaultValue SET defvalue=REPLACE(defvalue,'"sap_valuation"','"periodic_valuation"')
        WHERE defkey='installed_apps';
    UPDATE `tabInstalled Application` SET app_name='periodic_valuation' WHERE app_name='sap_valuation';
    UPDATE `tabModule Def` SET app_name='periodic_valuation' WHERE app_name='sap_valuation';
"""

import frappe

MODULES = {
	"SAP Valuation": "Periodic Valuation",
	"SAP Moving Average": "Periodic Moving Average",
	"SAP Standard Cost": "Periodic Standard Cost",
}

DOCTYPES = {
	"SAP Moving Average Settings": "Periodic Moving Average Settings",
	"SAP Standard Cost Settings": "Periodic Standard Cost Settings",
	"SAP MA Return Valuation Override": "PMA Return Valuation Override",
}


def execute():
	for old, new in MODULES.items():
		if frappe.db.exists("Module Def", old) and not frappe.db.exists("Module Def", new):
			frappe.rename_doc("Module Def", old, new, force=True, show_alert=False)
	for old, new in DOCTYPES.items():
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True, show_alert=False)
