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
from frappe.exceptions import QueryDeadlockError
from frappe.utils import validate_iban

from erpnextswiss.scripts.bank_camt_preview import preview_camt_archive
from erpnextswiss.scripts.bank_file_admission import BankFileError, MAX_FILE_BYTES, MAX_TOTAL_BYTES, read_bank_archive


PROFILE = "camt.053.001.08"
STATEMENT_PROFILES = {"04": "camt.053.001.04", "08": PROFILE}
MOVEMENT_PROFILES = set(STATEMENT_PROFILES.values()) | {"camt.054.001.08"}
MAX_DOWNLOADS_PER_RUN = 14
MAX_ARCHIVED_DOWNLOADS = 10000
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_RECEIPT_NAME = re.compile(r"statement-[0-9]{3}\.xml\Z")
_MOVEMENT_ID_FIELD = "custom_ebics_movement_id"
_MOVEMENT_FINGERPRINT_FIELD = "custom_ebics_movement_fingerprint"
_MOVEMENT_SOURCE_FIELD = "custom_ebics_source_download"
_MOVEMENT_ROW_FIELDS = [
    "name", "docstatus", "date", "company", "bank_account", "currency", "deposit", "withdrawal",
    "transaction_id", "reference_number", "modified", _MOVEMENT_ID_FIELD,
    _MOVEMENT_FINGERPRINT_FIELD, _MOVEMENT_SOURCE_FIELD,
]


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


def _normal_reference(value):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()
    if not value or value.upper().replace(" ", "") in {"NOTPROVIDED", "NONREF"}:
        return None
    return value


def _one_reference(values):
    normalized = list(dict.fromkeys(value for item in (values or [])
                                    for value in [_normal_reference(item)] if value))
    return normalized[0] if len(normalized) == 1 else None


def _movement_references(entry):
    """Keep every unambiguous bank ID so 053/054 enrichment retains its lineage."""
    references = entry.get("bank_reference_ids") or {}
    detail_count = int(entry.get("detail_count") or 0)
    priorities = []
    if detail_count == 1:
        priorities.extend((
            ("uetr", references.get("uetr")),
            ("detail_account_service_reference",
             references.get("detail_account_service_reference")),
        ))
    priorities.extend((
        ("account_service_reference", references.get("account_service_reference")
         or [entry.get("reference")]),
        ("entry_reference", references.get("entry_reference")
         or [entry.get("entry_reference")]),
    ))
    if detail_count == 1:
        priorities.extend((
            ("transaction_id", references.get("transaction_id")),
        ))
    result = []
    for kind, values in priorities:
        value = _one_reference(values)
        if value:
            result.append((kind, value))

    # PmtInfId identifies a payment group, not an individual bank movement.
    # Never promote it (or an ambiguous structured ID) through the old fallback.
    # Compatibility is limited to previews without structured reference fields.
    if not references and not result and detail_count == 1:
        value = _one_reference(entry.get("bank_detail_references"))
        if value:
            result.append(("detail_reference", value))
    return result


def _movement_reference(entry):
    references = _movement_references(entry)
    return references[0] if references else None


def _movement_identity(connection, statement, kind, reference):
    payload = ["ebics-movement-v1", connection.company, statement["account"],
               statement["currency"], kind, reference.upper()]
    return "EBICS-MOV-" + sha256(json.dumps(payload, ensure_ascii=False,
                                             separators=(",", ":")).encode()).hexdigest()


