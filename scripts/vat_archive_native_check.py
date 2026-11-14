#!/usr/bin/env python3
"""Native, transactional VAT acceptance on an isolated restored test site only.
Source records are supplied outside Git; no journal entry or external submission is made.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import frappe
from erpnextswiss.erpnextswiss.doctype.vat_declaration.vat_declaration import (
    import_existing_xml, inspect_existing_xml, checked_xml,
)
from erpnextswiss.vat_xml import read_xml


def digest_ledger():
    evidence = {}
    for table in ('Journal Entry', 'Journal Entry Account', 'GL Entry', 'Sales Invoice', 'Purchase Invoice'):
        rows = frappe.db.sql(f'SELECT * FROM `tab{table}` ORDER BY name', as_dict=True)
        evidence[table] = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', required=True)
    parser.add_argument('--sources', help='Private JSON list of file_name, journal_entry, sha256')
    args = parser.parse_args()
    if args.site != 'kt-platform-test.local':
        raise SystemExit('This test only runs on the isolated restored platform site')
    os.chdir('/home/frappe/frappe-bench/sites')
    frappe.init(site=args.site, sites_path=os.getcwd())
    assert frappe.conf.db_host == 'platform-restore-db'
    frappe.connect(); frappe.set_user('Administrator'); frappe.flags.mute_emails = True
    before = digest_ledger()
    counts = {d: frappe.db.count(d) for d in ('VAT Declaration', 'File')}
    frappe.db.savepoint('vat_acceptance')
    checks = []

    def check(condition, message):
        assert condition, message
        checks.append(message)
        print('PASS ' + message, flush=True)

    def rejects(function, message):
        try:
            function()
        except (frappe.ValidationError, frappe.PermissionError, frappe.DuplicateEntryError):
            check(True, message)
        else:
            raise AssertionError(message)

    try:
        sources = json.loads(Path(args.sources).read_text()) if args.sources else []
        company = None
        for source in sources:
            preview = inspect_existing_xml(source['file_name'])
            check(preview['sha256'] == source['sha256'], 'original XML checksum verified')
            first = import_existing_xml(source['file_name'], source['journal_entry'], source['sha256'], 'Isolated acceptance only')
            second = import_existing_xml(source['file_name'], source['journal_entry'], source['sha256'], 'Isolated acceptance only')
            check(first['name'] == second['name'], 'repeat import returns same declaration')
            doc = frappe.get_doc('VAT Declaration', first['name']); company = doc.company
            check(doc.docstatus == 1 and doc.filing_status == 'Filed (user confirmed)', 'historical submitted status and filing evidence')
            check(doc.filed_on is None, 'unknown external filing date remains blank')
            raw = checked_xml(doc)
            exported = doc.create_transfer_file()
            check(hashlib.sha256(exported['content'].encode()).hexdigest() == source['sha256'], 'download retains exact original bytes')
            check(read_xml(raw)['values']['payable_tax'] == read_xml(exported['content'].encode())['values']['payable_tax'], 'original tax amount retained')
            check(doc.linked_journal_entry == source['journal_entry'], 'existing closing entry linked')
            rejects(lambda: doc.cancel(), 'archived declaration cannot be cancelled')
            edited = frappe.get_doc('VAT Declaration', doc.name); edited.filing_status = 'XML ready'
            rejects(lambda: edited.save(), 'direct filing evidence modification blocked')
        company = company or frappe.db.get_value('Company', {'tax_id': ['like', 'CHE%']}, 'name')
        assert company

        def new_declaration(**updates):
            return frappe.get_doc(dict(doctype='VAT Declaration', title='VAT isolated acceptance 2097', company=company,
                start_date='2097-01-01', end_date='2097-03-31', vat_type='effective', auto_from_ledger=0,
                total_revenue=1000, normal_amount_2024=1000, normal_rate_2024=8.1, pretax_material=10, **updates))

        new = new_declaration(); new.insert(); new.submit()
        check(new.docstatus == 1 and new.filing_status == 'XML ready' and bool(new.xml_sha256), 'submission validates and archives XML atomically')
        check(float(new.payable_tax) == 71 and float(new.balance) == 0, 'native calculation reconciles Swiss tax cents')
        file_count = frappe.db.count('File')
        a = new.create_transfer_file(); b = new.create_transfer_file()
        check(a == b and frappe.db.count('File') == file_count, 'repeat download creates no new attachment')
        new.payable_tax = 9999
        check(new.create_transfer_file()['sha256'] == a['sha256'] and float(new.payable_tax) == 71, 'document API reloads authoritative submitted amounts')
        rejects(lambda: new_declaration().insert(), 'duplicate original period blocked')
        correction = frappe.copy_doc(new, ignore_no_copy=False); correction.title = 'VAT isolated explicit correction'; correction.submission_type = '2'; correction.corrects_declaration = new.name
        correction.insert(); correction.submit()
        check(correction.xml_sha256 != new.xml_sha256, 'explicit correction retains original and has its own XML')
        frappe.set_user('Guest')
        rejects(lambda: new.create_transfer_file(), 'anonymous XML export rejected')
        if sources:
            source = sources[0]
            rejects(lambda: import_existing_xml(source['file_name'], source['journal_entry'], source['sha256']), 'anonymous history import rejected')
        frappe.set_user('Administrator')
        check(digest_ledger() == before, 'all journals, GL entries and invoices byte-for-byte unchanged')
    finally:
        frappe.set_user('Administrator'); frappe.db.rollback(save_point='vat_acceptance')
        assert digest_ledger() == before
        assert all(frappe.db.count(d) == n for d, n in counts.items())
        frappe.db.rollback(); frappe.destroy()
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'all_test_changes_rolled_back': True}))

if __name__ == '__main__':
    main()
