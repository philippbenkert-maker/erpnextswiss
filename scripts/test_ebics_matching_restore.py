"""Native ledger acceptance; ONLY a disposable internal-network restore, rollback always.

Pass existing restored invoice IDs as arguments, never commit customer data.
"""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
from unittest.mock import patch

import frappe

parser = argparse.ArgumentParser()
parser.add_argument('--sales-invoice', required=True)
parser.add_argument('--purchase-invoice', required=True)
parser.add_argument('--discount-account-number', required=True)
args = parser.parse_args()
site = 'kt-platform-test.local'
frappe.init(site=site, sites_path='/home/frappe/frappe-bench/sites')
assert frappe.conf.db_host == 'platform-restore-db' and frappe.conf.pause_scheduler
frappe.connect()
frappe.set_user('Administrator')
from erpnextswiss.erpnextswiss import ebics_automation as auto

try:
    for doctype, name in [('Sales Invoice', args.sales_invoice), ('Purchase Invoice', args.purchase_invoice)]:
        invoice = frappe.get_doc(doctype, name)
        incoming = doctype == 'Sales Invoice'
        bank = frappe.get_all('Account', filters=dict(company=invoice.company, is_group=0, disabled=0,
            account_type='Bank', account_currency=invoice.currency), pluck='name')[0]
        discount_account = frappe.get_all('Account', filters=dict(company=invoice.company, is_group=0,
            disabled=0, account_number=args.discount_account_number), pluck='name')
        assert len(discount_account) == 1
        frappe.db.set_value('Company', invoice.company, 'default_discount_account', discount_account[0])
        frappe.clear_document_cache('Company', invoice.company)
        outstanding = Decimal(str(invoice.outstanding_amount))
        discount = Decimal('0')
        booking_date = str(invoice.posting_date)
        if incoming:
            term = invoice.payment_schedule[0]
            assert term.discount_type == 'Percentage' and term.discount
            booking_date = str(term.discount_date)
            discount = (outstanding * Decimal(str(term.discount)) / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        amount = outstanding - discount
        reference = 'EBICS-ISOLATED-ACCEPTANCE-' + ('RECEIVE' if incoming else 'PAY')
        candidate = dict(amount=float(amount), matched_amount=float(outstanding), currency=invoice.currency,
            credit_debit='CRDT' if incoming else 'DBIT', date=booking_date, unique_reference=reference,
            invoice_matches=[name], party_match=invoice.customer if incoming else invoice.supplier,
            transaction_reference=name)
        statement = dict(balance_check='matched', currency=invoice.currency, account=bank)
        entry = dict(issues=[], detail_count=1, candidates=[candidate], amount=str(amount),
            credit_debit=candidate['credit_debit'], booking_date=booking_date, reference='GROUP-REFERENCE',
            entry_reference=None, bank_detail_references=[reference])
        connection = frappe._dict(company=invoice.company)
        with patch.object(auto, 'get_site_config', return_value={'ebics_auto_discount_tax_split': True}), \
                patch.object(frappe.db, 'commit'):
            assert auto._book_exact_match(connection, statement, entry, 'ISOLATED-RESTORE')
            payments = frappe.get_all('Payment Entry', filters=dict(company=invoice.company, reference_no=reference), pluck='name')
            assert len(payments) == 1
            pe = frappe.get_doc('Payment Entry', payments[0])
            assert pe.docstatus == 1 and pe.difference_amount == 0 and pe.unallocated_amount == 0
            assert Decimal(str(pe.paid_amount)) == amount == Decimal(str(pe.received_amount))
            assert (pe.paid_to if incoming else pe.paid_from) == bank
            assert Decimal(str(pe.references[0].allocated_amount)) == outstanding
            after = frappe.get_doc(doctype, name)
            assert after.outstanding_amount == 0
            if incoming:
                assert Decimal(str(after.payment_schedule[0].discounted_amount)) == discount
                tax_accounts = {r.account_head for r in invoice.taxes if r.base_tax_amount_after_discount_amount}
                assert tax_accounts.issubset({r.account for r in pe.deductions})
                assert Decimal(str(sum(r.amount for r in pe.deductions))).quantize(Decimal('.01')) == discount
            gl = frappe.get_all('GL Entry', filters=dict(voucher_type='Payment Entry', voucher_no=pe.name, is_cancelled=0),
                                fields=['debit','credit'])
            assert gl and abs(sum(r.debit-r.credit for r in gl)) < .000001
            assert not auto._book_exact_match(connection, statement, entry, 'ISOLATED-RESTORE-REPEAT')
            assert frappe.db.count('Payment Entry', dict(company=invoice.company, reference_no=reference)) == 1
        frappe.db.rollback()
        assert Decimal(str(frappe.db.get_value(doctype, name, 'outstanding_amount'))) == outstanding
        print(json.dumps(dict(result='PASS', case='contract_discount_and_tax' if incoming else 'exact_supplier_payment',
                              native_submit=True, balanced_ledger=True, replay_safe=True, rolled_back=True)))
finally:
    frappe.db.rollback()
    frappe.destroy()
