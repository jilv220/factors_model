"""Build the established six-factor ranking for any selected ticker universe.

Normal runs collect the five reported-fundamental factors and analyst revisions
for a CSV, JSON, text, or directory universe. Frozen factor and revision JSONs
remain available only as explicit offline regression fixtures. Outputs are
portfolio-free: no holdings, sides, quantities, or position weights are used.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


FACTOR_FIELDS = (
    "quality_score",
    "fundamental_momentum_score",
    "analyst_revisions_score",
    "valuation_score",
    "conservative_investment_score",
    "shareholder_yield_score",
)

DEFAULT_WEIGHTS = {
    "quality_score": 0.25,
    "fundamental_momentum_score": 0.25,
    "analyst_revisions_score": 1.0 / 6.0,
    "valuation_score": 1.0 / 9.0,
    "conservative_investment_score": 1.0 / 9.0,
    "shareholder_yield_score": 1.0 / 9.0,
}

CSV_FIELDS = (
    "overall_rank",
    "ticker",
    "company",
    "overall_score",
    "factor_coverage",
    "quality_score",
    "quality_rank",
    "fundamental_momentum_score",
    "fundamental_momentum_rank",
    "fundamental_momentum_method",
    "analyst_revisions_score",
    "analyst_revisions_rank",
    "current_quarter_eps_estimate",
    "eps_estimate_30d_ago",
    "eps_revision_30d",
    "revision_status",
    "analyst_count",
    "valuation_score",
    "valuation_rank",
    "conservative_investment_score",
    "conservative_investment_rank",
    "shareholder_yield_score",
    "shareholder_yield_rank",
    "sector",
    "price_date",
    "latest_quarter",
    "latest_annual",
    "data_status",
    "revision_source_url",
    "quality_version",
    "quality_status",
    "quality_coverage",
    "quality_flags",
    "quality_annual_end",
    "quality_profitability",
    "quality_safety",
    "quality_accrual_penalty_points",
    "quality_peer_group",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--universe",
        help="Ticker CSV/JSON/text file or directory of CSV files; factors are collected live.",
    )
    source.add_argument(
        "--factor-input",
        help="Explicit five-factor JSON fixture for offline regression; not a universe selector.",
    )
    parser.add_argument(
        "--revisions",
        help="Optional frozen Yahoo analyst-revision JSON; omitted means fetch the selected universe.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--as-of", required=True, help="Overall model snapshot date (YYYY-MM-DD).")
    parser.add_argument("--factor-as-of", required=True, help="Required as-of date in the five-factor input.")
    parser.add_argument("--revision-as-of", help="Required date when a frozen revision fixture is used.")
    parser.add_argument("--refresh-revisions", action="store_true", help="Fetch Yahoo revisions even when a fixture is supplied.")
    parser.add_argument("--sleep", type=float, default=0.10)
    parser.add_argument("--workers", type=int, default=4, help="Concurrent SEC data requests.")
    parser.add_argument(
        "--sec-user-agent",
        default="factors-model public-equity research research-contact@example.com",
        help="SEC-compliant user agent including a contact address.",
    )
    parser.add_argument("--weight-quality", type=float, default=DEFAULT_WEIGHTS["quality_score"])
    parser.add_argument("--quality-method", choices=["sphq_quality_v1", "profitability_quality_v2"], default=None)
    parser.add_argument(
        "--weight-fundamental-momentum",
        type=float,
        default=DEFAULT_WEIGHTS["fundamental_momentum_score"],
    )
    parser.add_argument(
        "--weight-analyst-revisions",
        type=float,
        default=DEFAULT_WEIGHTS["analyst_revisions_score"],
    )
    parser.add_argument("--weight-valuation", type=float, default=DEFAULT_WEIGHTS["valuation_score"])
    parser.add_argument(
        "--weight-conservative-investment",
        type=float,
        default=DEFAULT_WEIGHTS["conservative_investment_score"],
    )
    parser.add_argument(
        "--weight-shareholder-yield",
        type=float,
        default=DEFAULT_WEIGHTS["shareholder_yield_score"],
    )
    return parser.parse_args()


def json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError(f"expected a JSON object with a rows array: {path}")
    return payload


def unique_rows(payload: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for raw in payload["rows"]:
        ticker = str(raw.get("ticker") or "").upper().strip()
        if not ticker:
            raise ValueError(f"{label} contains a row without a ticker")
        if ticker in rows:
            raise ValueError(f"{label} contains duplicate ticker {ticker}")
        rows[ticker] = raw
    if not rows:
        raise ValueError(f"{label} contains no rows")
    return rows


def excel_percent_rank_inc(values: list[float], value: float, significance: int = 3) -> float:
    """Match the reference workbook's PERCENTRANK.INC(..., significance=3)."""
    ordered = sorted(values)
    if len(ordered) < 2 or value < ordered[0] or value > ordered[-1]:
        raise ValueError("PERCENTRANK.INC requires at least two values and an in-range observation")
    left = bisect.bisect_left(ordered, value)
    if left < len(ordered) and ordered[left] == value:
        percentile = left / (len(ordered) - 1)
    else:
        lower = ordered[left - 1]
        upper = ordered[left]
        fraction = (value - lower) / (upper - lower)
        percentile = ((left - 1) + fraction) / (len(ordered) - 1)
    scale = 10**significance
    return math.floor((percentile + 1e-14) * scale) / scale


