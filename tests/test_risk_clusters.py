from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd

from factors_model.baselines import ConfigError, build_command, fingerprint_inputs, load_baseline
from factors_model.risk_cluster_report import write_risk_cluster_report
from factors_model.risk_clusters import (
    RiskClusterSettings,
    analyze_risk_clusters,
    load_returns_panel,
    load_risk_universe,
    nearest_correlation,
)
from factors_model.runner import is_frozen_baseline_run, run_baseline
from factors_model.validation import compare_risk_clusters, qa_risk_clusters


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "configs" / "residual_risk_clusters_v1.toml"
UNIVERSE_PATH = REPO_ROOT / "data" / "residual_risk" / "frozen_universe_v1.csv"
RETURNS_PATH = REPO_ROOT / "data" / "residual_risk" / "frozen_returns_v1.csv"
EXPECTED_PATH = REPO_ROOT / "baselines" / "residual_risk_clusters_v1" / "results.json"
SPREADSHEET_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class RiskClusterEngineTests(unittest.TestCase):
    def _payload(self, *, styles: tuple[str, ...] = ()) -> dict:
        universe = load_risk_universe(UNIVERSE_PATH)
        returns, source = load_returns_panel(RETURNS_PATH, None)
        return analyze_risk_clusters(
            universe,
            returns,
            RiskClusterSettings(
                as_of="2026-08-26",
                market="VTI",
                lookback_days=504,
                min_observations=252,
                min_pair_observations=126,
                residual_correlation_threshold=0.35,
                cluster_cap=0.15,
                style_factors=styles,
            ),
            data_source=source,
        )

    def test_market_sector_residuals_recover_known_clusters_and_cap_breach(self) -> None:
        payload = self._payload()
        assignments = {row["ticker"]: row for row in payload["rows"]}
        self.assertEqual(assignments["AAA"]["cluster_id"], assignments["AAB"]["cluster_id"])
        self.assertEqual(assignments["BBA"]["cluster_id"], assignments["BBB"]["cluster_id"])
        self.assertNotEqual(assignments["AAA"]["cluster_id"], assignments["BBA"]["cluster_id"])
        self.assertEqual(assignments["DDD"]["status"], "OK_MARKET_ONLY")
        self.assertLess(
            payload["diagnostics"]["residual_mean_pairwise_correlation"],
            payload["diagnostics"]["raw_mean_pairwise_correlation"],
        )
        breaches = [row for row in payload["cluster_summary"] if row["breach"]]
        self.assertEqual([row["members"] for row in breaches], ["BBA, BBB"])
        self.assertAlmostEqual(breaches[0]["max_multiplier"], 0.15 / 0.17)

    def test_optional_style_factors_are_orthogonalized_and_reported(self) -> None:
        payload = self._payload(styles=("VLUE", "MTUM", "USMV", "SPHQ"))
        factors = {row["factor"] for row in payload["factor_loadings"]}
        self.assertTrue({"VTI", "VLUE", "MTUM", "USMV", "SPHQ"}.issubset(factors))
        self.assertEqual(payload["model"]["style_factors"], ["VLUE", "MTUM", "USMV", "SPHQ"])
        self.assertTrue(all(row["cluster_id"] for row in payload["rows"]))

    def test_short_side_is_signed_and_portfolio_weights_remain_local(self) -> None:
        rows = load_risk_universe(UNIVERSE_PATH)
        by_ticker = {row["ticker"]: row for row in rows}
        self.assertEqual(by_ticker["DDD"]["portfolio_weight"], -0.04)
        self.assertEqual(by_ticker["DDD"]["side"], "short")

    def test_nearest_correlation_handles_missing_pair_values(self) -> None:
        raw = pd.DataFrame(
            [[1.0, 0.8, float("nan")], [0.8, 1.0, 0.2], [float("nan"), 0.2, 1.0]],
            index=["A", "B", "C"],
            columns=["A", "B", "C"],
        )
        projected, adjustment, missing = nearest_correlation(raw)
        self.assertEqual(missing, 1)
        self.assertGreaterEqual(adjustment, 0.0)
        self.assertTrue(projected.notna().all().all())
        self.assertTrue((projected.to_numpy().diagonal() == 1.0).all())


class RiskClusterPipelineTests(unittest.TestCase):
    def test_config_builds_private_safe_frozen_command(self) -> None:
        config = load_baseline(CONFIG_PATH, "residual_risk_clusters")
        command = build_command(config, {"frozen_baseline": True}, Path("/tmp/risk-output"))
        self.assertIn("--returns", command)
        self.assertNotIn("--fetch-prices", command)
        self.assertNotIn("--fetch-sector-metadata", command)
        self.assertEqual(Path(command[command.index("--universe") + 1]), UNIVERSE_PATH)
        self.assertEqual(Path(command[command.index("--returns") + 1]), RETURNS_PATH)
        self.assertTrue(is_frozen_baseline_run(config, {"frozen_baseline": True}))
        fingerprints = fingerprint_inputs(config, {"frozen_baseline": True})
        self.assertEqual({item["role"] for item in fingerprints}, {"universe_fixture", "returns_fixture"})

    def test_live_command_requires_explicit_market_data_route(self) -> None:
        config = load_baseline(CONFIG_PATH, "residual_risk_clusters")
        with self.assertRaisesRegex(ConfigError, "exactly one"):
            build_command(config, {}, Path("/tmp/risk-output"))
        command = build_command(
            config,
            {"returns": str(RETURNS_PATH)},
            Path("/tmp/risk-output"),
        )
        self.assertIn("--returns", command)

    def test_qa_comparison_and_workbook_are_valid(self) -> None:
        qa = qa_risk_clusters(EXPECTED_PATH)
        comparison = compare_risk_clusters(EXPECTED_PATH, EXPECTED_PATH, {})
        self.assertTrue(qa["pass"])
        self.assertTrue(comparison["pass"])
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "residual_risk_clusters.xlsx"
            summary = write_risk_cluster_report(EXPECTED_PATH, report_path, qa)
            self.assertTrue(report_path.is_file())
            self.assertEqual(summary["sheets"][0], "Cover")
            with zipfile.ZipFile(report_path) as archive:
                self.assertIsNone(archive.testzip())
                workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
                sheets = workbook.find("main:sheets", SPREADSHEET_NS)
                self.assertIsNotNone(sheets)
                self.assertEqual([sheet.attrib["name"] for sheet in sheets], summary["sheets"])
                formulas = b"".join(
                    archive.read(name)
                    for name in archive.namelist()
                    if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                )
                self.assertIn(b"SUMIF", formulas)
                self.assertIn(b"INDEX", formulas)

    def test_frozen_pipeline_writes_verified_artifacts(self) -> None:
        config = load_baseline(CONFIG_PATH, "residual_risk_clusters")
        with tempfile.TemporaryDirectory() as directory:
            result = run_baseline(
                config,
                {"frozen_baseline": True, "output_dir": directory},
            )
            self.assertTrue(Path(result["excel_report"]).is_file())
            self.assertTrue(Path(result["qa"]).is_file())
            self.assertTrue(Path(result["regression"]).is_file())
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "success")
            self.assertTrue(manifest["qa_pass"])
            self.assertEqual(manifest["regression_status"], "pass")


if __name__ == "__main__":
    unittest.main()
