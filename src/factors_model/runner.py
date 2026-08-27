from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .baselines import (
    BaselineConfig,
    build_command,
    default_output_dir,
    fingerprint_inputs,
    git_revision,
    json_text,
    sha256_file,
)
from .excel_reports import write_bad_beta_report
from .risk_cluster_report import write_risk_cluster_report
from .validation import qa_bad_beta, qa_risk_clusters, qa_six_factor, verify_baseline


class RunError(RuntimeError):
    """Raised when a configured pipeline does not complete successfully."""


def _as_of(config: BaselineConfig, options: dict[str, Any]) -> str:
    if config.pipeline in {"six_factor_ranking", "residual_risk_clusters"} and options.get("frozen_baseline"):
        return str(config.data["fixtures"]["as_of"])
    return str(options.get("as_of") or config.data["model"]["as_of"])


def _output_dir(config: BaselineConfig, options: dict[str, Any]) -> Path:
    if options.get("output_dir"):
        return Path(str(options["output_dir"])).expanduser().resolve()
    return default_output_dir(config, _as_of(config, options)).resolve()


def dry_run(config: BaselineConfig, options: dict[str, Any]) -> dict[str, Any]:
    output_dir = _output_dir(config, options)
    command = build_command(config, options, output_dir)
    return {
        "baseline_version": config.version,
        "pipeline": config.pipeline,
        "config": str(config.path),
        "project_root": str(config.project_root(options.get("project_root"))),
        "output_dir": str(output_dir),
        "command": command,
    }


def _prepare_output_dir(path: Path, force: bool) -> None:
    if path.exists() and any(path.iterdir()) and not force:
        raise RunError(f"output directory is not empty: {path}; pass --force to overwrite known outputs")
    path.mkdir(parents=True, exist_ok=True)


def is_frozen_baseline_run(config: BaselineConfig, options: dict[str, Any]) -> bool:
    if config.pipeline == "bad_beta":
        material_overrides = (
            "project_root",
            "universe",
            "universe_as_of",
            "start",
            "as_of",
            "rho",
            "window_months",
            "min_months",
        )
    elif config.pipeline == "six_factor_ranking":
        if not options.get("frozen_baseline"):
            return False
        material_overrides = (
            "project_root",
            "universe",
            "factor_input",
            "revisions",
            "as_of",
            "factor_as_of",
            "revision_as_of",
            "refresh_revisions",
        )
    else:
        if not options.get("frozen_baseline"):
            return False
        material_overrides = (
            "project_root",
            "universe",
            "returns",
            "prices",
            "allow_external_ticker_lookup",
            "fetch_sector_metadata",
            "sector_map",
            "weights_column",
            "as_of",
            "market",
            "style_factors",
            "lookback_days",
            "min_observations",
            "min_pair_observations",
            "residual_correlation_threshold",
            "cluster_cap",
        )
    return not any(options.get(key) not in (None, False) for key in material_overrides)


