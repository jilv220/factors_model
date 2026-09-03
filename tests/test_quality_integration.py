"""Exercise SEC extraction through the normal factor pipeline without network."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factors_model import fundamentals
from factors_model.quality import LEGACY_QUALITY_VERSION, QUALITY_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]


def companyfacts(debt: dict[int, dict[str, float]], *, cash: bool = True) -> dict:
    facts: dict = {}
    for year in (2024, 2025):
        instant = {"Assets": 200, "Liabilities": 100, "StockholdersEquity": 100, **debt.get(year, {})}
        if cash:
            instant["CashAndCashEquivalentsAtCarryingValue"] = 10
        duration = {"NetIncomeLoss": 10, "NetCashProvidedByUsedInOperatingActivities": 12,
                    "RevenueFromContractWithCustomerExcludingAssessedTax": 80}
        for concept, value in {**instant, **duration}.items():
            row = {"val": value, "end": f"{year}-12-31", "filed": "2026-02-20",
                   "form": "10-K", "fy": 2025, "fp": "FY", "accn": "test-2025-annual"}
            if concept in duration:
                row["start"] = f"{year}-01-01"
            facts.setdefault(concept, {"units": {"USD": []}})["units"]["USD"].append(row)
    return {"cik": 1234, "entityName": "Test Company", "facts": {"us-gaap": facts}}


def collect(facts: dict, quality_method: str = QUALITY_VERSION) -> dict:
    def fetch(url: str, *args: object, **kwargs: object) -> dict:
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "TEST", "cik_str": 1234}}
        if "/companyfacts/" in url:
            return facts
        if "/submissions/" in url:
            return {"name": "Test Company", "sic": "2000"}
        raise AssertionError(f"Unexpected network resource: {url}")

    with (
        patch.object(fundamentals, "AS_OF", date(2026, 8, 26)),
        patch.object(fundamentals, "PRICE_START", date(2024, 2, 18)),
        patch.object(fundamentals, "load_universe", return_value=[{"ticker": "TEST"}]),
        patch.object(fundamentals, "fetch_json", side_effect=fetch),
        patch.object(fundamentals, "nasdaq_market_snapshot", return_value=({"TEST": {"marketCap": "1000"}}, "mock:market")),
        patch.object(fundamentals, "yahoo_series", return_value={"prices": {"2026-08-26": 10}, "url": "mock:price"}),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        result = fundamentals.collect_five_factors(Path("unused.txt"), date(2026, 8, 26),
                                                  max_workers=1, quality_method=quality_method)
    if result["errors"]:
        raise AssertionError(result["errors"])
    return result


class QualityIntegrationTests(unittest.TestCase):
    def test_missing_debt_never_becomes_zero_leverage_or_enterprise_value(self) -> None:
        row = collect(companyfacts({}))["rows"][0]
        for key in ("debt", "debt_prior", "financial_leverage", "balance_sheet_accruals",
                    "enterprise_value", "sales_to_ev", "net_debt_paydown_yield"):
            with self.subTest(key=key):
                self.assertIsNone(row[key])
        self.assertEqual(row["quality_debt_status"], "missing")
        self.assertIn("incomplete_debt", row["quality_flags"])
        self.assertIn("missing_safety_neutral", row["quality_flags"])
        self.assertIsNone(row["quality_safety"])
        self.assertEqual(row["quality_coverage"], 0.75)

    def test_partial_current_debt_does_not_manufacture_a_debt_paydown(self) -> None:
        row = collect(companyfacts({2024: {"DebtAndCapitalLeaseObligations": 60},
                                   2025: {"ShortTermBorrowings": 5}}))["rows"][0]
        self.assertEqual(row["debt_prior"], 60)
        self.assertIsNone(row["debt"])
        self.assertIsNone(row["net_debt_paydown_yield"])
        self.assertIsNone(row["enterprise_value"])
        self.assertEqual(row["debt_source"]["status"], "partial")
        self.assertEqual(row["debt_source"]["observed_debt"], 5)
        self.assertEqual(row["quality_debt_status"], "partial")
        self.assertEqual(row["quality_sources"]["debt"]["components"][0]["concept"], "ShortTermBorrowings")

    def test_explicit_zero_debt_preserves_legitimate_paydown(self) -> None:
        row = collect(companyfacts({2024: {"DebtAndCapitalLeaseObligations": 60},
                                   2025: {"DebtAndCapitalLeaseObligations": 0}}))["rows"][0]
        self.assertEqual(row["debt"], 0)
        self.assertEqual(row["financial_leverage"], 0)
        self.assertEqual(row["enterprise_value"], 990)
        self.assertEqual(row["net_debt_paydown_yield"], 0.06)
        self.assertEqual(row["quality_debt_status"], "complete")
        self.assertNotIn("incomplete_debt", row["quality_flags"])

    def test_overlapping_current_debt_tags_are_flagged_as_conflicting(self) -> None:
        row = collect(companyfacts({2025: {"DebtCurrent": 5, "ShortTermBorrowings": 5,
                                           "LongTermDebtCurrent": 4, "LongTermDebtNoncurrent": 40}}))["rows"][0]
        self.assertIsNone(row["debt"])
        self.assertIsNone(row["financial_leverage"])
        self.assertIsNone(row["enterprise_value"])
        self.assertEqual(row["quality_debt_status"], "conflicting")
        self.assertIn("current_total_below_observed_components", row["debt_source"]["issues"])
        self.assertIn("incomplete_debt", row["quality_flags"])

    def test_missing_cash_prevents_enterprise_value_and_balance_sheet_accruals(self) -> None:
        row = collect(companyfacts({2024: {"DebtAndCapitalLeaseObligations": 50},
                                   2025: {"DebtAndCapitalLeaseObligations": 50}}, cash=False))["rows"][0]
        self.assertEqual(row["financial_leverage"], 0.5)
        self.assertEqual(row["net_debt_paydown_yield"], 0)
        self.assertIsNone(row["enterprise_value"])
        self.assertIsNone(row["balance_sheet_accruals"])

    def test_requested_quality_method_matches_payload_and_row_version(self) -> None:
        for method in (QUALITY_VERSION, LEGACY_QUALITY_VERSION):
            with self.subTest(method=method):
                result = collect(companyfacts({2024: {"DebtAndCapitalLeaseObligations": 50},
                                               2025: {"DebtAndCapitalLeaseObligations": 50}}), method)
                self.assertEqual(result["quality_version"], method)
                self.assertEqual(result["rows"][0]["quality_version"], method)

    def test_ranking_rejects_incompatible_scored_snapshot_before_fetching_revisions(self) -> None:
        spec = importlib.util.spec_from_file_location("quality_integration_ranker", REPO_ROOT / "scripts/rank_six_factor_model.py")
        ranker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ranker)
        args = SimpleNamespace(output_dir="/tmp/quality-integration-unused", workers=1,
                               universe=None, factor_input="unused.json", quality_method=QUALITY_VERSION)
        with (patch.object(ranker, "parse_args", return_value=args),
              patch.object(ranker, "load_json", return_value={"quality_version": LEGACY_QUALITY_VERSION}),
              patch.object(ranker, "refresh_revisions") as refresh):
            with self.assertRaisesRegex(ValueError, "different quality method"):
                ranker.main()
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
