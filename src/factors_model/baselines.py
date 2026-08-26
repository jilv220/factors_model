from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PIPELINES = {"bad_beta", "six_factor_ranking"}


class ConfigError(ValueError):
    """Raised when a baseline file is incomplete or inconsistent."""


@dataclass(frozen=True)
class BaselineConfig:
    path: Path
    data: dict[str, Any]

    @property
    def pipeline(self) -> str:
        return str(self.data["pipeline"])

    @property
    def version(self) -> str:
        return str(self.data["baseline_version"])

    def config_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.path.parent / path
        return path.resolve()

    def project_root(self, override: str | None = None) -> Path:
        if override:
            return Path(override).expanduser().resolve()
        return self.config_path(str(self.data["project_root"]))

    def project_path(self, value: str, project_root: Path | None = None) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (project_root or self.project_root()) / path
        return path.resolve()

    def python_path(self, override: str | None = None, project_root: Path | None = None) -> Path:
        if override:
            return Path(os.path.abspath(Path(override).expanduser()))
        value = str(self.data.get("python", sys.executable))
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (project_root or self.project_root()) / path
        # Preserve virtual-environment symlinks. Resolving the symlink here would
        # execute the base interpreter and lose the venv's installed packages.
        return Path(os.path.abspath(path))

    def script_path(self, project_root: Path | None = None) -> Path:
        # Pipeline implementations are owned by this repository and resolve
        # relative to their config, independently of the run's data root.
        return self.config_path(str(self.data["script"]))

    def output_root(self) -> Path:
        return self.config_path(str(self.data["output_root"]))

    def expected_output(self, project_root: Path | None = None) -> Path:
        regression = self.data.get("regression", {})
        value = regression.get("expected_output")
        if not value:
            raise ConfigError(f"{self.path}: regression.expected_output is required")
        return self.project_path(str(value), project_root)


def load_baseline(path: str | Path, expected_pipeline: str | None = None) -> BaselineConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"baseline config not found: {config_path}")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    for key in ("baseline_version", "pipeline", "project_root", "script", "output_root"):
        if not data.get(key):
            raise ConfigError(f"{config_path}: missing required key {key!r}")
    pipeline = str(data["pipeline"])
    if pipeline not in PIPELINES:
        raise ConfigError(f"{config_path}: unsupported pipeline {pipeline!r}")
    if expected_pipeline and pipeline != expected_pipeline:
        raise ConfigError(f"{config_path}: expected pipeline {expected_pipeline!r}, found {pipeline!r}")
    config = BaselineConfig(config_path, data)
    validate_baseline(config)
    return config


def validate_baseline(config: BaselineConfig) -> None:
    project_root = config.project_root()
    if not project_root.is_dir():
        raise ConfigError(f"project root not found: {project_root}")
    if not config.script_path(project_root).is_file():
        raise ConfigError(f"pipeline script not found: {config.script_path(project_root)}")
    if not config.python_path(project_root=project_root).is_file():
        raise ConfigError(f"configured Python not found: {config.python_path(project_root=project_root)}")
    model = config.data.get("model", {})
    inputs = config.data.get("inputs", {})
    if config.pipeline == "bad_beta":
        for key in ("universe",):
            if not inputs.get(key):
                raise ConfigError(f"{config.path}: inputs.{key} is required")
        for key in ("start", "as_of", "universe_as_of", "rho", "window_months", "min_months"):
            if key not in model:
                raise ConfigError(f"{config.path}: model.{key} is required")
        if int(model["min_months"]) > int(model["window_months"]):
            raise ConfigError("model.min_months cannot exceed model.window_months")
        universe = config.project_path(str(inputs["universe"]), project_root)
        if not universe.exists():
            raise ConfigError(f"universe input not found: {universe}")
    else:
        if not inputs.get("universe"):
            raise ConfigError(f"{config.path}: inputs.universe is required")
        universe = config.project_path(str(inputs["universe"]), project_root)
        if not universe.exists():
            raise ConfigError(f"six-factor universe not found: {universe}")
        for key in ("as_of", "factor_as_of", "method_id"):
            if not model.get(key):
                raise ConfigError(f"{config.path}: model.{key} is required")
        fixtures = config.data.get("fixtures")
        if fixtures is not None:
            for key in ("factor_input", "analyst_revisions", "reference_workbook"):
                if not fixtures.get(key):
                    raise ConfigError(f"{config.path}: fixtures.{key} is required")
                path = config.project_path(str(fixtures[key]), project_root)
                if not path.is_file():
                    raise ConfigError(f"six-factor fixture not found: {path}")
            for key in ("as_of", "factor_as_of", "revision_as_of"):
                if not fixtures.get(key):
                    raise ConfigError(f"{config.path}: fixtures.{key} is required")
        workers = int(config.data.get("execution", {}).get("workers", 4))
        if workers < 1:
            raise ConfigError(f"{config.path}: execution.workers must be at least 1")
        weights = model.get("weights", {})
        required_weights = {
            "quality",
            "fundamental_momentum",
            "analyst_revisions",
            "valuation",
            "conservative_investment",
            "shareholder_yield",
        }
        if set(weights) != required_weights:
            raise ConfigError(f"{config.path}: model.weights must define exactly {sorted(required_weights)}")
        if any(float(value) < 0 for value in weights.values()):
            raise ConfigError(f"{config.path}: model weights must be non-negative")
        if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
            raise ConfigError(f"{config.path}: model weights must sum to 1.0")


