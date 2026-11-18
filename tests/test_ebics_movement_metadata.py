"""Static contracts for the native EBICS movement mirror metadata."""

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


if __name__ == "__main__":
    unittest.main()
