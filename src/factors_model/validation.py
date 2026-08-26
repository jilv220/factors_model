from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .baselines import BaselineConfig


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def qa_bad_beta(path: Path) -> dict[str, Any]:
    data = _load_json(path)
    rows = data.get("universe") or []
    ok_rows = [row for row in rows if row.get("status") == "OK"]
    breakpoints = data.get("breakpoints") or {}
    checks = {
        "universe_nonempty": len(rows) > 0,
        "classified_count_matches": breakpoints.get("classified_count") == len(ok_rows),
        "all_classified_exposures_finite": all(
            _finite(row.get("beta")) and _finite(row.get("bad_beta")) for row in ok_rows
        ),
        "beta_breakpoints_ordered": _finite(breakpoints.get("beta_q1"))
        and _finite(breakpoints.get("beta_q2"))
        and breakpoints["beta_q1"] <= breakpoints["beta_q2"],
        "bad_beta_breakpoints_ordered": _finite(breakpoints.get("bad_beta_q1"))
        and _finite(breakpoints.get("bad_beta_q2"))
        and breakpoints["bad_beta_q1"] <= breakpoints["bad_beta_q2"],
        "classified_rows_have_grid_cells": all(bool(row.get("grid_cell")) for row in ok_rows),
        "no_portfolio_fields": all(
            not ({"quantity", "btal_weight", "btal_side", "paper_core_weight", "grid_target_weight"} & set(row))
            for row in rows
        ),
        "no_portfolio_outputs": not (
            {"paper_core_sizing", "graduated_grid_sizing", "sizing_meta", "holdings_as_of"} & set(data)
        ),
    }
    cell_count = sum(int(row.get("count", 0)) for row in data.get("cell_summary") or [])
    checks["cell_counts_match_classified"] = cell_count == len(ok_rows)
    return {
        "pipeline": "bad_beta",
        "pass": all(checks.values()),
        "checks": checks,
        "metrics": {
            "universe_rows": len(rows),
            "classified_rows": len(ok_rows),
            "error_count": len(data.get("yahoo_errors") or {}),
            "grid_cell_rows": cell_count,
        },
    }


SIX_FACTOR_FIELDS = (
    "quality_score",
    "fundamental_momentum_score",
    "analyst_revisions_score",
    "valuation_score",
    "conservative_investment_score",
    "shareholder_yield_score",
)

SIX_FACTOR_NAMES = (
    "quality",
    "fundamental_momentum",
    "analyst_revisions",
    "valuation",
    "conservative_investment",
    "shareholder_yield",
)

PORTFOLIO_FIELDS = {
    "quantity",
    "btal_weight",
    "btal_side",
    "paper_core_weight",
    "grid_target_weight",
    "portfolio_weight",
    "target_notional",
}


def _configured_weights(weights: dict[str, Any]) -> dict[str, float]:
    return {f"{name}_score": float(weights[name]) for name in SIX_FACTOR_NAMES}


