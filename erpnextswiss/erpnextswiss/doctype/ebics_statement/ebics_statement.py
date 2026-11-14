# -*- coding: utf-8 -*-
# Copyright (c) 2024-2025, libracore (https://www.libracore.com) and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard import read_camt053_meta, read_camt053, make_payment_entry, get_default_accounts
from frappe import _
import ast
from erpnextswiss.scripts.bank_matching_scope import BankMatchingScope
from erpnextswiss.scripts.bank_file_admission import BankFileError

class ebicsStatement(Document):
    def before_save(self):
        if self.status != "Completed":
            self.update_status_from_transactions()
        return
        
    def update_status_from_transactions(self):
        all_completed = True
        for t in self.transactions:
            if t.status != "Completed":
                all_completed = False
                break
        if all_completed:
            self.status = "Completed"
        return
        
    def parse_content(self, debug=False):
        """Bind every statement to its authorized connection before matching."""
        frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
        self.check_permission("write")
        if not self.xml_content:
            frappe.throw(_("Cannot parse an empty bank statement"))
        if any(row.payment_entry or row.status == "Completed" for row in self.transactions):
            frappe.throw(_("Processed statement rows must be retained; they cannot be parsed again"))
        connection = frappe.get_doc("ebics Connection", self.ebics_connection)
        connection.check_permission("read")
        company = connection.company
        if not company or (self.company and self.company != company):
            frappe.throw(_("The statement company must match its EBICS connection"))
        frappe.get_doc("Company", company).check_permission("read")
        try:
            meta = read_camt053_meta(self.xml_content)
        except BankFileError as error:
            frappe.throw(_("Bank file could not be admitted: {0}").format(str(error)))
        iban = "".join((meta.get("iban") or "").split()).upper()
        accounts = frappe.get_list("Account", filters={"company": company, "account_type": "Bank",
                                  "disabled": 0, "is_group": 0}, fields=["name", "iban", "account_currency"],
                                  limit_page_length=0)
        matches = [row.name for row in accounts if iban and "".join((row.iban or "").split()).upper() == iban
                   and (not meta.get("currency") or row.account_currency == meta["currency"])
                   and (not self.account or row.name == self.account)]
        if len(matches) != 1:
            frappe.throw(_("Select the unique authorized bank account matching this statement and connection"))
        scope = BankMatchingScope(matches[0])
        parsed = read_camt053(self.xml_content, scope.account)
        # Child records have no company permission boundary of their own.
        # Resolve permitted parents for this exact account before deduplication.
        parents = frappe.get_list("ebics Statement", filters={"company": company, "account": scope.account,
                                  "name": ["!=", self.name]}, pluck="name", limit_page_length=0)
        existing = set()
        if parents:
            existing = {(row.unique_reference, str(row.date)) for row in frappe.get_all(
                "ebics Statement Transaction", filters={"parenttype": "ebics Statement", "parent": ["in", parents]},
                fields=["unique_reference", "date"])}
        rows = []
        for candidate in parsed["transactions"]:
            if (candidate.get("unique_reference"), str(candidate.get("date"))) in existing:
                continue
            transaction = dict(candidate)
            for field in ("invoice_matches", "expense_matches"):
                if transaction.get(field):
                    transaction[field] = str(transaction[field])
            transaction["status"] = "Pending"
            rows.append(transaction)
        # All validation precedes modification. The enclosing request owns commit.
        self.update({"company": company, "account": scope.account, "currency": scope.currency,
                     "opening_balance": meta.get("opening_balance"), "closing_balance": meta.get("closing_balance"),
                     "status": "Pending", "transactions": rows})
        self.save()

    def process_transactions(self):
        """
        Analyse transactions and if possible, match them
        """
        default_accounts = get_default_accounts(company=self.company)
        
        for t in self.transactions:
            # if matched amount equals the transaction amount, create and submit payment
            if t.status == "Pending" and not t.payment_entry and t.matched_amount == t.amount and (t.party_match or t.employee_match):
                payment = {
                    'amount': t.amount,
                    'date': t.date,
                    'reference_no': t.unique_reference,
                    'party_iban': t.party_iban
                }
                if t.credit_debit == "DBIT":
                    # outflow: purchase invoice or expense
                    if t.invoice_matches:
                        payment.update({
                            'paid_from': self.account,
                            'paid_to': default_accounts.get('payable_account'),
                            'type': "Pay",
                            'party_type': "Supplier",
                            'party': t.party_match,
                            'references': t.invoice_matches,            # note: string will be parsed in make_payment_entry
                            'remarks': "{0}, {1}, {2}".format(t.transaction_reference or "", t.party_name or "", t.party_address or ""),
                            'auto_submit': 1,
                            'company': self.company
                        })
                    elif t.expense_matches:
                        payment.update({
                            'paid_from': self.account,
                            'paid_to': default_accounts.get('payable_account'),
                            'type': "Pay",
                            'party_type': "Employee",
                            'party': t.employee_match,
                            'references': t.expense_matches,            # note: string will be parsed in make_payment_entry
                            'remarks': "{0}, {1}, {2}".format(t.transaction_reference or "", t.party_name or "", t.party_address or ""),
                            'auto_submit': 1,
                            'company': self.company
                        })
                else:
                    # inflow: debtor
                    payment.update({
                        'paid_from': default_accounts.get('receivable_account'),
                        'paid_to': self.account,
                        'type': "Receive",
                        'party_type': "Customer",
                        'party': t.party_match,
                        'references': t.invoice_matches,            # note: string will be parsed in make_payment_entry
                        'remarks': "{0}, {1}, {2}".format(t.transaction_reference or "", t.party_name or "", t.party_address or ""),
                        'auto_submit': 1,
                        'company': self.company
                    })
                
                try:
                    payment = make_payment_entry(**payment)
                    t.payment_entry = payment.get('payment_entry')
                    t.status = "Completed"
                except Exception as err:
                    t.status == "Error"
                    t.remarks = "{0}".format(err)
        
        # save
        self.save()
        frappe.db.commit()
        
        # run post-processing triggers
        self.post_process()
        
        return

    def post_process(self):
        """
        Use this hook to add post processing actions, i.e. custom matching
        
        Add in your custom hooks.py:
            doc_events = {
                "ebics Statement": {
                    "post_process": "myapp.mymodule.ebics.post_process_ebics"
                }
            }
        """
        events = frappe.get_hooks("doc_events")
        if events:
            ebics_events = events.get('ebics Statement')
            if ebics_events:
                post_processing_hooks = ebics_events.get('post_process')
                for hook in post_processing_hooks:
                    frappe.call(hook, self, "post_process")
                    
        return
