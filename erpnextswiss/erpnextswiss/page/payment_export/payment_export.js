frappe.pages["payment_export"].on_page_load = function(wrapper) {
    const page = frappe.ui.make_app_page({parent: wrapper, title: __("Zahlungsexport"), single_column: true});
    frappe.payment_export.make(page);
    frappe.payment_export.run(page);
    frappe.breadcrumbs.add("Schweizer Buchhaltung");
};

frappe.payment_export = {
    make(page) {
        this.page = page;
        this.busy = false;
        this.body = $("<div>").appendTo(page.main);
        $(frappe.render_template("payment_export", {})).appendTo(this.body);
        page.main.find(".btn-create-file").on("click", () => this.export_selected(page));
        page.main.find(".btn-refresh").on("click", () => {
            page.main.find(".btn-create-file").removeClass("hide");
            page.main.find(".btn-refresh").addClass("hide");
            this.run(page);
        });
    },
    export_selected(page) {
        if (this.busy) return;
        const payments = page.main.find(".payment-table input[type=checkbox]:checked")
            .map(function() { return this.name; }).get();
        if (!payments.length) {
            frappe.msgprint(__("Bitte mindestens eine Zahlung auswählen."));
            return;
        }
        this.busy = true;
        const button = page.main.find(".btn-create-file").prop("disabled", true);
        frappe.call({
            method: "erpnextswiss.erpnextswiss.page.payment_export.payment_export.generate_payment_file",
            type: "POST", args: {payments}, btn: button, freeze: true,
            freeze_message: __("Zahlungen vollständig prüfen und Zahlungsoriginal sichern …"),
            callback: r => {
                if (!r.message) return;
                const result = r.message;
                const log = page.main.find(".insert-log-messages").empty();
                $("<p>").text(__("Zahlungen geprüft und gebucht. Die Zahlungsdatei ist zur Einreichung bei Ihrer Bank bereit.")).appendTo(log);
                if (result.file_url) {
                    $("<a>").attr({href: result.file_url, download: result.file_name})
                        .text(__("Gesichertes Zahlungsoriginal erneut herunterladen")).appendTo(log);
                }
                button.addClass("hide");
                page.main.find(".btn-refresh").removeClass("hide");
                this.download(result.file_name || "payments.xml", result.content);
            },
            always: () => { this.busy = false; button.prop("disabled", false); }
        });
    },
    run(page) {
        frappe.call({
            method: "erpnextswiss.erpnextswiss.page.payment_export.payment_export.get_payments",
            callback: r => {
                if (!r.message) return;
                const parent = page.main.find(".payment-table").empty();
                if (r.message.payments.length) {
                    $(frappe.render_template("payment_export_table", r.message)).appendTo(parent);
                } else {
                    $("<p>").addClass("text-muted").text(__("Keine zahlbaren Zahlungseinträge im Entwurfsstatus gefunden.")).appendTo(parent);
                }
            }
        });
    },
    download(filename, content) {
        const url = URL.createObjectURL(new Blob([content], {type: "application/xml;charset=utf-8"}));
        const element = document.createElement("a");
        element.href = url;
        element.download = filename;
        element.style.display = "none";
        document.body.appendChild(element);
        element.click();
        element.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
};