def _movement_fingerprint(connection, statement, entry, kind, reference):
    amount = _money(entry.get("amount"))
    if amount is None:
        return None
    payload = {
        "version": 2,
        "company": connection.company,
        "account": statement["account"],
        "currency": statement["currency"],
        "booking_date": (entry.get("booking_date") or "")[:10],
        "value_date": (entry.get("value_date") or "")[:10],
        "amount": format(amount, "f"),
        "credit_debit": entry.get("credit_debit"),
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def _statement_bank_account(connection, statement, *, current=False):
    rows = frappe.db.get_values("Bank Account", filters={
        "account": statement["account"], "is_company_account": 1,
    }, fieldname=["name", "company"], as_dict=True, limit=2, for_update=current)
    if (len(rows) != 1 or rows[0].get("company") != connection.company):
        raise BankFileError("The ledger account has no unique company Bank Account mapping")
    return rows[0]["name"]


def _movement_description(entry, reference):
    values = []
    for candidate in entry.get("candidates") or []:
        for key in ("party_name", "transaction_reference"):
            value = _normal_reference(candidate.get(key))
            if value and value not in values:
                values.append(value)
    if not values:
        values.append(reference)
    return " | ".join(values)[:1000]


def _movement_party(entry):
    candidates = entry.get("candidates") or []
    parties = {(candidate.get("party_name"), candidate.get("party_iban"))
               for candidate in candidates
               if candidate.get("party_name") or candidate.get("party_iban")}
    if len(parties) != 1:
        return {}
    bank_name, bank_iban = next(iter(parties))
    # Some legacy matcher results contain display placeholders instead of
    # party data. Keep the verified movement even when this enrichment is absent.
    party = {}
    if bank_name and bank_name not in ("???", "not found"):
        party["bank_party_name"] = bank_name
    if isinstance(bank_iban, str) and validate_iban(bank_iban):
        party["bank_party_iban"] = bank_iban
    return party


def _movement_shape_matches(row, bank_account, statement, entry, *, company=None):
    amount = _money(entry.get("amount"))
    if amount is None:
        return False
    try:
        deposit = Decimal(str(row.get("deposit") or 0)).quantize(Decimal("0.01"))
        withdrawal = Decimal(str(row.get("withdrawal") or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    expected_deposit = amount if entry.get("credit_debit") == "CRDT" else Decimal("0.00")
    expected_withdrawal = amount if entry.get("credit_debit") == "DBIT" else Decimal("0.00")
    return (row.get("bank_account") == bank_account
            and (company is None or row.get("company") == company)
            and row.get("currency") == statement.get("currency")
            and str(row.get("date"))[:10] == (entry.get("booking_date") or "")[:10]
            and deposit == expected_deposit and withdrawal == expected_withdrawal)


def _movement_snapshot(row):
    """Compare the entire claim and native reference state under a current row lock."""
    values = {field: None if row.get(field) is None else str(row.get(field))
              for field in _MOVEMENT_ROW_FIELDS}
    return sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _movement_rows(doctype, filters, fields, *, current, limit):
    # A regular repeatable-read snapshot remains stale even after another SELECT
    # acquired a lock. Every apply-path lookup must itself be a locking read.
    return frappe.db.get_values(doctype, filters, fields, as_dict=True,
                                for_update=current, limit=limit)


def _movement_plan(connection, statement, entry, *, current=False):
    if (entry.get("issues") or entry.get("status") != "BOOK"
            or entry.get("credit_debit") not in ("CRDT", "DBIT")
            or _money(entry.get("amount")) is None
            or not (entry.get("booking_date") or "")[:10]):
        return {"state": "review", "reason": "entry_not_safe_to_mirror"}
    references = _movement_references(entry)
    if not references:
        return {"state": "review", "reason": "missing_unique_bank_reference"}
    kind, reference = references[0]
    bank_account = _statement_bank_account(connection, statement, current=current)
    identity = _movement_identity(connection, statement, kind, reference)
    aliases = sorted({_movement_identity(connection, statement, key, value)
                      for key, value in references})
    fingerprint = _movement_fingerprint(connection, statement, entry, kind, reference)
    base = {
        "identity": identity, "fingerprint": fingerprint, "kind": kind,
        "reference": reference, "bank_account": bank_account, "aliases": aliases,
        "entry_sha256": sha256(entry["xml"].encode("utf-8")).hexdigest(),
        "description": _movement_description(entry, reference),
        "party": _movement_party(entry),
    }
    claims = _movement_rows("EBICS Movement Alias", {"alias_id": ("in", aliases)},
                            ["alias_id", "bank_transaction", "company", "bank_account", "account", "currency"],
                            current=current, limit=len(aliases) + 1)
    if any(claim.get("company") != connection.company
           or claim.get("bank_account") != bank_account
           or claim.get("account") != statement["account"]
           or claim.get("currency") != statement["currency"] for claim in claims):
        return {**base, "state": "review", "reason": "bank_alias_scope_conflict"}
    existing = _movement_rows("Bank Transaction", {_MOVEMENT_ID_FIELD: ("in", aliases)},
                               _MOVEMENT_ROW_FIELDS, current=current, limit=len(aliases) + 1)
    targets = {claim["bank_transaction"] for claim in claims} | {row["name"] for row in existing}
    if len(targets) > 1:
        return {**base, "state": "review", "reason": "bank_alias_multiple_movements"}
    if targets:
        existing = _movement_rows("Bank Transaction", {"name": next(iter(targets))},
                                   _MOVEMENT_ROW_FIELDS, current=current, limit=2)
        if len(existing) != 1:
            return {**base, "state": "review", "reason": "bank_alias_target_missing"}
    if existing:
        row = existing[0]
        if (row.get("docstatus") != 1 or not row.get(_MOVEMENT_ID_FIELD)
                or row.get(_MOVEMENT_FINGERPRINT_FIELD) != fingerprint
                or not _movement_shape_matches(row, bank_account, statement, entry, company=connection.company)):
            return {**base, "state": "review", "reason": "bank_identity_content_conflict"}
        return {**base, "identity": row[_MOVEMENT_ID_FIELD],
                "state": "existing", "bank_transaction": row["name"],
                "expected_snapshot": _movement_snapshot(row)}

    # A cancelled historical row may predate EBICS IDs/alias claims. Its bank
    # reference still reserves that movement: never silently undo a user's
    # cancellation through replay, even if date/amount were later changed.
    for field in ("transaction_id", "reference_number"):
        cancelled = _movement_rows("Bank Transaction", {
            "bank_account": bank_account, "docstatus": 2,
            field: ("in", list({value for _, value in references})),
        }, ["name"], current=current, limit=1)
        if cancelled:
            return {**base, "state": "review", "reason": "cancelled_legacy_bank_reference"}

    # Protect historical native rows. Adopt only an exact bank-reference match;
    # a same-day/same-amount row with another reference is a review case, never
    # evidence that two genuine equal payments are the same movement.
    candidates = _movement_rows("Bank Transaction", {
        "bank_account": bank_account, "date": (entry.get("booking_date") or "")[:10],
        "currency": statement["currency"], "docstatus": ("<", 2),
    }, _MOVEMENT_ROW_FIELDS, current=current, limit=21)
    if len(candidates) >= 21:
        return {**base, "state": "review", "reason": "legacy_candidate_limit"}
    shape = [row for row in candidates
             if not row.get(_MOVEMENT_ID_FIELD)
             and _movement_shape_matches(row, bank_account, statement, entry)]
    if any(row.get("company") != connection.company for row in shape):
        return {**base, "state": "review", "reason": "legacy_bank_transaction_company_conflict"}
    normalized = {value.upper() for _, value in references}
    exact = [row for row in shape if normalized.intersection({
        value.upper() for value in (_normal_reference(row.get("transaction_id")),
                                    _normal_reference(row.get("reference_number"))) if value
    }) and row.get("docstatus") == 1
        and not row.get(_MOVEMENT_FINGERPRINT_FIELD) and not row.get(_MOVEMENT_SOURCE_FIELD)]
    if len(exact) == 1:
        return {**base, "state": "adopt", "bank_transaction": exact[0]["name"],
                "expected_snapshot": _movement_snapshot(exact[0])}
    if exact or shape:
        return {**base, "state": "review", "reason": "legacy_bank_transaction_conflict"}
    return {**base, "state": "create"}


def _claim_movement_aliases(connection, statement, source_download, transaction, plan):
    for alias in plan["aliases"]:
        claims = _movement_rows("EBICS Movement Alias", {"alias_id": alias},
                                ["bank_transaction"], current=True, limit=2)
        if claims:
            if len(claims) != 1 or claims[0]["bank_transaction"] != transaction.name:
                raise BankFileError("Bank reference was claimed by another movement")
            continue
        # Both the immutable document name and alias_id unique index enforce one
        # persistent owner, including across connections and concurrent workers.
        frappe.get_doc({
            "doctype": "EBICS Movement Alias", "alias_id": alias,
            "bank_transaction": transaction.name, "company": connection.company,
            "bank_account": plan["bank_account"], "account": statement["account"],
            "currency": statement["currency"], "source_download": source_download,
        }).insert(ignore_permissions=True)


def _apply_movement_plan(connection, statement, entry, source_download, plan):
    if plan["state"] not in {"create", "adopt", "existing"}:
        return plan
    savepoint = "ebics_movement_" + plan["identity"][-20:]
    frappe.db.savepoint(savepoint)
    try:
        # Serialize by native account, not connection or preferred reference.
        # Different EBICS connections/053/054 can share the same bank account.
        locked_account = _statement_bank_account(connection, statement, current=True)
        if locked_account != plan["bank_account"]:
            raise BankFileError("Bank Account mapping changed during EBICS mirroring")
        if plan["state"] == "adopt":
            transaction = frappe.get_doc("Bank Transaction", plan["bank_transaction"], for_update=True)
            if (_movement_snapshot(transaction) != plan["expected_snapshot"]
                    or transaction.docstatus >= 2
                    or any(transaction.get(field) for field in (
                        _MOVEMENT_ID_FIELD, _MOVEMENT_FINGERPRINT_FIELD, _MOVEMENT_SOURCE_FIELD))
                    or not _movement_shape_matches(transaction, plan["bank_account"], statement, entry,
                                                   company=connection.company)):
                raise BankFileError("Historical Bank Transaction changed during EBICS adoption")
        fresh = _movement_plan(connection, statement, entry, current=True)
        if fresh["state"] == "review":
            raise BankFileError("Bank movement changed during EBICS mirroring: " + fresh["reason"])
        if plan["state"] == "existing" and (
                fresh["state"] != "existing" or fresh["bank_transaction"] != plan["bank_transaction"]):
            raise BankFileError("Existing Bank Transaction claim changed during EBICS mirroring")
        if plan["state"] == "adopt" and (
                fresh["state"] != "adopt" or fresh["bank_transaction"] != plan["bank_transaction"]
                or fresh["expected_snapshot"] != plan["expected_snapshot"]):
            raise BankFileError("Historical Bank Transaction claim changed during EBICS adoption")
        plan = fresh
        if plan["state"] == "existing":
            transaction = frappe.get_doc("Bank Transaction", plan["bank_transaction"], for_update=True)
            if _movement_snapshot(transaction) != plan["expected_snapshot"]:
                raise BankFileError("Bank Transaction changed while retaining bank references")
            _claim_movement_aliases(connection, statement, source_download, transaction, plan)
            return plan
        if plan["state"] == "adopt":
            transaction = frappe.get_doc("Bank Transaction", plan["bank_transaction"], for_update=True)
            if _movement_snapshot(transaction) != plan["expected_snapshot"]:
                raise BankFileError("Historical Bank Transaction changed before EBICS adoption")
            transaction.db_set({
                _MOVEMENT_ID_FIELD: plan["identity"],
                _MOVEMENT_FINGERPRINT_FIELD: plan["fingerprint"],
                _MOVEMENT_SOURCE_FIELD: source_download,
            }, commit=False)
            _claim_movement_aliases(connection, statement, source_download, transaction, plan)
            return {**plan, "state": "adopted"}
        if plan["state"] != "create":
            return plan
        amount = _money(entry["amount"])
        values = {
            "doctype": "Bank Transaction",
            "company": connection.company,
            "date": (entry.get("booking_date") or "")[:10],
            "bank_account": plan["bank_account"],
            "currency": statement["currency"],
            "deposit": float(amount) if entry["credit_debit"] == "CRDT" else 0,
            "withdrawal": float(amount) if entry["credit_debit"] == "DBIT" else 0,
            "description": plan["description"],
            "reference_number": plan["reference"],
            "transaction_id": plan["reference"][:140],
            "transaction_type": "EBICS",
            _MOVEMENT_ID_FIELD: plan["identity"],
            _MOVEMENT_FINGERPRINT_FIELD: plan["fingerprint"],
            _MOVEMENT_SOURCE_FIELD: source_download,
            **plan["party"],
        }
        transaction = frappe.get_doc(values).insert(ignore_permissions=True)
        transaction.submit()
        if (transaction.docstatus != 1
                or not _movement_shape_matches(transaction, plan["bank_account"], statement, entry,
                                               company=connection.company)
                or transaction.get(_MOVEMENT_ID_FIELD) != plan["identity"]
                or transaction.get(_MOVEMENT_FINGERPRINT_FIELD) != plan["fingerprint"]
                or transaction.payment_entries):
            raise BankFileError("Native Bank Transaction differs from the verified EBICS movement")
        _claim_movement_aliases(connection, statement, source_download, transaction, plan)
        return {**plan, "state": "created", "bank_transaction": transaction.name}
    except QueryDeadlockError:
        # MariaDB has already discarded the transaction and its savepoints.
        raise
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _mirror_bank_entry(connection, statement, entry, source_download, *, apply, planned=None):
    plan = _movement_plan(connection, statement, entry)
    if plan["state"] == "review":
        return plan
    if not apply:
        if planned is not None:
            previous = [planned[alias] for alias in plan["aliases"] if alias in planned]
            if previous:
                if any(item["fingerprint"] != plan["fingerprint"]
                       or item["identity"] != previous[0]["identity"] for item in previous):
                    return {**plan, "state": "review", "reason": "bank_identity_content_conflict"}
                plan = {**plan, "identity": previous[0]["identity"]}
            for alias in plan["aliases"]:
                planned[alias] = plan
            if previous:
                return {**plan, "state": "existing_in_plan"}
        return {**plan, "state": "would_" + plan["state"]}
    return _apply_movement_plan(connection, statement, entry, source_download, plan)


def _movement_map_row(record, statement, entry, result):
    return {
        "profile": record.profile,
        "file_sha256": statement.get("file_sha256"),
        "statement_id": statement.get("statement_id"),
        "statement_index": statement.get("statement_index"),
        "entry_index": entry.get("index"),
        "entry_sha256": result.get("entry_sha256")
            or sha256(entry["xml"].encode("utf-8")).hexdigest(),
        "movement_id": result.get("identity"),
        "bank_transaction": result.get("bank_transaction"),
        "state": result["state"],
        "reason": result.get("reason"),
    }


def _new_hashes_for_record(record, payload):
    if record.new_file_hashes_json:
        new_hashes = json.loads(record.new_file_hashes_json)
        if (not isinstance(new_hashes, list) or any(not isinstance(item, str) for item in new_hashes)
                or len(new_hashes) > 64):
            raise BankFileError("EBICS new-file metadata is invalid")
        return new_hashes
    # Old receipts predate per-file metadata. The movement identity still
    # protects replay; historical backfill first performs a read-only plan.
    return list(dict.fromkeys(digest for _, digest in _receipt_files(payload).values()))


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


def _strict_invoice_candidates(connection, statement, entry):
    """Return fully referenced live invoices only when bank, matcher and ledger agree."""
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
    if (not isinstance(names, list) or not 1 <= len(names) <= 20
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)):
        return None
    doctype = "Sales Invoice" if direction == "CRDT" else "Purchase Invoice"
    party_field = "customer" if direction == "CRDT" else "supplier"
    reference_text = candidate.get("transaction_reference") or ""
    invoices = []
    outstanding_total = Decimal("0.00")
    for name in names:
        invoice = frappe.get_doc(doctype, name)
        outstanding = _money(invoice.outstanding_amount)
        if (invoice.docstatus != 1 or invoice.company != connection.company
                or invoice.get(party_field) != candidate.get("party_match")
                or invoice.currency != statement["currency"] or outstanding is None):
            return None
        accepted = {str(invoice.name)}
        accepted.update(str(value) for value in (invoice.get("bill_no"), invoice.get("esr_reference"),
                                                 invoice.get("esr_reference_number"),
                                                 invoice.get("reference_number_full"))
                        if value and len(str(value)) >= 6)
        if not any(_reference_mentions(value, reference_text) for value in accepted):
            return None
        outstanding_total += outstanding
        invoices.append(invoice)
    if outstanding_total != _money(candidate.get("matched_amount")):
        return None
    if len(invoices) == 1:
        if _contract_discount(invoices[0], amount, candidate["date"]) is None:
            return None
    elif amount != outstanding_total:
        # Multi-invoice payments never infer discounts, partial allocations or
        # rounding differences. The bank amount must equal the exact open sum.
        return None
    return candidate, doctype, invoices, amount


def _strict_invoice_candidate(connection, statement, entry):
    """Backward-compatible single-invoice matcher used by probes and tests."""
    matched = _strict_invoice_candidates(connection, statement, entry)
    if matched is None or len(matched[2]) != 1:
        return None
    candidate, doctype, invoices, amount = matched
    return candidate, doctype, invoices[0], amount


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
        allocated = [_money(reference.get('allocated_amount')) for reference in references]
        if (payment.docstatus != 1 or payment.company != connection.company
                or payment.payment_type != payment_type
                or payment.get(bank_field) != statement['account']
                or str(payment.posting_date)[:10] != posting_date
                or not _cash_shape_matches(payment, amount, statement['currency'])
                or payment.party_type != party_type or payment.party != candidate['party_match']
                or not _remarks_mention_ebanking_id(payment.get('remarks'), identity)
                or not 1 <= len(references) <= 20
                or any(reference.get('reference_doctype') != expected_doctype
                       for reference in references)
                or any(value is None for value in allocated)
                or sum(allocated) != amount):
            continue
        party_field = 'customer' if incoming else 'supplier'
        invoices = [frappe.get_doc(expected_doctype, reference.get('reference_name'))
                    for reference in references]
        if (len({invoice.name for invoice in invoices}) != len(invoices)
                or any(invoice.docstatus != 1 or invoice.company != connection.company
                       or invoice.get(party_field) != candidate['party_match']
                       or invoice.currency != statement['currency']
                       or not _zero_money(invoice.outstanding_amount)
                       or not _invoice_reference_matches(invoice, candidate['transaction_reference'])
                       for invoice in invoices)):
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
    """Submit an exact payment inside the caller's receipt transaction."""
    matched = _strict_invoice_candidates(connection, statement, entry)
    if matched is None:
        return False
    candidate, doctype, invoices, amount = matched
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
        "party": candidate["party_match"], "references": repr([invoice.name for invoice in invoices]),
        "remarks": "EBICS {0}; {1}".format(download_name, candidate.get("transaction_reference") or ""),
        "auto_submit": False, "party_iban": candidate.get("party_iban"),
        "company": connection.company,
    }
    invoice = invoices[0]
    discount = _contract_discount(invoice, amount, candidate['date']) if len(invoices) == 1 else Decimal('0.00')
    if discount:
        payment = _discount_payment(invoice, doctype, candidate, statement, amount, download_name)
    else:
        result = make_payment_entry(**payload)
        payment = frappe.get_doc("Payment Entry", result["payment_entry"])
    references = payment.references or []
    expected_allocations = {
        invoice.name: _money(invoice.outstanding_amount)
        for invoice in invoices
    }
    actual_allocations = {
        reference.reference_name: _money(reference.allocated_amount)
        for reference in references
        if reference.reference_doctype == doctype
    }
    if (payment.docstatus != 0 or payment.company != connection.company
            or payment.party != candidate["party_match"]
            or _money(payment.paid_amount) != amount
            or _money(payment.received_amount) != amount
            or Decimal(str(payment.difference_amount or 0)) != 0
            or Decimal(str(payment.unallocated_amount or 0)) != 0
            or (not discount and payment.deductions) or len(references) != len(invoices)
            or (discount and sum(Decimal(str(d.amount)) for d in payment.deductions) != discount)
            or len(actual_allocations) != len(invoices)
            or actual_allocations != expected_allocations):
        raise BankFileError("Candidate payment differs from the confirmed bank and invoice amount")
    payment.submit()
    return True


def _process_download(connection, record):
    """Process a confirmed receipt, restarting the transaction on a database conflict."""
    initial_booked = int(record.booked_count or 0)
    for attempt in range(3):
        try:
            review = _process_confirmed_download(connection, record)
        except QueryDeadlockError:
            frappe.db.rollback()
            if attempt == 2:
                raise
            record = frappe.get_doc("EBICS Download", record.name)
        else:
            return {"booked": int(record.booked_count or 0) - initial_booked, "review": review}


def _process_confirmed_download(connection, record):
    if record.ack_state != "Confirmed":
        raise BankFileError("Bank receipt must be confirmed before automatic booking")
    payload = _verify_receipt(record)
    new_hashes = _new_hashes_for_record(record, payload)
    if not new_hashes:
        record.db_set("processing_state", "Duplicate", commit=True)
        return 0
    preview = _preview(payload, connection, included_hashes=new_hashes, profile=record.profile)
    review = 0
    mirrored = set()
    movement_map = []
    for statement in preview["statements"]:
        for entry in statement["entries"]:
            movement_review = False
            try:
                movement = _mirror_bank_entry(connection, statement, entry, record.name, apply=True)
            except QueryDeadlockError:
                raise
            except Exception as error:
                movement = {"state": "review", "reason": type(error).__name__}
                frappe.log_error(title="EBICS movement requires review", message=type(error).__name__)
            movement_map.append(_movement_map_row(record, statement, entry, movement))
            if movement.get("bank_transaction"):
                mirrored.add(movement["bank_transaction"])
            if movement["state"] == "review":
                review += 1
                movement_review = True
            if not entry['issues'] and _already_booked_entry(connection, statement, entry):
                continue
            savepoint = "ebics_entry_booking"
            frappe.db.savepoint(savepoint)
            try:
                if _book_exact_match(connection, statement, entry, record.name):
                    # A resumed download skips posted payments, so their count must
                    # already be durable even if finalizing the receipt is interrupted.
                    booked_count = int(record.booked_count or 0) + 1
                    frappe.db.set_value("EBICS Download", record.name, "booked_count",
                                        booked_count, update_modified=False)
                    frappe.db.commit()
                    record.booked_count = booked_count
                elif not movement_review:
                    review += 1
            except QueryDeadlockError:
                raise
            except Exception as error:
                frappe.db.rollback(save_point=savepoint)
                # An isolated candidate is never allowed to book a difference or
                # block archiving the other bank entries. It stays visible for review.
                if not movement_review:
                    review += 1
                frappe.log_error(title="EBICS candidate requires review", message=type(error).__name__)
    # Payment progress is committed per entry above. Persist the movement map
    # independently so a retry can reconstruct it from the existing native rows.
    record.db_set({"review_count": review, "mirrored_count": len(mirrored),
                   "movement_map_json": json.dumps(movement_map, ensure_ascii=False,
                                                   sort_keys=True, separators=(",", ":")),
                   "processing_state": "Review" if review else "Completed"}, commit=True)
    return review


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


def _movement_map_key(row):
    if not isinstance(row, dict):
        raise BankFileError("Invalid historical movement map row")
    digest = row.get("file_sha256")
    position = row.get("statement_index"), row.get("entry_index")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or any(type(index) is not int or index < 0 for index in position)):
        raise BankFileError("Invalid historical movement map position")
    return digest, *position


