frappe.ui.form.on("Swiss VAT Settings", {
    setup(frm) {
        frm.set_query("account", "accounts", () => ({filters: {company: frm.doc.company, is_group: 0, disabled: 0}}));
    }
});
