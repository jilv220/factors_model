from __future__ import annotations

from copy import deepcopy
from datetime import date
import unittest

from factors_model.quality import annual_quality_inputs, score_quality


def row(ticker: str, strength: float = 1.0, **overrides: object) -> dict:
    return {
        "ticker": ticker,
        "quality_peer_group": "nonfinancial",
        "quality_net_income": 10 * strength,
        "quality_roe": 0.1 * strength,
        "quality_roa": 0.05 * strength,
        "quality_cfoa": 0.06 * strength,
        "quality_debt_to_assets": 0.5 / strength,
        "quality_cash_accruals": 0.0,
        "quality_input_flags": [],
        **overrides,
    }


def annual_payload() -> dict:
    """One coherent annual statement plus its opening balance sheet."""
    facts: dict = {}

    def add(concept: str, value: float, end: str = "2025-12-31", start: str | None = None) -> None:
        fact = {
            "val": value, "end": end, "filed": "2026-02-20",
            "form": "10-K", "accn": "annual-2025",
        }
        if start:
            fact["start"] = start
        facts.setdefault(concept, {"units": {"USD": []}})["units"]["USD"].append(fact)

    add("NetIncomeLoss", 10, start="2025-01-01")
    add("NetCashProvidedByUsedInOperatingActivities", 12, start="2025-01-01")
    add("Assets", 110)
    add("Assets", 90, end="2024-12-31")
    add("StockholdersEquity", 60)
    add("StockholdersEquity", 40, end="2024-12-31")
    add("DebtAndFinanceLeaseObligations", 30)
    return {"facts": {"us-gaap": facts}}


def split_cfo_payload() -> dict:
    payload = annual_payload()
    facts = payload["facts"]["us-gaap"]
    total = facts.pop("NetCashProvidedByUsedInOperatingActivities")["units"]["USD"][0]
    facts["NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"] = {
        "units": {"USD": [{**total, "val": 15}]},
    }
    facts["CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"] = {
        "units": {"USD": [{**total, "val": -3}]},
    }
    return payload


