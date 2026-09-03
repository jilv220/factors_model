#!/usr/bin/env python3
"""Re-score only quality from SEC cache; preserve dated prices and other factors.

No network calls. Cache files are SEC companyfacts JSON named CIK##########.json.
Every fact is filtered by the input snapshot's as-of date, even in newer caches.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from factors_model.quality import QUALITY_VERSION, QUALITY_DESCRIPTION, annual_quality_inputs, score_quality
from factors_model.fundamentals import add_ranks
from rank_six_factor_model import score_model, write_outputs, DEFAULT_WEIGHTS


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-input", type=Path, required=True)
    parser.add_argument("--revisions", type=Path, required=True)
    parser.add_argument("--sec-cache", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty output directory; historical runs are never overwritten")
    original = json.loads(args.factor_input.read_text())
    revisions = json.loads(args.revisions.read_text())
    as_of = date.fromisoformat(original["as_of"])
    rows, sources = [], []
    for base in original["rows"]:
        cik = base.get("cik")
        cache = args.sec_cache / f"CIK{int(cik):010d}.json" if cik else None
        if cache is None or not cache.is_file():
            raise ValueError(f"Missing SEC cache for {base['ticker']}; will not silently reuse suspect debt")
        facts = json.loads(cache.read_text())
        if int(facts.get("cik", -1)) != int(cik):
            raise ValueError(f"SEC cache CIK mismatch for {base['ticker']}")
        derived = annual_quality_inputs(facts, as_of, bool(base.get("exclude_bsa")))
        row = {**base, **derived, "legacy_quality_score": base.get("quality_score"), "legacy_quality_rank": base.get("quality_rank")}
        for key in ("roe_z", "accruals_z", "leverage_z", "quality_average_z"):
            if key in row:
                row[f"legacy_{key}"] = row.pop(key)
        rows.append(row)
        sources.append({"ticker": base["ticker"], "cik": cik, "path": str(cache.resolve()), "sha256": sha(cache),
                        "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"})
    score_quality(rows)
    for row in rows:
        scores = [row.get(f"{name}_score") for name in ("quality", "fundamental_momentum", "valuation", "conservative_investment", "shareholder_yield")]
        present = [s for s in scores if s is not None]
        row["factor_coverage"] = len(present)
        row["overall_score"] = sum(present) / len(present) if present else None
    add_ranks(rows, "overall_score", "overall_rank")
    factor_payload = {**original, "rows": rows, "quality_version": QUALITY_VERSION,
                      "generated_at": datetime.now(timezone.utc).isoformat(),
                      "methodology": {**original["methodology"], "quality": QUALITY_DESCRIPTION},
                      "scope": "Quality-only revision; original market data and other five factors frozen. Legacy non-quality input fields retained for audit.",
                      "quality_source_cutoff": as_of.isoformat()}
    factor_payload["coverage"] = {**original["coverage"], "quality_scores": sum(r['quality_score'] is not None for r in rows),
                                 "all_five_scores": sum(r['factor_coverage'] == 5 for r in rows)}
    old_six = score_model(original, revisions, DEFAULT_WEIGHTS, as_of.isoformat())
    new_six = score_model(factor_payload, revisions, DEFAULT_WEIGHTS, as_of.isoformat())
    new_six["scope"] = factor_payload["scope"]
    from factors_model.validation import qa_six_factor
    old_map = {r["ticker"]: r for r in old_six["rows"]}
    new_map = {r["ticker"]: r for r in new_six["rows"]}
    comparison = []
    for row in rows:
        old, new = old_map[row["ticker"]], new_map[row["ticker"]]
        comparison.append({"ticker": row['ticker'], "company": old['company'], "sector": old['sector'],
                           "old_quality": old['quality_score'], "new_quality": new['quality_score'],
                           "quality_change": new['quality_score'] - old['quality_score'] if new['quality_score'] is not None and old['quality_score'] is not None else None,
                           "old_quality_rank": old['quality_rank'], "new_quality_rank": new['quality_rank'],
                           "old_overall": old['overall_score'], "new_overall": new['overall_score'],
                           "old_overall_rank": old['overall_rank'], "new_overall_rank": new['overall_rank'],
                           "quality_status": row['quality_status'], "quality_coverage": row['quality_coverage'],
                           "quality_annual_end": row.get('quality_annual_end'), "profitability": row['quality_profitability'],
                           "safety": row['quality_safety'], "accrual_penalty_points": row['quality_accrual_penalty_points'],
                           "quality_debt": row.get('quality_debt'), "debt_status": row.get('quality_debt_status'),
                           "flags": '; '.join(row['quality_flags'])})
    comparison.sort(key=lambda r: r['quality_change'] if r['quality_change'] is not None else -99)
    output.mkdir(parents=True, exist_ok=True)
    write_outputs(output, new_six, revisions)
    def dump(name: str, value: object) -> None:
        (output / name).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    dump("five_factor_inputs.json", factor_payload)
    dump("comparison.json", comparison)
    qa = qa_six_factor(output / 'results.json', {k.removesuffix('_score'): v for k, v in DEFAULT_WEIGHTS.items()})
    nonquality = [k for k in DEFAULT_WEIGHTS if k != 'quality_score']
    qa['checks']['other_factor_scores_unchanged'] = all(new_map[t][k] == old_map[t][k] for t in old_map for k in nonquality)
    qa['checks']['accrual_penalty_bounded'] = all(0 <= r['quality_accrual_penalty_points'] <= 0.9 + 1e-12 for r in rows)
    qa['checks']['negative_accruals_no_bonus'] = all(r['quality_accrual_penalty_points'] == 0 for r in rows if r.get('quality_cash_accruals') is not None and r['quality_cash_accruals'] <= 0)
    qa['pass'] = all(qa['checks'].values())
    dump('qa.json', qa)
    if not qa['pass']:
        raise ValueError('Quality comparison failed validation; see qa.json')
    from factors_model.quality_report import write_quality_report
    write_quality_report(output / 'quality_review.html', comparison, rows, as_of.isoformat())
    with (output / 'quality_comparison.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison[0]))
        writer.writeheader(); writer.writerows(comparison)
    dump("source_manifest.json", {"generated_at": factor_payload['generated_at'], "filing_cutoff": as_of.isoformat(),
                                  "factor_input": str(args.factor_input.resolve()), "factor_input_sha256": sha(args.factor_input),
                                  "revisions_sha256": sha(args.revisions), "sources": sources,
                                  "quality_version": QUALITY_VERSION, "scope": factor_payload['scope'],
                                  "quality_source_sha256": sha(Path(__file__).resolve().parents[1] / 'src/factors_model/quality.py'),
                                  "debt_source_sha256": sha(Path(__file__).resolve().parents[1] / 'src/factors_model/debt.py')})
    print(json.dumps({"output_dir": str(output), "rows": len(rows), "quality_scored": sum(r['quality_score'] is not None for r in rows),
                      "quality_ok": sum(r['quality_status'] == 'OK' for r in rows)}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
