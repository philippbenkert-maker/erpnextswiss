import unittest
from decimal import Decimal
from pathlib import Path
import hashlib, json
from lxml import etree
from erpnextswiss.vat_xml import build_xml, read_xml, calculate, validate_xml, uid, RESOURCES

class SwissVATXMLTests(unittest.TestCase):
    def values(self, **updates):
        values = dict(company='Test & Co <AG>', title='Test Q3', start_date='2026-07-01', end_date='2026-09-30', vat_type='effective', total_revenue='1080', normal_amount_2024='1000', reduced_amount_2024='80', pretax_material='40.51')
        return dict(values, **updates)

    def test_effective_roundtrip_and_escaping(self):
        raw = build_xml(self.values(), 'CHE-123.456.789 MWST')
        self.assertIn(b'Test &amp; Co &lt;AG&gt;', raw)
        result = read_xml(raw)
        self.assertEqual(result['values']['payable_tax'], Decimal('42.57'))
        self.assertEqual(result['uid'], 'CHE123456789')

    def test_opted_turnover_is_not_deducted_twice(self):
        result = read_xml(build_xml(self.values(non_taxable_revenue='200'), 'CHE123456789'))
        self.assertEqual(result['values']['taxable_revenue'], Decimal('1080.00'))
        self.assertEqual(result['values']['non_taxable_revenue'], Decimal('200.00'))

    def test_missing_turnover_reconciliation_blocks_export(self):
        with self.assertRaisesRegex(ValueError, 'turnover'):
            build_xml(self.values(total_revenue='1200'), 'CHE123456789')

    def test_credit_declaration(self):
        v=self.values(total_revenue=0, normal_amount_2024=0, reduced_amount_2024=0, pretax_material='63.95')
        self.assertEqual(read_xml(build_xml(v,'CHE123456789'))['values']['payable_tax'], Decimal('-63.95'))

    def test_net_tax_rate_method_separates_effective_fields(self):
        v=self.values(vat_type='flat', amount_1_2024=1080, rate_1_2024='2.1', pretax_material=0, normal_amount_2024=99999)
        self.assertEqual(read_xml(build_xml(v,'CHE123456789'))['values']['payable_tax'], Decimal('22.68'))
        with self.assertRaisesRegex(ValueError, 'Input tax'):
            build_xml(dict(v,pretax_material=1),'CHE123456789')

    def test_old_lodging_rate(self):
        v=self.values(total_revenue=100,normal_amount_2024=0,reduced_amount_2024=0,lodging_amount=100,pretax_material=0)
        self.assertEqual(read_xml(build_xml(v,'CHE123456789'))['values']['payable_tax'],Decimal('3.70'))

    def test_correction_has_distinct_submission_type(self):
        self.assertEqual(read_xml(build_xml(self.values(submission_type='2'),'CHE123456789'))['values']['submission_type'],'2')

    def test_acquisition_tax_and_input_corrections(self):
        v=self.values(additional_amount_2024='100', missing_pretax='2',pretax_correction_mixed='3',pretax_correction_other='4')
        self.assertEqual(read_xml(build_xml(v,'CHE123456789'))['values']['payable_tax'], Decimal('55.67'))

    def test_dtd_and_tampered_amount_are_rejected(self):
        raw=build_xml(self.values(),'CHE123456789')
        with self.assertRaises(ValueError):
            validate_xml(raw.replace(b'<VATDeclaration ',b'<!DOCTYPE VATDeclaration [<!ENTITY x SYSTEM "file:///etc/passwd">]><VATDeclaration ',1))
        with self.assertRaisesRegex(ValueError,'tax amount'):
            read_xml(raw.replace(b'<payableTax>42.57</payableTax>',b'<payableTax>42.58</payableTax>'))

    def test_schema_rejects_invalid_period_uid_and_unknown_elements(self):
        for kwargs in ({'start_date':'2026-10-01'}, {'misc':1}, {'normal_rate_2024':0}):
            with self.assertRaises(ValueError): build_xml(self.values(**kwargs),'CHE123456789')
        with self.assertRaises(ValueError): uid('DE123456789')
        raw=build_xml(self.values(),'CHE123456789')
        with self.assertRaises(etree.DocumentInvalid): validate_xml(raw.replace(b'</VATDeclaration>',b'<unmapped>1</unmapped></VATDeclaration>'))

    def test_bundled_official_schema_hashes(self):
        sources=json.loads((RESOURCES/'sources.json').read_text())
        for filename, entry in sources.items():
            self.assertEqual(hashlib.sha256((RESOURCES/filename).read_bytes()).hexdigest(),entry['sha256'])

if __name__ == '__main__': unittest.main()
