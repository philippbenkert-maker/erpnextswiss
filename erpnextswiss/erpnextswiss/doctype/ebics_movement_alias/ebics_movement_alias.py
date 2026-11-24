"""Immutable, uniquely owned bank reference; contains no raw bank identifiers."""

import re

import frappe
from frappe.model.document import Document


class EBICSMovementAlias(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("An EBICS bank reference claim cannot be changed")
        if not re.fullmatch(r"EBICS-MOV-[0-9a-f]{64}", self.alias_id or ""):
            frappe.throw("Invalid EBICS bank reference identity")
        account = frappe.db.get_value("Bank Account", self.bank_account,
                                      ["company", "account", "is_company_account"],
                                      as_dict=True, for_update=True)
        transaction = frappe.get_doc("Bank Transaction", self.bank_transaction, for_update=True)
        receipt_company = frappe.db.get_value("EBICS Download", self.source_download, "company")
        if (not account or account.company != self.company or account.account != self.account
                or not account.is_company_account or receipt_company != self.company
                or transaction.docstatus != 1 or transaction.company != self.company
                or transaction.bank_account != self.bank_account
                or transaction.currency != self.currency
                or not transaction.get("custom_ebics_movement_id")
                or not transaction.get("custom_ebics_movement_fingerprint")):
            frappe.throw("EBICS bank reference does not belong to the verified movement scope")

    def on_trash(self):
        frappe.throw("An EBICS bank reference claim cannot be deleted")