def resolve_cli_path(value: str | None, fallback: Path) -> Path:
    if value is None:
        return fallback.resolve()
    return Path(value).expanduser().resolve()


def default_output_dir(config: BaselineConfig, as_of: str) -> Path:
    return config.output_root() / as_of / config.pipeline


def build_command(config: BaselineConfig, options: dict[str, Any], output_dir: Path) -> list[str]:
    project_root = config.project_root(options.get("project_root"))
    python = config.python_path(options.get("python"), project_root)
    script = config.script_path(project_root)
    if config.pipeline == "bad_beta":
        inputs = config.data["inputs"]
        model = config.data["model"]
        universe = resolve_cli_path(
            options.get("universe"), config.project_path(str(inputs["universe"]), project_root)
        )
        command = [
            str(python),
            str(script),
            "--universe",
            str(universe),
            "--universe-as-of",
            str(options.get("universe_as_of") or model["universe_as_of"]),
            "--output",
            str(output_dir / "results.json"),
            "--cape-csv",
            str(config.project_path(str(inputs["cape_csv"]), project_root)),
            "--cape-xls",
            str(config.project_path(str(inputs["cape_xls"]), project_root)),
            "--start",
            str(options.get("start") or model["start"]),
            "--as-of",
            str(options.get("as_of") or model["as_of"]),
            "--rho",
            str(options.get("rho") if options.get("rho") is not None else model["rho"]),
            "--window-months",
            str(options.get("window_months") or model["window_months"]),
            "--min-months",
            str(options.get("min_months") or model["min_months"]),
        ]
        return command

    inputs = config.data["inputs"]
    fixtures = config.data.get("fixtures", {})
    model = config.data["model"]
    execution = config.data.get("execution", {})
    weights = model["weights"]
    frozen = bool(options.get("frozen_baseline"))
    if frozen and not fixtures:
        raise ConfigError(
            f"{config.path}: [fixtures] is required only when using --frozen-baseline"
        )
    if options.get("universe") and options.get("factor_input"):
        raise ConfigError("--universe and --factor-input are mutually exclusive")
    if frozen:
        forbidden = (
            "universe",
            "factor_input",
            "revisions",
            "as_of",
            "factor_as_of",
            "revision_as_of",
            "refresh_revisions",
        )
        if any(options.get(key) not in (None, False) for key in forbidden):
            raise ConfigError("--frozen-baseline cannot be combined with universe, input, date, or refresh overrides")
        run_as_of = str(fixtures["as_of"])
        factor_as_of = str(fixtures["factor_as_of"])
        revision_as_of = str(fixtures["revision_as_of"])
    else:
        run_as_of = str(options.get("as_of") or model["as_of"])
        factor_as_of = str(
            options.get("factor_as_of")
            or (run_as_of if options.get("as_of") else model["factor_as_of"])
        )
        revision_as_of = str(options.get("revision_as_of") or "")
    command = [
        str(python),
        str(script),
    ]
    if frozen:
        command.extend(
            [
                "--factor-input",
                str(config.project_path(str(fixtures["factor_input"]), project_root)),
                "--revisions",
                str(config.project_path(str(fixtures["analyst_revisions"]), project_root)),
            ]
        )
    elif options.get("factor_input"):
        command.extend(["--factor-input", str(resolve_cli_path(str(options["factor_input"]), Path()))])
    else:
        universe = resolve_cli_path(
            options.get("universe"), config.project_path(str(inputs["universe"]), project_root)
        )
        command.extend(["--universe", str(universe)])
    if not frozen and options.get("revisions"):
        command.extend(["--revisions", str(resolve_cli_path(str(options["revisions"]), Path()))])
    command.extend(
        [
        "--output-dir",
        str(output_dir),
        "--as-of",
        run_as_of,
        "--factor-as-of",
        factor_as_of,
        "--sleep",
        str(options.get("sleep") if options.get("sleep") is not None else execution.get("sleep", 0.12)),
        "--workers",
        str(options.get("workers") if options.get("workers") is not None else execution.get("workers", 4)),
        "--sec-user-agent",
        str(options.get("sec_user_agent") or execution.get("sec_user_agent", "factors-model research research-contact@example.com")),
        "--weight-quality",
        str(weights["quality"]),
        "--weight-fundamental-momentum",
        str(weights["fundamental_momentum"]),
        "--weight-analyst-revisions",
        str(weights["analyst_revisions"]),
        "--weight-valuation",
        str(weights["valuation"]),
        "--weight-conservative-investment",
        str(weights["conservative_investment"]),
        "--weight-shareholder-yield",
        str(weights["shareholder_yield"]),
        ]
    )
    if revision_as_of:
        command.extend(["--revision-as-of", revision_as_of])
    refresh = bool(options.get("refresh_revisions")) or (
        bool(execution.get("refresh_revisions", True))
        and not frozen
        and not options.get("revisions")
    )
    if refresh:
        command.append("--refresh-revisions")
    return command


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_inputs(config: BaselineConfig, options: dict[str, Any]) -> list[dict[str, Any]]:
    project_root = config.project_root(options.get("project_root"))
    inputs = config.data["inputs"]
    if config.pipeline == "bad_beta":
        universe = resolve_cli_path(
            options.get("universe"), config.project_path(str(inputs["universe"]), project_root)
        )
        if universe.is_dir():
            paths = [("universe", path) for path in sorted(universe.glob("decile_*.csv*")) if path.is_file()]
        else:
            paths = [("universe", universe)]
        for role in ("cape_csv", "cape_xls"):
            if inputs.get(role):
                candidate = config.project_path(str(inputs[role]), project_root)
                if candidate.is_file():
                    paths.append((role, candidate))
    else:
        if options.get("frozen_baseline"):
            fixtures = config.data.get("fixtures", {})
            if not fixtures:
                raise ConfigError(
                    f"{config.path}: [fixtures] is required only when using --frozen-baseline"
                )
            paths = [
                ("five_factor_fixture", config.project_path(str(fixtures["factor_input"]), project_root)),
                ("analyst_revisions_fixture", config.project_path(str(fixtures["analyst_revisions"]), project_root)),
                ("reference_workbook", config.project_path(str(fixtures["reference_workbook"]), project_root)),
            ]
        elif options.get("factor_input"):
            paths = [("five_factor_input", resolve_cli_path(str(options["factor_input"]), Path()))]
        else:
            universe = resolve_cli_path(
                options.get("universe"), config.project_path(str(inputs["universe"]), project_root)
            )
            if universe.is_dir():
                paths = [
                    ("universe", path)
                    for path in sorted(universe.glob("*.csv*"))
                    if path.is_file()
                ]
            else:
                paths = [("universe", universe)]
        if not options.get("frozen_baseline") and options.get("revisions"):
            paths.append(("analyst_revisions", resolve_cli_path(str(options["revisions"]), Path())))
    return [
        {"role": role, "path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for role, path in paths
    ]


def git_revision(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
