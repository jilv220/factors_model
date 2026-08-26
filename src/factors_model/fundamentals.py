"""Collect the established five fundamental factors for an arbitrary ticker universe.

The methodology is the same cohort-relative model used by the frozen five-factor
snapshot: SPHQ-style quality, SUE/CAR3 fundamental momentum, valuation,
conservative investment, and shareholder yield.  SEC filings provide reported
fundamentals; Yahoo provides adjusted prices; Nasdaq is used as the preferred
market-cap and sector source when available.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = "factors-model public-equity research research-contact@example.com"
SEC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
AS_OF = date.today()
PRICE_START = AS_OF - timedelta(days=920)


REVENUE = [
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "Revenues"),
    ("us-gaap", "SalesRevenueNet"),
    ("ifrs-full", "Revenue"),
    ("ifrs-full", "RevenueFromContractsWithCustomers"),
]
NET_INCOME = [
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "ProfitLoss"),
    ("ifrs-full", "ProfitLoss"),
]
CFO = [
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
    ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ("ifrs-full", "NetCashFlowsFromUsedInOperatingActivities"),
]
CAPEX = [
    ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("us-gaap", "PaymentsToAcquireProductiveAssets"),
    ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment"),
    ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
]
EPS = [
    ("us-gaap", "EarningsPerShareBasic"),
    ("us-gaap", "EarningsPerShareBasicAndDiluted"),
    ("ifrs-full", "BasicEarningsLossPerShare"),
]
ASSETS = [("us-gaap", "Assets"), ("ifrs-full", "Assets")]
LIABILITIES = [("us-gaap", "Liabilities"), ("ifrs-full", "Liabilities")]
EQUITY = [
    ("us-gaap", "StockholdersEquity"),
    ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ("ifrs-full", "Equity"),
]
CASH = [
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
    ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ("us-gaap", "CashAndCashEquivalentsAndShortTermInvestments"),
    ("ifrs-full", "CashAndCashEquivalents"),
]
DEBT_TOTAL = [
    ("us-gaap", "DebtAndFinanceLeaseObligations"),
    ("us-gaap", "LongTermDebtAndFinanceLeaseObligations"),
    ("us-gaap", "LongTermDebt"),
    ("ifrs-full", "Borrowings"),
]
DEBT_CURRENT = [
    ("us-gaap", "ShortTermBorrowings"),
    ("us-gaap", "ShortTermDebt"),
    ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsCurrent"),
    ("us-gaap", "LongTermDebtCurrent"),
    ("ifrs-full", "CurrentBorrowings"),
]
DEBT_NONCURRENT = [
    ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"),
    ("us-gaap", "LongTermDebtNoncurrent"),
    ("ifrs-full", "NoncurrentBorrowings"),
]
SHARES = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
]
WEIGHTED_SHARES = [
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
    ("ifrs-full", "WeightedAverageNumberOfSharesOutstanding"),
]
DIVIDENDS = [
    ("us-gaap", "PaymentsOfDividendsCommonStock"),
    ("us-gaap", "PaymentsOfDividends"),
    ("us-gaap", "PaymentsOfOrdinaryDividends"),
    ("ifrs-full", "DividendsPaid"),
]
REPURCHASES = [
    ("us-gaap", "PaymentsForRepurchaseOfCommonStock"),
    ("us-gaap", "PaymentsForRepurchaseOfEquity"),
]
ISSUANCE = [
    ("us-gaap", "ProceedsFromStockOptionsExercised"),
    ("us-gaap", "ProceedsFromIssuanceOfCommonStock"),
    ("us-gaap", "ProceedsFromIssuanceOrSaleOfEquity"),
]


def json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-").replace("/", "-")


def _ticker_from_record(record: dict[str, Any]) -> str:
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    return str(lowered.get("ticker") or lowered.get("symbol") or "").upper().strip()


def _normalize_universe_record(record: dict[str, Any], source: str) -> dict[str, Any] | None:
    ticker = _ticker_from_record(record)
    if not ticker:
        return None
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    company = (
        lowered.get("company")
        or lowered.get("issue_name")
        or lowered.get("full_name")
        or lowered.get("name")
    )
    sector = lowered.get("sector") or lowered.get("nasdaq_sector")
    return {
        "ticker": ticker,
        "issue_name": str(company).strip() if company else None,
        "sector": str(sector).strip() if sector else None,
        "universe_source_file": source,
    }


def _csv_records(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"universe CSV has no header: {path}")
        return [dict(record) for record in reader]


def load_universe(path: Path) -> list[dict[str, Any]]:
    """Load unique tickers from one CSV/JSON/text file or a directory of CSVs."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"universe input not found: {path}")
    raw_records: list[tuple[dict[str, Any], str]] = []
    if path.is_dir():
        files = [candidate for candidate in sorted(path.glob("*.csv*")) if candidate.is_file()]
        if not files:
            raise ValueError(f"universe directory contains no CSV files: {path}")
        for candidate in files:
            raw_records.extend((record, candidate.name) for record in _csv_records(candidate))
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = payload.get("rows") or payload.get("universe")
        else:
            rows = payload
        if not isinstance(rows, list):
            raise ValueError(f"universe JSON must contain a rows or universe array: {path}")
        raw_records.extend((record, path.name) for record in rows if isinstance(record, dict))
    elif ".csv" in path.name.lower():
        raw_records.extend((record, path.name) for record in _csv_records(path))
    else:
        raw_records.extend(({"ticker": line.strip()}, path.name) for line in path.read_text().splitlines())

    rows: dict[str, dict[str, Any]] = {}
    for record, source in raw_records:
        normalized = _normalize_universe_record(record, source)
        if normalized is None:
            continue
        ticker = normalized["ticker"]
        if ticker not in rows:
            rows[ticker] = normalized
        else:
            if not rows[ticker].get("issue_name") and normalized.get("issue_name"):
                rows[ticker]["issue_name"] = normalized["issue_name"]
            if not rows[ticker].get("sector") and normalized.get("sector"):
                rows[ticker]["sector"] = normalized["sector"]
    if not rows:
        raise ValueError(f"universe contains no tickers: {path}")
    return [rows[ticker] for ticker in sorted(rows)]


