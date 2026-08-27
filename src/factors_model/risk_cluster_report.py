from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name


ASSIGNMENT_COLUMNS = (
    "ticker",
    "issue_name",
    "sector",
    "sector_etf",
    "cluster_id",
    "cluster_size",
    "portfolio_weight",
    "abs_weight",
    "cluster_multiplier",
    "suggested_weight",
    "beta_market",
    "beta_sector",
    "r_squared",
    "residual_volatility",
    "observations",
    "status",
    "warnings",
)

CLUSTER_COLUMNS = (
    "cluster_id",
    "member_count",
    "members",
    "gross_weight",
    "net_weight",
    "cluster_cap",
    "excess_gross",
    "breach",
    "max_multiplier",
    "weight_complete",
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _formats(workbook: xlsxwriter.Workbook) -> dict[str, Any]:
    navy = "#17365D"
    teal = "#0F766E"
    light_blue = "#D9EAF7"
    light_teal = "#D9F0EC"
    light_gray = "#F3F4F6"
    border = "#CBD5E1"
    return {
        "title": workbook.add_format(
            {
                "font_name": "Aptos Display",
                "font_size": 20,
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": navy,
                "align": "left",
                "valign": "vcenter",
            }
        ),
        "subtitle": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_size": 10,
                "font_color": "#475569",
                "italic": True,
            }
        ),
        "section": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_size": 11,
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": teal,
                "align": "left",
                "valign": "vcenter",
            }
        ),
        "header": workbook.add_format(
            {
                "font_name": "Aptos",
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": navy,
                "align": "center",
                "valign": "vcenter",
                "border": 0,
            }
        ),
        "label": workbook.add_format(
            {
                "font_name": "Aptos",
                "bold": True,
                "font_color": "#334155",
                "bg_color": light_blue,
                "border": 1,
                "border_color": border,
                "valign": "vcenter",
            }
        ),
        "value": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_color": "#111827",
                "bg_color": "#FFFFFF",
                "border": 1,
                "border_color": border,
                "valign": "vcenter",
            }
        ),
        "value_link": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_color": "#008000",
                "bg_color": "#FFFFFF",
                "border": 1,
                "border_color": border,
                "valign": "vcenter",
            }
        ),
        "integer": workbook.add_format(
            {"font_name": "Aptos", "num_format": "#,##0", "align": "right"}
        ),
        "number": workbook.add_format(
            {"font_name": "Aptos", "num_format": "0.000", "align": "right"}
        ),
        "percent": workbook.add_format(
            {
                "font_name": "Aptos",
                "num_format": "0.0%;[Red](0.0%);-",
                "align": "right",
            }
        ),
        "percent_input": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_color": "#0000FF",
                "num_format": "0.0%;[Red](0.0%);-",
                "align": "right",
            }
        ),
        "percent_formula": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_color": "#000000",
                "num_format": "0.0%;[Red](0.0%);-",
                "align": "right",
            }
        ),
        "percent_link": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_color": "#008000",
                "num_format": "0.0%;[Red](0.0%);-",
                "align": "right",
            }
        ),
        "body": workbook.add_format({"font_name": "Aptos", "font_color": "#111827"}),
        "body_wrap": workbook.add_format(
            {"font_name": "Aptos", "font_color": "#111827", "text_wrap": True, "valign": "top"}
        ),
        "note": workbook.add_format(
            {
                "font_name": "Aptos",
                "font_color": "#475569",
                "bg_color": light_gray,
                "text_wrap": True,
                "valign": "top",
                "border": 1,
                "border_color": border,
            }
        ),
        "read": workbook.add_format(
            {
                "font_name": "Aptos",
                "bold": True,
                "font_color": "#0F172A",
                "bg_color": light_teal,
                "text_wrap": True,
                "valign": "vcenter",
                "border": 1,
                "border_color": teal,
            }
        ),
        "ok": workbook.add_format(
            {"font_name": "Aptos", "bold": True, "font_color": "#166534", "bg_color": "#DCFCE7", "align": "center"}
        ),
        "review": workbook.add_format(
            {"font_name": "Aptos", "bold": True, "font_color": "#991B1B", "bg_color": "#FEE2E2", "align": "center"}
        ),
    }


def _write_table(
    worksheet: Any,
    row_count: int,
    column_count: int,
    *,
    name: str,
    columns: tuple[str, ...],
) -> None:
    if row_count:
        worksheet.add_table(
            0,
            0,
            row_count,
            column_count - 1,
            {
                "name": name,
                "style": "Table Style Medium 2",
                "columns": [{"header": column} for column in columns],
            },
        )