def rank_map(rows: list[dict[str, Any]], field: str) -> dict[str, int | None]:
    values = [finite(row.get(field)) for row in rows]
    return {
        row["ticker"]: None if value is None else 1 + sum(other is not None and other > value for other in values)
        for row, value in zip(rows, values)
    }


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-").replace("/", "-")


def _frame_row(frame: Any, label: str) -> dict[str, Any]:
    if frame is None or getattr(frame, "empty", True) or label not in frame.index:
        return {}
    return {str(key): value for key, value in frame.loc[label].items()}


def fetch_revision(ticker: str, yf: Any) -> dict[str, Any]:
    symbol = yahoo_symbol(ticker)
    last_error: str | None = None
    for attempt in range(1, 4):
        try:
            security = yf.Ticker(symbol)
            trend = _frame_row(security.get_eps_trend(), "0q")
            revisions = _frame_row(security.get_eps_revisions(), "0q")
            estimate = _frame_row(security.get_earnings_estimate(), "0q")
            current = finite(trend.get("current"))
            prior = finite(trend.get("30daysAgo"))
            analysts_value = finite(estimate.get("numberOfAnalysts"))
            analyst_count = int(analysts_value) if analysts_value is not None else None
            revision_pct = None
            if current is not None and prior is not None and abs(prior) >= 0.0001:
                revision_pct = (current - prior) / abs(prior)
            if current is None and prior is None:
                status = "NO CURRENT-QUARTER DATA"
            elif revision_pct is None:
                status = "INVALID 30D BASE"
            elif analyst_count is not None and analyst_count < 3:
                status = "THIN COVERAGE"
            else:
                status = "OK"
            up = finite(revisions.get("upLast30days"))
            down = finite(revisions.get("downLast30days"))
            return {
                "ticker": ticker,
                "yahoo_symbol": symbol,
                "period_bucket": "0q",
                "current_eps_estimate": current,
                "eps_estimate_30d_ago": prior,
                "revision_pct_30d": revision_pct,
                "number_of_analysts": analyst_count,
                "up_last_30d": int(up) if up is not None else None,
                "down_last_30d": int(down) if down is not None else None,
                "currency": trend.get("currency") or estimate.get("currency"),
                "status": status,
                "source_url": f"https://finance.yahoo.com/quote/{symbol}/analysis/",
                "error": None,
            }
        except Exception as exc:  # Yahoo endpoints can transiently fail or rate-limit.
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(1.5 * attempt)
    return {
        "ticker": ticker,
        "yahoo_symbol": symbol,
        "period_bucket": "0q",
        "current_eps_estimate": None,
        "eps_estimate_30d_ago": None,
        "revision_pct_30d": None,
        "number_of_analysts": None,
        "up_last_30d": None,
        "down_last_30d": None,
        "currency": None,
        "status": "FETCH ERROR",
        "source_url": f"https://finance.yahoo.com/quote/{symbol}/analysis/",
        "error": last_error,
    }


def refresh_revisions(tickers: list[str], sleep_seconds: float) -> dict[str, Any]:
    import yfinance as yf

    rows: list[dict[str, Any]] = []
    for index, ticker in enumerate(tickers, start=1):
        row = fetch_revision(ticker, yf)
        rows.append(row)
        print(f"[{index:03d}/{len(tickers):03d}] {ticker:<6} {row['status']}", flush=True)
        time.sleep(max(0.0, sleep_seconds))
    return {
        "provider": "Yahoo Finance via yfinance",
        "yfinance_version": yf.__version__,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "period_definition": "0q current fiscal quarter in Yahoo Finance analysis data",
        "revision_definition": "(current EPS consensus - EPS consensus 30 days ago) / abs(EPS consensus 30 days ago)",
        "row_count": len(rows),
        "valid_revision_count": sum(finite(row.get("revision_pct_30d")) is not None for row in rows),
        "rows": rows,
    }


