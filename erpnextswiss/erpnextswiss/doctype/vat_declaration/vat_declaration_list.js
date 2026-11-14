frappe.listview_settings["VAT Declaration"] = {
    add_fields: ["filing_status", "xml_file", "linked_journal_entry", "payable_tax", "start_date", "end_date"],
    has_indicator_for_draft: true,
    onload(listview) {
        if (!frappe.model.can_create("VAT Declaration")) return;
        listview.page.add_inner_button(__("Link existing XML and closing entry"), () => {
            const dialog = new frappe.ui.Dialog({
                title: __("Link existing XML and closing entry"),
                fields: [
                    {fieldname: "file_name", fieldtype: "Link", options: "File", label: __("Original XML attachment"), reqd: 1,
                        get_query: () => ({filters: {attached_to_doctype: "Journal Entry", file_name: ["like", "%.xml"]}})},
                    {fieldname: "preview", fieldtype: "HTML"},
                    {fieldname: "already_filed", fieldtype: "Check", label: __("Already filed externally")},
                    {fieldname: "evidence", fieldtype: "Small Text", label: __("Filing evidence"), depends_on: "eval:doc.already_filed",
                        description: __("Record who confirmed the filing or attach the existing receipt after linking. No submission is sent.")},
                ],
                primary_action_label: __("Check original XML"),
                async primary_action(values) {
                    const source = values.file_name;
                    const result = await frappe.call({method: "erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration.inspect_existing_xml", args: {file_name: source}, freeze: true});
                    if (source !== dialog.get_value("file_name")) return;
                    const info = result.message;
                    dialog.fields_dict.preview.$wrapper.text(`${info.company} · ${info.start_date} – ${info.end_date} · CHF ${info.payable_tax.toFixed(2)} · ${info.journal_entry}`);
                    dialog.set_primary_action(__("Link checked XML"), async () => {
                        if (source !== dialog.get_value("file_name")) { dialog.hide(); return; }
                        const evidence = dialog.get_value("already_filed") ? (dialog.get_value("evidence") || "").trim() : null;
                        if (dialog.get_value("already_filed") && !evidence) { frappe.msgprint(__("Record the basis for confirming the external filing.")); return; }
                        const linked = await frappe.call({method: "erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration.import_existing_xml", type: "POST", freeze: true,
                            args: {file_name: source, journal_entry: info.journal_entry, expected_sha256: info.sha256, filing_evidence: evidence}});
                        dialog.hide(); listview.refresh(); frappe.set_route("Form", "VAT Declaration", linked.message.name);
                    });
                },
            });
            dialog.show();
        });
    },
    get_indicator(doc) {
        if (doc.filing_status === "Filed (user confirmed)") return [__("Filed (user confirmed)"), "green", "filing_status,=,Filed (user confirmed)"];
        if (doc.filing_status === "XML ready") return [__("XML ready"), "blue", "filing_status,=,XML ready"];
        return [__("Draft"), "orange", "docstatus,=,0"];
    },
};
