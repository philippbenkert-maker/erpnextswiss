"""Immutable, uniquely owned bank reference; contains no raw bank identifiers."""

import re

import frappe
from frappe.model.document import Document


class EBICSMovementAlias(Document):
    def _validate_links(self):
        if self.bank_transaction:
            # The account lock can reveal another worker's committed movement
            # while Frappe's ordinary link query still sees the old snapshot.
            # Seed that exact native lookup with a fresh locked read.
            fetched = self.meta.get_fields_to_fetch("bank_transaction")
            fields = ("name", *(field.fetch_from.rsplit(".", 1)[-1] for field in fetched
                                  if not field.get("fetch_if_empty") or not self.get(field.fieldname)))
            frappe.db.value_cache["Bank Transaction"].pop(self.bank_transaction, None)
            frappe.db.get_value("Bank Transaction", self.bank_transaction, fields, as_dict=True,
                                for_update=True, cache=True, order_by=None)
        super()._validate_links()

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
