from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

from factors_model.baselines import (
    ConfigError,
    build_command,
    default_output_dir,
    fingerprint_inputs,
    load_baseline,
)
from factors_model.excel_reports import BAD_BETA_REPORT_COLUMNS, write_bad_beta_report
from factors_model.fundamentals import load_universe
from factors_model.runner import is_frozen_baseline_run, run_baseline
from factors_model.validation import (
    compare_bad_beta,
    compare_six_factor,
    qa_bad_beta,
    qa_six_factor,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SPREADSHEET_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class BaselineConfigTests(unittest.TestCase):
    def _write_live_six_factor_config(self, directory: str) -> Path:
        config_path = Path(directory) / "six_factor_live.toml"
        config_path.write_text(
            f'''baseline_version = "six_factor_ranking_test"
pipeline = "six_factor_ranking"
project_root = "{REPO_ROOT}"
python = "{REPO_ROOT / '.venv' / 'bin' / 'python'}"
script = "{REPO_ROOT / 'scripts' / 'rank_six_factor_model.py'}"
output_root = "{Path(directory) / 'runs'}"

[inputs]
universe = "data/universes/2026-06-16"

[model]
as_of = "2026-08-26"
factor_as_of = "2026-08-26"
method_id = "low_beta_low_bad_beta_six_factor_v1"

[model.weights]
quality = 0.25
fundamental_momentum = 0.25
analyst_revisions = 0.16666666666666666
valuation = 0.1111111111111111
conservative_investment = 0.1111111111111111
shareholder_yield = 0.1111111111111111

[regression]
expected_output = "baselines/six_factor_ranking_v1/results.json"
''',
            encoding="utf-8",
        )
        return config_path

    def test_configs_resolve_code_inputs_and_baselines_inside_repository(self) -> None:
        for name in (
            "bad_beta_v1.toml",
            "six_factor_ranking_v1.toml",
            "residual_risk_clusters_v1.toml",
        ):
            config = load_baseline(REPO_ROOT / "configs" / name)
            self.assertEqual(config.project_root(), REPO_ROOT)
            self.assertTrue(config.script_path().is_relative_to(REPO_ROOT))
            self.assertTrue(config.expected_output().is_relative_to(REPO_ROOT))
            for value in config.data["inputs"].values():
                self.assertTrue(config.project_path(str(value)).is_relative_to(REPO_ROOT))
            for value in config.data.get("fixtures", {}).values():
                if isinstance(value, str) and not value[:4].isdigit():
                    self.assertTrue(config.project_path(value).is_relative_to(REPO_ROOT))

    def test_bad_beta_command_preserves_baseline_parameters(self) -> None:
        config = load_baseline(REPO_ROOT / "configs" / "bad_beta_v1.toml", "bad_beta")
        output_dir = default_output_dir(config, "2026-07-10")
        command = build_command(config, {}, output_dir)
        self.assertIn("--window-months", command)
        self.assertEqual(command[command.index("--window-months") + 1], "36")
        self.assertEqual(command[command.index("--min-months") + 1], "24")
        self.assertEqual(command[command.index("--rho") + 1], "0.95")
        self.assertIn("--universe", command)
        self.assertIn("--cape-csv", command)
        self.assertIn("--cape-xls", command)
        self.assertNotIn("--holdings", command)
        self.assertNotIn("--total-notional", command)
        self.assertEqual(Path(command[0]), REPO_ROOT / ".venv" / "bin" / "python")
        self.assertEqual(config.script_path(), REPO_ROOT / "scripts" / "estimate_bad_beta.py")
        self.assertEqual(
            Path(command[command.index("--universe") + 1]),
            REPO_ROOT / "data" / "universes" / "2026-06-16",
        )

    def test_six_factor_command_preserves_model_definition(self) -> None:
        config = load_baseline(
            REPO_ROOT / "configs" / "six_factor_ranking_v1.toml", "six_factor_ranking"
        )
        output_dir = default_output_dir(config, "2026-08-09")
        command = build_command(config, {}, output_dir)
        self.assertEqual(command[command.index("--as-of") + 1], "2026-08-26")
        self.assertEqual(command[command.index("--factor-as-of") + 1], "2026-08-26")
        self.assertIn("--universe", command)
        self.assertNotIn("--factor-input", command)
        self.assertNotIn("--revisions", command)
        self.assertIn("--refresh-revisions", command)
        self.assertEqual(command[command.index("--weight-quality") + 1], "0.25")
        self.assertEqual(command[command.index("--weight-fundamental-momentum") + 1], "0.25")
        self.assertEqual(
            command[command.index("--weight-analyst-revisions") + 1], "0.16666666666666666"
        )
        self.assertEqual(config.script_path(), REPO_ROOT / "scripts" / "rank_six_factor_model.py")
        self.assertEqual(Path(command[0]), REPO_ROOT / ".venv" / "bin" / "python")
        self.assertEqual(
            Path(command[command.index("--universe") + 1]),
            REPO_ROOT / "data" / "universes" / "2026-06-16",
        )

    def test_six_factor_frozen_fixture_is_explicit(self) -> None:
        config = load_baseline(
            REPO_ROOT / "configs" / "six_factor_ranking_v1.toml", "six_factor_ranking"
        )
        output_dir = default_output_dir(config, "2026-08-09")
        command = build_command(config, {"frozen_baseline": True}, output_dir)
        self.assertIn("--factor-input", command)
        self.assertIn("--revisions", command)
        self.assertNotIn("--universe", command)
        self.assertNotIn("--refresh-revisions", command)
        self.assertEqual(command[command.index("--as-of") + 1], "2026-08-09")
        self.assertEqual(command[command.index("--factor-as-of") + 1], "2026-07-10")
        self.assertEqual(command[command.index("--revision-as-of") + 1], "2026-08-09")
        self.assertTrue(is_frozen_baseline_run(config, {"frozen_baseline": True}))

    def test_six_factor_live_config_does_not_require_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_baseline(
                self._write_live_six_factor_config(directory),
                "six_factor_ranking",
            )
            command = build_command(config, {}, Path(directory) / "output")
            fingerprints = fingerprint_inputs(config, {})

        self.assertIn("--universe", command)
        self.assertNotIn("--factor-input", command)
        self.assertTrue(fingerprints)
        self.assertTrue(all(item["role"] == "universe" for item in fingerprints))

    def test_six_factor_frozen_run_requires_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_baseline(
                self._write_live_six_factor_config(directory),
                "six_factor_ranking",
            )
            with self.assertRaisesRegex(ConfigError, r"\[fixtures\].*--frozen-baseline"):
                build_command(
                    config,
                    {"frozen_baseline": True},
                    Path(directory) / "output",
                )

    def test_six_factor_universe_csv_uses_only_public_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "selection.csv"
            universe.write_text(
                "Ticker,Company,Sector,quantity,weight,side\nAAA,Alpha,Tech,100,0.5,long\nBBB,Beta,Energy,20,0.2,short\n",
                encoding="utf-8",
            )
            rows = load_universe(universe)
        self.assertEqual([row["ticker"] for row in rows], ["AAA", "BBB"])
        self.assertEqual(rows[0]["issue_name"], "Alpha")
        self.assertNotIn("quantity", rows[0])
        self.assertNotIn("weight", rows[0])
        self.assertNotIn("side", rows[0])

    def test_material_override_disables_frozen_regression(self) -> None:
        config = load_baseline(REPO_ROOT / "configs" / "bad_beta_v1.toml", "bad_beta")
        self.assertTrue(is_frozen_baseline_run(config, {}))
        self.assertFalse(is_frozen_baseline_run(config, {"as_of": "2026-08-25"}))


class ValidationTests(unittest.TestCase):
    def test_bad_beta_run_writes_excel_artifact_and_manifest(self) -> None:
        payload = {
            "universe": [
                {
                    "ticker": "AAA",
                    "status": "OK",
                    "beta": 0.5,
                    "bad_beta": 0.2,
                    "grid_cell": "Low Beta / Low Bad Beta",
                }
            ],
            "breakpoints": {
                "classified_count": 1,
                "beta_q1": 0.4,
                "beta_q2": 0.8,
                "bad_beta_q1": 0.1,
                "bad_beta_q2": 0.4,
            },
            "cell_summary": [{"grid_cell": "Low Beta / Low Bad Beta", "count": 1}],
            "yahoo_errors": {},
        }
        config = load_baseline(REPO_ROOT / "configs" / "bad_beta_v1.toml", "bad_beta")

        def fake_pipeline(command, **_kwargs):
            output_path = Path(command[command.index("--output") + 1])
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="completed", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            with patch("factors_model.runner.subprocess", SimpleNamespace(run=fake_pipeline)):
                result = run_baseline(
                    config,
                    {
                        "output_dir": directory,
                        "as_of": "2026-08-25",
                    },
                )

            report_path = Path(result["excel_report"])
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertTrue(report_path.is_file())
            self.assertEqual(manifest["excel_report"]["path"], str(report_path))
            self.assertEqual(
                manifest["excel_report"]["columns"],
                list(BAD_BETA_REPORT_COLUMNS),
            )
            self.assertEqual(manifest["excel_report"]["rows"], 1)
            self.assertEqual(len(manifest["excel_report"]["sha256"]), 64)

    def test_bad_beta_excel_report_contains_only_requested_columns(self) -> None:
        payload = {
            "universe": [
                {
                    "ticker": "AAA",
                    "status": "OK",
                    "beta": 0.5,
                    "bad_beta": 0.2,
                    "grid_cell": "Low Beta / Low Bad Beta",
                    "issue_name": "Alpha",
                },
                {
                    "ticker": "NEW",
                    "status": "Insufficient return history",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            report_path = Path(directory) / "bad_beta_analysis.xlsx"
            results_path.write_text(json.dumps(payload), encoding="utf-8")

            summary = write_bad_beta_report(results_path, report_path)

            self.assertEqual(summary["columns"], list(BAD_BETA_REPORT_COLUMNS))
            self.assertEqual(summary["rows"], 2)
            self.assertEqual(summary["complete_rows"], 1)
            self.assertTrue(report_path.is_file())
            with zipfile.ZipFile(report_path) as archive:
                self.assertIsNone(archive.testzip())
                table = ElementTree.fromstring(archive.read("xl/tables/table1.xml"))
                columns = table.find("main:tableColumns", SPREADSHEET_NS)
                self.assertIsNotNone(columns)
                self.assertEqual(
                    [column.attrib["name"] for column in columns],
                    list(BAD_BETA_REPORT_COLUMNS),
                )
                self.assertEqual(table.attrib["ref"], "A1:D3")

    def test_bad_beta_qa_and_comparison(self) -> None:
        payload = {
            "universe": [
                {
                    "ticker": "AAA",
                    "status": "OK",
                    "beta": 0.5,
                    "bad_beta": 0.2,
                    "grid_cell": "Low Beta / Low Bad Beta",
                }
            ],
            "breakpoints": {
                "classified_count": 1,
                "beta_q1": 0.4,
                "beta_q2": 0.8,
                "bad_beta_q1": 0.1,
                "bad_beta_q2": 0.4,
            },
            "cell_summary": [{"grid_cell": "Low Beta / Low Bad Beta", "count": 1}],
            "yahoo_errors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "expected.json"
            actual = Path(directory) / "actual.json"
            expected.write_text(json.dumps(payload), encoding="utf-8")
            actual.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(qa_bad_beta(actual)["pass"])
            self.assertTrue(compare_bad_beta(expected, actual, {})["pass"])

    def test_bad_beta_allows_tiny_numeric_drift_but_not_grid_changes(self) -> None:
        expected_payload = {
            "universe": [
                {
                    "ticker": "AAA",
                    "status": "OK",
                    "beta": 0.5,
                    "bad_beta": 0.2,
                    "grid_cell": "Low Beta / Low Bad Beta",
                }
            ]
        }
        actual_payload = json.loads(json.dumps(expected_payload))
        actual_payload["universe"][0]["beta"] += 5e-6
        actual_payload["universe"][0]["bad_beta"] += 5e-6
        regression = {
            "beta_abs_tolerance": 1e-5,
            "bad_beta_abs_tolerance": 1e-5,
            "allowed_grid_mismatches": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "expected.json"
            actual = Path(directory) / "actual.json"
            expected.write_text(json.dumps(expected_payload), encoding="utf-8")
            actual.write_text(json.dumps(actual_payload), encoding="utf-8")
            self.assertTrue(compare_bad_beta(expected, actual, regression)["pass"])

            actual_payload["universe"][0]["grid_cell"] = "High Beta / High Bad Beta"
            actual.write_text(json.dumps(actual_payload), encoding="utf-8")
            self.assertFalse(compare_bad_beta(expected, actual, regression)["pass"])

    def test_six_factor_qa_and_comparison(self) -> None:
        weights = {
            "quality": 0.25,
            "fundamental_momentum": 0.25,
            "analyst_revisions": 1 / 6,
            "valuation": 1 / 9,
            "conservative_investment": 1 / 9,
            "shareholder_yield": 1 / 9,
        }
        score_weights = {f"{key}_score": value for key, value in weights.items()}
        rows = []
        for rank, ticker, base in ((1, "AAA", 8.0), (2, "BBB", 6.0)):
            row = {
                "overall_rank": rank,
                "ticker": ticker,
                "factor_coverage": 6,
                **{field: base for field in score_weights},
            }
            row["overall_score"] = sum(row[field] * weight for field, weight in score_weights.items())
            rows.append(row)
        payload = {
            "model_id": "low_beta_low_bad_beta_six_factor_v1",
            "factor_names": list(weights),
            "weights": score_weights,
            "coverage": {
                "universe_rows": 2,
                "six_factor_rows": 2,
                "valid_revision_rows": 2,
            },
            "rows": rows,
        }
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "expected.json"
            actual = Path(directory) / "actual.json"
            expected.write_text(json.dumps(payload), encoding="utf-8")
            actual.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(qa_six_factor(actual, weights)["pass"])
            self.assertTrue(compare_six_factor(expected, actual, {})["pass"])


if __name__ == "__main__":
    unittest.main()
