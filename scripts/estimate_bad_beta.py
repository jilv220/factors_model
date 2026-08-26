"""Estimate beta and cash-flow-news bad beta for a public ticker universe."""

import argparse
import csv
import io
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

DATA_ROOT = Path.cwd()
UNIVERSE_PATH = DATA_ROOT / "candidates" / "20260616"
OUT_PATH = Path("/private/tmp/bad_beta_grid.json")
LOCAL_CAPE_XLS_PATH = DATA_ROOT / "data" / "shiller_cape.xls"
LOCAL_CAPE_CSV_PATH = DATA_ROOT / "data" / "shiller_cape.csv"

END = date(2026, 6, 16)
START = date(2016, 1, 1)
RHO = 0.95
ROLLING_MONTHS = 36
MIN_MONTHS = 24


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Estimate beta and cash-flow-news bad beta for a public stock universe.")
    parser.add_argument(
        "--universe",
        default=str(UNIVERSE_PATH),
        help="Generic ticker CSV or directory containing candidate CSVs.",
    )
    parser.add_argument("--universe-as-of", default=None, help="Universe snapshot date (YYYY-MM-DD).")
    parser.add_argument("--output", default=str(OUT_PATH), help="JSON output path.")
    parser.add_argument("--cape-csv", default=str(LOCAL_CAPE_CSV_PATH), help="Local Shiller CAPE CSV.")
    parser.add_argument("--cape-xls", default=str(LOCAL_CAPE_XLS_PATH), help="Local Shiller CAPE workbook.")
    parser.add_argument("--start", default=str(START), help="Price-history start date (YYYY-MM-DD).")
    parser.add_argument("--as-of", default=str(END), help="Price-history cutoff date (YYYY-MM-DD).")
    parser.add_argument("--rho", type=float, default=RHO, help="VAR news discount parameter.")
    parser.add_argument("--window-months", type=int, default=ROLLING_MONTHS)
    parser.add_argument("--min-months", type=int, default=MIN_MONTHS)
    return parser.parse_args(argv)


def normalize_ticker(value):
    return str(value or "").replace(".", "-").replace("/", "-").upper().strip()


def read_universe_csv(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        return []
    header_row = 0
    if len(lines) >= 3 and "Ticker" in lines[2] and "Asset Type" in lines[2]:
        header_row = 2
    rows = []
    for record in csv.DictReader(lines[header_row:]):
        asset_type = record.get("Asset Type")
        if asset_type and asset_type.upper() != "EQUITY":
            continue
        ticker = normalize_ticker(record.get("ticker") or record.get("Ticker"))
        if not ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "issue_name": (
                    record.get("full_name")
                    or record.get("Issue Name")
                    or record.get("issue_name")
                    or record.get("name")
                    or ticker
                ),
                "source_file": path.name,
                "source_bucket": path.name.split(" - ")[0].replace(".csv", ""),
            }
        )
    return rows


def infer_universe_as_of(path, override):
    if override:
        return date.fromisoformat(override).isoformat()
    match = re.search(r"(20\d{6})", str(path))
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
    return END.isoformat()


def read_universe(path, as_of_override=None):
    files = sorted(path.glob("decile_*.csv*")) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"no candidate CSVs found under {path}")
    by_ticker = {}
    for source in files:
        for row in read_universe_csv(source):
            by_ticker.setdefault(row["ticker"], row)
    if not by_ticker:
        raise ValueError(f"universe contains no usable tickers: {path}")
    return infer_universe_as_of(path, as_of_override), [by_ticker[ticker] for ticker in sorted(by_ticker)]


