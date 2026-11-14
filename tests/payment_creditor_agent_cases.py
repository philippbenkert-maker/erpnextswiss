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
def test_actual_generation_checks_before_archive_even_without_optional_xsd(xsd_enabled, valid_bank):
    # The Swiss branch delegates to the shared exporter; schema validation is
    # mandatory there, independent of the old optional settings checkbox.
    from datetime import date
    from decimal import Decimal
    from hashlib import sha256
    import json
    import re
    from erpnextswiss.erpnextswiss import swiss_payments as bank
    from erpnextswiss.erpnextswiss.iso20022 import normalize_iban
    path = ROOT / "erpnextswiss/erpnextswiss/page/payment_export/payment_export.py"
    nodes = [n for n in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(n, ast.FunctionDef)]
    for node in nodes:
        node.decorator_list = []
    facade = SimpleNamespace(db=Mock(), get_doc=Mock(), get_all=Mock(return_value=[]),
        throw=Mock(side_effect=lambda text: (_ for _ in ()).throw(ValueError(text))))
    scope = dict(frappe=facade, _=lambda x:x, Decimal=Decimal, sha256=sha256, json=json, re=re,
        getdate=lambda x:date.fromisoformat(str(x)[:10]), nowdate=lambda:"2026-11-14",
        normalize_iban=normalize_iban, is_qr_iban=is_qr_iban, render_credit_transfers=bank.render_credit_transfers)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    row = SimpleNamespace(name="ROW1", idx=1, payment_type="IBAN", iban="DE89370400440532013000",
        esr_participation_number=None, bic="COBADEFF" if valid_bank else "", execution_date="2026-11-16",
        reference="TEST-REF-1", amount=123.45, currency="EUR", receiver="Example",
        receiver_address_line1="Testweg 1", receiver_country="DE", receiver_pincode="8000",
        receiver_city="Zuerich", esr_reference="", is_salary=0)
    proposal = SimpleNamespace(check_permission=Mock(), docstatus=1, company="Example AG", pay_from_account="Test",
        name="TEST", payments=[row])
    facade.get_doc.return_value=proposal
    scope.update(_debtor=lambda *a:COMPANY, _country_code=lambda x:x,
        _archive=Mock(side_effect=lambda result,*a:result))
    if valid_bank:
        result=scope["generate_swiss_proposal_file"]("TEST")
        root=etree.fromstring(result["content"].encode())
        bank._schema().assertValid(root)
        assert root.findtext(".//{*}CdtrAgt/{*}FinInstnId/{*}BICFI")=="COBADEFF"
        assert result["total"]=="123.45"
        scope["_archive"].assert_called_once()
    else:
        with pytest.raises(ValueError, match="BIC"):
            scope["generate_swiss_proposal_file"]("TEST")
        scope["_archive"].assert_not_called()
    proposal.check_permission.assert_called_once_with("write")
