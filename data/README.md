# Frozen baseline data

These files are versioned default inputs and regression fixtures. Pipeline code
must treat dated files as immutable; create a new dated snapshot when needed.

## Shared universes

- `universes/2026-06-16/`: public candidate-universe CSV snapshots used by
  both bad beta and six-factor ranking.

## Bad beta

- `bad_beta/shiller_cape.csv` and `.xls`: local Shiller CAPE source snapshots.

## Six-factor ranking

- Normal six-factor runs use a ticker CSV, JSON, text file, or directory selected
  with `--universe`. The configured default is the public candidate universe
  under `universes/2026-06-16/`.
- `six_factor/five_factor_snapshot_2026-07-10.json`: regression-only fixture for
  the five established scores. It does not define normal-run universe membership.
- `six_factor/analyst_revisions_2026-08-09.json`: frozen Yahoo/yfinance
  current-quarter EPS revision fixture.
- `six_factor/reference_rankings_2026-08-09.xlsx`: verified workbook used as
  the independent ranking/formula reference.

Regression expectations live separately under `baselines/`.
