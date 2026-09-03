"""Small standalone reader report for the quality-only revision audit."""
from __future__ import annotations

from html import escape
from pathlib import Path


def write_quality_report(path: Path, comparison: list[dict], rows: list[dict], as_of: str) -> None:
    by_ticker = {r['ticker']: r for r in rows}
    complete = sum(r['quality_status'] == 'OK' for r in comparison)
    partial = sum(r['quality_status'] == 'Partial' for r in comparison)
    insufficient = len(rows) - complete - partial
    def number(value):
        return '—' if value is None else f'{value:.2f}'
    def table(records):
        body = []
        for r in records:
            facts = by_ticker[r['ticker']]
            source = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(facts['cik']):010d}.json"
            body.append(f"<tr><td><a href='{source}'>{escape(r['ticker'])}</a><small>{escape(r['company'] or '')}</small></td>"
                        f"<td>{number(r['old_quality'])}</td><td><b>{number(r['new_quality'])}</b></td>"
                        f"<td>{number(r['quality_change'])}</td><td>{r['new_quality_rank'] or '—'}</td>"
                        f"<td>{escape(r['quality_annual_end'] or '—')}</td>"
                        f"<td><span class='status {r['quality_status'].lower()}'>{r['quality_status']}</span>"
                        f"<small>{r['quality_coverage']:.0%} descriptor coverage</small></td></tr>")
        return '<div class="scroll"><table><thead><tr><th>Company / SEC source</th><th>Old quality</th><th>New quality</th><th>Change</th><th>New rank</th><th>Fiscal year end</th><th>Input status</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>'
    focus = [next(r for r in comparison if r['ticker'] == ticker) for ticker in ('KHC', 'TAP', 'GIS', 'RTX', 'HSY', 'FIS') if ticker in by_ticker]
    all_rows = sorted(comparison, key=lambda r: -(r['new_quality'] or -1))
    html = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quality factor revision</title><style>
