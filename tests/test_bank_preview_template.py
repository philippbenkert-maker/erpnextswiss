"""Bank-originated text remains literal even when Frappe disables autoescape."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest

try:
    from jinja2.sandbox import SandboxedEnvironment
except ImportError:
    SandboxedEnvironment = None


@unittest.skipIf(SandboxedEnvironment is None, "Jinja2 is required")
class BankPreviewTemplateTests(unittest.TestCase):
    def render(self, **fields):
        transaction = dict(txid=0, date="2026-09-12", credit_debit="CRDT", currency="CHF",
                           amount=12.34, party_name="Test & Co <Bank>", party_address="<b>Address</b>",
                           transaction_reference="<img src=x onerror=alert(1)>",
                           matched_amount=0, invoice_matches=[], party_match=None)
        transaction.update(fields)
        template = Path(__file__).resolve().parents[1] / "erpnextswiss/erpnextswiss/page/bank_wizard/transaction_table.html"
        env = SandboxedEnvironment(autoescape=False)
        frappe = SimpleNamespace(utils=SimpleNamespace(get_datetime=datetime.fromisoformat))
        return env.from_string(template.read_text(encoding="utf-8")).render(transactions=[transaction], frappe=frappe)

    def test_bank_text_cannot_become_markup(self):
        html = self.render()
        self.assertIn("Test &amp; Co &lt;Bank&gt;", html)
        self.assertIn("&lt;b&gt;Address&lt;/b&gt;", html)
        self.assertIn("&lt;img src=x", html)
        self.assertNotIn("<img", html)

    def test_attribute_and_currency_are_escaped_without_removing_actions(self):
        html = self.render(txid='0" onclick="alert(1)', currency='<svg onload="alert(2)">')
        self.assertNotIn('id="row-transaction-0" onclick=', html)
        self.assertNotIn("<svg", html)
        self.assertIn("btn-close-receivable-", html)
        self.assertIn("&#34; onclick=&#34;", html)
