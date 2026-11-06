"""Fail-closed EBICS 3.0 retrieval with a durable bank receipt.

The old statement importer acknowledges before persistence and may submit a
Payment Entry while downloading. This module never calls that importer.
"""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
import re
from zipfile import ZIP_STORED, ZipFile

import frappe
from frappe.config import get_site_config

from erpnextswiss.scripts.bank_camt_preview import preview_camt_archive
from erpnextswiss.scripts.bank_file_admission import BankFileError, MAX_FILE_BYTES, MAX_TOTAL_BYTES, read_bank_archive


PROFILE = "camt.053.001.08"
STATEMENT_PROFILES = {"04": "camt.053.001.04", "08": PROFILE}
MAX_DOWNLOADS_PER_RUN = 14
MAX_ARCHIVED_DOWNLOADS = 10000
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_RECEIPT_NAME = re.compile(r"statement-[0-9]{3}\.xml\Z")


def _archive_bytes(data):
    """Store SDK-delivered XML as bounded UTF-8 in a deterministic envelope."""
    if not isinstance(data, dict) or len(data) > 64:
        raise BankFileError("EBICS delivered an invalid file set")
    files = {}
    total = 0
    for index, (source, content) in enumerate(sorted(data.items(), key=lambda item: str(item[0]))):
        if not isinstance(source, str) or not source or not isinstance(content, (bytes, str)):
            raise BankFileError("EBICS delivered an invalid file")
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not 0 < len(raw) <= MAX_FILE_BYTES:
            raise BankFileError("EBICS XML exceeds the configured file limit")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise BankFileError("EBICS XML exceeds the configured transfer limit")
        # The bank-provided name is metadata only; it never becomes a path.
        files[f"statement-{index:03d}.xml"] = raw.decode("utf-8")
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_TOTAL_BYTES + 4096:
        raise BankFileError("EBICS receipt exceeds the storage limit")
    return encoded


def _receipt_files(payload):
    files = json.loads(payload)
    if not isinstance(files, dict) or len(files) > 64:
        raise BankFileError("Stored EBICS receipt is invalid")
    result = {}
    total = 0
    for name, content in sorted(files.items()):
        if not _RECEIPT_NAME.fullmatch(name) or not isinstance(content, str):
            raise BankFileError("Stored EBICS receipt is invalid")
        raw = content.encode("utf-8")
        total += len(raw)
        if not 0 < len(raw) <= MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise BankFileError("Stored EBICS receipt exceeds file limits")
        result[name] = (raw, sha256(raw).hexdigest())
    return result


def _zip_from_receipt(payload, included_hashes=None):
    files = _receipt_files(payload)
    if not files:
        return None
    output = BytesIO()
    included = set(included_hashes) if included_hashes is not None else None
    emitted = set()
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for name, (raw, digest) in files.items():
            if included is not None and (digest not in included or digest in emitted):
                continue
            archive.writestr(name, raw)
            emitted.add(digest)
    if not emitted:
        return None
    return output.getvalue()


def _accounts(connection):
    config = get_site_config(cached=False).get("ebics_auto_accounts")
    if (not isinstance(config, dict) or set(config) != {"version", "bindings"}
            or config["version"] != 1 or not isinstance(config["bindings"], dict)):
        raise BankFileError("Explicit EBICS account binding is not configured")
    accounts = config["bindings"].get(connection.name)
    if (not isinstance(accounts, list) or not 0 < len(accounts) <= 64
            or any(not isinstance(value, str) or not value for value in accounts)
            or len(set(accounts)) != len(accounts)):
        raise BankFileError("No bounded EBICS account binding is available")
    for name in accounts:
        account = frappe.get_doc("Account", name)
        if (account.company != connection.company or account.account_type != "Bank"
                or account.disabled or account.is_group or not account.iban
                or account.account_currency not in ("CHF", "EUR")):
            raise BankFileError("EBICS account binding no longer matches the ledger")
    return accounts


def _statement_profile(connection):
    version = connection.statement_btf_version
    if connection.scope != "CH" or version not in STATEMENT_PROFILES:
        raise BankFileError("EBICS connection is not configured for a supported Swiss statement profile")
    return STATEMENT_PROFILES[version]


