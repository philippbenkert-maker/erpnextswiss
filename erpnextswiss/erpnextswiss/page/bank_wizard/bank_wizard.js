frappe.pages['bank_wizard'].on_page_load = function(wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: 'Bank-Assistent',
        single_column: true
    });

    frappe.bank_wizard.make(page);
    frappe.bank_wizard.run();
    
    // add the application reference
    frappe.breadcrumbs.add("Schweizer Buchhaltung");
}

frappe.bank_wizard = {
    start: 0,
    make: function(page) {
        var me = frappe.bank_wizard;
        me.page = page;
        me.body = $('<div></div>').appendTo(me.page.main);
        var data = "";
        $(frappe.render_template('bank_wizard', data)).appendTo(me.body);
        
        this.page.main.find(".btn-parse-file").on('click', function() {
            if (me.busy) return;
            const account = document.getElementById("bank_account").value;
            const file = document.getElementById("input_file").files[0];
            me.clear_preview();
            if (!file || !account) {
                frappe.msgprint("Bitte eine Bankdatei und das zugehörige Bankkonto auswählen.");
                return;
            }
            if (!/\.(xml|zip)$/i.test(file.name) || !file.size || file.size > 32 * 1024 * 1024) {
                frappe.msgprint("Bitte eine XML- oder ZIP-CAMT-Datei mit höchstens 32 MB auswählen.");
                return;
            }
            me.start_wait();
            const reader = new FileReader();
            reader.onload = function(event) {
                // Preserve original bytes/encoding. ZIP members are validated
                // separately on the server, never joined into one XML string.
                const encoded = String(event.target.result).split(',')[1];
                me.parse(encoded, account);
            };
            reader.onerror = reader.onabort = function() {
                me.end_wait();
                frappe.msgprint("Die Datei konnte nicht gelesen werden. Bitte erneut auswählen.");
            };
            try {
                reader.readAsDataURL(file);
            } catch (error) {
                reader.onerror();
            }
        });
        this.page.main.find("#input_file").on('change', function() { me.clear_preview(); });
    },
    clear_preview: function() {
        document.getElementById("table_placeholder").innerHTML = "";
        document.getElementById("auto_process_matches").value = 0;
    },
    parse: function(content, account) {
        frappe.call({
            method: 'erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.read_camt_file',
            type: 'POST',
            args: { content_base64: content, account: account },
            callback: function(r) {
                if (!r.message || r.exc) return;
                document.getElementById("bank_account").value = r.message.bank;
                if (!frappe.bank_wizard.set_default_accounts(r.message.bank)) {
                    frappe.bank_wizard.clear_preview();
                    frappe.msgprint("Die Kontoeinstellungen konnten nicht geladen werden. Bitte erneut einlesen.");
                    return;
                }
                frappe.show_alert(r.message.transactions.length + " Transaktionen zum Abgleich");
                frappe.bank_wizard.render_response(r.message);
            },
            error: function() { frappe.bank_wizard.clear_preview(); },
            always: function() { frappe.bank_wizard.end_wait(); }
        });
    },
    run: function() {
        // populate bank accounts
        frappe.call({
            method: 'erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.get_bank_accounts',
            callback: function(r) {
                if (r.message) {
                    var select = document.getElementById("bank_account");
                    // add on change event
                    select.onchange = function() {
                        frappe.bank_wizard.clear_preview();
                        if (select.value) frappe.bank_wizard.set_default_accounts(select.value);
                    };
                    for (var i = 0; i < r.message.accounts.length; i++) {
                        var opt = document.createElement("option");
                        opt.value = r.message.accounts[i];
                        opt.textContent = r.message.accounts[i];
                        select.appendChild(opt);
                    }
                    // call with initial value
                    if (select.value) frappe.bank_wizard.set_default_accounts(select.value);
                } 
            }
        }); 
        frappe.call({
            method: 'erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.get_intermediate_account',
            callback: function(r) {
                if ((r.message) && (r.message.account != "")) {
                    document.getElementById("intermediate_account").value = r.message.account;
                } else {
                    frappe.msgprint("Bitte das <b>Zwischenkonto</b> in den <a href=\"/desk#Form/ERPNextSwiss Settings\">App-Einstellungen</a> setzen.");
                }
            }
        }); 
        frappe.call({
            method: 'erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.get_default_customer',
            callback: function(r) {
                if ((r.message) && (r.message.customer != "")) {
                    document.getElementById("default_customer").value = r.message.customer;
                } else {
                    frappe.msgprint("Bitte den <b>Standardkunden</b> in den <a href=\"/desk#Form/ERPNextSwiss Settings\">App-Einstellungen</a> setzen.");
                }
            }
        }); 
        frappe.call({
            method: 'erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.get_default_supplier',
            callback: function(r) {
                if ((r.message) && (r.message.supplier != "")) {
                    document.getElementById("default_supplier").value = r.message.supplier;
                } else {
                    frappe.msgprint("Bitte den <b>Standardlieferanten</b> in den <a href=\"/desk#Form/ERPNextSwiss Settings\">App-Einstellungen</a> setzen.");
                }
            }
        }); 
    },
    set_default_accounts: function(bank_account) {
        let loaded = false;
        frappe.call({
            'method': 'erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.get_default_accounts',
            'args': {
                'bank_account': bank_account
            },
            'async': false,                                             // important: needs to be done before render_response (which also triggers auto-submit)
            'callback': function(r) {
                if (r.message) {
                    document.getElementById("company").value = r.message.company;
                    document.getElementById("payable_account").value = r.message.payable_account;
                    document.getElementById("receivable_account").value = r.message.receivable_account;
                    document.getElementById("expense_payable_account").value = r.message.expense_payable_account;
                    document.getElementById("auto_process_matches").value = r.message.auto_process_matches;
                    loaded = true;
                }
            }
        });
        return loaded;
    },
    start_wait: function() {
        frappe.bank_wizard.busy = true;
        document.getElementById("waitingScreen").classList.remove("hidden");
        ["btn-parse-file", "input_file", "bank_account"].forEach(function(id) {
            document.getElementById(id).disabled = true;
        });
    },
    end_wait: function() {
        frappe.bank_wizard.busy = false;
        document.getElementById("waitingScreen").classList.add("hidden");
        ["btn-parse-file", "input_file", "bank_account"].forEach(function(id) {
            document.getElementById(id).disabled = false;
        });
    },
    render_response: function(message) {
        // display the transactions as table
        let container = document.getElementById("table_placeholder");
        // var content = frappe.render_template('transaction_table', message);
        container.innerHTML = message.html;
        // attach button handlers
        let auto_process_matches = parseInt(document.getElementById("auto_process_matches").value);
        let default_customer = document.getElementById("default_customer").value;
        let default_supplier = document.getElementById("default_supplier").value;
        message.transactions.forEach(function (transaction) {
        // add generic payables/receivables handler
        let button = null;
        if (transaction.credit_debit == "DBIT") {
            // quick match (purchase invoice)
            button = document.getElementById("btn-quick-pinv-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Supplier",
                        'party': transaction.party_match,
                        'references': transaction.invoice_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'auto_submit': 1,
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.quick_payment_entry(payment, transaction.txid);
                });
                if (auto_process_matches === 1) {
                    button.click();
                }
            }
            // quick match (purchase invoice)
            button = document.getElementById("btn-quick-exp-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Employee",
                        'party': transaction.employee_match,
                        'references': transaction.expense_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'auto_submit': 1,
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.quick_payment_entry(payment, transaction.txid);
                });
                if (auto_process_matches === 1) {
                    button.click();
                }
            }
            // purchase invoice match
            button = document.getElementById("btn-close-pinv-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Supplier",
                        'party': transaction.party_match,
                        'references': transaction.invoice_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // expense claim match
            button = document.getElementById("btn-close-exp-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("expense_payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Employee",
                        'party': transaction.employee_match,
                        'references': transaction.expense_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // supplier match
            button = document.getElementById("btn-close-supplier-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Supplier",
                        'party': transaction.party_match,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // employee match
            button = document.getElementById("btn-close-employee-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("expense_payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Employee",
                        'party': transaction.employee_match,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // payables
            button = document.getElementById("btn-close-payable-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Supplier",
                        'party': default_supplier,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // pattern match (purchase invoice)
            button = document.getElementById("btn-quick-pattern-pay-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("bank_account").value,
                        'paid_to': document.getElementById("payable_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Pay",
                        'party_type': "Supplier",
                        'party': (transaction.party_match || default_supplier),
                        'references': transaction.invoice_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'auto_submit': 1,
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value,
                        'pattern': transaction.pattern
                    }
                    frappe.bank_wizard.quick_payment_entry(payment, transaction.txid);
                });
                if (auto_process_matches === 1) {
                    button.click();
                }
            }
        } else {
            // quick match (sales invoice)
            let button = document.getElementById("btn-quick-sinv-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("receivable_account").value,
                        'paid_to': document.getElementById("bank_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Receive",
                        'party_type': "Customer",
                        'party': transaction.party_match,
                        'references': transaction.invoice_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'auto_submit': 1,
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.quick_payment_entry(payment, transaction.txid);
                });
                if (auto_process_matches === 1) {
                    button.click();
                }
            }
            // sales invoice match
            button = document.getElementById("btn-close-sinv-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("receivable_account").value,
                        'paid_to': document.getElementById("bank_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Receive",
                        'party_type': "Customer",
                        'party': transaction.party_match,
                        'references': transaction.invoice_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // customer match
            button = document.getElementById("btn-close-customer-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("receivable_account").value,
                        'paid_to': document.getElementById("bank_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Receive",
                        'party_type': "Customer",
                        'party': transaction.party_match,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // receivables
            button = document.getElementById("btn-close-receivable-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("receivable_account").value,
                        'paid_to': document.getElementById("bank_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Receive",
                        'party_type': "Customer",
                        'party': default_customer,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
            // pattern match (purchase invoice)
            button = document.getElementById("btn-quick-pattern-rec-" + transaction.txid);
            if (button) {
                button.addEventListener("click", function(e) {
                    e.target.disabled = true;
                    let payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': document.getElementById("receivable_account").value,
                        'paid_to': document.getElementById("bank_account").value,
                        'reference_no': transaction.unique_reference,
                        'type': "Receive",
                        'party_type': "Customer",
                        'party': (transaction.party_match || default_customer),
                        'references': transaction.invoice_matches,
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'auto_submit': 1,
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value,
                        'pattern': transaction.pattern
                    }
                    frappe.bank_wizard.quick_payment_entry(payment, transaction.txid);
                });
                if (auto_process_matches === 1) {
                    button.click();
                }
            }
        }
        // add intermediate account handler
        button = document.getElementById("btn-close-intermediate-" + transaction.txid);
        if (button) {
            button.addEventListener("click", function(e) {
                e.target.disabled = true;
                let paid_to = document.getElementById("bank_account").value;
                let paid_from = document.getElementById("intermediate_account").value;
                if (transaction.credit_debit == "DBIT") {
                    paid_from = document.getElementById("bank_account").value;
                    paid_to = document.getElementById("intermediate_account").value;
                    }
                    // note: currency is defined through account currencies of the bank account
                    var payment = {
                        'amount': transaction.amount,
                        'date': transaction.date,
                        'paid_from': paid_from,
                        'paid_to': paid_to,
                        'reference_no': transaction.unique_reference,
                        'type': "Internal Transfer",
                        'remarks': (transaction.transaction_reference + ", " + transaction.party_name + ", " + transaction.party_address),
                        'party_iban': transaction.party_iban,
                        'company': document.getElementById("company").value
                    }
                    frappe.bank_wizard.create_payment_entry(payment, transaction.txid);
                });
            }
        }); 
    },
    create_payment_entry: function(payment, txid) {
        frappe.call({
            method: "erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.make_payment_entry",
            args: payment,
            callback: function(r)
            {
                // open new record in a separate tab
                window.open(r.message.link, '_blank');
                frappe.bank_wizard.close_entry(txid);
            }
        });    
    },
    close_entry: function(txid) {
        // close the entry in the list
        var table_row = document.getElementById("row-transaction-" + txid);
        table_row.classList.add("hidden");    
    },
    quick_payment_entry: function(payment, txid) {
        frappe.call({
            'method': "erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard.make_payment_entry",
            'args': payment,
            'callback': function(r)
            {
                // show alert
                frappe.show_alert("Transaktion abgeglichen");
                frappe.bank_wizard.close_entry(txid);
            }
        });    
    }
}