class QualityScoringTests(unittest.TestCase):
    def test_negative_accruals_never_create_a_bonus(self) -> None:
        scores = []
        for accruals in (0.0, -0.02, -1000.0):
            cohort = [row("LOW", 1), row("TARGET", 2, quality_cash_accruals=accruals), row("HIGH", 3)]
            score_quality(cohort)
            scores.append(cohort[1]["quality_score"])
            self.assertEqual(cohort[1]["quality_accrual_penalty"], 0.0)
        self.assertEqual(scores, [scores[0]] * 3)

    def test_positive_accrual_penalty_is_linear_then_capped_at_point_nine(self) -> None:
        scores = []
        for accruals in (0, 0.05, 0.10, 1000):
            cohort = [row("LOW", 1), row("TARGET", 2, quality_cash_accruals=accruals), row("HIGH", 3)]
            score_quality(cohort)
            scores.append(cohort[1]["quality_score"])
            self.assertLessEqual(cohort[1]["quality_accrual_penalty_points"], 0.9 + 1e-12)
        self.assertAlmostEqual(scores[0] - scores[1], 0.45)
        self.assertAlmostEqual(scores[0] - scores[2], 0.9)
        self.assertAlmostEqual(scores[2], scores[3])

    def test_extreme_legacy_balance_sheet_accruals_cannot_change_any_score(self) -> None:
        base = [row("A", 1), row("B", 2), row("C", 3)]
        mutated = deepcopy(base)
        mutated[0].update(balance_sheet_accruals=-1e100, accruals_z=1e100)
        mutated[1].update(balance_sheet_accruals=1e100, accruals_z=-1e100)
        score_quality(base)
        score_quality(mutated)
        self.assertEqual([r["quality_score"] for r in base], [r["quality_score"] for r in mutated])

    def test_loss_cannot_be_overwhelmed_by_cash_generation_and_low_debt(self) -> None:
        loser = row("LOSS", 3, quality_net_income=-10, quality_roe=-0.1,
                    quality_roa=-0.05, quality_cash_accruals=-1)
        score_quality([row("A", 1), row("B", 2), loser])
        self.assertEqual(loser["quality_component_percentiles"]["quality_roe"], 0.0)
        self.assertEqual(loser["quality_component_percentiles"]["quality_roa"], 0.0)
        self.assertAlmostEqual(loser["quality_score"], 5.5)

    def test_adverse_equity_is_floored_and_does_not_distort_valid_roe_peers(self) -> None:
        bad_equity = row("BAD", 100, quality_input_flags=["nonpositive_equity"])
        good = row("GOOD", 2)
        score_quality([row("LOW", 1), good, bad_equity])
        self.assertEqual(bad_equity["quality_component_percentiles"]["quality_roe"], 0.0)
        self.assertEqual(good["quality_component_percentiles"]["quality_roe"], 1.0)

    def test_missing_cfo_retains_its_neutral_weight_and_is_disclosed(self) -> None:
        missing = row("TARGET", 3, quality_cfoa=None, quality_cash_accruals=None)
        score_quality([row("LOW", 1), row("MID", 2), missing])
        self.assertAlmostEqual(missing["quality_profitability"], (1 + 1 + 0.5) / 3)
        self.assertAlmostEqual(missing["quality_score"], 1 + 9 * (0.75 * 2.5 / 3 + 0.25 - 0.05))
        self.assertAlmostEqual(missing["quality_coverage"], 0.75)
        self.assertEqual(missing["quality_status"], "Partial")
        self.assertIn("missing_accruals_midpoint_penalty", missing["quality_flags"])

    def test_missing_debt_is_neutral_instead_of_debt_free(self) -> None:
        missing = row("TARGET", 3, quality_debt_to_assets=None)
        score_quality([row("LOW", 1), row("MID", 2), missing])
        self.assertIsNone(missing["quality_safety"])
        self.assertAlmostEqual(missing["quality_score"], 8.875)
        self.assertAlmostEqual(missing["quality_coverage"], 0.75)
        self.assertIn("missing_safety_neutral", missing["quality_flags"])

    def test_no_leverage_only_or_single_profitability_rating(self) -> None:
        cases = [
            row("DEBT_ONLY", quality_net_income=None, quality_roe=None, quality_roa=None, quality_cfoa=None),
            row("ROA_ONLY", quality_roe=None, quality_cfoa=None),
            row("MISSING_NI", quality_net_income=None),
        ]
        score_quality(cases)
        for candidate in cases:
            with self.subTest(ticker=candidate["ticker"]):
                self.assertIsNone(candidate["quality_score"])
                self.assertIsNone(candidate["quality_rank"])
                self.assertEqual(candidate["quality_status"], "Insufficient")

    def test_financials_use_capital_and_separate_peers_without_accrual_penalty(self) -> None:
        banks = [
            row("BANK_A", 1, quality_peer_group="financial_real_estate", quality_equity_to_assets=0.1),
            row("BANK_B", 2, quality_peer_group="financial_real_estate", quality_equity_to_assets=0.2,
                quality_cash_accruals=100, quality_cfoa=-1e100, quality_debt_to_assets=1e100),
        ]
        original = deepcopy(banks)
        score_quality(original)
        score_quality(banks + [row("INDUSTRIAL", 1000)])
        self.assertEqual([r["quality_score"] for r in banks], [r["quality_score"] for r in original])
        self.assertEqual(banks[1]["quality_score"], 10.0)
        self.assertEqual(banks[1]["quality_accrual_penalty"], 0.0)
        self.assertNotIn("quality_cfoa", banks[1]["quality_component_percentiles"])

    def test_ties_and_single_company_are_neutral_with_shared_ranks(self) -> None:
        tied = [row("B"), row("A"), row("C")]
        score_quality(tied)
        self.assertEqual([r["quality_score"] for r in tied], [5.5] * 3)
        self.assertEqual([r["quality_rank"] for r in tied], [1] * 3)
        singleton = row("SOLO")
        score_quality([singleton])
        self.assertEqual(singleton["quality_score"], 5.5)

    def test_stale_observations_do_not_change_current_peer_calibration(self) -> None:
        live = [row("A", 1), row("B", 2)]
        baseline = deepcopy(live)
        score_quality(baseline)
        stale = row("STALE", 1000, quality_input_flags=["stale_annual_quality_inputs"])
        score_quality(live + [stale])
        self.assertEqual([r["quality_score"] for r in live], [r["quality_score"] for r in baseline])
        self.assertIsNone(stale["quality_score"])

    def test_nonfinite_inputs_do_not_leak_into_scores(self) -> None:
        unavailable = row("INVALID", quality_roe=float("nan"), quality_cfoa=float("inf"))
        score_quality([row("VALID"), unavailable])
        self.assertIsNone(unavailable["quality_score"])
        self.assertEqual(unavailable["quality_status"], "Insufficient")