def _scoped_receipt_map(record, movement_map):
    try:
        existing = json.loads(record.movement_map_json or "[]")
    except (TypeError, ValueError) as exc:
        raise BankFileError("Invalid historical movement map") from exc
    if not isinstance(existing, list):
        raise BankFileError("Invalid historical movement map")
    selected = {_movement_map_key(row) for row in movement_map}
    if len(selected) != len(movement_map):
        raise BankFileError("Duplicate position in the selected bank movement map")
    return [row for row in existing if _movement_map_key(row) not in selected] + movement_map


def _historical_movement_pass(connection, *, apply, ledger_account=None):
    planned = {} if not apply else None
    scoped = ledger_account is not None
    plan_hash = sha256() if scoped and not apply else None
    totals = {
        "receipts_checked": 0, "entries_checked": 0, "created": 0,
        "adopted": 0, "existing": 0, "review": 0,
    }
    reasons = {}
    offset = 0
    while offset < MAX_ARCHIVED_DOWNLOADS:
        names = frappe.get_all("EBICS Download", filters={
            "connection": connection.name,
            "profile": ("in", sorted(MOVEMENT_PROFILES)),
            "ack_state": "Confirmed",
            "processing_state": ("!=", "Duplicate"),
        }, pluck="name", order_by="creation asc, name asc",
            limit_start=offset, limit_page_length=100)
        for name in names:
            record = frappe.get_doc("EBICS Download", name)
            payload = _verify_receipt(record)
            hashes = _new_hashes_for_record(record, payload)
            preview = _preview(payload, connection, included_hashes=hashes, profile=record.profile)
            movement_map = []
            for statement in preview["statements"]:
                if scoped and statement.get("account") != ledger_account:
                    continue
                for entry in statement["entries"]:
                    result = _mirror_bank_entry(connection, statement, entry, record.name,
                                                apply=apply, planned=planned)
                    movement_map.append(_movement_map_row(record, statement, entry, result))
                    totals["entries_checked"] += 1
                    state = result["state"]
                    if state in ("created", "would_create"):
                        totals["created"] += 1
                    elif state in ("adopted", "would_adopt"):
                        totals["adopted"] += 1
                    elif state in ("existing", "existing_in_plan", "would_existing"):
                        totals["existing"] += 1
                    elif state == "review":
                        totals["review"] += 1
                        reason = result.get("reason") or "unknown"
                        reasons[reason] = reasons.get(reason, 0) + 1
            if scoped and movement_map:
                persisted = _scoped_receipt_map(record, movement_map)
                if plan_hash is not None:
                    row = [record.name, record.payload_sha256, record.movement_map_json, movement_map]
                    plan_hash.update(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode() + b"\n")
            else:
                persisted = movement_map
            if apply and (not scoped or movement_map):
                mirrored = {row["bank_transaction"] for row in persisted if row.get("bank_transaction")}
                record.db_set({
                    "mirrored_count": len(mirrored),
                    "movement_map_json": json.dumps(persisted, ensure_ascii=False,
                                                    sort_keys=True, separators=(",", ":")),
                }, commit=False)
            totals["receipts_checked"] += 1
        if len(names) < 100:
            totals["review_reasons"] = reasons
            if plan_hash is not None:
                totals["plan_sha256"] = plan_hash.hexdigest()
            return totals
        offset += len(names)
    raise BankFileError("EBICS archive exceeds safe movement-backfill limit")