def epoch(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def yahoo_chart(symbol, start=None, end=None):
    start = START if start is None else start
    end = END if end is None else end
    suffix = (
        f"/v8/finance/chart/{quote(symbol)}"
        f"?period1={epoch(start)}&period2={epoch(end + timedelta(days=1))}"
        "&interval=1d&events=div%2Csplits"
    )
    last_error = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}{suffix}"
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read())
            break
        except Exception as exc:
            last_error = exc
    else:
        try:
            import yfinance as yf

            frame = yf.download(
                symbol,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=False,
            )
            if frame.empty:
                raise ValueError("yfinance returned no rows")
            column = "Adj Close" if "Adj Close" in frame.columns else "Close"
            values = frame[column]
            if isinstance(values, pd.DataFrame):
                values = values.iloc[:, 0]
            daily = {index.date(): float(value) for index, value in values.dropna().items()}
            return pd.Series(daily, name=symbol).sort_index(), f"yfinance:{symbol}"
        except Exception as exc:
            raise last_error from exc
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote_data = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    close = quote_data.get("close")
    values = adj or close
    daily = {}
    for ts, val in zip(timestamps, values):
        if val is None:
            continue
        dt = datetime.fromtimestamp(ts, timezone.utc).date()
        daily[dt] = float(val)
    return pd.Series(daily, name=symbol).sort_index(), url


def monthly_last(series):
    s = series.copy()
    s.index = pd.to_datetime(s.index)
    return s.resample("ME").last().dropna()


def fetch_yahoo_series(symbols):
    series = {}
    sources = {}
    errors = {}
    for idx, sym in enumerate(symbols, 1):
        last_error = None
        for attempt in range(3):
            try:
                s, url = yahoo_chart(sym)
                if len(s) < 50:
                    errors[sym] = f"only {len(s)} daily observations"
                series[sym] = s
                sources[sym] = url
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        if last_error is not None:
            errors[sym] = repr(last_error)
        time.sleep(0.04)
        if idx % 50 == 0:
            print(f"fetched {idx}/{len(symbols)} Yahoo series")
    return series, sources, errors


def parse_shiller_cape_frame(df):
    date_col = None
    cape_col = None
    for c in df.columns:
        c_str = str(c).strip().lower()
        if c_str == "date":
            date_col = c
        if c_str == "cape" or c_str in {"p/e10 or cape", "caperatio"}:
            cape_col = c
    if date_col is None:
        date_col = df.columns[0]
    if cape_col is None:
        for c in df.columns:
            if "cape" in str(c).strip().lower():
                cape_col = c
                break
    if cape_col is None:
        raise ValueError("CAPE column not found")

    rows = []
    for _, row in df[[date_col, cape_col]].dropna().iterrows():
        val = row[date_col]
        try:
            year = int(float(val))
            month_float = (float(val) - year) * 100
            month = int(round(month_float))
            if month < 1 or month > 12:
                continue
            dt = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
            cape = float(row[cape_col])
            if cape > 0 and math.isfinite(cape):
                rows.append((dt, cape))
        except Exception:
            continue
    s = pd.Series(dict(rows), name="cape").sort_index()
    if len(s) < 100:
        raise ValueError(f"short CAPE series: {len(s)}")
    return s


def cache_shiller_cape_csv(cape_series):
    if LOCAL_CAPE_CSV_PATH.exists():
        return
    LOCAL_CAPE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    normalized = pd.DataFrame(
        {
            "Date": [f"{idx.year}.{idx.month:02d}" for idx in cape_series.index],
            "CAPE": cape_series.values,
        }
    )
    normalized.to_csv(LOCAL_CAPE_CSV_PATH, index=False)


def fetch_shiller_cape():
    local_errors = []
    if LOCAL_CAPE_CSV_PATH.exists():
        try:
            cape = parse_shiller_cape_frame(pd.read_csv(LOCAL_CAPE_CSV_PATH))
            return np.log(cape), str(LOCAL_CAPE_CSV_PATH), None
        except Exception as exc:
            local_errors.append(f"{LOCAL_CAPE_CSV_PATH}: {exc!r}")

    if LOCAL_CAPE_XLS_PATH.exists():
        try:
            df = pd.read_excel(LOCAL_CAPE_XLS_PATH, sheet_name="Data", header=7)
            cape = parse_shiller_cape_frame(df)
            cache_shiller_cape_csv(cape)
            return np.log(cape), str(LOCAL_CAPE_XLS_PATH), None
        except Exception as exc:
            local_errors.append(f"{LOCAL_CAPE_XLS_PATH}: {exc!r}")

    url = "https://www.econ.yale.edu/~shiller/data/ie_data.xls"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
        # Try Excel first. The file has descriptive header rows.
        df = pd.read_excel(io.BytesIO(raw), sheet_name="Data", header=7)
        cape = parse_shiller_cape_frame(df)
        return np.log(cape), url, None
    except Exception as exc:
        all_errors = local_errors + [f"{url}: {exc!r}"]
        return None, url, "; ".join(all_errors)


