// Swiss VAT workflow. Server calculation and XML archive are authoritative.
const VAT_API = "erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration.";
frappe.ui.form.on("VAT Declaration", {
    onload(frm) {
        if (!frm.is_new()) return;
        const today = frappe.datetime.get_today().split("-").map(Number);
        const end = new Date(Date.UTC(today[0], Math.floor((today[1] - 1) / 3) * 3, 0));
        const start = new Date(Date.UTC(end.getUTCFullYear(), Math.floor(end.getUTCMonth() / 3) * 3, 1));
        if (!frm.doc.start_date) frm.set_value("start_date", start.toISOString().slice(0, 10));
        if (!frm.doc.end_date) frm.set_value("end_date", end.toISOString().slice(0, 10));
        if (!frm.doc.company) frm.set_value("company", frappe.defaults.get_user_default("Company"));
    },
    refresh(frm) {
        if (frm.doc.docstatus === 0) {
            frm.set_intro(__("Check period, accounting amounts and reporting basis. Submit validates and archives the Swiss XML; it does not post or file anything."), "blue");
            frm.add_custom_button(__("Get values"), () => vat_get_values(frm));
            frm.add_custom_button(__("Recalculate"), () => vat_calculate(frm));
        } else if (frm.doc.docstatus === 1) {
            const filed = frm.doc.filing_status === "Filed (user confirmed)";
            frm.set_intro(filed ? __("External filing confirmed. Original XML and closing entry are preserved. Downloading does not file again.") : __("XML archived. Upload this file to the ESTV portal and then record the external filing here."), filed ? "green" : "blue");
            if (frappe.perm.has_perm("VAT Declaration", 0, "write")) {
                frm.add_custom_button(__("Download original XML"), async () => {
                    const r = await frm.call({method: "create_transfer_file", freeze: true});
                    if (!r.message) return;
                    const url = URL.createObjectURL(new Blob([r.message.content], {type: "application/xml;charset=utf-8"}));
                    const link = document.createElement("a");
                    link.href = url; link.download = r.message.filename; link.click();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                    await frm.reload_doc();
                });
                if (!filed && frappe.perm.has_perm("VAT Declaration", 0, "submit")) {
                    frm.add_custom_button(__("Record external filing"), () => vat_confirm_filing(frm));
                }
            }
        }
    },
    company: vat_title,
    start_date: vat_title,
    end_date: vat_title,
});

function vat_title(frm) {
    if (!frm.is_new() || !frm.doc.end_date || !frm.doc.company) return;
    const end = frm.doc.end_date.split("-").map(Number);
    const title = `MWST ${end[0]} Q${Math.ceil(end[1] / 3)} - ${frm.doc.company}`;
    if (!frm.doc.title || frm.doc.title === frm._vat_auto_title) {
        frm._vat_auto_title = title;
        frm.set_value("title", title);
    }
}

async function vat_calculate(frm) {
    if (frm.doc.docstatus !== 0) return;
    const snapshot = JSON.stringify(frm.doc);
    const r = await frappe.call({method: VAT_API + "calculate_values", args: {values: snapshot}});
    // An older response must never overwrite a newer edit.
    if (JSON.stringify(frm.doc) === snapshot && r.message) await frm.set_value(r.message);
}

async function vat_get_values(frm) {
    if (!frm.doc.company || !frm.doc.start_date || !frm.doc.end_date) {
        frappe.msgprint(__("Select company and period first.")); return;
    }
    const snapshot = JSON.stringify(frm.doc);
    const r = await frappe.call({method: VAT_API + "get_period_values", freeze: true,
        freeze_message: __("Retrieving accounting amounts…"),
        args: {company: frm.doc.company, start_date: frm.doc.start_date, end_date: frm.doc.end_date, vat_type: frm.doc.vat_type}});
    if (r.message && JSON.stringify(frm.doc) === snapshot) { await frm.set_value(r.message); await vat_calculate(frm); }
}

function vat_confirm_filing(frm) {
    const dialog = new frappe.ui.Dialog({title: __("Record external filing"), fields: [
        {fieldtype: "HTML", options: `<p>${__("This records an upload already completed in the ESTV portal. No data is sent to the ESTV.")}</p>`},
        {fieldname: "evidence", fieldtype: "Small Text", label: __("Basis of filing confirmation"), reqd: 1},
        {fieldname: "filed_on", fieldtype: "Date", label: __("Actual external filing date"), description: __("Blank if the actual upload date is unknown.")},
    ], primary_action_label: __("Confirm completed filing"), primary_action: async (values) => {
        await frm.call({method: "confirm_external_filing", args: values, freeze: true});
        dialog.hide(); await frm.reload_doc();
    }});
    dialog.show();
}

// Keep the displayed calculation responsive without floating-point tax arithmetic in the browser.
const vat_inputs = ["total_revenue", "non_taxable_revenue", "tax_free_services", "revenue_abroad", "transfers", "non_taxable_services", "losses", "misc", "pretax_material", "pretax_investments", "missing_pretax", "pretax_correction_mixed", "pretax_correction_other", "form_1050", "form_1055", "vat_type"];
for (const suffix of ["_2024", ""]) {
    vat_inputs.push("additional_amount" + suffix);
    for (const prefix of ["normal", "reduced", "lodging"]) vat_inputs.push(prefix + "_amount" + suffix, prefix + "_rate" + suffix);
    for (const n of ["1", "2"]) vat_inputs.push("amount_" + n + suffix, "rate_" + n + suffix);
}
const vat_handlers = {};
for (const field of vat_inputs) vat_handlers[field] = frm => {
    clearTimeout(frm._vat_calc_timer);
    frm._vat_calc_timer = setTimeout(() => vat_calculate(frm), 250);
};
frappe.ui.form.on("VAT Declaration", vat_handlers);