def fetch_json(url: str, user_agent: str = DEFAULT_USER_AGENT, retries: int = 3) -> Any:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {retries} attempts: {url}: {last!r}")


def nasdaq_market_snapshot() -> tuple[dict[str, dict[str, Any]], str]:
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25000&download=true"
    payload = fetch_json(url, user_agent="Mozilla/5.0 factors-model research")
    rows = payload.get("data", {}).get("rows", []) or []
    return {str(row.get("symbol", "")).upper(): row for row in rows}, url


def iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def days_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def facts_for(
    payload: dict[str, Any],
    concepts: list[tuple[str, str]],
    units: tuple[str, ...],
    forms: set[str] | None = SEC_FORMS,
) -> list[dict[str, Any]]:
    chosen: dict[tuple[Any, ...], dict[str, Any]] = {}
    facts = payload.get("facts", {})
    for priority, (taxonomy, concept) in enumerate(concepts):
        node = facts.get(taxonomy, {}).get(concept, {})
        unit_map = node.get("units", {})
        raw: list[dict[str, Any]] = []
        for unit in units:
            if unit in unit_map:
                raw = unit_map[unit]
                break
        for item in raw:
            value = finite(item.get("val"))
            filed = iso(item.get("filed"))
            end = iso(item.get("end"))
            start = iso(item.get("start"))
            form = item.get("form")
            if value is None or not filed or not end or filed > AS_OF.isoformat():
                continue
            if forms is not None and form not in forms:
                continue
            record = {
                "value": value,
                "start": start,
                "end": end,
                "filed": filed,
                "form": form,
                "fy": item.get("fy"),
                "fp": item.get("fp"),
                "frame": item.get("frame"),
                "taxonomy": taxonomy,
                "concept": concept,
                "priority": priority,
                "days": days_between(start, end),
            }
            key = (start, end, item.get("fy"), item.get("fp"), form)
            old = chosen.get(key)
            if old is None or (priority, filed) < (old["priority"], old["filed"]):
                chosen[key] = record
            elif priority == old["priority"] and filed > old["filed"]:
                chosen[key] = record
    return list(chosen.values())