def qa_six_factor(path: Path, configured_weights: dict[str, Any]) -> dict[str, Any]:
    payload = _load_json(path)
    rows = payload.get("rows") or []
    expected_weights = _configured_weights(configured_weights)
    actual_weights = payload.get("weights") or {}
    ranks: list[int] = []
    formula_differences: list[float] = []
    coverage_matches = True
    bounded_scores = True
    for row in rows:
        rank = row.get("overall_rank")
        if isinstance(rank, int) and not isinstance(rank, bool):
            ranks.append(rank)
        available = []
        for field in SIX_FACTOR_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            number = float(value) if _finite(value) else None
            if number is None:
                bounded_scores = False
                continue
            bounded_scores = bounded_scores and 1.0 <= number <= 10.0
            available.append((number, expected_weights[field]))
        coverage_matches = coverage_matches and row.get("factor_coverage") == len(available)
        denominator = sum(weight for _, weight in available)
        expected_score = sum(score * weight for score, weight in available) / denominator if denominator else None
        if expected_score is None or not _finite(row.get("overall_score")):
            formula_differences.append(math.inf)
        else:
            formula_differences.append(abs(expected_score - float(row["overall_score"])))
    checks = {
        "model_id_matches": payload.get("model_id") == "low_beta_low_bad_beta_six_factor_v1",
        "factor_names_match": tuple(payload.get("factor_names") or ()) == SIX_FACTOR_NAMES,
        "weights_match_config": set(actual_weights) == set(expected_weights)
        and all(abs(float(actual_weights[key]) - value) <= 1e-12 for key, value in expected_weights.items()),
        "weights_sum_to_one": abs(sum(float(value) for value in actual_weights.values()) - 1.0) <= 1e-9,
        "universe_nonempty": len(rows) > 0,
        "tickers_are_unique": len({row.get("ticker") for row in rows}) == len(rows),
        "overall_ranks_are_unique_and_sequential": len(ranks) == len(rows)
        and sorted(ranks) == list(range(1, len(rows) + 1)),
        "all_factor_scores_are_bounded": bounded_scores,
        "factor_coverage_matches": coverage_matches,
        "overall_scores_match_weighted_formula": max(formula_differences, default=math.inf) <= 1e-12,
        "no_portfolio_fields": all(not (PORTFOLIO_FIELDS & set(row)) for row in rows),
    }
    coverage = payload.get("coverage") or {}
    checks["coverage_summary_matches"] = (
        coverage.get("universe_rows") == len(rows)
        and coverage.get("six_factor_rows") == sum(row.get("factor_coverage") == 6 for row in rows)
        and coverage.get("valid_revision_rows")
        == sum(row.get("analyst_revisions_score") is not None for row in rows)
    )
    return {
        "pipeline": "six_factor_ranking",
        "pass": all(checks.values()),
        "checks": checks,
        "metrics": {
            "universe_rows": len(rows),
            "six_factor_rows": sum(row.get("factor_coverage") == 6 for row in rows),
            "five_factor_rows": sum(row.get("factor_coverage") == 5 for row in rows),
            "max_formula_abs_difference": max(formula_differences, default=None),
            "top_ticker": rows[0].get("ticker") if rows else None,
        },
    }


def compare_bad_beta(expected_path: Path, actual_path: Path, regression: dict[str, Any]) -> dict[str, Any]:
    expected = _load_json(expected_path)
    actual = _load_json(actual_path)
    expected_rows = {row["ticker"]: row for row in expected.get("universe", [])}
    actual_rows = {row["ticker"]: row for row in actual.get("universe", [])}
    common = sorted(set(expected_rows) & set(actual_rows))
    missing = sorted(set(expected_rows) - set(actual_rows))
    extra = sorted(set(actual_rows) - set(expected_rows))
    beta_diffs: list[float] = []
    bad_beta_diffs: list[float] = []
    grid_mismatches: list[str] = []
    for ticker in common:
        left, right = expected_rows[ticker], actual_rows[ticker]
        if left.get("status") == right.get("status") == "OK":
            beta_diffs.append(abs(float(left["beta"]) - float(right["beta"])))
            bad_beta_diffs.append(abs(float(left["bad_beta"]) - float(right["bad_beta"])))
            if left.get("grid_cell") != right.get("grid_cell"):
                grid_mismatches.append(ticker)
        elif left.get("status") != right.get("status"):
            grid_mismatches.append(ticker)
    beta_tolerance = float(regression.get("beta_abs_tolerance", 1e-6))
    bad_beta_tolerance = float(regression.get("bad_beta_abs_tolerance", 1e-6))
    allowed_grid = int(regression.get("allowed_grid_mismatches", 0))
    max_beta = max(beta_diffs, default=0.0)
    max_bad_beta = max(bad_beta_diffs, default=0.0)
    passed = not missing and not extra and max_beta <= beta_tolerance and max_bad_beta <= bad_beta_tolerance
    passed = passed and len(grid_mismatches) <= allowed_grid
    return {
        "pipeline": "bad_beta",
        "pass": passed,
        "expected": str(expected_path),
        "actual": str(actual_path),
        "metrics": {
            "expected_rows": len(expected_rows),
            "actual_rows": len(actual_rows),
            "common_rows": len(common),
            "max_beta_abs_difference": max_beta,
            "max_bad_beta_abs_difference": max_bad_beta,
            "grid_mismatch_count": len(grid_mismatches),
        },
        "differences": {
            "missing_tickers": missing,
            "extra_tickers": extra,
            "grid_mismatch_tickers": grid_mismatches,
        },
        "tolerances": {
            "beta_abs_tolerance": beta_tolerance,
            "bad_beta_abs_tolerance": bad_beta_tolerance,
            "allowed_grid_mismatches": allowed_grid,
        },
    }


