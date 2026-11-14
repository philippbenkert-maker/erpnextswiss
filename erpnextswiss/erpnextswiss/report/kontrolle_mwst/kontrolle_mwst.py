"""Swiss VAT audit: the same allocation and bound period/company as 'Get values'."""
import re
import frappe
from frappe import _
from erpnextswiss.vat_ledger import BOX_FIELDS, INPUTS
from erpnextswiss.vat_automation import check_read, is_configured, get_report
from erpnextswiss.vat_xml import period


def execute(filters=None):
    filters = frappe._dict(filters or {})
    columns = [
        {'label': _('Document'), 'fieldname': 'name', 'fieldtype': 'Dynamic Link', 'options': 'doctype', 'width': 190},
        {'label': _('Date'), 'fieldname': 'posting_date', 'fieldtype': 'Date', 'width': 110},
        {'label': _('Account'), 'fieldname': 'account', 'fieldtype': 'Link', 'options': 'Account', 'width': 260},
        {'label': _('Code'), 'fieldname': 'box', 'fieldtype': 'Data', 'width': 80},
        {'label': _('Total'), 'fieldname': 'base_grand_total', 'fieldtype': 'Currency', 'width': 130},
        {'label': _('Tax Amount'), 'fieldname': 'total_taxes_and_charges', 'fieldtype': 'Currency', 'width': 130},
        {'label': _('Remarks'), 'fieldname': 'remarks', 'fieldtype': 'Data', 'width': 290},
    ]
    data = get_data(filters.from_date, filters.end_date, filters.code or '200', filters.company)
    return columns, data


def get_data(from_date, end_date, code, company=None):
    check_read(company)
    start, end = period({'start_date': from_date, 'end_date': end_date})
    code = str(code)
    if code not in set(BOX_FIELDS) | {'322', '323', '332', '333'}:
        frappe.throw(_('Select a Swiss VAT box.'))
    if is_configured(company):
        report = get_report(company, str(start), str(end))
        return [{**r, 'remarks': _(r['remarks']), 'base_grand_total': 0 if code in INPUTS else r['amount'],
            'total_taxes_and_charges': r['amount'] if code in INPUTS else 0,
            'taxes_and_charges': r['account']} for r in report['evidence'] if r['box'] == code]
    # Preserve explicitly configured legacy queries, never interpolate user input.
    name = 'viewVAT_' + code
    if not re.fullmatch(r'viewVAT_[0-9]{3}', name) or not frappe.db.exists('VAT query', name):
        frappe.throw(_('Configure the company VAT mapping before retrieving amounts.'))
    query = frappe.db.get_value('VAT query', name, 'query') or ''
    if '{company}' not in query:
        frappe.throw(_('The VAT query must filter the selected company using {company}.'))
    query = query.replace("'{company}'", '%(company)s').replace('"{company}"', '%(company)s').replace('{company}', '%(company)s')
    return frappe.db.sql(f'SELECT * FROM ({query}) s WHERE s.posting_date BETWEEN %(start)s AND %(end)s ORDER BY s.posting_date',
        {'company': company, 'start': start, 'end': end}, as_dict=True)


def get_tax_details(from_date, end_date, code, company=None):
    return get_data(from_date, end_date, code, company)


def get_vat_control_details(from_date, end_date, code, company=None):
    return get_data(from_date, end_date, code, company)