def score_model(
    factor_payload: dict[str, Any],
    revision_payload: dict[str, Any],
    weights: dict[str, float],
    as_of: str,
) -> dict[str, Any]:
    factors = unique_rows(factor_payload, "factor input")
    revisions = unique_rows(revision_payload, "revision input")
    if set(factors) != set(revisions):
        missing = sorted(set(factors) - set(revisions))
        extra = sorted(set(revisions) - set(factors))
        raise ValueError(f"revision universe mismatch; missing={missing}, extra={extra}")

    # The verified workbook calculates PERCENTRANK.INC across all cohort rows.
    # Its blank raw-revision cells occupy zero in that distribution, while those
    # rows still receive no revision score and renormalize the overall weights.
    revision_distribution = [finite(revisions[ticker].get("revision_pct_30d")) or 0.0 for ticker in factors]
    rows: list[dict[str, Any]] = []
    for ticker, base in factors.items():
        revision = revisions[ticker]
        revision_pct = finite(revision.get("revision_pct_30d"))
        revision_percentile = (
            excel_percent_rank_inc(revision_distribution, revision_pct) if revision_pct is not None else None
        )
        revision_score = 1.0 + 9.0 * revision_percentile if revision_percentile is not None else None
        row = {
            "ticker": ticker,
            "company": base.get("issue_name") or base.get("company") or base.get("entity_name"),
            "quality_score": finite(base.get("quality_score")),
            "fundamental_momentum_score": finite(base.get("fundamental_momentum_score")),
            "fundamental_momentum_method": base.get("fundamental_momentum_method"),
            "analyst_revisions_score": revision_score,
            "analyst_revisions_percentile": revision_percentile,
            "current_quarter_eps_estimate": finite(revision.get("current_eps_estimate")),
            "eps_estimate_30d_ago": finite(revision.get("eps_estimate_30d_ago")),
            "eps_revision_30d": revision_pct,
            "revision_status": revision.get("status"),
            "analyst_count": revision.get("number_of_analysts"),
            "revision_source_url": revision.get("source_url"),
            "valuation_score": finite(base.get("valuation_score")),
            "conservative_investment_score": finite(base.get("conservative_investment_score")),
            "shareholder_yield_score": finite(base.get("shareholder_yield_score")),
            "sector": base.get("nasdaq_sector") or base.get("sector"),
            "price_date": base.get("price_date"),
            "latest_quarter": base.get("latest_quarter_end"),
            "latest_annual": base.get("latest_annual_end"),
            "data_status": base.get("data_status"),
        }
        for key in ("quality_version", "quality_status", "quality_coverage", "quality_flags",
                    "quality_annual_end", "quality_profitability", "quality_safety",
                    "quality_accrual_penalty_points", "quality_peer_group"):
            if key in base and (key == "quality_version" or base.get("quality_version") == "profitability_quality_v2"):
                row[key] = base[key]
        available = [(row[field], weights[field]) for field in FACTOR_FIELDS if row[field] is not None]
        denominator = sum(weight for _, weight in available)
        row["factor_coverage"] = len(available)
        row["weight_coverage"] = denominator
        row["overall_score"] = (
            sum(float(score) * weight for score, weight in available) / denominator if denominator else None
        )
        rows.append(row)

    for field in FACTOR_FIELDS + ("overall_score",):
        ranks = rank_map(rows, field)
        rank_field = "overall_rank" if field == "overall_score" else field.removesuffix("_score") + "_rank"
        for row in rows:
            row[rank_field] = ranks[row["ticker"]]
    rows.sort(key=lambda row: (-(row.get("overall_score") or -math.inf), row["ticker"]))

    factor_methodology = factor_payload.get("methodology") or {}
    return {
        "model_id": "low_beta_low_bad_beta_six_factor_v2" if factor_payload.get("quality_version") == "profitability_quality_v2" else "low_beta_low_bad_beta_six_factor_v1",
        **({"quality_version": factor_payload["quality_version"]} if factor_payload.get("quality_version") else {}),
        "as_of": as_of,
        "factor_data_as_of": factor_payload.get("as_of"),
        "revision_data_as_of": str(revision_payload.get("retrieved_at_utc") or "")[:10],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factor_names": [
            "quality",
            "fundamental_momentum",
            "analyst_revisions",
            "valuation",
            "conservative_investment",
            "shareholder_yield",
        ],
        "weights": weights,
        "missing_factor_policy": "Renormalize the configured weights across available factor scores.",
        "methodology": {
            "quality": factor_methodology.get("quality"),
            "fundamental_momentum": factor_methodology.get("fundamental_momentum"),
            "analyst_revisions": (
                "Current-quarter EPS consensus change versus 30 days earlier; reference-workbook "
                "PERCENTRANK.INC with three-decimal significance maps to a 1-10 score."
            ),
            "valuation": factor_methodology.get("valuation"),
            "conservative_investment": factor_methodology.get("conservative_investment"),
            "shareholder_yield": factor_methodology.get("shareholder_yield"),
            "overall": "Configured weighted average of available 1-10 factor scores.",
        },
        "sources": {
            "five_factor_input": factor_payload.get("universe_source"),
            "five_factor_market_data": factor_payload.get("market_data_source"),
            "analyst_revisions_provider": revision_payload.get("provider"),
            "analyst_revisions_retrieved_at": revision_payload.get("retrieved_at_utc"),
            "analyst_revisions_definition": revision_payload.get("revision_definition"),
        },
        "coverage": {
            "universe_rows": len(rows),
            "six_factor_rows": sum(row["factor_coverage"] == 6 for row in rows),
            "five_factor_rows": sum(row["factor_coverage"] == 5 for row in rows),
            "valid_revision_rows": sum(row["analyst_revisions_score"] is not None for row in rows),
            "thin_revision_rows": sum(row["revision_status"] == "THIN COVERAGE" for row in rows),
        },
        "rows": rows,
    }


