# Non-quality scoring audit — September 4, 2026

Reviewed fundamental momentum, analyst revisions, valuation, conservative investment, shareholder yield, and the combined-score input boundary. Quality scoring and its weights were not changed. Existing run artifacts were not overwritten.

## Confirmed defects corrected

- SEC observations were grouped using filing `fy/fp` labels. Comparative facts can carry the filing's labels rather than their observation's quarter. Quarter reconstruction now uses actual dates and matching cumulative concepts, requires a complete preceding cumulative period, and prefers direct quarterly facts. It no longer subtracts Q1 alone from nine months when Q2 is missing.
- Fact selection could retain an older revision depending on input order. Concept priority now precedes latest filing date deterministically.
- TTM and SUE previously accepted gaps in quarterly history. TTM now requires contiguous quarters; SUE uses only the uninterrupted suffix.
- FCF could subtract annual capex from a different TTM CFO period. Start and end dates must now match; annual/TTM flow dates are retained in `flow_periods`, and flows older than 550 days are unavailable.
- Missing dividends/repurchases/issuance were represented as zero, and either cash-buyback leg was sufficient to calculate net buybacks. Missing legs now remain missing; cash net buybacks require both observed legs, otherwise the existing share-count fallback applies. Payout facts must match the latest annual balance-sheet endpoint and not be stale.
- Financial-company FCF, sales/EV and capex inputs influenced nonfinancial peer distributions even though excluded from those companies' own scores. They are now excluded from calibration too.
- Missing revisions were zero-valued observations in peer calibration. New `nonquality_v2` snapshots exclude them and assign neutral 5.5 scores to singleton/constant revision cohorts. Unversioned historical fixtures retain their explicitly documented workbook convention.
- Invalid nonpositive market caps no longer become valuation denominators. Nonfinite ratios are unavailable. Out-of-range input factor scores fail clearly rather than entering the combined score.
- CAR3 no longer maps an event more than four calendar days forward to the first available return.

## KHC evidence from local files

September 3 snapshot: conservative investment 9.1581, shareholder yield 9.0288, valuation 6.7619, momentum 4.4150.

The locally cached SEC companyfacts show $8 million of stock-option proceeds for the year ended December 28, 2024. The old extraction combined this with 2025 repurchases. The corrected extraction marks current-year issuance unavailable. KHC reports revenue under `RevenueFromContractWithCustomerIncludingAssessedTax`; this missing fallback has been added.

KHC's 7.36% asset contraction receives approximately the 97.8th percentile for conservative asset growth in the stored cohort. This is consistent with the existing formula, but asset contraction can reflect impairments or disposals rather than restrained capital investment. Negative asset growth now carries an explicit interpretation flag. This audit does not assert that all KHC contraction was impairment-driven.

## Remaining methodology and data limitations

These safeguards do not make a high score a guarantee of clean economics:

- Conservative investment still rewards accounting asset contraction. Adjusting for impairments/disposals requires reconciled annual asset bridges and a versioned methodology change.
- Available-component averaging can raise valuation or momentum scores when a weak component is missing. Overall weights also renormalize by design. Valuation coverage is now exposed; momentum already labels SUE-only/CAR3-only fallback. Changing those weights to neutral missing slots is a model decision.
- Shareholder yield still sums available components, which can omit a negative missing leg. Coverage and partial status are now exported. Share-count fallback can be distorted by splits or acquisition issuance; cash-flow issuance concept coverage is not a full reconciliation of all financing transactions.
- Near-zero EPS revision denominators and GAAP earnings shocks can create extreme raw signals. Percentile mapping bounds displayed scores but does not establish economic comparability. Thin analyst coverage remains eligible and labeled.
- CAR3 uses filing-date event proxies, not verified announcement timestamps; generic 6-K filings can be unrelated to earnings. Price gaps, splits, fiscal transitions and mixed accounting concepts remain possible data issues requiring source reconciliation.
- Annual balance sheets and current market data are intentionally mixed in some descriptors. Arbitrary historical runs do not guarantee historically available market metadata or analyst estimates.

## Validation

76 unit/integration tests pass, including 12 new missing-data, period-alignment, peer-exclusion, KHC revenue-tag, stale-issuance and revision-distribution cases. Offline extraction was exercised on 140 cached companies; the diagnostic is in `runs/2026-09-04/nonquality_audit/cached_extraction_audit.json`. August 26 caches and September 3 inputs have different source vintages, so differences are not clean code-only attribution or refreshed rankings. No external data was fetched.