class AnnualQualityInputTests(unittest.TestCase):
    cutoff = date(2026, 8, 26)

    def test_aligned_annual_flows_and_average_denominators_are_auditable(self) -> None:
        result = annual_quality_inputs(annual_payload(), self.cutoff, False)
        self.assertEqual(result["quality_annual_start"], "2025-01-01")
        self.assertEqual(result["quality_annual_end"], "2025-12-31")
        self.assertEqual(result["quality_average_assets"], 100)
        self.assertEqual(result["quality_average_equity"], 50)
        self.assertAlmostEqual(result["quality_roe"], 0.2)
        self.assertAlmostEqual(result["quality_roa"], 0.1)
        self.assertAlmostEqual(result["quality_cfoa"], 0.12)
        self.assertAlmostEqual(result["quality_cash_accruals"], -0.02)
        self.assertEqual(result["quality_debt"], 30)
        self.assertEqual(result["quality_sources"]["net_income"]["accession"], "annual-2025")
        self.assertEqual(result["quality_input_flags"], [])

    def test_same_end_but_different_flow_start_is_not_subtracted(self) -> None:
        payload = annual_payload()
        cfo = payload["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"][0]
        cfo["start"] = "2025-01-02"
        result = annual_quality_inputs(payload, self.cutoff, False)
        self.assertIsNone(result["quality_cfo"])
        self.assertIsNone(result["quality_cash_accruals"])
        self.assertIn("missing_matched_annual_cfo", result["quality_input_flags"])

    def test_stale_previous_year_cfo_is_not_used_for_latest_income(self) -> None:
        payload = annual_payload()
        cfo = payload["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"][0]
        cfo.update(start="2024-01-01", end="2024-12-31")
        result = annual_quality_inputs(payload, self.cutoff, False)
        self.assertIsNone(result["quality_cfo"])
        self.assertIsNone(result["quality_cash_accruals"])

    def test_total_cfo_can_be_derived_from_matching_signed_components(self) -> None:
        result = annual_quality_inputs(split_cfo_payload(), self.cutoff, False)
        self.assertEqual(result["quality_cfo"], 12)
        self.assertAlmostEqual(result["quality_cash_accruals"], -0.02)
        source = result["quality_sources"]["cfo"]
        self.assertEqual(source["concept"], "derived:total_operating_cash_flow")
        self.assertEqual([item["value"] for item in source["components"]], [15, -3])
        self.assertEqual({item["accession"] for item in source["components"]}, {"annual-2025"})
        self.assertNotIn("missing_matched_annual_cfo", result["quality_input_flags"])

    def test_cfo_component_missing_counterpart_is_not_assumed_zero(self) -> None:
        for absent in ("CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations",
                       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"):
            with self.subTest(absent=absent):
                payload = split_cfo_payload()
                del payload["facts"]["us-gaap"][absent]
                result = annual_quality_inputs(payload, self.cutoff, False)
                self.assertIsNone(result["quality_cfo"])
                self.assertIsNone(result["quality_cash_accruals"])
                self.assertIn("missing_matched_annual_cfo", result["quality_input_flags"])

    def test_cfo_components_require_the_same_filing_and_period(self) -> None:
        mismatches = ({"accn": "other-filing"}, {"filed": "2026-03-01"}, {"start": "2025-01-02"})
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                payload = split_cfo_payload()
                discontinued = payload["facts"]["us-gaap"]["CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"]["units"]["USD"][0]
                discontinued.update(mismatch)
                result = annual_quality_inputs(payload, self.cutoff, False)
                self.assertIsNone(result["quality_cfo"])
                self.assertIsNone(result["quality_cash_accruals"])
                self.assertIn("missing_matched_annual_cfo", result["quality_input_flags"])

    def test_future_restatements_and_nonannual_forms_are_excluded(self) -> None:
        payload = annual_payload()
        for node in payload["facts"]["us-gaap"].values():
            records = node["units"]["USD"]
            for record in list(records):
                records.append({**record, "val": 9999, "filed": "2026-09-01", "accn": "future"})
                records.append({**record, "val": 8888, "filed": "2026-03-01", "form": "10-Q", "accn": "quarter"})
        result = annual_quality_inputs(payload, self.cutoff, False)
        self.assertEqual(result["quality_net_income"], 10)
        self.assertEqual(result["quality_cfo"], 12)
        self.assertEqual(result["quality_assets"], 110)
        self.assertEqual(result["quality_debt"], 30)
        early = annual_quality_inputs(payload, date(2026, 2, 1), False)
        self.assertEqual(early["quality_input_flags"], ["missing_matched_annual_income_assets"])

    def test_missing_prior_assets_does_not_fabricate_an_average(self) -> None:
        payload = annual_payload()
        payload["facts"]["us-gaap"]["Assets"]["units"]["USD"].pop()
        result = annual_quality_inputs(payload, self.cutoff, False)
        self.assertIsNone(result["quality_average_assets"])
        self.assertIsNone(result["quality_roa"])
        self.assertIsNone(result["quality_cash_accruals"])
        result["ticker"] = "NO_PRIOR"
        score_quality([result])
        self.assertIsNone(result["quality_score"])

    def test_nonpositive_current_equity_is_flagged_even_with_positive_average(self) -> None:
        payload = annual_payload()
        payload["facts"]["us-gaap"]["StockholdersEquity"]["units"]["USD"][0]["val"] = -10
        result = annual_quality_inputs(payload, self.cutoff, False)
        self.assertEqual(result["quality_average_equity"], 15)
        self.assertIn("nonpositive_equity", result["quality_input_flags"])
        result["ticker"] = "NEG_EQUITY"
        score_quality([result])
        self.assertEqual(result["quality_component_percentiles"]["quality_roe"], 0.0)

    def test_missing_and_explicit_zero_debt_have_different_meanings(self) -> None:
        payload = annual_payload()
        del payload["facts"]["us-gaap"]["DebtAndFinanceLeaseObligations"]
        missing = annual_quality_inputs(payload, self.cutoff, False)
        self.assertIsNone(missing["quality_debt"])
        self.assertIsNone(missing["quality_debt_to_assets"])
        self.assertIn("incomplete_debt", missing["quality_input_flags"])
        zero_payload = annual_payload()
        zero_payload["facts"]["us-gaap"]["DebtAndFinanceLeaseObligations"]["units"]["USD"][0]["val"] = 0
        zero = annual_quality_inputs(zero_payload, self.cutoff, False)
        self.assertEqual(zero["quality_debt"], 0)
        self.assertNotIn("incomplete_debt", zero["quality_input_flags"])


if __name__ == "__main__":
    unittest.main()