def run_baseline(config: BaselineConfig, options: dict[str, Any]) -> dict[str, Any]:
    output_dir = _output_dir(config, options)
    _prepare_output_dir(output_dir, bool(options.get("force")))
    command = build_command(config, options, output_dir)
    project_root = config.project_root(options.get("project_root"))
    started = datetime.now(timezone.utc)
    result = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
    finished = datetime.now(timezone.utc)
    log_path = output_dir / "run.log"
    log_path.write_text(
        "COMMAND\n" + " ".join(command) + "\n\nSTDOUT\n" + result.stdout + "\nSTDERR\n" + result.stderr,
        encoding="utf-8",
    )
    expected = config.expected_output(project_root)
    manifest: dict[str, Any] = {
        "baseline_version": config.version,
        "pipeline": config.pipeline,
        "status": "success" if result.returncode == 0 else "failed",
        "return_code": result.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "config_path": str(config.path),
        "config_sha256": sha256_file(config.path),
        "project_root": str(project_root),
        "project_git_revision": git_revision(project_root),
        "pipeline_script_sha256": sha256_file(config.script_path(project_root)),
        "cli_git_revision": git_revision(Path(__file__).resolve().parents[2]),
        "cli_source_sha256": _cli_source_sha256(),
        "command": command,
        "inputs": fingerprint_inputs(config, options),
        "expected_snapshot": {
            "path": str(expected),
            "sha256": sha256_file(expected) if expected.is_file() else None,
        },
        "output_dir": str(output_dir),
    }
    manifest_path = output_dir / "run_manifest.json"
    manifest_path.write_text(json_text(manifest), encoding="utf-8")
    if result.returncode != 0:
        raise RunError(f"{config.pipeline} failed with exit code {result.returncode}; see {log_path}")

    if config.pipeline == "bad_beta":
        primary_output = output_dir / "results.json"
        excel_report_path = output_dir / "bad_beta_analysis.xlsx"
        try:
            excel_report = write_bad_beta_report(primary_output, excel_report_path)
        except Exception as exc:
            manifest["status"] = "failed_excel_report"
            manifest["excel_report_error"] = repr(exc)
            manifest_path.write_text(json_text(manifest), encoding="utf-8")
            raise RunError(f"bad_beta completed but Excel report generation failed: {exc}") from exc
        qa = qa_bad_beta(primary_output)
    elif config.pipeline == "six_factor_ranking":
        primary_output = output_dir / "results.json"
        excel_report_path = None
        excel_report = None
        qa = qa_six_factor(primary_output, config.data["model"]["weights"])
    else:
        primary_output = output_dir / "results.json"
        qa = qa_risk_clusters(primary_output)
        excel_report_path = output_dir / "residual_risk_clusters.xlsx"
        try:
            excel_report = write_risk_cluster_report(primary_output, excel_report_path, qa)
        except Exception as exc:
            manifest["status"] = "failed_excel_report"
            manifest["excel_report_error"] = repr(exc)
            manifest_path.write_text(json_text(manifest), encoding="utf-8")
            raise RunError(f"risk clusters completed but Excel report generation failed: {exc}") from exc
    qa_path = output_dir / "qa.json"
    qa_path.write_text(json_text(qa), encoding="utf-8")
    manifest["qa_pass"] = qa["pass"]
    manifest["primary_output"] = str(primary_output)
    manifest["primary_output_sha256"] = sha256_file(primary_output)
    if excel_report_path is not None and excel_report is not None:
        manifest["excel_report"] = {
            **excel_report,
            "sha256": sha256_file(excel_report_path),
        }
    manifest_path.write_text(json_text(manifest), encoding="utf-8")
    regression_path: Path | None = None
    if is_frozen_baseline_run(config, options):
        regression = verify_baseline(config, primary_output)
        regression_path = output_dir / "regression.json"
        regression_path.write_text(json_text(regression), encoding="utf-8")
        manifest["regression_status"] = "pass" if regression["pass"] else "fail"
        manifest["regression_report"] = str(regression_path)
    else:
        regression = None
        manifest["regression_status"] = "not_run_custom_overrides"
    if not qa["pass"]:
        manifest["status"] = "failed_qa"
    elif regression is not None and not regression["pass"]:
        manifest["status"] = "failed_regression"
    manifest_path.write_text(json_text(manifest), encoding="utf-8")
    if not qa["pass"]:
        raise RunError(f"{config.pipeline} completed but QA failed; see {qa_path}")
    if regression is not None and not regression["pass"]:
        raise RunError(f"{config.pipeline} completed but baseline regression failed; see {regression_path}")
    return {
        "pass": True,
        "pipeline": config.pipeline,
        "baseline_version": config.version,
        "output_dir": str(output_dir),
        "primary_output": str(primary_output),
        "manifest": str(manifest_path),
        "qa": str(qa_path),
        "regression": str(regression_path) if regression_path else None,
        "excel_report": str(excel_report_path) if excel_report_path else None,
    }


def revalidate_run(config: BaselineConfig, actual: Path) -> dict[str, Any]:
    """Re-run offline verification and refresh an existing run's status artifacts."""
    actual = actual.expanduser().resolve()
    report = verify_baseline(config, actual)
    output_dir = actual.parent
    regression_path = output_dir / "regression.json"
    regression_path.write_text(json_text(report), encoding="utf-8")

    manifest_path = output_dir / "run_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["regression_status"] = "pass" if report["pass"] else "fail"
        manifest["regression_report"] = str(regression_path)
        manifest["revalidated_at"] = datetime.now(timezone.utc).isoformat()
        manifest["revalidation_config_path"] = str(config.path)
        manifest["revalidation_config_sha256"] = sha256_file(config.path)
        if not manifest.get("qa_pass"):
            manifest["status"] = "failed_qa"
        elif report["pass"]:
            manifest["status"] = "success"
        else:
            manifest["status"] = "failed_regression"
        manifest_path.write_text(json_text(manifest), encoding="utf-8")
    return report


def _cli_source_sha256() -> str:
    import hashlib

    digest = hashlib.sha256()
    source_root = Path(__file__).resolve().parent
    for path in sorted(source_root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()
