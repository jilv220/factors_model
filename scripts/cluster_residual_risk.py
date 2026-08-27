#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from factors_model.risk_clusters import (
    RiskClusterSettings,
    analyze_risk_clusters,
    fetch_adjusted_returns,
    fetch_missing_sectors,
    load_returns_panel,
    load_risk_universe,
    load_sector_map,
    write_risk_cluster_outputs,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cluster stocks using correlations of market/sector-neutral residual returns."
    )
    parser.add_argument("--universe", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--returns", help="Wide CSV of daily returns with a date column.")
    source.add_argument("--prices", help="Wide CSV of adjusted prices with a date column.")
    source.add_argument(
        "--fetch-prices",
        action="store_true",
        help="Fetch adjusted prices from Yahoo; ticker symbols are sent externally.",
    )
    parser.add_argument("--sector-map", default=None)
    parser.add_argument(
        "--fetch-sector-metadata",
        action="store_true",
        help="Resolve missing sectors through Nasdaq; ticker symbols are sent externally.",
    )
    parser.add_argument("--weights-column", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--market", default="VTI")
    parser.add_argument("--style-factors", default="")
    parser.add_argument("--lookback-days", type=int, default=504)
    parser.add_argument("--min-observations", type=int, default=252)
    parser.add_argument("--min-pair-observations", type=int, default=126)
    parser.add_argument("--residual-correlation-threshold", type=float, default=0.35)
    parser.add_argument("--cluster-cap", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sector_map = load_sector_map(Path(args.sector_map)) if args.sector_map else {}
    universe = load_risk_universe(
        Path(args.universe),
        sector_map=sector_map,
        weights_column=args.weights_column,
    )
    metadata_source = None
    if args.fetch_sector_metadata:
        universe, metadata_source = fetch_missing_sectors(universe)

    styles = tuple(
        value.strip().upper()
        for value in str(args.style_factors).split(",")
        if value.strip()
    )
    settings = RiskClusterSettings(
        as_of=args.as_of,
        market=args.market.upper(),
        lookback_days=args.lookback_days,
        min_observations=args.min_observations,
        min_pair_observations=args.min_pair_observations,
        residual_correlation_threshold=args.residual_correlation_threshold,
        cluster_cap=args.cluster_cap,
        style_factors=styles,
    )
    if args.fetch_prices:
        symbols = {row["ticker"] for row in universe}
        symbols.add(settings.market)
        symbols.update(row["sector_etf"] for row in universe if row.get("sector_etf"))
        symbols.update(styles)
        returns, data_source = fetch_adjusted_returns(
            symbols,
            as_of=settings.as_of,
            lookback_days=settings.lookback_days,
        )
    else:
        returns, data_source = load_returns_panel(
            Path(args.returns) if args.returns else None,
            Path(args.prices) if args.prices else None,
        )
    if metadata_source:
        data_source["sector_metadata"] = metadata_source
    payload = analyze_risk_clusters(
        universe,
        returns,
        settings,
        data_source=data_source,
    )
    outputs = write_risk_cluster_outputs(Path(args.output_dir), payload)
    print(
        json.dumps(
            {
                "model_id": payload["model_id"],
                "as_of": payload["as_of"],
                "eligible_rows": payload["coverage"]["eligible_rows"],
                "clusters": len(payload["cluster_summary"]),
                "outputs": outputs,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