def _preview(payload, connection, included_hashes=None, profile=None):
    archive = _zip_from_receipt(payload, included_hashes=included_hashes)
    if archive is None:
        return {"statements": []}
    return preview_camt_archive(archive, profile or _statement_profile(connection),
                                company=connection.company, accounts=_accounts(connection))


def _receipt_key(connection_name, transaction_id):
    digest = sha256(json.dumps([connection_name, transaction_id], separators=(",", ":")).encode()).hexdigest()
    return "EBICS-" + digest


def _verify_receipt(record):
    payload = record.payload_json.encode("utf-8")
    if len(payload) != record.payload_bytes or sha256(payload).hexdigest() != record.payload_sha256:
        raise BankFileError("Stored EBICS receipt failed integrity verification")
    return payload


def _known_files(connection):
    """Build the first-seen per-file index, including legacy single-file receipts."""
    known = {}
    offset = 0
    while offset < MAX_ARCHIVED_DOWNLOADS:
        names = frappe.get_all("EBICS Download", filters={"connection": connection.name,
                                                        "ack_state": "Confirmed"},
                               pluck="name", order_by="creation asc, name asc",
                               limit_start=offset, limit_page_length=100)
        for name in names:
            record = frappe.get_doc("EBICS Download", name)
            for _, digest in _receipt_files(_verify_receipt(record)).values():
                known.setdefault(digest, name)
        if len(names) < 100:
            return known
        offset += len(names)
    raise BankFileError("EBICS archive requires indexed review before further downloads")


def _classify_files(payload, known_files):
    hashes = list(dict.fromkeys(digest for _, digest in _receipt_files(payload).values()))
    duplicate_sources = {digest: known_files[digest] for digest in hashes if digest in known_files}
    new_hashes = [digest for digest in hashes if digest not in known_files]
    sources = set(duplicate_sources.values())
    return {
        "file_hashes_json": json.dumps(hashes),
        "new_file_hashes_json": json.dumps(new_hashes),
        "duplicate_sources_json": json.dumps(duplicate_sources, sort_keys=True),
        "duplicate_of": next(iter(sources)) if not new_hashes and len(sources) == 1 else None,
    }, new_hashes


def _acknowledge(connection, record, client=None, reader=None):
    if record.ack_state == "Confirmed":
        return
    _verify_receipt(record)
    # The bank reply is sent only after a completed ERP commit and readback.
    if reader is not None:
        def verify(transaction_id):
            if transaction_id != record.bank_transaction_id:
                raise BankFileError("Stored receipt transaction identity changed")
            _verify_receipt(frappe.get_doc("EBICS Download", record.name))
        reader.confirm(record.bank_transaction_id, verify)
    else:
        (client or connection.get_client()).confirm_download(trans_id=record.bank_transaction_id)
    record.db_set("ack_state", "Confirmed", commit=True)


def _pending_receipt(connection):
    names = frappe.get_all("EBICS Download", filters={"connection": connection.name, "ack_state": "Pending"},
                           pluck="name", order_by="creation asc", limit_page_length=2)
    if len(names) > 1:
        raise BankFileError("Multiple unacknowledged EBICS downloads require review")
    return frappe.get_doc("EBICS Download", names[0]) if names else None


