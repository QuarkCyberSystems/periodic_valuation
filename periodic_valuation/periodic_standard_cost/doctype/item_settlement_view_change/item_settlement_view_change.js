// Copyright (c) 2026, Quark Cyber Systems
// License: GNU General Public License v3. See license.txt

// Approval is a separate action from Submit: a different user approves the
// request (segregation of duties), then anyone with submit rights posts it.
frappe.ui.form.on("Item Settlement View Change", {
	refresh(frm) {
		const colors = { Draft: "grey", Approved: "blue", Posted: "green" };
		if (frm.doc.status) {
			frm.page.set_indicator(__(frm.doc.status), colors[frm.doc.status] || "grey");
		}
		if (frm.is_new() || frm.doc.docstatus !== 0) {
			if (frm.doc.status === "Posted") {
				frm.dashboard.set_headline(
					__("Posted on {0}: {1} settles {2} from the next period.", [
						frappe.datetime.str_to_user(frm.doc.posted_on) || "",
						frm.doc.item_code,
						frm.doc.to_view,
					]),
					"green"
				);
			}
			return;
		}
		if (frm.doc.status === "Draft") {
			if (frappe.session.user === frm.doc.requested_by) {
				frm.dashboard.set_headline(
					__("Awaiting approval by another user - the requester cannot approve their own request."),
					"orange"
				);
			} else {
				frm.add_custom_button(__("Approve"), () => {
					frm.call({ method: "approve", doc: frm.doc, freeze: true }).then(() => frm.reload_doc());
				});
				frm.change_custom_button_type(__("Approve"), null, "primary");
				frm.dashboard.set_headline(
					__("Requested by {0}. Approve to allow posting.", [frm.doc.requested_by]),
					"blue"
				);
			}
		} else if (frm.doc.status === "Approved") {
			frm.dashboard.set_headline(
				__("Approved by {0}. Submit to post: open activity is settled under {1} first, then the item settles {2}.", [
					frm.doc.approved_by,
					frm.doc.from_view,
					frm.doc.to_view,
				]),
				"blue"
			);
		}
	},
});