def write_risk_cluster_report(
    results_path: Path,
    output_path: Path,
    qa: dict[str, Any],
) -> dict[str, Any]:
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    clusters = payload.get("cluster_summary") or []
    loadings = payload.get("factor_loadings") or []
    correlation = payload.get("residual_correlation") or {}
    tickers = correlation.get("tickers") or []
    matrix = correlation.get("matrix") or []
    model = payload.get("model") or {}
    coverage = payload.get("coverage") or {}
    diagnostics = payload.get("diagnostics") or {}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(output_path)
    formats = _formats(workbook)
    workbook.set_properties(
        {
            "title": "Residual Risk Clusters",
            "subject": "Market and sector neutral residual-correlation clusters",
            "comments": "Historical risk screen; not an expected-return model or trade instruction.",
        }
    )
    try:
        cover = workbook.add_worksheet("Cover")
        cluster_sheet = workbook.add_worksheet("Clusters")
        assignment_sheet = workbook.add_worksheet("Assignments")
        loading_sheet = workbook.add_worksheet("Loadings")
        corr_sheet = workbook.add_worksheet("Residual Corr")
        checks_sheet = workbook.add_worksheet("Checks")
        for sheet in (cover, cluster_sheet, assignment_sheet, loading_sheet, corr_sheet, checks_sheet):
            sheet.hide_gridlines(2)

        # Assignments first so cluster and cover formulas can reference stable ranges.
        assignment_sheet.freeze_panes(1, 4)
        assignment_sheet.set_row(0, 24)
        widths = (12, 24, 24, 12, 12, 12, 16, 14, 18, 18, 14, 14, 12, 18, 14, 24, 42)
        for index, width in enumerate(widths):
            assignment_sheet.set_column(index, index, width)
        assignment_sheet.write_row(0, 0, ASSIGNMENT_COLUMNS, formats["header"])
        cluster_end = len(clusters) + 1
        for row_index, row in enumerate(rows, start=1):
            excel_row = row_index + 1
            warning_text = "; ".join(row.get("warnings") or [])
            values = (
                row.get("ticker"),
                row.get("issue_name"),
                row.get("sector"),
                row.get("sector_etf"),
                row.get("cluster_id"),
                row.get("cluster_size"),
                row.get("portfolio_weight"),
            )
            for column_index, value in enumerate(values):
                cell_format = formats["percent"] if column_index == 6 else formats["body"]
                if value is None:
                    assignment_sheet.write_blank(row_index, column_index, None, cell_format)
                else:
                    assignment_sheet.write(row_index, column_index, value, cell_format)
            abs_weight = abs(float(row["portfolio_weight"])) if row.get("portfolio_weight") is not None else None
            assignment_sheet.write_formula(
                row_index,
                7,
                f'=IF(G{excel_row}="","",ABS(G{excel_row}))',
                formats["percent_formula"],
                abs_weight if abs_weight is not None else "",
            )
            multiplier = row.get("cluster_multiplier")
            multiplier_formula = (
                f'=IF(E{excel_row}="","",INDEX(\'Clusters\'!$I$2:$I${cluster_end},'
                f'MATCH(E{excel_row},\'Clusters\'!$A$2:$A${cluster_end},0)))'
            )
            assignment_sheet.write_formula(
                row_index,
                8,
                multiplier_formula,
                formats["percent_link"],
                multiplier if multiplier is not None else "",
            )
            suggested = row.get("suggested_weight")
            assignment_sheet.write_formula(
                row_index,
                9,
                f'=IF(OR(G{excel_row}="",I{excel_row}=""),"",G{excel_row}*I{excel_row})',
                formats["percent_formula"],
                suggested if suggested is not None else "",
            )
            for column_index, field in enumerate(
                ("beta_market", "beta_sector", "r_squared", "residual_volatility", "observations"),
                start=10,
            ):
                value = row.get(field)
                cell_format = formats["percent"] if field in {"r_squared", "residual_volatility"} else formats["integer"] if field == "observations" else formats["number"]
                if value is None:
                    assignment_sheet.write_blank(row_index, column_index, None, cell_format)
                else:
                    assignment_sheet.write_number(row_index, column_index, float(value), cell_format)
            assignment_sheet.write(row_index, 15, row.get("status"), formats["body"])
            assignment_sheet.write(row_index, 16, warning_text, formats["body_wrap"])
        _write_table(
            assignment_sheet,
            len(rows),
            len(ASSIGNMENT_COLUMNS),
            name="RiskClusterAssignments",
            columns=ASSIGNMENT_COLUMNS,
        )
        if rows:
            assignment_sheet.conditional_format(
                1,
                15,
                len(rows),
                15,
                {"type": "text", "criteria": "not containing", "value": "OK", "format": formats["review"]},
            )

        # Cluster summaries are formula-driven from Assignments.
        cluster_sheet.freeze_panes(1, 1)
        cluster_sheet.set_row(0, 24)
        for index, width in enumerate((12, 14, 42, 16, 16, 14, 16, 14, 18, 18)):
            cluster_sheet.set_column(index, index, width)
        cluster_sheet.write_row(0, 0, CLUSTER_COLUMNS, formats["header"])
        assignment_end = len(rows) + 1
        for row_index, summary in enumerate(clusters, start=1):
            excel_row = row_index + 1
            cluster_id = summary["cluster_id"]
            cluster_sheet.write(row_index, 0, cluster_id, formats["body"])
            cluster_sheet.write_formula(
                row_index,
                1,
                f'=COUNTIF(\'Assignments\'!$E$2:$E${assignment_end},A{excel_row})',
                formats["integer"],
                summary["member_count"],
            )
            cluster_sheet.write(row_index, 2, summary.get("members"), formats["body_wrap"])
            cluster_sheet.write_formula(
                row_index,
                3,
                f'=SUMIF(\'Assignments\'!$E$2:$E${assignment_end},A{excel_row},\'Assignments\'!$H$2:$H${assignment_end})',
                formats["percent_link"],
                summary.get("gross_weight") or 0.0,
            )
            cluster_sheet.write_formula(
                row_index,
                4,
                f'=SUMIF(\'Assignments\'!$E$2:$E${assignment_end},A{excel_row},\'Assignments\'!$G$2:$G${assignment_end})',
                formats["percent_link"],
                summary.get("net_weight") or 0.0,
            )
            cap = summary.get("cluster_cap")
            if cap is None:
                cluster_sheet.write_blank(row_index, 5, None, formats["percent_input"])
                cluster_sheet.write_blank(row_index, 6, None, formats["percent_formula"])
                cluster_sheet.write(row_index, 7, "NO CAP", formats["body"])
                cluster_sheet.write_blank(row_index, 8, None, formats["percent_formula"])
            else:
                cluster_sheet.write_number(row_index, 5, float(cap), formats["percent_input"])
                cluster_sheet.write_formula(
                    row_index,
                    6,
                    f'=ROUND(MAX(0,D{excel_row}-F{excel_row}),12)',
                    formats["percent_formula"],
                    summary.get("excess_gross") or 0.0,
                )
                breach_text = "BREACH" if summary.get("breach") else "OK"
                cluster_sheet.write_formula(
                    row_index,
                    7,
                    f'=IF(G{excel_row}>0,"BREACH","OK")',
                    formats["review"] if breach_text == "BREACH" else formats["ok"],
                    breach_text,
                )
                cluster_sheet.write_formula(
                    row_index,
                    8,
                    f'=IF(D{excel_row}=0,1,IF(G{excel_row}=0,1,MIN(1,F{excel_row}/D{excel_row})))',
                    formats["percent_formula"],
                    summary.get("max_multiplier") or 0.0,
                )
            cluster_sheet.write(row_index, 9, bool(summary.get("weight_complete")), formats["body"])
        _write_table(
            cluster_sheet,
            len(clusters),
            len(CLUSTER_COLUMNS),
            name="RiskClusterSummary",
            columns=CLUSTER_COLUMNS,
        )
        if clusters:
            cluster_sheet.conditional_format(
                1,
                7,
                len(clusters),
                7,
                {"type": "text", "criteria": "containing", "value": "BREACH", "format": formats["review"]},
            )

        # Long-form loadings.
        loading_headers = ("ticker", "factor", "coefficient")
        loading_sheet.freeze_panes(1, 0)
        loading_sheet.set_column("A:B", 16)
        loading_sheet.set_column("C:C", 18)
        loading_sheet.write_row(0, 0, loading_headers, formats["header"])
        for row_index, row in enumerate(loadings, start=1):
            loading_sheet.write(row_index, 0, row.get("ticker"), formats["body"])
            loading_sheet.write(row_index, 1, row.get("factor"), formats["body"])
            value = _finite(row.get("coefficient"))
            if value is None:
                loading_sheet.write_blank(row_index, 2, None, formats["number"])
            else:
                loading_sheet.write_number(row_index, 2, value, formats["number"])
        _write_table(loading_sheet, len(loadings), len(loading_headers), name="FactorLoadings", columns=loading_headers)

        # Residual-correlation heatmap.
        corr_sheet.freeze_panes(1, 1)
        corr_sheet.set_column(0, 0, 14)
        if tickers:
            corr_sheet.set_column(1, len(tickers), 12)
        corr_sheet.write(0, 0, "ticker", formats["header"])
        for column_index, ticker in enumerate(tickers, start=1):
            corr_sheet.write(0, column_index, ticker, formats["header"])
        for row_index, ticker in enumerate(tickers, start=1):
            corr_sheet.write(row_index, 0, ticker, formats["header"])
            for column_index, value in enumerate(matrix[row_index - 1], start=1):
                corr_sheet.write_number(row_index, column_index, float(value), formats["number"])
        if tickers:
            corr_sheet.conditional_format(
                1,
                1,
                len(tickers),
                len(tickers),
                {
                    "type": "3_color_scale",
                    "min_color": "#2563EB",
                    "mid_color": "#FFFFFF",
                    "max_color": "#DC2626",
                    "min_type": "num",
                    "min_value": -1,
                    "mid_type": "num",
                    "mid_value": 0,
                    "max_type": "num",
                    "max_value": 1,
                },
            )

        # Visible QA ledger.
        check_headers = ("check", "actual", "expected", "difference", "tolerance", "status")
        checks_sheet.set_column("A:A", 42)
        checks_sheet.set_column("B:F", 14)
        checks_sheet.write_row(0, 0, check_headers, formats["header"])
        check_items = list((qa.get("checks") or {}).items())
        for row_index, (name, passed) in enumerate(check_items, start=1):
            excel_row = row_index + 1
            checks_sheet.write(row_index, 0, name, formats["body"])
            checks_sheet.write_boolean(row_index, 1, bool(passed), formats["body"])
            checks_sheet.write_boolean(row_index, 2, True, formats["body"])
            checks_sheet.write_formula(
                row_index,
                3,
                f'=--(B{excel_row}<>C{excel_row})',
                formats["integer"],
                0 if passed else 1,
            )
            checks_sheet.write_number(row_index, 4, 0, formats["integer"])
            status = "OK" if passed else "REVIEW"
            checks_sheet.write_formula(
                row_index,
                5,
                f'=IF(D{excel_row}<=E{excel_row},"OK","REVIEW")',
                formats["ok"] if passed else formats["review"],
                status,
            )
        _write_table(checks_sheet, len(check_items), len(check_headers), name="RiskClusterChecks", columns=check_headers)

        # First-read cover / insight dashboard.
        cover.set_column("A:A", 23)
        cover.set_column("B:B", 24)
        cover.set_column("C:C", 3)
        cover.set_column("D:D", 25)
        cover.set_column("E:E", 18)
        cover.set_column("F:H", 14)
        cover.set_column("I:I", 3)
        cover.set_column("J:R", 12)
        cover.set_row(0, 34)
        cover.merge_range("A1:H1", "Residual Risk Clusters", formats["title"])
        cover.write("A2", "Historical shared-risk screen after market and sector neutralization", formats["subtitle"])
        cover.merge_range("A4:B4", "Run status", formats["section"])
        cover.merge_range("D4:E4", "Risk snapshot", formats["section"])
        status = "OK" if qa.get("pass") else "REVIEW"
        checks_end = len(check_items) + 1
        cover.write("A5", "Model status", formats["label"])
        cover.write_formula(
            "B5",
            f'=IF(COUNTIF(\'Checks\'!$F$2:$F${checks_end},"REVIEW")>0,"REVIEW","OK")',
            formats["ok"] if status == "OK" else formats["review"],
            status,
        )
        for excel_row, label, value in (
            (6, "As of", payload.get("as_of")),
            (7, "Market factor", model.get("market")),
            (8, "Lookback / minimum", f'{model.get("lookback_days")} / {model.get("min_observations")} days'),
            (9, "Data mode", (payload.get("data_source") or {}).get("mode")),
        ):
            cover.write(excel_row - 1, 0, label, formats["label"])
            cover.write(excel_row - 1, 1, value, formats["value"])
        assignment_status_column = "P"
        cover.write("D5", "Eligible stocks", formats["label"])
        cover.write_formula(
            "E5",
            f'=COUNTIF(\'Assignments\'!${assignment_status_column}$2:${assignment_status_column}${assignment_end},"OK")+'
            f'COUNTIF(\'Assignments\'!${assignment_status_column}$2:${assignment_status_column}${assignment_end},"OK_MARKET_ONLY")',
            formats["value_link"],
            coverage.get("eligible_rows") or 0,
        )
        cover.write("D6", "Clusters", formats["label"])
        cover.write_formula(
            "E6",
            f'=COUNTA(\'Clusters\'!$A$2:$A${cluster_end})',
            formats["value_link"],
            len(clusters),
        )
        breach_count = sum(summary.get("breach") is True for summary in clusters)
        cover.write("D7", "Cap breaches", formats["label"])
        cover.write_formula(
            "E7",
            f'=COUNTIF(\'Clusters\'!$H$2:$H${cluster_end},"BREACH")',
            formats["value_link"],
            breach_count,
        )
        largest_gross = max((_finite(summary.get("gross_weight")) or 0.0 for summary in clusters), default=0.0)
        cover.write("D8", "Largest gross cluster", formats["label"])
        cover.write_formula(
            "E8",
            f'=MAX(\'Clusters\'!$D$2:$D${cluster_end})',
            formats["percent_link"],
            largest_gross,
        )
        cover.write("D9", "Residual mean corr.", formats["label"])
        residual_mean = _finite(diagnostics.get("residual_mean_pairwise_correlation"))
        cover.write_number("E9", residual_mean or 0.0, formats["percent"])

        cover.merge_range("A11:H11", "Decision read", formats["section"])
        if breach_count:
            read = (
                f"{breach_count} cluster cap breach{'es' if breach_count != 1 else ''} detected. "
                "Review the Clusters sheet and the proportional scale-down illustration before changing positions."
            )
        else:
            read = "No configured cluster cap breach is present in this run. Continue to monitor cluster membership and correlation drift."
        cover.merge_range("A12:H14", read, formats["read"])
        cover.merge_range("A16:H16", "Use and limitations", formats["section"])
        limitation = (
            "Use this workbook to identify historically shared residual risk and cluster-cap pressure. "
            "Do not use it as an expected-return ranking, a complete factor model, an automated rebalance, "
            "or a substitute for liquidity, event-gap, borrow, thesis, and implementation checks. "
            "Sector ETF proxies carry basis risk, especially outside large-cap U.S. equities."
        )
        cover.merge_range("A17:H20", limitation, formats["note"])
        cover.write_comment("A17", "Residual correlations are historical and regime-dependent. Cap outputs are analytical illustrations, not trade instructions.")
        cover.merge_range("A22:E22", "Workbook map", formats["section"])
        workbook_map = (
            ("Clusters", "Cluster exposure, cap breach, and scale multiplier"),
            ("Assignments", "Stock membership, weights, loadings, and residual volatility"),
            ("Loadings", "Long-form market, sector, and optional style coefficients"),
            ("Residual Corr", "Projected residual-correlation matrix and heatmap"),
            ("Checks", "Deterministic structural and reconciliation checks"),
        )
        for offset, (sheet_name, description) in enumerate(workbook_map, start=23):
            cover.write(offset - 1, 0, sheet_name, formats["label"])
            cover.merge_range(offset - 1, 1, offset - 1, 4, description, formats["value"])

        if clusters:
            chart = workbook.add_chart({"type": "column"})
            chart.add_series(
                {
                    "name": "Gross weight",
                    "categories": f"='Clusters'!$A$2:$A${cluster_end}",
                    "values": f"='Clusters'!$D$2:$D${cluster_end}",
                    "fill": {"color": "#0F766E"},
                }
            )
            if model.get("cluster_cap") is not None:
                chart.add_series(
                    {
                        "name": "Cluster cap",
                        "categories": f"='Clusters'!$A$2:$A${cluster_end}",
                        "values": f"='Clusters'!$F$2:$F${cluster_end}",
                        "fill": {"color": "#94A3B8"},
                    }
                )
            chart.set_title({"name": "Gross cluster exposure vs cap"})
            chart.set_y_axis({"num_format": "0%", "major_gridlines": {"visible": True, "line": {"color": "#E2E8F0"}}})
            chart.set_legend({"position": "bottom"})
            chart.set_style(10)
            cover.insert_chart("J4", chart, {"x_scale": 1.2, "y_scale": 1.15})

        cover.activate()
        cover.set_tab_color("#17365D")
        cluster_sheet.set_tab_color("#0F766E")
        checks_sheet.set_tab_color("#DC2626" if not qa.get("pass") else "#16A34A")
    finally:
        workbook.close()

    return {
        "path": str(output_path),
        "sheets": ["Cover", "Clusters", "Assignments", "Loadings", "Residual Corr", "Checks"],
        "assignment_rows": len(rows),
        "cluster_rows": len(clusters),
        "qa_pass": bool(qa.get("pass")),
    }