def _download_pending(connection, requested_date, known_files=None, reader=None):
    """Fetch one bank-provided current file set, never a historical DateRange.

    requested_date is the ERP retrieval date for audit, not a filter sent to the
    bank. The bank may provide several files for the same day, so it is not a
    uniqueness key; the EBICS transaction ID is.
    """
    pending = _pending_receipt(connection)
    if pending:
        _acknowledge(connection, pending, reader=reader)
        return pending

    if reader is not None:
        return _download_python(connection, requested_date, known_files, reader)

    from fintech.ebics import BusinessTransactionFormat

    profile = _statement_profile(connection)
    client = connection.get_client()
    btf = BusinessTransactionFormat(service="EOP", msg_name="camt.053", scope="CH", container="ZIP",
                                    version=connection.statement_btf_version)
    try:
        data = client.BTD(btf)
    except Exception as error:
        if str(error) == "EBICS_NO_DOWNLOAD_DATA_AVAILABLE":
            return None
        raise

    transaction_id = client.last_trans_id
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(transaction_id):
        raise BankFileError("EBICS did not supply a safe receipt transaction ID")
    payload = _archive_bytes(data)
    _preview(payload, connection, profile=profile)  # Validate every file/account before positive bank receipt.
    name = _receipt_key(connection.name, transaction_id)
    if frappe.db.exists("EBICS Download", name):
        raise BankFileError("EBICS transaction identity is already archived")
    if known_files is None:
        known_files = _known_files(connection)
    file_metadata, new_hashes = _classify_files(payload, known_files)
    record = frappe.get_doc({
        "doctype": "EBICS Download", "download_key": name, "connection": connection.name,
        "company": connection.company, "requested_date": requested_date.isoformat(),
        "profile": profile, "bank_transaction_id": transaction_id, "ack_state": "Pending",
        "processing_state": "Duplicate" if not new_hashes else "Pending", **file_metadata,
        "payload_sha256": sha256(payload).hexdigest(),
        "payload_bytes": len(payload), "payload_json": payload.decode("utf-8"),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    _verify_receipt(frappe.get_doc("EBICS Download", record.name))
    _acknowledge(connection, record, client=client)
    for digest in new_hashes:
        known_files[digest] = record.name
    return record


def _download_python(connection, requested_date, known_files, reader):
    profile = _statement_profile(connection)
    known_files = _known_files(connection) if known_files is None else known_files
    records = []

    def persist(transaction_id, archive):
        if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(transaction_id):
            raise BankFileError("EBICS did not supply a safe receipt transaction ID")
        admitted = read_bank_archive(archive, profile)
        payload = _archive_bytes({member.name: member.content for member in admitted.files})
        _preview(payload, connection, profile=profile)
        name = _receipt_key(connection.name, transaction_id)
        # The pinned SDK does not retry automatically. Reject a callback replay,
        # including one with altered bytes, rather than acknowledging twice.
        if records or frappe.db.exists("EBICS Download", name):
            raise BankFileError("EBICS transaction identity is already archived")
        metadata, new_hashes = _classify_files(payload, known_files)
        record = frappe.get_doc({
            "doctype": "EBICS Download", "download_key": name, "connection": connection.name,
            "company": connection.company, "requested_date": requested_date.isoformat(),
            "profile": profile, "bank_transaction_id": transaction_id, "ack_state": "Pending",
            "processing_state": "Duplicate" if not new_hashes else "Pending", **metadata,
            "payload_sha256": sha256(payload).hexdigest(), "payload_bytes": len(payload),
            "payload_json": payload.decode("utf-8"),
        }).insert(ignore_permissions=True)
        frappe.db.commit()
        _verify_receipt(frappe.get_doc("EBICS Download", record.name))
        records.append((record, new_hashes))
        # Returning authorizes ONLY this transaction's positive bank receipt.

    transaction_id = reader.download(persist)
    if transaction_id is None:
        if records:
            raise BankFileError("Bank reported no data after a persisted download")
        return None
    if len(records) != 1 or records[0][0].bank_transaction_id != transaction_id:
        raise BankFileError("Bank receipt does not match the durable ERP download")
    record, new_hashes = records[0]
    # SDK returns only after authenticated positive receipt confirmation.
    record.db_set("ack_state", "Confirmed", commit=True)
    for digest in new_hashes:
        known_files[digest] = record.name
    return record


def _configured_reader(connection):
    provider = get_site_config(cached=False).get("ebics_statement_reader", "fintech")
    if provider == "fintech":
        return None
    if provider != "ebicsclient":
        raise BankFileError("Unknown configured EBICS statement reader")
    from erpnextswiss.erpnextswiss.python_ebics_reader import PythonEbicsReader
    return PythonEbicsReader(connection)


def _money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return None
        rounded = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount > 0 and amount == rounded else None


def _reference_mentions(value, text):
    if not isinstance(value, str) or not value or not isinstance(text, str):
        return False
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", text))


def _contract_discount(invoice, amount, booking_date):
    """Only a wholly unpaid, single-term sales invoice with dated agreed discount."""
    outstanding = _money(invoice.outstanding_amount)
    if outstanding == amount:
        return Decimal('0.00')
    terms = invoice.get('payment_schedule') or []
    if (len(terms) != 1 or invoice.get('supplier') or invoice.get('is_return')
            or outstanding is None or outstanding != _money(invoice.get('grand_total'))
            or Decimal(str(invoice.get('conversion_rate') or 0)) != 1):
        return None
    term = terms[0]
    try:
        booking = date.fromisoformat(str(booking_date)[:10])
        if (not term.get('payment_term') or term.get('paid_amount') or term.get('discounted_amount')
                or _money(term.get('outstanding')) != outstanding
                or not date.fromisoformat(str(invoice.get('posting_date'))[:10]) <= booking
                    <= date.fromisoformat(str(term.get('discount_date'))[:10])):
            return None
        discount = Decimal(str(term.get('discount')))
        if not discount.is_finite() or discount <= 0:
            return None
        if term.get('discount_type') == 'Percentage':
            discount = outstanding * discount / 100
        elif term.get('discount_type') != 'Amount':
            return None
        discount = discount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return discount if 0 < discount < outstanding and outstanding - discount == amount else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _verified_bank_references(entry):
    references = {entry.get('reference'), entry.get('entry_reference')}
    # These values come from explicit paths in schema-validated bank XML,
    # never from the legacy matcher's inference or a remittance free-text field.
    if entry['detail_count'] == 1:
        references.update(entry.get('bank_detail_references') or [])
        if entry.get('reference'):
            references.add(entry['reference'] + '-1')
    return references - {None, '', 'NOTPROVIDED'}


def _strict_invoice_candidate(connection, statement, entry):
    """Return one live invoice only when bank, matcher and ledger all agree."""
    if (statement["balance_check"] != "matched" or entry["issues"]
            or entry["detail_count"] > 1 or len(entry["candidates"]) != 1):
        return None
    candidate = entry["candidates"][0]
    amount = _money(entry["amount"])
    if (amount is None or amount != _money(candidate.get("amount"))
            or candidate.get("currency") != statement["currency"]
            or candidate.get("credit_debit") != entry["credit_debit"]
            or candidate.get("date") != (entry["booking_date"] or "")[:10]):
        return None
    bank_reference = candidate.get("unique_reference")
    if (not isinstance(bank_reference, str) or not bank_reference.strip()
            or bank_reference == statement.get('iban')
            or bank_reference not in _verified_bank_references(entry)):
        return None
    direction = candidate["credit_debit"]
    if direction not in ("CRDT", "DBIT") or candidate.get("expense_matches"):
        return None
    names = candidate.get("invoice_matches")
    if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
        return None
    doctype = "Sales Invoice" if direction == "CRDT" else "Purchase Invoice"
    invoice = frappe.get_doc(doctype, names[0])
    party_field = "customer" if direction == "CRDT" else "supplier"
    if (invoice.docstatus != 1 or invoice.company != connection.company
            or invoice.get(party_field) != candidate.get("party_match")
            or _money(invoice.outstanding_amount) != _money(candidate.get('matched_amount'))
            or _contract_discount(invoice, amount, candidate['date']) is None
            or invoice.currency != statement["currency"]):
        return None
    reference_text = candidate.get("transaction_reference") or ""
    accepted = {str(invoice.name)}
    accepted.update(str(value) for value in (invoice.get("bill_no"), invoice.get("esr_reference"),
                                             invoice.get("esr_reference_number"),
                                             invoice.get("reference_number_full"))
                    if value and len(str(value)) >= 6)
    if not any(_reference_mentions(value, reference_text) for value in accepted):
        return None
    return candidate, doctype, invoice, amount


def _discount_payment(invoice, doctype, candidate, statement, amount, download_name):
    """Use ERPNext's own invoice/term/tax accounting, never an arbitrary write-off."""
    from erpnext.accounts.doctype.payment_entry.payment_entry import (
        get_payment_entry, split_early_payment_discount_loss)
    company = frappe.get_doc('Company', invoice.company)
    if (doctype != 'Sales Invoice' or company.default_currency != invoice.currency
            or not company.default_discount_account
            or get_site_config(cached=False).get('ebics_auto_discount_tax_split') is not True):
        raise BankFileError('Automatic cash discount requires explicit tax/account configuration')
    discount_account = frappe.get_doc('Account', company.default_discount_account)
    if (discount_account.company != invoice.company or discount_account.disabled or discount_account.is_group
            or discount_account.root_type != 'Income'):
        raise BankFileError('Cash discount account does not match the company')
    pe = get_payment_entry(doctype, invoice.name, bank_account=statement['account'],
                           reference_date=candidate['date'])
    # Keep native term allocation, but split the deduction for this EBICS payment
    # without changing the global manual-payment accounting preference.
    pe.set('deductions', [])
    term = invoice.payment_schedule[0]
    split_early_payment_discount_loss(pe, invoice, [{'type': term.discount_type, 'discount': term.discount}])
    expected = _money(invoice.outstanding_amount) - amount
    remainder = expected - sum(Decimal(str(d.amount)) for d in pe.deductions)
    if abs(remainder) > Decimal('0.02'):
        raise BankFileError('Native discount split does not explain the bank difference')
    if remainder:
        if not company.round_off_account:
            raise BankFileError('Discount rounding account is missing')
        pe.append('deductions', dict(account=company.round_off_account, cost_center=company.cost_center,
                                     amount=float(remainder)))
    pe.update(dict(posting_date=candidate['date'], reference_date=candidate['date'],
                   reference_no=candidate['unique_reference'], paid_amount=float(amount), received_amount=float(amount),
                   camt_amount=float(amount), remarks='EBICS {0}; contractual cash discount; {1}'.format(
                       download_name, candidate.get('transaction_reference') or '')))
    pe.set_amounts()
    pe.insert()
    if (pe.paid_to != statement['account'] or pe.paid_from != invoice.debit_to
            or pe.payment_type != 'Receive' or pe.party_type != 'Customer'
            or pe.paid_to_account_currency != invoice.currency or pe.paid_from_account_currency != invoice.currency
            or str(pe.posting_date)[:10] != candidate['date'] or pe.reference_no != candidate['unique_reference']):
        raise BankFileError('Native discount payment has unexpected accounts or bank identity')
    return pe


def _zero_money(value):
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount == Decimal('0.00')
    except (InvalidOperation, TypeError, ValueError):
        return False


def _cross_export_ebanking_id(entry):
    """Return one AKB e-banking identity carried by the authenticated XML fields."""
    identities = set()
    values = {entry.get('reference'), entry.get('entry_reference')}
    values.update(entry.get('bank_detail_references') or [])
    for value in values:
        if not isinstance(value, str):
            continue
        match = re.fullmatch(r'O/(\d{6,30})(?:-1)?', value.strip())
        if match:
            identities.add(match.group(1))
    return next(iter(identities)) if len(identities) == 1 else None


def _remarks_mention_ebanking_id(remarks, identity):
    if not isinstance(remarks, str) or not identity:
        return False
    labelled = re.findall(
        r'(?i)(?:e|electronic)[ -]?banking(?:[ -]?(?:nr|nummer))?\.?\s*[:#=-]?\s*(\d{6,30})',
        remarks)
    return identity in labelled


def _cash_shape_matches(payment, amount, currency):
    return (all(_money(payment.get(key)) == amount for key in ('paid_amount', 'received_amount'))
            and all(payment.get(key) == currency
                    for key in ('paid_from_account_currency', 'paid_to_account_currency')))


def _invoice_reference_matches(invoice, text):
    accepted = {str(invoice.name)}
    accepted.update(str(value) for value in (invoice.get('bill_no'), invoice.get('esr_reference'),
                                             invoice.get('esr_reference_number'),
                                             invoice.get('reference_number_full'))
                    if value and len(str(value)) >= 6)
    return any(_reference_mentions(value, text) for value in accepted)


def _cross_export_payment_matches(connection, statement, entry, amount, incoming):
    """Prove that an O/... bank entry is the same cash movement as a submitted ERP payment."""
    if len(entry.get('candidates') or []) != 1:
        return False
    candidate = entry['candidates'][0]
    posting_date = (entry.get('booking_date') or '')[:10]
    party_type = 'Customer' if incoming else 'Supplier'
    expected_doctype = 'Sales Invoice' if incoming else 'Purchase Invoice'
    if (candidate.get('credit_debit') != entry.get('credit_debit')
            or _money(candidate.get('amount')) != amount
            or candidate.get('currency') != statement.get('currency')
            or candidate.get('date') != posting_date
            or not isinstance(candidate.get('party_match'), str)
            or not candidate.get('party_match')
            or not isinstance(candidate.get('transaction_reference'), str)
            or not candidate.get('transaction_reference')):
        return False
    identity = _cross_export_ebanking_id(entry)
    if identity is None:
        return False

    bank_field = 'paid_to' if incoming else 'paid_from'
    payment_type = 'Receive' if incoming else 'Pay'
    matches = frappe.get_all('Payment Entry', filters={
        'company': connection.company, 'docstatus': 1, 'payment_type': payment_type,
        bank_field: statement['account'], 'posting_date': posting_date},
        fields=['name', 'paid_amount', 'received_amount', 'paid_from_account_currency',
                'paid_to_account_currency', 'party_type', 'party', 'remarks'],
        limit_page_length=21)
    if len(matches) >= 21:
        return False
    proven = []
    for row in matches:
        if (not _cash_shape_matches(row, amount, statement['currency'])
                or row.get('party_type') != party_type
                or row.get('party') != candidate['party_match']
                or not _remarks_mention_ebanking_id(row.get('remarks'), identity)):
            continue
        payment = frappe.get_doc('Payment Entry', row['name'])
        references = payment.get('references') or []
        if (payment.docstatus != 1 or payment.company != connection.company
                or payment.payment_type != payment_type
                or payment.get(bank_field) != statement['account']
                or str(payment.posting_date)[:10] != posting_date
                or not _cash_shape_matches(payment, amount, statement['currency'])
                or payment.party_type != party_type or payment.party != candidate['party_match']
                or not _remarks_mention_ebanking_id(payment.get('remarks'), identity)
                or len(references) != 1
                or references[0].get('reference_doctype') != expected_doctype
                or _money(references[0].get('allocated_amount')) != amount):
            continue
        invoice = frappe.get_doc(expected_doctype, references[0].get('reference_name'))
        party_field = 'customer' if incoming else 'supplier'
        if (invoice.docstatus != 1 or invoice.company != connection.company
                or invoice.get(party_field) != candidate['party_match']
                or invoice.currency != statement['currency']
                or not _zero_money(invoice.outstanding_amount)
                or not _invoice_reference_matches(invoice, candidate['transaction_reference'])):
            continue
        proven.append(payment.name)
    return len(proven) == 1


def _already_booked_entry(connection, statement, entry):
    """Resolve only a posted payment proven to represent this exact bank movement."""
    amount = _money(entry['amount'])
    direction = entry.get('credit_debit')
    if (entry['issues'] or entry['detail_count'] != 1 or amount is None
            or direction not in ('CRDT', 'DBIT')):
        return False
    refs = list(_verified_bank_references(entry) - {statement.get('iban')})
    if not refs:
        return False
    incoming = direction == 'CRDT'
    matches = frappe.get_all('Payment Entry', filters={
        'company': connection.company, 'docstatus': 1, 'reference_no': ('in', refs),
        'payment_type': 'Receive' if incoming else 'Pay',
        'paid_to' if incoming else 'paid_from': statement['account'],
        'posting_date': (entry['booking_date'] or '')[:10]},
        fields=['paid_amount', 'received_amount', 'paid_from_account_currency', 'paid_to_account_currency'])
    if len(matches) == 1 and _cash_shape_matches(matches[0], amount, statement['currency']):
        return True
    return _cross_export_payment_matches(connection, statement, entry, amount, incoming)


def _book_exact_match(connection, statement, entry, download_name):
    matched = _strict_invoice_candidate(connection, statement, entry)
    if matched is None:
        return False
    candidate, doctype, invoice, amount = matched
    reference = candidate["unique_reference"]
    if frappe.db.exists("Payment Entry", {"company": connection.company, "reference_no": reference,
                                           "docstatus": ("<", 2)}):
        return False

    from erpnextswiss.erpnextswiss.page.bank_wizard.bank_wizard import get_default_accounts, make_payment_entry

    defaults = get_default_accounts(company=connection.company)
    incoming = candidate["credit_debit"] == "CRDT"
    payload = {
        "amount": float(amount), "date": candidate["date"], "reference_no": reference,
        "paid_from": defaults.get("receivable_account") if incoming else statement["account"],
        "paid_to": statement["account"] if incoming else defaults.get("payable_account"),
        "type": "Receive" if incoming else "Pay", "party_type": "Customer" if incoming else "Supplier",
        "party": candidate["party_match"], "references": repr([invoice.name]),
        "remarks": "EBICS {0}; {1}".format(download_name, candidate.get("transaction_reference") or ""),
        "auto_submit": False, "party_iban": candidate.get("party_iban"),
        "company": connection.company,
    }
    savepoint = "ebics_booking_" + sha256(reference.encode()).hexdigest()[:20]
    frappe.db.savepoint(savepoint)
    try:
        discount = _contract_discount(invoice, amount, candidate['date'])
        if discount:
            payment = _discount_payment(invoice, doctype, candidate, statement, amount, download_name)
        else:
            result = make_payment_entry(**payload)
            payment = frappe.get_doc("Payment Entry", result["payment_entry"])
        references = payment.references or []
        if (payment.docstatus != 0 or payment.company != connection.company
                or payment.party != candidate["party_match"]
                or _money(payment.paid_amount) != amount
                or _money(payment.received_amount) != amount
                or Decimal(str(payment.difference_amount or 0)) != 0
                or Decimal(str(payment.unallocated_amount or 0)) != 0
                or (not discount and payment.deductions) or len(references) != 1
                or (discount and sum(Decimal(str(d.amount)) for d in payment.deductions) != discount)
                or references[0].reference_doctype != doctype
                or references[0].reference_name != invoice.name
                or _money(references[0].allocated_amount) != amount + discount):
            raise BankFileError("Candidate payment differs from the confirmed bank and invoice amount")
        payment.submit()
        frappe.db.commit()
        return True
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _process_download(connection, record):
    if record.ack_state != "Confirmed":
        raise BankFileError("Bank receipt must be confirmed before automatic booking")
    payload = _verify_receipt(record)
    if record.new_file_hashes_json:
        new_hashes = json.loads(record.new_file_hashes_json)
        if (not isinstance(new_hashes, list) or any(not isinstance(item, str) for item in new_hashes)
                or len(new_hashes) > 64):
            raise BankFileError("EBICS new-file metadata is invalid")
    else:
        # Old receipts predate per-file metadata. They are only replayed after
        # their durable bank receipt; the ledger reference guard still applies.
        new_hashes = list(dict.fromkeys(digest for _, digest in _receipt_files(payload).values()))
    if not new_hashes:
        record.db_set("processing_state", "Duplicate", commit=True)
        return {"booked": 0, "review": 0}
    preview = _preview(payload, connection, included_hashes=new_hashes, profile=record.profile)
    booked = review = 0
    for statement in preview["statements"]:
        for entry in statement["entries"]:
            if not entry['issues'] and _already_booked_entry(connection, statement, entry):
                continue
            try:
                if _book_exact_match(connection, statement, entry, record.name):
                    booked += 1
                else:
                    review += 1
            except Exception as error:
                # An isolated candidate is never allowed to book a difference or
                # block archiving the other bank entries. It stays visible for review.
                review += 1
                frappe.log_error(title="EBICS candidate requires review", message=type(error).__name__)
    record.db_set({"booked_count": int(record.booked_count or 0) + booked, "review_count": review,
                   "processing_state": "Review" if review else "Completed"}, commit=True)
    return {"booked": booked, "review": review}


def sync_connection(connection_name, debug=False):
    """Drain current pending bank files, with durable receipts and no payments."""
    lock = frappe.cache().lock("ebics-download:" + connection_name, timeout=3600, blocking_timeout=0)
    if not lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        connection = frappe.get_doc("ebics Connection", connection_name)
        if not connection.enable_sync or not connection.activated or connection.ebics_version != "H005":
            return {"status": "disabled"}
        _statement_profile(connection)
        reader = _configured_reader(connection)
        waiting = frappe.get_all("EBICS Download", filters={"connection": connection.name,
                                                          "ack_state": "Confirmed", "processing_state": ("in", ["Pending", "Review"])},
                                 pluck="name", order_by="modified asc, creation asc", limit_page_length=100)
        for name in waiting:
            _process_download(connection, frappe.get_doc("EBICS Download", name))
        repeated_payloads = set()
        duplicates = 0
        pending = _pending_receipt(connection)
        if pending:
            # Resolve an interrupted bank acknowledgement before any other BTD.
            resumed = _download_pending(connection, frappe.utils.getdate(pending.requested_date), reader=reader)
            if resumed.processing_state == "Pending":
                _process_download(connection, resumed)
            elif resumed.processing_state == "Duplicate":
                repeated_payloads.add(resumed.payload_sha256)
                duplicates += 1
        known_files = _known_files(connection)
        # Each BTD without a date filter returns a bank-pending file set. Keep
        # fetching until the bank reports no data, but bound each scheduler run.
        # The request date is audit metadata only, never a file/date cursor.
        fetched = 0
        exhausted = True
        for _ in range(MAX_DOWNLOADS_PER_RUN):
            record = _download_pending(connection, frappe.utils.getdate(), known_files, reader=reader)
            if record is None:
                exhausted = False
                break
            if record.processing_state == "Duplicate":
                duplicates += 1
                if record.payload_sha256 in repeated_payloads:
                    return {"status": "repeated_duplicate", "downloads_fetched": fetched,
                            "duplicate_receipts": duplicates}
                repeated_payloads.add(record.payload_sha256)
                continue
            _process_download(connection, record)
            fetched += 1
        result = {"status": "limit_reached" if exhausted else "ok", "downloads_fetched": fetched}
        if duplicates:
            result["duplicate_receipts"] = duplicates
        return result
    finally:
        lock.release()


def reconcile_historical_receipts(connection_name, apply=False):
    """Audit and optionally mark old repeated receipts; never delete bank data.

    Default is read-only. An explicit boolean True updates only deduplication
    metadata/status after validating every archived receipt in chronological
    order. Existing booked entries are never silently reclassified.
    """
    if type(apply) is not bool:
        raise ValueError("apply must be a boolean")
    lock = frappe.cache().lock("ebics-download:" + connection_name, timeout=300, blocking_timeout=0)
    if not lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        connection = frappe.get_doc("ebics Connection", connection_name)
        known_files = {}
        changed = duplicate = 0
        offset = 0
        while offset < MAX_ARCHIVED_DOWNLOADS:
            names = frappe.get_all("EBICS Download", filters={"connection": connection.name,
                                                            "profile": PROFILE, "ack_state": "Confirmed"},
                                   pluck="name", order_by="creation asc, name asc",
                                   limit_start=offset, limit_page_length=100)
            for name in names:
                record = frappe.get_doc("EBICS Download", name)
                metadata, new_hashes = _classify_files(_verify_receipt(record), known_files)
                if not new_hashes:
                    if int(record.booked_count or 0):
                        raise BankFileError("Historical duplicate with booked entries requires manual review")
                    duplicate += 1
                    target_state = "Duplicate"
                else:
                    if record.processing_state == "Duplicate":
                        raise BankFileError("A unique historical receipt is marked duplicate")
                    target_state = record.processing_state
                updates = {key: value for key, value in metadata.items() if record.get(key) != value}
                if record.processing_state != target_state:
                    updates["processing_state"] = target_state
                if updates:
                    changed += 1
                    if apply is True:
                        record.db_set(updates, commit=False)
                for digest in new_hashes:
                    known_files[digest] = name
            if len(names) < 100:
                if apply is True:
                    frappe.db.commit()
                return {"status": "applied" if apply is True else "dry_run",
                        "receipts_checked": offset + len(names), "receipts_changed": changed,
                        "duplicate_receipts": duplicate}
            offset += len(names)
        raise BankFileError("EBICS archive exceeds safe reconciliation limit")
    except Exception:
        if apply is True:
            frappe.db.rollback()
        raise
    finally:
        lock.release()


def sync(debug=False):
    for row in frappe.get_all("ebics Connection", filters={"enable_sync": 1}, pluck="name"):
        try:
            result = sync_connection(row, debug=debug)
            if result.get("status") in ("limit_reached", "repeated_duplicate"):
                frappe.log_error(title="EBICS retrieval needs bank review",
                                 message="Connection {0}: {1}; downloads {2}, duplicates {3}".format(
                                     row, result["status"], result.get("downloads_fetched", 0),
                                     result.get("duplicate_receipts", 0)))
        except Exception as error:
            frappe.log_error(title="EBICS retrieval failed", message=type(error).__name__)
