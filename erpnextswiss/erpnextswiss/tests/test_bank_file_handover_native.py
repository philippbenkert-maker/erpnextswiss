"""Native DB, File and permission tests using synthetic, rollback-owned bank data."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.core.doctype.file.utils import find_file_by_url
from frappe.core.doctype.file.file import File
from frappe.utils import CallbackManager
from frappe.utils.file_manager import save_file
from lxml import etree

from erpnextswiss.erpnextswiss import ebics_automation
from erpnextswiss.erpnextswiss.tests import test_bank_matching_native as matching_tests
from erpnextswiss.erpnextswiss.tests.test_bank_camt_preview import detailed_xml
from erpnextswiss.erpnextswiss.tests.test_bank_file_admission import PROFILE, xml, zipped
from erpnextswiss.scripts import bank_file_handover as handover
from erpnextswiss.scripts.bank_file_admission import BankFileError


class TestBankFileHandoverNative(unittest.TestCase):
    row = matching_tests.TestBankMatchingNative.row

    def setUp(self):
        self.callbacks = {name: getattr(frappe.db, name) for name in (
            'before_commit', 'after_commit', 'before_rollback', 'after_rollback')}
        for name in self.callbacks:
            setattr(frappe.db, name, CallbackManager())
        matching_tests.TestBankMatchingNative.setUp(self)
        self.connection = 'KT-HANDOVER-' + self.key
        self.row('ebics Connection', self.connection, title=self.connection, company=self.company_a,
                 ebics_version='H005', scope='CH', enable_sync=0)
        self.payload = zipped([('original.xml', xml(entries=True))])

    def cleanup_rows(self):
        frappe.set_user('Administrator')
        try:
            frappe.db.before_rollback.run()
            frappe.db.rollback(save_point=self.savepoint)
            frappe.db.after_rollback.run()
        finally:
            for name, manager in self.callbacks.items():
                setattr(frappe.db, name, manager)
            frappe.set_user(self.original_user)

    def stage(self, payload=None, source_reference='source-1', accounts=None, profile=PROFILE):
        return handover.stage_bank_archive(self.payload if payload is None else payload, profile,
                                           connection=self.connection, accounts=accounts or [self.account_a],
                                           source_reference=source_reference)

    def manager(self):
        user = 'kt-handover-' + self.key + '@example.invalid'
        frappe.get_doc({'doctype': 'User', 'email': user, 'first_name': 'Synthetic handover',
                        'enabled': 1, 'send_welcome_email': 0, 'user_type': 'System User',
                        'roles': [{'role': 'Accounts Manager'}]}).insert()
        return user

    def disk_files(self):
        root = Path(frappe.get_site_path('private', 'files'))
        return {path.resolve() for path in root.iterdir() if path.is_file()} if root.exists() else set()

    def test_stages_original_without_legacy_payment_or_bank_side_effects(self):
        before = self.disk_files()
        insert = Document.insert
        def controlled_insert(document, *args, **kwargs):
            self.assertIn(document.doctype, ('Bank File Handover', 'File', 'Version', 'Comment'))
            return insert(document, *args, **kwargs)
        with patch.object(Document, 'insert', controlled_insert), \
                patch.object(Document, 'submit', side_effect=AssertionError('No submit')), \
                patch.object(frappe.db, 'commit', side_effect=AssertionError('Caller owns commit')):
            result = self.stage()
        self.assertTrue(result['commit_required'])
        self.assertFalse(result['import_approved'])
        self.assertEqual(result['status'], 'Staged')
        document = frappe.get_doc(handover.DOCTYPE, result['name'])
        self.assertEqual(document.statement_count, 1)
        self.assertEqual(document.entry_count, 1)
        self.assertEqual(handover._original(document), self.payload)
        source = frappe.get_doc('File', document.original_file)
        self.assertEqual(source.is_private, 1)
        self.assertEqual(source.attached_to_name, document.name)
        self.assertEqual(self.disk_files() - before, {Path(source.get_full_path()).resolve()})
        self.assertEqual(handover.get_handover_preview(document.name)['archive'].files[0].content,
                         xml(entries=True))

    def test_replay_retains_one_original_and_all_distinct_source_receipts(self):
        first = self.stage()
        original = frappe.db.get_value(handover.DOCTYPE, first['name'], 'original_file')
        for _ in range(5):
            repeated = self.stage()
            self.assertEqual(repeated['name'], first['name'])
            self.assertEqual(repeated['receipt_count'], 1)
            self.assertTrue(repeated['replayed'])
        second = self.stage(source_reference='another-request')
        self.assertEqual(second['name'], first['name'])
        self.assertEqual(second['receipt_count'], 2)
        self.assertFalse(second['replayed'])
        self.assertEqual(frappe.db.get_value(handover.DOCTYPE, first['name'], 'original_file'), original)
        self.assertEqual(frappe.db.count('File', {'attached_to_doctype': handover.DOCTYPE,
                                                'attached_to_name': first['name']}), 1)

    def test_multi_invoice_payment_remains_exact_after_original_handover_and_replay(self):
        supplier = 'KT-MULTI-SUPPLIER-' + self.key
        self.row('Supplier', supplier, supplier_name=supplier, disabled=0)
        invoices = {}
        references = []
        for index, amount in enumerate(('12.34', '7.66'), start=1):
            reference = f'KT-MULTI-{self.key}-{index}'
            name = f'KT-PINV-{self.key}-{index}'
            self.row('Purchase Invoice', name, company=self.company_a, docstatus=1, currency='CHF',
                     supplier=supplier, outstanding_amount=amount, grand_total=amount, bill_no=reference)
            references.append(reference)
            invoices[name] = Decimal(amount)
        self.row('Purchase Invoice', f'KT-PINV-FOREIGN-{self.key}', company=self.company_b,
                 docstatus=1, currency='CHF', supplier=supplier, outstanding_amount='12.34',
                 grand_total='12.34', bill_no=references[0])

        root = etree.fromstring(detailed_xml(amounts=('20.00',), remark=' '.join(references),
                                             reference=f'KT-MULTI-BANK-{self.key}'))
        ns = {'b': root.nsmap[None]}
        entry_xml = root.find('.//b:Ntry', ns)
        entry_xml.find('b:CdtDbtInd', ns).text = 'DBIT'
        creditor = entry_xml.find('.//b:Dbtr', ns)
        creditor.tag = '{' + ns['b'] + '}Cdtr'
        creditor.find('b:Pty/b:Nm', ns).text = supplier
        closing = root.find('.//b:Stmt/b:Bal', ns)
        opening = deepcopy(closing)
        opening.find('b:Tp/b:CdOrPrtry/b:Cd', ns).text = 'OPBD'
        opening.find('b:Amt', ns).text = '1254.00'
        closing.addprevious(opening)
        payload = zipped([('two-invoices.xml', etree.tostring(root, encoding='UTF-8', xml_declaration=True))])
        frappe.db.set_single_value('ERPNextSwiss Settings', 'always_use_entry_transaction_type', 1)
        frappe.db.set_single_value('ERPNextSwiss Settings', 'always_use_entry_amount', 0)
        protected = ('Purchase Invoice', 'Payment Entry', 'Bank Transaction', 'GL Entry')
        counts = {doctype: frappe.db.count(doctype) for doctype in protected}

        first = self.stage(payload, source_reference='two-invoice-payment')
        self.assertFalse(first['replayed'])
        self.assertFalse(first['import_approved'])
        document = frappe.get_doc(handover.DOCTYPE, first['name'])
        connection = frappe.get_doc('ebics Connection', self.connection)
        for attempt in range(2):
            if attempt:
                replay = self.stage(payload, source_reference='two-invoice-payment')
                self.assertEqual((replay['name'], replay['receipt_count']), (first['name'], 1))
                self.assertTrue(replay['replayed'])
            preview = handover.get_handover_preview(first['name'])
            statement, = preview['statements']
            entry, = statement['entries']
            self.assertEqual(statement['balance_check'], 'matched')
            match = ebics_automation._strict_invoice_candidates(connection, statement, entry)
            self.assertIsNotNone(match)
            _, doctype, records, amount = match
            self.assertEqual((doctype, amount), ('Purchase Invoice', Decimal('20.00')))
            self.assertEqual({invoice.name: Decimal(str(invoice.outstanding_amount)) for invoice in records}, invoices)
        self.assertEqual(handover._original(document), payload)
        self.assertEqual(frappe.db.count('File', {'attached_to_doctype': handover.DOCTYPE,
                                                'attached_to_name': first['name']}), 1)
        self.assertEqual({doctype: frappe.db.count(doctype) for doctype in protected}, counts)

    def test_source_reference_cannot_be_rebound_to_changed_original_or_account_scope(self):
        first = self.stage()
        with self.assertRaises(BankFileError):
            self.stage(zipped([('original.xml', xml(entries=False))]))
        frappe.db.set_value('Account', self.account_other, 'iban', 'CH5604835012345678009')
        with self.assertRaises(BankFileError):
            self.stage(accounts=[self.account_a, self.account_other])
        self.assertEqual(frappe.db.count(handover.DOCTYPE, {'connection': self.connection}), 1)
        self.assertEqual(len(frappe.get_doc(handover.DOCTYPE, first['name']).receipts), 1)

    def test_utf16_multiple_units_and_balance_only_original_survive_staging(self):
        for index, profile in enumerate(('camt.053.001.08', 'camt.054.001.08')):
            content = xml(profile, multiple=True).decode().replace('UTF-8', 'UTF-16').encode('utf-16')
            payload = zipped([('original.xml', content)])
            result = self.stage(payload, 'multi-' + str(index), profile=profile)
            preview = handover.get_handover_preview(result['name'])
            self.assertEqual(preview['archive'].files[0].content, content)
            self.assertEqual(len(preview['statements']), 2)
            self.assertEqual(frappe.db.get_value(handover.DOCTYPE, result['name'], 'entry_count'), 0)

    def test_bank_original_stays_binary_even_if_content_detection_returns_unknown(self):
        from erpnextswiss.erpnextswiss.tests.bank_handover_process_acceptance import _payload

        payload = _payload()
        result = self.stage(payload)
        document = frappe.get_doc(handover.DOCTYPE, result['name'])
        source = frappe.get_doc('File', document.original_file)
        self.assertEqual(source.get_content(), payload)
        # Simulate a native binary-classification miss without depending on
        # the ZIP signatures or default encodings of a Frappe point release.
        with patch('frappe.core.doctype.file.file.filetype.guess', return_value=None):
            native = File.get_content(source, encodings=('latin-1',))
            self.assertIsInstance(native, str)
            self.assertNotEqual(native.encode(), payload)
            self.assertEqual(source.get_content(encodings=('latin-1',)), payload)
        self.assertEqual(handover._original(document), payload)
        self.assertTrue(self.stage(payload)['replayed'])

    def test_failure_after_file_write_rolls_back_only_local_rows_and_callbacks(self):
        paths, callbacks = [], []
        before = self.disk_files()
        frappe.db.after_commit.add(lambda: callbacks.append('outer'))
        insert = Document.insert
        def observed_insert(document, *args, **kwargs):
            source = insert(document, *args, **kwargs)
            if document.doctype == 'File':
                paths.append(Path(source.get_full_path()))
                frappe.db.after_commit.add(lambda: callbacks.append('failed-inner'))
            return source
        with patch.object(Document, 'insert', observed_insert), \
                patch.object(handover, '_original', side_effect=BankFileError('Injected readback failure')):
            with self.assertRaises(BankFileError):
                self.stage()
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertEqual(self.disk_files(), before)
        self.assertEqual(frappe.db.count(handover.DOCTYPE, {'connection': self.connection}), 0)
        self.assertTrue(frappe.db.exists('ebics Connection', self.connection))
        frappe.db.after_commit.run()
        self.assertEqual(callbacks, ['outer'])
        self.assertFalse(handover._controlled_write.get())
        self.assertEqual(self.stage()['receipt_count'], 1)

    def test_failure_during_receipt_save_does_not_leave_a_staged_file_or_receipt(self):
        save = Document.save
        def fail_after_file(document, *args, **kwargs):
            if document.doctype == handover.DOCTYPE and document.original_file:
                raise BankFileError('Injected receipt persistence failure')
            return save(document, *args, **kwargs)
        with patch.object(Document, 'save', fail_after_file):
            with self.assertRaises(BankFileError):
                self.stage()
        self.assertEqual(frappe.db.count(handover.DOCTYPE, {'connection': self.connection}), 0)
        self.assertEqual(frappe.db.count('Bank Handover Receipt', {'receipt_key': handover._digest([
            self.connection, 'source-1'])}), 0)
        self.assertEqual(self.stage()['receipt_count'], 1)

    def test_changed_original_bytes_are_rejected_by_preview_and_replay(self):
        result = self.stage()
        document = frappe.get_doc(handover.DOCTYPE, result['name'])
        path = Path(frappe.get_doc('File', document.original_file).get_full_path())
        original = path.read_bytes()
        try:
            path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            with self.assertRaises(BankFileError):
                handover.get_handover_preview(document.name)
            with self.assertRaises(BankFileError):
                self.stage()
        finally:
            path.write_bytes(original)
        self.assertTrue(self.stage()['replayed'])

    def test_generic_document_writes_and_content_scope_edits_are_rejected(self):
        result = self.stage()
        document = frappe.get_doc(handover.DOCTYPE, result['name'])
        with self.assertRaises(frappe.PermissionError):
            document.save(ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(handover.DOCTYPE, document.name, ignore_permissions=True)
        frappe.db.set_value(handover.DOCTYPE, document.name, 'archive_sha256', 'f' * 64)
        with self.assertRaises(BankFileError):
            handover.get_handover_preview(document.name)

    def test_missing_or_foreign_account_and_legacy_sync_fail_without_partial_records(self):
        for accounts in ([self.account_b], [self.account_a, self.account_a]):
            with self.assertRaises(BankFileError):
                self.stage(accounts=accounts)
        frappe.db.set_value('ebics Connection', self.connection, 'enable_sync', 1)
        with self.assertRaises(BankFileError):
            self.stage()
        self.assertEqual(frappe.db.count(handover.DOCTYPE, {'connection': self.connection}), 0)

    def test_revoked_account_blocks_owner_file_and_native_parent_and_file_lists(self):
        manager = self.manager()
        frappe.set_user(manager)
        first = self.stage()
        document = frappe.get_doc(handover.DOCTYPE, first['name'])
        source = frappe.get_doc('File', document.original_file)
        self.assertEqual(source.owner, manager)
        self.assertTrue(frappe.has_permission('File', 'read', doc=source))
        self.assertTrue(source.is_downloadable())
        self.assertIsNotNone(find_file_by_url(source.file_url, name=source.name))
        self.assertEqual(frappe.get_list(handover.DOCTYPE, filters={'name': document.name}, pluck='name'),
                         [document.name])
        frappe.set_user('Administrator')
        frappe.db.set_value('Account', self.account_a, 'disabled', 1)
        frappe.set_user(manager)
        self.assertFalse(frappe.has_permission('File', 'read', doc=source))
        self.assertFalse(source.is_downloadable())
        self.assertIsNone(find_file_by_url(source.file_url, name=source.name))
        self.assertIsNone(find_file_by_url(source.file_url))
        with self.assertRaises(frappe.PermissionError):
            source.get_content()
        self.assertFalse(frappe.has_permission(handover.DOCTYPE, 'read', doc=document))
        self.assertEqual(frappe.get_list(handover.DOCTYPE, filters={'name': document.name}, pluck='name'), [])
        self.assertEqual(frappe.get_list('File', filters={'name': source.name}, pluck='name'), [])
        with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
            handover.get_handover_preview(document.name)

    def test_every_account_in_multiscope_handover_must_remain_authorized(self):
        manager = self.manager()
        frappe.db.set_value('Account', self.account_other, 'iban', 'CH5604835012345678009')
        frappe.set_user(manager)
        first = self.stage(accounts=[self.account_a, self.account_other])
        document = frappe.get_doc(handover.DOCTYPE, first['name'])
        frappe.set_user('Administrator')
        frappe.db.set_value('Account', self.account_other, 'disabled', 1)
        frappe.set_user(manager)
        self.assertFalse(frappe.has_permission(handover.DOCTYPE, 'read', doc=document))
        self.assertEqual(frappe.get_list(handover.DOCTYPE, filters={'name': document.name}, pluck='name'), [])

    def test_unrelated_file_permissions_are_not_changed_by_the_banking_guard(self):
        source = save_file('unrelated-' + self.key + '.txt', b'Unrelated synthetic public file', None, None,
                           is_private=0)
        source.file_name = 'Still an ordinary attachment.txt'
        source.save()
        get_hooks = frappe.get_hooks
        def baseline_hooks(hook=None, *args, **kwargs):
            result = get_hooks(hook, *args, **kwargs)
            if hook == 'has_permission':
                result = dict(result)
                result['File'] = [entry for entry in result.get('File', [])
                                  if entry != 'erpnextswiss.scripts.bank_file_handover.file_has_permission']
            return result
        manager = self.manager()
        for user in ('Guest', manager, 'Administrator'):
            frappe.set_user(user)
            with patch.object(frappe, 'get_hooks', side_effect=baseline_hooks):
                before = {action: frappe.has_permission('File', action, doc=source)
                          for action in ('read', 'write', 'delete', 'share')}
            after = {action: frappe.has_permission('File', action, doc=source)
                     for action in ('read', 'write', 'delete', 'share')}
            self.assertEqual(after, before, user)
            self.assertEqual(source.is_downloadable(), File.is_downloadable(source), user)
        frappe.set_user('Guest')
        # Public bytes can be downloaded without granting Guest ERP File access.
        self.assertTrue(source.is_downloadable())
        with self.assertRaises(frappe.PermissionError):
            self.stage()

    def test_original_cannot_be_detached_published_or_copied_by_its_owner(self):
        manager = self.manager()
        frappe.set_user(manager)
        result = self.stage()
        original = frappe.db.get_value(handover.DOCTYPE, result['name'], 'original_file')
        for changes in ({'attached_to_doctype': None, 'attached_to_name': None}, {'is_private': 0}):
            source = frappe.get_doc('File', original)
            source.update(changes)
            with self.assertRaises(frappe.PermissionError):
                source.save(ignore_permissions=True)
        source = frappe.get_doc('File', original)
        with self.assertRaises(frappe.PermissionError):
            source.create_attachment_copy('User', manager, ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc('File', source.name, ignore_permissions=True)
        self.assertEqual(handover._original(frappe.get_doc(handover.DOCTYPE, result['name'])), self.payload)

    def test_file_lists_work_during_install_and_when_account_read_permission_is_missing(self):
        original = save_file('normal-' + self.key + '.txt', b'Ordinary attachment', None, None, is_private=1)
        with patch.object(frappe.db, 'table_exists', return_value=False):
            condition = handover.file_permission_query_conditions()
            self.assertNotIn('SELECT', condition)
            self.assertTrue(frappe.db.sql('SELECT name FROM `tabFile` WHERE name=%s AND ' + condition,
                                         original.name))
        frappe.set_user(self.manager())
        get_list = frappe.get_list
        def restricted(doctype, *args, **kwargs):
            if doctype == 'Account':
                raise frappe.PermissionError('Synthetic role restriction')
            return get_list(doctype, *args, **kwargs)
        with patch.object(frappe, 'get_list', side_effect=restricted):
            condition = handover.file_permission_query_conditions()
            self.assertTrue(frappe.db.sql('SELECT name FROM `tabFile` WHERE name=%s AND ' + condition,
                                         original.name))
            self.assertEqual(handover.permission_query_conditions(), '1=0')

    def test_original_size_metadata_cannot_bypass_archive_limit(self):
        result = self.stage()
        document = frappe.get_doc(handover.DOCTYPE, result['name'])
        for value in (0, -1, handover.MAX_ARCHIVE_BYTES + 1):
            document.byte_count = value
            with self.assertRaises(BankFileError):
                handover._original(document)

    def test_accounting_reader_can_preview_but_cannot_stage(self):
        first = self.stage()
        user = self.manager()
        frappe.get_doc('User', user).remove_roles('Accounts Manager')
        frappe.get_doc('User', user).add_roles('Accounts User')
        frappe.set_user(user)
        self.assertTrue(handover.get_handover_preview(first['name'])['read_only'])
        with self.assertRaises(frappe.PermissionError):
            self.stage(source_reference='reader-cannot-write')

    def test_new_permission_hooks_do_not_whitelist_handover_or_payment_methods(self):
        self.assertNotIn(handover.stage_bank_archive, frappe.whitelisted)
        self.assertNotIn(handover.receive_bank_archive, frappe.whitelisted)
        self.assertNotIn(handover.get_handover_preview, frappe.whitelisted)
        for value in ('', 'https://example.invalid/', 'line\nbreak', 'x' * 129):
            with self.assertRaises(BankFileError):
                self.stage(source_reference=value)

    def test_dedicated_receiver_rejects_pending_caller_writes_without_committing_or_rolling_back(self):
        with patch.object(frappe.db, 'commit', side_effect=AssertionError('No outer commit')), \
                patch.object(frappe.db, 'rollback', side_effect=AssertionError('No outer rollback')):
            with self.assertRaises(BankFileError):
                handover.receive_bank_archive(self.payload, PROFILE, connection=self.connection,
                                               accounts=[self.account_a], source_reference='pending-caller')
        self.assertTrue(frappe.db.exists('ebics Connection', self.connection))
