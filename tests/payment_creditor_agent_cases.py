"""SPS creditor-agent regression: EUR without SEPA is still cross-border.

Synthetic fixtures only. No bank upload, ERP write, or real account data.
"""
import ast
from copy import deepcopy
from datetime import datetime
import html
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import nullcontext
import os

import jinja2
from lxml import etree
import pytest

from erpnextswiss.erpnextswiss.iso20022 import (
    creditor_agent_bic, create_message_id, create_payment_file_name,
    is_qr_iban, is_valid_qr_reference,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "erpnextswiss/erpnextswiss/doctype/payment_proposal"
COMPANY = dict(name="Example AG", address_line1="Testweg 1", address_line2="8000 Zuerich", street="Testweg",
               building="1", pincode="8000", city="Zuerich", country_code="CH",
               iban="CH9300762011623852957", bic="POFICHBEXXX")


def payment(**overrides):
    record = dict(id="PMTINF-test-0", method="TRF", batch="true", service_level="IBAN",
                  required_execution_date="2026-11-05", end_to_end_id="TEST-REF-1",
                  currency="EUR", amount=123.45, creditor=COMPANY.copy(), is_salary=0,
                  iban="DE89370400440532013000", bic="COBADEFFXXX", reference="TEST-REF-1")
    record.update(overrides)
    return record


@pytest.mark.parametrize("currency", ["EUR", "USD", "CHF", "GBP"])
def test_all_cross_border_currencies_need_bank(currency):
    assert creditor_agent_bic(payment(currency=currency), "CH") == "COBADEFFXXX"


@pytest.mark.parametrize("bic", [None, "", "   ", "SHORT", "12345678", "AAAA11XX", "COBADEFF<"])
def test_no_silent_export_with_missing_or_invalid_bank(bic):
    with pytest.raises(ValueError, match="BIC"):
        creditor_agent_bic(payment(bic=bic), "CH")


def test_normalizes_bic_without_mutating_payment():
    row = payment(bic=" cobadeff xxx\n")
    before = deepcopy(row)
    assert creditor_agent_bic(row, "CH") == "COBADEFFXXX"
    assert row == before
    assert creditor_agent_bic(payment(bic="COBADEFF"), "CH") == "COBADEFF"


@pytest.mark.parametrize("overrides,region", [
    ({"service_level": "SEPA", "bic": ""}, "CH"),
    ({"service_level": "QRR", "bic": ""}, "CH"),
    ({"service_level": "ESR", "bic": ""}, "CH"),
    ({"iban": " ch93 0076 2011 6238 5295 7", "bic": ""}, "CH"),
    ({"iban": "LI21088100002324013AA", "bic": ""}, "CH"),
    ({"bic": ""}, "AT"),
])
def test_unchanged_iban_only_routing(overrides, region):
    assert creditor_agent_bic(payment(**overrides), region) is None


@pytest.mark.parametrize("version", ["03", "09"])
@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("kind", ["foreign_eur", "foreign_usd", "sepa", "ch", "li", "qrr"])
def test_rendered_templates_are_schema_valid_and_preserve_transaction(version, single, kind):
    row = payment()
    if kind == "foreign_usd":
        row["currency"] = "USD"
    elif kind == "sepa":
        row.update(service_level="SEPA", bic="")
    elif kind in ("ch", "li"):
        row.update(iban=COMPANY["iban"] if kind == "ch" else "LI21088100002324013AA", bic="")
    elif kind == "qrr":
        row.update(service_level="QRR", esr_participation_number="CH4431999123000889012",
                   esr_reference="210000000003139471430009017", currency="CHF", bic="")
    row["creditor_agent_bic"] = creditor_agent_bic(row, "CH")
    base = "pain-001-001-09" if version == "09" else "pain-001"
    name = base + ("_single_payment" if single else "") + ".html"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), undefined=jinja2.StrictUndefined)
    content = env.get_template(name).render(
        msgid="MSG-TEST-ONLY", date="2026-11-05T12:00:00", transaction_count=1,
        control_sum=row["amount"], company=COMPANY, payments=[row], xml_region="CH", xml_version=version)
    root = etree.fromstring(content.encode())
    schema = etree.XMLSchema(etree.parse(str(ROOT / f"erpnextswiss/public/xsd/pain.001.001.{version}.xsd")))
    schema.assertValid(root)
    ns = {"p": root.nsmap[None]}
    tx = root.find(".//p:CdtTrfTxInf", ns)
    bank = tx.find("p:CdtrAgt/p:FinInstnId", ns)
    if kind.startswith("foreign"):
        assert len(bank) == 1
        assert etree.QName(bank[0]).localname == ("BICFI" if version == "09" else "BIC")
        assert bank[0].text == "COBADEFFXXX"
    else:
        assert bank is None
    assert tx.find("p:Amt/p:InstdAmt", ns).text == "123.45"
    assert tx.find("p:Amt/p:InstdAmt", ns).get("Ccy") == row["currency"]
    assert tx.find("p:PmtId/p:EndToEndId", ns).text == "TEST-REF-1"
    assert tx.find("p:CdtrAcct/p:Id/p:IBAN", ns).text == row.get("esr_participation_number", row["iban"])


