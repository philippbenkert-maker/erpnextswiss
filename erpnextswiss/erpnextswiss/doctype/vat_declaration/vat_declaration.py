# Copyright (c) libracore and contributors. See license.txt.
"""VAT declarations: immutable XML evidence, never journal posting or ESTV submission."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
import hashlib
import re
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime, get_system_timezone
from erpnextswiss.vat_xml import build_xml, calculate, money, period, read_xml, uid

_ARCHIVE_WRITE = ContextVar("vat_archive_write", default=False)
EVIDENCE = ("xml_file", "xml_sha256", "xml_generated_at", "filing_status", "filing_confirmed_by",
            "filing_confirmed_at", "filed_on", "filing_evidence", "submission_receipt", "archive_source")

@contextmanager
def archive_write():
    token = _ARCHIVE_WRITE.set(True)
    try:
        yield
    finally:
        _ARCHIVE_WRITE.reset(token)


def local_xml_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def file_bytes(file_doc):
    content = file_doc.get_content()
    return content.encode("utf-8") if isinstance(content, str) else bytes(content)


def checked_xml(doc):
    files = frappe.get_all("File", filters={"file_url": doc.xml_file,
        "attached_to_doctype": doc.doctype, "attached_to_name": doc.name}, pluck="name")
    if not files:
        frappe.throw(_("The archived XML attachment is missing."))
    content = file_bytes(frappe.get_doc("File", files[0]))
    if hashlib.sha256(content).hexdigest() != doc.xml_sha256:
        frappe.throw(_("The archived XML checksum does not match. No new export was created."))
    return content


class VATDeclaration(Document):
    def validate(self):
        old = self.get_doc_before_save()
        if not _ARCHIVE_WRITE.get():
            for field in EVIDENCE:
                if self.get(field) != (old.get(field) if old else None) and self.get(field) not in (None, "", "Draft"):
                    frappe.throw(_("XML and filing evidence can only be changed through the archive actions."))
            if old and old.xml_file:
                for field in EVIDENCE:
                    if self.get(field) != old.get(field):
                        frappe.throw(_("Archived evidence cannot be removed or changed."))
        try:
            start, end = period(self.as_dict())
            uid(frappe.db.get_value("Company", self.company, "tax_id"))
            totals = calculate(self.as_dict())
        except ValueError as exc:
            frappe.throw(str(exc))
        self.submission_type = self.submission_type or "1"
        self.reporting_basis = self.reporting_basis or "1"
        if self.submission_type == "2" and not self.corrects_declaration:
            frappe.throw(_("Select the original declaration for a correction."))
        key = f"{self.company}|{end}|{self.submission_type}|{self.corrects_declaration or ''}"
        self.period_key = hashlib.sha256(key.encode()).hexdigest() if self.docstatus != 2 else None
        if self.submission_type == "2":
            original = frappe.get_doc("VAT Declaration", self.corrects_declaration)
            original.check_permission("read")
            if original.name == self.name or original.docstatus != 1 or original.company != self.company or getdate(original.start_date) != start or getdate(original.end_date) != end:
                frappe.throw(_("A correction must refer to a submitted declaration for the same company and period."))
        if self.submission_type == "1" and self.docstatus != 2:
            duplicate = frappe.get_all("VAT Declaration", filters={"company": self.company, "docstatus": ["!=", 2],
                "name": ["!=", self.name or ""], "start_date": ["<=", end], "end_date": [">=", start]},
                fields=["name", "submission_type"])
            if any((d.submission_type or "1") == "1" for d in duplicate):
                frappe.throw(_("An original VAT declaration already exists for this period. Open it or create an explicit correction."))
        if not (old and old.xml_file):
            self.update({key: float(value) for key, value in totals.items()})
        self.filing_status = self.filing_status or "Draft"

    def before_update_after_submit(self):
        # Frappe skips validate() on this action; evidence protection must run here too.
        self.validate()

    def before_submit(self):
        if self.xml_file:
            checked_xml(self)
        else:
            self._archive_xml()

    def before_cancel(self):
        if self.xml_file:
            frappe.throw(_("An archived VAT declaration is preserved. Create a correction instead."))

    def on_trash(self):
        if self.xml_file:
            frappe.throw(_("Archived VAT declarations cannot be deleted."))

    def _build_xml(self):
        try:
            return build_xml(self.as_dict(), frappe.db.get_value("Company", self.company, "tax_id"))
        except Exception as exc:
            # lxml schema errors also stop export before a file is saved.
            frappe.throw(_("XML validation failed: {0}").format(str(exc)))

    def _archive_xml(self):
        content = self._build_xml()
        from frappe.utils.file_manager import save_file
        file = save_file("MWST-" + self.name.replace("/", "-") + ".xml", content,
                         self.doctype, self.name, is_private=1, df="xml_file")
        self.xml_file = file.file_url
        self.xml_sha256 = hashlib.sha256(content).hexdigest()
        self.xml_generated_at = local_xml_time(read_xml(content)["generated_at"])
        self.filing_status = "XML ready"
        self.archive_source = "ERP export"
        return content

    @frappe.whitelist(methods=["POST"])
    def create_transfer_file(self):
        # Never export client-supplied amounts in a submitted document method.
        self.reload()
        self.check_permission("write")
        if self.docstatus != 1:
            frappe.throw(_("Save and submit the checked declaration before exporting XML."))
        with frappe.cache.lock("vat-xml:" + self.name, timeout=60, blocking_timeout=10):
            self.reload()
            if self.xml_file:
                content = checked_xml(self)
            else:
                with archive_write():
                    content = self._archive_xml()
                    self.save()
            return {"content": content.decode("utf-8"), "file_url": self.xml_file,
                    "sha256": self.xml_sha256, "filename": self.xml_file.rsplit("/", 1)[-1]}

    @frappe.whitelist(methods=["POST"])
    def confirm_external_filing(self, evidence, filed_on=None, receipt=None):
        self.reload()
        self.check_permission("write")
        self.check_permission("submit")
        if self.docstatus != 1 or not self.xml_file:
            frappe.throw(_("Archive the submitted declaration's XML first."))
        checked_xml(self)
        if self.filing_status == "Filed (user confirmed)":
            return self.name
        if not str(evidence or "").strip():
            frappe.throw(_("Record the basis for confirming the external filing."))
        if filed_on and getdate(filed_on) > getdate():
            frappe.throw(_("The filing date cannot be in the future."))
        if receipt:
            file = frappe.get_doc("File", {"file_url": receipt})
            file.check_permission("read")
            if file.attached_to_doctype != self.doctype or file.attached_to_name != self.name:
                frappe.throw(_("Attach the receipt to this declaration first."))
        with archive_write():
            self.filing_status = "Filed (user confirmed)"
            self.filing_confirmed_by = frappe.session.user
            self.filing_confirmed_at = now_datetime()
            self.filed_on = filed_on or None
            self.filing_evidence = str(evidence).strip()
            self.submission_receipt = receipt
            self.save()
        return self.name

@frappe.whitelist(methods=["POST"])
def import_existing_xml(file_name, journal_entry, expected_sha256, filing_evidence=None):
    """Link existing submitted evidence. Does not post, change, or submit a Journal Entry."""
    if not frappe.has_permission("VAT Declaration", "create") or not frappe.has_permission("VAT Declaration", "submit"):
        frappe.throw(_("VAT declaration create and submit permission is required."), frappe.PermissionError)
    file = frappe.get_doc("File", file_name)
    file.check_permission("read")
    journal = frappe.get_doc("Journal Entry", journal_entry)
    journal.check_permission("read")
    if journal.docstatus != 1 or file.attached_to_doctype != "Journal Entry" or file.attached_to_name != journal.name:
        frappe.throw(_("The source XML must be attached to the existing submitted closing Journal Entry."))
    content = file_bytes(file)
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        frappe.throw(_("The source XML differs from the reviewed checksum."))
    try:
        parsed = read_xml(content)
        if parsed["uid"] != uid(frappe.db.get_value("Company", journal.company, "tax_id")):
            raise ValueError("The XML UID does not match the Journal Entry company")
    except ValueError as exc:
        frappe.throw(str(exc))
    values = parsed["values"]
    if getdate(journal.posting_date) != getdate(values["end_date"]):
        frappe.throw(_("The closing Journal Entry date does not match the XML period end."))
    settlement = Decimal(0)
    found = False
    for row in journal.accounts:
        if frappe.db.get_value("Account", row.account, "account_number") == "2201":
            settlement += money(row.credit_in_account_currency) - money(row.debit_in_account_currency)
            if row.account_currency != "CHF":
                frappe.throw(_("The VAT settlement account must be in CHF."))
            found = True
    if not found or money(settlement) != values["payable_tax"]:
        frappe.throw(_("The existing VAT settlement booking does not reconcile with the XML payable tax."))
    with frappe.cache.lock("vat-import:" + digest, timeout=60, blocking_timeout=10):
        existing = frappe.db.get_value("VAT Declaration", {"xml_sha256": digest}, "name")
        if existing:
            doc = frappe.get_doc("VAT Declaration", existing)
            doc.check_permission("read")
            if doc.linked_journal_entry != journal.name or doc.company != journal.company or doc.docstatus != 1:
                frappe.throw(_("The XML is already linked to different or incomplete evidence."))
        else:
            end = getdate(values["end_date"])
            title = f"MWST {end.year} Q{(end.month - 1) // 3 + 1} - {frappe.db.get_value('Company', journal.company, 'abbr')}"
            doc = frappe.get_doc({"doctype": "VAT Declaration", **{k: float(v) if isinstance(v, Decimal) else v for k, v in values.items()},
                "title": title, "company": journal.company, "linked_journal_entry": journal.name,
                "xml_file": file.file_url, "xml_sha256": digest, "archive_source": "Existing XML",
                "xml_generated_at": local_xml_time(parsed["generated_at"]), "filing_status": "XML ready"})
            with archive_write():
                doc.insert()
                # A second File record grants access from the declaration; original bytes and JE attachment remain intact.
                frappe.get_doc({"doctype": "File", "file_name": file.file_name, "file_url": file.file_url,
                    "is_private": 1, "attached_to_doctype": doc.doctype, "attached_to_name": doc.name,
                    "attached_to_field": "xml_file"}).insert()
                doc.submit()
        if filing_evidence:
            doc.confirm_external_filing(filing_evidence)
        return {"name": doc.name, "xml_sha256": doc.xml_sha256, "filing_status": doc.filing_status}


def _query_total(view_name, start_date, end_date, company, column):
    if not frappe.has_permission("VAT Declaration", "read"):
        frappe.throw(_("VAT declaration read permission is required."), frappe.PermissionError)
    frappe.get_doc("Company", company).check_permission("read")
    start, end = period({"start_date": start_date, "end_date": end_date})
    if not frappe.db.exists("VAT query", view_name):
        frappe.throw(_("VAT query {0} is missing. Configure it before retrieving amounts; no fields were changed.").format(view_name))
    if not re.fullmatch(r"viewVAT_[0-9]{3}", view_name):
        frappe.throw(_("Select a configured Swiss VAT query."))
    query = frappe.db.get_value("VAT query", view_name, "query")
    if "{company}" not in (query or ""):
        frappe.throw(_("The VAT query must explicitly filter the selected company using {company}."))
    # VAT query text is maintained by authorized administrators, dates/company are bound values.
    query = query.replace("'{company}'", "%(company)s").replace('"{company}"', "%(company)s").replace("{company}", "%(company)s")
    sql = f"SELECT COALESCE(SUM(s.`{column}`), 0) FROM ({query}) s WHERE s.posting_date >= %(start)s AND s.posting_date <= %(end)s"
    return frappe.db.sql(sql, {"start": start, "end": end, "company": company})[0][0] or 0


@frappe.whitelist()
def get_view_total(view_name, start_date, end_date, company=None):
    return {"total": _query_total(view_name, start_date, end_date, company, "base_grand_total")}


@frappe.whitelist()
def get_view_tax(view_name, start_date, end_date, company=None):
    return {"total": _query_total(view_name, start_date, end_date, company, "total_taxes_and_charges")}


@frappe.whitelist()
def get_tax_rate(taxes_and_charges_template):
    frappe.get_doc("Purchase Taxes and Charges Template", taxes_and_charges_template).check_permission("read")
    return frappe.db.get_value("Purchase Taxes and Charges", {"parent": taxes_and_charges_template}, "rate", order_by="idx") or 0


@frappe.whitelist()
def get_period_values(company, start_date, end_date, vat_type="effective"):
    """One response, only after all required queries succeeded; no partial UI overwrites."""
    mapping = {"200": "total_revenue", "220": "tax_free_services", "221": "revenue_abroad", "225": "transfers",
               "230": "non_taxable_services", "235": "losses", "383": "additional_amount_2024", "382": "additional_amount"}
    if vat_type == "effective":
        mapping.update({"303": "normal_amount_2024", "313": "reduced_amount_2024", "343": "lodging_amount_2024",
                        "302": "normal_amount", "312": "reduced_amount", "342": "lodging_amount"})
    elif vat_type == "flat":
        mapping.update({"323": "amount_1_2024", "333": "amount_2_2024", "322": "amount_1", "332": "amount_2"})
    else:
        frappe.throw(_("Unsupported VAT method."))
    result = {field: _query_total("viewVAT_" + number, start_date, end_date, company, "base_grand_total") for number, field in mapping.items()}
    if vat_type == "effective":
        result.update({field: _query_total("viewVAT_" + number, start_date, end_date, company, "total_taxes_and_charges")
                       for number, field in (("400", "pretax_material"), ("405", "pretax_investments"))})
    return result


@frappe.whitelist()
def calculate_values(values):
    if not frappe.has_permission("VAT Declaration", "read"):
        frappe.throw(_("VAT declaration read permission is required."), frappe.PermissionError)
    try:
        return {key: float(value) for key, value in calculate(frappe.parse_json(values)).items()}
    except ValueError as exc:
        frappe.throw(str(exc))


@frappe.whitelist()
def inspect_existing_xml(file_name):
    if not frappe.has_permission("VAT Declaration", "create"):
        frappe.throw(_("VAT declaration create permission is required."), frappe.PermissionError)
    file = frappe.get_doc("File", file_name)
    file.check_permission("read")
    if file.attached_to_doctype != "Journal Entry":
        frappe.throw(_("Select an XML attached to the existing closing entry."))
    journal = frappe.get_doc("Journal Entry", file.attached_to_name)
    journal.check_permission("read")
    content = file_bytes(file)
    parsed = read_xml(content)
    values = parsed["values"]
    return {"file_name": file.name, "journal_entry": journal.name, "company": journal.company,
            "start_date": values["start_date"], "end_date": values["end_date"],
            "payable_tax": float(values["payable_tax"]), "sha256": hashlib.sha256(content).hexdigest()}
