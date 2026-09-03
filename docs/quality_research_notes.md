# Quality scoring research and implementation recommendation

Research checked 2026-09-03. This note records the evidence review and selected design, not a replication of any named index or paper and not a predictive backtest. The implementation and run report document the observed effects separately.

## Findings from primary sources

| Source | Finding relevant to this model | Design implication and limit |
| --- | --- | --- |
| [Novy-Marx (2013), The Other Side of Value: The Gross Profitability Premium](https://www.nber.org/papers/w15940), published in *Journal of Financial Economics* 108, 1–28; linked NBER working paper | Gross profits divided by assets predicts cross-sectional returns and complements value. | Quality should contain direct productive earning power. Gross profit/assets is worth adding when revenue and cost of goods sold have reliable matching coverage; it is not equivalent to ROE or CFO/assets. |
| [Asness, Frazzini and Pedersen (2019), Quality Minus Junk](https://link.springer.com/article/10.1007/s11142-018-9470-2), *Review of Accounting Studies* 24, 34–112, sections 3.2 and appendix | Uses ranks standardized as z-scores, rather than z-scores of raw ratio magnitudes. Profitability averages six descriptors: gross profit/assets, ROE, ROA, cash flow/assets, gross margin, and low accruals. Published baseline quality combines profitability, growth, and safety; payout is an alternative specification. | Support for several profitability measures and bounded rank influence. Accruals is one descriptor inside a larger block, not one-third of the whole score. The paper's cash-flow descriptor includes capital expenditure and working-capital adjustments, so SEC operating CFO/assets must be labeled a proxy, not an exact QMJ replication. |
| [Sloan (1996), Do Stock Prices Fully Reflect Information in Accruals and Cash Flows About Future Earnings?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2598), *The Accounting Review* 71, 289–315 | Earnings persistence depends on the cash versus accrual composition; prices do not fully incorporate that distinction in the study. | Cash backing of earnings matters. This does not prove that every large negative accrual, especially a write-down, means a high-quality business. |
| [Hribar and Collins (2002), Errors in Estimating Accruals: Implications for Empirical Research](https://onlinelibrary.wiley.com/doi/abs/10.1111/1475-679x.00041), *Journal of Accounting Research* 40, 105–134 | Changes in successive balance sheets can mismeasure accruals relative to cash-flow-statement measurements, particularly around acquisitions and discontinued operations. | Supports avoiding balance-sheet change as the sole earnings-quality measure. Their result concerns measurement error; the specific treatment of impairments below is an engineering response to this model's failure case. |
| [Ball, Gerakos, Linnainmaa and Nikolaev (2016), Accruals, Cash Flows, and Operating Profitability in the Cross Section of Stock Returns](https://faculty.tuck.dartmouth.edu/images/uploads/faculty/joseph-gerakos/Ball%2C_Gerakos%2C_Linnainmaa%2C_et_al._2016.pdf), *Journal of Financial Economics* 121, 28–45, Table 1 and appendix | Cash-based operating profitability outperforms accrual-inclusive profitability in their sample and subsumes the accrual predictor. Their numerator is operating profit, adjusted for operating working-capital changes, scaled by lagged assets. Financial SICs beginning with 6 are excluded. | Favors cash profitability over a large standalone negative-accrual reward. SEC CFO/assets is a useful available-data proxy but is not their exact measure: it has different treatment of interest, tax, R&D and working capital. Do not claim a replication or extend their empirical result directly to financial firms. |
| [S&P Quality Indices Methodology](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-quality-indices.pdf), current downloaded link, appendices A–B | Uses ROE, debt/equity, and the change in net operating assets divided by average assets. The composite averages winsorized z-scores. Negative earnings or book equity receive the worst ROE z-score; affected names are excluded from index inclusion/rankings. Accruals is omitted for Financials and Real Estate. | The existing model was only S&P-style and did not reproduce these safeguards. A replacement may keep every name scored while reducing weak profitability's contribution; it should not continue claiming S&P methodology equivalence. |
| [MSCI Quality Indexes](https://www.msci.com/indexes/group/quality-indexes) and [data-handling change effective May 2025](https://app2.msci.com/webapp/index_ann/DocGet?format=html&lang=en&pub_key=Rsk%2FI1RPQbI%3D) | Quality uses profitability, earnings stability and leverage. The 2025 change made ROE and debt/equity mandatory descriptors. | Accruals is not a necessary ingredient of every quality definition. Completeness of leverage and profitability data is a design issue, not merely an output warning. |

## Selected bounded design

These weights and safeguards are engineering choices. None of the papers establish their optimality for this candidate universe. Prefer a transparent initial model and future point-in-time validation over fitting weights to KHC, TAP or current rankings.

For nonfinancial companies, define a midrank percentile `p(x)` on eligible, finite, comparable observations. Ties receive their average rank. Use separate peer normalization where accounting economics differ; broad sector ranks need sufficient observations. A singleton or constant-valued cohort should be neutral, not best quality.

Use direct profitability descriptors with aligned numerator/denominator periods:

```text
ROE   = net income / positive average equity
ROA   = net income / positive average assets
CFOA  = operating cash flow / positive average assets
D_A   = complete interest-bearing debt / positive assets
P     = mean(p(ROE), p(ROA), p(CFOA)) using fixed slots
S     = 1 - p(D_A)
A     = (net income - operating cash flow) / positive average assets
gap   = max(0, A)
```

The selected implementation uses matching annual net income/CFO with average annual assets/equity and exposes dated annual quality fields. This gives up some timeliness to avoid unreliable TTM assembly. Any future TTM implementation must give net income and CFO the same four-quarter window and reporting scope. Do not subtract annual CFO from TTM earnings or silently mix discontinued/continuing operations scopes.

The initial rule is:

```text
penalty = min(gap / 0.10, 1)
Q_raw   = clip(0.75*P + 0.25*S - 0.10*penalty, 0, 1)
Q_score = 1 + 9*Q_raw
```

The fixed penalty scale reaches its cap when positive accruals equal 10% of average assets, and is exactly zero when `A <= 0`. This is an unoptimized engineering threshold; positive accruals can also reflect legitimate working-capital growth. Missing accrual input is flagged unavailable, never represented as an observed zero. Gross profitability can later diversify `P` once its coverage is audited, without creating another top-level block.

Removing negative-accrual rewards is the central fix: a write-down must not create a large positive earnings-quality term. Using `(NI-CFO)/assets` without this safeguard still makes impairments look favorable under an inverted score. CFO/assets recognizes actual operating cash generation while ROE and ROA retain the accounting loss. An impairment can still alter ratios, asset denominators and an existing positive-gap penalty; this design does not guarantee universal impairment invariance.

The direct score transformation bounds the accrual penalty at 0.9 displayed points on a 1–10 score. There is no final percentile remapping, which could otherwise amplify the penalty's displayed-point effect. Cohort ranks may still change substantially in a dense distribution. The new score represents a bounded composite rather than the old final percentile interpretation.

Debt/assets reduces the sensitivity to tiny equity denominators and buybacks; it is a safety proxy, not a full solvency model. Complete gross interest-bearing debt must be sourced first. Do not substitute `0` for missing debt, infer a full total from a short-term-only fact, or add overlapping total/current/noncurrent debt concepts.

Require observed earnings plus at least two measured profitability descriptors; never calculate quality from leverage alone. Unavailable slots receive neutral 0.5 percentiles without reallocating their weights, with explicit coverage flags. If equity is nonpositive, ROE must not become artificially positive or simply disappear to improve the average: retain an adverse ROE contribution and flag the condition. Missing debt must result in a neutral safety slot and a clearly provisional score, not a best safety rank. Neutral missing CFO/debt can leave a reasonably high numerical score, making coverage disclosure essential.

## Financials, scope and validation

For financials and real estate, preserve the exclusion of ordinary-company accruals. Bank operating CFO, interest-bearing debt and gross margins have different meanings. The selected fallback uses equally weighted ROE/ROA inside the 75% profitability block and equity/assets inside the 25% safety block, ranked separately from ordinary companies. Label equity/assets a capital proxy, not regulatory capital adequacy. A richer bank profile ultimately needs asset quality and earnings stability as well. The reviewed papers do not validate a one-size-fits-all financial-sector substitution; this fallback remains a disclosed model limitation.

The existing model already separately scores fundamental momentum and shareholder payout. Rebuilding all QMJ growth/payout measures inside quality would alter those exposures; keep this change focused on profitability, cash backing and balance-sheet strength. This is a quality diagnostic, not a determination that a stock is a value trap.

Validate denominator signs, missing versus zero debt, total-versus-component debt combinations, aligned cash-flow periods, tied ranks, and the numerical accrual-impact bound. Show KHC/TAP and the full cohort before/after, including coverage and component attribution. Preserve the historical run; a refreshed ranking is a formula/data sensitivity analysis, not evidence of out-of-sample return improvement.
