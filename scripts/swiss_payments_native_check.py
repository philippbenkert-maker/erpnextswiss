#!/usr/bin/env python3
"""Payment acceptance on a restored production copy; no network or bank calls.

Creates two isolated, unallocated test payments and always rolls them back.
Never runs against erp.local or a non-isolated database.
"""
import argparse
import json
import os
from decimal import Decimal
from unittest.mock import patch
import frappe
from lxml import etree
from vat_accounting_integrity import accounting_digest


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--site',required=True);args=parser.parse_args()
    assert args.site=='kt-platform-test.local', 'Restored test site required'
    os.chdir('/home/frappe/frappe-bench/sites')
    frappe.init(site=args.site,sites_path=os.getcwd())
    assert frappe.conf.db_host=='platform-restore-db', 'Isolated database required'
    frappe.connect();frappe.set_user('Administrator');frappe.flags.mute_emails=True
    from erpnextswiss.erpnextswiss.page.payment_export import payment_export as api
    from erpnextswiss.erpnextswiss import swiss_payments as bank
    from erpnextswiss.erpnextswiss.common_functions import get_primary_address
    before=accounting_digest()
    before_counts={kind:frappe.db.count(kind) for kind in ('Payment Entry','File','GL Entry','Payment Ledger Entry')}
    checks=[]
    def check(condition,message):
        assert condition,message
        checks.append(message);print('PASS '+message,flush=True)
    try:
        check(frappe.get_meta('Employee').has_field('bic'),'salary export uses installed Employee BIC field')
        company=frappe.db.get_value('Company',{'default_currency':'CHF'},['name','default_payable_account'],as_dict=True)
        assert company and company.default_payable_account,'CHF company with payable account required'
        bank_account=frappe.db.get_value('Account',{'company':company.name,'account_type':'Bank','disabled':0,
            'is_group':0,'account_currency':'CHF'},'name')
        supplier=frappe.db.get_value('Supplier',{},'name')
        assert bank_account and supplier,'Restored bank account and supplier required'
        check(bool(get_primary_address(company.name,'Company')),'company payment address is accessible through guarded lookup')
        records=[]
        for index in range(2):
            record=frappe.get_doc(dict(doctype='Payment Entry',payment_type='Pay',company=company.name,
                posting_date='2026-11-14',party_type='Supplier',party=supplier,paid_from=bank_account,
                paid_to=company.default_payable_account,paid_amount=1.25,received_amount=1.25,
                source_exchange_rate=1,target_exchange_rate=1,reference_no='TEST-ONLY-'+str(index),
                reference_date='2026-11-14',transaction_type='IBAN',iban='CH9300762011623852957'))
            record.insert(ignore_permissions=True);records.append(record)
        fixture=dict(name='Synthetic & Co',street='Testweg',building='1',pincode='8000',city='Zuerich',country_code='CH')
        def payment_row(record):
            return dict(name=record.name,amount=record.received_amount,currency='CHF',execution_date='2026-11-16',
                iban='CH9300762011623852957',creditor=fixture,debtor=dict(fixture,iban='CH9300762011623852957'),
                reference='ISOLATED TEST ONLY')
        names=[record.name for record in records]
        with patch.object(api,'_payment_row',side_effect=lambda r:dict(payment_row(r),iban='invalid') if r.name==names[1] else payment_row(r)):
            try:api._generate_payment_export(names)
            except frappe.ValidationError:pass
            else:raise AssertionError('Invalid second payment accepted')
        check(all(frappe.db.get_value('Payment Entry',name,'docstatus')==0 for name in names),
              'invalid last payment leaves all drafts unsubmitted')
        check(not frappe.db.exists('File',{'attached_to_doctype':'Payment Entry','attached_to_name':['in',names]}),
              'invalid batch creates no payment originals')
        # First native submit succeeds, second fails: SQL savepoint must restore both drafts and file rows.
        from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry
        submit=PaymentEntry.submit
        def fail_second(doc,*args,**kwargs):
            if doc.name==names[1]:raise frappe.ValidationError('Synthetic second submit failure')
            return submit(doc,*args,**kwargs)
        with patch.object(api,'_payment_row',side_effect=payment_row),patch.object(PaymentEntry,'submit',fail_second):
            try:api._generate_payment_export(names)
            except frappe.ValidationError:pass
            else:raise AssertionError('Second submit failure ignored')
        check(all(frappe.db.get_value('Payment Entry',name,'docstatus')==0 for name in names),
              'native posting failure rolls back first submission')
        check(not frappe.db.exists('GL Entry',{'voucher_no':['in',names]}),'failed batch leaves no ledger rows')
        check(not frappe.db.exists('File',{'attached_to_doctype':'Payment Entry','attached_to_name':['in',names]}),
              'failed batch leaves no archived file records')
        with patch.object(api,'_payment_row',side_effect=payment_row):
            result=api._generate_payment_export(names,expected_count=2,expected_total='2.50')
        check(all(frappe.db.get_value('Payment Entry',name,'docstatus')==1 for name in names),
              'valid batch submits both native Payment Entries')
        check(frappe.db.count('GL Entry',{'voucher_no':['in',names]})>=4,'valid native payments create balanced ledger entries')
        originals=frappe.get_all('File',filters={'attached_to_doctype':'Payment Entry','attached_to_name':['in',names]},
            fields=['name','is_private','attached_to_name'])
        check(len(originals)==2 and all(f.is_private for f in originals),'every payment has a private original attachment')
        bank._schema().assertValid(etree.fromstring(result['content'].encode()))
        check(result['count']==2 and Decimal(result['total'])==Decimal('2.50'),'complete Swiss XML reconciles with payment total')
        check(all(frappe.get_doc('File',f.name).get_content() in (result['content'],result['content'].encode()) for f in originals),
              'downloaded originals exactly equal validated XML')
        with patch.object(api,'_payment_row',side_effect=payment_row):
            try:api._generate_payment_export(names)
            except frappe.ValidationError:pass
            else:raise AssertionError('Repeated export submitted again')
        check(frappe.db.count('File',{'attached_to_doctype':'Payment Entry','attached_to_name':['in',names]})==2,
              'repeated request cannot create a second bank file')
        # A real submitted proposal keeps its original identifiers across repeated
        # downloads. Synthetic recipient data is held only in memory; no bank call.
        proposal_name=frappe.db.get_value('Payment Proposal',{'docstatus':1},'name')
        assert proposal_name,'Submitted restored proposal required'
        proposal=frappe.get_doc('Payment Proposal',proposal_name)
        proposal.payments=[frappe._dict(name='isolated-proposal-row',payment_type='IBAN',
            amount=Decimal('0.10'),currency='CHF',execution_date='2026-11-14',
            iban='CH9300762011623852957',bic='',esr_participation_number='',
            esr_reference='',reference='ISOLATED TEST ONLY',is_salary=0,
            receiver='Synthetic & Co',receiver_address_line1='Testweg 1',
            receiver_pincode='8000',receiver_city='Zuerich',receiver_country='CH')]
        get_doc=frappe.get_doc
        def fixture_doc(doctype,*args,**kwargs):
            if doctype=='Payment Proposal' and args==(proposal_name,):return proposal
            return get_doc(doctype,*args,**kwargs)
        with patch.object(frappe,'get_doc',side_effect=fixture_doc),patch.object(api,'_debtor',return_value=dict(fixture,iban='CH9300762011623852957')):
            first=proposal.create_bank_file()
            with patch.object(api,'nowdate',return_value='2026-11-17'):
                second=proposal.create_bank_file()
            check(first['content']==second['content'] and second.get('reused'),
                  'native Swiss proposal re-download retains identical original across dates')
            check(first['message_id']==second['message_id'] and first['file_url']==second['file_url'],
                  'native proposal retains bank duplicate-detection identifiers and private URL')
            check(frappe.get_doc('File',{'file_url':first['file_url']}).is_private==1,
                  'native proposal original is private')
            proposal.payments[0].amount=Decimal('0.20')
            try:proposal.create_bank_file()
            except frappe.ValidationError:pass
            else:raise AssertionError('Changed proposal created second bank original')
            check(frappe.db.count('File',{'attached_to_doctype':'Payment Proposal',
                'attached_to_name':proposal_name,'file_name':['like','payments-proposal-%.xml']})==1,
                'changed native proposal cannot silently create another original')
    finally:
        frappe.db.rollback()
        assert accounting_digest()==before,'Accounting changed after rollback'
        assert before_counts=={kind:frappe.db.count(kind) for kind in before_counts},'Test records survived rollback'
        print('PASS full rollback preserves every existing accounting row and document count',flush=True)
        frappe.destroy()
    print(json.dumps(dict(success=True,checks=len(checks)+1,bank_calls=0,production_writes=0)))

if __name__=='__main__':main()
