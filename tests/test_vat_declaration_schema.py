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


if __name__ == "__main__":
    unittest.main()
