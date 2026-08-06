// Copyright (c) 2026, Quark Cyber Systems
// Create Cancellation button for documents containing periodic-valuation items.
// Direct cancel is blocked server-side; this is the sanctioned path.

(() => {
	const DOCTYPES = [
		"Purchase Receipt", "Delivery Note", "Stock Entry", "Purchase Invoice",
		"Sales Invoice", "Subcontracting Receipt", "Landed Cost Voucher",
	];

	// A reversal (Cancellation) document is display-only: all fields are locked
	// except the posting date, and a clear "Reversal of …" banner is shown
	// (WA-0003-01 items 5 & 13). Applies in draft and after submit.
	function lockReversal(frm) {
		if (!frm.doc.is_cancellation) return;
		const keep = new Set(["posting_date", "set_posting_time", "posting_time"]);
		frm.fields.forEach((f) => {
			if (!keep.has(f.df.fieldname)) frm.set_df_property(f.df.fieldname, "read_only", 1);
		});
		// Lock the child table by walking the grid's DOCFIELDS. Iterating the
		// row object's keys instead walks framework metadata too (name, owner,
		// creation, docstatus, idx ...); update_docfield_property throws
		// "field <x> not found" on the first of those, which aborted this
		// function before the grid was locked AND before the banner below ever
		// rendered (WA-0003-01 item 5b).
		const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
		if (grid) {
			const fields = (grid.docfields || []).map((df) => df.fieldname);
			fields.forEach((fieldname) => {
				try {
					grid.update_docfield_property(fieldname, "read_only", 1);
				} catch (e) {
					// never let one unknown field stop the rest of the lock
				}
			});
			if (grid.refresh) grid.refresh();
		}
		if (frm.doc.cancellation_against) {
			frm.dashboard.clear_headline();
			frm.dashboard.set_headline(
				__("Reversal of {0} — fields are locked; only the posting date may be changed.",
					[frm.doc.cancellation_against]),
				"orange"
			);
			if (frm.doc.docstatus === 1) {
				frm.page.set_indicator(__("Reversal of {0}", [frm.doc.cancellation_against]), "orange");
			}
		}
	}

	// Hide core ERPNext Cancel for routed documents: direct cancel is blocked
	// server-side anyway (immutable ledger); showing the menu entry only
	// produces an error dialog. Standard-item documents keep core Cancel.
	function hideCoreCancel(frm) {
		const label = __("Cancel");
		const enc = encodeURIComponent(label);
		// toolbar secondary-action button ("Cancel" on submitted docs)
		const sec = frm.page.btn_secondary;
		if (sec && sec.text().trim() === label) sec.hide();
		// menu entries carry data-label (encoded); fall back to a
		// starts-with text match because the entry text includes the
		// keyboard-shortcut suffix.
		frm.page.menu
			.find(`a[data-label="${enc}"]`)
			.closest("li")
			.hide();
		frm.page.menu
			.find(".dropdown-item")
			.filter((_, el) => el.textContent.trim().startsWith(label))
			.closest("li")
			.hide();
	}

	function applyRoutedUx(frm) {
		frm.add_custom_button(__("Create Cancellation"), () => make_cancellation_dialog(frm));
		// Revoke the client-side cancel permission: frappe's own toolbar
		// logic then omits Cancel wherever it renders it (page action
		// button, menu entry, keyboard shortcut) — no DOM chasing. The
		// server guard remains the actual enforcement.
		if (frm.perm && frm.perm[0] && frm.perm[0].cancel) {
			frm.perm[0].cancel = 0;
			if (frm.toolbar && frm.toolbar.refresh) frm.toolbar.refresh();
		}
		// belt-and-braces for any straggler renders
		hideCoreCancel(frm);
		for (const delay of [250, 750]) {
			setTimeout(() => hideCoreCancel(frm), delay);
		}
		if (!frm._pv_menu_hook) {
			frm._pv_menu_hook = true;
			frm.page.wrapper.on("click", ".menu-btn-group", () => hideCoreCancel(frm));
		}
	}

	for (const doctype of DOCTYPES) {
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				lockReversal(frm);
				// A reversal is itself immutable: hide core Cancel there too.
				// It offers an action the server always refuses, and it must
				// not gain a "Create Cancellation" button (no reversing a
				// reversal) — so hide only, then stop.
				if (frm.doc.docstatus === 1 && frm.doc.is_cancellation) {
					hideCoreCancel(frm);
					// ERPNext's own Stock Entry refresh re-derives the warehouse
					// fields' read-only state from the purpose and runs after
					// this handler, re-enabling s_warehouse on a locked
					// reversal. Re-apply the lock behind it.
					for (const delay of [250, 750]) {
						setTimeout(() => {
							lockReversal(frm);
							hideCoreCancel(frm);
						}, delay);
					}
					return;
				}
				if (frm.doc.docstatus !== 1 || frm.doc.is_cancellation) return;

				// one routed-check per form load, cached on the frm
				if (frm._pv_routed === undefined) {
					frappe.call({
						method: "periodic_valuation.overrides.cancel_guard.is_routed_document",
						args: { doctype: frm.doc.doctype, name: frm.doc.name },
						callback: (r) => {
							frm._pv_routed = !!r.message;
							if (frm._pv_routed) applyRoutedUx(frm);
						},
					});
				} else if (frm._pv_routed) {
					applyRoutedUx(frm);
				}
				return;
			},
		});
	}

	function make_cancellation_dialog(frm) {
		frappe.confirm(
			__("Post a dated reversal document for {0}? The original stays submitted; GL mirrors on today's date.", [frm.doc.name]),
			() => {
				frappe.call({
					method: "periodic_valuation.periodic_moving_average.cancellation.make_cancellation",
					args: { doctype: frm.doc.doctype, name: frm.doc.name },
					freeze: true,
					callback(r) {
						if (r.message) frappe.set_route("Form", frm.doc.doctype, r.message);
					},
				});
			}
		);
	}
})();