def compare_six_factor(expected_path: Path, actual_path: Path, regression: dict[str, Any]) -> dict[str, Any]:
    expected_rows = {row["ticker"]: row for row in _load_json(expected_path).get("rows", [])}
    actual_rows = {row["ticker"]: row for row in _load_json(actual_path).get("rows", [])}
    common = sorted(set(expected_rows) & set(actual_rows))
    missing = sorted(set(expected_rows) - set(actual_rows))
    extra = sorted(set(actual_rows) - set(expected_rows))
    score_diffs: list[float] = []
    rank_mismatches: list[str] = []
    factor_diffs: list[float] = []
    factor_mismatches: list[str] = []
    for ticker in common:
        left, right = expected_rows[ticker], actual_rows[ticker]
        score_diffs.append(abs(float(left["overall_score"]) - float(right["overall_score"])))
        if int(left["overall_rank"]) != int(right["overall_rank"]):
            rank_mismatches.append(ticker)
        for field in SIX_FACTOR_FIELDS:
            expected_value = left.get(field)
            actual_value = right.get(field)
            if expected_value is None and actual_value is None:
                continue
            if expected_value is None or actual_value is None:
                factor_mismatches.append(f"{ticker}:{field}")
                continue
            difference = abs(float(expected_value) - float(actual_value))
            factor_diffs.append(difference)
            if difference > float(regression.get("factor_score_abs_tolerance", 1e-12)):
                factor_mismatches.append(f"{ticker}:{field}")
    score_tolerance = float(regression.get("score_abs_tolerance", 1e-12))
    factor_tolerance = float(regression.get("factor_score_abs_tolerance", 1e-12))
    allowed_ranks = int(regression.get("allowed_rank_mismatches", 0))
    allowed_factors = int(regression.get("allowed_factor_mismatches", 0))
    max_score = max(score_diffs, default=0.0)
    max_factor = max(factor_diffs, default=0.0)
    passed = not missing and not extra and max_score <= score_tolerance
    passed = passed and max_factor <= factor_tolerance
    passed = passed and len(rank_mismatches) <= allowed_ranks and len(factor_mismatches) <= allowed_factors
    return {
        "pipeline": "six_factor_ranking",
        "pass": passed,
        "expected": str(expected_path),
        "actual": str(actual_path),
        "metrics": {
            "expected_rows": len(expected_rows),
            "actual_rows": len(actual_rows),
            "common_rows": len(common),
            "max_score_abs_difference": max_score,
            "max_factor_score_abs_difference": max_factor,
            "rank_mismatch_count": len(rank_mismatches),
            "factor_mismatch_count": len(factor_mismatches),
        },
        "differences": {
            "missing_tickers": missing,
            "extra_tickers": extra,
            "rank_mismatch_tickers": rank_mismatches,
            "factor_mismatches": factor_mismatches,
        },
        "tolerances": {
            "score_abs_tolerance": score_tolerance,
            "factor_score_abs_tolerance": factor_tolerance,
            "allowed_rank_mismatches": allowed_ranks,
            "allowed_factor_mismatches": allowed_factors,
        },
    }


def verify_baseline(config: BaselineConfig, actual: Path | None = None) -> dict[str, Any]:
    expected = config.expected_output()
    actual_path = (actual or expected).expanduser().resolve()
    if not expected.is_file():
        raise FileNotFoundError(f"expected baseline snapshot not found: {expected}")
    if not actual_path.is_file():
        raise FileNotFoundError(f"actual output not found: {actual_path}")
    regression = config.data.get("regression", {})
    if config.pipeline == "bad_beta":
        qa = qa_bad_beta(actual_path)
        comparison = compare_bad_beta(expected, actual_path, regression)
    else:
        qa = qa_six_factor(actual_path, config.data["model"]["weights"])
        comparison = compare_six_factor(expected, actual_path, regression)
    return {
        "baseline_version": config.version,
        "pipeline": config.pipeline,
        "pass": bool(qa["pass"] and comparison["pass"]),
        "qa": qa,
        "comparison": comparison,
    }
