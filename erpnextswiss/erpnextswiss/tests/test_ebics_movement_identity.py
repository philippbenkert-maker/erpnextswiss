"""Offline behavioral regressions using the actual movement helpers, no bank/SQL.

The stateful adapter deliberately separates snapshot reads from locking reads.
Native SQL/unique-index/concurrency acceptance is a separate release gate.
Run: python -B -m unittest erpnextswiss.erpnextswiss.tests.test_ebics_movement_identity
"""

import ast
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


class Row(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    __setattr__ = dict.__setitem__


class BankFileError(Exception):
    pass


class Store:
    def __init__(self):
        self.tables = {
            "Bank Account": {"EUR": Row(name="EUR", company="KT", account="1021", is_company_account=1)},
            "Bank Transaction": {}, "EBICS Movement Alias": {},
        }
        self.calls, self.snapshots = [], {}
        self.stale_tables = None
        self.fail_alias = False
        self.db = self

    def get_all(self, doctype, filters, fields, limit_page_length=20, **kwargs):
        return self.get_values(doctype, filters, fields, limit=limit_page_length, **kwargs)

    def get_values(self, doctype, filters=None, fieldname=None, *, for_update=False,
                   limit=None, **kwargs):
        self.calls.append(("read", doctype, for_update))
        tables = self.tables if for_update or self.stale_tables is None else self.stale_tables
        def matches(row):
            if isinstance(filters, str):
                return row["name"] == filters
            for key, expected in (filters or {}).items():
                actual = row.get(key)
                if isinstance(expected, (tuple, list)):
                    op, value = expected
                    if op == "in" and actual not in value:
                        return False
                    if op == "<" and not actual < value:
                        return False
                elif actual != expected:
                    return False
            return True
        return [Row(deepcopy(row)) for row in tables[doctype].values() if matches(row)][:limit]

    def get_value(self, doctype, filters, fieldname=None, *, for_update=False, **kwargs):
        rows = self.get_values(doctype, filters, fieldname, for_update=for_update, limit=1)
        return rows[0].get(fieldname) if rows else None

    def get_doc(self, doctype, name=None, *, for_update=False):
        if isinstance(doctype, dict):
            return Doc(self, Row(deepcopy(doctype)))
        self.calls.append(("doc", doctype, for_update))
        tables = self.tables if for_update or self.stale_tables is None else self.stale_tables
        return Doc(self, Row(deepcopy(tables[doctype][name]), doctype=doctype))

    def savepoint(self, name):
        self.snapshots[name] = deepcopy(self.tables)

    def rollback(self, save_point):
        self.tables = deepcopy(self.snapshots[save_point])


class Doc(Row):
    def __init__(self, store, values):
        super().__init__(values)
        object.__setattr__(self, "store", store)

    def __deepcopy__(self, memo):
        return Row(deepcopy(dict(self), memo))

    def insert(self, **kwargs):
        if self["doctype"] == "EBICS Movement Alias":
            if self.store.fail_alias:
                raise BankFileError("Synthetic unique-claim failure")
            self["name"] = self["alias_id"]
            if self["name"] in self.store.tables[self["doctype"]]:
                raise BankFileError("Duplicate alias")
        else:
            self["name"] = "BT-" + str(len(self.store.tables["Bank Transaction"]) + 1)
            self["docstatus"], self["payment_entries"] = 0, []
        self.store.tables[self["doctype"]][self["name"]] = self
        return self

    def submit(self):
        self["docstatus"] = 1
        return self

    def db_set(self, values, **kwargs):
        self.store.calls.append(("write", self["name"], tuple(values)))
        self.update(values)
        self.store.tables["Bank Transaction"][self["name"]].update(values)


def helpers(store):
    source = Path(__file__).parents[1] / "ebics_automation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and (
                node.name.startswith(("_movement_", "_claim_movement_", "_apply_movement_"))
                or node.name in {"_money", "_normal_reference", "_one_reference",
                                 "_statement_bank_account", "_mirror_bank_entry"}):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith("_MOVEMENT_")
                for target in node.targets):
            selected.append(node)
    env = dict(frappe=store, Decimal=Decimal, InvalidOperation=InvalidOperation,
               ROUND_HALF_UP=ROUND_HALF_UP, sha256=sha256, json=json,
               BankFileError=BankFileError)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), env)
    return SimpleNamespace(**env)


