import frappe
from frappe.model.document import Document
from erpnextswiss.vat_ledger import validate_mapping
from erpnextswiss.vat_xml import money

class SwissVATSettings(Document):
    def validate(self):
        company = frappe.get_doc("Company", self.company)
        company.check_permission("read")
        if company.default_currency != "CHF":
            frappe.throw("Die automatische Schweizer MWST-Abrechnung benötigt CHF als Gesellschaftswährung.")
        if not 0 <= money(self.tax_tolerance) <= 1:
            frappe.throw("Die Rundungstoleranz muss zwischen CHF 0.00 und CHF 1.00 liegen.")
        accounts = {r.name: r for r in frappe.get_all("Account", filters={"company": self.company},
            fields=["name", "is_group", "disabled", "root_type", "account_type"])}
        try:
            validate_mapping([r.as_dict() for r in self.accounts], accounts)
        except ValueError as exc:
            frappe.throw(str(exc))
