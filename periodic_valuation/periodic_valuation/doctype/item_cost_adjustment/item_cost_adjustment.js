// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Item Cost Adjustment", {
	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.doc.is_cancellation) {
			frm.add_custom_button(__("Get Pending Allocations"), () => {
				if (!frm.doc.company) {
					frappe.msgprint(__("Choose the company first."));
					return;
				}
				frappe
					.xcall("periodic_valuation.periodic_valuation.doctype.item_cost_adjustment.item_cost_adjustment.get_pending", {
						company: frm.doc.company,
					})
					.then((r) => {
						if (!r.available) {
							frappe.msgprint(__("No installed app allocates cost to items on this site, so nothing is pending."));
							return;
						}
						if (!r.lines.length) {
							frappe.msgprint(__("Nothing is pending for {0}.", [frm.doc.company]));
							return;
						}
						const taken = new Set((frm.doc.items || []).map((d) => d.handover_key));
						r.lines.filter((l) => !taken.has(l.handover_key)).forEach((l) => frm.add_child("items", l));
						frm.refresh_field("items");
					});
			});
		}
	},
});