def dedupe_by_end(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for item in items:
        old = chosen.get(item["end"])
        if old is None or (item["filed"], -item["priority"]) > (old["filed"], -old["priority"]):
            chosen[item["end"]] = item
    return sorted(chosen.values(), key=lambda item: item["end"])


def instant_series(
    payload: dict[str, Any],
    concepts: list[tuple[str, str]],
    units: tuple[str, ...] = ("USD",),
    annual_only: bool = False,
) -> list[dict[str, Any]]:
    forms = ANNUAL_FORMS if annual_only else SEC_FORMS
    return dedupe_by_end(
        item for item in facts_for(payload, concepts, units, forms) if item["start"] is None
    )


def duration_items(
    payload: dict[str, Any],
    concepts: list[tuple[str, str]],
    units: tuple[str, ...] = ("USD",),
) -> list[dict[str, Any]]:
    return [
        item
        for item in facts_for(payload, concepts, units, SEC_FORMS)
        if item["start"] is not None and item["days"] is not None
    ]


def choose_latest(items: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    sequence = list(items)
    return max(sequence, key=lambda item: (item["end"], item["filed"], -item["priority"])) if sequence else None


def annual_duration_series(
    payload: dict[str, Any], concepts: list[tuple[str, str]], units: tuple[str, ...] = ("USD",)
) -> list[dict[str, Any]]:
    return dedupe_by_end(
        item
        for item in duration_items(payload, concepts, units)
        if item["form"] in ANNUAL_FORMS and 300 <= item["days"] <= 460
    )


def quarter_series(
    payload: dict[str, Any], concepts: list[tuple[str, str]], units: tuple[str, ...] = ("USD",)
) -> list[dict[str, Any]]:
    items = duration_items(payload, concepts, units)
    by_fy: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        if isinstance(item.get("fy"), int):
            by_fy.setdefault(item["fy"], []).append(item)
    output: list[dict[str, Any]] = []
    for group in by_fy.values():
        def pick(fp: str, low: int, high: int) -> dict[str, Any] | None:
            return choose_latest(item for item in group if item.get("fp") == fp and low <= item["days"] <= high)

        q1, q2, q3, q4 = (pick("Q1", 50, 140), pick("Q2", 50, 140), pick("Q3", 50, 140), pick("Q4", 50, 140))
        y2, y3, year = pick("Q2", 140, 235), pick("Q3", 220, 335), pick("FY", 300, 460)
        if q1:
            output.append({**q1, "quarter": "Q1", "derived": False})
        if q2:
            output.append({**q2, "quarter": "Q2", "derived": False})
        elif y2 and q1:
            output.append({**y2, "value": y2["value"] - q1["value"], "quarter": "Q2", "derived": True})
        if q3:
            output.append({**q3, "quarter": "Q3", "derived": False})
        elif y3:
            base = y2["value"] if y2 else (q1["value"] + (q2["value"] if q2 else 0.0) if q1 else None)
            if base is not None:
                output.append({**y3, "value": y3["value"] - base, "quarter": "Q3", "derived": True})
        if q4:
            output.append({**q4, "quarter": "Q4", "derived": False})
        elif year:
            base = y3["value"] if y3 else (q1["value"] + q2["value"] + q3["value"] if q1 and q2 and q3 else None)
            if base is not None:
                output.append({**year, "value": year["value"] - base, "quarter": "Q4", "derived": True})
    return dedupe_by_end(output)


def ttm_value(quarters: list[dict[str, Any]]) -> float | None:
    if len(quarters) < 4:
        return None
    latest = quarters[-4:]
    span = (date.fromisoformat(latest[-1]["end"]) - date.fromisoformat(latest[0]["end"])).days
    return sum(item["value"] for item in latest) if 240 <= span <= 430 else None


def latest_and_prior(
    series: list[dict[str, Any]], gap_low: int = 270, gap_high: int = 470
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not series:
        return None, None
    latest = series[-1]
    latest_date = date.fromisoformat(latest["end"])
    candidates = []
    for item in series[:-1]:
        gap = (latest_date - date.fromisoformat(item["end"])).days
        if gap_low <= gap <= gap_high:
            candidates.append((abs(gap - 365), item))
    prior = min(candidates, key=lambda pair: pair[0])[1] if candidates else None
    return latest, prior


def value_on_date(
    series: list[dict[str, Any]], target: str | None, tolerance: int = 15
) -> dict[str, Any] | None:
    if not target:
        return None
    target_date = date.fromisoformat(target)
    candidates = []
    for item in series:
        gap = abs((date.fromisoformat(item["end"]) - target_date).days)
        if gap <= tolerance:
            candidates.append((gap, -int(item["filed"].replace("-", "")), item))
    return min(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2] if candidates else None


def combine_debt(payload: dict[str, Any], annual_only: bool) -> list[dict[str, Any]]:
    total = {item["end"]: item for item in instant_series(payload, DEBT_TOTAL, annual_only=annual_only)}
    current = {item["end"]: item for item in instant_series(payload, DEBT_CURRENT, annual_only=annual_only)}
    noncurrent = {item["end"]: item for item in instant_series(payload, DEBT_NONCURRENT, annual_only=annual_only)}
    output: list[dict[str, Any]] = []
    for end in sorted(set(total) | set(current) | set(noncurrent)):
        if end in total:
            output.append(total[end])
            continue
        parts = [item for item in (current.get(end), noncurrent.get(end)) if item is not None]
        if parts:
            base = max(parts, key=lambda item: item["filed"])
            output.append({**base, "value": sum(item["value"] for item in parts), "concept": "+".join(item["concept"] for item in parts)})
    return dedupe_by_end(output)


def latest_annual_value(
    payload: dict[str, Any], concepts: list[tuple[str, str]], units: tuple[str, ...] = ("USD",)
) -> dict[str, Any] | None:
    series = annual_duration_series(payload, concepts, units)
    return series[-1] if series else None


def sue_from_eps(quarters: list[dict[str, Any]]) -> dict[str, Any]:
    values = [item["value"] for item in quarters]
    innovations = [values[index] - values[index - 4] for index in range(4, len(values))]
    recent = innovations[-8:]
    if len(recent) < 6:
        return {"sue": None, "ue": innovations[-1] if innovations else None, "innovation_count": len(recent)}
    deviation = statistics.stdev(recent) if len(recent) > 1 else 0.0
    return {
        "sue": recent[-1] / deviation if deviation > 1e-12 else None,
        "ue": recent[-1],
        "innovation_count": len(recent),
    }


def submission_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    recent = payload.get("filings", {}).get("recent", {})
    count = len(recent.get("form", [])) if recent else 0
    rows = []
    for index in range(count):
        filed = iso(recent.get("filingDate", [None] * count)[index])
        if not filed or filed > AS_OF.isoformat():
            continue
        rows.append(
            {
                "form": recent.get("form", [None] * count)[index],
                "filed": filed,
                "items": recent.get("items", [""] * count)[index] or "",
            }
        )
    return rows


def earnings_date(submissions: dict[str, Any], eps_quarters: list[dict[str, Any]]) -> tuple[str | None, str]:
    rows = submission_rows(submissions)
    item_202 = [row for row in rows if row["form"] == "8-K" and "2.02" in row["items"]]
    if item_202:
        return max(item_202, key=lambda row: row["filed"])["filed"], "8-K Item 2.02 filing date"
    sixk = [row for row in rows if row["form"] == "6-K"]
    if sixk:
        recent = max(sixk, key=lambda row: row["filed"])
        if (AS_OF - date.fromisoformat(recent["filed"])).days <= 180:
            return recent["filed"], "latest recent 6-K filing date fallback"
    if eps_quarters:
        return eps_quarters[-1]["filed"], "latest EPS fact filing date fallback"
    return None, "unavailable"


def sec_metrics(ticker: str, cik: int) -> dict[str, Any]:
    companyfacts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    company = fetch_json(companyfacts_url)
    submissions = fetch_json(submissions_url)
    revenue_q = quarter_series(company, REVENUE)
    income_q = quarter_series(company, NET_INCOME)
    cfo_q = quarter_series(company, CFO)
    capex_q = quarter_series(company, CAPEX)
    eps_q = quarter_series(company, EPS, ("USD/shares", "USD / shares"))

    def ttm_or_annual(quarters: list[dict[str, Any]], concepts: list[tuple[str, str]]) -> float | None:
        value = ttm_value(quarters)
        annual = latest_annual_value(company, concepts)
        return value if value is not None else annual["value"] if annual else None

    revenue_ttm = ttm_or_annual(revenue_q, REVENUE)
    net_income_ttm = ttm_or_annual(income_q, NET_INCOME)
    cfo_ttm = ttm_or_annual(cfo_q, CFO)
    capex_ttm = ttm_or_annual(capex_q, CAPEX)
    fcf_ttm = cfo_ttm - capex_ttm if cfo_ttm is not None and capex_ttm is not None else None

    assets_a = instant_series(company, ASSETS, annual_only=True)
    liabilities_a = instant_series(company, LIABILITIES, annual_only=True)
    equity_a = instant_series(company, EQUITY, annual_only=True)
    cash_a = instant_series(company, CASH, annual_only=True)
    debt_a = combine_debt(company, annual_only=True)
    assets_now, assets_prior = latest_and_prior(assets_a)
    annual_end = assets_now["end"] if assets_now else None
    prior_end = assets_prior["end"] if assets_prior else None

    def aligned(series: list[dict[str, Any]], end: str | None) -> float | None:
        item = value_on_date(series, end)
        return item["value"] if item else None

    assets = assets_now["value"] if assets_now else None
    assets_p = assets_prior["value"] if assets_prior else None
    liabilities = aligned(liabilities_a, annual_end)
    liabilities_p = aligned(liabilities_a, prior_end)
    equity = aligned(equity_a, annual_end)
    cash = aligned(cash_a, annual_end)
    cash_p = aligned(cash_a, prior_end)
    debt = aligned(debt_a, annual_end)
    debt_p = aligned(debt_a, prior_end)
    noa = assets + (debt or 0.0) - (cash or 0.0) - liabilities if assets is not None and liabilities is not None else None
    noa_p = assets_p + (debt_p or 0.0) - (cash_p or 0.0) - liabilities_p if assets_p is not None and liabilities_p is not None else None
    average_assets = (assets + assets_p) / 2 if assets is not None and assets_p is not None else None

    weighted = annual_duration_series(company, WEIGHTED_SHARES, ("shares",))
    weighted_latest, weighted_prior = latest_and_prior(weighted)
    share_change = (
        weighted_latest["value"] / weighted_prior["value"] - 1
        if weighted_latest and weighted_prior and weighted_prior["value"] != 0
        else None
    )
    shares = instant_series(company, SHARES, ("shares",), annual_only=False)
    dividends = latest_annual_value(company, DIVIDENDS)
    repurchases = latest_annual_value(company, REPURCHASES)
    issuance = latest_annual_value(company, ISSUANCE)
    sue = sue_from_eps(eps_q)
    event_date, event_source = earnings_date(submissions, eps_q)
    try:
        sic = int(submissions.get("sic")) if submissions.get("sic") is not None else None
    except (TypeError, ValueError):
        sic = None
    exclude_bsa = bool(sic is not None and 6000 <= sic <= 6799)
    return {
        "ticker": ticker,
        "cik": cik,
        "entity_name": company.get("entityName") or submissions.get("name"),
        "sic": sic,
        "sic_description": submissions.get("sicDescription"),
        "exclude_bsa": exclude_bsa,
        "companyfacts_url": companyfacts_url,
        "submissions_url": submissions_url,
        "latest_quarter_end": eps_q[-1]["end"] if eps_q else revenue_q[-1]["end"] if revenue_q else None,
        "latest_annual_end": annual_end,
        "revenue_ttm": revenue_ttm,
        "net_income_ttm": net_income_ttm,
        "cfo_ttm": cfo_ttm,
        "capex_ttm": capex_ttm,
        "fcf_ttm": fcf_ttm,
        "assets": assets,
        "assets_prior": assets_p,
        "liabilities": liabilities,
        "equity": equity,
        "cash": cash,
        "debt": debt,
        "debt_prior": debt_p,
        "roe": ratio(net_income_ttm, equity),
        "balance_sheet_accruals": None if exclude_bsa else ratio(noa - noa_p, average_assets) if noa is not None and noa_p is not None else None,
        "financial_leverage": ratio(debt or 0.0, equity),
        "asset_growth": assets / assets_p - 1 if assets is not None and assets_p not in (None, 0) else None,
        "capex_to_avg_assets": ratio(capex_ttm, average_assets),
        "sue": sue["sue"],
        "earnings_innovation": sue["ue"],
        "sue_innovation_count": sue["innovation_count"],
        "eps_quarters": len(eps_q),
        "earnings_announcement_date": event_date,
        "earnings_date_source": event_source,
        "shares_outstanding": shares[-1]["value"] if shares else None,
        "weighted_share_change": share_change,
        "repurchase_yield_proxy": -share_change if share_change is not None else None,
        "annual_dividends": dividends["value"] if dividends else 0.0,
        "annual_repurchase_cash": repurchases["value"] if repurchases else 0.0,
        "annual_issuance_cash": issuance["value"] if issuance else 0.0,
        "repurchase_cash_available": repurchases is not None,
        "issuance_cash_available": issuance is not None,
    }


def yahoo_series(symbol: str) -> dict[str, Any]:
    period1 = int(datetime.combine(PRICE_START, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.combine(AS_OF + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?period1={period1}&period2={period2}&interval=1d&events=div%2Csplits"
    payload = fetch_json(url, user_agent="Mozilla/5.0 factors-model research")
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    quote_node = result.get("indicators", {}).get("quote", [{}])[0]
    adjusted = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
    closes = adjusted or quote_node.get("close", [])
    prices: dict[str, float] = {}
    for timestamp, price in zip(timestamps, closes):
        value = finite(price)
        if value is not None:
            prices[datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()] = value
    return {"symbol": symbol, "url": url, "prices": prices}


def daily_returns(prices: dict[str, float]) -> dict[str, float]:
    dates = sorted(prices)
    return {
        dates[index]: prices[dates[index]] / prices[dates[index - 1]] - 1
        for index in range(1, len(dates))
        if prices[dates[index - 1]] != 0
    }


def car3(
    stock_prices: dict[str, float], market_prices: dict[str, float], event_date: str | None
) -> dict[str, Any]:
    if not event_date:
        return {"car3": None, "mapped_event_date": None, "window": []}
    stock_returns, market_returns = daily_returns(stock_prices), daily_returns(market_prices)
    common = sorted(set(stock_returns) & set(market_returns))
    eligible = [value for value in common if value >= event_date]
    if not eligible:
        return {"car3": None, "mapped_event_date": None, "window": []}
    mapped = eligible[0]
    index = common.index(mapped)
    if index < 1 or index + 1 >= len(common):
        return {"car3": None, "mapped_event_date": mapped, "window": []}
    window = common[index - 1 : index + 2]
    return {
        "car3": sum(stock_returns[value] - market_returns[value] for value in window),
        "mapped_event_date": mapped,
        "window": window,
    }


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def winsor_z(
    rows: list[dict[str, Any]], key: str, higher: bool, invalid_key: str | None = None
) -> dict[str, float | None]:
    valid = [
        value
        for row in rows
        if (value := finite(row.get(key))) is not None and not (bool(row.get(invalid_key)) if invalid_key else False)
    ]
    if not valid:
        return {row["ticker"]: None for row in rows}
    low, high = quantile(valid, 0.025), quantile(valid, 0.975)
    clipped = [min(max(value, low), high) for value in valid]
    mean, deviation = statistics.mean(clipped), statistics.pstdev(clipped)
    deviation = deviation if deviation > 1e-12 else 1.0
    direction = 1 if higher else -1
    valid_z = [((value - mean) / deviation) * direction for value in clipped]
    worst = min(valid_z) if valid_z else -4.0
    output: dict[str, float | None] = {}
    for row in rows:
        value = finite(row.get(key))
        invalid = bool(row.get(invalid_key)) if invalid_key else False
        if invalid:
            output[row["ticker"]] = worst
        elif value is None:
            output[row["ticker"]] = None
        else:
            output[row["ticker"]] = ((min(max(value, low), high) - mean) / deviation) * direction
    return output


def percentile_map(
    rows: list[dict[str, Any]], key: str, higher: bool = True
) -> dict[str, float | None]:
    pairs = [(row["ticker"], finite(row.get(key))) for row in rows]
    actual = sorted(value for _, value in pairs if value is not None)
    output: dict[str, float | None] = {}
    for ticker, value in pairs:
        if value is None or not actual:
            output[ticker] = None
        elif len(actual) == 1:
            output[ticker] = 0.5
        else:
            below = sum(candidate < value for candidate in actual)
            equal = sum(candidate == value for candidate in actual)
            percentile = (below + 0.5 * (equal - 1)) / (len(actual) - 1)
            output[ticker] = percentile if higher else 1.0 - percentile
    return output


def avg_present(values: Iterable[float | None]) -> float | None:
    valid = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.mean(valid) if valid else None


def score10(percentile: float | None) -> float | None:
    return 1.0 + 9.0 * percentile if percentile is not None else None


def add_ranks(rows: list[dict[str, Any]], key: str, rank_key: str) -> None:
    ordered = sorted(
        (row for row in rows if finite(row.get(key)) is not None),
        key=lambda row: (-row[key], row["ticker"]),
    )
    last_value: float | None = None
    last_rank = 0
    for index, row in enumerate(ordered, 1):
        if last_value is None or abs(row[key] - last_value) > 1e-12:
            last_value, last_rank = row[key], index
        row[rank_key] = last_rank
    for row in rows:
        row.setdefault(rank_key, None)


def score_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["roe_invalid"] = row.get("equity") is not None and row.get("net_income_ttm") is not None and (row["equity"] <= 0 or row["net_income_ttm"] < 0)
        row["leverage_invalid"] = row.get("equity") is not None and row["equity"] <= 0
    roe_z = winsor_z(rows, "roe", True, "roe_invalid")
    accruals_z = winsor_z(rows, "balance_sheet_accruals", False)
    leverage_z = winsor_z(rows, "financial_leverage", False, "leverage_invalid")
    for row in rows:
        ticker = row["ticker"]
        row["roe_z"] = roe_z[ticker]
        row["accruals_z"] = None if row.get("exclude_bsa") else accruals_z[ticker]
        row["leverage_z"] = leverage_z[ticker]
        row["quality_average_z"] = avg_present([row["roe_z"], row["accruals_z"], row["leverage_z"]])
    quality = percentile_map(rows, "quality_average_z")
    sue = percentile_map(rows, "sue")
    event = percentile_map(rows, "car3")
    for row in rows:
        ticker = row["ticker"]
        row["sue_percentile"] = sue[ticker]
        row["car3_percentile"] = event[ticker]
        row["fundamental_momentum_percentile"] = avg_present([sue[ticker], event[ticker]])
        row["fundamental_momentum_method"] = (
            "SUE + CAR3" if sue[ticker] is not None and event[ticker] is not None
            else "SUE only" if sue[ticker] is not None
            else "CAR3 only" if event[ticker] is not None
            else "Insufficient"
        )
    valuation_keys = ["earnings_yield", "fcf_yield", "book_to_price", "sales_to_ev"]
    valuation = {key: percentile_map(rows, key) for key in valuation_keys}
    asset_growth = percentile_map(rows, "asset_growth", False)
    capex_intensity = percentile_map(rows, "capex_to_avg_assets", False)
    shareholder_yield = percentile_map(rows, "shareholder_yield")
    for row in rows:
        ticker = row["ticker"]
        row["quality_score"] = score10(quality[ticker])
        row["fundamental_momentum_score"] = score10(row["fundamental_momentum_percentile"])
        row["valuation_component_percentiles"] = {
            key: None if row.get("exclude_bsa") and key in {"fcf_yield", "sales_to_ev"} else valuation[key][ticker]
            for key in valuation_keys
        }
        row["valuation_percentile"] = avg_present(row["valuation_component_percentiles"].values())
        row["valuation_score"] = score10(row["valuation_percentile"])
        row["asset_growth_percentile"] = asset_growth[ticker]
        row["capex_intensity_percentile"] = capex_intensity[ticker]
        row["conservative_investment_percentile"] = avg_present([
            asset_growth[ticker],
            None if row.get("exclude_bsa") else capex_intensity[ticker],
        ])
        row["conservative_investment_score"] = score10(row["conservative_investment_percentile"])
        row["shareholder_yield_percentile"] = shareholder_yield[ticker]
        row["shareholder_yield_score"] = score10(shareholder_yield[ticker])
        scores = [
            row["quality_score"],
            row["fundamental_momentum_score"],
            row["valuation_score"],
            row["conservative_investment_score"],
            row["shareholder_yield_score"],
        ]
        row["factor_coverage"] = sum(score is not None for score in scores)
        row["overall_score"] = avg_present(scores)
    for key, rank_key in [
        ("quality_score", "quality_rank"),
        ("fundamental_momentum_score", "fundamental_momentum_rank"),
        ("valuation_score", "valuation_rank"),
        ("conservative_investment_score", "conservative_investment_rank"),
        ("shareholder_yield_score", "shareholder_yield_rank"),
        ("overall_score", "overall_rank"),
    ]:
        add_ranks(rows, key, rank_key)


def collect_five_factors(
    universe_path: Path,
    as_of: date,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    max_workers: int = 4,
) -> dict[str, Any]:
    global AS_OF, PRICE_START
    AS_OF = as_of
    PRICE_START = as_of - timedelta(days=920)
    universe = load_universe(universe_path)
    ticker_map_payload = fetch_json("https://www.sec.gov/files/company_tickers.json", user_agent)
    ticker_map = {str(item["ticker"]).upper(): int(item["cik_str"]) for item in ticker_map_payload.values()}
    errors: dict[str, str] = {}
    try:
        nasdaq, nasdaq_url = nasdaq_market_snapshot()
    except Exception as exc:
        nasdaq, nasdaq_url = {}, "unavailable"
        errors["NASDAQ"] = repr(exc)

    sec_by_ticker: dict[str, dict[str, Any]] = {}

    def get_sec(row: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        ticker = row["ticker"]
        cik = ticker_map.get(ticker)
        if cik is None:
            return ticker, None, "No SEC ticker-map match"
        try:
            return ticker, sec_metrics(ticker, cik), None
        except Exception as exc:
            return ticker, None, repr(exc)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(get_sec, row) for row in universe]
        for index, future in enumerate(as_completed(futures), 1):
            ticker, data, error = future.result()
            if error:
                errors[f"SEC:{ticker}"] = error
            elif data:
                sec_by_ticker[ticker] = data
            if index % 20 == 0 or index == len(futures):
                print(f"SEC {index}/{len(futures)}", flush=True)

    symbols = [yahoo_symbol(row["ticker"]) for row in universe] + ["VTI"]
    prices: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(2, max_workers * 2)) as pool:
        futures = {pool.submit(yahoo_series, symbol): symbol for symbol in symbols}
        for index, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                prices[symbol] = future.result()
            except Exception as exc:
                errors[f"YAHOO:{symbol}"] = repr(exc)
            if index % 25 == 0 or index == len(futures):
                print(f"Yahoo {index}/{len(futures)}", flush=True)

    market_prices = prices.get("VTI", {}).get("prices", {})
    rows: list[dict[str, Any]] = []
    for base in universe:
        ticker = base["ticker"]
        symbol = yahoo_symbol(ticker)
        sec = sec_by_ticker.get(ticker, {})
        nasdaq_row = nasdaq.get(ticker) or nasdaq.get(symbol) or nasdaq.get(ticker.replace("-", "/")) or {}
        price_payload = prices.get(symbol, {})
        price_map = price_payload.get("prices", {})
        price_date = max(price_map) if price_map else None
        price = price_map.get(price_date) if price_date else None
        event = car3(price_map, market_prices, sec.get("earnings_announcement_date")) if price_map and market_prices else {"car3": None, "mapped_event_date": None, "window": []}
        calculated_market_cap = price * sec["shares_outstanding"] if price is not None and sec.get("shares_outstanding") is not None else None
        nasdaq_market_cap = finite(nasdaq_row.get("marketCap"))
        market_cap = nasdaq_market_cap or calculated_market_cap
        market_cap_source = "Nasdaq screener" if nasdaq_market_cap is not None else "Yahoo price x SEC shares" if calculated_market_cap is not None else None
        debt, cash = sec.get("debt"), sec.get("cash")
        enterprise_value = market_cap + (debt or 0.0) - (cash or 0.0) if market_cap is not None else None
        dividend_yield = ratio(sec.get("annual_dividends"), market_cap)
        debt_paydown_yield = ratio((sec.get("debt_prior") or 0.0) - (debt or 0.0), market_cap) if market_cap is not None and sec.get("debt_prior") is not None and not sec.get("exclude_bsa") else None
        cash_buyback_available = bool(sec.get("repurchase_cash_available") or sec.get("issuance_cash_available"))
        cash_buyback_yield = ratio((sec.get("annual_repurchase_cash") or 0.0) - (sec.get("annual_issuance_cash") or 0.0), market_cap) if cash_buyback_available else None
        buyback_yield = cash_buyback_yield if cash_buyback_yield is not None else sec.get("repurchase_yield_proxy")
        shareholder_parts = [value for value in (dividend_yield, buyback_yield, debt_paydown_yield) if value is not None]
        row = {
            **base,
            **sec,
            "ticker": ticker,
            "issue_name": base.get("issue_name") or sec.get("entity_name"),
            "price_symbol": symbol,
            "price_date": price_date,
            "price": price,
            "price_source": price_payload.get("url"),
            "car3": event.get("car3"),
            "mapped_event_date": event.get("mapped_event_date"),
            "car3_window": event.get("window"),
            "market_cap": market_cap,
            "market_cap_source": market_cap_source,
            "nasdaq_source": nasdaq_url,
            "nasdaq_sector": nasdaq_row.get("sector") or base.get("sector"),
            "nasdaq_industry": nasdaq_row.get("industry"),
            "enterprise_value": enterprise_value,
            "earnings_yield": ratio(sec.get("net_income_ttm"), market_cap),
            "fcf_yield": ratio(sec.get("fcf_ttm"), market_cap),
            "book_to_price": ratio(sec.get("equity"), market_cap),
            "sales_to_ev": ratio(sec.get("revenue_ttm"), enterprise_value) if enterprise_value and enterprise_value > 0 else None,
            "dividend_yield": dividend_yield,
            "cash_net_buyback_yield": cash_buyback_yield,
            "net_repurchase_yield_proxy": sec.get("repurchase_yield_proxy"),
            "net_buyback_yield_used": buyback_yield,
            "buyback_method": "cash repurchases less issuance" if cash_buyback_yield is not None else "inverse diluted-share growth proxy",
            "net_debt_paydown_yield": debt_paydown_yield,
            "shareholder_yield": sum(shareholder_parts) if shareholder_parts else None,
            "data_status": "OK" if ticker in sec_by_ticker and price_map else "Partial",
        }
        rows.append(row)

    score_rows(rows)
    rows.sort(key=lambda row: (-(row.get("overall_score") or -999), row["ticker"]))
    coverage = {
        "universe": len(rows),
        "sec_success": len(sec_by_ticker),
        "price_success": sum(bool(prices.get(yahoo_symbol(row["ticker"]), {}).get("prices")) for row in rows),
        "quality_scores": sum(row.get("quality_score") is not None for row in rows),
        "fundamental_momentum_scores": sum(row.get("fundamental_momentum_score") is not None for row in rows),
        "full_sue_car3": sum(row.get("fundamental_momentum_method") == "SUE + CAR3" for row in rows),
        "valuation_scores": sum(row.get("valuation_score") is not None for row in rows),
        "conservative_investment_scores": sum(row.get("conservative_investment_score") is not None for row in rows),
        "shareholder_yield_scores": sum(row.get("shareholder_yield_score") is not None for row in rows),
        "all_five_scores": sum(row.get("factor_coverage") == 5 for row in rows),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": AS_OF.isoformat(),
        "universe_source": str(universe_path.expanduser().resolve()),
        "universe_rule": "All unique ticker or symbol values in the selected universe input.",
        "coverage": coverage,
        "errors": errors,
        "methodology": {
            "quality": "SPHQ-style: average z of ROE, inverted balance-sheet accruals and inverted leverage; BSA excluded for financial/real-estate SICs; final 1-10 cohort percentile.",
            "fundamental_momentum": "Equal average of SUE and CAR3 cohort percentiles when both exist; available-component fallback flagged; score = 1 + 9*pct.",
            "valuation": "Equal average of earnings yield, FCF yield, book-to-price and sales-to-EV cohort percentiles; financial/real-estate names use earnings yield and book-to-price only.",
            "conservative_investment": "Equal average of inverted asset-growth and inverted capex-to-average-assets cohort percentiles; financial/real-estate names use asset growth only.",
            "shareholder_yield": "Annual dividend yield + cash net-buyback yield (share-count fallback) + net debt paydown yield; debt paydown is excluded for financial/real-estate names.",
            "overall": "Equal average of available 1-10 factor scores; coverage count is reported.",
        },
        "market_data_source": nasdaq_url,
        "rows": rows,
    }


def write_five_factor_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_text(payload), encoding="utf-8")
