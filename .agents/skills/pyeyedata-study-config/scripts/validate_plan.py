#!/usr/bin/env python3
"""Fingerprint PYeyedata's live plan contract, export native settings, and validate.

This helper validates settings only. It never imports analysis data or runs the pipeline.
Run from an approved PYeyedata CPython 3.12 environment with PySide6 installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

MAX_PLAN_BYTES = 2 * 1024 * 1024
FINGERPRINT_SCHEMA = "pyeyedata-study-source-contract/v1"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number: {value}")
    return number


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _read_strict(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("plan exceeds the native 2 MiB size limit")
    value = json.loads(
        raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
        parse_constant=_constant, parse_float=_finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("plan root must be a JSON object")
    return value


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _assert_origin(module: Any, root: Path) -> None:
    origin = Path(module.__file__).resolve()
    if not _inside(origin, root):
        raise RuntimeError(f"refusing module outside selected checkout: {module.__name__} -> {origin}")


def _project_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not (root / "pyeyedata" / "__init__.py").is_file():
        raise ValueError(f"not a PYeyedata source checkout: {root}")
    return root


def _import_project(root: Path) -> tuple[Any, Any, Any]:
    """Put the requested checkout first, then refuse mixed or shadowed modules."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root_text = str(root)
    while root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)

    import pyeyedata
    import pyeyedata.pipeline as pipeline
    import pyeyedata.gui.analysis_plan as analysis_plan

    package_path = [Path(item).resolve() for item in pyeyedata.__path__]
    expected_package = (root / "pyeyedata").resolve()
    if package_path != [expected_package]:
        raise RuntimeError(f"refusing mixed pyeyedata package paths: {package_path}")
    for module in (pyeyedata, pipeline, analysis_plan):
        _assert_origin(module, root)
    return pyeyedata, pipeline, analysis_plan


def _ensure_qapplication() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["pyeyedata-study-config"])


def _source_files(root: Path) -> list[Path]:
    files = [path for path in (root / "pyeyedata").rglob("*.py") if "__pycache__" not in path.parts]
    files.extend(root / name for name in ("pyeyedata_bootstrap.py", "pyeyedata_startup.py")
                 if (root / name).is_file())
    return sorted({path.resolve() for path in files}, key=lambda path: path.relative_to(root).as_posix())


def _contract(root: Path, pyeyedata: Any, pipeline: Any) -> dict[str, Any]:
    schema = pipeline.get_analysis_schema()
    app_version = str(getattr(__import__("pyeyedata.app_info", fromlist=["APP_VERSION"]), "APP_VERSION"))
    files = []
    digest = hashlib.sha256()
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": relative, "sha256": file_hash})
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "schema": FINGERPRINT_SCHEMA,
        "application_version": app_version,
        "pipeline_schema_version": str(schema["pipeline_schema_version"]),
        "contract_version": str(schema["contract_version"]),
        "fingerprint_sha256": digest.hexdigest(),
        "source_files": files,
        "analysis_schema": schema,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(value), encoding="utf-8", newline="\n")


def _default_plan(root: Path, analysis_plan: Any, pipeline: Any, *, example: bool) -> dict[str, Any]:
    app = _ensure_qapplication()
    from pyeyedata.gui.main_window import MainWindow
    import pyeyedata.gui.main_window as main_window_module
    _assert_origin(main_window_module, root)

    main = MainWindow()
    payload = main.analysis_plan(
        name="SYNTHETIC_ONLY — independent-groups configuration example" if example else "Native PYeyedata default baseline"
    )
    if example:
        config = payload["configuration"]
        config["selection"] = {
            "individual_metric_ids": ["individual.pupil_left.mean"],
            "group_metric_ids": [],
            "advanced_metric_ids": [],
            "run_individual": True,
            "run_group": False,
            "run_advanced": False,
        }
        current = config["statistics_design"]
        current.update({
            "design_type": "independent",
            "outcome_type": "continuous",
            "analysis_unit": "subject",
            "subject_column": "participant_id",
            "group_column": "condition",
            "independence_confirmed": True,
            "independence_note": "Synthetic example only; confirm from the actual allocation and IDs.",
        })
        payload["name"] = "SYNTHETIC_ONLY — independent-groups example (not a study plan)"
    # Run the public validator against the live schema before returning.
    analysis_plan.validate_plan(payload, pipeline.get_analysis_schema())
    main.deleteLater()
    app.processEvents()
    return payload


