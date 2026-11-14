#!/usr/bin/env python3
"""Transactional Swiss VAT automation acceptance, restricted to a full restored site.

Private case JSON supplies the reviewed company, periods and expected accounting
amounts. No test fixture/customer data or filing is embedded in this script.
"""
import argparse
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
import frappe
from erpnextswiss.vat_setup import configure_company
from erpnextswiss.vat_automation import get_report
from erpnextswiss.vat_xml import build_xml, read_xml
from erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration import get_period_values, checked_xml
from erpnextswiss.erpnextswiss.report.kontrolle_mwst.kontrolle_mwst import get_data


def accounting_digest():
    tables = ('Account', 'Journal Entry', 'Journal Entry Account', 'GL Entry',
              'Sales Invoice', 'Sales Invoice Item', 'Sales Taxes and Charges',
              'Purchase Invoice', 'Purchase Invoice Item', 'Purchase Taxes and Charges',
              'Payment Entry', 'Payment Entry Reference', 'Payment Entry Deduction')
    return {table: hashlib.sha256(json.dumps(frappe.db.sql(
        f'SELECT * FROM `tab{table}` ORDER BY name', as_dict=True),
        sort_keys=True, default=str).encode()).hexdigest() for table in tables}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', required=True)
    parser.add_argument('--cases', required=True)
    args = parser.parse_args()
    assert args.site == 'kt-platform-test.local', 'Isolated test site required'
    os.chdir('/home/frappe/frappe-bench/sites')
    frappe.init(site=args.site, sites_path=os.getcwd())
    assert frappe.conf.db_host == 'platform-restore-db', 'Isolated database required'
    frappe.connect(); frappe.set_user('Administrator'); frappe.flags.mute_emails = True
    cases = json.loads(Path(args.cases).read_text())
    company = cases['company']
    checks = []
    before = accounting_digest()
    historical = {d.name: (d.as_dict(), checked_xml(d)) for d in
        [frappe.get_doc('VAT Declaration', name) for name in cases.get('originals', [])]}
    counts = {table: frappe.db.count(table) for table in ('VAT Declaration', 'Swiss VAT Settings', 'File')}
    frappe.db.savepoint('vat_automation_acceptance')

    def check(condition, message):
        assert condition, message
        checks.append(message); print('PASS ' + message, flush=True)

    def rejects(fn, message):
        try:
            fn()
        except (frappe.ValidationError, frappe.PermissionError):
            check(True, message)
        else:
            raise AssertionError(message)

    try:
        preview = configure_company(company)
        check(not preview['applied'], 'setup preview is read-only')
        check(accounting_digest() == before, 'preview preserves every accounting table')
        setup = configure_company(company, apply=True)
        check(setup['applied'], 'reviewed company mapping configured')
        second = configure_company(company, apply=True)
        check(second['existing_mapping_preserved'] and not second['future_template_changes'], 'repeated setup is idempotent')
        settings = frappe.get_doc('Swiss VAT Settings', company)
        check(bool(settings.enabled) and len(settings.accounts) >= 10, 'enabled explicit account mapping exists')
        for case in cases['periods']:
            report = get_report(company, case['start'], case['end'])
            repeated = get_report(company, case['start'], case['end'])
            check(report == repeated, 'repeat extraction is identical: ' + case['start'])
            for field, expected in case['values'].items():
                check(Decimal(str(report['values'][field])) == Decimal(str(expected)), 'ledger amount reconciled: ' + case['start'] + ' / ' + field)
            legacy = get_period_values(company, case['start'], case['end'], 'effective', '1')
            check(legacy == report['values'], 'existing values endpoint uses company mapping')
            raw = build_xml(dict(report['values'], company=company, start_date=case['start'], end_date=case['end'],
                vat_type='effective', reporting_basis='1'), frappe.db.get_value('Company', company, 'tax_id'))
            check(read_xml(raw)['values']['payable_tax'] == report['values']['payable_tax'], 'official-schema XML reconciles with extracted values')
            for box in ('200', '235', '400', '405'):
                evidence = get_data(case['start'], case['end'], box, company)
                expected_rows = [r for r in report['evidence'] if r['box'] == box]
                check(len(evidence) == len(expected_rows), 'audit report has same source allocation: ' + box)
        # Persist and submit a future empty period: stale client amounts must be
        # replaced by the actual ledger before the XML is saved, without posting.
        doc = frappe.get_doc(dict(doctype='VAT Declaration', title='Isolated automatic VAT acceptance 2098',
            company=company, start_date='2098-01-01', end_date='2098-03-31', vat_type='effective',
            reporting_basis='1', auto_from_ledger=1, total_revenue=1000, normal_amount_2024=1000,
            normal_rate_2024=8.1, pretax_material=10))
        doc.insert(); doc.submit(); doc.reload()
        check(doc.docstatus == 1 and doc.filing_status == 'XML ready', 'automatic submission archives validated XML')
        check(doc.total_revenue == 0 and doc.payable_tax == 0 and doc.pretax_material == 0, 'submission re-reads ledger instead of stale preview')
        check(len(doc.ledger_source_sha256) == 64 and bool(doc.ledger_control_summary), 'ledger fingerprint and reconciliation archived')
        check(read_xml(checked_xml(doc))['values']['payable_tax'] == 0, 'saved automatic XML has current ledger values')
        exported = doc.create_transfer_file(); repeated = doc.create_transfer_file()
        check(exported == repeated, 'automatic XML download remains idempotent')
        check(not doc.linked_journal_entry, 'automatic declaration creates no closing journal')
        rejects(lambda: get_report(company, '2026-01-01', '2026-03-31', reporting_basis='2'), 'unsupported cash basis fails clearly')
        rejects(lambda: get_report(company, '2026-01-01', '2026-03-31', vat_type='flat'), 'wrong method fails clearly')
        invalid = frappe.get_doc('Swiss VAT Settings', company)
        invalid.append('accounts', invalid.accounts[0].as_dict())
        rejects(invalid.validate, 'duplicate account mapping rejected')
        settings.enabled = 0; settings.save()
        rejects(lambda: get_report(company, '2026-01-01', '2026-03-31'), 'disabled configuration stops entire extraction')
        settings.enabled = 1; settings.save()
        frappe.set_user('Guest')
        rejects(lambda: get_report(company, '2026-01-01', '2026-03-31'), 'anonymous extraction rejected')
        rejects(lambda: configure_company(company, apply=True), 'anonymous configuration rejected')
        frappe.set_user('Administrator')
        for name, (snapshot, raw) in historical.items():
            current = frappe.get_doc('VAT Declaration', name)
            check(current.as_dict() == snapshot and checked_xml(current) == raw, 'filed original and exact XML unchanged: ' + name)
        check(accounting_digest() == before, 'all accounts, GL, journals, invoices, taxes and payments unchanged')
    finally:
        frappe.set_user('Administrator'); frappe.db.rollback(save_point='vat_automation_acceptance')
        assert accounting_digest() == before
        assert all(frappe.db.count(table) == count for table, count in counts.items())
        frappe.db.rollback(); frappe.destroy()
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'accounting_unchanged': True, 'all_test_changes_rolled_back': True}))


if __name__ == '__main__':
    main()
