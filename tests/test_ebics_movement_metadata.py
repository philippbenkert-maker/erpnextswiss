"""Static contracts for the native EBICS movement mirror metadata."""

import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestEbicsMovementMetadata(unittest.TestCase):
    def test_bank_transaction_identity_is_unique_and_source_is_linked(self):
        fields = {
            row["name"]: row
            for row in json.loads((ROOT / "erpnextswiss" / "fixtures" / "custom_field.json")
                                  .read_text(encoding="utf-8"))
        }
        identity = fields["Bank Transaction-custom_ebics_movement_id"]
        self.assertEqual(identity["dt"], "Bank Transaction")
        self.assertEqual(identity["fieldtype"], "Data")
        self.assertEqual(identity["unique"], 1)
        self.assertEqual(identity["read_only"], 1)
        source = fields["Bank Transaction-custom_ebics_source_download"]
        self.assertEqual(source["fieldtype"], "Link")
        self.assertEqual(source["options"], "EBICS Download")

    def test_receipt_schema_keeps_native_source_map(self):
        schema = json.loads((ROOT / "erpnextswiss" / "erpnextswiss" / "doctype"
                             / "ebics_download" / "ebics_download.json")
                            .read_text(encoding="utf-8"))
        fields = {row["fieldname"]: row for row in schema["fields"]}
        self.assertEqual(fields["mirrored_count"]["fieldtype"], "Int")
        self.assertEqual(fields["movement_map_json"]["fieldtype"], "Code")
        self.assertEqual(fields["movement_map_json"]["read_only"], 1)

    def test_alias_registry_has_persistent_unique_identity_and_read_only_permissions(self):
        schema = json.loads((ROOT / "erpnextswiss" / "erpnextswiss" / "doctype"
                             / "ebics_movement_alias" / "ebics_movement_alias.json")
                            .read_text(encoding="utf-8"))
        fields = {row["fieldname"]: row for row in schema["fields"]}
        self.assertEqual(schema["autoname"], "field:alias_id")
        self.assertEqual(schema["allow_rename"], 0)
        self.assertEqual(fields["alias_id"]["unique"], 1)
        self.assertEqual(fields["bank_transaction"]["options"], "Bank Transaction")
        for field in fields.values():
            self.assertEqual(field["read_only"], 1)
            self.assertEqual(field["reqd"], 1)
        self.assertEqual({p["role"] for p in schema["permissions"]},
                         {"Accounts Manager", "System Manager"})
        for permission in schema["permissions"]:
            self.assertFalse(any(permission.get(key) for key in ("create", "write", "delete", "submit")))

    def test_disposable_native_cases_are_explicit_and_do_not_enter_generic_discovery(self):
        cases = ROOT / "erpnextswiss/erpnextswiss/tests/native_ebics_movement_cases.py"
        self.assertTrue(cases.is_file())
        self.assertFalse(cases.with_name("test_ebics_movement_native.py").exists())
        tree = ast.parse(cases.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "TestEbicsMovementNative")
        tests = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        self.assertEqual(len(tests), 4)
        self.assertTrue(all(not node.decorator_list for node in tests))


if __name__ == "__main__":
    unittest.main()