def build_state_variables(monthly_prices, yahoo_sources):
    vti = monthly_prices["VTI"].pct_change().dropna()
    irx_level = monthly_prices["^IRX"].copy()
    tnx_level = monthly_prices["^TNX"].copy()
    rf_monthly = (1 + irx_level / 100.0) ** (1 / 12) - 1
    market_excess = (vti - rf_monthly).dropna()
    term_spread = ((tnx_level - irx_level) / 100.0).dropna()

    cape_log, cape_url, cape_error = fetch_shiller_cape()
    if cape_log is None:
        # Fallback: use VTI log price deviation from 36-month moving average as valuation proxy.
        vti_log = np.log(monthly_prices["VTI"])
        cape_log = (vti_log - vti_log.rolling(36, min_periods=18).mean()).rename("valuation_proxy")
        cape_source = f"fallback VTI log price vs 36m trend; Shiller fetch error={cape_error}"
    else:
        cape_source = cape_url

    # Practical current proxy for the paper's small-stock value spread:
    # log cumulative relative wealth of Russell 2000 Value vs Growth ETFs.
    small_value_spread = np.log(monthly_prices["IWN"] / monthly_prices["IWO"]).rename("small_value_proxy")

    state = pd.concat(
        [
            market_excess.rename("market_excess"),
            term_spread.rename("term_spread"),
            cape_log.rename("cape_log"),
            small_value_spread.rename("small_value_proxy"),
        ],
        axis=1,
    ).dropna()
    return state, {
        "market_return": yahoo_sources.get("VTI"),
        "risk_free_proxy": yahoo_sources.get("^IRX"),
        "term_spread_10y": yahoo_sources.get("^TNX"),
        "cape": cape_source,
        "small_value_proxy": "Yahoo adjusted prices: IWN / IWO",
    }


def fit_var_news(state):
    # VAR(1): x_t = c + Gamma x_{t-1} + u_t
    y = state.iloc[1:].values
    x_lag = state.iloc[:-1].values
    x_design = np.column_stack([np.ones(len(x_lag)), x_lag])
    coef = np.linalg.lstsq(x_design, y, rcond=None)[0]  # rows: const + predictors, cols: equations
    gamma = coef[1:, :].T
    fitted = x_design @ coef
    resid = y - fitted
    resid_idx = state.index[1:]

    e1 = np.zeros((1, gamma.shape[0]))
    e1[0, 0] = 1.0
    inv = np.linalg.inv(np.eye(gamma.shape[0]) - RHO * gamma)
    dr_weights = e1 @ (RHO * gamma) @ inv
    n_dr = (resid @ dr_weights.T).flatten()
    market_innov = resid[:, 0]
    n_cf = market_innov + n_dr

    news = pd.DataFrame(
        {
            "market_innovation": market_innov,
            "discount_rate_news": n_dr,
            "cash_flow_news": n_cf,
        },
        index=resid_idx,
    )
    return gamma, news


def safe_cov(x, y):
    if len(x) < 2:
        return np.nan
    return float(np.cov(x, y, ddof=1)[0, 1])


