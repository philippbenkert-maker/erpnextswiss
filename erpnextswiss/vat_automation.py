"""Frappe adapter for the read-only, company-scoped Swiss VAT ledger allocator."""
import frappe
from frappe import _
from erpnextswiss.vat_ledger import allocate, classify_item
from erpnextswiss.vat_xml import period, money


def check_read(company):
    if not frappe.has_permission('VAT Declaration', 'read') or not frappe.has_permission('GL Entry', 'read'):
        frappe.throw(_('Accounting and VAT declaration read permission is required.'), frappe.PermissionError)
    frappe.get_doc('Company', company).check_permission('read')


def is_configured(company):
    return frappe.db.exists('Swiss VAT Settings', {'company': company, 'enabled': 1})


def get_report(company, start_date, end_date, vat_type='effective', reporting_basis='1'):
    check_read(company)
    start, end = period({'start_date': start_date, 'end_date': end_date})
    if not is_configured(company):
        frappe.throw(_('Configure the company VAT mapping before retrieving amounts.'))
    settings = frappe.get_doc('Swiss VAT Settings', company)
    settings.check_permission('read')
    if not settings.enabled or settings.vat_type != vat_type or str(settings.reporting_basis) != str(reporting_basis or '1'):
        frappe.throw(_('VAT method/basis does not match the enabled company mapping.'))
    if frappe.db.get_value('Company', company, 'default_currency') != 'CHF':
        frappe.throw(_('Automatic Swiss VAT allocation requires CHF company currency.'))
    args = {'company': company, 'start': start, 'end': end}
    accounts = frappe.get_all('Account', filters={'company': company},
        fields=['name', 'root_type', 'account_type', 'is_group', 'disabled'])
    ledger = frappe.db.sql('''SELECT name, company, posting_date, voucher_type, voucher_no,
        account, debit, credit, is_cancelled, is_opening FROM `tabGL Entry`
        WHERE company=%(company)s AND posting_date BETWEEN %(start)s AND %(end)s
        AND is_cancelled=0 ORDER BY posting_date, voucher_type, voucher_no, account, name''', args, as_dict=True)
    # Invoice dates need not match GL dates (reposting); select by actual included GL vouchers.
    sales = sorted({r.voucher_no for r in ledger if r.voucher_type == 'Sales Invoice'})
    mapping = {r.account: r for r in settings.accounts}
    payments = sorted({r.voucher_no for r in ledger if r.voucher_type == 'Payment Entry'
        and r.account in mapping and mapping[r.account].role == 'Reduction'})
    references = frappe.get_all('Payment Entry Reference', filters={'parent': ['in', payments],
        'parenttype': 'Payment Entry', 'reference_doctype': 'Sales Invoice'},
        fields=['parent', 'reference_name']) if payments else []
    all_sales = sorted(set(sales) | {r.reference_name for r in references})
    invoices = frappe.get_all('Sales Invoice', filters={'company': company, 'docstatus': 1,
        'name': ['in', all_sales]}, fields=['name', 'taxes_and_charges']) if all_sales else []
    allowed = [r.name for r in invoices]
    if set(all_sales) - set(allowed):
        frappe.throw(_('A ledger invoice is no longer submitted. Complete accounting reposting before VAT extraction.'))
    item_fields = ['parent', 'idx', 'income_account', 'base_net_amount', 'item_tax_rate']
    if frappe.get_meta('Sales Invoice Item').has_field('steuerziffer_ch'):
        item_fields.append('steuerziffer_ch')
    items = frappe.get_all('Sales Invoice Item', filters={'parent': ['in', allowed], 'parenttype': 'Sales Invoice'},
        fields=item_fields, order_by='parent, idx') if allowed else []
    taxes = frappe.get_all('Sales Taxes and Charges', filters={'parent': ['in', allowed], 'parenttype': 'Sales Invoice'},
        fields=['parent', 'idx', 'account_head', 'rate'], order_by='parent, idx') if allowed else []
    # Optional future templates can explicitly distinguish zero-rated turnover boxes.
    codes = {}
    if frappe.get_meta('Sales Taxes and Charges Template').has_field('tax_code'):
        for invoice in invoices:
            if invoice.taxes_and_charges:
                codes[invoice.name] = frappe.db.get_value('Sales Taxes and Charges Template', invoice.taxes_and_charges, 'tax_code')
    output = {r.account for r in settings.accounts if r.role == 'Output tax'}
    try:
        rates_by_invoice = {}
        for invoice in allowed:
            boxes = set()
            for item in items:
                if item.parent != invoice or not money(item.base_net_amount):
                    continue
                config = mapping.get(item.income_account)
                if not config or config.role != 'Turnover':
                    continue
                boxes.add(classify_item(item, config.box, output,
                    [t for t in taxes if t.parent == invoice], codes.get(invoice)))
            rates_by_invoice[invoice] = boxes
        payment_rates = {p: sorted({box for ref in references if ref.parent == p
            for box in rates_by_invoice.get(ref.reference_name, [])}) for p in payments}
        result = allocate(company=company, start_date=start, end_date=end,
            mappings=[{k: r.get(k) for k in ('account', 'role', 'box', 'rate_box', 'opted', 'reason')} for r in settings.accounts],
            accounts={r.name: r for r in accounts}, ledger=ledger, items=items, sales_taxes=taxes,
            invoice_codes=codes, payment_rates=payment_rates, vat_type=vat_type, reporting_basis=reporting_basis,
            tax_tolerance=settings.tax_tolerance)
    except ValueError as exc:
        frappe.throw(_('VAT allocation needs review: {0}').format(str(exc)))
    return result
