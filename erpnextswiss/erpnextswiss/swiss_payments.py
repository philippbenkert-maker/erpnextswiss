"""Validated Swiss credit transfers, shared by direct and payroll exports.

Pure serialization: this module neither books nor transmits payments.  Decimal
amounts and XML nodes avoid float drift and unsafe string interpolation.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re
from lxml import etree

from erpnextswiss.erpnextswiss.iso20022 import (
    creditor_agent_bic, create_message_id, is_qr_iban, is_valid_qr_reference,
    normalize_iban, normalize_qr_reference,
)

NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"


# EPC409-09 v8.0: geographical scope, checked 14 November 2026.
# Gibraltar uses GI IBANs; the other included territories use their parent IBAN prefix.
SEPA_IBAN_COUNTRIES = frozenset("AD AL AT BE BG CH CY CZ DE DK EE ES FI FR GB GI GR HR HU IE IS IT LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS SE SI SK SM VA".split())

def valid_iban(value):
    iban = normalize_iban(value)
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        return False
    if iban[:2] in ("CH", "LI") and len(iban) != 21:
        return False
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in iban[4:] + iban[:4])
    return int(converted) % 97 == 1


def payment_amount(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
        if amount >= Decimal("1000000000000000"):
            raise ValueError
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Payment amount must be positive, finite and have at most two decimals.") from None


def _text(value, label, limit=140, required=True):
    value = str(value or "").strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label}: missing or invalid text (maximum {limit} characters).")
    return value


def _identifier(value):
    value = str(value or "")
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", value).strip("-") or "PAYMENT"
    return (cleaned[:25] + "-" + sha256(value.encode()).hexdigest()[:9])[:35]


def _node(parent, name, value=None, **attributes):
    node = etree.SubElement(parent, "{" + NAMESPACE + "}" + name, **attributes)
    if value is not None:
        node.text = str(value)
    return node


def _party(parent, name, party):
    element = _node(parent, name)
    _node(element, "Nm", _text(party.get("name"), name + " name"))
    address = _node(element, "PstlAdr")
    street = _text(party.get("street"), name + " street", 70, required=False)
    if street:
        _node(address, "StrtNm", street)
    building = _text(party.get("building"), name + " building number", 16, required=False)
    if building:
        _node(address, "BldgNb", building)
    postcode = _text(party.get("pincode"), name + " postal code", 16, required=False)
    if postcode:
        _node(address, "PstCd", postcode)
    _node(address, "TwnNm", _text(party.get("city"), name + " town", 35))
    country = str(party.get("country_code") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError(name + ": an explicit two-letter address country is required.")
    _node(address, "Ctry", country)


@lru_cache(maxsize=1)
def _schema():
    path = Path(__file__).resolve().parents[1] / "public" / "xsd" / "pain.001.001.09.ch.03.xsd"
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    return etree.XMLSchema(etree.parse(str(path), parser))


def render_credit_transfers(payments, message_id=None, created_at=None):
    """Return a complete SPS pain.001.001.09 file or raise before any write.

    Each row owns its debtor account, so multiple bank accounts can be exported
    without accidentally using the first row's account for subsequent rows.
    """
    if not payments:
        raise ValueError("Please select at least one payment.")
    message_id = message_id or create_message_id()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,35}", message_id):
        raise ValueError("Invalid payment message ID.")
    amounts = [payment_amount(row.get("amount")) for row in payments]
    root = etree.Element("{" + NAMESPACE + "}Document", nsmap={None: NAMESPACE})
    body = _node(root, "CstmrCdtTrfInitn")
    header = _node(body, "GrpHdr")
    _node(header, "MsgId", message_id)
    _node(header, "CreDtTm", (created_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    _node(header, "NbOfTxs", len(payments))
    _node(header, "CtrlSum", format(sum(amounts, Decimal(0)), ".2f"))
    _node(_node(header, "InitgPty"), "Nm", _text(payments[0]["debtor"].get("name"), "Company"))
    seen = set()
    for row, amount in zip(payments, amounts):
        source = _text(row.get("name"), "Payment source", 140)
        if source in seen:
            raise ValueError("Duplicate payment source: " + source)
        seen.add(source)
        identifier = _identifier(source)
        debtor = row["debtor"]
        iban = normalize_iban(row.get("iban"))
        debtor_iban = normalize_iban(debtor.get("iban"))
        if not valid_iban(iban) or not valid_iban(debtor_iban) or is_qr_iban(debtor_iban):
            raise ValueError(source + ": valid creditor and regular debtor IBANs are required.")
        currency = str(row.get("currency") or "").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(source + ": payment currency is missing or invalid.")
        method = row.get("service_level") or "IBAN"
        if method not in ("IBAN", "SEPA", "QRR", "SCOR"):
            raise ValueError(source + ": obsolete ESR or unsupported payment method; use an IBAN or QR-IBAN.")
        reference = str(row.get("reference") or source).strip()
        if is_qr_iban(iban):
            reference = normalize_qr_reference(row.get("qr_reference") or reference)
            if currency not in ("CHF", "EUR") or not is_valid_qr_reference(reference):
                raise ValueError(source + ": a QR-IBAN requires a valid 27-digit QR reference and CHF/EUR.")
            method = "QRR"
        elif method == "QRR":
            raise ValueError(source + ": a QR reference requires a QR-IBAN.")
        if method == "SEPA" and (currency != "EUR" or iban[:2] not in SEPA_IBAN_COUNTRIES or debtor_iban[:2] not in SEPA_IBAN_COUNTRIES):
            raise ValueError(source + ": SEPA payments need EUR and accounts within the SEPA geographical scope.")
        if method == "SCOR":
            reference = "".join(reference.split()).upper()
            if not re.fullmatch(r"RF[0-9]{2}[A-Z0-9]{1,21}", reference):
                raise ValueError(source + ": invalid ISO 11649 creditor reference.")
            converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in reference[4:] + reference[:4])
            if int(converted) % 97 != 1:
                raise ValueError(source + ": invalid ISO 11649 creditor-reference checksum.")
        # SCOR is a reference type, not a cross-border exemption.
        bank = creditor_agent_bic(dict(row, service_level="IBAN" if method == "SCOR" else method), "CH")
        info = _node(body, "PmtInf")
        _node(info, "PmtInfId", identifier)
        _node(info, "PmtMtd", "TRF")
        _node(info, "BtchBookg", "true")
        _node(info, "NbOfTxs", 1)
        _node(info, "CtrlSum", format(amount, ".2f"))
        if method == "SEPA" or row.get("is_salary"):
            kind = _node(info, "PmtTpInf")
            if method == "SEPA":
                _node(_node(kind, "SvcLvl"), "Cd", "SEPA")
            if row.get("is_salary"):
                _node(_node(kind, "CtgyPurp"), "Cd", "SALA")
        execution = date.fromisoformat(str(row["execution_date"])[:10])
        _node(_node(info, "ReqdExctnDt"), "Dt", execution.isoformat())
        _party(info, "Dbtr", debtor)
        _node(_node(_node(info, "DbtrAcct"), "Id"), "IBAN", debtor_iban)
        agent = _node(_node(info, "DbtrAgt"), "FinInstnId")
        debtor_bic = "".join((debtor.get("bic") or "").split()).upper()
        if debtor_bic:
            if not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?", debtor_bic):
                raise ValueError(source + ": invalid debtor bank BIC.")
            _node(agent, "BICFI", debtor_bic)
        elif debtor_iban[:2] in ("CH", "LI"):
            member = _node(agent, "ClrSysMmbId")
            _node(_node(member, "ClrSysId"), "Cd", "CHBCC")
            _node(member, "MmbId", debtor_iban[4:9])
        else:
            raise ValueError(source + ": a debtor bank BIC is required for a foreign account.")
        _node(info, "ChrgBr", "SLEV" if method == "SEPA" else "SHAR")
        tx = _node(info, "CdtTrfTxInf")
        identity = _node(tx, "PmtId")
        _node(identity, "InstrId", identifier)
        _node(identity, "EndToEndId", identifier)
        _node(_node(tx, "Amt"), "InstdAmt", format(amount, ".2f"), Ccy=currency)
        if bank:
            _node(_node(_node(tx, "CdtrAgt"), "FinInstnId"), "BICFI", bank)
        _party(tx, "Cdtr", row["creditor"])
        _node(_node(_node(tx, "CdtrAcct"), "Id"), "IBAN", iban)
        remittance = _node(tx, "RmtInf")
        if method in ("QRR", "SCOR"):
            ref = _node(_node(remittance, "Strd"), "CdtrRefInf")
            kind = _node(ref, "Tp")
            _node(_node(kind, "CdOrPrtry"), "Prtry" if method == "QRR" else "Cd", method)
            if method == "SCOR":
                _node(kind, "Issr", "ISO")
            _node(ref, "Ref", reference)
        else:
            _node(remittance, "Ustrd", _text(reference, source + " remittance", 140))
    try:
        _schema().assertValid(root)
    except etree.DocumentInvalid as error:
        raise ValueError("The payment file does not meet the Swiss bank schema: " + str(error.error_log.last_error)) from None
    content = etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True).decode("utf-8")
    return {"content": content, "message_id": message_id, "count": len(payments),
            "total": format(sum(amounts, Decimal(0)), ".2f"), "skipped": []}