:root{font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#203333;background:#f3f5f2}
body{margin:0}main{max-width:1160px;margin:auto;padding:52px 28px 70px}h1{font-size:38px;line-height:1.16;letter-spacing:-1px;max-width:850px;margin:12px 0 20px}h2{font-size:25px;margin:38px 0 14px}p{max-width:920px}.eyebrow{letter-spacing:2px;font-size:12px;font-weight:700;color:#446b66;text-transform:uppercase}
.lead{font-size:19px;max-width:930px}.panel{background:white;border:1px solid #dce3dd;border-radius:12px;padding:22px 26px;margin:22px 0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.metric{background:#e3ede7;padding:18px 22px;border-radius:10px}.metric b{display:block;font-size:30px}.metric span{font-size:14px}.note{font-size:14px;color:#526762}.formula{font:16px/1.8 ui-monospace,SFMono-Regular,monospace;background:#173f38;color:#eaf3eb;border-radius:10px;padding:20px;white-space:pre-wrap}table{border-collapse:collapse;width:100%;background:white;font-size:14px}th,td{text-align:left;padding:13px 12px;border-bottom:1px solid #dce3dd;vertical-align:top}th{background:#e3ede7;font-size:12px;white-space:nowrap}td:nth-child(n+2):nth-child(-n+5){font-variant-numeric:tabular-nums;white-space:nowrap}small{display:block;font-size:12px;color:#62746f;margin-top:3px;max-width:210px}.scroll{overflow-x:auto;border:1px solid #dce3dd;border-radius:10px}a{color:#176456;text-underline-offset:3px}.status{font-size:12px;font-weight:600}.partial{color:#865916}.insufficient{color:#934439}.ok{color:#21644c}li{margin:10px 0}input{font:inherit;border:1px solid #9db1a9;padding:10px 14px;border-radius:7px;width:300px;max-width:90%;margin-bottom:16px}footer{margin-top:38px;font-size:13px;color:#62746f}@media(max-width:680px){main{padding:28px 16px}h1{font-size:30px}.metrics{grid-template-columns:1fr}.panel{padding:18px}.formula{font-size:13px}}
</style><main><div class="eyebrow">Model revision · September 3, 2026</div>
<h1>Quality now emphasizes profitability and cash generation</h1>
<p class="lead">Large negative accruals no longer earn a quality bonus. The revised model limits earnings-quality penalties, repairs incomplete debt handling, and makes input coverage visible.</p>
<div class="metrics"><div class="metric"><b>75% / 25%</b><span>Profitability / balance-sheet safety</span></div><div class="metric"><b>0.90 points</b><span>Maximum accrual deduction on the 1–10 quality score</span></div><div class="metric"><b>COMPLETE / COUNT</b><span>Stocks with complete, unflagged quality inputs</span></div></div>
<div class="panel"><b>Formula verified; input coverage remains partial.</b><p>PARTIAL scores are provisional and INSUFFICIENT stock lacks enough evidence for a quality rating. Missing or conflicting debt receives a neutral safety component, never zero-debt credit. This is a formula and data comparison, not evidence of improved future returns.</p><p class="note">Comparison date: ASOF. Prices, analyst revisions, and the other factor scores remain frozen at their original values. Only quality inputs and its scoring rule are revised. The new score is a weighted composite; the old score represented a final cohort percentile, so the scales are not identical.</p></div>
<h2>The cases that exposed the problem</h2>FOCUS_TABLE
<p class="note">KHC remains provisional: its SEC tags identify $21.219bn of long-term debt, but do not establish complete short-term coverage for the matched year. No unverified zero is inserted. Company links lead to the public SEC data; exact concepts, periods, filing dates and accessions are retained in the accompanying input file.</p>
<h2>The revised formula</h2><div class="formula">P = mean(percentile(ROE), percentile(ROA), percentile(CFO / average assets))
S = inverted percentile(complete debt / assets)
A = (net income − operating cash flow) / average assets
penalty = 0.10 × min(1, max(0, A) / 0.10)
Quality = 1 + 9 × clip(0.75 × P + 0.25 × S − penalty, 0, 1)</div>
<ul><li><b>Profitability:</b> matching annual net income and operating cash flow, with average annual assets/equity. Nonpositive profitability receives an adverse component; nonpositive equity cannot produce favorable ROE.</li><li><b>Bounded accounting adjustment:</b> only positive earnings-minus-CFO gaps incur a penalty. Negative gaps earn nothing. No final percentile transformation amplifies this penalty.</li><li><b>Missing data:</b> fixed neutral slots preserve weights; missing accrual evidence receives the midpoint deduction of 0.45 points. At least two measured profitability descriptors, including ROA and observed earnings, are required. Missing data and nonpositive equity remain visibly flagged.</li><li><b>Financials and real estate:</b> separately ranked ROE/ROA profitability and equity/assets safety; industrial cash-flow accruals are excluded. Equity/assets is a broad capital proxy, not a regulatory capital assessment.</li></ul>
<h2>What the papers support</h2><div class="panel"><p><a href="https://link.springer.com/article/10.1007/s11142-018-9470-2">Asness, Frazzini &amp; Pedersen — Quality Minus Junk</a> supports rank-normalized descriptors and a diversified profitability block. Accruals is one component of a wider definition.</p>
<p><a href="https://onlinelibrary.wiley.com/doi/abs/10.1111/1475-679x.00041">Hribar &amp; Collins — Errors in Estimating Accruals</a> documents why balance-sheet changes can mismeasure accruals around corporate transactions.</p>
<p><a href="https://faculty.tuck.dartmouth.edu/images/uploads/faculty/joseph-gerakos/Ball%2C_Gerakos%2C_Linnainmaa%2C_et_al._2016.pdf">Ball and coauthors — Accruals, Cash Flows, and Operating Profitability</a> supports emphasizing cash profitability. CFO/assets here is an available-data proxy, not their exact cash-based operating profitability measure.</p>
<p><a href="https://www.nber.org/papers/w15940">Novy-Marx — The Other Side of Value</a> supports asset-scaled productive profitability. Gross profitability is deferred until matching revenue/cost coverage is audited.</p>
<p class="note">The 75/25 weights, penalty-only treatment and 10%-of-assets threshold are explicit engineering choices. They were not optimized on these stocks and are not coefficients prescribed by the papers. Growth and payout remain in the existing separate factors.</p></div>
<h2>Full cohort comparison</h2><label for="filter">Find a company</label><br><input id="filter" placeholder="Ticker or company name" aria-label="Filter companies"><div id="cohort">ALL_TABLE</div>
<h2>Limits and next validation</h2><p>Annual alignment gives up some timeliness. Positive accruals can reflect legitimate working-capital growth; impairments can still affect profitability and asset denominators. Coverage flags require source review before relying on a specific score. The unchanged valuation, investment and shareholder-yield scores retain their historical inputs and may have their own defects. This revision does not establish that any security is a value trap or a buy/sell candidate.</p>
<p>Forward validation should freeze inputs before measuring returns, compare turnover and sector exposures, and test multiple periods without tuning to the named cases.</p>
<footer>Sources: original August 26 model snapshot; SEC companyfacts restricted to filings available by ASOF; paper links above. Original run preserved. Method identifier: profitability_quality_v2.</footer></main>
<script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('#cohort tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script></html>"""
    replacements = {'COMPLETE': str(complete), 'COUNT': str(len(rows)), 'PARTIAL': str(partial),
                    'INSUFFICIENT': str(insufficient), 'ASOF': escape(as_of),
                    'FOCUS_TABLE': table(focus), 'ALL_TABLE': table(all_rows)}
    for key, value in replacements.items():
        html = html.replace(key, value)
    path.write_text(html)
