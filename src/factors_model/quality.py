"""Bounded, profitability-led quality; see docs/quality_methodology.md.

The empirical papers motivate descriptors, not these exact engineering weights.
Annual inputs share a fiscal period; the quality score is NOT re-percentiled.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

QUALITY_VERSION = "profitability_quality_v2"
LEGACY_QUALITY_VERSION = "sphq_quality_v1"
QUALITY_DESCRIPTION = (
    "Quality v2: 75% profitability and 25% safety using bounded peer percentiles; "
    "matched annual ROE, ROA and CFO/assets (financials: ROE/ROA, equity/assets). "
    "Positive (net income - CFO)/average assets incurs at most 0.9 score points; "
    "negative accruals receive no bonus. Fixed neutral missing slots, visible coverage; "
    "direct 1-10 composite, no final percentile remapping."
)
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
CONCEPTS = {
    "income": [("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss"), ("ifrs-full", "ProfitLoss")],
    "cfo": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"), ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"), ("ifrs-full", "NetCashFlowsFromUsedInOperatingActivities")],
    "cfo_continuing": [("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")],
    "cfo_discontinued": [("us-gaap", "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations")],
    "assets": [("us-gaap", "Assets"), ("ifrs-full", "Assets")],
    "equity": [("us-gaap", "StockholdersEquity"), ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), ("ifrs-full", "Equity")],
}


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _facts(payload: dict, key: str, as_of: date, duration: bool) -> list[dict]:
    records = []
    for priority, (namespace, concept) in enumerate(CONCEPTS[key]):
        for item in payload.get("facts", {}).get(namespace, {}).get(concept, {}).get("units", {}).get("USD", []):
            try:
                end, filed = date.fromisoformat(item["end"]), date.fromisoformat(item["filed"])
                start = date.fromisoformat(item["start"]) if item.get("start") else None
            except (KeyError, ValueError):
                continue
            value = finite(item.get("val"))
            if value is None or filed > as_of or end > as_of or item.get("form") not in ANNUAL_FORMS:
                continue
            if duration and (start is None or not 330 <= (end - start).days <= 400):
                continue
            if not duration and start is not None:
                continue
            records.append({"value": value, "end": end.isoformat(), "start": start.isoformat() if start else None,
                            "filed": filed.isoformat(), "concept": f"{namespace}:{concept}",
                            "accession": item.get("accn"), "priority": priority})
    return records


def _at(records: list[dict], end: str, start: str | None = None) -> dict | None:
    matches = [r for r in records if r["end"] == end and (start is None or r["start"] == start)]
    return min(matches, key=lambda r: (r["priority"], -date.fromisoformat(r["filed"]).toordinal())) if matches else None


def annual_quality_inputs(payload: dict, as_of: date, financial: bool) -> dict:
    """Use only facts filed by the cutoff; never substitute stale unmatched flows."""
    from .debt import debt_series

    income_facts = _facts(payload, "income", as_of, True)
    asset_facts = _facts(payload, "assets", as_of, False)
    common_ends = {r["end"] for r in income_facts} & {r["end"] for r in asset_facts}
    output: dict[str, Any] = {"quality_version": QUALITY_VERSION, "quality_peer_group": "financial_real_estate" if financial else "nonfinancial"}
    if not common_ends:
        return {**output, "quality_input_flags": ["missing_matched_annual_income_assets"]}
    end = max(common_ends)
    ni = _at(income_facts, end)
    assets = _at(asset_facts, end)
    equity_facts = _facts(payload, "equity", as_of, False)
    equity = _at(equity_facts, end)
    cfo = _at(_facts(payload, "cfo", as_of, True), end, ni["start"])
    if cfo is None:
        continuing = _at(_facts(payload, "cfo_continuing", as_of, True), end, ni["start"])
        discontinued = _at(_facts(payload, "cfo_discontinued", as_of, True), end, ni["start"])
        if (continuing and discontinued and continuing["accession"] == discontinued["accession"]
                and continuing["filed"] == discontinued["filed"]):
            cfo = {**continuing, "value": continuing["value"] + discontinued["value"],
                   "concept": "derived:total_operating_cash_flow", "components": [continuing, discontinued]}
    prior_ends = [r["end"] for r in asset_facts if 330 <= (date.fromisoformat(end) - date.fromisoformat(r["end"])).days <= 400]
    prior_end = min(prior_ends, key=lambda d: abs((date.fromisoformat(end) - date.fromisoformat(d)).days - 365)) if prior_ends else None
    prior_assets = _at(asset_facts, prior_end) if prior_end else None
    prior_equity = _at(equity_facts, prior_end) if prior_end else None
    avg_assets = (assets["value"] + prior_assets["value"]) / 2 if prior_assets else None
    avg_equity = (equity["value"] + prior_equity["value"]) / 2 if equity and prior_equity else None
    debt = next((r for r in debt_series(payload, as_of=as_of, annual_only=True) if r["end"] == end), None)
    def divide(n: float | None, d: float | None) -> float | None:
        return n / d if n is not None and d is not None and d > 0 else None
    sources = {"net_income": ni, "cfo": cfo, "assets": assets, "assets_prior": prior_assets,
               "equity": equity, "equity_prior": prior_equity, "debt": debt}
    flags = []
    if (as_of - date.fromisoformat(end)).days > 550:
        flags.append("stale_annual_quality_inputs")
    if cfo is None and not financial:
        flags.append("missing_matched_annual_cfo")
    if prior_assets is None:
        flags.append("missing_prior_annual_assets")
    if equity is None or prior_equity is None:
        flags.append("missing_annual_equity")
    elif equity["value"] <= 0 or prior_equity["value"] <= 0:
        flags.append("nonpositive_equity")
    debt_value = finite(debt.get("value")) if debt else None
    if debt_value is None and not financial:
        flags.append("incomplete_debt")
    output.update({
        "quality_annual_end": end, "quality_annual_start": ni["start"],
        "quality_net_income": ni["value"], "quality_cfo": cfo["value"] if cfo else None,
        "quality_assets": assets["value"], "quality_average_assets": avg_assets,
        "quality_equity": equity["value"] if equity else None, "quality_average_equity": avg_equity,
        "quality_roe": divide(ni["value"], avg_equity), "quality_roa": divide(ni["value"], avg_assets),
        "quality_cfoa": divide(cfo["value"] if cfo else None, avg_assets),
        "quality_debt": debt_value, "quality_debt_status": debt.get("status", "missing") if debt else "missing",
        "quality_debt_to_assets": divide(debt_value, assets["value"]),
        "quality_equity_to_assets": divide(equity["value"] if equity else None, assets["value"]),
        "quality_cash_accruals": divide(ni["value"] - cfo["value"], avg_assets) if cfo else None,
        "quality_sources": sources, "quality_input_flags": flags,
    })
    return output


def _percentiles(rows: list[dict], key: str, higher: bool = True, profit: bool = False) -> dict[str, float | None]:
    # Ineligible stale data must not shift current companies' reference ranks.
    # An adverse equity condition is scored explicitly below, not calibrated as
    # a positive ROE when averaging one positive and one negative equity value.
    values = [
        None if "stale_annual_quality_inputs" in r.get("quality_input_flags", [])
        or (key == "quality_roe" and "nonpositive_equity" in r.get("quality_input_flags", []))
        else finite(r.get(key))
        for r in rows
    ]
    valid = sorted(v for v in values if v is not None and (not profit or v > 0))
    result = {}
    for row, value in zip(rows, values):
        if value is None:
            p = None
        elif profit and value <= 0:
            p = 0.0
        elif len(valid) < 2:
            p = 0.5
        else:
            p = (sum(v < value for v in valid) + 0.5 * (sum(v == value for v in valid) - 1)) / (len(valid) - 1)
            if not higher:
                p = 1 - p
        result[row["ticker"]] = p
    return result


def score_quality(rows: list[dict]) -> None:
    """Fixed slots prevent missing values from concentrating the remaining weights."""
    for group in ("nonfinancial", "financial_real_estate"):
        peers = [r for r in rows if r.get("quality_peer_group", "financial_real_estate" if r.get("exclude_bsa") else "nonfinancial") == group]
        financial = group == "financial_real_estate"
        profit_keys = ["quality_roe", "quality_roa"] + ([] if financial else ["quality_cfoa"])
        maps = {k: _percentiles(peers, k, profit=True) for k in profit_keys}
        safety_key = "quality_equity_to_assets" if financial else "quality_debt_to_assets"
        safety = _percentiles(peers, safety_key, higher=financial)
        for row in peers:
            ticker = row["ticker"]
            flags = list(row.get("quality_input_flags", []))
            parts = {k: maps[k][ticker] for k in profit_keys}
            if "nonpositive_equity" in flags:
                parts["quality_roe"] = 0.0
            present = sum(v is not None for v in parts.values())
            profitability = sum(v if v is not None else 0.5 for v in parts.values()) / len(parts)
            safety_p = safety[ticker]
            if safety_p is None:
                flags.append("missing_safety_neutral")
            if present < len(parts):
                flags.append("missing_profitability_slots_neutral")
            cash_accruals = finite(row.get("quality_cash_accruals"))
            if financial:
                penalty = 0.0
            elif cash_accruals is None:
                penalty = 0.05
                flags.append("missing_accruals_midpoint_penalty")
            else:
                penalty = 0.10 * min(1.0, max(0.0, cash_accruals) / 0.10)
            raw = max(0.0, min(1.0, 0.75 * profitability + 0.25 * (safety_p if safety_p is not None else 0.5) - penalty))
            eligible = (finite(row.get("quality_net_income")) is not None and
                        finite(row.get("quality_roa")) is not None and present >= 2 and
                        "stale_annual_quality_inputs" not in flags)
            if not eligible:
                flags.append("insufficient_quality_evidence")
            row.update({"quality_version": QUALITY_VERSION, "quality_peer_group": group,
                        "quality_component_percentiles": parts, "quality_profitability": profitability,
                        "quality_safety": safety_p, "quality_accrual_penalty": penalty,
                        "quality_accrual_penalty_points": 9 * penalty, "quality_composite": raw if eligible else None,
                        "quality_score": 1 + 9 * raw if eligible else None,
                        "quality_coverage": (0.75 * present / len(parts) + 0.25 * (safety_p is not None)),
                        "quality_flags": sorted(set(flags)),
                        "quality_status": "Insufficient" if not eligible else "Partial" if flags else "OK"})
    ordered = sorted((r for r in rows if r.get("quality_score") is not None), key=lambda r: (-r["quality_score"], r["ticker"]))
    for row in rows:
        row["quality_rank"] = None
    last_value, last_rank = None, 0
    for index, row in enumerate(ordered, 1):
        if last_value is None or abs(row["quality_score"] - last_value) > 1e-12:
            last_value, last_rank = row["quality_score"], index
        row["quality_rank"] = last_rank
