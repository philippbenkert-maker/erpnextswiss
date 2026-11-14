"""Explicit, repeatable setup of Swiss VAT mappings; never edits accounting vouchers.

Run only for a reviewed company. Existing settings are preserved. Tax templates apply
only to future documents; submitted invoices and their child tax rows are untouched.
"""
import frappe


def configure_company(company, apply=False):
    frappe.only_for(['System Manager', 'Accounts Manager'])
    frappe.get_doc('Company', company).check_permission('read')
    accounts = frappe.get_all('Account', filters={'company': company, 'is_group': 0, 'disabled': 0},
        fields=['name', 'account_number', 'root_type', 'account_type'])
    number_index = {}
    for row in accounts:
        if row.account_number:
            if row.account_number in number_index:
                frappe.throw('Doppelte Kontonummer: ' + row.account_number)
            number_index[row.account_number] = row.name
    required = {'1170', '1171', '2200', '2201', '3200', '3950'}
    if required - number_index.keys():
        frappe.throw('Kontenplan vor Einrichtung prüfen: ' + ', '.join(sorted(required - number_index.keys())))
    definitions = {
        '3000': ('Turnover', '303'), '3200': ('Turnover', '303'),
        '3400': ('Turnover', '303'), '3600': ('Turnover', '303'),
        '3805': ('Reduction', '235'), '3950': ('Reduction', '235'),
        '1170': ('Input tax', '400'), '1171': ('Input tax', '405'),
        '2200': ('Output tax', ''), '2201': ('Settlement', ''),
        '3900': ('Excluded', ''), '8900': ('Excluded', ''),
    }
    rows = []
    for number, (role, box) in definitions.items():
        if number in number_index:
            rows.append({'account': number_index[number], 'role': role, 'box': box,
                'rate_box': '303' if role == 'Reduction' else '',
                'reason': 'Bestandesänderung, kein Entgelt' if number == '3900' else 'Direkte Steuern, keine MWST' if number == '8900' else ''})
    output = {number_index['2200']}
    for account in accounts:
        if account.account_type == 'Tax' and any(account.name.startswith(f'VAT {rate}% - ') for rate in ('8.1', '2.6', '3.8', '7.7', '2.5', '3.7')):
            output.add(account.name)
            rows.append({'account': account.name, 'role': 'Output tax', 'box': ''})
    settings_exists = frappe.db.exists('Swiss VAT Settings', company)
    if apply and not settings_exists:
        frappe.get_doc({'doctype': 'Swiss VAT Settings', 'company': company, 'enabled': 1,
            'vat_type': 'effective', 'reporting_basis': '1', 'tax_tolerance': 0.05, 'accounts': rows}).insert()
    # Only these known Swiss standard purchase templates use liability accounts.
    # Correct their future input-tax account; do not touch existing invoice rows.
    template_changes = []
    for doctype in ('Purchase Taxes and Charges Template',):
        for name in frappe.get_all(doctype, filters={'company': company}, pluck='name'):
            doc = frappe.get_doc(doctype, name)
            changed = []
            for tax in doc.taxes:
                if doctype.startswith('Purchase') and name.startswith('Switzerland ') and tax.account_head in output:
                    changed.append({'row': tax.name, 'from_account': tax.account_head, 'to_account': number_index['1170']})
                    tax.account_head = number_index['1170']
            if doctype.startswith('Purchase') and doc.taxes and frappe.get_meta(doctype).has_field('tax_code'):
                used = {r.account_head for r in doc.taxes}
                if used <= {number_index['1170'], number_index['1171']}:
                    code = '405' if used == {number_index['1171']} else '400' if used == {number_index['1170']} else None
                    if code and doc.get('tax_code') != code:
                        changed.append({'from_code': doc.get('tax_code'), 'to_code': code})
                        doc.tax_code = code
            if changed:
                # ERPNext validates account currencies when saving a template.
                # Include every such normalization in the exact reviewed preview.
                for tax in doc.taxes:
                    currency = frappe.db.get_value('Account', tax.account_head, 'account_currency')
                    row_change = next((r for r in changed if r.get('row') == tax.name), None)
                    if row_change is not None or tax.get('account_currency') != currency:
                        if row_change is None:
                            row_change = {'row': tax.name, 'from_account': tax.account_head,
                                'to_account': tax.account_head}
                            changed.append(row_change)
                        row_change.update(from_currency=tax.get('account_currency'), to_currency=currency)
                        tax.account_currency = currency
                template_changes.append({'doctype': doctype, 'name': name, 'changes': changed})
                if apply:
                    doc.save()
    return {'company': company, 'existing_mapping_preserved': bool(settings_exists),
        'accounts': rows, 'future_template_changes': template_changes, 'applied': bool(apply)}
