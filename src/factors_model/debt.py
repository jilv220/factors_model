"""Extract debt without treating missing or partial SEC facts as total debt.

``LongTermDebt`` excludes short-term borrowings. The older capital-lease
``LongTermDebtAndCapitalLeaseObligations`` tag is *noncurrent*, whereas its
``IncludingCurrentMaturities`` counterpart includes current maturities.
Missing short-term borrowings never means zero: a long-term-only observation is
returned with ``value=None`` and its known amount in ``observed_debt``.

Only observations from one filing are combined. ``as_of`` applies to both the
filing and balance-sheet dates. Custom extension tags require an explicit role
mapping; guessing debt coverage from issuer labels is unsafe.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Mapping, Sequence


ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
SEC_FORMS = ANNUAL_FORMS | {"10-Q", "10-Q/A"}

# Roles describe coverage, not just maturity. Never put short-term borrowings
# and the current portion of long-term debt in the same alias list.
DEBT_CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "total": (
        ("us-gaap", "DebtAndFinanceLeaseObligations"),
        ("us-gaap", "DebtAndCapitalLeaseObligations"),
        ("ifrs-full", "Borrowings"),
    ),
    "long_term": (
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsIncludingCurrentMaturities"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"),
        ("us-gaap", "LongTermDebt"),
    ),
    "long_term_current": (
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsCurrent"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
        ("us-gaap", "LongTermDebtCurrent"),
    ),
    "noncurrent": (
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"),
        ("us-gaap", "LongTermDebtAndFinanceLeaseObligations"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsNoncurrent"),
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("ifrs-full", "NoncurrentBorrowings"),
    ),
    "short_term": (
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "ShortTermDebt"),
    ),
    "current_total": (
        ("us-gaap", "DebtCurrent"),
        ("ifrs-full", "CurrentBorrowings"),
    ),
}


def _iso(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _equal(left: float, right: float) -> bool:
    # Small rounding differences between balance-sheet and note presentations.
    return math.isclose(left, right, rel_tol=0.001, abs_tol=1.0)


def _sum(parts: Sequence[dict[str, Any]]) -> float:
    return sum(item["value"] for item in parts)


def _assemble(records: list[dict[str, Any]]) -> dict[str, Any]:
    selected: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=lambda item: item["priority"]):
        selected.setdefault(record["role"], record)
    total = selected.get("total")
    lt = selected.get("long_term")
    current = selected.get("long_term_current")
    noncurrent = selected.get("noncurrent")
    short = selected.get("short_term")
    current_total = selected.get("current_total")
    issues: list[str] = []
    missing: list[str] = []
    conflict = False

    # Prefer an explicit split, which can include lease debt excluded from a
    # plain LongTermDebt aggregate. Do not add the aggregate to its components.
    long_parts = [current, noncurrent] if current and noncurrent else ([lt] if lt else [])
    if not long_parts:
        long_parts = [item for item in (current, noncurrent) if item is not None]
    long_complete = bool(lt or (current and noncurrent))
    if lt and current and noncurrent and not _equal(lt["value"], _sum(long_parts)):
        if lt["concept"] == "LongTermDebt" and _sum(long_parts) >= lt["value"]:
            issues.append("long_term_components_include_additional_lease_debt")
        else:
            issues.append("conflicting_long_term_total")
            conflict = True

    if current_total:
        current_parts = [item for item in (current, short) if item is not None]
        if _sum(current_parts) > current_total["value"] and not _equal(_sum(current_parts), current_total["value"]):
            issues.append("current_total_below_observed_components")
            conflict = True
        parts = [current_total] + ([noncurrent] if noncurrent else [])
        complete = noncurrent is not None
        if not complete:
            missing.append("noncurrent_debt")
    else:
        parts = long_parts + ([short] if short else [])
        complete = long_complete and short is not None
        if not long_complete:
            missing.append("long_term_debt_or_both_maturity_components")
        if short is None:
            missing.append("short_term_borrowings")

    amount = _sum(parts) if parts else None
    if total:
        if amount is not None and amount > total["value"] and not _equal(amount, total["value"]):
            # Some filers use a nominal total-debt tag for their LT debt table.
            # Reconcile to disjoint ST + LT components, without a ticker rule.
            if complete and not current_total and short and _equal(total["value"], _sum(long_parts)):
                issues.append("reported_total_excludes_short_term_borrowings")
            else:
                issues.append("reported_total_below_observed_components")
                conflict = True
        else:
            if complete and amount is not None and not _equal(amount, total["value"]):
                issues.append("reported_total_exceeds_component_sum")
            parts, amount, complete, missing = [total], total["value"], True, []

    if conflict:
        complete = False
    base = max(records, key=lambda item: (item["filed"], -item["priority"]))
    return {
        **{key: base.get(key) for key in ("end", "filed", "form", "fy", "fp", "frame", "accn")},
        "start": None,
        "days": None,
        "priority": 0,
        "taxonomy": "derived",
        "concept": "+".join(f"{item['taxonomy']}:{item['concept']}" for item in parts),
        "value": amount if complete else None,
        "observed_debt": amount,
        "status": "complete" if complete else ("conflicting" if conflict else "partial"),
        "missing_components": missing,
        "issues": issues,
        "components": [{key: item.get(key) for key in ("role", "taxonomy", "concept", "value", "filed", "accn")} for item in parts],
        "available_components": [{key: item.get(key) for key in ("role", "taxonomy", "concept", "value")} for item in selected.values()],
    }


def debt_series(
    payload: dict[str, Any],
    *,
    as_of: date | str,
    annual_only: bool = False,
    extra_concepts: Mapping[str, Sequence[tuple[str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """Return one auditable debt observation per balance-sheet date.

    ``value`` is usable only for ``status == 'complete'``. Missing debt facts
    produce no observation; partial coverage retains ``observed_debt`` solely
    for diagnostics. A reported zero remains zero. ``extra_concepts`` can add
    issuer namespace facts to one of the explicitly defined coverage roles.
    All amounts are USD, consistent with the fundamentals collection pipeline.
    """
    cutoff = _iso(as_of)
    if cutoff is None:
        raise ValueError("as_of must be an ISO date")
    concepts = {role: list(items) for role, items in DEBT_CONCEPTS.items()}
    for role, items in (extra_concepts or {}).items():
        if role not in concepts:
            raise ValueError(f"unknown debt coverage role: {role}")
        concepts[role].extend(items)
    forms = ANNUAL_FORMS if annual_only else SEC_FORMS
    groups: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
    for role, aliases in concepts.items():
        for priority, (taxonomy, concept) in enumerate(aliases):
            node = payload.get("facts", {}).get(taxonomy, {}).get(concept, {})
            for item in node.get("units", {}).get("USD", []):
                end, filed = _iso(item.get("end")), _iso(item.get("filed"))
                if not end or not filed or filed > cutoff or end > cutoff or item.get("start"):
                    continue
                if item.get("form") not in forms or isinstance(item.get("val"), bool):
                    continue
                try:
                    value = float(item["val"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not math.isfinite(value) or value < 0:
                    continue
                record = {
                    **{key: item.get(key) for key in ("form", "fy", "fp", "frame", "accn")},
                    "value": value, "end": end, "filed": filed, "role": role,
                    "priority": priority, "taxonomy": taxonomy, "concept": concept,
                }
                # Without accession metadata, same-date facts remain combinable
                # for fixtures and other companyfacts-compatible sources.
                key = (filed, str(item.get("accn") or item.get("form")))
                groups.setdefault(end, {}).setdefault(key, []).append(record)
    return [_assemble(filings[max(filings)]) for _, filings in sorted(groups.items())]