def write_outputs(output_dir: Path, payload: dict[str, Any], revisions: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(json_text(payload), encoding="utf-8")
    (output_dir / "analyst_revisions.json").write_text(json_text(revisions), encoding="utf-8")
    with (output_dir / "rankings.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["rows"])


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.universe:
        from factors_model.fundamentals import collect_five_factors, write_five_factor_snapshot

        factor_path = output_dir / "five_factor_inputs.json"
        factor_payload = collect_five_factors(
            Path(args.universe),
            date.fromisoformat(args.factor_as_of),
            user_agent=args.sec_user_agent,
            max_workers=args.workers,
            quality_method=args.quality_method or "profitability_quality_v2",
        )
        write_five_factor_snapshot(factor_path, factor_payload)
    else:
        factor_path = Path(args.factor_input).expanduser().resolve()
        factor_payload = load_json(factor_path)
        actual_method = factor_payload.get("quality_version", "sphq_quality_v1")
        if args.quality_method and args.quality_method != actual_method:
            raise ValueError("The scored input uses a different quality method; rebuild its quality inputs before ranking")
    if str(factor_payload.get("as_of")) != args.factor_as_of:
        raise ValueError(
            f"factor input is dated {factor_payload.get('as_of')!r}, expected {args.factor_as_of!r}"
        )
    if args.refresh_revisions or not args.revisions:
        revisions = refresh_revisions(list(unique_rows(factor_payload, "factor input")), args.sleep)
    else:
        revision_path = Path(args.revisions).expanduser().resolve()
        revisions = load_json(revision_path)
        actual_revision_date = str(revisions.get("retrieved_at_utc") or "")[:10]
        if args.revision_as_of and actual_revision_date != args.revision_as_of:
            raise ValueError(
                f"revision input is dated {actual_revision_date!r}, expected {args.revision_as_of!r}"
            )
    weights = {
        "quality_score": args.weight_quality,
        "fundamental_momentum_score": args.weight_fundamental_momentum,
        "analyst_revisions_score": args.weight_analyst_revisions,
        "valuation_score": args.weight_valuation,
        "conservative_investment_score": args.weight_conservative_investment,
        "shareholder_yield_score": args.weight_shareholder_yield,
    }
    if any(weight < 0 or not math.isfinite(weight) for weight in weights.values()):
        raise ValueError("factor weights must be finite and non-negative")
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"factor weights must sum to 1.0, found {sum(weights.values())}")
    payload = score_model(factor_payload, revisions, weights, args.as_of)
    write_outputs(output_dir, payload, revisions)
    print(
        json_text(
            {
                "pass": True,
                "model_id": payload["model_id"],
                "as_of": payload["as_of"],
                "output_dir": str(output_dir),
                "universe_rows": payload["coverage"]["universe_rows"],
                "six_factor_rows": payload["coverage"]["six_factor_rows"],
                "top_ticker": payload["rows"][0]["ticker"],
            }
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
