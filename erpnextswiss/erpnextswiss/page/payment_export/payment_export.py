"""Direct payment and payroll exports with complete preflight and private originals."""
from decimal import Decimal
from hashlib import sha256
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate

from erpnextswiss.erpnextswiss.iso20022 import create_payment_file_name, is_qr_iban, normalize_iban
from erpnextswiss.erpnextswiss.swiss_payments import payment_amount, render_credit_transfers


@frappe.whitelist()
def get_payments():
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    return {"payments": frappe.get_list("Payment Entry",
        filters={"docstatus": 0, "payment_type": "Pay"},
        fields=["name", "posting_date", "received_amount", "party", "paid_from", "paid_to_account_currency"],
        order_by="posting_date, name", limit_page_length=0)}


def _truthy(value, default=False):
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "ja") if isinstance(value, str) else bool(value)


def _assert_expected_total(actual, expected_total):
    if expected_total is None or expected_total == "":
        return
    try:
        expected = Decimal(str(expected_total))
        if not expected.is_finite() or expected != actual:
            raise ValueError
    except (ValueError, ArithmeticError):
        frappe.throw(_("The expected total does not match the validated payment total."))


def _assert_expected_count(actual, expected_count, fieldname):
    if expected_count is None or expected_count == "":
        return
    if str(expected_count).strip() != str(actual):
        frappe.throw(_("The expected payment count does not match the selected entries."))


def _require_confirmed(confirm, action):
    if not _truthy(confirm):
        frappe.throw(_("Confirmation is required before {0}.").format(action))


def _parse_payment_names(payments):
    names = frappe.parse_json(payments) if isinstance(payments, str) else payments
    if not isinstance(names, (list, tuple)) or not names or len(names) > 1000:
        frappe.throw(_("Please select between 1 and 1000 payments."))
    if any(not isinstance(name, str) or not name.strip() for name in names):
        frappe.throw(_("Invalid payment selection."))
    return list(dict.fromkeys(names))


def _country_code(country):
    if not country:
        frappe.throw(_("The payment address needs an explicit country."))
    code = frappe.db.get_value("Country", country, "code")
    if not code and re.fullmatch(r"[A-Za-z]{2}", country):
        code = frappe.db.get_value("Country", {"code": country.lower()}, "code")
    if not code:
        frappe.throw(_("Unknown payment address country: {0}").format(frappe.utils.escape_html(country)))
    return code.upper()


def _street_parts(line):
    match = re.fullmatch(r"(.+?)\s+(\d+\S*)", (line or "").strip())
    return (match[1], match[2]) if match else ((line or "").strip(), "")


def get_billing_address(party_name, party_type="Supplier"):
    if party_type not in ("Company", "Customer", "Supplier", "Employee"):
        frappe.throw(_("Unsupported payment party type."))
    links = frappe.get_all("Dynamic Link", filters={"link_doctype": party_type,
        "link_name": party_name, "parenttype": "Address"}, fields=["parent"], order_by="parent")
    addresses = [frappe.get_doc("Address", row.parent) for row in links]
    addresses.sort(key=lambda doc: (
        not (doc.address_type == "Billing" and doc.is_primary_address),
        doc.address_type != "Billing", not bool(doc.is_primary_address), doc.name))
    return addresses[0] if addresses else None


def _party_address(party_name, party_type, display_name=None):
    address = get_billing_address(party_name, party_type)
    if address:
        street, building = _street_parts(address.address_line1)
        return dict(name=display_name or party_name, street=street, building=building,
                    pincode=address.pincode, city=address.city, country_code=_country_code(address.country))
    if party_type == "Employee":
        employee = frappe.get_doc("Employee", party_name)
        lines = [line.strip() for line in (employee.permanent_address or employee.current_address or "").splitlines() if line.strip()]
        if len(lines) >= 3:
            postcode, separator, city = lines[-2].partition(" ")
            if separator and city:
                street, building = _street_parts(lines[0])
                return dict(name=display_name or employee.employee_name, street=street, building=building,
                            pincode=postcode, city=city, country_code=_country_code(lines[-1]))
    frappe.throw(_("{0}: add a linked payment address with town and country. For an employee, a three-line address with the country on the last line is also supported.").format(frappe.utils.escape_html(party_name)))


def _debtor(company_name, account_name):
    account = frappe.get_doc("Account", account_name)
    if account.company != company_name or account.disabled or account.is_group or account.account_type != "Bank":
        frappe.throw(_("The paying account must be an active bank account of the selected company."))
    debtor = _party_address(company_name, "Company")
    debtor.update(iban=account.iban, bic=account.bic)
    return debtor


