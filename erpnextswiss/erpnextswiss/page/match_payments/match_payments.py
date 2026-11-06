# Copyright (c) 2017-2018, libracore and contributors
# License: AGPL v3. See LICENCE

from __future__ import unicode_literals
import frappe
from frappe import throw, _
from erpnextswiss.erpnextswiss.page.bankimport.bankimport import create_reference

NON_ASSIGNABLE_INTERNAL_TRANSFER_MARKER = 'Nicht zuzuweisende interne Übertragung'

@frappe.whitelist()
def get_open_sales_invoices():
    # get unpaid sales invoices
    sql_query = ("""SELECT `name`, `customer`, `base_grand_total`, `outstanding_amount`, `due_date`, `esr_reference` 
                FROM `tabSales Invoice` 
                WHERE `docstatus` = 1 AND `outstanding_amount` > 0 
                ORDER BY `due_date` ASC""")
    unpaid_sales_invoices = frappe.db.sql(sql_query, as_dict=True)
    return {'unpaid_sales_invoices': unpaid_sales_invoices }

def _reference_variants(reference):
    if not isinstance(reference, str) or not reference.strip():
        return set()
    reference = reference.strip()
    variants = {reference}
    if reference.endswith('/1'):
        variants.add(reference[:-2])
    else:
        variants.add(reference + '/1')
    return variants


def _submitted_equivalent_exists(payment):
    """Hide a legacy draft only when one submitted, assigned cash movement proves it obsolete."""
    references = _reference_variants(payment.get('reference_no'))
    if not references:
        return False
    matches = frappe.get_all('Payment Entry', filters={
        'company': payment.get('company'), 'docstatus': 1, 'payment_type': 'Receive',
        'paid_to': payment.get('paid_to'), 'posting_date': payment.get('posting_date'),
        'reference_no': ('in', list(references))},
        fields=['name', 'paid_amount', 'received_amount', 'paid_from_account_currency',
                'paid_to_account_currency', 'unallocated_amount'],
        limit_page_length=2)
    if len(matches) != 1:
        return False
    match = matches[0]
    if (match.get('paid_amount') != payment.get('paid_amount')
            or match.get('received_amount') != payment.get('received_amount')
            or match.get('paid_from_account_currency') != payment.get('paid_from_account_currency')
            or match.get('paid_to_account_currency') != payment.get('paid_to_account_currency')
            or match.get('unallocated_amount') != 0):
        return False
    return bool(frappe.db.exists('Payment Entry Reference', {
        'parent': match['name'], 'parenttype': 'Payment Entry'}))


def _is_non_assignable_internal_transfer(payment):
    remarks = payment.get('remarks')
    return (
        payment.get('custom_remarks') == 1
        and isinstance(remarks, str)
        and remarks.startswith(NON_ASSIGNABLE_INTERNAL_TRANSFER_MARKER)
    )


@frappe.whitelist()
def get_unallocated_payment_entries():
    sql_query = ("""SELECT `name`, `company`, `party`, `paid_amount`, `received_amount`,
                       `unallocated_amount`, `posting_date`, `custom_remarks`, `remarks`, `reference_no`,
                       `paid_to`, `paid_from_account_currency`, `paid_to_account_currency`
                FROM `tabPayment Entry`
                WHERE `docstatus` = 0
                  AND `payment_type` = 'Receive'
                  AND `unallocated_amount` > 0
                ORDER BY `posting_date` ASC""")
    drafts = frappe.db.sql(sql_query, as_dict=True)
    unallocated = [
        payment for payment in drafts
        if not _is_non_assignable_internal_transfer(payment)
        and not _submitted_equivalent_exists(payment)
    ]
    return {'unallocated_payment_entries': unallocated}

@frappe.whitelist(methods=["POST"])
def match(sales_invoice, payment_entry):
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    # get the customer
    customer = frappe.get_value("Sales Invoice", sales_invoice, "customer")
    if customer:
        payment_entry_record = frappe.get_doc("Payment Entry", payment_entry)
        if payment_entry:
            # assign the actual customer
            payment_entry_record.party = customer            
            payment_entry_record.save()
            
            # now, add the reference to the sales invoice
            create_reference(payment_entry, sales_invoice)
            
            # and finally submit the payment entry (direct submit will void the reference :-( )
            #payment_entry_record.submit()
            
            return { 'payment_entry': payment_entry_record.name }
        else:
            return { 'error': "Payment entry not found" }
    else:
        return { 'error': "Customer not found" }
 
@frappe.whitelist(methods=["POST"])
def submit(payment_entry):
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    payment_entry_record = frappe.get_doc("Payment Entry", payment_entry)
    payment_entry_record.submit()
    return { 'message': "Done" }

@frappe.whitelist(methods=["POST"])
def submit_all(payment_entries):
    # get the array from the string parameter
    payments = frappe.parse_json(payment_entries) if isinstance(payment_entries, str) else payment_entries
    # loop through all matched payments and submit them
    for payment_entry in payments:
        submit(payment_entry)
    return { 'message': "Done" }

@frappe.whitelist(methods=["POST"])
def auto_match(method="docid"):
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    # make method lower case
    method = method.lower()
    # prepare array of matched payments
    matched_payments = []
    # read all new payments
    new_payments = get_unallocated_payment_entries()['unallocated_payment_entries']
    # loop through all unpaid sales invoices
    pending_sales_invoices = get_open_sales_invoices()['unpaid_sales_invoices']
    for unpaid_sales_invoice in pending_sales_invoices:
        if method == "docid":
            for payment in new_payments:
                if unpaid_sales_invoice['name'] in payment['remarks']:
                    matched_payment_entry = match(unpaid_sales_invoice['name'], payment['name'])['payment_entry']
                    matched_payments.append(matched_payment_entry)
        elif method == "esr":
            # only check Sales Invoice records with an ESR reference
            if unpaid_sales_invoice['esr_reference']:
                for payment in new_payments:
                    if unpaid_sales_invoice['esr_reference'].replace(' ', '') in payment['remarks']:
                        matched_payment_entry = match(unpaid_sales_invoice['name'], payment['name'])['payment_entry']
                        matched_payments.append(matched_payment_entry)
    return { 'message': "Done", 'payments': matched_payments }