def estimate_stock_betas(equities, monthly_returns, news, rf_monthly):
    rows = []
    for h in equities:
        ticker = h["ticker"]
        r = monthly_returns.get(ticker)
        if r is None or len(r.dropna()) < MIN_MONTHS:
            rows.append({**h, "status": "Insufficient return history"})
            continue
        aligned = pd.concat(
            [
                r.rename("stock_return"),
                rf_monthly.rename("rf"),
                news[["market_innovation", "cash_flow_news", "discount_rate_news"]],
            ],
            axis=1,
        ).dropna()
        aligned = aligned.tail(ROLLING_MONTHS)
        if len(aligned) < MIN_MONTHS:
            rows.append({**h, "status": f"Insufficient aligned history ({len(aligned)} months)"})
            continue
        stock_excess = aligned["stock_return"] - aligned["rf"]
        denom = float(np.var(aligned["market_innovation"], ddof=1))
        if denom <= 0 or math.isnan(denom):
            rows.append({**h, "status": "Invalid market innovation variance"})
            continue
        beta = safe_cov(stock_excess, aligned["market_innovation"]) / denom
        bad_beta = safe_cov(stock_excess, aligned["cash_flow_news"]) / denom
        dr_beta = safe_cov(stock_excess, aligned["discount_rate_news"]) / denom
        rows.append(
            {
                **h,
                "status": "OK",
                "observations": int(len(aligned)),
                "beta": beta,
                "bad_beta": bad_beta,
                "discount_rate_beta": dr_beta,
                "good_beta_proxy": beta - bad_beta,
                "start_month": aligned.index[0].strftime("%Y-%m-%d"),
                "end_month": aligned.index[-1].strftime("%Y-%m-%d"),
                "stock_ann_vol": float(aligned["stock_return"].std(ddof=1) * math.sqrt(12)),
            }
        )
    ok = [r for r in rows if r.get("status") == "OK"]
    beta_vals = pd.Series([r["beta"] for r in ok])
    bad_vals = pd.Series([r["bad_beta"] for r in ok])
    beta_q1, beta_q2 = beta_vals.quantile([1 / 3, 2 / 3]).tolist()
    bad_q1, bad_q2 = bad_vals.quantile([1 / 3, 2 / 3]).tolist()

    def tercile(v, q1, q2, label):
        if v <= q1:
            return f"Low {label}"
        if v <= q2:
            return f"Mid {label}"
        return f"High {label}"

    for r in rows:
        if r.get("status") != "OK":
            continue
        bt = tercile(r["beta"], beta_q1, beta_q2, "Beta")
        bbt = tercile(r["bad_beta"], bad_q1, bad_q2, "Bad Beta")
        r["beta_tercile"] = bt
        r["bad_beta_tercile"] = bbt
        r["grid_cell"] = f"{bt} / {bbt}"

    return rows, {
        "beta_q1": float(beta_q1),
        "beta_q2": float(beta_q2),
        "bad_beta_q1": float(bad_q1),
        "bad_beta_q2": float(bad_q2),
        "classified_count": len(ok),
    }


def summarize_grid(rows):
    ok = [r for r in rows if r.get("status") == "OK"]
    cell_summary = []
    for beta_t in ["Low Beta", "Mid Beta", "High Beta"]:
        for bad_t in ["Low Bad Beta", "Mid Bad Beta", "High Bad Beta"]:
            cell = [r for r in ok if r["beta_tercile"] == beta_t and r["bad_beta_tercile"] == bad_t]
            cell_summary.append(
                {
                    "beta_tercile": beta_t,
                    "bad_beta_tercile": bad_t,
                    "grid_cell": f"{beta_t} / {bad_t}",
                    "count": len(cell),
                    "avg_beta": float(np.mean([r["beta"] for r in cell])) if cell else None,
                    "avg_bad_beta": float(np.mean([r["bad_beta"] for r in cell])) if cell else None,
                }
            )
    return cell_summary


