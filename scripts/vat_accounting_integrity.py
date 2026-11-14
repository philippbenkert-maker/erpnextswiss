"""Read-only release evidence separating posted taxes from future tax templates."""
import hashlib
import json

ACCOUNTING_TABLES = (
    'Account', 'Journal Entry', 'Journal Entry Account', 'GL Entry',
    'Sales Invoice', 'Sales Invoice Item', 'Sales Taxes and Charges',
    'Purchase Invoice', 'Purchase Invoice Item', 'Purchase Taxes and Charges',
    'Payment Entry', 'Payment Entry Reference', 'Payment Entry Deduction',
)
TEMPLATE = 'Purchase Taxes and Charges Template'


def accounting_digest():
    import frappe
    result = {}
    for table in ACCOUNTING_TABLES:
        # ERPNext reuses this child DocType for invoices AND future templates.
        # Every non-template row remains covered, including other parent types.
        where = " WHERE COALESCE(parenttype, '') != %s" if table == 'Purchase Taxes and Charges' else ''
        rows = frappe.db.sql(f'SELECT * FROM `tab{table}`{where} ORDER BY name',
            (TEMPLATE,) if where else (), as_dict=True)
        result[table] = {'count': len(rows), 'sha256': hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()}
    return result


def template_snapshot():
    import frappe
    return {
        'headers': {r.name: dict(r) for r in frappe.db.sql(
            f'SELECT * FROM `tab{TEMPLATE}` ORDER BY name', as_dict=True)},
        'taxes': {r.name: dict(r) for r in frappe.db.sql(
            'SELECT * FROM `tabPurchase Taxes and Charges` WHERE parenttype=%s ORDER BY name',
            (TEMPLATE,), as_dict=True)},
    }


def verify_template_changes(before, after, reviewed_changes):
    """Reject any new/deleted row or field change outside the reviewed preview."""
    expected_headers = {}
    expected_taxes = {}
    for template in reviewed_changes:
        assert template['doctype'] == TEMPLATE
        name = template['name']
        assert name in before['headers']
        expected_headers.setdefault(name, {})
        for change in template['changes']:
            if 'row' in change:
                row = change['row']
                assert before['taxes'][row]['parent'] == name
                assert before['taxes'][row]['account_head'] == change['from_account']
                assert before['taxes'][row].get('account_currency') == change['from_currency']
                expected_taxes[row] = {'account_head': change['to_account'],
                    'account_currency': change['to_currency']}
            else:
                assert before['headers'][name]['tax_code'] == change['from_code']
                expected_headers[name]['tax_code'] = change['to_code']
    changed = []
    for kind in ('headers', 'taxes'):
        assert before[kind].keys() == after[kind].keys(), (kind, 'rows added/deleted')
        for name, old in before[kind].items():
            new = after[kind][name]
            expected = expected_headers.get(name, {}) if kind == 'headers' else expected_taxes.get(name, {})
            saved = name in expected_headers if kind == 'headers' else old['parent'] in expected_headers
            expected_row = dict(old, **expected)
            # Frappe stamps the saved template and its child rows. This does not
            # permit metadata changes to invoices, accounts or any other template.
            if saved:
                for field in ('modified', 'modified_by'):
                    expected_row[field] = new[field]
            differences = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
            assert new == expected_row, (kind, name, 'unexpected field changes', differences)
            if differences:
                changed.append({'kind': kind, 'name': name, 'fields': differences})
    return changed