@pytest.mark.parametrize("xsd_enabled", [0, 1])
@pytest.mark.parametrize("valid_bank", [False, True])
def test_actual_generation_checks_before_render_even_without_xsd(xsd_enabled, valid_bank):
    # Execute the real method, with I/O collaborators replaced, without requiring a database.
    tree = ast.parse((TEMPLATES / "payment_proposal.py").read_text(encoding="utf-8"))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PaymentProposal")
    method = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "create_bank_file")
    method.decorator_list = []
    settings = dict(xml_version="09", banking_region="CH", validate_xml=xsd_enabled)
    account = SimpleNamespace(iban=COMPANY["iban"], bic=COMPANY["bic"])
    facade = SimpleNamespace(get_doc=Mock(side_effect=[settings, account]), get_value=Mock(return_value="DE"), render_template=Mock(),
                             throw=Mock(side_effect=ValueError("missing creditor BIC")))
    scope = dict(frappe=facade, html=html, datetime=datetime, time=SimpleNamespace(strftime=lambda _: "2026-11-05T12:00:00"),
                 create_message_id=create_message_id, create_payment_file_name=create_payment_file_name,
                 get_primary_address=lambda **_: None, get_street_name=lambda x: x,
                 get_building_number=lambda _: "1", is_qr_iban=is_qr_iban,
                 is_valid_qr_reference=is_valid_qr_reference, creditor_agent_bic=creditor_agent_bic, _=lambda x: x)
    scope.update(cint=lambda x: int(x or 0), os=os, XML_SCHEMA_FILES={"CH": {"09": "unused.xsd"}},
                 validate_xml_against_xsd=Mock(return_value=(True, None)))
    facade.utils = SimpleNamespace(get_bench_path=lambda: "/test")
    facade.render_template.return_value = "TEST-XML"
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(TEMPLATES / "payment_proposal.py"), "exec"), scope)
    row = SimpleNamespace(idx=1, payment_type="IBAN", iban="DE89370400440532013000",
                          esr_participation_number=None, bic="COBADEFF" if valid_bank else "", execution_date="2026-11-05",
                          reference="TEST-REF-1", amount=123.45, currency="EUR", receiver="Example",
                          receiver_address_line1="Testweg 1", receiver_address_line2="8000 Zuerich",
                          receiver_country="DE", receiver_country_code="DE", receiver_pincode="8000",
                          receiver_city="Zuerich", is_salary=0)
    proposal = SimpleNamespace(check_permission=Mock(), company="Example AG", pay_from_account="Test",
                               name="TEST", payments=[row], get=lambda _: 0)
    generate = scope["create_bank_file"]
    context = nullcontext()
    if os.environ.get("KT_TEST_NATIVE_PAYMENT_IMPORT") == "1":
        from erpnextswiss.erpnextswiss.doctype.payment_proposal import payment_proposal as native
        generate = native.PaymentProposal.create_bank_file
        # Frappe wraps methods for argument validation; patch the implementation
        # module, not the wrapper's globals, while retaining the actual wrapper.
        context = patch.dict(vars(native), scope)
    with context:
        if valid_bank:
            assert generate(proposal)["content"] == "TEST-XML"
            exported = facade.render_template.call_args.args[1]
            assert exported["payments"][0]["creditor_agent_bic"] == "COBADEFF"
            assert exported["payments"][0]["amount"] == 123.45
            assert exported["payments"][0]["service_level"] == "IBAN"
        else:
            with pytest.raises(ValueError, match="missing creditor BIC"):
                generate(proposal)
            facade.render_template.assert_not_called()
    proposal.check_permission.assert_called_once_with("write")
