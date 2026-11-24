"""Dedicated real SQL/ORM cases; excluded from generic test-site discovery.

Run only on the disposable restore through scripts/ebics_movement_native_check.py.
The separate concurrency phase requires committed synthetic fixtures there.
"""

from copy import deepcopy
import unittest
from uuid import uuid4

import frappe

from erpnextswiss.erpnextswiss import ebics_automation as auto
from erpnextswiss.scripts.bank_file_admission import BankFileError


def fixture(key):
    """Minimal SQL setup; the system under test uses real native insert/submit."""
    names = frappe._dict(company="KT-MIRROR-C-" + key, account="KT-MIRROR-A-" + key,
                         bank="KT-MIRROR-B-" + key, bank_account="KT-MIRROR-BA-" + key,
                         source="KT-MIRROR-S-" + key)

    def row(doctype, name, **values):
        values = {"name": name, "owner": "Administrator", **values}
        # Identifiers are static test code; all values remain parameterized.
        frappe.db.sql("INSERT INTO `tab" + doctype + "` ("
                      + ",".join("`" + field + "`" for field in values) + ") VALUES ("
                      + ",".join(["%s"] * len(values)) + ")", tuple(values.values()))

    row("Company", names.company, company_name=names.company, abbr=key,
        default_currency="EUR", country="Switzerland")
    row("Account", names.account, account_name=names.account, company=names.company,
        account_type="Bank", root_type="Asset", report_type="Balance Sheet",
        is_group=0, disabled=0, account_currency="EUR")
    row("Bank", names.bank, bank_name=names.bank)
    row("Bank Account", names.bank_account, account=names.account, company=names.company,
        bank=names.bank, is_company_account=1, account_name=names.bank_account)
    row("EBICS Download", names.source, company=names.company, ack_state="Confirmed")
    return names


def movement(names, *, enriched=False):
    connection = frappe._dict(company=names.company)
    statement = dict(account=names.account, currency="EUR")
    refs = {"account_service_reference": ["SYNTHETIC-" + names.source]}
    if enriched:
        refs["uetr"] = ["UETR-" + names.source]
    entry = dict(issues=[], status="BOOK", amount="12.34", credit_debit="DBIT",
                 booking_date="2026-11-17", value_date="2026-11-17", detail_count=1,
                 reference=refs["account_service_reference"][0], bank_reference_ids=refs,
                 candidates=[], xml="<Ntry/>")
    return connection, statement, entry


class TestEbicsMovementNative(unittest.TestCase):
    def setUp(self):
        if not (frappe.local.site == "kt-platform-test.local"
                and frappe.conf.db_host == "platform-restore-db" and frappe.conf.pause_scheduler):
            raise RuntimeError("Disposable paused restore required for native mirror acceptance")
        self.user = frappe.session.user
        frappe.set_user("Administrator")
        self.savepoint = "mirror_" + uuid4().hex[:12]
        frappe.db.savepoint(self.savepoint)
        self.addCleanup(self.cleanup)
        self.names = fixture(uuid4().hex[:12])

    def cleanup(self):
        try:
            frappe.db.rollback(save_point=self.savepoint)
        finally:
            frappe.set_user(self.user)

    def apply(self, *, enriched=False, plan=None, entry=None):
        connection, statement, original = movement(self.names, enriched=enriched)
        entry = entry or original
        return auto._apply_movement_plan(connection, statement, entry, self.names.source,
                                          plan or auto._movement_plan(connection, statement, entry))

    def test_real_insert_submit_enrichment_and_alias_only_replay(self):
        first = self.apply()
        second = self.apply(enriched=True)
        connection, statement, entry = movement(self.names, enriched=True)
        entry["reference"] = None
        del entry["bank_reference_ids"]["account_service_reference"]
        third = self.apply(entry=entry)
        self.assertEqual([first["state"], second["state"], third["state"]],
                         ["created", "existing", "existing"])
        self.assertEqual(first["identity"], third["identity"])
        self.assertEqual(first["bank_transaction"], third["bank_transaction"])
        self.assertEqual(frappe.db.count("Bank Transaction", {"bank_account": self.names.bank_account}), 1)
        self.assertEqual(frappe.db.count("EBICS Movement Alias", {"bank_account": self.names.bank_account}), 2)
        doc = frappe.get_doc("Bank Transaction", first["bank_transaction"])
        self.assertEqual(doc.docstatus, 1)
        self.assertEqual(doc.currency, "EUR")
        self.assertEqual(float(doc.withdrawal), 12.34)
        self.assertEqual(float(doc.deposit), 0)
        self.assertFalse(doc.payment_entries)

    def test_alias_unique_index_rejects_second_owner_and_claim_is_immutable(self):
        self.apply()
        claim = frappe.get_doc("EBICS Movement Alias", frappe.db.get_value(
            "EBICS Movement Alias", {"bank_account": self.names.bank_account}, "name"))
        duplicate = frappe.get_doc(claim.as_dict())
        duplicate.name = "SECOND-NAME-" + uuid4().hex
        duplicate.flags.name_set = True
        with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
            duplicate.db_insert()
        claim.currency = "CHF"
        with self.assertRaises(frappe.ValidationError):
            claim.save(ignore_permissions=True)
        with self.assertRaises(frappe.ValidationError):
            frappe.delete_doc("EBICS Movement Alias", claim.name, ignore_permissions=True)

    def test_legacy_reference_and_claim_changes_are_rejected_under_real_row_lock(self):
        connection, statement, entry = movement(self.names)
        doc = frappe.get_doc({"doctype": "Bank Transaction", "date": entry["booking_date"], "company": self.names.company,
                              "bank_account": self.names.bank_account, "currency": "EUR",
                              "withdrawal": 12.34, "transaction_id": entry["reference"]})
        doc.insert(ignore_permissions=True).submit()
        baseline = auto._movement_plan(connection, statement, entry)
        self.assertEqual(baseline["state"], "adopt")
        for field, changed in {
            "transaction_id": "ANOTHER-REFERENCE", "reference_number": "ANOTHER-REFERENCE",
            auto._MOVEMENT_ID_FIELD: "ANOTHER-CLAIM", auto._MOVEMENT_FINGERPRINT_FIELD: "OTHER-HASH",
            auto._MOVEMENT_SOURCE_FIELD: self.names.source,
        }.items():
            with self.subTest(field=field):
                old = frappe.db.get_value("Bank Transaction", doc.name, field)
                frappe.db.set_value("Bank Transaction", doc.name, field, changed, update_modified=False)
                with self.assertRaises(BankFileError):
                    self.apply(plan=baseline)
                self.assertEqual(frappe.db.get_value("Bank Transaction", doc.name, field), changed)
                frappe.db.set_value("Bank Transaction", doc.name, field, old, update_modified=False)
        self.assertEqual(self.apply(plan=baseline)["state"], "adopted")

    def test_alias_content_conflict_and_guest_are_fail_closed(self):
        self.apply(enriched=True)
        connection, statement, entry = movement(self.names)
        changed = deepcopy(entry)
        changed["amount"] = "13.34"
        self.assertEqual(auto._movement_plan(connection, statement, changed)["state"], "review")
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            auto.mirror_historical_bank_movements("NO-PROVIDER-CALL")