def _validate_payment_entries_for_export(payment_names):
    records, total = [], Decimal(0)
    company = None
    for name in payment_names:
        record = frappe.get_doc("Payment Entry", name)
        record.check_permission("write")
        record.check_permission("submit")
        if record.docstatus != 0 or record.payment_type != "Pay":
            frappe.throw(_("{0}: only draft outgoing payments can be exported. Previously exported originals remain attached to their payment entries.").format(frappe.utils.escape_html(name)))
        if company and company != record.company:
            frappe.throw(_("Select payment entries from one company per file."))
        company = record.company
        total += payment_amount(record.received_amount)
        records.append(record)
    return records, total


def _payment_row(record):
    method = record.transaction_type or "IBAN"
    iban = normalize_iban(record.iban)
    participant = normalize_iban(record.esr_participant_number)
    if method == "ESR":
        if not (is_qr_iban(participant) or is_qr_iban(iban)):
            frappe.throw(_("{0}: legacy ESR payments are no longer supported; enter the current IBAN or QR-IBAN.").format(frappe.utils.escape_html(record.name)))
        iban, method = participant if is_qr_iban(participant) else iban, "QRR"
    party = frappe.get_doc(record.party_type, record.party)
    display = party.get("employee_name") or party.get("supplier_name") or party.get("customer_name") or record.party
    return dict(name=record.name, execution_date=max(getdate(record.posting_date), getdate(nowdate())).isoformat(),
        amount=record.received_amount, currency=record.paid_to_account_currency,
        iban=iban, bic=record.bic, service_level=method, qr_reference=record.esr_reference,
        reference=record.reference_no or record.name,
        creditor=_party_address(record.party, record.party_type, display),
        debtor=_debtor(record.company, record.paid_from))


def _archive(result, doctype, names, file_name=None):
    from frappe.utils.file_manager import save_file
    file_name = file_name or create_payment_file_name(result["message_id"])
    urls = []
    for name in names:
        original = save_file(file_name, result["content"], doctype, name, is_private=1)
        urls.append(original.file_url)
    result.update(file_name=file_name, file_url=urls[0])
    return result


def _generate_payment_export(payments, expected_total=None, expected_count=None):
    names = _parse_payment_names(payments)
    # Sorted row locks serialize double clicks and concurrent API requests. Locks
    # remain held until Frappe commits the successful POST or rolls it back.
    for name in sorted(names):
        frappe.db.sql("SELECT name FROM `tabPayment Entry` WHERE name=%s FOR UPDATE", (name,))
    records, total = _validate_payment_entries_for_export(names)
    _assert_expected_total(total, expected_total)
    _assert_expected_count(len(records), expected_count, "expected_count")
    try:
        result = render_credit_transfers([_payment_row(record) for record in records])
    except ValueError as error:
        frappe.throw(str(error))
    # No partial export: every transaction and the complete Swiss schema must
    # pass before any document is submitted. Roll back even if a caller catches.
    savepoint = "swiss_payment_export"
    frappe.db.savepoint(savepoint)
    try:
        result = _archive(result, "Payment Entry", names)
        for record in records:
            record.submit()
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    result.update(payment_entries=names)
    return result


@frappe.whitelist(methods=["POST"])
def generate_payment_file(payments):
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    return _generate_payment_export(payments)


@frappe.whitelist(methods=["POST"])
def generate_payment_file_for_mcp(payments, expected_total=None, expected_count=None, confirm=0, reason=None):
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    _require_confirmed(confirm, "generating and submitting a payment export")
    result = _generate_payment_export(payments, expected_total, expected_count)
    return dict(result, success=True)


def _payroll_rows(payroll_record):
    names = frappe.get_all("Salary Slip", filters={"payroll_entry": payroll_record.name,
        "company": payroll_record.company, "docstatus": ["!=", 2]}, fields=["name"],
        order_by="employee, name", limit_page_length=0)
    if not names:
        frappe.throw(_("No submitted salary slips linked to this payroll entry were found."))
    employees = {row.employee for row in payroll_record.employees}
    debtor = _debtor(payroll_record.company, payroll_record.payment_account)
    rounding_disabled = cint(frappe.db.get_single_value("Payroll Settings", "disable_rounded_total"))
    rows, seen = [], set()
    for item in names:
        slip = frappe.get_doc("Salary Slip", item.name)
        slip.check_permission("read")
        if (slip.docstatus != 1 or slip.payroll_entry != payroll_record.name or slip.company != payroll_record.company
            or slip.employee not in employees or slip.employee in seen
            or getdate(slip.start_date) != getdate(payroll_record.start_date)
            or getdate(slip.end_date) != getdate(payroll_record.end_date)):
            frappe.throw(_("Salary slips do not match the employees and period of the selected payroll entry."))
        seen.add(slip.employee)
        amount = slip.net_pay if rounding_disabled or slip.rounded_total is None else slip.rounded_total
        if Decimal(str(amount or 0)) == 0:
            continue
        rows.append(dict(name=slip.name, execution_date=max(getdate(slip.posting_date), getdate(nowdate())).isoformat(),
            amount=amount, currency=slip.currency, iban=slip.bank_account_no, is_salary=True,
            service_level="IBAN", reference=slip.name, bic=frappe.db.get_value("Employee", slip.employee, "bic"),
            creditor=_party_address(slip.employee, "Employee", slip.employee_name), debtor=debtor))
    if seen != employees:
        frappe.throw(_("Not every employee in this payroll entry has a matching submitted salary slip. Complete the payroll before exporting."))
    return rows


