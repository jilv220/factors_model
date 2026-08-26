# factors-model

This repository provides versioned command-line baselines around the existing bad-beta and six-factor ranking models. It standardizes how a run is configured, invoked, recorded, checked, and compared without changing either investment methodology.

## Baselines

- `configs/bad_beta_v1.toml`: VAR cash-flow-news bad beta, `rho=0.95`, 36-month stock window, 24-month minimum history, and a universe-relative 3x3 beta/bad-beta grid. The default public universe is the 2026-06-16 candidate set; no holdings, weights, sides, or portfolio sizing are used.
- `configs/six_factor_ranking_v1.toml`: the established six-factor model—quality, fundamental momentum, analyst revisions, valuation, conservative investment, and shareholder yield. Its normal input is a ticker universe, not a pre-scored factor snapshot.

Both pipeline implementations and all frozen baseline inputs are owned by this repository:

- `scripts/`: bad-beta and six-factor pipeline implementations.
- `data/bad_beta/`: the frozen public ticker universe and Shiller CAPE inputs.
- `data/six_factor/`: regression-only five-factor and revision fixtures plus the verified reference workbook.
- `baselines/`: immutable regression snapshots used by `factors verify`.

The configs resolve scripts relative to `configs/` and inputs relative to the repository root. Both the CLI launcher and pipeline configs use the project-local `.venv`; no configured code, data, or runtime path depends on another project.

## Set up the self-contained runtime

From this repository, run once:

```bash
./bin/setup
```

This creates the ignored project-local `.venv` and installs the pinned NumPy,
pandas, xlrd, and yfinance dependencies declared in `pyproject.toml`.

## Run the pipelines

After setup:

```bash
./bin/factors --help
./bin/factors bad-beta --dry-run
./bin/factors rank-six-factor --dry-run
```

The equivalent project-local Python form is:

```bash
.venv/bin/python -m factors_model --help
```

`./bin/factors` refuses to fall back to a system or external-project Python. If
the local runtime is missing, it tells you to run `./bin/setup`.

## Run six-factor rankings on any universe

```bash
./bin/factors rank-six-factor
```

The configured default is the repository's public candidate universe. Select any
CSV, JSON, text file, or directory of CSVs with `--universe`:

```bash
./bin/factors rank-six-factor \
  --universe "/path/to/my_tickers.csv" \
  --as-of 2026-08-26 \
  --output-dir "runs/2026-08-26/six_factor_ranking"
```

The universe needs a `ticker` or `symbol` column; `Ticker` and `Symbol` are also
accepted. For each unique ticker, the pipeline collects SEC reported fundamentals,
Yahoo adjusted prices and analyst revisions, and Nasdaq market metadata. Factor
scores and ranks are calculated relative to the selected cohort. Input quantity,
weight, and side columns are ignored.

`rank-fundamentals` remains an alias for compatibility. The old five-factor
snapshot is not used by normal runs. It is available only through the explicit
offline regression command:

```bash
./bin/factors rank-six-factor --frozen-baseline
```

The weighting contract is:

- Quality: 25%
- Fundamental momentum: 25%
- Analyst revisions: 16.67%
- Valuation: 11.11%
- Conservative investment: 11.11%
- Shareholder yield: 11.11%

Missing factor values are handled by renormalizing these weights across the available scores. Outputs contain model and source fields only—no portfolio quantities, sides, or position weights.

## Run the bad-beta baseline

The bad-beta baseline runs on a generic public ticker universe. It defaults to the same candidate directory used by the fundamental-ranking baseline:

```bash
./bin/factors bad-beta
```

Use `--universe /path/to/tickers.csv` or `--universe /path/to/candidate-directory` to select another research universe. The input needs a `ticker` or `Ticker` column. The shared default is under `data/universes/2026-06-16`. Use `--dry-run` to inspect the exact command without contacting external services.

## Verify frozen baselines offline

```bash
./bin/factors verify --config configs/bad_beta_v1.toml
./bin/factors verify --config configs/six_factor_ranking_v1.toml
```

Compare a new output with its frozen baseline:

```bash
./bin/factors verify \
  --config configs/bad_beta_v1.toml \
  --actual runs/2026-07-10/bad_beta/results.json
```

Bad-beta verification allows up to `1e-5` absolute drift in numeric exposures to
accommodate small revisions in Yahoo's adjusted-close history. Ticker coverage,
row status, and beta/bad-beta grid membership must still match exactly.

To refresh the regression report and manifest for an existing run after changing
only the verification contract, add `--update-run`:

```bash
./bin/factors verify \
  --config configs/bad_beta_v1.toml \
  --actual runs/2026-06-16/bad_beta/results.json \
  --update-run
```

## Run artifacts

Successful runs write into `runs/<as-of>/<pipeline>/` by default:

- `run_manifest.json`: baseline version, command, code revisions, input hashes, expected-snapshot hash, and status.
- `qa.json`: deterministic structural checks and summary metrics.
- `regression.json`: comparison with the frozen snapshot when no material baseline inputs or parameters were overridden.
- `run.log`: command output and errors.
- `results.json` for both models.
- `rankings.csv`, `five_factor_inputs.json`, and `analyst_revisions.json` for live six-factor ranking.

An existing non-empty run directory is not overwritten unless `--force` is supplied.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
