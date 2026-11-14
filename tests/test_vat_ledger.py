import unittest
from decimal import Decimal as D
from erpnextswiss.vat_ledger import allocate, AllocationError, validate_mapping

class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.accounts={
            'sales':dict(root_type='Income'), 'discount':dict(root_type='Income'),
            'input':dict(account_type='Tax'), 'investment':dict(account_type='Tax'),
            'output':dict(account_type='Tax'), 'settlement':dict(account_type='Tax'),
            'stock':dict(root_type='Income'), 'direct':dict(account_type='Tax'),
            'bank':dict(root_type='Asset'), 'expense':dict(root_type='Expense'),
        }
        self.maps=[dict(account=a,role=r,box=b,rate_box='303' if a=='discount' else '') for a,r,b in [
            ('sales','Turnover','303'),('discount','Reduction','235'),('input','Input tax','400'),
            ('investment','Input tax','405'),('output','Output tax',''),('settlement','Settlement',''),
            ('stock','Excluded',''),('direct','Excluded','')]]
        self.ledger=[];self.items=[];self.taxes=[];self.extra={}
    def row(self,account,amount,kind='Journal Entry',number='JV1',**kw):
        self.ledger.append(dict(name=f'GL{len(self.ledger)}',company='Company',posting_date='2026-10-01',
            voucher_type=kind,voucher_no=number,account=account,credit=max(amount,0),debit=max(-amount,0),**kw))
    def invoice(self,number='SI1',amount=100,rate='8.1',**kw):
        self.row('sales',amount,'Sales Invoice',number,**kw)
        self.row('output',float(D(str(amount))*D(rate)/100),'Sales Invoice',number,**kw)
        self.items.append(dict(parent=number,income_account='sales',base_net_amount=amount,item_tax_rate={'output':rate}))
        self.taxes.append(dict(parent=number,account_head='output',rate=rate))
    def report(self):
        return allocate(company='Company',start_date='2026-10-01',end_date='2026-12-31',mappings=self.maps,
            accounts=self.accounts,ledger=self.ledger,items=self.items,sales_taxes=self.taxes,**self.extra)
    def test_net_sales_and_input_not_gross(self):
        self.invoice();self.row('input',-2.60)
        v=self.report()['values'];self.assertEqual(v['total_revenue'],D(100));self.assertEqual(v['payable_tax'],D('5.50'))
    def test_multiple_rates_on_same_invoice(self):
        self.invoice();self.row('sales',50,'Sales Invoice','SI1');self.row('output',1.3,'Sales Invoice','SI1')
        self.items.append(dict(parent='SI1',income_account='sales',base_net_amount=50,item_tax_rate={'output':2.6}))
        v=self.report()['values'];self.assertEqual(v['normal_amount_2024'],100);self.assertEqual(v['reduced_amount_2024'],50)
    def test_old_rate(self):
        self.invoice(rate='7.7');self.assertEqual(self.report()['values']['normal_amount'],100)
    def test_lodging_rate(self):
        self.invoice(rate='3.8');self.assertEqual(self.report()['values']['lodging_amount_2024'],100)
    def test_signed_credit_note(self):
        self.invoice(amount=-100);self.row('input',2.60)
        v=self.report()['values'];self.assertEqual(v['total_revenue'],-100);self.assertEqual(v['pretax_material'],D('-2.60'))
    def test_opening_cancelled_foreign_company_and_out_of_period_excluded(self):
        self.invoice();self.invoice('Cancelled',200,is_cancelled=1);self.row('sales',500,is_opening='Yes')
        self.row('sales',600,number='Foreign');self.ledger[-1]['company']='Other'
        self.row('sales',700,number='Old');self.ledger[-1]['posting_date']='2026-09-30'
        self.assertEqual(self.report()['values']['total_revenue'],100)
    def test_closing_does_not_erase_input(self):
        self.row('input',-63.95,number='Purchase');self.row('input',63.95,number='Close');self.row('settlement',-63.95,number='Close')
        r=self.report();self.assertEqual(r['values']['pretax_material'],D('63.95'));self.assertEqual(r['control']['excluded_closings'],['Close'])
    def test_reclass_import_and_discount_input(self):
        self.row('input',-100,number='PI');self.row('input',20,number='Reclass');self.row('investment',-20,number='Reclass')
        self.row('input',-5,number='Import');self.row('input',1,kind='Payment Entry',number='Discount')
        v=self.report()['values'];self.assertEqual(v['pretax_material'],84);self.assertEqual(v['pretax_investments'],20)
    def test_payment_to_estv_not_a_new_tax_event(self):
        self.row('settlement',100,kind='Payment Entry');self.row('bank',-100,kind='Payment Entry')
        self.assertEqual(self.report()['values']['payable_tax'],0)
    def test_mixed_settlement_needs_review(self):
        self.row('input',100);self.row('settlement',-100);self.row('bank',3)
        with self.assertRaisesRegex(AllocationError,'Mixed VAT settlement'):self.report()
    def test_stock_and_direct_tax_explicitly_excluded(self):
        self.row('stock',500);self.row('direct',-100)
        self.assertEqual(self.report()['values']['total_revenue'],0)
    def test_unknown_income_never_silently_ignored(self):
        self.accounts['unknown']=dict(root_type='Income');self.row('unknown',100)
        with self.assertRaisesRegex(AllocationError,'mapping missing'):self.report()
    def test_unknown_tax_never_silently_ignored(self):
        self.accounts['unknown']=dict(account_type='Tax');self.row('unknown',-100)
        with self.assertRaisesRegex(AllocationError,'mapping missing'):self.report()
    def test_explicit_exempt_sales(self):
        self.row('sales',100,kind='Sales Invoice',number='SI1')
        self.items=[dict(parent='SI1',income_account='sales',base_net_amount=100,item_tax_rate={},steuerziffer_ch='221')]
        v=self.report()['values'];self.assertEqual(v['revenue_abroad'],100);self.assertEqual(v['taxable_revenue'],0)
    def test_zero_rate_needs_classification(self):
        self.invoice(rate='0')
        with self.assertRaisesRegex(AllocationError,'Zero-rated'):self.report()
    def test_invoice_gl_difference_fails_atomically(self):
        self.invoice();self.items[0]['base_net_amount']=80
        with self.assertRaisesRegex(AllocationError,'disagree'):self.report()
    def test_conflicting_tax_code_and_rate_rejected(self):
        self.invoice();self.items[0]['steuerziffer_ch']='313'
        with self.assertRaisesRegex(AllocationError,'disagree'):self.report()
    def test_unknown_tax_rate_rejected(self):
        self.invoice(rate='5')
        with self.assertRaisesRegex(AllocationError,'Unsupported'):self.report()
    def test_unknown_tax_code_rejected(self):
        self.invoice();self.items[0]['steuerziffer_ch']='nonsense'
        with self.assertRaisesRegex(AllocationError,'Invalid turnover'):self.report()
    def test_invoice_with_missing_items_fails(self):
        self.invoice();self.items=[]
        with self.assertRaisesRegex(AllocationError,'no matching'):self.report()
    def test_ambiguous_mixed_rates_without_item_rates_rejected(self):
        self.invoice();self.items[0]['item_tax_rate']={};self.taxes.append(dict(parent='SI1',account_head='output',rate=2.6))
        with self.assertRaisesRegex(AllocationError,'Mixed VAT rates'):self.report()
    def test_posted_reduction_decreases_taxable_base_once(self):
        self.invoice();self.row('discount',-10,number='PE');self.row('output',-.81,number='PE')
        v=self.report()['values'];self.assertEqual(v['total_revenue'],100);self.assertEqual(v['losses'],10);self.assertEqual(v['normal_amount_2024'],90)
    def test_mixed_rate_payment_discount_rejected(self):
        self.row('discount',-10,kind='Payment Entry',number='PE');self.extra['payment_rates']={'PE':['303','313']}
        with self.assertRaisesRegex(AllocationError,'Mixed-rate payment'):self.report()
    def test_discount_follows_single_referenced_invoice_rate(self):
        self.invoice(rate='2.6');self.row('discount',-10,kind='Payment Entry',number='PE');self.row('output',-.26,kind='Payment Entry',number='PE')
        self.extra['payment_rates']={'PE':['313']};self.assertEqual(self.report()['values']['reduced_amount_2024'],90)
    def test_incorrect_purchase_liability_account_detected(self):
        self.row('output',-8.1,kind='Purchase Invoice')
        with self.assertRaisesRegex(AllocationError,'does not reconcile'):self.report()
    def test_rounding_visible(self):
        self.invoice();self.row('output',.01,number='Rounding')
        r=self.report();self.assertEqual(r['control']['difference'],D('.01'));self.assertTrue(r['warnings'])
    def test_large_output_difference_rejected(self):
        self.invoice();self.row('output',1,number='Rounding')
        with self.assertRaisesRegex(AllocationError,'does not reconcile'):self.report()
    def test_duplicate_mapping_invalid(self):
        with self.assertRaisesRegex(AllocationError,'more than once'):validate_mapping(self.maps+self.maps[:1],self.accounts)
    def test_wrong_role_box_invalid(self):
        self.maps[0]['box']='400'
        with self.assertRaisesRegex(AllocationError,'Invalid VAT box'):self.report()
    def test_cash_basis_and_flat_not_misrepresented(self):
        for opts in [dict(reporting_basis='2'),dict(vat_type='flat')]:
            self.extra=opts
            with self.assertRaisesRegex(AllocationError,'requires the effective'):self.report()
    def test_repeatable_and_no_mutation(self):
        import copy
        self.invoice();before=copy.deepcopy((self.ledger,self.items,self.maps))
        self.assertEqual(self.report(),self.report());self.assertEqual(before,(self.ledger,self.items,self.maps))
    def test_source_changes_change_fingerprint(self):
        self.invoice();before=self.report()['source_sha256'];self.row('bank',1)
        self.assertNotEqual(before,self.report()['source_sha256'])
    def test_inactive_group_or_foreign_account_rejected(self):
        for info in [dict(is_group=1),dict(disabled=1),None]:
            if info is None:del self.accounts['sales']
            else:self.accounts['sales']=info
            with self.assertRaisesRegex(AllocationError,'leaf account'):self.report()
    def test_opted_turnover_included_without_double_tax(self):
        self.invoice();self.maps[0]['opted']=1
        v=self.report()['values'];self.assertEqual(v['non_taxable_revenue'],100);self.assertEqual(v['payable_tax'],D('8.10'))
    def test_all_source_fields_return_zero_to_clear_stale_form(self):
        v=self.report()['values'];self.assertEqual(v['reduced_amount_2024'],0);self.assertEqual(v['pretax_correction_other'],0)
    def test_auto_rates_are_fixed_to_swiss_rate_boxes(self):
        v=self.report()['values'];self.assertEqual(v['normal_rate_2024'],D('8.1'));self.assertEqual(v['normal_rate'],D('7.7'))

    def test_dedicated_exempt_account_accepts_explicit_zero_rate(self):
        self.maps[0]['box']='221';self.invoice(rate='0')
        self.assertEqual(self.report()['values']['revenue_abroad'],100)
    def test_exempt_code_cannot_hide_posted_output_tax(self):
        self.invoice();self.items[0]['steuerziffer_ch']='221'
        with self.assertRaisesRegex(AllocationError,'Exempt VAT box'):self.report()
    def test_dedicated_exempt_account_with_tax_requires_review(self):
        self.maps[0]['box']='221';self.invoice()
        with self.assertRaisesRegex(AllocationError,'Exempt VAT box'):self.report()
    def test_exempt_payment_reduction_stays_exempt(self):
        self.maps[0]['box']='221';self.invoice(rate='0');self.row('discount',-5,kind='Payment Entry',number='PE1')
        self.extra['payment_rates']={'PE1':['221']}
        v=self.report()['values'];self.assertEqual(v['revenue_abroad'],95);self.assertEqual(v['losses'],5)
        self.assertEqual(v['taxable_revenue'],0)
    def test_multiple_payment_tax_treatments_need_explicit_allocation(self):
        self.row('discount',-5,kind='Payment Entry',number='PE1');self.extra['payment_rates']={'PE1':['221','303']}
        with self.assertRaisesRegex(AllocationError,'Mixed-rate'):self.report()


    def test_input_correction_credit_reduces_deduction(self):
        self.maps += [dict(account='mixed', role='Input tax', box='415'),
                      dict(account='other', role='Input tax', box='420'),
                      dict(account='subsequent', role='Input tax', box='410')]
        self.accounts.update({a: dict(account_type='Tax') for a in ('mixed','other','subsequent')})
        self.row('input', -100); self.row('mixed', 15); self.row('other', 20); self.row('subsequent', -5)
        v = self.report()['values']
        self.assertEqual(v['pretax_correction_mixed'], 15)
        self.assertEqual(v['pretax_correction_other'], 20)
        self.assertEqual(v['missing_pretax'], 5)
        self.assertEqual(v['total_pretax_reductions'], 70)
        self.assertEqual(v['payable_tax'], -70)

    def test_reversal_of_input_correction_restores_deduction(self):
        self.maps.append(dict(account='mixed', role='Input tax', box='415'))
        self.accounts['mixed'] = dict(account_type='Tax')
        self.row('mixed', -15)
        self.assertEqual(self.report()['values']['payable_tax'], -15)

if __name__=='__main__':unittest.main()
