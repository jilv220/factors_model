from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import xlsxwriter


BAD_BETA_REPORT_COLUMNS = ("ticker", "beta", "bad_beta", "grid_cell")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def write_bad_beta_report(results_path: Path, output_path: Path) -> dict[str, Any]:
    """Write the compact bad-beta Excel run artifact from results.json."""
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    universe = payload.get("universe")
    if not isinstance(universe, list) or not universe:
        raise ValueError(f"bad-beta results contain no universe rows: {results_path}")

    rows = []
    for row in universe:
        if not isinstance(row, dict):
            raise ValueError(f"bad-beta universe contains a non-object row: {results_path}")
        rows.append(
            (
                str(row.get("ticker") or ""),
                _finite_number(row.get("beta")),
                _finite_number(row.get("bad_beta")),
                str(row.get("grid_cell") or ""),
            )
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(output_path)
    try:
        worksheet = workbook.add_worksheet("Bad Beta")
        worksheet.hide_gridlines(2)
        worksheet.freeze_panes(1, 0)
        worksheet.set_column("A:A", 14)
        worksheet.set_column("B:C", 15)
        worksheet.set_column("D:D", 34)
        worksheet.set_row(0, 22)

        header_format = workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#17365D",
                "align": "center",
                "valign": "vcenter",
                "border": 0,
            }
        )
        ticker_format = workbook.add_format({"align": "left", "valign": "vcenter"})
        number_format = workbook.add_format(
            {
                "align": "right",
                "valign": "vcenter",
                "num_format": "0.000000;[Red](0.000000);-",
            }
        )
        text_format = workbook.add_format({"align": "left", "valign": "vcenter"})

        worksheet.write_row(0, 0, BAD_BETA_REPORT_COLUMNS, header_format)
        for row_number, (ticker, beta, bad_beta, grid_cell) in enumerate(rows, start=1):
            worksheet.write_string(row_number, 0, ticker, ticker_format)
            if beta is None:
                worksheet.write_blank(row_number, 1, None, number_format)
            else:
                worksheet.write_number(row_number, 1, beta, number_format)
            if bad_beta is None:
                worksheet.write_blank(row_number, 2, None, number_format)
            else:
                worksheet.write_number(row_number, 2, bad_beta, number_format)
            worksheet.write_string(row_number, 3, grid_cell, text_format)

        worksheet.add_table(
            0,
            0,
            len(rows),
            len(BAD_BETA_REPORT_COLUMNS) - 1,
            {
                "name": "BadBetaTable",
                "style": "Table Style Medium 2",
                "columns": [{"header": column} for column in BAD_BETA_REPORT_COLUMNS],
            },
        )
    finally:
        workbook.close()

    complete_rows = sum(
        beta is not None and bad_beta is not None and bool(grid_cell)
        for _, beta, bad_beta, grid_cell in rows
    )
    return {
        "path": str(output_path),
        "columns": list(BAD_BETA_REPORT_COLUMNS),
        "rows": len(rows),
        "complete_rows": complete_rows,
        "incomplete_rows": len(rows) - complete_rows,
    }