class TestMovementIdentity(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.api = helpers(self.store)
        self.connection = SimpleNamespace(company="KT")
        self.statement = dict(account="1021", currency="EUR")
        self.entry = dict(issues=[], status="BOOK", amount="100.00", credit_debit="DBIT",
                          booking_date="2026-11-17", value_date="2026-11-17", detail_count=1,
                          reference="BANK-A", bank_reference_ids={"account_service_reference": ["BANK-A"]},
                          candidates=[], xml="<Ntry/>")

    def plan(self, entry=None):
        return self.api._movement_plan(self.connection, self.statement, entry or self.entry)

    def apply(self, entry=None, plan=None):
        entry = entry or self.entry
        return self.api._apply_movement_plan(self.connection, self.statement, entry, "DOWNLOAD-1",
                                             plan or self.plan(entry))

    def enriched(self):
        entry = deepcopy(self.entry)
        entry["bank_reference_ids"]["uetr"] = ["UETR-A"]
        return entry

    def legacy(self):
        self.store.tables["Bank Transaction"]["OLD"] = Row(
            name="OLD", docstatus=1, company="KT", bank_account="EUR", currency="EUR", date="2026-11-17",
            deposit=0, withdrawal=100, transaction_id="BANK-A", reference_number=None,
            custom_ebics_movement_id=None, custom_ebics_movement_fingerprint=None,
            custom_ebics_source_download=None, modified="2026-11-18 10:00:00")

    def test_reference_enrichment_and_later_alias_only_preserve_one_native_id(self):
        first = self.apply()
        second = self.apply(self.enriched())
        only_new_alias = self.enriched()
        only_new_alias["reference"] = None
        only_new_alias["bank_reference_ids"].pop("account_service_reference")
        third = self.apply(only_new_alias)
        self.assertEqual(second["state"], "existing")
        self.assertEqual(third["bank_transaction"], first["bank_transaction"])
        self.assertEqual(third["identity"], first["identity"])
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 1)

    def test_reverse_profile_order_is_idempotent(self):
        first = self.apply(self.enriched())
        second = self.apply()
        self.assertEqual(second["bank_transaction"], first["bank_transaction"])
        self.assertEqual(second["identity"], first["identity"])

    def test_changed_content_via_later_alias_is_review(self):
        self.apply()
        self.apply(self.enriched())
        changed = self.enriched()
        changed.update(reference=None, amount="101.00")
        changed["bank_reference_ids"].pop("account_service_reference")
        self.assertEqual(self.plan(changed)["state"], "review")
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 1)

    def test_two_genuine_equal_movements_with_distinct_references_remain_two(self):
        self.apply()
        other = deepcopy(self.entry)
        other.update(reference="BANK-B", bank_reference_ids={"account_service_reference": ["BANK-B"]})
        self.assertEqual(self.apply(other)["state"], "created")
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 2)

    def test_stale_adoption_never_overwrites_changed_claim_or_reference(self):
        for field, value in {
            "transaction_id": "CHANGED", "reference_number": "CHANGED",
            "custom_ebics_movement_id": "OTHER-CLAIM",
            "custom_ebics_movement_fingerprint": "OTHER-CONTENT",
            "custom_ebics_source_download": "OTHER-SOURCE", "modified": "CHANGED",
            "docstatus": 2, "company": "OTHER", "currency": "CHF", "withdrawal": 101,
        }.items():
            with self.subTest(field=field):
                self.setUp()
                self.legacy()
                plan = self.plan()
                self.assertEqual(plan["state"], "adopt")
                self.store.stale_tables = deepcopy(self.store.tables)
                self.store.tables["Bank Transaction"]["OLD"][field] = value
                with self.assertRaises(BankFileError):
                    self.apply(plan=plan)
                self.assertEqual(self.store.tables["Bank Transaction"]["OLD"][field], value)
                self.assertFalse(any(call[0] == "write" for call in self.store.calls))

    def test_apply_rechecks_current_aliases_after_account_lock(self):
        plan = self.plan()
        stale = deepcopy(self.store.tables)
        first = self.apply()
        self.store.stale_tables = stale
        second = self.apply(plan=plan)
        self.assertEqual(second["state"], "existing")
        self.assertEqual(second["bank_transaction"], first["bank_transaction"])
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 1)

    def test_alias_failure_rolls_back_native_creation(self):
        self.store.fail_alias = True
        with self.assertRaises(BankFileError):
            self.apply()
        self.assertFalse(self.store.tables["Bank Transaction"])
        self.assertFalse(self.store.tables["EBICS Movement Alias"])

    def test_dry_run_detects_conflict_across_enriched_references_without_writes(self):
        planned = {}
        first = self.api._mirror_bank_entry(self.connection, self.statement, self.entry,
                                             "D1", apply=False, planned=planned)
        enriched = self.enriched()
        second = self.api._mirror_bank_entry(self.connection, self.statement, enriched,
                                              "D2", apply=False, planned=planned)
        enriched["amount"] = "101.00"
        third = self.api._mirror_bank_entry(self.connection, self.statement, enriched,
                                             "D3", apply=False, planned=planned)
        self.assertEqual(first["state"], "would_create")
        self.assertEqual(second["state"], "existing_in_plan")
        self.assertEqual(third["state"], "review")
        self.assertFalse(self.store.tables["Bank Transaction"])
        self.assertFalse(self.store.tables["EBICS Movement Alias"])

    def test_two_existing_claims_cannot_be_merged_by_an_enriched_receipt(self):
        self.apply()
        other = self.enriched()
        other["reference"] = None
        other["bank_reference_ids"].pop("account_service_reference")
        self.apply(other)
        self.assertEqual(self.plan(self.enriched())["reason"], "bank_alias_multiple_movements")
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 2)

    def test_changed_content_under_any_known_alias_never_creates(self):
        self.apply(self.enriched())
        for field, value in {"amount": "101.00", "booking_date": "2026-11-18",
                             "value_date": "2026-11-18", "credit_debit": "CRDT"}.items():
            with self.subTest(field=field):
                entry = deepcopy(self.entry)
                entry[field] = value
                self.assertEqual(self.plan(entry)["state"], "review")
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 1)

    def test_cancelled_target_is_review_and_retains_its_claim(self):
        first = self.apply()
        self.store.tables["Bank Transaction"][first["bank_transaction"]]["docstatus"] = 2
        self.assertEqual(self.plan()["state"], "review")
        self.assertTrue(self.store.tables["EBICS Movement Alias"])

    def test_corrupt_scope_or_missing_target_is_review(self):
        self.apply()
        claim = next(iter(self.store.tables["EBICS Movement Alias"].values()))
        claim["company"] = "OTHER"
        self.assertEqual(self.plan()["reason"], "bank_alias_scope_conflict")
        claim["company"] = "KT"
        self.store.tables["Bank Transaction"].clear()
        self.assertEqual(self.plan()["reason"], "bank_alias_target_missing")

    def test_legacy_adoption_claims_all_references_without_replacing_native_values(self):
        self.legacy()
        original = deepcopy(self.store.tables["Bank Transaction"]["OLD"])
        result = self.apply(self.enriched())
        self.assertEqual(result["state"], "adopted")
        row = self.store.tables["Bank Transaction"]["OLD"]
        for key in ("name", "transaction_id", "reference_number", "docstatus", "date",
                    "bank_account", "deposit", "withdrawal", "currency"):
            self.assertEqual(row[key], original[key])
        self.assertEqual(len(self.store.tables["EBICS Movement Alias"]), 2)

    def test_failed_legacy_alias_claim_rolls_back_claim_metadata(self):
        self.legacy()
        original = deepcopy(self.store.tables)
        self.store.fail_alias = True
        with self.assertRaises(BankFileError):
            self.apply()
        self.assertEqual(self.store.tables, original)

    def test_batch_never_claims_a_detail_payment_as_the_whole_entry(self):
        entry = self.enriched()
        entry["detail_count"] = 2
        refs = self.api._movement_references(entry)
        self.assertEqual(refs, [("account_service_reference", "BANK-A")])

    def test_ambiguous_or_missing_bank_reference_is_review(self):
        entry = deepcopy(self.entry)
        entry["reference"] = None
        entry["bank_reference_ids"] = {"uetr": ["ONE", "TWO"]}
        self.assertEqual(self.plan(entry)["reason"], "missing_unique_bank_reference")

    def test_shared_payment_group_does_not_conflate_two_bank_movements(self):
        entry = deepcopy(self.entry)
        entry["bank_reference_ids"]["payment_information_id"] = ["SHARED-GROUP"]
        self.apply(entry)
        entry.update(reference="BANK-B")
        entry["bank_reference_ids"]["account_service_reference"] = ["BANK-B"]
        self.apply(entry)
        self.assertEqual(len(self.store.tables["Bank Transaction"]), 2)

    def test_payment_group_alone_is_not_a_unique_movement_reference(self):
        entry = deepcopy(self.entry)
        entry.update(reference=None, bank_detail_references=["SHARED-GROUP"],
                     bank_reference_ids={"payment_information_id": ["SHARED-GROUP"]})
        self.assertEqual(self.plan(entry)["state"], "review")

    def test_legacy_row_with_inconsistent_company_is_review_not_duplicate(self):
        self.legacy()
        self.store.tables["Bank Transaction"]["OLD"]["company"] = "OTHER"
        self.assertEqual(self.plan()["reason"], "legacy_bank_transaction_company_conflict")


if __name__ == "__main__":
    unittest.main()
