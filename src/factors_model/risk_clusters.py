from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform


MODEL_ID = "residual_risk_clusters_v1"
TRADING_DAYS = 252

SECTOR_ETFS = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}

_SECTOR_ALIASES = {
    "basic materials": "Materials",
    "communication services": "Communication Services",
    "communications": "Communication Services",
    "consumer cyclical": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "consumer discretionary": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "energy": "Energy",
    "finance": "Financials",
    "financial services": "Financials",
    "financials": "Financials",
    "health care": "Health Care",
    "healthcare": "Health Care",
    "industrial": "Industrials",
    "industrials": "Industrials",
    "materials": "Materials",
    "real estate": "Real Estate",
    "technology": "Technology",
    "telecom": "Communication Services",
    "telecommunications": "Communication Services",
    "utilities": "Utilities",
}

_TICKER_FIELDS = ("ticker", "symbol")
_WEIGHT_FIELDS = ("portfolio_weight", "weight")


@dataclass(frozen=True)
class RiskClusterSettings:
    as_of: str
    market: str = "VTI"
    lookback_days: int = 504
    min_observations: int = 252
    min_pair_observations: int = 126
    residual_correlation_threshold: float = 0.35
    cluster_cap: float | None = None
    style_factors: tuple[str, ...] = ()


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        if text.startswith("(") and text.endswith(")"):
            text = f"-{text[1:-1]}"
        if text.endswith("%"):
            text = text[:-1]
            try:
                return float(text) / 100.0
            except ValueError:
                return None
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, (int, float, np.number)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def normalize_ticker(value: Any) -> str:
    return str(value or "").upper().strip().replace(".", "-").replace("/", "-")


def normalize_sector(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text in {"-", "N/A", "None", "nan"}:
        return None
    return _SECTOR_ALIASES.get(text.casefold(), text if text in SECTOR_ETFS else None)


def _lowered(record: dict[str, Any]) -> dict[str, Any]:
    return {str(key).strip().casefold(): value for key, value in record.items()}


def _records_from_path(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        records: list[dict[str, Any]] = []
        for candidate in sorted(path.glob("*.csv*")):
            if candidate.is_file():
                records.extend(_records_from_path(candidate))
        return records
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows") or payload.get("universe") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError(f"universe JSON must contain a rows or universe array: {path}")
        return [dict(row) for row in rows if isinstance(row, dict)]
    if ".csv" in path.name.lower():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"universe CSV has no header: {path}")
            return [dict(row) for row in reader]
    return [{"ticker": line.strip()} for line in path.read_text(encoding="utf-8").splitlines()]


def load_sector_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    rows = _records_from_path(path.expanduser().resolve())
    result: dict[str, str] = {}
    for record in rows:
        values = _lowered(record)
        ticker = normalize_ticker(next((values.get(field) for field in _TICKER_FIELDS if values.get(field)), None))
        sector = normalize_sector(values.get("sector") or values.get("nasdaq_sector"))
        if ticker and sector:
            result[ticker] = sector
    return result


def load_risk_universe(
    path: Path,
    *,
    sector_map: dict[str, str] | None = None,
    weights_column: str | None = None,
) -> list[dict[str, Any]]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"universe input not found: {path}")
    sector_map = sector_map or {}
    by_ticker: dict[str, dict[str, Any]] = {}
    for record in _records_from_path(path):
        values = _lowered(record)
        ticker = normalize_ticker(next((values.get(field) for field in _TICKER_FIELDS if values.get(field)), None))
        if not ticker:
            continue
        sector = normalize_sector(values.get("sector") or values.get("nasdaq_sector")) or sector_map.get(ticker)
        requested_weight = weights_column.casefold() if weights_column else None
        weight_value = values.get(requested_weight) if requested_weight else next(
            (values.get(field) for field in _WEIGHT_FIELDS if values.get(field) not in (None, "")),
            None,
        )
        weight = finite_number(weight_value)
        side = str(values.get("side") or "").strip().casefold()
        if weight is not None and weight >= 0 and side in {"short", "s", "sell"}:
            weight = -weight
        row = {
            "ticker": ticker,
            "issue_name": str(values.get("company") or values.get("issue_name") or values.get("name") or "").strip() or None,
            "sector": sector,
            "sector_etf": SECTOR_ETFS.get(sector) if sector else None,
            "portfolio_weight": weight,
            "side": side or ("short" if weight is not None and weight < 0 else "long" if weight is not None else None),
        }
        if ticker not in by_ticker:
            by_ticker[ticker] = row
        else:
            existing = by_ticker[ticker]
            for field in ("issue_name", "sector", "sector_etf", "portfolio_weight", "side"):
                if existing.get(field) is None and row.get(field) is not None:
                    existing[field] = row[field]
    if not by_ticker:
        raise ValueError(f"universe contains no tickers: {path}")
    return [by_ticker[ticker] for ticker in sorted(by_ticker)]


