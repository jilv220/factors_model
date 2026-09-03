from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .baselines import ConfigError, json_text, load_baseline
from .runner import RunError, dry_run, run_baseline
from .validation import verify_baseline


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BAD_BETA_CONFIG = REPO_ROOT / "configs" / "bad_beta_v1.toml"
DEFAULT_SIX_FACTOR_CONFIG = REPO_ROOT / "configs" / "six_factor_ranking_v3.toml"
DEFAULT_RISK_CLUSTERS_CONFIG = REPO_ROOT / "configs" / "residual_risk_clusters_v1.toml"


def _common_run_arguments(parser: argparse.ArgumentParser, default_config: Path) -> None:
    parser.add_argument("--config", default=str(default_config))
    parser.add_argument(
        "--project-root",
        default=None,
        help="Override the input-data root; pipeline scripts remain config-relative and project-owned.",
    )
    parser.add_argument("--python", default=None, help="Override the configured pipeline Python executable.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factors", description="Run versioned local factor-pipeline baselines.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bad = subparsers.add_parser("bad-beta", help="Run the bad_beta_v1 baseline.")
    _common_run_arguments(bad, DEFAULT_BAD_BETA_CONFIG)
    bad.add_argument("--universe", default=None)
    bad.add_argument("--universe-as-of", default=None)
    bad.add_argument("--start", default=None)
    bad.add_argument("--rho", type=float, default=None)
    bad.add_argument("--window-months", type=int, default=None)
    bad.add_argument("--min-months", type=int, default=None)

    fundamental = subparsers.add_parser(
        "rank-six-factor",
        aliases=["rank-fundamentals"],
        help="Run the quality, fundamental momentum, revisions, valuation, conservative investment, and shareholder-yield baseline.",
    )
    _common_run_arguments(fundamental, DEFAULT_SIX_FACTOR_CONFIG)
    fundamental.add_argument(
        "--universe",
        default=None,
        help="Ticker CSV/JSON/text file or directory of CSVs; all six factors are built for this cohort.",
    )
    fundamental.add_argument(
        "--factor-input",
        default=None,
        help="Explicit five-factor JSON fixture; use --universe for normal runs.",
    )
    fundamental.add_argument("--revisions", default=None, help="Optional frozen Yahoo revision fixture.")
    fundamental.add_argument("--factor-as-of", default=None)
    fundamental.add_argument("--revision-as-of", default=None)
    fundamental.add_argument("--sleep", type=float, default=None)
    fundamental.add_argument("--workers", type=int, default=None)
    fundamental.add_argument("--sec-user-agent", default=None)
    fundamental.add_argument(
        "--frozen-baseline",
        action="store_true",
        help="Run the versioned five-factor and revision fixtures for exact offline regression.",
    )
    fundamental.add_argument(
        "--refresh-revisions",
        action="store_true",
        help="Force a fresh Yahoo revision pull even when a revision fixture is supplied.",
    )

    risk = subparsers.add_parser(
        "risk-clusters",
        help="Cluster stocks using correlations of market/sector-neutral residual returns.",
    )
    _common_run_arguments(risk, DEFAULT_RISK_CLUSTERS_CONFIG)
    risk.add_argument("--universe", default=None)
    risk.add_argument("--returns", default=None, help="Wide CSV of daily returns with a date column.")
    risk.add_argument("--prices", default=None, help="Wide CSV of adjusted prices with a date column.")
    risk.add_argument(
        "--allow-external-ticker-lookup",
        action="store_true",
        help="Allow ticker symbols to be sent to Yahoo for adjusted-price history.",
    )
    risk.add_argument("--sector-map", default=None)
    risk.add_argument(
        "--fetch-sector-metadata",
        action="store_true",
        help="Allow missing ticker sectors to be resolved through Nasdaq.",
    )
    risk.add_argument("--weights-column", default=None)
    risk.add_argument("--market", default=None)
    risk.add_argument("--style-factors", default=None, help="Comma-separated ETF tickers.")
    risk.add_argument("--lookback-days", type=int, default=None)
    risk.add_argument("--min-observations", type=int, default=None)
    risk.add_argument("--min-pair-observations", type=int, default=None)
    risk.add_argument("--residual-correlation-threshold", type=float, default=None)
    risk.add_argument("--cluster-cap", type=float, default=None)
    risk.add_argument(
        "--frozen-baseline",
        action="store_true",
        help="Run the deterministic local universe and return fixtures for offline regression.",
    )

    verify = subparsers.add_parser("verify", help="Validate or compare a baseline output without network access.")
    verify.add_argument("--config", required=True)
    verify.add_argument("--actual", default=None, help="Actual JSON or CSV to compare; defaults to the baseline snapshot.")
    verify.add_argument(
        "--update-run",
        action="store_true",
        help="Rewrite regression.json and run_manifest.json beside --actual after verification.",
    )
    return parser


def _run_options(args: argparse.Namespace) -> dict[str, Any]:
    ignored = {"command", "config", "dry_run"}
    return {key: value for key, value in vars(args).items() if key not in ignored}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            config = load_baseline(args.config)
            actual = Path(args.actual).expanduser().resolve() if args.actual else None
            if args.update_run and actual is None:
                parser.error("--update-run requires --actual")
            if args.update_run:
                from .runner import revalidate_run

                report = revalidate_run(config, actual)
            else:
                report = verify_baseline(config, actual)
            print(json_text(report), end="")
            return 0 if report["pass"] else 1

        expected_pipeline = {
            "bad-beta": "bad_beta",
            "rank-six-factor": "six_factor_ranking",
            "rank-fundamentals": "six_factor_ranking",
            "risk-clusters": "residual_risk_clusters",
        }[args.command]
        config = load_baseline(args.config, expected_pipeline)
        options = _run_options(args)
        if args.dry_run:
            print(json_text(dry_run(config, options)), end="")
            return 0
        result = run_baseline(config, options)
        print(json_text(result), end="")
        return 0
    except (ConfigError, RunError, FileNotFoundError, ValueError) as exc:
        print(f"factors: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