def main(argv=None):
    global UNIVERSE_PATH, OUT_PATH, LOCAL_CAPE_CSV_PATH, LOCAL_CAPE_XLS_PATH
    global START, END, RHO, ROLLING_MONTHS, MIN_MONTHS

    args = parse_args(argv)
    if args.window_months <= 0 or args.min_months <= 1:
        raise SystemExit("window-months must be positive and min-months must be greater than 1")
    if args.min_months > args.window_months:
        raise SystemExit("min-months cannot exceed window-months")
    if not 0 < args.rho < 1:
        raise SystemExit("rho must be between 0 and 1")
    UNIVERSE_PATH = Path(args.universe).expanduser().resolve()
    OUT_PATH = Path(args.output).expanduser().resolve()
    LOCAL_CAPE_CSV_PATH = Path(args.cape_csv).expanduser().resolve()
    LOCAL_CAPE_XLS_PATH = Path(args.cape_xls).expanduser().resolve()
    START = date.fromisoformat(args.start)
    END = date.fromisoformat(args.as_of)
    RHO = args.rho
    ROLLING_MONTHS = args.window_months
    MIN_MONTHS = args.min_months
    if END < START:
        raise SystemExit("as-of cannot be earlier than start")
    if not UNIVERSE_PATH.exists():
        raise SystemExit(f"universe path not found: {UNIVERSE_PATH}")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    universe_as_of, equities = read_universe(UNIVERSE_PATH, args.universe_as_of)
    symbols = sorted({e["ticker"] for e in equities} | {"VTI", "^IRX", "^TNX", "IWN", "IWO"})
    yahoo_series, yahoo_sources, yahoo_errors = fetch_yahoo_series(symbols)
    monthly_prices = {sym: monthly_last(s) for sym, s in yahoo_series.items() if len(s.dropna())}
    state, state_sources = build_state_variables(monthly_prices, yahoo_sources)
    gamma, news = fit_var_news(state)
    monthly_returns = {sym: s.pct_change().dropna() for sym, s in monthly_prices.items()}
    rf_monthly = ((monthly_prices["^IRX"] / 100.0 + 1) ** (1 / 12) - 1).rename("rf")
    rows, breakpoints = estimate_stock_betas(equities, monthly_returns, news, rf_monthly)
    cell_summary = summarize_grid(rows)

    OUT_PATH.write_text(
        json.dumps(
            {
                "universe_as_of": universe_as_of,
                "universe_source": str(UNIVERSE_PATH),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "method": {
                    "state_variables": [
                        "VTI monthly excess return",
                        "10Y minus 13W yield spread from Yahoo ^TNX and ^IRX",
                        "Shiller CAPE log where available, otherwise VTI valuation proxy",
                        "small value proxy: log(IWN/IWO)",
                    ],
                    "var": "VAR(1), full sample through latest available month",
                    "rho": RHO,
                    "stock_beta_window_months": ROLLING_MONTHS,
                    "min_months": MIN_MONTHS,
                    "bad_beta_formula": "cov(stock excess return, VAR cash-flow news) / var(market return innovation)",
                    "classification": "Universe-relative beta and bad-beta terciles forming a 3x3 exposure grid",
                },
                "date_range": {
                    "start": str(START),
                    "end": str(END),
                    "state_start": state.index.min().strftime("%Y-%m-%d"),
                    "state_end": state.index.max().strftime("%Y-%m-%d"),
                    "news_start": news.index.min().strftime("%Y-%m-%d"),
                    "news_end": news.index.max().strftime("%Y-%m-%d"),
                },
                "state_sources": state_sources,
                "yahoo_errors": yahoo_errors,
                "breakpoints": breakpoints,
                "universe": rows,
                "cell_summary": cell_summary,
                "var_gamma": gamma.tolist(),
                "news_summary": {
                    "market_innovation_vol_ann": float(news["market_innovation"].std(ddof=1) * math.sqrt(12)),
                    "cash_flow_news_vol_ann": float(news["cash_flow_news"].std(ddof=1) * math.sqrt(12)),
                    "discount_rate_news_vol_ann": float(news["discount_rate_news"].std(ddof=1) * math.sqrt(12)),
                },
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "output": str(OUT_PATH),
                "equities": len(equities),
                "classified": breakpoints["classified_count"],
                "state_start": state.index.min().strftime("%Y-%m-%d"),
                "state_end": state.index.max().strftime("%Y-%m-%d"),
                "yahoo_error_count": len(yahoo_errors),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