def mirror_historical_bank_movements(connection_name, apply=False, *, bank_account=None,
                                     expected_plan_sha256=None):
    """Plan first, then mirror verified archives without touching GL or payments.

    The default is a bounded read-only dry run. An apply run is refused while
    any entry lacks a safe bank identity, conflicts with a changed bank ID or
    resembles an unadopted historical native Bank Transaction. Scoped writes
    also require the plan digest returned by a dry run for the same Bank Account.
    """
    if type(apply) is not bool:
        raise ValueError("apply must be a boolean")
    if bank_account is not None and (not isinstance(bank_account, str) or not bank_account):
        raise ValueError("bank_account must be a Bank Account name")
    if (apply and bank_account is not None and
            (not isinstance(expected_plan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256))):
        raise ValueError("A scoped apply requires the exact dry-run digest")
    if expected_plan_sha256 is not None and (not apply or bank_account is None):
        raise ValueError("The dry-run digest is only used for scoped apply")
    frappe.only_for(("Accounts Manager", "System Manager"))
    lock = frappe.cache().lock("ebics-download:" + connection_name, timeout=900,
                               blocking_timeout=0)
    if not lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        connection = frappe.get_doc("ebics Connection", connection_name)
        connection.check_permission("read")
        linked_accounts = _accounts(connection)
        scope = {}
        if bank_account is not None:
            selected = frappe.get_doc("Bank Account", bank_account)
            selected.check_permission("read")
            if (selected.company != connection.company or not selected.is_company_account or selected.disabled
                    or selected.account not in linked_accounts
                    or _statement_bank_account(connection, {"account": selected.account}) != selected.name):
                raise BankFileError("Bank Account does not match the EBICS connection")
            scope["ledger_account"] = selected.account
        plan = _historical_movement_pass(connection, apply=False, **scope)
        if not apply:
            return {"status": "dry_run", **plan}
        if plan["review"]:
            return {"status": "review_required", **plan}
        if scope and plan["plan_sha256"] != expected_plan_sha256:
            return {"status": "plan_changed", **plan}
        try:
            result = _historical_movement_pass(connection, apply=True, **scope)
            counts = ("receipts_checked", "entries_checked", "created", "adopted", "existing")
            if result["review"] or (scope and any(plan[key] != result[key] for key in counts)):
                raise BankFileError("The EBICS movement plan changed during backfill")
            frappe.db.commit()
            return {"status": "applied", **result}
        except Exception:
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
