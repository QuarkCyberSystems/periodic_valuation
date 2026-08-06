// Copyright (c) 2026, Quark Cyber Systems
// Show what movement a routed document actually posted, SAP-style.
//
// ERPNext has no movement-type field on Purchase Receipt / Delivery Note: the
// direction is spread across is_return, is_cancellation and the sign of the
// quantity. So a reversal of a purchase return shows "-500" with nothing on the
// document saying what it is, while the same posting in SAP reads as movement
// type 123, "reversal of return delivery", with a positive quantity. That is
// what the client was asking for in WA-0003-01 item 9.
//
// The label is DERIVED from the Stock Movement Event on every form load, never
// stored on the document. The event log already records the movement
// authoritatively; a copy on the document would be one more shadow to keep in
// step, and it would leave every existing document blank until a migration ran.

(() => {
	const DOCTYPES = [
		"Purchase Receipt", "Delivery Note", "Stock Entry", "Purchase Invoice",
		"Sales Invoice", "Subcontracting Receipt", "Landed Cost Voucher",
	];

	function showMovement(frm) {
		if (frm.doc.docstatus !== 1) return;
		frappe.call({
			method: "periodic_valuation.periodic_moving_average.api.get_movement_summary",
			args: { doctype: frm.doc.doctype, docname: frm.doc.name },
			callback(r) {
				const label = r.message && r.message.label;
				if (!label) return;           // not a routed document — say nothing
				// Two earlier attempts failed here, both silently:
				//   1. frm.dashboard.add_indicator() draws nothing on a submitted
				//      stock document — the indicator area is not instantiated.
				//   2. set_headline collides with lockReversal, which owns that
				//      slot on a reversal and re-applies it on delayed passes,
				//      wiping anything appended to it.
				// set_intro is a separate region, so the two never fight: the
				// reversal keeps its "fields are locked" banner AND the movement
				// line is shown.
				frm.set_intro(__("Movement: {0}", [label]), "blue");
			},
		});
	}

	for (const doctype of DOCTYPES) {
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				// one lookup per form load; the dashboard is rebuilt on refresh
				// so the indicator does not accumulate
				showMovement(frm);
			},
		});
	}
})();
