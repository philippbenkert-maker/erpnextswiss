#!/usr/bin/env python3
"""CAMT/EBICS parsing acceptance on an isolated full restore; no bank calls."""
import argparse
import os
import unittest
import frappe
from vat_accounting_integrity import accounting_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', required=True)
    args = parser.parse_args()
    assert args.site == 'kt-platform-test.local', 'Restored test site required'
    os.chdir('/home/frappe/frappe-bench/sites')
    frappe.init(site=args.site, sites_path=os.getcwd())
    assert frappe.conf.db_host == 'platform-restore-db', 'Isolated database required'
    frappe.connect()
    frappe.set_user('Administrator')
    frappe.flags.mute_emails = True
    before = accounting_digest()
    try:
        from erpnextswiss.erpnextswiss.tests.test_bank_matching_native import TestBankMatchingNative
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestBankMatchingNative)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        assert result.wasSuccessful(), 'Native bank import acceptance failed'
        frappe.db.rollback()
        assert accounting_digest() == before, 'Accounting tables changed during import acceptance'
        print(f'PASS {result.testsRun} native CAMT/EBICS checks; accounting tables unchanged', flush=True)
    finally:
        frappe.db.rollback()
        frappe.destroy()


if __name__ == '__main__':
    main()
