// Copyright (c) 2026, Quark Cyber Systems
// WA-0003-01 item 15 — bill the remaining QUANTITY of a receipt.
//
// ERPNext gates Create > Purchase Invoice on per_billed, which it derives
// from billed AMOUNT, not quantity. Invoicing part of the quantity at a
// higher rate can therefore cover the receipt's whole value, push per_billed
// to 100, and remove the action from the form while units remain uninvoiced.
// The server mapper still returns those units happily — only the button is
// missing — so the receipt simply cannot be finished from the UI.
//
// Quantity is the honest measure of "is there anything left to bill", so the
// button is restored whenever uninvoiced units remain. Over-billing is not a
// concern here: the fork already exempts kernel-routed items from the
// over-billing throw (purchase_receipt.py), which is why the second invoice
// posts correctly once it can be raised at all.

frappe.ui.form.on("Purchase Receipt", {
	refresh(frm) {
		frm._pv_notice = null;   // recompute per load; never show a stale line
		if (frm.doc.docstatus !== 1) return;
		if (frm.doc.is_return || frm.doc.is_cancellation) return;
		// core already offers it — nothing to restore
		if (flt(frm.doc.per_billed) < 100) return;

		frappe.call({
			method: "periodic_valuation.periodic_moving_average.api.get_uninvoiced_qty",
			args: { purchase_receipt: frm.doc.name },
			callback(r) {
				const left = r.message && flt(r.message.remaining);
				if (!left) return;
				frm.add_custom_button(
					__("Purchase Invoice"),
					() => {
						frappe.model.open_mapped_doc({
							method: "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice",
							frm: frm,
						});
					},
					__("Create")
				);
				// Say why the action is back: the receipt reads "Completed"
				// and 100% billed, so an unexplained button looks like a bug.
				// Routed through the shared writer — .form-message has one
				// owner, and set_intro/add_comment here render nothing.
				frm._pv_notice = __(
					"{0} unit(s) on this receipt are still uninvoiced even though its value is fully billed — use Create > Purchase Invoice to bill the remainder.",
					[format_number(left)]
				);
				if (window.pv_render_message) window.pv_render_message(frm);
			},
		});
	},
});
