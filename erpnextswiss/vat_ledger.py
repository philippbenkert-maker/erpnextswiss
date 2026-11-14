"""Read-only Swiss VAT allocation from posted CHF general-ledger entries.

No Frappe dependency: this module cannot post, amend or submit accounting documents.
Mappings are explicit per company; ambiguous tax treatments fail before returning values.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import hashlib
import json
from erpnextswiss.vat_xml import calculate, money

BOX_FIELDS = {
    '200': 'total_revenue', '205': 'non_taxable_revenue', '220': 'tax_free_services',
    '221': 'revenue_abroad', '225': 'transfers', '230': 'non_taxable_services',
    '235': 'losses', '280': 'misc', '303': 'normal_amount_2024', '313': 'reduced_amount_2024',
    '343': 'lodging_amount_2024', '302': 'normal_amount', '312': 'reduced_amount',
    '342': 'lodging_amount', '383': 'additional_amount_2024', '382': 'additional_amount',
    '400': 'pretax_material', '405': 'pretax_investments', '410': 'missing_pretax',
    '415': 'pretax_correction_mixed', '420': 'pretax_correction_other',
    '900': 'grants', '910': 'donations',
}
RATES = {'303': '8.1', '313': '2.6', '343': '3.8', '302': '7.7', '312': '2.5', '342': '3.7'}
DEDUCTIONS = {'220', '221', '225', '230', '280'}
INPUTS = {'400', '405', '410', '415', '420'}
ROLES = {'Turnover', 'Reduction', 'Input tax', 'Output tax', 'Settlement', 'Excluded', 'Other funds', 'Acquisition tax'}


class AllocationError(ValueError):
    pass


def validate_mapping(mappings, accounts):
    """Validate without inventing a tax treatment for an unmapped account."""
    result = {}
    for source in mappings:
        row = dict(source)
        account, role, box = row.get('account'), row.get('role'), str(row.get('box') or '')
        if account in result:
            raise AllocationError(f'Account mapped more than once: {account}')
        info = accounts.get(account)
        if not info or info.get('is_group') or info.get('disabled'):
            raise AllocationError(f'Choose an enabled leaf account of this company: {account}')
        if role not in ROLES:
            raise AllocationError(f'Invalid VAT role for {account}')
        allowed = {'Turnover': set(RATES) | DEDUCTIONS, 'Reduction': {'235'}, 'Input tax': INPUTS,
                   'Other funds': {'900', '910'}, 'Acquisition tax': {'382', '383'}}
        if role in allowed and box not in allowed[role]:
            raise AllocationError(f'Invalid VAT box {box} for {role}: {account}')
        if role not in allowed and box:
            raise AllocationError(f'{role} is a control/exclusion role and has no VAT box: {account}')
        if role == 'Reduction' and str(row.get('rate_box') or '') not in RATES:
            raise AllocationError(f'Choose the tax-rate box for reductions: {account}')
        if row.get('opted') and (role != 'Turnover' or box not in RATES):
            raise AllocationError(f'Opted turnover requires a taxable turnover account: {account}')
        if box == '280' and not str(row.get('reason') or '').strip():
            raise AllocationError(f'Box 280 requires a description: {account}')
        row['box'] = box
        result[account] = row
    if not result:
        raise AllocationError('Configure the company VAT account mapping first')
    return result


def rate_box(rate):
    matching = [box for box, value in RATES.items() if money(value) == money(rate)]
    if len(matching) != 1:
        raise AllocationError(f'Unsupported or ambiguous Swiss VAT rate: {rate}')
    return matching[0]


def classify_item(item, default, output_accounts, taxes, invoice_code=None):
    """Use the same classification for invoice bases and related payment reductions."""
    invoice = item['parent']
    code = str(item.get('steuerziffer_ch') or invoice_code or '')
    if code == 'Mwstnummer':
        code = ''  # Historical dimension placeholder, not a Swiss VAT box.
    if code and code not in set(RATES) | DEDUCTIONS:
        raise AllocationError(f"Invalid turnover VAT code {code}: {invoice}")
    raw = item.get('item_tax_rate') or '{}'
    rates = json.loads(raw) if isinstance(raw, str) else raw
    matched = {money(v) for a, v in rates.items() if a in output_accounts}
    if not matched:
        matched = {money(t['rate']) for t in taxes if t['account_head'] in output_accounts}
    if len(matched) > 1:
        raise AllocationError(f"Mixed VAT rates need a rate on each invoice item: {invoice}")
    exempt = code if code in DEDUCTIONS else default if default in DEDUCTIONS and not code else None
    if exempt:
        if any(matched):
            raise AllocationError(f"Exempt VAT box and posted rate disagree: {invoice}")
        return exempt
    if matched:
        actual_rate = next(iter(matched))
        if not actual_rate:
            raise AllocationError(f"Zero-rated invoice needs an explicit VAT box 220/221/225/230/280: {invoice}")
        box = rate_box(actual_rate)
        if code in RATES and code != box:
            raise AllocationError(f"VAT code and posted rate disagree: {invoice}")
        return box
    # Historic untaxed income uses the explicitly reviewed account classification.
    return code if code in RATES else default


def allocate(*, company, start_date, end_date, mappings, accounts, ledger, items=(), sales_taxes=(),
             invoice_codes=None, payment_rates=None, reporting_basis='1', vat_type='effective', tax_tolerance='0.05'):
    """Return every source field, evidence per VAT box, and output-tax reconciliation.

    Values are net CHF from GL, with signed credit notes and tax corrections. Invoice
    items only split the actually posted income by rate, never replace the GL amount.
    Settlement entries are excluded only if the *whole* voucher is a pure VAT closing.
    """
    if vat_type != 'effective' or str(reporting_basis or '1') != '1':
        raise AllocationError('Automatic ledger allocation requires the effective method and agreed consideration (basis 1)')
    start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if start > end:
        raise AllocationError('The period ends before it starts')
    mapping = validate_mapping(mappings, accounts)
    values = {field: Decimal('0.00') for field in BOX_FIELDS.values()}
    evidence, vouchers = [], defaultdict(list)
    invoice_codes, payment_rates = invoice_codes or {}, payment_rates or {}
    source = []
    for row in ledger:
        if row.get('company', company) != company or row.get('is_cancelled') or row.get('is_opening') == 'Yes':
            continue
        if not start <= date.fromisoformat(str(row['posting_date'])) <= end:
            continue
        source.append(row)
        if row.get('voucher_type') != 'Period Closing Voucher':
            vouchers[(row['voucher_type'], row['voucher_no'])].append(row)
    item_index, tax_index = defaultdict(list), defaultdict(list)
    for item in items:
        item_index[(item['parent'], item['income_account'])].append(item)
    for tax in sales_taxes:
        tax_index[tax['parent']].append(tax)
    output_accounts = {name for name, row in mapping.items() if row['role'] == 'Output tax'}
    output_tax, acquisition_tax = Decimal(0), Decimal(0)
    excluded_closings = []

    def add(box, amount, row, reason):
        amount = money(amount)
        if not amount:
            return
        values[BOX_FIELDS[box]] += amount
        evidence.append({'box': box, 'doctype': row['voucher_type'], 'name': row['voucher_no'],
                         'posting_date': str(row['posting_date']), 'account': row['account'],
                         'amount': amount, 'gl_entry': row.get('name'), 'remarks': reason})

    def invoice_split(row, default):
        selected = item_index[(row['voucher_no'], row['account'])]
        if not selected:
            raise AllocationError(f"Posted invoice has no matching income items: {row['voucher_no']} / {row['account']}")
        splits = defaultdict(Decimal)
        for item in selected:
            box = classify_item(item, default, output_accounts, tax_index[row['voucher_no']],
                                invoice_codes.get(row['voucher_no']))
            splits[box] += money(item['base_net_amount'])
        total = sum(splits.values(), Decimal(0))
        posted = money(row['credit']) - money(row['debit'])
        if abs(total - posted) > Decimal('0.02'):
            raise AllocationError(f"Invoice items and posted income disagree: {row['voucher_no']} / {row['account']}")
        # Account for GL currency rounding once, with deterministic allocation.
        splits[sorted(splits)[-1]] += posted - total
        return splits

    for (kind, number), rows in sorted(vouchers.items()):
        roles = {mapping[r['account']]['role'] if r['account'] in mapping else None for r in rows}
        if 'Settlement' in roles:
            if roles & {'Input tax', 'Output tax', 'Acquisition tax'}:
                if kind != 'Journal Entry' or not roles <= {'Input tax', 'Output tax', 'Settlement', 'Acquisition tax'}:
                    raise AllocationError(f'Mixed VAT settlement needs review: {number}')
                excluded_closings.append(number)
                continue
            if roles & {'Turnover', 'Reduction', 'Other funds'}:
                raise AllocationError(f'Mixed VAT settlement needs review: {number}')
            # Paying a filed VAT balance against the bank is not a new tax event.

        # Group same account inside a voucher (cost centres can split one posting).
        grouped = {}
        for row in rows:
            account = row['account']
            if account not in grouped:
                grouped[account] = {**row, 'debit': Decimal(0), 'credit': Decimal(0)}
            grouped[account]['debit'] += money(row['debit'])
            grouped[account]['credit'] += money(row['credit'])
        for account, row in grouped.items():
            net = row['credit'] - row['debit']
            if not net:
                continue
            config = mapping.get(account)
            if not config:
                info = accounts.get(account, {})
                if info.get('root_type') == 'Income' or info.get('account_type') == 'Tax':
                    raise AllocationError(f'VAT mapping missing for posted account: {account} ({number})')
                continue
            role, box = config['role'], config['box']
            if role == 'Turnover':
                splits = invoice_split(row, box) if kind == 'Sales Invoice' else {box: net}
                add('200', net, row, 'Posted net turnover')
                for actual_box, amount in splits.items():
                    add(actual_box, amount, row, 'Turnover allocation')
                if config.get('opted'):
                    add('205', sum((v for k, v in splits.items() if k in RATES), Decimal(0)), row, 'Opted turnover, included in 200')
            elif role == 'Reduction':
                add('235', -net, row, 'Posted reduction of consideration')
                rates = payment_rates.get(number) if kind == 'Payment Entry' else None
                if rates and len(rates) != 1:
                    raise AllocationError(f'Mixed-rate payment reduction needs separate posting accounts: {number}')
                actual_box = next(iter(rates)) if rates else str(config['rate_box'])
                add(actual_box, net, row, 'Reduction of taxable base')
            elif role == 'Input tax':
                # 415/420 are reductions of the input-tax deduction; a credit
                # on a dedicated correction account is a positive reduction.
                add(box, net if box in {'415', '420'} else -net, row, 'Posted input tax including corrections')
            elif role == 'Output tax':
                output_tax += net
            elif role == 'Acquisition tax':
                tax_rate = Decimal('8.1' if box == '383' else '7.7')
                add(box, net * 100 / tax_rate, row, 'Acquisition taxable base from dedicated tax account')
                acquisition_tax += net
            elif role == 'Other funds':
                add(box, net, row, 'Other funds, excluded from turnover 200')
    values = {key: money(value) for key, value in values.items()}
    reasons = sorted({r.get('reason', '').strip() for r in mapping.values() if r['box'] == '280' and r.get('reason')})
    for box, field in RATES.items():
        amount_field = BOX_FIELDS[box]
        values[amount_field.replace('_amount', '_rate')] = Decimal(field)
    values.update({field: Decimal(0) for field in ('form_1050', 'form_1055', 'amount_1', 'amount_2', 'amount_1_2024', 'amount_2_2024')})
    values['misc_description'] = '; '.join(reasons) if values['misc'] else ''
    totals = calculate({**values, 'vat_type': vat_type})
    rate_bases = sum((values[BOX_FIELDS[box]] for box in RATES), Decimal(0))
    if money(rate_bases) != totals['taxable_revenue']:
        raise AllocationError('Taxable turnover does not reconcile with the rate-specific bases')
    difference = money(output_tax + acquisition_tax - totals['total_tax'])
    if abs(difference) > money(tax_tolerance):
        raise AllocationError(f'Output tax does not reconcile: booked {money(output_tax + acquisition_tax)}, calculated {totals["total_tax"]}, difference {difference}. Check VAT accounts/rates and unclassified documents.')
    fingerprint = hashlib.sha256(json.dumps({'company': company, 'start': str(start), 'end': str(end),
        'mappings': mappings, 'ledger': source, 'items': items, 'taxes': sales_taxes,
        'invoice_codes': invoice_codes, 'payment_rates': payment_rates}, sort_keys=True, default=str).encode()).hexdigest()
    return {'values': {**values, **totals}, 'evidence': evidence, 'source_sha256': fingerprint,
            'control': {'booked_output_tax': money(output_tax + acquisition_tax), 'calculated_output_tax': totals['total_tax'],
                        'difference': difference, 'excluded_closings': excluded_closings,
                        'entry_count': len(source)},
            'warnings': [f'Rounding difference on output tax: CHF {difference}'] if difference else []}
