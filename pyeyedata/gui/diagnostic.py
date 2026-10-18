"""Bounded, application-owned diagnostics for the packaged Windows build.

The command accepts only a JSON config with a fixed scenario and local paths.
It never imports or executes caller-supplied code.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


CONFIG_SCHEMA = "pyeyedata.diagnostic-config/v1"
EVIDENCE_SCHEMA = "pyeyedata.diagnostic-evidence/v1"
MAX_CONFIG_BYTES = 64 * 1024
SCENARIOS = frozenset({"gui-startup", "synthetic-pipeline", "open-archive", "reliability-smoke", "usability-smoke"})
_DEMO_INPUTS = {
    "signals.csv": "demo/SYNTHETIC_ONLY_A_low_rate_signals.csv",
    "segments.csv": "demo/SYNTHETIC_ONLY_A_segments.csv",
    "aois.csv": "demo/SYNTHETIC_ONLY_A_aois.csv",
}


class DiagnosticConfigError(ValueError):
    """The config is malformed or requests an unsupported action."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiagnosticConfigError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DiagnosticConfigError(f"Non-finite JSON value is not allowed: {value}")


def _read_config(path_text: str) -> dict[str, Any]:
    path = Path(path_text).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise DiagnosticConfigError("Diagnostic config must be a regular file no larger than 64 KiB")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticConfigError(f"Cannot read diagnostic config: {exc}") from exc
    if not isinstance(value, dict):
        raise DiagnosticConfigError("Diagnostic config must be a JSON object")
    scenario = value.get("scenario")
    expected = {"schema", "scenario", "workspace", "evidence_path", "display"}
    if scenario == "open-archive":
        expected.add("archive_path")
    if set(value) != expected:
        raise DiagnosticConfigError("Diagnostic config fields do not match the selected fixed scenario")
    if value.get("schema") != CONFIG_SCHEMA:
        raise DiagnosticConfigError(f"schema must be {CONFIG_SCHEMA!r}")
    if scenario not in SCENARIOS:
        raise DiagnosticConfigError(
            "scenario must be gui-startup, synthetic-pipeline, open-archive, reliability-smoke, or usability-smoke"
        )
    if value.get("display") not in {"native", "offscreen"}:
        raise DiagnosticConfigError("display must be 'native' or 'offscreen'")
    return value