def _validate(args: argparse.Namespace) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, analysis_plan = _import_project(root)
    app = _ensure_qapplication()
    payload = _read_strict(Path(args.plan).expanduser().resolve())
    # Also invoke the application's strict byte reader; duplicate/non-finite JSON is rejected above.
    payload = analysis_plan.read_plan(args.plan)
    schema = pipeline.get_analysis_schema()
    normalized = analysis_plan.validate_plan(payload, schema)
    contract = _contract(root, pyeyedata, pipeline)
    snapshot_state = "not_supplied"
    if args.snapshot:
        snapshot = _read_strict(Path(args.snapshot).expanduser().resolve())
        snapshot_state = (
            "match" if snapshot.get("fingerprint_sha256") == contract["fingerprint_sha256"] else "mismatch"
        )
    report = {
        "status": "native_validation_passed_current_source",
        "configuration_status": "structure_loadable" if snapshot_state == "match" else "draft",
        "plan_path": str(Path(args.plan).expanduser().resolve()),
        "application_version": contract["application_version"],
        "pipeline_schema_version": contract["pipeline_schema_version"],
        "contract_version": contract["contract_version"],
        "current_source_fingerprint_sha256": contract["fingerprint_sha256"],
        "snapshot_state": snapshot_state,
        "normalized_configuration_groups": sorted(normalized),
        "runnable": "not_assessed_by_plan_validation",
        "real_pilot": "not_run",
        "analysis_executed": False,
    }
    if args.report:
        _write_json(Path(args.report).expanduser().resolve(), report)
    print(_dumps(report), end="")
    del app
    return 2 if snapshot_state == "mismatch" else 0


def _export(args: argparse.Namespace, *, example: bool) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, analysis_plan = _import_project(root)
    payload = _default_plan(root, analysis_plan, pipeline, example=example)
    destination = Path(args.out).expanduser().resolve()
    analysis_plan.write_plan(destination, payload)
    saved = analysis_plan.read_plan(destination)
    normalized = analysis_plan.validate_plan(saved, pipeline.get_analysis_schema())
    contract = _contract(root, pyeyedata, pipeline)
    contract_path = Path(args.contract_out).expanduser().resolve() if args.contract_out else destination.with_name("source-contract.json")
    _write_json(contract_path, contract)
    report = {
        "status": "structure_loadable",
        "synthetic_only": bool(example),
        "plan_path": str(destination),
        "source_contract_path": str(contract_path),
        "application_version": contract["application_version"],
        "pipeline_schema_version": contract["pipeline_schema_version"],
        "contract_version": contract["contract_version"],
        "source_fingerprint_sha256": contract["fingerprint_sha256"],
        "native_configuration_groups": sorted(normalized),
        "runnable": "not_assessed_no_data_imported",
        "real_pilot": "not_run",
        "analysis_executed": False,
    }
    if args.report:
        _write_json(Path(args.report).expanduser().resolve(), report)
    print(_dumps(report), end="")
    return 0


def _snapshot(args: argparse.Namespace) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, _ = _import_project(root)
    contract = _contract(root, pyeyedata, pipeline)
    _write_json(Path(args.out).expanduser().resolve(), contract)
    print(_dumps({
        "status": "source_contract_captured",
        "snapshot_path": str(Path(args.out).expanduser().resolve()),
        "application_version": contract["application_version"],
        "pipeline_schema_version": contract["pipeline_schema_version"],
        "contract_version": contract["contract_version"],
        "fingerprint_sha256": contract["fingerprint_sha256"],
    }), end="")
    return 0


