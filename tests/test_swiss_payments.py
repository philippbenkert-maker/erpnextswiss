"""Synthetic SPS tests; no ERP writes or bank connections."""
import ast
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import re
import unittest

from lxml import etree
from erpnextswiss.erpnextswiss import swiss_payments as bank
from erpnextswiss.erpnextswiss.iso20022 import is_qr_iban

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'erpnextswiss/erpnextswiss/page/payment_export/payment_export.py'
COMPANY = dict(name='Example & Co AG', street='Testweg', building='1', pincode='8000',
               city='Zuerich', country_code='CH', iban='CH9300762011623852957', bic='')

def row(**changes):
    value = dict(name='PE/2026/001', execution_date='2026-11-16', amount='123.45', currency='CHF',
                 iban=COMPANY['iban'], debtor=deepcopy(COMPANY), creditor=deepcopy(COMPANY), reference='Invoice <1> & 2')
    value.update(changes)
    return value

class SerializationTests(unittest.TestCase):
    def render(self, rows):
        result = bank.render_credit_transfers(rows, message_id='MSG-TEST-1',
            created_at=datetime(2026, 11, 14, 12, tzinfo=timezone.utc))
        root = etree.fromstring(result['content'].encode())
        bank._schema().assertValid(root)
        return result, root

    def test_decimal_sum_xml_escape_and_input_unchanged(self):
        rows = [row(amount='0.10'), row(name='PE2', amount='0.20')]
        before = deepcopy(rows)
        result, root = self.render(rows)
        self.assertEqual(result['total'], '0.30')
        self.assertEqual(root.findtext('.//{*}GrpHdr/{*}CtrlSum'), '0.30')
        self.assertEqual(root.findtext('.//{*}Cdtr/{*}Nm'), COMPANY['name'])
        self.assertEqual(root.findtext('.//{*}Ustrd'), 'Invoice <1> & 2')
        self.assertEqual(rows, before)
        self.assertEqual(result['count'], 2)

    def test_each_source_keeps_its_own_debtor(self):
        other = dict(COMPANY, iban='LI21088100002324013AA', name='Other account')
        _, root = self.render([row(), row(name='PE2', debtor=other)])
        self.assertEqual(root.xpath('//*[local-name()="DbtrAcct"]//*[local-name()="IBAN"]/text()'),
                         [COMPANY['iban'], other['iban']])
        identifiers = root.xpath('//*[local-name()="PmtInfId"]/text()')
        self.assertEqual(len(set(identifiers)), 2)
        self.assertTrue(all(len(value) <= 35 for value in identifiers))

    def test_reject_invalid_amounts_before_export(self):
        for value in (0, -1, 'NaN', 'Infinity', '1.001', '1000000000000000', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.render([row(amount=value)])

    def test_invalid_or_duplicate_sources(self):
        for values in ([], [row(), row()], [row(name='')], [row(iban='CH0000762011623852957')],
                       [row(currency='')], [row(service_level='ESR')], [row(execution_date='bad')]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.render(values)

    def test_missing_structured_addresses_rejected(self):
        for party in ('creditor', 'debtor'):
            for field in ('name', 'city', 'country_code'):
                with self.subTest(party=party, field=field), self.assertRaises(ValueError):
                    self.render([row(**{party: dict(COMPANY, **{field: ''})})])

    def test_does_not_silently_truncate_address(self):
        with self.assertRaises(ValueError):
            self.render([row(creditor=dict(COMPANY, city='x'*36))])

    def test_qr_iban_and_reference_are_bound(self):
        _, root = self.render([row(iban='CH4431999123000889012', qr_reference='210000000003139471430009017')])
        self.assertEqual(root.findtext('.//{*}CdOrPrtry/{*}Prtry'), 'QRR')
        for changes in (dict(iban='CH4431999123000889012', reference='missing'),
                        dict(iban='CH4431999123000889012', qr_reference='210000000003139471430009018'),
                        dict(service_level='QRR'),
                        dict(iban='CH4431999123000889012', currency='USD')):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.render([row(**changes)])
        self.assertTrue(is_qr_iban('LI4431999123000889012'))  # classification, not checksum validation
        self.assertFalse(is_qr_iban(COMPANY['iban']))

    def test_scor_iso_reference_checksum(self):
        _, root = self.render([row(service_level='SCOR', reference='RF18 5390 0754 7034')])
        self.assertEqual(root.findtext('.//{*}CdtrRefInf/{*}Tp/{*}Issr'), 'ISO')
        self.assertEqual(root.findtext('.//{*}CdtrRefInf/{*}Ref'), 'RF18539007547034')
        with self.assertRaises(ValueError): self.render([row(service_level='SCOR', reference='RF19539007547034')])

    def test_non_sepa_eur_needs_bank_but_sepa_does_not(self):
        for method in ('IBAN', 'SCOR'):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, 'BIC'):
                self.render([row(iban='DE89370400440532013000', currency='EUR', service_level=method,
                                 reference='RF18539007547034')])
        self.render([row(iban='DE89370400440532013000', currency='EUR', service_level='SEPA')])
        self.render([row(iban='DE89370400440532013000', currency='EUR', bic='COBADEFFXXX')])
        with self.assertRaises(ValueError): self.render([row(service_level='SEPA')])

    def test_salary_category_is_on_payment_level(self):
        _, root = self.render([row(is_salary=True)])
        self.assertEqual(root.findtext('.//{*}PmtInf/{*}PmtTpInf/{*}CtgyPurp/{*}Cd'), 'SALA')
        self.assertEqual(root.findtext('.//{*}DbtrAgt/{*}FinInstnId/{*}ClrSysMmbId/{*}MmbId'), '00762')

    def test_schema_validation_is_mandatory_and_errors_are_reviewable(self):
        with patch.object(bank, '_schema') as schema:
            schema.return_value = NS(assertValid=Mock(side_effect=etree.DocumentInvalid('invalid')))
            with self.assertRaisesRegex(ValueError, 'Swiss bank schema'): self.render([row()])


def load_adapter():
    tree = ast.parse(ADAPTER.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    for node in nodes: node.decorator_list = []
    frappe = NS(db=Mock(), throw=Mock(side_effect=lambda text: (_ for _ in ()).throw(ValueError(text))),
                get_doc=Mock(), get_all=Mock(), only_for=Mock(), parse_json=json.loads, utils=NS(escape_html=str))
    from hashlib import sha256
    from datetime import date
    ns = dict(frappe=frappe, _=lambda s:s, Decimal=Decimal, sha256=sha256, json=json, re=re,
              cint=lambda x:int(x or 0), getdate=lambda d: date.fromisoformat(str(d)[:10]),
              nowdate=lambda:'2026-11-14', payment_amount=bank.payment_amount,
              render_credit_transfers=bank.render_credit_transfers,
              normalize_iban=lambda x:''.join((x or '').split()).upper(), is_qr_iban=is_qr_iban)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ADAPTER), 'exec'), ns)
    return ns, frappe

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.frappe = load_adapter()
        self.events = []
        self.records = {name:NS(name=name, docstatus=0, payment_type='Pay', company='Company', received_amount='1.25',
                               check_permission=Mock(), submit=Mock(side_effect=lambda:self.events.append('submit')))
                        for name in ('PE-B','PE-A')}
        self.frappe.get_doc.side_effect = lambda kind,name:self.records[name]
        self.ns['_payment_row'] = lambda doc: row(name=doc.name, amount=doc.received_amount)
        self.ns['_archive'] = lambda result,*a: (self.events.append('archive') or dict(result,file_url='/private/files/test.xml'))

    def test_complete_preflight_then_archive_then_submit_and_sorted_locks(self):
        result = self.ns['_generate_payment_export'](['PE-B','PE-A','PE-B'], expected_total='2.50', expected_count=2)
        self.assertEqual(result['payment_entries'], ['PE-B','PE-A'])
        self.assertEqual([call.args[1] for call in self.frappe.db.sql.call_args_list], [('PE-A',),('PE-B',)])
        self.assertEqual(self.events, ['archive','submit','submit'])
        self.records['PE-A'].check_permission.assert_any_call('submit')

    def test_invalid_last_row_does_not_archive_or_submit_first(self):
        self.ns['_payment_row'] = lambda doc:row(name=doc.name, amount='invalid' if doc.name=='PE-A' else 1)
        with self.assertRaises(ValueError): self.ns['_generate_payment_export'](['PE-B','PE-A'])
        self.assertEqual(self.events, [])

    def test_previously_exported_or_foreign_company_not_submitted(self):
        for field,value in (('docstatus',1),('company','Other')):
            with self.subTest(field=field):
                old=getattr(self.records['PE-A'],field);setattr(self.records['PE-A'],field,value)
                with self.assertRaises(ValueError): self.ns['_generate_payment_export'](['PE-B','PE-A'])
                self.assertEqual(self.events,[]);setattr(self.records['PE-A'],field,old)

    def test_expected_values_checked_before_any_mutation(self):
        for values in (dict(expected_total='NaN'),dict(expected_total='3'),dict(expected_count='1.5')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.ns['_generate_payment_export'](['PE-B','PE-A'],**values)
        self.assertEqual(self.events,[])

    def test_archive_failure_rolls_back_without_submit(self):
        self.ns['_archive'] = Mock(side_effect=RuntimeError('storage unavailable'))
        with self.assertRaises(RuntimeError): self.ns['_generate_payment_export'](['PE-B'])
        self.frappe.db.rollback.assert_called_once_with(save_point='swiss_payment_export')
        self.assertEqual(self.events,[])

    def test_second_submit_failure_rolls_back_first(self):
        self.records['PE-A'].submit.side_effect=RuntimeError('posting rejected')
        with self.assertRaises(RuntimeError): self.ns['_generate_payment_export'](['PE-B','PE-A'])
        self.frappe.db.rollback.assert_called_once_with(save_point='swiss_payment_export')
        self.assertEqual(self.events,['archive','submit'])

    def payroll(self):
        payroll=NS(name='PAYROLL-ONE',company='Company',payment_account='Bank',employees=[NS(employee='EMP1')],
                   start_date='2026-11-01',end_date='2026-11-30')
        slip=NS(name='SAL1',payroll_entry=payroll.name,company='Company',employee='EMP1',employee_name='Test Person',
                docstatus=1,start_date=payroll.start_date,end_date=payroll.end_date,posting_date='2026-11-14',
                net_pay=123.45,rounded_total=123,currency='CHF',bank_account_no=COMPANY['iban'],check_permission=Mock())
        self.frappe.get_all.return_value=[NS(name=slip.name)]
        self.frappe.get_doc.side_effect=lambda *a:slip
        self.frappe.db.get_single_value.return_value=0
        self.frappe.db.get_value.return_value='TESTBIC1'
        self.ns['_debtor']=lambda *a:COMPANY
        self.ns['_party_address']=lambda *a:COMPANY
        return payroll,slip

    def test_payroll_is_exact_run_not_all_slips_in_period(self):
        payroll,slip=self.payroll();rows=self.ns['_payroll_rows'](payroll)
        options=self.frappe.get_all.call_args.kwargs
        self.assertEqual(options['filters']['payroll_entry'],payroll.name)
        self.assertEqual(options['filters']['company'],payroll.company)
        self.assertEqual(options['limit_page_length'],0)
        self.assertEqual(rows[0]['amount'],123)
        self.assertEqual(rows[0]['bic'],'TESTBIC1')
        self.frappe.db.get_value.assert_called_with('Employee','EMP1','bic')
        self.frappe.db.get_single_value.return_value=1
        self.assertEqual(self.ns['_payroll_rows'](payroll)[0]['amount'],123.45)

    def test_payroll_rejects_drafts_other_run_company_employee_or_period(self):
        payroll,slip=self.payroll()
        for field,value in (('docstatus',0),('payroll_entry','OTHER'),('company','OTHER'),('employee','OTHER'),('end_date','2026-11-29')):
            with self.subTest(field=field):
                old=getattr(slip,field);setattr(slip,field,value)
                with self.assertRaises(ValueError):self.ns['_payroll_rows'](payroll)
                setattr(slip,field,old)
        payroll.employees.append(NS(employee='EMP2'))
        with self.assertRaisesRegex(ValueError,'every employee'):self.ns['_payroll_rows'](payroll)

    def test_payroll_repeat_reuses_original_instead_of_new_bank_message(self):
        payroll,_=self.payroll();payroll.docstatus=1;payroll.check_permission=Mock()
        self.ns['_payroll_rows']=lambda p:[row(is_salary=True)]
        saved={}
        def archive(result,kind,names,filename):
            saved.update(name='FILE1',file_name=filename,file_url='/private/files/'+filename,content=result['content'])
            return dict(result,file_url=saved['file_url'])
        self.ns['_archive']=archive
        self.frappe.get_all.side_effect=lambda *a,**k: [NS(**saved)] if saved else []
        self.frappe.get_doc.side_effect=lambda kind,name:payroll if kind=='Payroll Entry' else NS(get_content=lambda:saved['content'])
        first=self.ns['generate_payment_file_from_payroll']('PAYROLL-ONE')
        self.ns['_payroll_rows']=lambda p:[row(is_salary=True,execution_date='2026-11-17')]
        second=self.ns['generate_payment_file_from_payroll']('PAYROLL-ONE')
        self.assertTrue(second['reused']);self.assertEqual(first['content'],second['content'])
        self.assertEqual(first['message_id'],second['message_id'])
        self.ns['_payroll_rows']=lambda p:[row(is_salary=True,amount='999')]
        with self.assertRaisesRegex(ValueError,'different source data'):self.ns['generate_payment_file_from_payroll']('PAYROLL-ONE')

class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.ns,self.frappe=load_adapter()
        self.ns['_debtor']=lambda *a:COMPANY
        self.ns['_country_code']=lambda x:x.upper()
        payment=NS(name='ROW1',payment_type='IBAN',iban=COMPANY['iban'],esr_participation_number='',
            receiver_address_line1='Testweg 1',receiver='Creditor & Co',receiver_pincode='8000',receiver_city='Zuerich',
            receiver_country='CH',amount='0.10',currency='CHF',execution_date='2026-11-16',bic='',reference='REF1',
            esr_reference='',is_salary=0)
        self.proposal=NS(name='PP1',docstatus=1,company='Company',pay_from_account='Bank',payments=[payment],check_permission=Mock())
        self.frappe.get_doc.side_effect=lambda *a:self.proposal
        self.captured=[]
        self.ns['_immutable_payment_export']=lambda rows,*a,**k:self.captured.extend(rows) or bank.render_credit_transfers(rows)

    def test_current_swiss_proposal_preserves_structured_data_and_exact_amount(self):
        result=self.ns['generate_swiss_proposal_file']('PP1')
        self.assertEqual(result['total'],'0.10')
        self.assertEqual(self.captured[0]['creditor']['name'],'Creditor & Co')
        self.assertEqual(self.captured[0]['creditor']['building'],'1')
        self.assertEqual(self.captured[0]['name'],'ROW1')
        self.frappe.db.sql.assert_called_once_with('SELECT name FROM `tabPayment Proposal` WHERE name=%s FOR UPDATE',('PP1',))
        self.proposal.check_permission.assert_called_with('write')

    def test_draft_or_obsolete_esr_never_exports(self):
        self.proposal.docstatus=0
        with self.assertRaisesRegex(ValueError,'submitted'):self.ns['generate_swiss_proposal_file']('PP1')
        self.assertEqual(self.captured,[])
        self.proposal.docstatus=1;self.proposal.payments[0].payment_type='ESR'
        with self.assertRaisesRegex(ValueError,'Legacy ESR'):self.ns['generate_swiss_proposal_file']('PP1')
        self.assertEqual(self.captured,[])

    def test_swiss_method_routes_to_shared_exporter(self):
        path=ROOT/'erpnextswiss/erpnextswiss/doctype/payment_proposal/payment_proposal.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        klass=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='PaymentProposal')
        method=next(n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name=='create_bank_file')
        self.assertIn('generate_swiss_proposal_file(self.name)',ast.unparse(method))
        self.assertIn("settings.get('banking_region') == 'CH'",ast.unparse(method))

class AddressSecurityTests(unittest.TestCase):
    def test_owner_permission_and_sql_parameters(self):
        path=ROOT/'erpnextswiss/erpnextswiss/common_functions.py'
        node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='get_primary_address')
        node.decorator_list=[]
        frappe=NS(db=NS(sql=Mock(return_value=[])),get_doc=Mock(),throw=Mock(side_effect=ValueError))
        ns=dict(frappe=frappe,_=lambda s:s)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
        hostile="x' OR 1=1 --"
        self.assertIsNone(ns['get_primary_address'](hostile,'Supplier'))
        query,parameters=frappe.db.sql.call_args.args
        self.assertNotIn(hostile,query);self.assertEqual(parameters,('Supplier',hostile))
        frappe.get_doc.return_value.check_permission.assert_called_once_with('read')
        with self.assertRaises(ValueError):ns['get_primary_address']('x','User')

if __name__=='__main__': unittest.main()