def _absolute_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DiagnosticConfigError(f"{name} must be a nonempty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DiagnosticConfigError(f"{name} must be absolute")
    return path.resolve()


def _paths(config: dict[str, Any]) -> tuple[Path, Path]:
    workspace = _absolute_path(config.get("workspace"), "workspace")
    evidence = _absolute_path(config.get("evidence_path"), "evidence_path")
    if evidence == workspace or not evidence.is_relative_to(workspace):
        raise DiagnosticConfigError("evidence_path must be below workspace")
    if config["scenario"] == "open-archive":
        archive = _absolute_path(config.get("archive_path"), "archive_path")
        if not archive.is_file() or archive.suffix.lower() != ".pyea":
            raise DiagnosticConfigError("archive_path must be an existing .pyea file")
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise DiagnosticConfigError("workspace must be a new or empty directory")
    if evidence.exists():
        raise DiagnosticConfigError("evidence_path already exists; diagnostics never overwrite evidence")
    return workspace, evidence


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _resource_record(relative_path: str) -> dict[str, Any]:
    from pyeyedata.resource_paths import read_resource_bytes

    content = read_resource_bytes(relative_path)
    return {"path": relative_path, "size_bytes": len(content), "sha256": _sha256_bytes(content)}


def _wait_for_idle(app: Any, manager: Any, *, timeout_seconds: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while manager.active:
        app.processEvents()
        if time.monotonic() >= deadline:
            manager.cancel()
            raise TimeoutError("Diagnostic task exceeded its fixed 180-second wait bound")
        time.sleep(0.01)
    app.processEvents()


def _wait_for_condition(app: Any, predicate: Any, description: str,
                        *, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        app.processEvents()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for {description}")
        time.sleep(0.01)
    app.processEvents()


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    encoded = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _write_config_error_receipt(exc: Exception) -> Path:
    """Keep config failures locatable without opening a modal window."""
    from pyeyedata_bootstrap import user_directory

    root = user_directory("logs", "diagnostics")
    path = root / f"diagnostic-error-{os.getpid()}-{time.time_ns()}.json"
    receipt = {
        "schema": EVIDENCE_SCHEMA,
        "status": "failed",
        "stage": "configuration-or-startup",
        "error": {"type": type(exc).__name__, "message": str(exc)},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_evidence(path, receipt)
    return path


def _prepare_demo_dataset(workspace: Path) -> tuple[Any, dict[str, bytes], dict[str, Any]]:
    """Build the fixed A fixture through the product CSV/XLSX import APIs."""
    import pandas as pd

    from pyeyedata import (
        CoordinateMetadata, SignalImportOptions, normalize_aois,
        normalize_segments, normalize_signals, read_table,
    )
    from pyeyedata.resource_paths import read_resource_bytes

    input_root = workspace / "inputs"
    input_root.mkdir()
    source_bytes = {name: read_resource_bytes(resource) for name, resource in _DEMO_INPUTS.items()}
    for name, content in source_bytes.items():
        (input_root / name).write_bytes(content)

    csv_signals = read_table(input_root / "signals.csv", role="signals")
    xlsx_path = input_root / "signals.xlsx"
    # Convert only the bundled synthetic fixture; no caller data or code reaches
    # the spreadsheet writer.
    csv_signals.frame.to_excel(xlsx_path, index=False, sheet_name="signals")
    csv_probe = read_table(input_root / "signals.csv", role="signals")
    xlsx_signals = read_table(xlsx_path, role="signals", sheet_name="signals")
    pd.testing.assert_frame_equal(csv_probe.frame, xlsx_signals.frame, check_dtype=False)

    time = CoordinateMetadata(
        time_unit="s", time_precision_s=0.1,
        coordinate_unit="px", coordinate_frame="synthetic_top_left",
    )
    dataset = normalize_signals(xlsx_signals, SignalImportOptions(time=time))
    segment_import = read_table(input_root / "segments.csv", role="segments")
    aoi_import = read_table(input_root / "aois.csv", role="aois")
    dataset.segments = normalize_segments(segment_import, time=time)
    dataset.aois = normalize_aois(
        aoi_import, coordinate_unit="px", coordinate_frame="synthetic_top_left",
        overlap_policy="exclusive",
    )
    dataset.metadata["time_confirmed"] = True
    dataset.metadata["diagnostic_fixture"] = "SYNTHETIC_ONLY_A"
    dataset.source_manifest.extend((segment_import.manifest_entry(), aoi_import.manifest_entry()))
    raw_sources = {
        "signals.xlsx": xlsx_path.read_bytes(),
        "segments.csv": source_bytes["segments.csv"],
        "aois.csv": source_bytes["aois.csv"],
    }
    evidence = {
        "fixture": "SYNTHETIC_ONLY_A",
        "csv_xlsx_import_equivalent": True,
        "tables": {
            "signals_csv": {"rows": len(csv_probe.frame), "sha256": csv_probe.sha256},
            "signals_xlsx": {"rows": len(xlsx_signals.frame), "sha256": xlsx_signals.sha256,
                             "sheet_name": xlsx_signals.sheet_name},
            "segments_csv": segment_import.manifest_entry(),
            "aois_csv": aoi_import.manifest_entry(),
        },
        "normalized_rows": {"signals": len(dataset.signals), "segments": len(dataset.segments),
                            "aois": len(dataset.aois)},
        "device_validation_status": dataset.metadata.get("device_validation_status", "unknown"),
        "physiology_validation_status": "NOT_RUN",
    }
    return dataset, raw_sources, evidence


def _prepare_team_dataset(workspace: Path) -> tuple[Any, dict[str, bytes], dict[str, Any]]:
    """Build the fixed six-team C06 comparison fixture from code-owned inputs."""
    import numpy as np
    import pandas as pd

    from pyeyedata import (
        CoordinateMetadata, EyeCoordinateMetadata, SignalImportOptions,
        normalize_signals, read_table,
    )

    members_by_team = {
        f"T{team}": [f"T{team}-M{member}" for member in range(6)]
        for team in range(6)
    }
    angles = np.tile(np.r_[np.zeros(7), np.arange(1, 7) * 0.6, np.full(7, 3.6)], 4)
    rows: list[dict[str, Any]] = []
    for team, (interaction, members) in enumerate(members_by_team.items()):
        for member, subject in enumerate(members):
            for sample, angle in enumerate(angles):
                time_s = 0.02 * sample
                radians = np.deg2rad(angle)
                rows.append({
                    "subject_id": subject, "session_id": "S", "interaction_id": interaction,
                    "condition": "A" if team < 3 else "B", "timestamp": time_s,
                    "clock_id": "synthetic-shared-clock",
                    "gaze_left_direction_x": np.sin(radians),
                    "gaze_left_direction_y": 0.0,
                    "gaze_left_direction_z": np.cos(radians),
                    "gaze_left_valid": True,
                    "pupil_left": 3.0 + member * 0.1 + np.sin(3.0 * time_s + 0.1 * (team + 1) * member),
                    "pupil_left_valid": True,
                })

    source = workspace / "inputs" / "SYNTHETIC_ONLY_SIX_DISJOINT_TEAMS.csv"
    content = pd.DataFrame(rows).to_csv(index=False).encode("utf-8")
    if source.exists():
        raise RuntimeError("Fixed synthetic team input already exists; diagnostics never overwrite inputs")
    source.write_bytes(content)
    time = CoordinateMetadata(
        time_unit="s", clock_source="synthetic-shared-clock",
        coordinate_representation="direction",
        left_eye=EyeCoordinateMetadata(
            direction_unit="unitless", direction_frame="synthetic-ray-frame",
        ),
    )
    imported = read_table(source, role="signals")
    dataset = normalize_signals(imported, SignalImportOptions(time=time))
    dataset.metadata.update(
        time_confirmed=True,
        pupil_unit="mm",
        cross_subject_pupil_comparable=True,
        pupil_comparability_refs=["synthetic:same-declared-measurement-scale"],
        synchronization={
            "confirmed": True,
            "method": "explicit synthetic common timestamps",
            "source_refs": ["synthetic:shared-clock"],
        },
        device_verified=False,
        physiology_validation_status="NOT_RUN",
    )
    raw_sources = {source.name: source.read_bytes()}
    expected = {
        "fixture": "SYNTHETIC_ONLY_SIX_DISJOINT_TEAMS",
        "rows": len(dataset.signals),
        "members": sum(len(items) for items in members_by_team.values()),
        "teams": members_by_team,
        "condition_assignment": {"A": ["T0", "T1", "T2"], "B": ["T3", "T4", "T5"]},
        "sample_step_s": 0.02,
        "samples_per_member": 80,
        "pupil_formula": "3+0.1*member+sin(3*t+0.1*(team+1)*member)",
        "purpose": "disjoint synthetic team comparison; no device or physiology evidence",
        "device_verified": False,
        "physiology_validation_status": "NOT_RUN",
        "sha256": _sha256_bytes(content),
    }
    if expected["rows"] != 2880 or expected["members"] != 36:
        raise RuntimeError("Fixed six-team fixture dimensions changed")
    return dataset, raw_sources, expected


def _run_gui_task(app: Any, window: Any, *, phase: str) -> tuple[Any | None, list[dict[str, str]]]:
    """Run one GUI-owned task and return its typed outcome and task failures."""
    completed: list[Any] = []
    failures: list[dict[str, str]] = []
    on_completed = lambda _run_id, _generation, result: completed.append(result)
    on_failed = lambda _run_id, _generation, kind, message: failures.append(
        {"type": str(kind), "message": str(message)}
    )
    window.task_manager.completed.connect(on_completed)
    window.task_manager.failed.connect(on_failed)
    try:
        if phase == "preflight":
            window.start_preflight()
        elif phase == "run":
            window.start_analysis()
        else:
            raise ValueError(f"Unsupported diagnostic task phase: {phase}")
        _wait_for_idle(app, window.task_manager)
    finally:
        window.task_manager.completed.disconnect(on_completed)
        window.task_manager.failed.disconnect(on_failed)
    return (completed[-1] if completed else None), failures


def _diagnostic_value(value: Any) -> Any:
    """Convert archive identity facts to deterministic JSON without repr fallbacks."""
    import math
    from collections.abc import Mapping
    from datetime import date, datetime

    import numpy as np
    import pandas as pd

    if value is None or value is pd.NA or value is pd.NaT:
        return {"$missing": type(value).__name__}
    if isinstance(value, np.generic):
        return _diagnostic_value(value.item())
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return {"$datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes_sha256": _sha256_bytes(value), "length": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _diagnostic_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_diagnostic_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": str(value)}
    if isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported diagnostic identity type: {type(value).__name__}")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(_diagnostic_value(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(payload)


def _record_archive_facts(record: Any, app_archive: Any, archive: Any) -> dict[str, Any]:
    from pyeyedata.gui.models import _table_hash

    metadata = dict(record.metadata())
    tables = record.tables()
    app = dict(app_archive)
    runtime = dict(app.get("runtime", {}))
    audit_fields = {
        key: value for key, value in metadata.items()
        if "audit" in key.casefold() or key == "execution_cache"
    }
    return {
        "mode": str(archive.mode),
        "run": {
            "run_id": str(record.run_id), "generation": int(record.generation),
            "revision_id": str(record.revision_id),
            "config_fingerprint": str(record.config_fingerprint),
            "phase": str(record.phase), "status": str(record.status),
        },
        "table_hashes": {name: _table_hash(frame) for name, frame in tables.items()},
        "audit_table_hashes": {
            name: _table_hash(frame) for name, frame in tables.items() if "audit" in name.casefold()
        },
        "metadata_audit_sha256": _canonical_sha256(audit_fields),
        "version_facts": {
            "application_name": runtime.get("application_name"),
            "application_version": runtime.get("application_version"),
            "prd_version": runtime.get("prd_version"),
            "data_contract_version": runtime.get("data_contract_version"),
            "pipeline_schema_version": runtime.get("pipeline_schema_version"),
            "version_document_sha256": runtime.get("version_document_sha256"),
            "build_identity": runtime.get("build_identity"),
            "archive_schema_version": archive.manifest.get("schema_version"),
            "archive_contract_version": archive.manifest.get("contract_version"),
        },
        "runtime_sha256": _canonical_sha256(runtime),
        "recomputable": bool(app.get("recomputable", False)),
        "snapshot_schema_version": (
            str(record.snapshot.schema_version) if record.snapshot is not None else None
        ),
    }


def _write_archive_expectation(path: Path, record: Any, loaded: Any) -> dict[str, Any]:
    """Write the saved archive's exact projection for later GUI reopen comparisons."""
    from pyeyedata.archive_session import project_result_tables
    from pyeyedata.gui.models import _table_hash

    mode = str(loaded.archive_mode)
    projected, inventory = project_result_tables(record.tables(), mode=mode)
    projected_hashes = {name: _table_hash(frame) for name, frame in projected.items()}
    facts = _record_archive_facts(loaded.record, loaded.app_archive, loaded.analysis.archive)
    actual_hashes = facts["table_hashes"]
    if actual_hashes != projected_hashes:
        expected_names, actual_names = set(projected_hashes), set(actual_hashes)
        differing = sorted(
            name for name in expected_names & actual_names
            if projected_hashes[name] != actual_hashes[name]
        )
        raise RuntimeError(
            f"{mode} archive table projection differed from the original run; "
            f"missing={sorted(expected_names - actual_names)}, "
            f"unexpected={sorted(actual_names - expected_names)}, "
            f"hash_mismatches={differing}"
        )

    excluded_tables = sorted(item.name for item in inventory if item.action == "exclude")
    if mode == "full":
        source_raw = record.snapshot_details().get("raw_sources", {}) if record.snapshot is not None else {}
        if (loaded.record.snapshot is None or loaded.analysis.dataset is None
                or len(loaded.analysis.raw_sources) != len(source_raw)
                or facts["snapshot_schema_version"] != record.snapshot.schema_version):
            raise RuntimeError("Full archive omitted the original dataset, raw sources, or snapshot")
    elif mode == "results_only":
        expected_exclusions = sorted({
            "input_signals", "input_segments", "input_aois", "input_performance",
            "processed_signals", "analysis_streams",
        })
        if (len(record.table_names()) != 23 or len(projected) != 17
                or excluded_tables != expected_exclusions
                or loaded.analysis.dataset is not None or loaded.analysis.raw_sources
                or loaded.record.snapshot is not None
                or loaded.app_archive.get("recomputable") is not False
                or facts["snapshot_schema_version"] is not None):
            raise RuntimeError(
                "Results-only archive violated its fixed projection contract: "
                f"source_tables={len(record.table_names())}, retained={len(projected)}, "
                f"excluded={excluded_tables}, dataset_present={loaded.analysis.dataset is not None}, "
                f"raw_source_count={len(loaded.analysis.raw_sources)}, "
                f"snapshot_present={loaded.record.snapshot is not None}, "
                f"recomputable={loaded.app_archive.get('recomputable')}"
            )
    else:
        raise RuntimeError(f"Unexpected diagnostic archive mode: {mode}")

    facts["table_projection"] = {
        "mode": mode,
        "source_table_count": len(record.table_names()),
        "retained_table_count": len(projected),
        "retained_tables": sorted(projected),
        "excluded_table_count": len(excluded_tables),
        "excluded_tables": excluded_tables,
        "dataset_present": loaded.analysis.dataset is not None,
        "raw_source_count": len(loaded.analysis.raw_sources),
        "snapshot_present": loaded.record.snapshot is not None,
    }
    sidecar = Path(str(path) + ".diagnostic-expected.json")
    _write_evidence(sidecar, {"schema": "pyeyedata.diagnostic-archive-baseline/v1", **facts})
    return {"path": sidecar.name, "sha256": _sha256_bytes(sidecar.read_bytes()), **facts}


def _same_process_archive_roundtrip(app: Any, window: Any, path: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    loaded: list[Any] = []
    failures: list[str] = []
    on_completed = lambda _generation, result: loaded.append(result)
    on_failed = lambda _generation, details: failures.append(str(details))
    window.operation_manager.completed.connect(on_completed)
    window.operation_manager.failed.connect(on_failed)
    window.open_run_archive(path)
    try:
        _wait_for_idle(app, window.operation_manager)
    finally:
        window.operation_manager.completed.disconnect(on_completed)
        window.operation_manager.failed.disconnect(on_failed)
    if failures or not loaded:
        raise RuntimeError("GUI archive reopen failed: " + (failures[-1] if failures else "no result"))
    result = loaded[-1]
    facts = _record_archive_facts(result.record, result.app_archive, result.analysis.archive)
    if any(facts.get(key) != baseline.get(key) for key in (
        "mode", "run", "table_hashes", "audit_table_hashes", "metadata_audit_sha256",
        "version_facts", "runtime_sha256", "recomputable", "snapshot_schema_version",
    )):
        raise RuntimeError(f"Same-process archive roundtrip differed from saved archive baseline: {path.name}")
    return {"mode": result.archive_mode, "run_id": result.record.run_id,
            "table_count": len(result.record.table_names()), "table_hashes_match": True,
            "version_facts_match": True, "audit_facts_match": True,
            "recomputable": bool(result.app_archive.get("recomputable", False))}


def _capture_modal_dialog(app: Any, action: Any, *, expected_title: str,
                          text_markers: tuple[str, ...]) -> dict[str, Any]:
    from PySide6.QtWidgets import QMessageBox, QPlainTextEdit

    captured: dict[str, Any] = {}

    def inspect_and_close() -> None:
        dialog = app.activeModalWidget()
        if dialog is None:
            visible = [widget for widget in app.topLevelWidgets() if widget.isVisible()]
            dialog = next((widget for widget in visible
                           if expected_title in widget.windowTitle()), None)
        if dialog is None:
            return
        if isinstance(dialog, QMessageBox):
            text = dialog.text()
        else:
            viewer = dialog.findChild(QPlainTextEdit)
            text = viewer.toPlainText() if viewer is not None else ""
        captured.update({
            "title": dialog.windowTitle(),
            "dialog_type": type(dialog).__name__,
            "text_length": len(text),
            "text_sha256": _sha256_bytes(text.encode("utf-8")),
            "markers_present": {marker: marker in text for marker in text_markers},
        })
        dialog.accept()

    from PySide6.QtCore import QTimer
    QTimer.singleShot(0, inspect_and_close)
    action()
    if not captured:
        raise RuntimeError(f"Expected GUI dialog was not shown: {expected_title}")
    if any(not present for present in captured["markers_present"].values()):
        raise RuntimeError(f"GUI dialog omitted expected text: {expected_title}")
    return captured


def _exercise_about_licenses_and_help(app: Any, window: Any) -> dict[str, Any]:
    from PySide6.QtGui import QDesktopServices
    from pyeyedata.app_info import APP_VERSION, PRD_VERSION
    from pyeyedata.gui.main_window import _license_materials
    from pyeyedata.resource_paths import read_resource_bytes
    from pyeyedata_bootstrap import user_directory

    about = _capture_modal_dialog(
        app, window._show_about, expected_title="关于",
        text_markers=(APP_VERSION, PRD_VERSION, "数据契约版本", "分析流程 schema"),
    )
    license_material_paths = tuple(path for path, _ in _license_materials())
    licenses = _capture_modal_dialog(
        app, window._show_licenses, expected_title="许可与第三方声明",
        text_markers=license_material_paths,
    )
    licenses["material_paths"] = list(license_material_paths)

    opened: list[str] = []
    original_open_url = QDesktopServices.openUrl
    try:
        QDesktopServices.openUrl = staticmethod(
            lambda url: opened.append(url.toLocalFile()) or True
        )
        window._open_pdf_guide()
    finally:
        QDesktopServices.openUrl = original_open_url
    if len(opened) != 1:
        raise RuntimeError("Offline help did not open exactly one cached PDF")
    cached_path = Path(opened[0]).resolve(strict=True)
    expected = read_resource_bytes("help/PYeyedata-user-guide-zh.pdf")
    cached = cached_path.read_bytes()
    if cached != expected or not cached.startswith(b"%PDF-"):
        raise RuntimeError("Help PDF cache bytes differ from the bundled offline guide")
    cache_root = user_directory("cache", "viewed-artifacts").resolve(strict=True)
    if not cached_path.is_relative_to(cache_root):
        raise RuntimeError("Help PDF was not opened from the application cache")
    return {
        "about_dialog": about,
        "license_dialog": licenses,
        "help_pdf_cache": {
            "path": cached_path.name, "size_bytes": len(cached),
            "sha256": _sha256_bytes(cached), "matches_bundled_resource": True,
            "cache_location": str(cache_root),
        },
    }


def _run_pipeline_scenario(app: Any, window: Any, workspace: Path,
                           evidence: dict[str, Any]) -> None:
    import time
    import pandas as pd

    from PySide6.QtCore import QTimer
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from pyeyedata.archive_session import load_run_archive, save_run_archive
    from pyeyedata.exports import create_table_export_request, export_table, preview_table_export
    from pyeyedata.reporting import create_report_request, render_existing_figure_artifact, write_analysis_report
    from pyeyedata.gui.models import DatasetRevision, SelectionSpec

    dataset, raw_sources, import_evidence = _prepare_demo_dataset(workspace)
    evidence["import"] = import_evidence
    revision = DatasetRevision.create(dataset, raw_sources=raw_sources)
    window.set_dataset_revision(revision, preview=dataset.signals.head(50))
    selection = SelectionSpec(
        individual_metric_ids=("individual.usable_duration",), run_individual=True,
    )
    window.set_selection(selection)
    window.set_parameters({"metadata": {"time_confirmed": True}})
    task_events = 0

    def count_task_event(*_args: Any) -> None:
        nonlocal task_events
        task_events += 1

    window.task_manager.completed.connect(count_task_event)
    window.task_manager.failed.connect(count_task_event)

    preflight, failures = _run_gui_task(app, window, phase="preflight")
    if failures:
        raise RuntimeError("Synthetic individual preflight failed: " + failures[-1]["message"])
    if preflight is None or preflight.availability is None:
        raise RuntimeError("Synthetic individual preflight returned no typed availability")
    if preflight.backend_status != "complete":
        raise RuntimeError("Synthetic individual preflight was not complete: " + preflight.backend_status)
    individual_availability = preflight.availability
    selected_individual = individual_availability.loc[
        individual_availability["metric_id"].astype("string").eq("individual.usable_duration")
    ]
    if selected_individual.empty or not selected_individual["available"].fillna(False).astype(bool).any():
        raise RuntimeError("Selected individual usable-duration metric was not available in the completed preflight")
    evidence["preflight"] = {
        "status": preflight.status.value, "backend_status": preflight.backend_status,
        "availability_rows": len(individual_availability),
        "selected_metric": "individual.usable_duration",
        "selected_metric_available": True,
    }

    window.set_selection(selection)
    outcome, failures = _run_gui_task(app, window, phase="run")
    if failures:
        raise RuntimeError("Synthetic individual analysis failed: " + failures[-1]["message"])
    if outcome is None or outcome.payload is None or outcome.status.value not in {"complete", "partial"}:
        raise RuntimeError("Synthetic individual analysis returned no usable result bundle")
    record = next((item for item in reversed(window.session.history) if item.run_id == outcome.run_id), None)
    if record is None:
        raise RuntimeError("MainWindow did not preserve the synthetic individual run in session history")
    evidence["analysis"] = {
        "status": outcome.status.value, "backend_status": outcome.backend_status,
        "run_id": record.run_id, "history_count": len(window.session.history),
        "table_rows": {name: len(frame) for name, frame in record.tables().items()},
    }

    tab = window.results_page.individual_tab
    if tab.record is None or tab.record.run_id != record.run_id or not tab.chart.chart_available:
        raise RuntimeError("Individual results did not bind a real available GUI chart to the saved run")
    if not isinstance(tab.chart.canvas, FigureCanvasQTAgg):
        raise RuntimeError("Selected result chart is not a Qt-embedded Matplotlib canvas")
    tab.chart.canvas.draw()
    table_name, bound_positions = tab.source_binding()
    if table_name not in record.table_names() or not bound_positions:
        raise RuntimeError("Selected GUI result table is not bound to an actual RunRecord table")
    proxy_index = tab._proxy_model.index(0, 0)
    source_index = tab._proxy_model.mapToSource(proxy_index)
    if not source_index.isValid():
        raise RuntimeError("Selected GUI result row cannot be mapped to its source table")
    selected_position = bound_positions[source_index.row()]
    selected_exports = []
    for fmt in ("csv", "xlsx"):
        path = workspace / f"diagnostic-selected-{fmt}.{fmt}"
        request = create_table_export_request(
            record, table_name, path, file_format=fmt, scope="selected",
            source_positions=(int(selected_position),),
            filter_description="one real row selected in the individual results table",
        )
        preview = preview_table_export(record, request)
        result = export_table(record, request)
        if preview.row_count != 1 or result.rows_written != 1:
            raise RuntimeError(f"Selected {fmt.upper()} export did not preserve the one selected real row")
        selected_exports.append({"format": fmt, "path": path.name, "sha256": result.sha256,
                        "rows": result.rows_written, "preview_rows": preview.row_count})
    figure_result = render_existing_figure_artifact(
        record, tab.chart.figure, table_name=table_name,
        source_label="currently selected individual result figure",
        source_tables=(table_name,), selection=tab.figure_selection(),
        formats=("png", "svg", "pdf"), dpi=300,
    )
    figure_exports = []
    for fmt, content in figure_result.formats.items():
        path = workspace / f"diagnostic-selected-figure.{fmt}"
        if path.exists():
            raise RuntimeError("Figure export destination already exists")
        path.write_bytes(content)
        figure_exports.append({"format": fmt, "path": path.name, "size_bytes": len(content),
                               "sha256": _sha256_bytes(content),
                               "provenance_run_id": figure_result.provenance["run_id"]})
    report_path = workspace / "diagnostic-analysis-report.pdf"
    report_request = create_report_request(
        record, table_names=tuple(name for name in ("metrics", "availability") if name in record.table_names()),
        title="PYeyedata D03 diagnostic result report",
    )
    report = write_analysis_report(record, report_path, request=report_request)
    if not report_path.read_bytes().startswith(b"%PDF-"):
        raise RuntimeError("Selected-run report export did not create a PDF")
    evidence["exports"] = {
        "table_name": table_name,
        "selected_rows": 1,
        "selected_csv_xlsx": selected_exports,
        "selected_figure": figure_exports,
        "report_pdf": {"path": report_path.name, "size_bytes": report.byte_length,
                       "sha256": report.sha256, "run_id": record.run_id},
    }

    team_dataset, team_raw_sources, team_fixture = _prepare_team_dataset(workspace)
    team_revision = DatasetRevision.create(
        team_dataset, raw_sources=team_raw_sources,
        revision_id="diagnostic-six-disjoint-teams-v1",
    )
    metric_id = "group.windowed_synchrony"
    team_selection = SelectionSpec(group_metric_ids=(metric_id,), run_group=True)
    team_parameters = {
        "eye_selection": {"mode": "left", "representation": "direction"},
        "event_detection": {
            "fixation_method": "ivt", "saccade_method": "disabled",
            "velocity_threshold_deg_s": 45.0,
        },
        "group": {"metric_ids": [metric_id], "comparison_signal": "pupil_left",
                  "window_length_s": 0.4, "window_step_s": 0.2},
        "statistics_design": {
            "design_type": "team_independent", "outcome_type": "continuous", "analysis_unit": "team",
            "team_column": "interaction_id", "interaction_column": "interaction_id",
            "group_column": "condition", "independence_confirmed": True,
            "independence_note": "Six explicitly disjoint synthetic teams, three per condition; pair observations are aggregated to team means.",
            "options": {"aggregate_pairs": True, "team_members": team_fixture["teams"],
                        "metric_types": {metric_id: "continuous"}},
        },
        "metadata": {"time_confirmed": True, "device_verified": False},
    }
    window.set_dataset_revision(team_revision, preview=team_dataset.signals.head(50))
    group_index = window.group_page.mode_combo.findData("group")
    if group_index < 0:
        raise RuntimeError("Group analysis mode is not exposed by the GUI")
    window.group_page.mode_combo.setCurrentIndex(group_index)
    window.set_selection(team_selection)
    window.set_parameters(team_parameters)
    team_preflight, failures = _run_gui_task(app, window, phase="preflight")
    if failures or team_preflight is None or team_preflight.availability is None:
        raise RuntimeError("Synthetic team preflight failed: " + (failures[-1]["message"] if failures else "no typed availability"))
    if team_preflight.backend_status != "complete":
        raise RuntimeError("Synthetic team preflight was not complete: " + team_preflight.backend_status)
    team_availability = team_preflight.availability.loc[
        team_preflight.availability["metric_id"].astype("string").eq(metric_id)
        & team_preflight.availability["scope"].astype("string").eq("source_stream")
    ]
    if team_availability.empty or not team_availability["available"].fillna(False).astype(bool).any():
        raise RuntimeError("Six-team group metric was not available in preflight")
    outcome, failures = _run_gui_task(app, window, phase="run")
    if failures or outcome is None or outcome.status.value not in {"complete", "partial"}:
        raise RuntimeError("Synthetic legal-team analysis failed: " + (failures[-1]["message"] if failures else "no complete/partial outcome"))
    team_record = next((item for item in reversed(window.session.history) if item.run_id == outcome.run_id), None)
    if team_record is None:
        raise RuntimeError("MainWindow did not retain the legal-team run in history")
    if "group_windowed_synchrony" not in team_record.table_names():
        raise RuntimeError(
            "Legal-team run omitted the selected group_windowed_synchrony result table; "
            f"available tables: {', '.join(team_record.table_names())}"
        )
    synchrony = team_record.table_copy("group_windowed_synchrony")
    if len(synchrony) != 540:
        raise RuntimeError(f"Six-team fixture produced {len(synchrony)} group observations; expected 540")
    team_metrics = team_record.table_copy("metrics")
    selected_team_metrics = team_metrics.loc[team_metrics["metric_id"].astype("string").eq(metric_id)]
    if selected_team_metrics.empty or not selected_team_metrics["status"].astype("string").eq("complete").all():
        raise RuntimeError("Legal-team comparison returned incomplete windowed synchrony metric rows")
    saved_design = team_record.snapshot.parameter_values().get("statistics_design", {})
    if (saved_design.get("design_type") != "team_independent"
            or saved_design.get("analysis_unit") != "team"
            or saved_design.get("independence_confirmed") is not True
            or saved_design.get("team_column") != "interaction_id"):
        raise RuntimeError("Team analysis snapshot did not preserve its explicit independent-team design")
    evidence["team_fixture"] = team_fixture
    evidence["team_preflight"] = {
        "backend_status": team_preflight.backend_status,
        "selected_metric": metric_id, "selected_metric_available": True,
        "availability_rows": len(team_preflight.availability),
    }
    evidence["legal_team_analysis"] = {
        "run_id": team_record.run_id, "status": outcome.status.value,
        "backend_status": outcome.backend_status,
        "design_type": saved_design["design_type"], "analysis_unit": saved_design["analysis_unit"],
        "independence_confirmed": saved_design["independence_confirmed"],
        "teams_per_condition": {"A": 3, "B": 3},
        "selected_metric_rows": len(selected_team_metrics),
        "group_observations": len(synchrony),
        "windowed_synchrony_rows": len(synchrony),
        "history_count": len(window.session.history),
        "table_rows": {name: len(frame) for name, frame in team_record.tables().items()},
    }

    evidence["gui_help_checks"] = _exercise_about_licenses_and_help(app, window)

    archives = []
    full_archive = workspace / "diagnostic-full.pyea"
    results_archive = workspace / "diagnostic-results_only.pyea"
    archive_baselines: dict[str, dict[str, Any]] = {}
    for mode, path in (("full", full_archive), ("results_only", results_archive)):
        save_run_archive(path, team_record, mode=mode)
        loaded = load_run_archive(path)
        expected = _write_archive_expectation(path, team_record, loaded)
        archive_baselines[mode] = expected
        archives.append({"mode": mode, "path": path.name, "size_bytes": path.stat().st_size,
                         "sha256": _sha256_bytes(path.read_bytes()),
                         "diagnostic_expected": expected["path"],
                         "expected_tables": len(expected["table_hashes"]),
                         "recomputable": expected["recomputable"]})
    evidence["archives"] = archives
    evidence["archive_comparison_baselines"] = {
        mode: {key: value for key, value in baseline.items() if key != "table_hashes"}
        | {"table_count": len(baseline["table_hashes"])}
        for mode, baseline in archive_baselines.items()
    }
    if not archive_baselines["full"]["recomputable"] or archive_baselines["results_only"]["recomputable"]:
        raise RuntimeError("Full/results-only recomputability labels did not match their saved contents")

    from pyeyedata.resource_paths import read_resource_bytes
    evidence["resources"] = [
        _resource_record("help/PYeyedata-user-guide-zh.pdf"),
        _resource_record("licenses/THIRD-PARTY-NOTICES.md"),
        _resource_record("licenses/LGPL-3.0.txt"),
        _resource_record("fonts/OFL.txt"),
    ]
    read_resource_bytes("help/PYeyedata-user-guide-zh.pdf")
    read_resource_bytes("licenses/THIRD-PARTY-NOTICES.md")

    full_roundtrip = _same_process_archive_roundtrip(app, window, full_archive, archive_baselines["full"])
    task_events_before_results_only = task_events
    results_roundtrip = _same_process_archive_roundtrip(
        app, window, results_archive, archive_baselines["results_only"],
    )
    if task_events != task_events_before_results_only or window.task_manager.active:
        raise RuntimeError("Opening a results-only archive started or overlapped a pipeline task")
    if results_roundtrip["recomputable"] is not False:
        raise RuntimeError("Results-only GUI reopen incorrectly claimed recomputability")
    evidence["same_process_archive_reopen"] = {
        "full": full_roundtrip,
        "results_only": results_roundtrip,
        "results_only_no_recompute": {
            "pipeline_task_events_before": task_events_before_results_only,
            "pipeline_task_events_after": task_events,
            "new_pipeline_task_started": False,
            "archive_mode": "results_only",
            "recomputable": False,
        },
        "history_count": len(window.session.history),
        "fresh_process_reopen_scenario": "open-archive",
    }

    # Restore the legal-team input and snapshot before the lifecycle checks;
    # the missing-ID preflight below is deliberately a separate revision.
    window.set_dataset_revision(team_revision, preview=team_dataset.signals.head(50))
    window.group_page.mode_combo.setCurrentIndex(window.group_page.mode_combo.findData("group"))
    window.set_selection(team_selection)
    window.set_parameters(team_parameters)
    original_run = window.bridge.run
    cancellation_requested: list[bool] = []

    def wait_for_cancel(snapshot: Any, *, cancel=None, progress=None, reuse_plan=None):
        while cancel is not None and not cancel():
            time.sleep(0.005)
        return original_run(snapshot, cancel=cancel, progress=progress, reuse_plan=reuse_plan)

    window.bridge.run = wait_for_cancel
    QTimer.singleShot(25, lambda: cancellation_requested.append(window.task_manager.cancel()))
    try:
        cancelled, failures = _run_gui_task(app, window, phase="run")
    finally:
        window.bridge.run = original_run
    if failures or cancelled is None or cancelled.status.value != "cancelled" or cancellation_requested != [True]:
        raise RuntimeError("GUI task cancellation did not produce a retained cancelled outcome")
    cancelled_record = next((item for item in reversed(window.session.history)
                             if item.run_id == cancelled.run_id), None)
    if cancelled_record is None or cancelled_record.status != "cancelled":
        raise RuntimeError("Cancelled task was not preserved in read-only history")

    def controlled_failure(snapshot: Any, *, cancel=None, progress=None, reuse_plan=None):
        raise RuntimeError("diagnostic controlled task failure")

    window.bridge.run = controlled_failure
    try:
        failed_outcome, failures = _run_gui_task(app, window, phase="run")
    finally:
        window.bridge.run = original_run
    if failed_outcome is not None or not failures or failures[-1]["type"] != "RuntimeError":
        raise RuntimeError("Controlled GUI task failure did not reach the task failure signal")
    failed_record = window.session.history[-1] if window.session.history else None
    if failed_record is None or failed_record.status != "failed":
        raise RuntimeError("Controlled failed task was not preserved in read-only history")
    retained_ids = {item.run_id for item in window.session.history}
    if record.run_id not in retained_ids or team_record.run_id not in retained_ids:
        raise RuntimeError("Task cancellation/failure replaced an earlier successful history record")
    evidence["task_lifecycle"] = {
        "cancelled": {"run_id": cancelled_record.run_id, "status": cancelled_record.status,
                      "history_retained": True, "cancel_requested": cancellation_requested[0]},
        "controlled_failure": {"run_id": failed_record.run_id, "status": failed_record.status,
                               "error_type": failures[-1]["type"],
                               "controlled_message_present": "diagnostic controlled task failure" in failures[-1]["message"],
                               "history_retained": True},
        "successful_individual_run_retained": record.run_id in retained_ids,
        "successful_legal_team_run_retained": team_record.run_id in retained_ids,
        "history_count": len(window.session.history),
    }

    blocked_dataset = team_revision.dataset_copy()
    blocked_dataset.signals["interaction_id"] = pd.NA
    # The stable observation identity includes optional interaction scope.
    # Rebuild that identity after removing the scope; never fill or infer the
    # missing interaction IDs being tested here.
    from pyeyedata.scopes import observation_identity_frame
    blocked_dataset.signals, _ = observation_identity_frame(
        blocked_dataset.signals.drop(columns=["observation_id"])
    )
    blocked_revision = DatasetRevision.create(blocked_dataset, revision_id="diagnostic-missing-interaction-id-v1")
    window.set_dataset_revision(blocked_revision, preview=blocked_dataset.signals.head(50))
    window.group_page.mode_combo.setCurrentIndex(window.group_page.mode_combo.findData("group"))
    window.set_selection(team_selection)
    window.set_parameters(team_parameters)
    blocked_preflight, failures = _run_gui_task(app, window, phase="preflight")
    if failures or blocked_preflight is None or blocked_preflight.availability is None:
        raise RuntimeError("Missing-interaction group preflight failed before returning blocked availability")
    blocked_rows = blocked_preflight.availability.loc[
        blocked_preflight.availability["metric_id"].astype("string").eq(metric_id)
    ]
    if (blocked_rows.empty
            or not blocked_rows["status"].astype("string").eq("blocked").all()
            or not blocked_rows["reason_code"].astype("string").eq("explicit_interaction_id_required").all()):
        raise RuntimeError("Missing explicit interaction_id did not block the selected group method")
    evidence["blocked_group_preflight"] = {
        "backend_status": blocked_preflight.backend_status,
        "selected_metric": metric_id,
        "status": "blocked",
        "reason_code": "explicit_interaction_id_required",
        "rows": len(blocked_rows),
        "no_ids_inferred": True,
    }
    window.task_manager.completed.disconnect(count_task_event)
    window.task_manager.failed.disconnect(count_task_event)


def _run_open_archive(app: Any, window: Any, config: dict[str, Any],
                      evidence: dict[str, Any]) -> None:
    import json

    archive = _absolute_path(config["archive_path"], "archive_path")
    loaded: list[Any] = []
    failures: list[str] = []
    on_completed = lambda _generation, result: loaded.append(result)
    on_failed = lambda _generation, details: failures.append(str(details))
    window.operation_manager.completed.connect(on_completed)
    window.operation_manager.failed.connect(on_failed)
    window.open_run_archive(archive)
    try:
        _wait_for_idle(app, window.operation_manager)
    finally:
        window.operation_manager.completed.disconnect(on_completed)
        window.operation_manager.failed.disconnect(on_failed)
    if failures or not loaded or not window.session.history:
        raise RuntimeError("Archive reopen failed: " + (failures[-1] if failures else "no restored record"))
    result = loaded[-1]
    record = result.record
    expected_path = Path(str(archive) + ".diagnostic-expected.json")
    exact_comparison: dict[str, Any]
    if expected_path.is_file():
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        if expected.get("schema") != "pyeyedata.diagnostic-archive-baseline/v1":
            raise RuntimeError("Archive comparison sidecar has an unsupported diagnostic schema")
        actual = _record_archive_facts(record, result.app_archive, result.analysis.archive)
        mismatches = [key for key in (
            "mode", "run", "table_hashes", "audit_table_hashes", "metadata_audit_sha256",
            "version_facts", "runtime_sha256", "recomputable", "snapshot_schema_version",
        ) if actual.get(key) != expected.get(key)]
        if mismatches:
            raise RuntimeError("Fresh-process archive comparison differs in: " + ", ".join(mismatches))
        exact_comparison = {
            "status": "pass", "sidecar": expected_path.name,
            "sidecar_sha256": _sha256_bytes(expected_path.read_bytes()),
            "table_count": len(actual["table_hashes"]), "table_hashes_match": True,
            "audit_facts_match": True, "version_facts_match": True,
            "runtime_facts_sha256": actual["runtime_sha256"],
            "mode": actual["mode"], "recomputable": actual["recomputable"],
        }
    else:
        exact_comparison = {
            "status": "not-provided",
            "reason": "Archive has no diagnostic expectation sidecar; exact fresh-process comparison was not claimed.",
        }
    evidence["archive_reopen"] = {
        "path": archive.name, "mode": result.archive_mode,
        "run_id": record.run_id, "status": record.status,
        "table_rows": {name: len(frame) for name, frame in record.tables().items()},
        "app_archive": dict(record.metadata().get("app_archive", {})),
        "exact_fresh_process_comparison": exact_comparison,
        "fresh_process_required_for_release_proof": True,
    }


def _run_reliability_smoke(app: Any, window: Any, workspace: Path,
                           evidence: dict[str, Any]) -> None:
    """Exercise the fixed 0.9.1 reliability paths through product-owned APIs."""
    import pandas as pd

    from pyeyedata.contracts import AnalysisBundle
    from pyeyedata.exports import create_table_export_request, export_table
    from pyeyedata.gui.models import RunRecord, RunSnapshot
    from pyeyedata.gui.presets import load_custom_preset, save_custom_preset
    from pyeyedata.processing import ProcessingConfig, ProcessingError
    from PySide6.QtWidgets import QDialog

    input_path = workspace / "reliability-import.csv"
    pd.DataFrame({
        "subject_id": ["synthetic-r01", "synthetic-r01", "synthetic-r01"],
        "session_id": ["session-1"] * 3,
        "time_s": [0.0, 0.01, 0.02],
        "gaze_x": [1.0, 2.0, 3.0],
        "gaze_y": [2.0, 3.0, 4.0],
    }).to_csv(input_path, index=False)

    page = window.import_page
    panel = page.sources["signals"]
    panel.path_edit.setText(str(input_path))
    previews: list[Any] = []
    preview_failures: list[str] = []

    def on_preview(role: str, frame: Any) -> None:
        if role == "signals":
            previews.append(frame)

    def on_preview_failed(role: str, message: str) -> None:
        if role == "signals":
            preview_failures.append(message)

    panel.preview_loaded.connect(on_preview)
    panel.preview_failed.connect(on_preview_failed)
    panel.request_preview()
    _wait_for_condition(
        app, lambda: bool(previews or preview_failures), "fixed reliability import preview",
    )
    panel.preview_loaded.disconnect(on_preview)
    panel.preview_failed.disconnect(on_preview_failed)
    if preview_failures or not previews or panel.mapping is None:
        raise RuntimeError("Fixed reliability import preview failed: " + (preview_failures[-1] if preview_failures else "no preview"))

    for field_name in ("subject_id", "session_id", "time_s", "gaze_x", "gaze_y"):
        row = next((index for index, spec in enumerate(panel.mapping.fields) if spec.name == field_name), None)
        if row is None:
            raise RuntimeError(f"Fixed reliability fixture has no mapping field {field_name}")
        control = panel.mapping._table.cellWidget(row, 2)
        selected = control.findData(field_name)
        if selected < 0:
            raise RuntimeError(f"Fixed reliability fixture cannot map {field_name}")
        control.setCurrentIndex(selected)

    coordinate = page.coordinate
    time_index = coordinate.time_unit.findData("s")
    if time_index < 0:
        raise RuntimeError("Time-in-seconds option is missing from the import page")
    coordinate.time_unit.setCurrentIndex(time_index)
    coordinate.time_confirmed.setChecked(True)
    coordinate.precision.setValue(0.01)
    coordinate.coordinate_unit.setText("px")
    coordinate.coordinate_frame.setText("synthetic_top_left")

    imported: list[tuple[Any, Any]] = []
    import_failures: list[str] = []

    def on_import_completed(revision: Any, preview: Any) -> None:
        imported.append((revision, preview))

    def on_import_failed(message: str) -> None:
        import_failures.append(str(message))

    page.import_completed.connect(on_import_completed)
    page.import_failed.connect(on_import_failed)
    page.start_import()
    _wait_for_condition(app, lambda: not page.import_active, "initial real reliability import")
    if import_failures or len(imported) != 1:
        raise RuntimeError("Initial real reliability import failed: " + (import_failures[-1] if import_failures else "no import result"))
    prior_revision, prior_preview = imported[0]
    if (window.session.dataset_revision is None
            or window.session.dataset_revision.revision_id != prior_revision.revision_id
            or len(prior_preview) != 3):
        raise RuntimeError("Initial import did not install its current three-row revision")

    quality = window.quality_page
    quality.set_processing_config({"baseline_min_length_s": 0.0})
    valid_config = quality.processing_config()
    if valid_config["baseline_min_length_s"] != 0.0:
        raise RuntimeError("GUI processing controls did not preserve baseline_min_length_s=0")
    try:
        ProcessingConfig(gaze_max_gap_s=float("nan"))
    except ProcessingError as exc:
        invalid_core_message = str(exc)
    else:
        raise RuntimeError("ProcessingConfig accepted a non-finite gaze interval")
    try:
        window.set_parameters({"processing": {"gaze_max_gap_s": float("nan")}})
    except (ProcessingError, TypeError, ValueError, OverflowError) as exc:
        invalid_gui_message = str(exc)
    else:
        raise RuntimeError("MainWindow accepted a non-finite processing parameter")
    if quality.processing_config() != valid_config:
        raise RuntimeError("Rejected processing parameters partially changed the GUI controls")

    selection = window.metrics_page.selection_spec()
    parameters, context, scoped_revision = window._snapshot_configuration(selection)
    schema_version = str(window.bridge.schema()["pipeline_schema_version"])
    snapshot = RunSnapshot.create(
        scoped_revision, generation=1, selection=selection, parameters=parameters,
        context=context, schema_version=schema_version,
    )
    snapshot_processing = snapshot.parameter_values().get("processing", {})
    if snapshot_processing.get("baseline_min_length_s") != 0.0:
        raise RuntimeError("Legal MainWindow snapshot did not preserve baseline_min_length_s=0")

    metrics = window.metrics_page
    original_parameters = metrics.all_parameter_values()
    custom_preset_path = workspace / "reliability-custom-preset.json"
    preset_parameters = {
        "individual": {
            "calibration_window_s": (0.2, 0.8),
            "multimatch.grouping": False,
            "reading_direction": "rtl",
            "scanmatch.substitution_matrix": {"A": 1.0, "B": -0.5},
        },
    }
    metrics.set_parameter_values(preset_parameters, emit=False, strict=True)
    save_custom_preset(
        custom_preset_path,
        name="Reliability smoke",
        selections=metrics._current_selections(),
        parameters=metrics.all_parameter_values(),
        schema=window.bridge.schema(),
    )
    loaded_preset = load_custom_preset(custom_preset_path)
    serialized_window = loaded_preset.parameters["individual"]["calibration_window_s"]
    prepared_preset = metrics._normalize_parameter_values(loaded_preset.parameters, strict=True)
    restored_window = prepared_preset["individual"]["calibration_window_s"]
    if (not isinstance(serialized_window, list)
            or restored_window != (0.2, 0.8)
            or type(prepared_preset["individual"]["multimatch.grouping"]) is not bool
            or prepared_preset["individual"]["multimatch.grouping"] is not False
            or prepared_preset["individual"]["reading_direction"] != "rtl"
            or prepared_preset["individual"]["scanmatch.substitution_matrix"] != {"A": 1.0, "B": -0.5}):
        raise RuntimeError("Custom preset JSON round-trip did not preserve tuple, boolean, enum, or typed mapping values")

    # Exercise the real preset apply path while accepting its review dialog in
    # this headless smoke; all validation and widget mutation stays product-owned.
    import pyeyedata.gui.metrics_page as metrics_page_module

    class _AcceptPresetDiff:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def exec(self) -> Any:
            return QDialog.DialogCode.Accepted

    original_diff_dialog = metrics_page_module._PresetDiffDialog
    metrics_page_module._PresetDiffDialog = _AcceptPresetDiff
    metrics_signals_were_blocked = metrics.blockSignals(True)
    try:
        if not metrics._apply_preset(loaded_preset):
            raise RuntimeError("A valid custom preset was not applied")
    finally:
        metrics.blockSignals(metrics_signals_were_blocked)
        metrics_page_module._PresetDiffDialog = original_diff_dialog
    applied_preset_values = metrics.all_parameter_values()
    if (applied_preset_values["individual"]["calibration_window_s"] != (0.2, 0.8)
            or applied_preset_values["individual"]["multimatch"]["grouping"] is not False
            or applied_preset_values["individual"]["reading_direction"] != "rtl"
            or applied_preset_values["individual"]["scanmatch"]["substitution_matrix"] != {"A": 1.0, "B": -0.5}):
        raise RuntimeError("Applying a loaded custom preset changed a typed value or its Python type")

    rejected_preset_inputs: list[str] = []
    for label, invalid_parameters in (
        ("unknown-empty-namespace", {"individual": {"future_namespace": {}}}),
        ("malformed-float-mapping", {"individual": {
            "multimatch.grouping": True,
            "scanmatch.substitution_matrix": {"A": "not-a-float"},
        }}),
    ):
        try:
            metrics.set_parameter_values(invalid_parameters, emit=False, strict=True)
        except (TypeError, ValueError):
            rejected_preset_inputs.append(label)
        else:
            raise RuntimeError(f"Preset input {label} was accepted")
        if metrics.all_parameter_values() != applied_preset_values:
            raise RuntimeError(f"Rejected preset input {label} partially changed parameter controls")

    group_page = window.group_page
    original_group_ids = group_page._selected_group_metric_ids or ()
    original_group_mode = group_page.mode_combo.currentData()
    group_page.set_group_metric_ids(("group.lagged_correlation",))
    group_page.mode_combo.setCurrentIndex(group_page.mode_combo.findData("individual"))
    lag_step = group_page._forms["lag"].editors["lag_step_s"]
    lag_step._enabled.setChecked(True)
    lag_step._editor.setText("-")
    if group_page.parameter_tabs.isEnabled():
        raise RuntimeError("Group parameter editors remained enabled in individual mode")
    group_page.set_group_metric_ids(("group.windowed_synchrony",))
    group_page.set_group_metric_ids(("group.lagged_correlation",))
    preserved_lag_step = group_page._forms["lag"].editors["lag_step_s"]
    group_draft_preserved = (preserved_lag_step._editor.text() == "-"
                             and preserved_lag_step._enabled.isChecked())
    if not group_draft_preserved:
        raise RuntimeError("Selection changes discarded an incomplete disabled group parameter draft")
    if group_page.configuration_values(include_group=False) != {"group": {}, "metadata": {}}:
        raise RuntimeError("An inactive group draft blocked an individual-only configuration")
    group_page.mode_combo.setCurrentIndex(group_page.mode_combo.findData("group"))
    try:
        group_page.configuration_values(include_group=True)
    except (TypeError, ValueError):
        active_group_draft_rejected = True
    else:
        raise RuntimeError("Enabling group analysis accepted the malformed active group parameter draft")
    preserved_lag_step._enabled.setChecked(False)
    preserved_lag_step._editor.clear()
    group_page.set_group_metric_ids(original_group_ids)
    group_page.mode_combo.setCurrentIndex(group_page.mode_combo.findData(original_group_mode))
    metrics.set_parameter_values(original_parameters, emit=False, strict=True)

    cancellation: list[dict[str, Any]] = []

    def cancel_after_worker_progress(percent: int, message: str) -> None:
        if message == "读取与规范化：主要信号" and not cancellation:
            worker_generation = page._worker_generation
            was_active = page.import_active
            accepted = page.cancel_import()
            cancellation.append({
                "percent": int(percent), "message": message,
                "worker_generation": worker_generation,
                "request_generation_after_cancel": page._request_generation,
                "was_active": was_active, "cancel_accepted": bool(accepted),
            })

    published_before_cancel = len(imported)
    page.import_progress.connect(cancel_after_worker_progress)
    page.start_import()
    _wait_for_condition(app, lambda: not page.import_active, "cancelled reliability import completion")
    page.import_progress.disconnect(cancel_after_worker_progress)
    if not cancellation or not cancellation[0]["was_active"] or not cancellation[0]["cancel_accepted"]:
        raise RuntimeError("Real import worker progress did not accept the fixed cooperative cancellation")
    if (cancellation[0]["request_generation_after_cancel"] == cancellation[0]["worker_generation"]
            or len(imported) != published_before_cancel
            or page._last_revision is None
            or page._last_revision.revision_id != prior_revision.revision_id
            or window.session.dataset_revision is None
            or window.session.dataset_revision.revision_id != prior_revision.revision_id):
        raise RuntimeError("Cancelled import published a stale result or discarded the prior valid revision")

    bundle = AnalysisBundle(
        tables={"diagnostic_reliability": pd.DataFrame({"value": [3.3], "unit": ["mm"]})},
        metadata={"execution_status": "complete"},
    )
    record = RunRecord.from_bundle(snapshot, bundle)
    export_path = workspace / "reliability-committed.csv"
    request = create_table_export_request(
        record, "diagnostic_reliability", export_path, file_format="csv",
    )
    operation_results: list[tuple[int, Any]] = []
    operation_failures: list[str] = []
    operation_cancellations: list[int] = []
    window.operation_manager.completed.connect(
        lambda generation, result: operation_results.append((generation, result))
    )
    window.operation_manager.failed.connect(
        lambda generation, message: operation_failures.append(f"{generation}: {message}")
    )
    window.operation_manager.cancelled.connect(operation_cancellations.append)

    def export_then_request_late_cancel(token: Any, progress: Any) -> Any:
        result = export_table(record, request, cancel=token, progress=progress)
        token.cancel()
        return result

    window._start_operation("export", export_then_request_late_cancel)
    _wait_for_condition(
        app, lambda: not window.operation_manager.active, "committed reliability CSV export",
    )
    if operation_failures or operation_cancellations or len(operation_results) != 1:
        raise RuntimeError("Committed export was not delivered as success: " + "; ".join(operation_failures))
    export_result = operation_results[0][1]
    if (export_result.commit_complete is not True
            or export_result.path != export_path
            or not export_path.is_file()
            or not export_result.audit_path.is_file()
            or _sha256_bytes(export_path.read_bytes()) != export_result.sha256):
        raise RuntimeError("Committed export receipt does not match the promoted CSV bytes")
    if str(export_path) not in window.status_label.text():
        raise RuntimeError("MainWindow did not preserve the committed export destination in its success status")

    evidence["reliability_smoke"] = {
        "status": "pass",
        "parameter_validation": {
            "nonfinite_core_parameter_rejected": True,
            "nonfinite_gui_parameter_rejected": True,
            "error": invalid_gui_message,
            "core_error": invalid_core_message,
            "rejected_parameter_left_controls_unchanged": True,
            "valid_snapshot_baseline_min_length_s": snapshot_processing["baseline_min_length_s"],
            "snapshot_fingerprint": snapshot.config_fingerprint,
        },
        "custom_preset_roundtrip": {
            "json_tuple_serialized_as_array": isinstance(serialized_window, list),
            "tuple_restored_from_live_schema": restored_window == (0.2, 0.8),
            "boolean_false_preserved": prepared_preset["individual"]["multimatch.grouping"] is False,
            "enum_preserved": prepared_preset["individual"]["reading_direction"] == "rtl",
            "typed_mapping_preserved": prepared_preset["individual"]["scanmatch.substitution_matrix"] == {"A": 1.0, "B": -0.5},
            "loaded_preset_applied": True,
            "applied_values_preserved": True,
            "invalid_inputs_rejected_before_mutation": rejected_preset_inputs,
        },
        "disabled_group_draft": {
            "selection_change_preserved_raw_text": group_draft_preserved,
            "inactive_group_configuration_accepted": True,
            "active_group_configuration_rejected_invalid_value": active_group_draft_rejected,
        },
        "import_lifecycle": {
            "normalization_api_import_rows": len(prior_preview),
            "cancelled_from_real_worker_progress": cancellation[0],
            "public_result_count_before_cancel": published_before_cancel,
            "public_result_count_after_cancel": len(imported),
            "prior_revision_retained": prior_revision.revision_id,
            "stale_result_not_published": len(imported) == published_before_cancel,
        },
        "committed_export": {
            "format": "csv",
            "operation_manager_result": "completed",
            "late_cancel_requested_after_export_return": True,
            "commit_complete": export_result.commit_complete,
            "destination": export_result.path.name,
            "sha256": export_result.sha256,
            "audit_sidecar": export_result.audit_path.name,
            "success_status_contains_destination": True,
        },
    }


def _run_usability_smoke(app: Any, window: Any, workspace: Path, evidence: dict[str, Any]) -> None:
    """Fixed beginner workflow, plan transaction and real archive/close behavior."""
    from copy import deepcopy
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QPushButton, QToolBar, QMessageBox
    from .analysis_plan import read_plan, write_plan
    from .help_center import export_learning_pack, input_instructions, INPUT_ROLES, make_help_dialog
    from pyeyedata.archive_session import prepare_run_archive
    from pyeyedata.resource_paths import read_resource_bytes

    pack = export_learning_pack(workspace)
    if not all("```csv" in input_instructions(role) and "常见错误" in input_instructions(role)
               for role in INPUT_ROLES):
        raise RuntimeError("An input role lacks its worked example or repair instructions")
    help_dialog = make_help_dialog(window)
    help_dialog.show(); app.processEvents()
    if help_dialog.help_tabs.count() != 3:
        raise RuntimeError("The beginner help chapters are incomplete")
    opened = []
    messages = []
    original_open = QDesktopServices.openUrl
    original_information = QMessageBox.information
    try:
        QDesktopServices.openUrl = staticmethod(lambda url: opened.append(Path(url.toLocalFile())) or True)
        toolbar = next(bar for bar in window.findChildren(QToolBar) if window.pdf_guide_action in bar.actions())
        toolbar.widgetForAction(window.pdf_guide_action).click()
        pdf_button = help_dialog.findChild(QPushButton, "pdfGuideButton")
        if pdf_button is None:
            raise RuntimeError("Beginner help lacks the PDF button")
        pdf_button.click()
        pdf = read_resource_bytes("help/PYeyedata-user-guide-zh.pdf")
        if len(opened) != 2 or any(path.read_bytes() != pdf for path in opened):
            raise RuntimeError("PDF buttons did not open the actual bundled guide")
        QDesktopServices.openUrl = staticmethod(lambda _url: False)
        QMessageBox.information = staticmethod(lambda _parent, title, message: messages.append(str(message)))
        pdf_button.click()
        if len(messages) != 1 or str(opened[0]) not in messages[0]:
            raise RuntimeError("Missing-viewer message lacks the cached PDF path")
    finally:
        QDesktopServices.openUrl = original_open
        QMessageBox.information = original_information
    help_dialog.grab().save(str(workspace/"beginner-help.png"))
    help_dialog.close()
    # Short windows must scroll, never collapse form rows into a few pixels.
    window.resize(1080, 700)
    window.step_list.setCurrentRow(1)
    app.processEvents()
    q = window.quality_page
    if q.use_lowpass.height() < q.use_lowpass.minimumSizeHint().height():
        raise RuntimeError("Processing controls are vertically clipped")
    if window.quality_page_scroll.widget() is not q:
        raise RuntimeError("Processing page is not reachable by scrolling")
    window.step_list.setCurrentRow(3)
    window.group_page.tabs.setCurrentIndex(2)
    app.processEvents()
    g = window.group_page
    if g.sync_confirmed.height() < g.sync_confirmed.minimumSizeHint().height():
        raise RuntimeError("Group evidence controls are vertically clipped")
    if window.group_page_scroll.widget() is not g:
        raise RuntimeError("Group evidence is not reachable by scrolling")
    window.step_list.setCurrentRow(0)
    practice_plan = read_plan(pack/"pupil-mean-practice.pyeplan")
    window.apply_analysis_plan(practice_plan)
    saved_plan = workspace/"saved-practice.pyeplan"
    write_plan(saved_plan, window.analysis_plan("合成练习方案往返"))
    window.apply_analysis_plan(read_plan(saved_plan))
    before = window.analysis_plan()["configuration"]
    generation = window.session.generation
    bad = deepcopy(practice_plan)
    bad["configuration"]["statistics_design"]["options"]["fit_method"] = "INVALID"
    try:
        window.apply_analysis_plan(bad)
    except (TypeError, ValueError):
        pass
    else:
        raise RuntimeError("An invalid late design field was applied")
    if window.analysis_plan()["configuration"] != before or window.session.generation != generation:
        raise RuntimeError("Rejected plan changed settings or session state")

    panel = window.import_page.sources["signals"]
    previewed, preview_failed = [], []
    panel.preview_loaded.connect(lambda role, frame: previewed.append(frame))
    panel.preview_failed.connect(lambda role, error: preview_failed.append(error))
    panel.path_edit.setText(str(pack/"signals-practice.csv"))
    panel.request_preview()
    _wait_for_condition(app, lambda: bool(previewed or preview_failed), "practice preview")
    if preview_failed or panel.mapping is None:
        raise RuntimeError("Practice preview failed: " + str(preview_failed))
    for name in ("subject_id", "session_id", "time_s", "pupil_left", "pupil_left_valid"):
        row = next(i for i, spec in enumerate(panel.mapping.fields) if spec.name == name)
        control = panel.mapping._table.cellWidget(row, 2)
        control.setCurrentIndex(control.findData(name))
        if name == "pupil_left":
            panel.mapping._table.cellWidget(row, 5).setCurrentText("mm")
    coordinate = window.import_page.coordinate
    coordinate.time_unit.setCurrentIndex(coordinate.time_unit.findData("s"))
    coordinate.time_confirmed.setChecked(True)
    coordinate.pupil_left_unit.setText("mm")
    failures = []
    window.import_page.import_failed.connect(failures.append)
    window.import_page.start_import()
    _wait_for_condition(app, lambda: not window.import_page.import_active, "practice import")
    if failures or window.session.dataset_revision is None:
        raise RuntimeError("Practice import failed: " + str(failures) + "; " + window.import_page.status.text())
    revision = window.session.dataset_revision
    if len(revision.dataset_copy().signals) != 201:
        raise RuntimeError("Practice import did not retain all 201 rows")
    outcome, failures = _run_gui_task(app, window, phase="preflight")
    if failures or outcome is None:
        raise RuntimeError("Practice preflight failed: " + str(failures))
    outcome, failures = _run_gui_task(app, window, phase="run")
    if failures or outcome is None or len(window.session.history) != 1:
        raise RuntimeError("Practice analysis failed: " + str(failures))
    record = window.session.history[0]
    metrics = record.tables()["metrics"]
    row = metrics.loc[metrics["metric_id"] == "individual.pupil_left.mean"]
    if len(row) != 1 or row.iloc[0]["status"] != "complete" or abs(float(row.iloc[0]["value"])-3.3) > 1e-10:
        raise RuntimeError("Worked example did not produce the documented 3.3 mm metric")
    chart = window.results_page.individual_tab.chart
    if chart._unit(metrics, "n_observed") != "count" or chart._unit(metrics, "time_s") != "s" or chart._unit(metrics, "value") != "mm":
        raise RuntimeError("Result axis units confuse sample counts, time and metric values")
    # A valid plan affects configuration, never imported data or prior records.
    window.apply_analysis_plan(read_plan(saved_plan))
    if window.session.dataset_revision is not revision or window.session.history != (record,):
        raise RuntimeError("Loading a plan replaced data or history")
    if window.unarchived_records() != (record,):
        raise RuntimeError("A plan save was incorrectly treated as a run archive")
    window._confirm_unarchived_close = lambda _records: "cancel"
    close_event = QCloseEvent()
    window.closeEvent(close_event)
    if close_event.isAccepted() or not window.isVisible():
        raise RuntimeError("Returning from the unsaved warning did not keep the window open")

    archive = workspace/"practice-run.pyea"
    prepared = prepare_run_archive(record, mode="full")
    window._prepared_archive = prepared
    window._archive_saving_record = record
    window._archive_destination = str(archive)
    operation_failures = []
    window.operation_manager.failed.connect(lambda _generation, details: operation_failures.append(details))
    window._start_operation("save", lambda cancel, progress: prepared.commit(archive, cancel=cancel, progress=progress))
    _wait_for_condition(app, lambda: not window.operation_manager.active, "practice archive commit")
    if operation_failures or not archive.is_file() or window.unarchived_records():
        raise RuntimeError("Committed archive was not attributed to its original record")
    window.open_run_archive(archive)
    _wait_for_condition(app, lambda: not window.operation_manager.active, "practice archive reopen")
    if operation_failures or len(window.session.history) != 2 or window.unarchived_records():
        raise RuntimeError("Reopened archive was incorrectly counted as unsaved")
    window.grab().save(str(workspace/"practice-results.png"))
    evidence["usability_smoke"] = {
        "status": "pass", "input_roles_with_examples": list(INPUT_ROLES),
        "learning_pack_file_count": len(list(pack.iterdir())), "practice_import_rows": 201,
        "metric_id": "individual.pupil_left.mean", "metric_value": float(row.iloc[0]["value"]),
        "metric_status": str(row.iloc[0]["status"]), "overall_run_status": record.status,
        "plan_file_roundtrip": True, "invalid_late_field_left_settings_and_generation_unchanged": True,
        "plan_load_kept_data_and_history": True, "plan_save_did_not_archive_results": True,
        "cancel_close_kept_window": True, "committed_archive_marked_original_record": True,
        "reopened_archive_not_unsaved": True, "archive_sha256": _sha256_bytes(archive.read_bytes()),
        "pdf_sha256": _sha256_bytes(read_resource_bytes("help/PYeyedata-user-guide-zh.pdf")),
        "pdf_toolbar_and_help_buttons_open_bundled_bytes": True,
        "pdf_viewer_failure_reports_cached_path": True,
        "short_window_processing_and_group_controls_visible_and_scrollable": True,
        "result_axes_distinguish_count_seconds_and_metric_units": True,
    }


def run_diagnostic(config_path: str) -> int:
    """Run one allowlisted scenario and write evidence below its workspace."""
    workspace: Path | None = None
    evidence_path: Path | None = None
    failure: Exception | None = None
    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA, "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        config = _read_config(config_path)
        workspace, evidence_path = _paths(config)
        workspace.mkdir(parents=True, exist_ok=True)
        evidence.update({"scenario": config["scenario"], "display": config["display"],
                         "workspace": str(workspace)})
        if config["display"] == "offscreen":
            os.environ["QT_QPA_PLATFORM"] = "offscreen"

        from PySide6.QtWidgets import QApplication
        from pyeyedata.app_info import APP_NAME, APP_VERSION, PRD_VERSION
        from pyeyedata.gui.app import create_application
        from pyeyedata.gui.main_window import MainWindow

        app = QApplication.instance() or create_application([APP_NAME])
        if app.applicationName() != APP_NAME:
            app.setApplicationName(APP_NAME)
        window = MainWindow()
        window.show()
        app.processEvents()
        evidence.update({
            "application": {"name": APP_NAME, "version": APP_VERSION, "prd_version": PRD_VERSION},
            "process": {"pid": os.getpid(), "executable": sys.executable,
                        "frozen": bool(getattr(sys, "frozen", False)),
                        "cwd": str(Path.cwd()),
                        "bundle_root": str(getattr(sys, "_MEIPASS", "")) or None},
            "gui": {"window_title": window.windowTitle(), "visible_after_show": window.isVisible(),
                    "task_manager": type(window.task_manager).__name__,
                    "operation_manager": type(window.operation_manager).__name__},
        })
        if config["scenario"] == "synthetic-pipeline":
            _run_pipeline_scenario(app, window, workspace, evidence)
        elif config["scenario"] == "open-archive":
            _run_open_archive(app, window, config, evidence)
        elif config["scenario"] == "reliability-smoke":
            _run_reliability_smoke(app, window, workspace, evidence)
        elif config["scenario"] == "usability-smoke":
            _run_usability_smoke(app, window, workspace, evidence)
        evidence["status"] = "pass"
    except Exception as exc:
        failure = exc
        evidence["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            if "window" in locals():
                # Fixed diagnostics own synthetic records. Cleanup must not ask
                # an unattended test process to make a user-facing exit choice.
                window._confirm_unarchived_close = lambda _records: "discard"
                window.close()
                if "app" in locals():
                    app.processEvents()
        except Exception as exc:
            evidence.setdefault("cleanup_error", {"type": type(exc).__name__, "message": str(exc)})
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        if evidence_path is not None:
            try:
                _write_evidence(evidence_path, evidence)
                evidence["evidence_path"] = str(evidence_path)
            except Exception as exc:
                print(f"Could not write diagnostic evidence: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 2
    if evidence["status"] != "pass":
        error = evidence.get("error", {})
        if evidence_path is None and failure is not None:
            try:
                receipt_path = _write_config_error_receipt(failure)
            except Exception as receipt_error:
                print(f"Diagnostic error receipt failed: {type(receipt_error).__name__}: {receipt_error}",
                      file=sys.stderr)
            else:
                print(f"Diagnostic error receipt: {receipt_path}", file=sys.stderr)
        print(f"Diagnostic failed: {error.get('type')}: {error.get('message')}", file=sys.stderr)
        return 2
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0