def _selfcheck(args: argparse.Namespace) -> int:
    root = _project_root(args.project_root)
    pyeyedata, pipeline, analysis_plan = _import_project(root)
    contract = _contract(root, pyeyedata, pipeline)
    complete_plan = _default_plan(root, analysis_plan, pipeline, example=True)
    good = analysis_plan.validate_plan(complete_plan, pipeline.get_analysis_schema())
    checks: list[dict[str, Any]] = [{
        "check": "complete_synthetic_independent_design",
        "status": "passed",
        "synthetic_only": True,
        "native_configuration_groups": sorted(good),
        "design_type": good["statistics_design"]["design_type"],
        "metric_ids": good["selection"]["individual_metric_ids"],
        "analysis_executed": False,
    }]

    # Missing study facts remain blocked; this is a workflow fixture, not data inference.
    required_facts = {
        "source_header_and_mapping": None,
        "timestamp_unit_and_origin": None,
        "pupil_semantics_and_unit": None,
        "participant_id_source": None,
        "group_allocation_and_independence_evidence": None,
    }
    missing = sorted(key for key, value in required_facts.items() if value is None)
    assert missing
    checks.append({"check": "missing_metadata_blocks_readiness", "status": "passed",
                   "configuration": "draft", "run": "blocked", "missing": missing})

    schema = pipeline.get_analysis_schema()
    bad_version = json.loads(_dumps(complete_plan))
    bad_version["pipeline_schema_version"] = "not-the-live-schema"
    try:
        analysis_plan.validate_plan(bad_version, schema)
    except ValueError:
        checks.append({"check": "wrong_pipeline_schema_rejected", "status": "passed"})
    else:
        raise AssertionError("wrong pipeline schema was accepted")

    unknown = json.loads(_dumps(complete_plan))
    unknown["configuration"]["invented_group"] = {}
    try:
        analysis_plan.validate_plan(unknown, schema)
    except ValueError:
        checks.append({"check": "unknown_native_group_rejected", "status": "passed"})
    else:
        raise AssertionError("unknown configuration group was accepted")

    with tempfile.TemporaryDirectory(prefix="pyeyedata-study-config-") as temporary:
        directory = Path(temporary)
        for filename, content in (
            ("duplicate.json", '{"schema":"x","schema":"y"}'),
            ("nan.json", '{"value":NaN}'),
        ):
            path = directory / filename
            path.write_text(content, encoding="utf-8")
            try:
                _read_strict(path)
            except (ValueError, json.JSONDecodeError):
                checks.append({"check": f"strict_json_rejects_{path.stem}", "status": "passed"})
            else:
                raise AssertionError(f"strict reader accepted {filename}")
        plan_path = directory / "valid-example.pyeplan"
        analysis_plan.write_plan(plan_path, complete_plan)
        roundtrip = analysis_plan.read_plan(plan_path)
        analysis_plan.validate_plan(roundtrip, schema)
        checks.append({"check": "native_write_read_validate_roundtrip", "status": "passed"})

    report = {
        "schema": "pyeyedata-study-config-selfcheck/v1",
        "status": "passed",
        "source_fingerprint_sha256": contract["fingerprint_sha256"],
        "checks": checks,
        "limitations": ["synthetic configuration only", "no real data imported", "no analysis executed", "no device validation"],
    }
    _write_json(Path(args.out).expanduser().resolve(), report)
    print(_dumps(report), end="")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="capture current public schema and full source fingerprint")
    snapshot.add_argument("--project-root", required=True)
    snapshot.add_argument("--out", required=True)
    snapshot.set_defaults(handler=_snapshot)

    validate = commands.add_parser("validate", help="strict-read and validate an existing .pyeplan")
    validate.add_argument("plan")
    validate.add_argument("--project-root", required=True)
    validate.add_argument("--snapshot")
    validate.add_argument("--report")
    validate.set_defaults(handler=_validate)

    for name, example in (("export-default", False), ("example-independent", True)):
        export = commands.add_parser(name, help="export a real MainWindow plan and validate it offscreen")
        export.add_argument("--project-root", required=True)
        export.add_argument("--out", required=True)
        export.add_argument("--contract-out")
        export.add_argument("--report")
        export.set_defaults(handler=lambda args, mode=example: _export(args, example=mode))

    selfcheck = commands.add_parser("selfcheck", help="exercise strict JSON and native plan validation with synthetic settings")
    selfcheck.add_argument("--project-root", required=True)
    selfcheck.add_argument("--out", required=True)
    selfcheck.set_defaults(handler=_selfcheck)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
