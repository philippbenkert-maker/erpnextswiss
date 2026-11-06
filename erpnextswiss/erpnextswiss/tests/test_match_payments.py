"""Regression tests for truly unallocated payment assignment rows."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from erpnextswiss.erpnextswiss.page.match_payments import match_payments


class TestMatchPayments(TestCase):
    @staticmethod
    def _draft():
        return frappe._dict(
            name='ACC-PAY-DRAFT', company='KT Wärmesysteme AG', party='Bankimport Unbekannter Kunde',
            paid_amount=4229.43, received_amount=4229.43, unallocated_amount=4229.43,
            posting_date='2026-07-14', custom_remarks=0, remarks='Bank entry', reference_no='3200980681/1',
            paid_to='BANK', paid_from_account_currency='CHF', paid_to_account_currency='CHF')

    def test_submitted_assigned_base_reference_hides_legacy_suffix_draft(self):
        draft = self._draft()
        submitted = frappe._dict(
            name='ACC-PAY-SUBMITTED', paid_amount=4229.43, received_amount=4229.43,
            paid_from_account_currency='CHF', paid_to_account_currency='CHF',
            unallocated_amount=0)
        with (patch.object(match_payments.frappe.db, 'sql', return_value=[draft]),
              patch.object(match_payments.frappe, 'get_all', return_value=[submitted]) as lookup,
              patch.object(match_payments.frappe.db, 'exists', return_value='REFERENCE')):
            result = match_payments.get_unallocated_payment_entries()
        self.assertEqual(result, {'unallocated_payment_entries': []})
        self.assertEqual(set(lookup.call_args.kwargs['filters']['reference_no'][1]),
                         {'3200980681', '3200980681/1'})

    def test_unproven_or_ambiguous_draft_stays_visible(self):
        draft = self._draft()
        for matches, assigned in [([], False), ([frappe._dict(name='ONE')], False),
                                  ([frappe._dict(name='ONE'), frappe._dict(name='TWO')], True)]:
            with self.subTest(matches=len(matches), assigned=assigned):
                with (patch.object(match_payments.frappe.db, 'sql', return_value=[draft]),
                      patch.object(match_payments.frappe, 'get_all', return_value=matches),
                      patch.object(match_payments.frappe.db, 'exists', return_value=assigned)):
                    result = match_payments.get_unallocated_payment_entries()
                self.assertEqual(result, {'unallocated_payment_entries': [draft]})

    def test_marked_internal_bank_transfer_is_not_offered_for_customer_assignment(self):
        draft = self._draft()
        draft.custom_remarks = 1
        draft.remarks = (
            "Nicht zuzuweisende interne Übertragung vom AG-Kapitaleinzahlungskonto "
            "auf das operative Bankkonto."
        )
        with (patch.object(match_payments.frappe.db, 'sql', return_value=[draft]),
              patch.object(match_payments.frappe, 'get_all') as lookup):
            result = match_payments.get_unallocated_payment_entries()
        self.assertEqual(result, {'unallocated_payment_entries': []})
        lookup.assert_not_called()
