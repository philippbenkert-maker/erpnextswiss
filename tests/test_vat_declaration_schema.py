import csv
import json
from pathlib import Path
import unittest


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "erpnextswiss"
    / "erpnextswiss"
    / "doctype"
    / "vat_declaration"
    / "vat_declaration.json"
)


class VATDeclarationSchemaTests(unittest.TestCase):
    def test_2024_normal_tax_is_limited_to_effective_method(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fields = {field["fieldname"]: field for field in schema["fields"]}

        self.assertEqual(
            fields["normal_tax_2024"].get("depends_on"),
            "eval:doc.vat_type == 'effective'",
        )

    def test_archive_actions_translate_without_a_context(self):
        path = SCHEMA.parents[3] / "translations" / "de.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        # Frappe uses the third CSV column as an explicit context, not as a source location.
        translations = {
            row[0] + (":" + row[2] if len(row) == 3 and row[2] else ""): row[1]
            for row in rows if len(row) in (2, 3)
        }
        self.assertEqual(translations.get("Filed (user confirmed)"), "Eingereicht (vom Benutzer bestätigt)")
        self.assertEqual(translations.get("Download original XML"), "Original-XML herunterladen")
        self.assertEqual(translations.get("Record external filing"), "Erfolgte Einreichung erfassen")


if __name__ == "__main__":
    unittest.main()
