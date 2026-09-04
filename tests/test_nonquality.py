"""Regression cases for malformed/missing non-quality scoring inputs."""
import importlib.util
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from factors_model import fundamentals as f

spec = importlib.util.spec_from_file_location('ranker', Path(__file__).resolve().parents[1] / 'scripts/rank_six_factor_model.py')
ranker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ranker)


def fact(start, end, value, fp='FY'):
    return dict(start=start, end=end, val=value, filed='2026-02-20', form='10-K', fy=2025, fp=fp)


def payload(items):
    return {'facts': {'us-gaap': {'NetIncomeLoss': {'units': {'USD': items}}}}}


class NonQualityTests(unittest.TestCase):
    def test_missing_q2_cannot_be_invented_from_q1_and_nine_month_total(self):
        data = payload([fact('2025-01-01', '2025-03-31', 10, 'Q1'), fact('2025-01-01', '2025-09-30', 90, 'Q3')])
        self.assertEqual(len(f.quarter_series(data, f.NET_INCOME)), 1)

    def test_comparative_filing_labels_do_not_destroy_quarters(self):
        data = payload([fact('2025-01-01','2025-03-31',10), fact('2025-01-01','2025-06-30',30),
                        fact('2025-01-01','2025-09-30',60), fact('2025-01-01','2025-12-31',100)])
        q = f.quarter_series(data, f.NET_INCOME)
        self.assertEqual([x['value'] for x in q], [10,20,30,40])
        self.assertEqual(f.ttm_value(q),100)
        self.assertEqual(q[-1]['start'],'2025-10-01')

    def test_latest_restatement_wins_regardless_of_input_order(self):
        old = fact('2025-01-01','2025-03-31',10)
        new = {**old,'val':12,'filed':'2026-03-20'}
        for records in ([old,new], [new,old]):
            self.assertEqual(f.quarter_series(payload(records),f.NET_INCOME)[0]['value'],12)

    def test_missing_quarter_invalidates_ttm_and_sue(self):
        ends=['2023-03-31','2023-06-30','2023-09-30','2023-12-31','2024-03-31','2024-06-30','2024-09-30','2024-12-31','2025-03-31','2025-09-30','2025-12-31']
        q=[dict(end=end,value=i) for i,end in enumerate(ends)]
        self.assertIsNone(f.ttm_value(q))
        self.assertIsNone(f.sue_from_eps(q)['sue'])

    def test_excluded_financial_metrics_do_not_calibrate_peers(self):
        rows=[dict(ticker='A',fcf_yield=1,capex_to_avg_assets=1), dict(ticker='B',fcf_yield=2,capex_to_avg_assets=2),
              dict(ticker='BANK',exclude_bsa=True,fcf_yield=999,capex_to_avg_assets=999)]
        f.score_rows(rows)
        self.assertEqual(rows[1]['valuation_component_percentiles']['fcf_yield'],1)
        self.assertEqual(rows[1]['capex_intensity_percentile'],0)

    def test_missing_revisions_do_not_shift_valid_distribution(self):
        factors={'scoring_version':'nonquality_v2','rows':[{'ticker':t} for t in 'ABC']}
        revisions={'rows':[{'ticker':t,'revision_pct_30d':v} for t,v in zip('ABC',[0.1,0.2,None])]}
        rows={r['ticker']:r for r in ranker.score_model(factors,revisions,ranker.DEFAULT_WEIGHTS,'2026-09-04')['rows']}
        self.assertEqual(rows['A']['analyst_revisions_score'],1)
        self.assertEqual(rows['B']['analyst_revisions_score'],10)
        self.assertIsNone(rows['C']['analyst_revisions_score'])
        revisions['rows'][1]['revision_pct_30d']=0.1
        rows=ranker.score_model(factors,revisions,ranker.DEFAULT_WEIGHTS,'2026-09-04')['rows']
        self.assertEqual(rows[0]['analyst_revisions_score'],5.5)

    def test_singleton_revision_is_neutral(self):
        result=ranker.score_model({'scoring_version':'nonquality_v2','rows':[{'ticker':'A'}]},
                                 {'rows':[{'ticker':'A','revision_pct_30d':1}]},ranker.DEFAULT_WEIGHTS,'2026-09-04')
        self.assertEqual(result['rows'][0]['analyst_revisions_score'],5.5)

    def test_event_cannot_map_months_forward(self):
        prices={'2025-08-01':10,'2025-08-04':11,'2025-08-05':12,'2025-08-06':13}
        self.assertIsNone(f.car3(prices,prices,'2025-01-01')['car3'])

    def test_nonfinite_ratio_is_missing(self):
        self.assertIsNone(f.ratio(float('inf'),10))
        self.assertIsNone(f.ratio(1e308,1e-308))

class ExtractionIntegrationTests(unittest.TestCase):
    def test_stale_issuance_is_missing_and_buybacks_use_share_proxy(self):
        from test_quality_integration import companyfacts, collect
        data=companyfacts({2024:{'DebtAndCapitalLeaseObligations':50},2025:{'DebtAndCapitalLeaseObligations':50}})
        nodes=data['facts']['us-gaap']
        nodes['PaymentsForRepurchaseOfCommonStock']={'units':{'USD':[fact('2025-01-01','2025-12-31',20)]}}
        nodes['ProceedsFromStockOptionsExercised']={'units':{'USD':[fact('2024-01-01','2024-12-31',8)]}}
        row=collect(data)['rows'][0]
        self.assertIsNone(row['annual_issuance_cash'])
        self.assertIsNone(row['cash_net_buyback_yield'])
        self.assertIsNone(row['annual_dividends'])
        self.assertEqual(row['shareholder_yield_status'],'partial')

    def test_mismatched_cash_flow_years_do_not_produce_fcf(self):
        from test_quality_integration import companyfacts, collect
        data=companyfacts({})
        data['facts']['us-gaap']['PaymentsToAcquirePropertyPlantAndEquipment']={'units':{'USD':[fact('2024-01-01','2024-12-31',1)]}}
        self.assertIsNone(collect(data)['rows'][0]['fcf_ttm'])

    def test_khc_revenue_tag_is_supported(self):
        data={'facts':{'us-gaap':{'RevenueFromContractWithCustomerIncludingAssessedTax':{'units':{'USD':[fact('2025-01-01','2025-12-31',100)]}}}}}
        self.assertEqual(f.latest_annual_value(data,f.REVENUE)['value'],100)