@frappe.whitelist(methods=["POST"])
def generate_payment_file_from_payroll(payroll_entry):
    frappe.only_for(("Accounts User", "Accounts Manager", "HR Manager", "System Manager"))
    frappe.db.sql("SELECT name FROM `tabPayroll Entry` WHERE name=%s FOR UPDATE", (payroll_entry,))
    payroll = frappe.get_doc("Payroll Entry", payroll_entry)
    payroll.check_permission("write")
    if payroll.docstatus != 1:
        frappe.throw(_("Only a submitted payroll entry can be exported."))
    rows = _payroll_rows(payroll)
    return _immutable_payment_export(rows, "Payroll Entry", payroll.name, "payroll", ignore_execution_date=True)


def _immutable_payment_export(rows, doctype, name, kind, ignore_execution_date=False):
    # A later download must keep the bank's duplicate-detection identifiers.
    identity = [{key: value for key, value in row.items()
                 if not (ignore_execution_date and key == "execution_date")} for row in rows]
    fingerprint = sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    filename = "payments-" + kind + "-" + fingerprint[:24] + ".xml"
    originals = frappe.get_all("File", filters={"attached_to_doctype": doctype,
        "attached_to_name": name, "is_private": 1, "file_name": ["like", "payments-" + kind + "-%.xml"]},
        fields=["name", "file_name", "file_url"])
    if originals:
        matching = next((row for row in originals if row.file_name == filename), None)
        if not matching:
            frappe.throw(_("This document already has a payment original with different source data. Review the existing export before preparing a replacement."))
        from lxml import etree
        from erpnextswiss.erpnextswiss.swiss_payments import _schema
        content = frappe.get_doc("File", matching.name).get_content()
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        root = etree.fromstring(content.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
        _schema().assertValid(root)
        return dict(content=content, file_name=filename, file_url=matching.file_url, skipped=[], reused=True,
            message_id=root.findtext(".//{*}MsgId"), count=int(root.findtext(".//{*}GrpHdr/{*}NbOfTxs")),
            total=root.findtext(".//{*}GrpHdr/{*}CtrlSum"))
    try:
        result = render_credit_transfers(rows)
    except ValueError as error:
        frappe.throw(str(error))
    return _archive(result, doctype, [name], filename)


def generate_swiss_proposal_file(proposal_name):
    """Use the same current Swiss format for submitted payment proposals."""
    frappe.db.sql("SELECT name FROM `tabPayment Proposal` WHERE name=%s FOR UPDATE", (proposal_name,))
    proposal = frappe.get_doc("Payment Proposal", proposal_name)
    proposal.check_permission("write")
    if proposal.docstatus != 1:
        frappe.throw(_("Only a submitted payment proposal can be exported."))
    debtor = _debtor(proposal.company, proposal.pay_from_account)
    rows = []
    for payment in proposal.payments:
        method = payment.payment_type or "IBAN"
        iban = normalize_iban(payment.iban)
        participant = normalize_iban(payment.esr_participation_number)
        if method == "ESR":
            if not (is_qr_iban(participant) or is_qr_iban(iban)):
                frappe.throw(_("Legacy ESR payments must be replaced with current IBAN or QR-IBAN details."))
            iban, method = participant if is_qr_iban(participant) else iban, "QRR"
        street, building = _street_parts(payment.receiver_address_line1)
        rows.append(dict(name=str(payment.name), amount=payment.amount, currency=payment.currency,
            execution_date=max(getdate(payment.execution_date), getdate(nowdate())).isoformat(),
            iban=iban, bic=payment.bic, service_level=method, reference=payment.reference,
            qr_reference=payment.esr_reference, is_salary=bool(payment.is_salary), debtor=debtor,
            creditor=dict(name=payment.receiver, street=street, building=building,
                pincode=payment.receiver_pincode, city=payment.receiver_city,
                country_code=_country_code(payment.receiver_country))))
    return _immutable_payment_export(rows, "Payment Proposal", proposal.name, "proposal", ignore_execution_date=True)
