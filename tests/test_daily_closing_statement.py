from pathlib import Path
import unittest


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "erpnextswiss"
    / "erpnextswiss"
    / "doctype"
    / "daily_closing_statement"
    / "daily_closing_statement.py"
)


class DailyClosingStatementTests(unittest.TestCase):
    def test_item_and_group_totals_include_global_invoice_discounts(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertEqual(source.count("`tabSales Invoice Item`.`net_amount`"), 6)
        self.assertNotIn("`tabSales Invoice Item`.`amount`", source)


if __name__ == "__main__":
    unittest.main()
