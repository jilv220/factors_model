# Quality v2: profitability first, bounded accrual influence

Implemented 2026-09-03 as `profitability_quality_v2` in the existing six-factor pipeline. The new default configuration is `configs/six_factor_ranking_v3.toml`; the six-factor model ID is `low_beta_low_bad_beta_six_factor_v2`. Quality retains its 25% overall weight. The other five factor definitions and weights are unchanged.

## Evidence and design

See [the primary-source research review](quality_research_notes.md) for seven academic/official sources, their findings, and distinctions between the published constructions and this implementation. The key changes are motivated by QMJ's use of ranks and several profitability descriptors, Hribar–Collins' evidence of balance-sheet accrual measurement error, and the cash-profitability literature. These exact weights and thresholds are engineering choices, not published optimal coefficients or fitted backtest results.

The old raw-ratio z-scores gave a loss-making company's ROE a modest negative contribution while extreme balance-sheet shrinkage generated a much larger favorable accruals contribution. Missing or partial debt could simultaneously corrupt leverage, accruals, enterprise value and debt paydown. The new formula removes balance-sheet accruals from quality scoring and repairs debt extraction separately.

## Exact rule

For nonfinancials, use matched annual net income and operating cash flow. Average assets/equity are the arithmetic mean of current and prior annual balances, with prior dates 330–400 days apart. Annual flow durations are 330–400 days; income and CFO must share the exact start/end dates. If total CFO is unavailable, continuing plus discontinued operating cash flow can be added only when both are explicitly reported for the same period in the same filing. No omitted component is assumed zero.

```text
ROE  = annual net income / positive average equity
ROA  = annual net income / positive average assets
CFOA = annual operating cash flow / positive average assets
P    = mean(p(ROE), p(ROA), p(CFOA))
S    = 1 - p(complete gross debt / current annual assets)
A    = (annual net income - annual CFO) / average assets
penalty = 0.10 * min(1, max(0, A) / 0.10)
Q    = 1 + 9 * clip(0.75*P + 0.25*S - penalty, 0, 1)
```

`p` is a midrank percentile. Ties share the midpoint; a singleton or constant-valued set is neutral. Nonpositive profitability descriptors receive zero, while positive observations are ranked among positive observations. Nonpositive current/prior equity gives ROE an adverse zero slot and excludes the misleading ROE value from peer calibration. Stale annual data (over 550 days old) does not enter peer calibration and cannot generate an eligible quality score. There is **no final percentile remapping**: the maximum direct accrual deduction is 0.9 quality points, or 0.225 overall-score points when all six factors are present and quality has its normal 25% weight. Rank movement can be larger in a dense cohort.

Negative accruals contribute no positive bonus. Impairments can still change profitability, denominators, and an existing positive-gap penalty; the rule does not promise universal impairment invariance. Positive accruals may also reflect legitimate working-capital investment. The 10%-of-average-assets saturation threshold is deliberately simple and not optimized on this sample.

## Missing observations and sector treatment

- Missing profitability/safety slots remain at neutral 0.5; their weights are not reassigned to remaining observations.
- Missing nonfinancial cash-accrual evidence gets a flagged midpoint deduction of 0.45 quality points. It is not recorded as observed zero accruals.
- At least two measured profitability descriptors, observed annual earnings, and valid ROA are required. Leverage alone cannot generate quality.
- `quality_coverage`, `quality_status` and `quality_flags` accompany results and CSV rankings. Scores with missing inputs or adverse equity flags are provisional; flags do not automatically veto a stock.
- Financial/real-estate SICs retain their separate treatment: P averages ROE and ROA, safety uses equity/assets, and there is no industrial CFO/accrual component. These companies are ranked in a separate reference group. Equity/assets is a broad capital proxy, not regulatory capital adequacy. This group is not a complete industry-specific financial-company model.
- Quality uses annual data for period consistency. Fiscal period and source filing dates are exposed; this is less timely than a correctly assembled TTM model.

## Debt controls

`debt.py` treats short-term borrowings, current long-term debt and noncurrent long-term debt as disjoint roles. Aggregates are reconciled rather than summed again. Capital-lease and finance-lease aliases are supported. Components must come from the same filing and precede the cutoff. Conflicting tags, missing components and partial totals are unavailable for arithmetic, with observed lower bounds retained solely as diagnostics. Explicitly reported zero remains zero. Custom issuer tags require an explicit semantic mapping; names/labels are not guessed.

These controls apply to all future live collection, including legacy-formula configurations. Missing debt/cash no longer produces a false enterprise value or debt-paydown yield. Legacy frozen fixture execution remains exact because it does not refetch inputs.

## Running and comparing

Normal runs use v2 by default:

```sh
./bin/factors rank-six-factor --as-of 2026-09-03 --output-dir runs/2026-09-03/six_factor_quality_v2
```

Quality-only historical comparison, with a local SEC companyfacts cache:

```sh
.venv/bin/python scripts/revise_quality_snapshot.py \
  --factor-input runs/2026-08-26/six_factor_ranking/five_factor_inputs.json \
  --revisions runs/2026-08-26/six_factor_ranking/analyst_revisions.json \
  --sec-cache runs/2026-08-26/quality_revision/sec_cache \
  --output-dir runs/2026-08-26/quality_v2_review
```

The comparison script is offline, checks CIK identity, filters all facts by the original as-of date, requires an empty destination, and stores source hashes and filing provenance. It emits the reader report, revised six-factor results, detailed quality inputs, comparison table, and QA. Prices, revisions and all non-quality factor scores stay frozen; inherited non-quality input fields are identified as legacy. The unchanged investment/value/yield scores can retain the historical defects identified during the audit; this comparison does not claim a full fundamental refresh.

Original fixture check:

```sh
./bin/factors rank-six-factor --frozen-baseline --output-dir /tmp/quality-legacy-check
.venv/bin/python -m unittest discover -s tests
```

Old configurations explicitly request `sphq_quality_v1`. A scored snapshot cannot be relabeled as a different method; it must be rebuilt. The default v3 configuration retains the old fixture route for regression compatibility.

## Validation posture

Tests cover the accrual influence bound, no negative-accrual reward, loss/negative-equity behavior, missing components, finite/tied observations, financial peers, annual alignment, as-of exclusion, debt reconciliation, and collection-to-scoring integration. The August 26 cohort comparison is a mechanical formula/data sensitivity, **not** a predictive backtest. Future validation needs point-in-time input freezes, forward returns, turnover and sector exposure checks across multiple periods. No claim of better realized or expected returns is made.