def _read_wide_panel(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if frame.empty:
        raise ValueError(f"market-data panel has no rows: {path}")
    date_column = next((column for column in frame.columns if str(column).strip().casefold() in {"date", "as_of"}), None)
    if date_column is None:
        raise ValueError(f"market-data panel needs a date column: {path}")
    dates = pd.to_datetime(frame.pop(date_column), errors="coerce")
    if dates.isna().any():
        raise ValueError(f"market-data panel contains invalid dates: {path}")
    frame.columns = [normalize_ticker(column) for column in frame.columns]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame.index = pd.DatetimeIndex(dates).tz_localize(None)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame


def load_returns_panel(returns_path: Path | None = None, prices_path: Path | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    if bool(returns_path) == bool(prices_path):
        raise ValueError("provide exactly one of --returns or --prices")
    if returns_path is not None:
        path = returns_path.expanduser().resolve()
        return _read_wide_panel(path), {"mode": "local_returns", "path": str(path)}
    path = prices_path.expanduser().resolve()  # type: ignore[union-attr]
    prices = _read_wide_panel(path)
    return prices.pct_change(fill_method=None), {"mode": "local_adjusted_prices", "path": str(path)}


def fetch_adjusted_returns(
    symbols: Iterable[str],
    *,
    as_of: str,
    lookback_days: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    import yfinance as yf

    end = date.fromisoformat(as_of) + timedelta(days=1)
    start = end - timedelta(days=max(lookback_days * 2, 730))
    ordered = sorted({normalize_ticker(symbol) for symbol in symbols if normalize_ticker(symbol)})
    frame = yf.download(
        ordered,
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    if frame.empty:
        raise ValueError("yfinance returned no price rows")
    if isinstance(frame.columns, pd.MultiIndex):
        level0 = set(frame.columns.get_level_values(0))
        key = "Adj Close" if "Adj Close" in level0 else "Close"
        prices = frame[key].copy()
    else:
        key = "Adj Close" if "Adj Close" in frame.columns else "Close"
        prices = frame[[key]].rename(columns={key: ordered[0]})
    if isinstance(prices, pd.Series):
        prices = prices.to_frame(name=ordered[0])
    prices.columns = [normalize_ticker(column) for column in prices.columns]
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index)).tz_localize(None)
    returns = prices.sort_index().pct_change(fill_method=None)
    return returns, {
        "mode": "external_yahoo_adjusted_prices",
        "provider": "Yahoo Finance via yfinance",
        "yfinance_version": yf.__version__,
        "external_symbols_sent": ordered,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
    }


def fetch_missing_sectors(universe: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from .fundamentals import nasdaq_market_snapshot

    market, source = nasdaq_market_snapshot()
    updated = []
    for original in universe:
        row = dict(original)
        if not row.get("sector"):
            ticker = row["ticker"]
            match = market.get(ticker) or market.get(ticker.replace("-", "/")) or {}
            sector = normalize_sector(match.get("sector"))
            if sector:
                row["sector"] = sector
                row["sector_etf"] = SECTOR_ETFS[sector]
        updated.append(row)
    return updated, {"provider": "Nasdaq stock screener", "source": source}


def _orthogonalized_factor(raw: pd.Series, market: pd.Series) -> pd.Series:
    frame = pd.concat([raw.rename("raw"), market.rename("market")], axis=1).dropna()
    result = pd.Series(index=raw.index, dtype=float)
    if len(frame) < 3 or float(frame["market"].var()) == 0.0:
        return result
    design = np.column_stack([np.ones(len(frame)), frame["market"].to_numpy(dtype=float)])
    coefficients, *_ = np.linalg.lstsq(design, frame["raw"].to_numpy(dtype=float), rcond=None)
    result.loc[frame.index] = frame["raw"] - design @ coefficients
    return result


def _fit_stock(
    row: dict[str, Any],
    returns: pd.DataFrame,
    settings: RiskClusterSettings,
) -> tuple[dict[str, Any], pd.Series | None, pd.Series | None]:
    ticker = row["ticker"]
    base = {
        **row,
        "cluster_id": None,
        "cluster_size": None,
        "suggested_weight": None,
        "cluster_multiplier": None,
        "alpha_daily": None,
        "beta_market": None,
        "beta_sector": None,
        "r_squared": None,
        "residual_volatility": None,
        "stock_volatility": None,
        "observations": 0,
        "condition_number": None,
        "status": None,
        "warnings": [],
    }
    if ticker not in returns:
        base["status"] = "MISSING_STOCK_RETURNS"
        return base, None, None
    market = settings.market
    if market not in returns:
        raise ValueError(f"market factor {market} is missing from the return panel")

    factors: dict[str, pd.Series] = {"market": returns[market]}
    sector_etf = row.get("sector_etf")
    if sector_etf:
        if sector_etf not in returns:
            base["status"] = "MISSING_SECTOR_FACTOR"
            base["warnings"].append(f"sector ETF {sector_etf} is missing")
            return base, None, None
        factors["sector"] = _orthogonalized_factor(returns[sector_etf], returns[market])
    else:
        base["warnings"].append("sector unavailable; market-only neutralization used")

    for style in settings.style_factors:
        if style not in returns:
            raise ValueError(f"requested style factor {style} is missing from the return panel")
        factors[f"style_{style}"] = _orthogonalized_factor(returns[style], returns[market])

    frame = pd.concat([returns[ticker].rename("stock"), *[series.rename(name) for name, series in factors.items()]], axis=1)
    frame = frame.dropna()
    base["observations"] = int(len(frame))
    if len(frame) < settings.min_observations:
        base["status"] = "INSUFFICIENT_HISTORY"
        base["warnings"].append(
            f"{len(frame)} observations; minimum is {settings.min_observations}"
        )
        return base, None, None

    factor_names = list(factors)
    design = np.column_stack([np.ones(len(frame)), frame[factor_names].to_numpy(dtype=float)])
    target = frame["stock"].to_numpy(dtype=float)
    coefficients, _, rank, _ = np.linalg.lstsq(design, target, rcond=None)
    if rank < design.shape[1]:
        base["status"] = "RANK_DEFICIENT"
        base["warnings"].append("factor matrix is rank deficient")
        return base, None, None

    fitted = design @ coefficients
    residual = pd.Series(target - fitted, index=frame.index, name=ticker)
    centered = target - target.mean()
    total_ss = float(centered @ centered)
    residual_ss = float(residual.to_numpy() @ residual.to_numpy())
    r_squared = 1.0 - residual_ss / total_ss if total_ss > 0 else 0.0
    condition_number = float(np.linalg.cond(design))
    if condition_number > 1e6:
        base["warnings"].append(f"high factor condition number: {condition_number:.1f}")

    base.update(
        {
            "alpha_daily": float(coefficients[0]),
            "beta_market": float(coefficients[1]),
            "beta_sector": float(coefficients[factor_names.index("sector") + 1]) if "sector" in factor_names else None,
            "r_squared": float(r_squared),
            "residual_volatility": float(residual.std(ddof=1) * math.sqrt(TRADING_DAYS)),
            "stock_volatility": float(frame["stock"].std(ddof=1) * math.sqrt(TRADING_DAYS)),
            "condition_number": condition_number,
            "status": "OK_MARKET_ONLY" if not sector_etf else "OK",
        }
    )
    for index, name in enumerate(factor_names, start=1):
        if name.startswith("style_"):
            base[f"beta_{name}"] = float(coefficients[index])
    return base, residual, frame["stock"].rename(ticker)


def pairwise_correlation(
    series_by_ticker: dict[str, pd.Series],
    *,
    min_observations: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    tickers = sorted(series_by_ticker)
    matrix = pd.DataFrame(np.nan, index=tickers, columns=tickers, dtype=float)
    overlaps = pd.DataFrame(0, index=tickers, columns=tickers, dtype=int)
    for left_index, left in enumerate(tickers):
        for right in tickers[left_index:]:
            pair = pd.concat([series_by_ticker[left], series_by_ticker[right]], axis=1).dropna()
            count = len(pair)
            overlaps.loc[left, right] = overlaps.loc[right, left] = count
            if left == right and count:
                correlation = 1.0
            elif count >= min_observations and pair.iloc[:, 0].std() > 0 and pair.iloc[:, 1].std() > 0:
                correlation = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
            else:
                correlation = math.nan
            matrix.loc[left, right] = matrix.loc[right, left] = correlation
    return matrix, overlaps


def nearest_correlation(matrix: pd.DataFrame) -> tuple[pd.DataFrame, float, int]:
    values = matrix.to_numpy(dtype=float)
    missing_pairs = int(np.isnan(values).sum() // 2)
    values = np.nan_to_num(values, nan=0.0)
    values = (values + values.T) / 2.0
    np.fill_diagonal(values, 1.0)
    eigenvalues, eigenvectors = np.linalg.eigh(values)
    clipped = np.maximum(eigenvalues, 1e-8)
    projected = eigenvectors @ np.diag(clipped) @ eigenvectors.T
    diagonal = np.sqrt(np.maximum(np.diag(projected), 1e-12))
    projected = projected / np.outer(diagonal, diagonal)
    projected = np.clip((projected + projected.T) / 2.0, -1.0, 1.0)
    np.fill_diagonal(projected, 1.0)
    adjustment = float(np.linalg.norm(projected - values, ord="fro"))
    return pd.DataFrame(projected, index=matrix.index, columns=matrix.columns), adjustment, missing_pairs


def cluster_correlation(matrix: pd.DataFrame, threshold: float) -> dict[str, str]:
    tickers = list(matrix.index)
    if not tickers:
        return {}
    if len(tickers) == 1:
        return {tickers[0]: "C01"}
    distance = np.sqrt(np.maximum(0.0, (1.0 - matrix.to_numpy(dtype=float)) / 2.0))
    np.fill_diagonal(distance, 0.0)
    condensed = squareform(distance, checks=True)
    tree = linkage(condensed, method="average", optimal_ordering=True)
    cut_distance = math.sqrt(max(0.0, (1.0 - threshold) / 2.0))
    raw_labels = fcluster(tree, t=cut_distance, criterion="distance")
    members: dict[int, list[str]] = {}
    for ticker, label in zip(tickers, raw_labels):
        members.setdefault(int(label), []).append(ticker)
    ordered_labels = sorted(members, key=lambda label: (min(members[label]), tuple(sorted(members[label]))))
    normalized = {label: f"C{index:02d}" for index, label in enumerate(ordered_labels, start=1)}
    return {ticker: normalized[int(label)] for ticker, label in zip(tickers, raw_labels)}


def _mean_off_diagonal(matrix: pd.DataFrame) -> float | None:
    if len(matrix) < 2:
        return None
    values = matrix.to_numpy(dtype=float)
    selected = values[np.triu_indices_from(values, k=1)]
    selected = selected[np.isfinite(selected)]
    return float(selected.mean()) if len(selected) else None


def analyze_risk_clusters(
    universe: list[dict[str, Any]],
    returns: pd.DataFrame,
    settings: RiskClusterSettings,
    *,
    data_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    as_of = pd.Timestamp(settings.as_of)
    returns = returns.loc[returns.index <= as_of].sort_index()
    if settings.market not in returns:
        raise ValueError(f"market factor {settings.market} is missing from the return panel")
    market_dates = returns.index[returns[settings.market].notna()]
    if len(market_dates) > settings.lookback_days:
        start_date = market_dates[-settings.lookback_days]
        returns = returns.loc[returns.index >= start_date]
    else:
        start_date = returns.index.min()

    rows: list[dict[str, Any]] = []
    residuals: dict[str, pd.Series] = {}
    raw_stock_returns: dict[str, pd.Series] = {}
    for row in universe:
        fitted, residual, raw = _fit_stock(row, returns, settings)
        rows.append(fitted)
        if residual is not None and raw is not None:
            residuals[row["ticker"]] = residual
            raw_stock_returns[row["ticker"]] = raw

    residual_corr, overlaps = pairwise_correlation(
        residuals,
        min_observations=settings.min_pair_observations,
    )
    raw_corr, _ = pairwise_correlation(
        raw_stock_returns,
        min_observations=settings.min_pair_observations,
    )
    projected, projection_adjustment, missing_pairs = nearest_correlation(residual_corr)
    assignments = cluster_correlation(projected, settings.residual_correlation_threshold)
    cluster_sizes: dict[str, int] = {}
    for cluster_id in assignments.values():
        cluster_sizes[cluster_id] = cluster_sizes.get(cluster_id, 0) + 1

    for row in rows:
        cluster_id = assignments.get(row["ticker"])
        if cluster_id:
            row["cluster_id"] = cluster_id
            row["cluster_size"] = cluster_sizes[cluster_id]

    cluster_summary: list[dict[str, Any]] = []
    for cluster_id in sorted(cluster_sizes):
        members = [row for row in rows if row.get("cluster_id") == cluster_id]
        weights = [row.get("portfolio_weight") for row in members]
        complete = all(weight is not None for weight in weights)
        gross = sum(abs(float(weight)) for weight in weights if weight is not None) if complete else None
        net = sum(float(weight) for weight in weights if weight is not None) if complete else None
        cap = settings.cluster_cap
        excess = max(0.0, gross - cap) if gross is not None and cap is not None else None
        if excess is not None and excess <= 1e-12:
            excess = 0.0
        multiplier = min(1.0, cap / gross) if gross not in (None, 0.0) and cap is not None else 1.0 if gross == 0 else None
        if multiplier is not None and 1.0 - multiplier <= 1e-12:
            multiplier = 1.0
        breach = bool(excess and excess > 1e-12) if excess is not None else None
        summary = {
            "cluster_id": cluster_id,
            "member_count": len(members),
            "members": ", ".join(sorted(row["ticker"] for row in members)),
            "gross_weight": gross,
            "net_weight": net,
            "cluster_cap": cap,
            "excess_gross": excess,
            "breach": breach,
            "max_multiplier": multiplier,
            "weight_complete": complete,
        }
        cluster_summary.append(summary)
        for row in members:
            row["cluster_multiplier"] = multiplier
            weight = row.get("portfolio_weight")
            row["suggested_weight"] = float(weight) * float(multiplier) if weight is not None and multiplier is not None else None

    eligible_rows = [row for row in rows if str(row.get("status", "")).startswith("OK")]
    style_names = [f"style_{ticker}" for ticker in settings.style_factors]
    factor_loadings: list[dict[str, Any]] = []
    for row in eligible_rows:
        factor_loadings.append({"ticker": row["ticker"], "factor": settings.market, "coefficient": row.get("beta_market")})
        if row.get("sector_etf"):
            factor_loadings.append({"ticker": row["ticker"], "factor": row["sector_etf"], "coefficient": row.get("beta_sector")})
        for style, name in zip(settings.style_factors, style_names):
            factor_loadings.append({"ticker": row["ticker"], "factor": style, "coefficient": row.get(f"beta_{name}")})

    future_date_count = int((returns.index > as_of).sum())
    extreme_return_count = int((returns.abs() > 0.5).sum().sum())
    return {
        "model_id": MODEL_ID,
        "as_of": settings.as_of,
        "model": {
            "market": settings.market,
            "sector_factor_method": "sector ETF residualized against market",
            "style_factor_method": "style ETF residualized against market",
            "style_factors": list(settings.style_factors),
            "lookback_days": settings.lookback_days,
            "min_observations": settings.min_observations,
            "min_pair_observations": settings.min_pair_observations,
            "residual_correlation_threshold": settings.residual_correlation_threshold,
            "cluster_method": "average-linkage hierarchical clustering",
            "distance_method": "sqrt((1-correlation)/2)",
            "cluster_cap": settings.cluster_cap,
        },
        "data_source": data_source or {},
        "coverage": {
            "universe_rows": len(rows),
            "eligible_rows": len(eligible_rows),
            "excluded_rows": len(rows) - len(eligible_rows),
            "market_only_rows": sum(row.get("status") == "OK_MARKET_ONLY" for row in rows),
            "weighted_rows": sum(row.get("portfolio_weight") is not None for row in rows),
            "return_start": start_date.date().isoformat() if pd.notna(start_date) else None,
            "return_end": returns.index.max().date().isoformat() if len(returns) else None,
            "future_date_count": future_date_count,
            "extreme_return_count": extreme_return_count,
        },
        "diagnostics": {
            "raw_mean_pairwise_correlation": _mean_off_diagonal(raw_corr),
            "residual_mean_pairwise_correlation": _mean_off_diagonal(residual_corr),
            "nearest_correlation_adjustment_frobenius": projection_adjustment,
            "missing_pair_correlations_filled_with_zero": missing_pairs,
        },
        "rows": rows,
        "cluster_summary": cluster_summary,
        "factor_loadings": factor_loadings,
        "residual_correlation": {
            "tickers": list(projected.index),
            "matrix": projected.to_numpy(dtype=float).tolist(),
            "raw_pairwise_matrix": (
                residual_corr.astype(object)
                .where(pd.notna(residual_corr), None)
                .values.tolist()
            ),
            "overlap_matrix": overlaps.to_numpy(dtype=int).tolist(),
        },
    }


def dataframe_from_matrix(payload: dict[str, Any], key: str = "matrix") -> pd.DataFrame:
    correlation = payload.get("residual_correlation") or {}
    tickers = correlation.get("tickers") or []
    return pd.DataFrame(correlation.get(key) or [], index=tickers, columns=tickers)


def write_risk_cluster_outputs(output_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.json"
    results_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    assignments_path = output_dir / "cluster_assignments.csv"
    pd.DataFrame(payload.get("rows") or []).to_csv(assignments_path, index=False)
    cluster_path = output_dir / "cluster_summary.csv"
    pd.DataFrame(payload.get("cluster_summary") or []).to_csv(cluster_path, index=False)
    loadings_path = output_dir / "factor_loadings.csv"
    pd.DataFrame(payload.get("factor_loadings") or []).to_csv(loadings_path, index=False)
    correlation_path = output_dir / "residual_correlation.csv"
    correlation = dataframe_from_matrix(payload)
    correlation.index.name = "ticker"
    correlation.to_csv(correlation_path)
    return {
        "results": str(results_path),
        "assignments": str(assignments_path),
        "cluster_summary": str(cluster_path),
        "factor_loadings": str(loadings_path),
        "residual_correlation": str(correlation_path),
    }
