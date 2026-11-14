import copy
import unittest
from scripts.vat_accounting_integrity import verify_template_changes


class VATIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.before = {
            'headers': {'Swiss': {'name': 'Swiss', 'tax_code': '', 'modified': 'old', 'modified_by': 'owner'},
                        'Other': {'name': 'Other', 'tax_code': '405', 'modified': 'old', 'modified_by': 'owner'}},
            'taxes': {'a': {'name': 'a', 'parent': 'Swiss', 'account_head': 'output', 'account_currency': None, 'rate': 8.1,
                            'modified': 'old', 'modified_by': 'owner'},
                      'b': {'name': 'b', 'parent': 'Other', 'account_head': 'input', 'account_currency': 'CHF', 'rate': 8.1,
                            'modified': 'old', 'modified_by': 'owner'}}}
        self.after = copy.deepcopy(self.before)
        self.after['headers']['Swiss'].update(tax_code='400', modified='new', modified_by='admin')
        self.after['taxes']['a'].update(account_head='input', account_currency='CHF', modified='new', modified_by='admin')
        self.changes = [{'doctype': 'Purchase Taxes and Charges Template', 'name': 'Swiss',
            'changes': [{'row': 'a', 'from_account': 'output', 'to_account': 'input',
                         'from_currency': None, 'to_currency': 'CHF'},
                        {'from_code': '', 'to_code': '400'}]}]

    def test_reviewed_future_template_fields_only(self):
        evidence = verify_template_changes(self.before, self.after, self.changes)
        self.assertEqual(len(evidence), 2)
        self.assertIn('account_head', evidence[1]['fields'])

    def test_tax_rate_change_is_rejected(self):
        self.after['taxes']['a']['rate'] = 2.6
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)

    def test_untouched_template_metadata_is_protected(self):
        self.after['headers']['Other']['modified'] = 'new'
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)

    def test_unreviewed_tax_row_is_protected(self):
        self.after['taxes']['b']['account_head'] = 'wrong'
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)

    def test_new_and_deleted_rows_are_rejected(self):
        self.after['taxes'].pop('b')
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)

    def test_stale_preview_is_rejected(self):
        self.changes[0]['changes'][0]['from_account'] = 'stale'
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)

    def test_unreviewed_currency_is_rejected(self):
        self.after['taxes']['a']['account_currency'] = 'EUR'
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)

    def test_stale_currency_preview_is_rejected(self):
        self.changes[0]['changes'][0]['from_currency'] = 'EUR'
        with self.assertRaises(AssertionError):
            verify_template_changes(self.before, self.after, self.changes)
