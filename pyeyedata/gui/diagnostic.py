"""Bounded, application-owned diagnostics for the packaged Windows build.

The command accepts only a JSON config with a fixed scenario and local paths.
It never imports or executes caller-supplied code.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import fields, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping


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


def _json_evidence_value(value: Any) -> Any:
    """Convert fixed diagnostic receipts to finite, JSON-native values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_evidence_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_evidence_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_evidence_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_evidence_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    encoded = (json.dumps(_json_evidence_value(evidence), ensure_ascii=False, sort_keys=True, indent=2,
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

    from .batch_queue import BatchQueue, BatchQueueError
    from .import_profiles import (
        ImportProfile,
        ImportProfileError,
        ImportSourceProfile,
        load_import_profile,
        save_import_profile,
    )
    import json
    import tempfile

    profile_and_queue_evidence: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="hexiplan-models-", dir=workspace) as temp_dir:
        profile_dir = Path(temp_dir) / "profiles"
        profile = ImportProfile(
            profile_id="fixed-diagnostic", name="Synthetic signal mapping", revision=1,
            sources=(ImportSourceProfile(
                role="signals",
                column_map={"time_s": "Time", "gaze_x": "X", "gaze_y": "Y"},
                units={"gaze_x": "px", "gaze_y": "px"},
            ),),
            structure_signature={"signals": ["Time", "X", "Y"]},
            validity_values={"gaze_valid": {"ok": True, "lost": False}},
            reading_direction_values={"L": "left_to_right"},
            pupil_units_by_eye={"left": "mm", "right": "mm"},
        )
        saved_profile_path = save_import_profile(profile, directory=profile_dir)
        loaded_profile = load_import_profile(profile.profile_id, directory=profile_dir)
        comparison = loaded_profile.compare_structure({"signals": ["Time", "X", "Y", "Status"]})
        if loaded_profile != profile or comparison.compatible or not comparison.differences:
            raise RuntimeError("Import profile roundtrip or explicit column-structure mismatch failed")
        serialized_profile = saved_profile_path.read_text(encoding="utf-8")
        if "source_path" in json.loads(serialized_profile):
            raise RuntimeError("Import profile unexpectedly persisted a source path")

        malformed = profile.to_dict()
        malformed["sources"][0]["csv_options"]["encoding"] = None
        malformed_path = Path(temp_dir) / "malformed-profile.json"
        malformed_path.write_text(json.dumps(malformed), encoding="utf-8")
        try:
            from .import_profiles import read_import_profile
            read_import_profile(malformed_path)
        except ImportProfileError:
            malformed_rejected = True
        else:
            raise RuntimeError("Malformed CSV options were not rejected as an import-profile error")
        malformed["sources"][0]["csv_options"]["encoding"] = "utf-8-sig"
        malformed["version"] = 1.0
        malformed_path.write_text(json.dumps(malformed), encoding="utf-8")
        try:
            read_import_profile(malformed_path)
        except ImportProfileError:
            float_version_rejected = True
        else:
            raise RuntimeError("A floating-point import-profile version was accepted")
        malformed["version"] = 1
        malformed["schema"] = "pyeyedata.import-profile/unsupported"
        malformed_path.write_text(json.dumps(malformed), encoding="utf-8")
        try:
            read_import_profile(malformed_path)
        except ImportProfileError:
            unknown_schema_rejected = True
        else:
            raise RuntimeError("An unknown import-profile schema was accepted")

        queue = BatchQueue()
        source_hash = "a" * 64
        first = queue.create_item(
            source_path=str(Path(temp_dir) / "one.csv"), source_sha256=source_hash,
            profile_id=profile.profile_id, profile_revision=profile.revision, structure_confirmed=True,
        )
        second = queue.create_item(
            source_path=str(Path(temp_dir) / "two.csv"), source_sha256=source_hash,
            profile_id=profile.profile_id, profile_revision=profile.revision, structure_confirmed=True,
        )
        third = queue.create_item(
            source_path=str(Path(temp_dir) / "three.csv"), source_sha256=source_hash,
            profile_id=profile.profile_id, profile_revision=profile.revision, structure_confirmed=True,
        )
        fingerprint = queue.freeze_configuration({"processing": {"lowpass_hz": 8.0}})
        if not fingerprint or queue.start_next().item_id != first.item_id:
            raise RuntimeError("Batch did not freeze a shared configuration and start its first item")
        queue.transition(first.item_id, "checking")
        queue.transition(first.item_id, "running")
        queue.transition(first.item_id, "failed", error="synthetic input error")
        if queue.start_next().item_id != second.item_id:
            raise RuntimeError("A failed batch item prevented the next item from starting")
        queue.transition(second.item_id, "checking")
        queue.transition(second.item_id, "running")
        queue.transition(second.item_id, "partial", run_id="synthetic-run-2")
        queue.start_export(second.item_id)
        try:
            queue.retry_analysis(second.item_id)
        except BatchQueueError:
            concurrent_analysis_rejected = True
        else:
            raise RuntimeError("Analysis retry was allowed while an export worker was active")
        try:
            queue.start_next()
        except BatchQueueError:
            concurrent_dispatch_rejected = True
        else:
            concurrent_dispatch_rejected = queue.next_pending() is None
        queue.finish_export(second.item_id, "failed", error="synthetic output error")
        if queue.get(second.item_id).analysis_status != "partial":
            raise RuntimeError("An export failure changed the analysis status")
        queue.retry_export(second.item_id)
        queue.finish_export(second.item_id, "complete", output_paths=[str(Path(temp_dir) / "two.csv")])
        pending_before_cancel = queue.next_pending()
        if pending_before_cancel is None or pending_before_cancel.item_id != third.item_id:
            raise RuntimeError("Batch did not retain the next pending item after export retry")
        queue.cancel()
        manifest = queue.manifest()
        if (queue.next_pending() is not None or queue.get(third.item_id).analysis_status != "cancelled"
                or queue.get(second.item_id).current_attempt.export_attempts != 2):
            raise RuntimeError("Batch retry/export/cancel state was not retained correctly")
        profile_and_queue_evidence = {
            "profile_roundtrip": True,
            "structure_mismatch_requires_review": [item.to_dict() for item in comparison.differences],
            "source_paths_excluded_from_profile": True,
            "malformed_csv_options_rejected": malformed_rejected,
            "float_schema_version_rejected": float_version_rejected,
            "unknown_schema_rejected": unknown_schema_rejected,
            "frozen_configuration_fingerprint": fingerprint,
            "failed_analysis_continued": True,
            "export_retry_count": queue.get(second.item_id).current_attempt.export_attempts,
            "export_failure_preserved_partial_analysis": True,
            "analysis_retry_during_export_rejected": concurrent_analysis_rejected,
            "dispatch_blocked_during_export": concurrent_dispatch_rejected,
            "cancelled_pending_item_status": manifest["items"][2]["analysis_status"],
            "manifest_contains_separate_export_status": manifest["items"][1]["export_status"],
        }

        # Exercise the GUI's cancellation gap between a completed phase and its
        # next worker dispatch, then verify that explicit retries resume only
        # the selected operation.
        gap_queue = BatchQueue()
        gap_item = gap_queue.create_item(
            source_path=str(input_path), source_sha256=_sha256_bytes(input_path.read_bytes()),
            profile_id=profile.profile_id, profile_revision=profile.revision,
            structure_confirmed=True,
        )
        gap_queue.freeze_configuration({"synthetic": "preflight-cancel-gap"})
        gap_queue.start_item(gap_item.item_id)
        gap_queue.transition(gap_item.item_id, "checking")
        gap_queue.transition(gap_item.item_id, "running")
        window._batch_queue = gap_queue
        window._batch_output_store = None
        window._batch_active = True
        window._batch_cancel_flag = False
        window._batch_current_item_id = gap_item.item_id
        window._batch_current_attempt = 1
        window._batch_current_phase = "run"
        window._batch_cancel_requested()
        if (window._batch_active or gap_queue.get(gap_item.item_id).status != "cancelled"
                or gap_queue.active_item_id is not None):
            raise RuntimeError("Cancelling in the preflight-to-run idle gap left an active queue item")
        gap_queue.resume()
        retried_gap = gap_queue.retry_analysis(gap_item.item_id)
        if retried_gap.current_attempt is None or retried_gap.current_attempt.number != 2:
            raise RuntimeError("Explicit analysis retry did not resume the cancelled queue with a new attempt")
        gap_queue.transition(gap_item.item_id, "failed", error="synthetic retry cleanup")

        export_gap_queue = BatchQueue()
        export_gap_item = export_gap_queue.create_item(
            source_path=str(input_path), source_sha256=_sha256_bytes(input_path.read_bytes()),
            profile_id=profile.profile_id, profile_revision=profile.revision,
            structure_confirmed=True,
        )
        export_gap_queue.freeze_configuration({"synthetic": "export-cancel-gap"})
        export_gap_queue.start_item(export_gap_item.item_id)
        export_gap_queue.transition(export_gap_item.item_id, "checking")
        export_gap_queue.transition(export_gap_item.item_id, "running")
        export_gap_queue.transition(export_gap_item.item_id, "partial", run_id=record.run_id)
        export_gap_queue.cancel()
        window._batch_queue = export_gap_queue
        window._batch_active = True
        window._batch_cancel_flag = False
        window._batch_current_item_id = export_gap_item.item_id
        window._batch_current_attempt = 1
        window._batch_current_phase = "export-pending"
        window._batch_cancel_requested()
        cancelled_export_gap = export_gap_queue.get(export_gap_item.item_id)
        if (cancelled_export_gap.analysis_status != "partial"
                or cancelled_export_gap.export_status != "cancelled"
                or cancelled_export_gap.current_attempt.export_attempts != 0):
            raise RuntimeError("Queue cancellation did not close an unstarted export without changing analysis status")
        export_gap_queue.resume()
        export_gap_queue.retry_export(export_gap_item.item_id)
        export_gap_queue.finish_export(
            export_gap_item.item_id, "complete", output_paths=[str(Path(temp_dir) / "retry.csv")],
        )
        if (export_gap_queue.get(export_gap_item.item_id).analysis_status != "partial"
                or export_gap_queue.get(export_gap_item.item_id).export_status != "complete"
                or export_gap_queue.get(export_gap_item.item_id).current_attempt.export_attempts != 1):
            raise RuntimeError("Explicit export retry changed analysis or failed to resume after cancellation")

        # Run the real batch export worker and MainWindow completion/idle route
        # against a synthetic RunRecord so the final CSV/archive receipt is
        # exercised without using caller data or touching the source file.
        from .batch_export import BatchOutputStore

        batch_output = BatchOutputStore(Path(temp_dir) / "batch-main-window")
        batch_output.validate_root()
        integration_queue = BatchQueue()
        integration_item = integration_queue.create_item(
            source_path=str(input_path), source_sha256=_sha256_bytes(input_path.read_bytes()),
            profile_id=profile.profile_id, profile_revision=profile.revision,
            structure_confirmed=True,
        )
        integration_queue.freeze_configuration({"synthetic": "batch-export-idle"})
        integration_queue.start_item(integration_item.item_id)
        integration_queue.transition(integration_item.item_id, "checking")
        integration_queue.transition(integration_item.item_id, "running")
        integration_queue.transition(integration_item.item_id, "partial", run_id=record.run_id)
        integration_queue.start_export(integration_item.item_id)
        attempt_dir = batch_output.reserve_attempt_dir(
            Path(input_path).name, integration_item.item_id, "a1-e1",
        )
        window._batch_queue = integration_queue
        window._batch_output_store = batch_output
        window._batch_output_root = str(batch_output.output_root)
        window._batch_record_by_run = {record.run_id: record}
        window._batch_active = True
        window._batch_cancel_flag = False
        window._batch_retry_ids = []
        window._batch_current_item_id = integration_item.item_id
        window._batch_current_attempt = 1
        window._batch_current_phase = "exporting"
        window._batch_pending_idle_action = None
        window._batch_export_generation = None
        window._batch_export_error = None
        window._batch_export_cancelled = False
        window._start_operation(
            "batch_export",
            lambda cancel, progress: batch_output.export_record(
                record, attempt_dir, cancel_requested=cancel,
            ),
        )
        window._batch_export_generation = window.operation_manager.generation
        _wait_for_condition(app, lambda: not window.operation_manager.active,
                            "MainWindow batch CSV and results-only archive export")
        integration_result = integration_queue.get(integration_item.item_id)
        output_files = [Path(path) for path in integration_result.output_paths]
        manifest_path = batch_output.queue_directory / "manifest.json"
        if (integration_result.analysis_status != "partial"
                or integration_result.export_status != "complete"
                or len(output_files) < 2
                or any(not path.is_file() for path in output_files)
                or not any(path.suffix.lower() == ".pyea" for path in output_files)
                or not manifest_path.is_file()
                or window._batch_active):
            raise RuntimeError("MainWindow did not finish the real batch export and record its output manifest")
        profile_and_queue_evidence.update({
            "cancel_gap_active_analysis_closed": True,
            "cancelled_queue_explicit_analysis_retry_new_attempt": retried_gap.current_attempt.number,
            "unstarted_export_cancelled_without_changing_partial": cancelled_export_gap.export_status,
            "cancelled_queue_explicit_export_retry_same_analysis": integration_queue.get(
                integration_item.item_id,
            ).analysis_status == "partial",
            "real_operation_manager_batch_export_idle_route": True,
            "batch_export_files": [path.name for path in output_files],
            "batch_manifest_written": manifest_path.is_file(),
        })

        from .timeline_model import TimelineModel
        from .aoi_editor_model import AOIEditorModel

        raw_timeline = pd.DataFrame({
            "source_id": ["source-a", "source-a"],
            "subject_id": ["subject-1", "subject-1"],
            "session_id": ["session-1", "session-1"],
            "processing_partition_id": ["partition-1", "partition-1"],
            "observation_id": ["obs-2", "obs-1"],
            "time_s": [1.0, 0.0],
            "gaze_x": [22.0, 11.0],
        })
        processed_timeline = pd.DataFrame({
            "source_id": ["source-a", "source-a"],
            "subject_id": ["subject-1", "subject-1"],
            "session_id": ["session-1", "session-1"],
            "processing_partition_id": ["partition-1", "partition-1"],
            "observation_id": ["obs-1", "obs-2"],
            "time_s": [0.0, 1.0],
            "gaze_x": [101.0, 202.0],
        })
        event_rows = pd.DataFrame({
            "source_id": ["source-a", "source-b"],
            "subject_id": ["subject-1", "subject-2"],
            "processing_partition_id": ["partition-1", "partition-2"],
            "start_observation_id": ["obs-1", "foreign-1"],
            "end_observation_id": ["obs-2", "foreign-2"],
            "event_id": ["event-a", "event-b"],
        })
        timeline = TimelineModel()
        timeline.set_data(raw_timeline, processed_timeline, events=event_rows)
        if len(timeline.stream_options) != 1:
            raise RuntimeError("Timeline did not expose the source/partition stream")
        prepared_timeline = timeline.prepare(timeline.stream_options[0].key)
        if (prepared_timeline.raw.get("observation_id", pd.Series(dtype=object)).tolist()
                != ["obs-1", "obs-2"]
                or prepared_timeline.processed.get("observation_id", pd.Series(dtype=object)).tolist()
                != ["obs-1", "obs-2"]
                or prepared_timeline.raw.get("gaze_x", pd.Series(dtype=float)).tolist() != [11.0, 22.0]
                or prepared_timeline.processed.get("gaze_x", pd.Series(dtype=float)).tolist() != [101.0, 202.0]
                or prepared_timeline.events.get("event_id", pd.Series(dtype=object)).tolist() != ["event-a"]):
            raise RuntimeError("Timeline did not align samples by observation identity/time or isolate scoped events")
        unidentifiable = TimelineModel()
        unidentifiable.set_data(
            pd.DataFrame({"time_s": [0.0], "gaze_x": [1.0]}),
            pd.DataFrame({"time_s": [0.0], "gaze_x": [2.0]}),
        )
        unidentifiable_timeline = unidentifiable.prepare(unidentifiable.stream_options[0].key)
        if not unidentifiable_timeline.processed.empty:
            raise RuntimeError("Timeline overlaid curves without stable sample identity")
        ambiguous = TimelineModel()
        ambiguous.set_data(pd.concat([
            raw_timeline,
            pd.DataFrame({
                "source_id": ["source-b"], "subject_id": ["subject-2"],
                "session_id": ["session-2"], "processing_partition_id": ["partition-2"],
                "observation_id": ["foreign-1"], "time_s": [0.0], "gaze_x": [99.0],
            }),
        ], ignore_index=True), events=event_rows)
        if len(ambiguous.stream_options) != 2:
            raise RuntimeError("Timeline fixture did not create an ambiguous two-source selection")
        for selection in (None, "unknown-stream"):
            unselected = ambiguous.prepare(selection)
            if (not unselected.message
                    or any(not getattr(unselected, name).empty
                           for name in ("raw", "processed", "events", "interval_audit"))):
                raise RuntimeError("Timeline displayed source data when its stream was unknown or ambiguous")

        from PIL import Image

        image_path = Path(temp_dir) / "aoi-stimulus.png"
        Image.new("RGB", (12, 8), color="white").save(image_path)
        aoi_model = AOIEditorModel()
        aoi_model.set_image(image_path)
        aoi_model.set_aois(pd.DataFrame([
            {"aoi_id": "aoi-a", "stimulus_id": "stimulus-a", "shape": "rectangle",
             "aoi_name": "First", "xmin": 1, "ymin": 1, "xmax": 5, "ymax": 5,
             "vertices": pd.NA, "priority": 1, "overlap_policy": "unresolved",
             "unit": "pixel", "coordinate_frame": "image_pixel"},
            {"aoi_id": "aoi-b", "stimulus_id": "stimulus-b", "shape": "rectangle",
             "aoi_name": "Other stimulus", "xmin": 2, "ymin": 2, "xmax": 6, "ymax": 6,
             "vertices": pd.NA, "priority": 1, "overlap_policy": "unresolved",
             "unit": "pixel", "coordinate_frame": "image_pixel"},
        ]))
        unbound_rejected = bool(aoi_model.validate())
        try:
            aoi_model.set_active_records([])
        except ValueError:
            unbound_edit_rejected = True
        else:
            raise RuntimeError("AOI editing was allowed before image/stimulus binding")
        aoi_model.bind_stimulus("stimulus-a")
        active = aoi_model.active_records
        if len(active) != 1 or active[0]["stimulus_id"] != "stimulus-a":
            raise RuntimeError("AOI editor mixed records from multiple stimuli")
        active[0]["aoi_name"] = "Renamed first"
        aoi_model.set_active_records(active)
        full_aoi_table = aoi_model.table()
        other_stimulus = full_aoi_table.loc[full_aoi_table["stimulus_id"] == "stimulus-b"]
        exported_aoi, exported_manifest = aoi_model.export_to(Path(temp_dir) / "edited-aois.csv")
        exported_columns = pd.read_csv(exported_aoi, encoding="utf-8-sig").columns.tolist()
        manifest_payload = json.loads(exported_manifest.read_text(encoding="utf-8"))
        stimulus_manifest = manifest_payload["stimuli"]["stimulus-a"]
        if (not unbound_rejected or not unbound_edit_rejected or len(other_stimulus) != 1
                or other_stimulus.iloc[0]["aoi_name"] != "Other stimulus"
                or "Renamed first" not in full_aoi_table["aoi_name"].tolist()
                or "_canonical_geometry" in exported_columns
                or stimulus_manifest.get("width") != 12 or stimulus_manifest.get("height") != 8
                or stimulus_manifest.get("sha256") != aoi_model.image_metadata["sha256"]):
            raise RuntimeError("AOI binding, stimulus preservation, CSV schema, or image manifest failed")
        control_model_evidence = {
            "timeline": {
                "samples_paired_by_observation_id_and_time_s": ["obs-1", "obs-2"],
                "event_rows_scoped_to_selected_source_partition": 1,
                "overlay_without_stable_sample_identity_rejected": True,
                "unknown_or_ambiguous_stream_hides_all_records": True,
            },
            "aoi_editor_model": {
                "unbound_edit_rejected": unbound_edit_rejected,
                "stimulus_rows_kept_separate": len(other_stimulus) == 1,
                "image_size_recorded": [stimulus_manifest["width"], stimulus_manifest["height"]],
                "manifest_image_sha256": stimulus_manifest["sha256"],
                "exported_csv_has_no_internal_geometry_columns": "_canonical_geometry" not in exported_columns,
            },
        }

    # Exercise the saved mapping preset through the real import-page widgets.
    # Keep the profile store isolated so the fixed diagnostic never reads or
    # writes a user's persistent presets.
    from unittest.mock import patch
    from PySide6.QtWidgets import QComboBox, QDialog, QInputDialog, QMessageBox
    from pyeyedata.io import read_table
    from . import import_page as import_page_module
    from .import_profiles import list_import_profiles as read_profile_listing
    import_profile_gui_evidence: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="hexiplan-import-profile-gui-", dir=workspace) as temp_dir:
        profile_root = Path(temp_dir) / "profiles"
        source_path = Path(temp_dir) / "signals.csv"
        segment_path = Path(temp_dir) / "segments.csv"
        pd.DataFrame({
            "Time": [0.0, 0.01], "X": [1.0, 2.0], "Y": [3.0, 4.0],
            "X_alt": [8.0, 9.0], "Status": ["ok", "lost"],
        }).to_csv(source_path, index=False, encoding="utf-8-sig")
        pd.DataFrame({"Name": ["trial-a"], "Start": [0.0], "End": [0.01],
                      "Name_alt": ["trial-alt"]}).to_csv(segment_path, index=False, encoding="utf-8-sig")
        real_save_profile = import_page_module.save_import_profile
        real_list_profiles = import_page_module.list_import_profiles
        with (
            patch.object(import_page_module, "save_import_profile",
                         lambda profile: real_save_profile(profile, directory=profile_root)),
            patch.object(import_page_module, "list_import_profiles",
                         lambda: read_profile_listing(directory=profile_root)),
            patch.object(QInputDialog, "getText", return_value=("Synthetic device", True)),
            patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok),
        ):
            profile_page = import_page_module.ImportPage()
            signal_panel = profile_page.sources["signals"]
            signal_panel.path_edit.setText(str(source_path))
            imported_table = read_table(source_path, role="signals")
            signal_panel._on_preview(signal_panel._generation, {
                "frame": imported_table.frame,
                "manifest": imported_table.manifest_entry(),
                "read_path": str(source_path),
                "read_csv_options": signal_panel.csv_options(),
                "sheets": (),
            })
            segment_panel = profile_page.sources["segments"]
            segment_panel.path_edit.setText(str(segment_path))
            imported_segments = read_table(segment_path, role="segments")
            segment_panel._on_preview(segment_panel._generation, {
                "frame": imported_segments.frame,
                "manifest": imported_segments.manifest_entry(),
                "read_path": str(segment_path),
                "read_csv_options": segment_panel.csv_options(),
                "sheets": (),
            })
            profile_page.coordinate.subject_id.setText("subject-live")
            profile_page.coordinate.session_id.setText("session-live")
            profile_page.coordinate.sync_method.setText("manual-sync-method")
            profile_page.coordinate.sync_refs.setText("reference-live")
            profile_page.coordinate.coordinate_unit.setText("pixel-saved")
            editor = signal_panel.mapping
            if editor is None:
                raise RuntimeError("Fixed import-profile GUI fixture has no signal mapping editor")
            chosen_columns = {"time_s": "Time", "gaze_x": "X", "gaze_y": "Y", "gaze_valid": "Status"}
            for field_name, column in chosen_columns.items():
                row = next(index for index, spec in enumerate(editor.fields) if spec.name == field_name)
                choice = editor._table.cellWidget(row, 2)
                if not isinstance(choice, QComboBox) or choice.findData(column) < 0:
                    raise RuntimeError(f"Fixed import-profile GUI fixture cannot select {field_name} → {column}")
                choice.setCurrentIndex(choice.findData(column))
            segment_editor = segment_panel.mapping
            if segment_editor is None:
                raise RuntimeError("Fixed import-profile GUI fixture has no segment mapping editor")
            for field_name, column in {"segment_name": "Name", "start_s": "Start", "end_s": "End"}.items():
                row = next(index for index, spec in enumerate(segment_editor.fields) if spec.name == field_name)
                choice = segment_editor._table.cellWidget(row, 2)
                if not isinstance(choice, QComboBox) or choice.findData(column) < 0:
                    raise RuntimeError(f"Fixed import-profile GUI fixture cannot select {field_name} → {column}")
                choice.setCurrentIndex(choice.findData(column))
            for field_name in ("gaze_x", "gaze_y"):
                row = next(index for index, spec in enumerate(editor.fields) if spec.name == field_name)
                unit_choice = editor._table.cellWidget(row, 5)
                if not isinstance(unit_choice, QComboBox):
                    raise RuntimeError(f"Fixed import-profile GUI fixture has no {field_name} unit selector")
                unit_choice.setCurrentText("px")
            profile_page._validity_codes.configure_validity_fields(editor.mapping(), signal_panel._preview)
            profile_page._validity_codes.set_values({"gaze_valid": {"ok": True, "lost": False}})
            profile_page.scope.overlap.setCurrentIndex(profile_page.scope.overlap.findData("keep_labels"))
            profile_page.scope.duplicate.setCurrentIndex(profile_page.scope.duplicate.findData("deduplicate"))
            profile_page.save_current_import_profile()
            if profile_page.import_profile_combo.count() != 2 or "已保存“Synthetic device”" not in profile_page.import_profile_status.text():
                raise RuntimeError("Import-page GUI did not save and list the named profile: "
                                   + profile_page.import_profile_status.text())
            saved_path = next(profile_root.glob("*.json"))
            saved_profile_payload = json.loads(saved_path.read_text(encoding="utf-8"))
            if str(source_path) in saved_path.read_text(encoding="utf-8") or "source_path" in saved_profile_payload:
                raise RuntimeError("Import-page GUI persisted the selected source path in a profile")

            # Deliberately change mapped values; applying the candidate restores
            # the confirmed map/units while excluded identity/sync/scope stays live.
            profile_page.coordinate.coordinate_unit.setText("pixel-live")
            gaze_x_row = next(index for index, spec in enumerate(editor.fields) if spec.name == "gaze_x")
            gaze_x_choice = editor._table.cellWidget(gaze_x_row, 2)
            gaze_x_choice.setCurrentIndex(gaze_x_choice.findData("X_alt"))
            editor._table.cellWidget(gaze_x_row, 5).setCurrentText("cm")

            def accept_profile_candidate(dialog: Any) -> int:
                dialog.accept()
                return dialog.result()

            with patch.object(import_page_module._ProfileStageDialog, "exec", accept_profile_candidate):
                profile_page.apply_selected_import_profile()
            if (editor.mapping().get("gaze_x") != "X" or editor.units().get("gaze_x") != "px"
                    or profile_page.coordinate.coordinate_unit.text() != "pixel-saved"
                    or profile_page.coordinate.subject_id.text() != "subject-live"
                    or profile_page.coordinate.session_id.text() != "session-live"
                    or profile_page.coordinate.sync_method.text() != "manual-sync-method"
                    or profile_page.coordinate.sync_refs.text() != "reference-live"
                    or profile_page.scope.overlap.currentData() != "keep_labels"
                    or profile_page.scope.duplicate.currentData() != "deduplicate"
                    or profile_page._validity_codes.values().get("gaze_valid") != {"ok": True, "lost": False}):
                raise RuntimeError("Import-page GUI did not restore included values or preserve excluded values")

            # Busy status changing while the confirmation modal is open cannot
            # commit the already staged candidate.
            before_busy = (editor.mapping().copy(), profile_page.coordinate.coordinate_unit.text())

            def mark_busy_during_review(dialog: Any) -> int:
                profile_page.set_external_busy(True)
                return QDialog.DialogCode.Accepted

            with patch.object(import_page_module._ProfileStageDialog, "exec", mark_busy_during_review):
                profile_page.apply_selected_import_profile()
            profile_page.set_external_busy(False)
            if (editor.mapping(), profile_page.coordinate.coordinate_unit.text()) != before_busy:
                raise RuntimeError("Import-page GUI applied a profile after the operation state became busy")

            # Cause the second source to change after staging: the first role
            # has already been applied, so this exercises rollback of a partial
            # commit across the whole candidate rather than a preflight-only error.
            editor_row = next(index for index, spec in enumerate(editor.fields) if spec.name == "gaze_x")
            editor._table.cellWidget(editor_row, 2).setCurrentIndex(
                editor._table.cellWidget(editor_row, 2).findData("X_alt")
            )
            editor._table.cellWidget(editor_row, 5).setCurrentText("cm")
            segment_row = next(index for index, spec in enumerate(segment_editor.fields)
                               if spec.name == "segment_name")
            segment_editor._table.cellWidget(segment_row, 2).setCurrentIndex(
                segment_editor._table.cellWidget(segment_row, 2).findData("Name_alt")
            )
            before_rollback = (
                editor.mapping().copy(), editor.units().copy(), segment_editor.mapping().copy(),
                signal_panel._preview.copy(deep=True), segment_panel._preview.copy(deep=True),
                profile_page.coordinate.coordinate_unit.text(),
            )

            def change_second_source_during_review(dialog: Any) -> int:
                dialog.accept()
                segment_path.write_text(
                    "Name,Start,End,Name_alt\ntrial-changed,0,0.01,trial-other\n", encoding="utf-8-sig",
                )
                return dialog.result()

            with patch.object(import_page_module._ProfileStageDialog, "exec", change_second_source_during_review):
                profile_page.apply_selected_import_profile()
            after_rollback = (
                editor.mapping().copy(), editor.units().copy(), segment_editor.mapping().copy(),
                signal_panel._preview.copy(deep=True), segment_panel._preview.copy(deep=True),
                profile_page.coordinate.coordinate_unit.text(),
            )
            if (after_rollback[:3] != before_rollback[:3]
                    or not after_rollback[3].equals(before_rollback[3])
                    or not after_rollback[4].equals(before_rollback[4])
                    or after_rollback[5] != before_rollback[5]):
                raise RuntimeError("Import-page GUI failed to roll back a partial multi-role profile apply")

            # Changed headers require review; cancelling leaves the live controls alone.
            source_path.write_text(
                "Clock,Xnew,Ynew,X_alt,Status\n0,2,3,4,ok\n", encoding="utf-8-sig",
            )
            before_cancel = (editor.mapping().copy(), editor.units().copy(),
                             profile_page.coordinate.coordinate_unit.text())

            def reject_changed_structure(dialog: Any) -> int:
                if not dialog._confirmations:
                    raise RuntimeError("Changed profile structure did not request explicit confirmation")
                if any(check.isChecked() for check in dialog._confirmations.values()):
                    raise RuntimeError("Changed profile structure was pre-confirmed")
                return QDialog.DialogCode.Rejected

            with patch.object(import_page_module._ProfileStageDialog, "exec", reject_changed_structure):
                profile_page.apply_selected_import_profile()
            after_cancel = (editor.mapping().copy(), editor.units().copy(),
                            profile_page.coordinate.coordinate_unit.text())
            if after_cancel != before_cancel or "取消" not in profile_page.import_profile_status.text():
                raise RuntimeError("Cancelling a changed profile structure mutated the live import settings")
            import_profile_gui_evidence = {
                "saved_and_listed_through_import_page": True,
                "source_path_excluded": True,
                "mapping_units_and_validity_restored": True,
                "subject_session_sync_and_scope_preserved": True,
                "busy_during_confirmation_blocks_apply": True,
                "partial_multi_role_apply_rolls_back_on_source_change": True,
                "changed_structure_requires_unchecked_confirmation": True,
                "cancelled_structure_review_preserves_live_settings": True,
                "profile_store": "temporary diagnostic directory",
            }
            profile_page.close()

    from .diagnostic_timeline import run_timeline_integration_checks

    timeline_evidence = run_timeline_integration_checks()
    if not timeline_evidence.get("passed"):
        raise RuntimeError(
            "quality timeline integration checks failed: "
            + ", ".join(str(item) for item in timeline_evidence.get("failures", ()))
        )
    evidence["quality_timeline_integration"] = timeline_evidence

    from .diagnostic_results_actions import run_results_actions_checks

    results_action_evidence = run_results_actions_checks()
    if not results_action_evidence.get("passed"):
        raise RuntimeError(
            "results action integration checks failed: "
            + ", ".join(str(item) for item in results_action_evidence.get("failures", ()))
        )
    evidence["results_actions_integration"] = results_action_evidence

    from .diagnostic_timing import run_timing_diagnostic

    timing_evidence = run_timing_diagnostic(window)
    if not timing_evidence.get("passed"):
        raise RuntimeError(
            "task timing diagnostics failed: "
            + ", ".join(str(item) for item in timing_evidence.get("failures", ()))
        )
    evidence["task_timing_diagnostic"] = timing_evidence

    from .diagnostic_preprocessing import run_preprocessing_diagnostic

    preprocessing_evidence = run_preprocessing_diagnostic(app=app, workspace=workspace)
    if preprocessing_evidence.get("status") != "passed":
        raise RuntimeError("pupil preprocessing diagnostics failed")
    evidence["pupil_preprocessing_diagnostic"] = preprocessing_evidence

    from .diagnostic_design_presets import run_design_presets_diagnostic

    design_presets_evidence = run_design_presets_diagnostic(app=app, workspace=workspace)
    if design_presets_evidence.get("status") != "passed":
        raise RuntimeError("statistics preset diagnostics failed")
    evidence["statistics_design_presets_diagnostic"] = design_presets_evidence

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
        "import_profile_and_batch_queue": profile_and_queue_evidence,
        "import_profile_gui": import_profile_gui_evidence,
        "control_model_contracts": control_model_evidence,
    }


def _run_aoi_editor_host_integration(app: Any, workspace: Path) -> dict[str, Any]:
    """Exercise the import-page AOI editor handoff without changing user data."""
    import tempfile

    import pandas as pd
    from PIL import Image
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from unittest.mock import patch

    from pyeyedata.aoi_geometry import AOIGeometryBudget
    from pyeyedata.gui.main_window import MainWindow
    from pyeyedata.individual import INDIVIDUAL_CATALOGUE, _availability
    from pyeyedata.io import read_table
    from pyeyedata.pipeline import _preflight_aoi_availability

    test_dir = Path(tempfile.mkdtemp(prefix="aoi-editor-host-", dir=workspace))
    source_path = test_dir / "signals.csv"
    image_path = test_dir / "stimulus.png"
    output_path = test_dir / "edited-aois.csv"
    sample_count = 101
    sample_index = list(range(sample_count))
    gaze_x = [5.0 if 0 <= index <= 24 or 50 <= index <= 74 else 20.0
              for index in sample_index]
    gaze_y = [5.0] * sample_count
    pd.DataFrame({
        "subject_id": ["synthetic-subject"] * sample_count,
        "session_id": ["synthetic-session"] * sample_count,
        "time_s": [index * 0.01 for index in range(sample_count)],
        "gaze_x": gaze_x,
        "gaze_y": gaze_y,
        "gaze_valid": ["ok"] * sample_count,
        "gaze_left_x": gaze_x,
        "gaze_left_y": gaze_y,
        "gaze_left_valid": ["ok"] * sample_count,
        "gaze_right_x": gaze_x,
        "gaze_right_y": gaze_y,
        "gaze_right_valid": ["ok"] * sample_count,
        "pupil_left": [3.3] * sample_count,
        "pupil_left_valid": ["ok"] * sample_count,
        "stimulus_id": ["stimulus-a"] * sample_count,
    }).to_csv(source_path, index=False, encoding="utf-8-sig")
    Image.new("RGB", (64, 32), "white").save(image_path)

    window = MainWindow()
    window.show()
    app.processEvents()
    page = window.import_page

    def wait_until(predicate: Any, label: str, timeout_seconds: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if not predicate():
            raise RuntimeError(f"AOI host diagnostic timed out while waiting for {label}")

    try:
        signal_panel = page.sources["signals"]
        signal_panel.path_edit.setText(str(source_path))
        imported = read_table(source_path, role="signals", csv_options=signal_panel.csv_options())
        signal_panel._on_preview(signal_panel._generation, {
            "frame": imported.frame, "manifest": imported.manifest_entry(),
            "read_path": str(source_path), "read_csv_options": signal_panel.csv_options(), "sheets": (),
        })
        mapping_editor = signal_panel.mapping
        field_mapping = {
            "subject_id": "subject_id", "session_id": "session_id", "time_s": "time_s",
            "gaze_x": "gaze_x", "gaze_y": "gaze_y", "gaze_valid": "gaze_valid",
            "gaze_left_x": "gaze_left_x", "gaze_left_y": "gaze_left_y",
            "gaze_left_valid": "gaze_left_valid", "gaze_right_x": "gaze_right_x",
            "gaze_right_y": "gaze_right_y", "gaze_right_valid": "gaze_right_valid",
            "pupil_left": "pupil_left", "pupil_left_valid": "pupil_left_valid",
            "stimulus_id": "stimulus_id",
        }
        for field_name, column_name in field_mapping.items():
            row = next(index for index, spec in enumerate(mapping_editor.fields) if spec.name == field_name)
            combo = mapping_editor._table.cellWidget(row, 2)
            combo.setCurrentIndex(combo.findData(column_name))
        for field_name, unit in {
            "gaze_x": "px", "gaze_y": "px", "gaze_left_x": "px", "gaze_left_y": "px",
            "gaze_right_x": "px", "gaze_right_y": "px", "pupil_left": "mm",
        }.items():
            row = next(index for index, spec in enumerate(mapping_editor.fields) if spec.name == field_name)
            mapping_editor._table.cellWidget(row, 5).setCurrentText(unit)
        coordinate = page.coordinate
        coordinate.time_unit.setCurrentIndex(coordinate.time_unit.findData("s"))
        coordinate.time_confirmed.setChecked(True)
        coordinate.pupil_left_unit.setText("mm")
        coordinate.coordinate_unit.setText("pixel")
        image_frame = "image_pixel:64x32:origin=top_left:y=down"
        coordinate.coordinate_frame.setText(image_frame)
        coordinate.representation.setCurrentIndex(coordinate.representation.findData("2d_point"))
        coordinate.eye_fields["left_point_unit"].setText("pixel")
        coordinate.eye_fields["left_point_frame"].setText(image_frame)
        coordinate.eye_fields["right_point_unit"].setText("pixel")
        coordinate.eye_fields["right_point_frame"].setText(image_frame)
        page._validity_codes.configure_validity_fields(mapping_editor.mapping(), imported.frame)
        page._validity_codes.set_values({
            "gaze_valid": {"ok": True}, "gaze_left_valid": {"ok": True},
            "gaze_right_valid": {"ok": True}, "pupil_left_valid": {"ok": True},
        })
        page.start_import()
        wait_until(lambda: not page.import_active, "initial signal import")
        old_revision = window.session.dataset_revision
        if old_revision is None:
            raise RuntimeError("AOI host diagnostic signal import did not create a revision")

        page.sources["aois"].aoi_editor_button.click()
        app.processEvents()
        dialog = window._aoi_editor_dialog
        if dialog is None:
            raise RuntimeError("ImportPage did not open the static-image AOI editor")
        dialog.set_image(image_path, stimulus_id="stimulus-a")
        dialog.model.set_active_records([{
            "aoi_id": "aoi-target", "stimulus_id": "stimulus-a", "shape": "rectangle",
            "aoi_name": "Target", "xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10,
            "vertices": pd.NA, "priority": 1, "overlap_policy": "unresolved",
            "unit": "pixel", "coordinate_frame": "image_pixel",
        }])
        dialog._load_active_items()
        dialog._commit_active()
        with (
            patch.object(QFileDialog, "getSaveFileName", return_value=(str(output_path), "CSV (*.csv)")),
            patch.object(QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok),
        ):
            dialog.apply_button.click()
            app.processEvents()
        aoi_panel = page.sources["aois"]
        wait_until(lambda: aoi_panel.mapping is not None and not aoi_panel._preview.empty,
                   "exported AOI preview")
        if window.session.dataset_revision.revision_id != old_revision.revision_id:
            raise RuntimeError("AOI staging changed the installed revision before explicit import")
        pending = page._aoi_editor_pending_import
        if not isinstance(pending, dict):
            raise RuntimeError("AOI export was not bound to import provenance")
        manifest_path = Path(str(pending.get("manifest_path", "")))
        csv_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if csv_hash != pending.get("csv_sha256") or manifest_hash != pending.get("manifest_sha256"):
            raise RuntimeError("AOI CSV or image manifest provenance hash did not verify")

        page.start_import()
        wait_until(lambda: not page.import_active, "explicit AOI import")
        revision = window.session.dataset_revision
        if revision is None or revision.revision_id == old_revision.revision_id:
            raise RuntimeError("Explicit AOI import did not create a new immutable revision")
        dataset = revision.dataset_copy()
        provenance = dataset.metadata.get("aoi_editor_provenance", {})
        if (len(dataset.aois) != 1 or provenance.get("verification_status") != "verified"
                or provenance.get("csv_sha256") != csv_hash
                or provenance.get("manifest_sha256") != manifest_hash
                or provenance.get("coordinate_mapping_status") != "declared_same_image_pixel_frame"
                or dataset.aois.iloc[0].get("unit") != "pixel"
                or dataset.aois.iloc[0].get("coordinate_frame") != "image_pixel:64x32:origin=top_left:y=down"
                or not old_revision.dataset_copy().aois.empty):
            raise RuntimeError("AOI source, frame, provenance, or previous revision was not preserved")

        # The image and signals declare the same explicit pixel frame. Geometry
        # compatibility is still checked by the existing preflight gate.
        signal_frame = dataset.signals.copy(deep=True)
        module_config = {
            "coordinate_unit": "pixel", "coordinate_frame": image_frame,
            "time_unit": "s", "time_confirmed": True,
            "aoi_mapping_confirmed": True, "metadata": {},
        }
        availability = _availability(signal_frame, module_config)
        metric_id = "individual.aoi.dwell_proportion"
        selected = availability.loc[availability["metric_id"].eq(metric_id)]
        if selected.empty or not bool(selected.iloc[0]["available"]):
            raise RuntimeError("AOI geometry preflight fixture lacks metric preconditions: "
                               + selected.to_json(orient="records", force_ascii=False))
        checked = _preflight_aoi_availability(
            availability, signal_frame, dataset.aois, module_config,
            {spec["metric_id"] for spec in INDIVIDUAL_CATALOGUE
             if spec["metric_id"].startswith("individual.aoi.")},
            AOIGeometryBudget(), None,
        )
        matched = checked.loc[checked["metric_id"].eq(metric_id)]
        if matched.empty or not matched["available"].fillna(False).astype(bool).all():
            raise RuntimeError("Explicit same-pixel coordinate declaration did not pass AOI geometry preflight")
        mismatch_config = {**module_config, "coordinate_frame": "other_pixel_frame"}
        mismatch_availability = _availability(signal_frame, mismatch_config)
        mismatch_checked = _preflight_aoi_availability(
            mismatch_availability, signal_frame, dataset.aois, mismatch_config,
            {spec["metric_id"] for spec in INDIVIDUAL_CATALOGUE
             if spec["metric_id"].startswith("individual.aoi.")},
            AOIGeometryBudget(), None,
        )
        blocked = mismatch_checked.loc[mismatch_checked["metric_id"].eq(metric_id)]
        issue = blocked.iloc[0].get("aoi_geometry_issue") if not blocked.empty else None
        if (blocked.empty or blocked.iloc[0]["status"] != "blocked"
                or blocked.iloc[0]["reason_code"] != "aoi_coordinate_semantics_mismatch"
                or not isinstance(issue, dict) or issue.get("canonical_field") != "coordinate_frame"):
            raise RuntimeError("Existing AOI geometry preflight did not block the mismatched pixel frame")

        # A results-only archive deliberately omits the embedded input dataset.
        # The pipeline's immutable run metadata must therefore carry the editor
        # provenance through the archive reader as well.
        from pyeyedata.archive_session import load_run_archive, save_run_archive
        from pyeyedata.gui.models import SelectionSpec

        selection = SelectionSpec(
            individual_metric_ids=(
                "individual.aoi.dwell_proportion", "individual.aoi.visits",
                "individual.aoi.first_visit_duration", "individual.aoi.mean_visit_duration",
                "individual.aoi.coverage",
            ), run_individual=True, run_group=False, run_advanced=False,
        )
        window.set_selection(selection)
        preconfirm_parameters, _, _ = window._snapshot_configuration(selection)
        preconfirm_metadata = preconfirm_parameters.get("individual", {}).get("metadata", {})
        if preconfirm_metadata.get("aoi_mapping_confirmed") is not False:
            raise RuntimeError("Ordinary AOI mapping must remain unconfirmed until the user checks the new control")
        unconfirmed_preflight, preflight_failures = _run_gui_task(app, window, phase="preflight")
        if preflight_failures or unconfirmed_preflight is None or unconfirmed_preflight.availability is None:
            raise RuntimeError("Unconfirmed AOI preflight failed to return availability: " + repr(preflight_failures))
        unconfirmed_rows = unconfirmed_preflight.availability.loc[
            unconfirmed_preflight.availability["metric_id"].eq(metric_id)
        ]
        if (unconfirmed_rows.empty
                or unconfirmed_rows["available"].fillna(False).astype(bool).any()
                or not unconfirmed_rows["reason"].astype("string").str.contains("aoi_mapping", regex=False).all()):
            raise RuntimeError("AOI mapping remained available before the explicit user confirmation")
        confirmation = page.sources["aois"].aoi_mapping_confirmation
        if not confirmation.isEnabled() or confirmation.isChecked():
            raise RuntimeError("The current imported revision did not expose an unchecked confirmation control")
        confirmation.click()
        app.processEvents()
        confirmed_metadata = page.individual_aoi_mapping_metadata(revision)
        if (confirmed_metadata.get("aoi_mapping_confirmed") is not True
                or not confirmed_metadata.get("aoi_mapping_refs")
                or confirmed_metadata.get("aoi_mapping_revision_fingerprint") != revision.fingerprint):
            raise RuntimeError("The AOI checkbox did not bind its explicit confirmation to the current revision")
        confirmed_parameters, _, confirmed_revision = window._snapshot_configuration(selection)
        individual_metadata = confirmed_parameters.get("individual", {}).get("metadata", {})
        group_metadata = confirmed_parameters.get("group", {}).get("metadata", {})
        if (confirmed_revision.revision_id != revision.revision_id
                or individual_metadata.get("aoi_mapping_confirmed") is not True
                or individual_metadata.get("aoi_mapping_refs") != confirmed_metadata["aoi_mapping_refs"]
                or group_metadata.get("aoi_mapping_confirmed") is True):
            raise RuntimeError("AOI confirmation was not isolated in the individual snapshot metadata")
        window.set_selection(selection)
        confirmed_preflight, preflight_failures = _run_gui_task(app, window, phase="preflight")
        if preflight_failures or confirmed_preflight is None or confirmed_preflight.availability is None:
            raise RuntimeError("Confirmed AOI preflight failed to return availability: " + repr(preflight_failures))
        ready_rows = confirmed_preflight.availability.loc[
            confirmed_preflight.availability["metric_id"].isin(selection.individual_metric_ids)
            & confirmed_preflight.availability.get(
                "module", pd.Series(index=confirmed_preflight.availability.index, dtype="string")
            ).astype("string").eq("individual")
        ]
        aoi_ready_rows = ready_rows.loc[ready_rows["metric_id"].str.startswith("individual.aoi.")]
        if (aoi_ready_rows.empty or not aoi_ready_rows["available"].fillna(False).astype(bool).all()):
            raise RuntimeError("Confirmed AOI preflight did not make the selected individual metrics available: "
                               + aoi_ready_rows.to_json(orient="records", force_ascii=False))
        window.set_selection(selection)
        outcome, task_failures = _run_gui_task(app, window, phase="run")
        if task_failures or outcome is None:
            raise RuntimeError("AOI individual analysis failed: " + repr(task_failures))
        record = next((item for item in window.session.history if item.run_id == outcome.run_id), None)
        if record is None:
            raise RuntimeError("AOI individual run was not retained in session history")
        snapshot_details = record.snapshot_details()
        run_parameters = snapshot_details.get("parameters", {})
        run_individual_metadata = run_parameters.get("individual", {}).get("metadata", {}) \
            if isinstance(run_parameters, dict) else {}
        if (run_individual_metadata.get("aoi_mapping_confirmed") is not True
                or list(run_individual_metadata.get("aoi_mapping_refs", ()))
                != list(confirmed_metadata["aoi_mapping_refs"])
                or run_individual_metadata.get("aoi_mapping_revision_fingerprint") != revision.fingerprint):
            raise RuntimeError(
                "The actual individual run snapshot lost its explicit AOI mapping confirmation: "
                f"snapshot={run_individual_metadata!r}; confirmed={confirmed_metadata!r}"
            )
        metrics = record.table_copy("metrics")
        selected_aoi_metrics = metrics.loc[metrics["metric_id"].isin(selection.individual_metric_ids)].copy()
        expected_values = {
            "individual.aoi.dwell_proportion": 0.5,
            "individual.aoi.visits": 2.0,
            "individual.aoi.first_visit_duration": 0.25,
            "individual.aoi.mean_visit_duration": 0.25,
            "individual.aoi.coverage": 1.0,
        }
        observed_values: dict[str, list[float]] = {}
        for selected_metric, expected_value in expected_values.items():
            rows = selected_aoi_metrics.loc[selected_aoi_metrics["metric_id"].eq(selected_metric)]
            usable = rows.loc[rows["status"].astype("string").isin(["complete", "partial"])]
            values = pd.to_numeric(usable["value"], errors="coerce").dropna().astype(float).tolist()
            if not values or any(not math.isclose(value, expected_value, rel_tol=1e-10, abs_tol=1e-10)
                                 for value in values):
                raise RuntimeError(f"Individual AOI oracle mismatch for {selected_metric}: "
                                   f"expected={expected_value}, rows={rows.to_dict(orient='records')}")
            observed_values[selected_metric] = values
        run_dataset_metadata = record.metadata().get("dataset_metadata", {})
        run_provenance = run_dataset_metadata.get("aoi_editor_provenance", {}) \
            if isinstance(run_dataset_metadata, dict) else {}
        if (run_provenance.get("csv_sha256") != csv_hash
                or run_provenance.get("manifest_sha256") != manifest_hash
                or run_provenance.get("verification_status") != "verified"):
            raise RuntimeError("Analysis RunRecord lost the imported AOI editor provenance")
        archive_path = test_dir / "aoi-provenance-results-only.pyea"
        save_run_archive(archive_path, record, mode="results_only")
        archived = load_run_archive(archive_path)
        archived_metadata = archived.record.metadata().get("dataset_metadata", {})
        archived_provenance = archived_metadata.get("aoi_editor_provenance", {}) \
            if isinstance(archived_metadata, dict) else {}
        archived_details = archived.record.snapshot_details()
        archived_parameters = archived_details.get("parameters", {})
        archived_individual_metadata = (
            archived_parameters.get("individual", {}).get("metadata", {})
            if isinstance(archived_parameters, dict) else {}
        )
        if (archived.archive_mode != "results_only"
                or archived_provenance.get("csv_sha256") != csv_hash
                or archived_provenance.get("manifest_sha256") != manifest_hash
                or archived_provenance.get("verification_status") != "verified"
                or archived_individual_metadata.get("aoi_mapping_confirmed") is not True
                or list(archived_individual_metadata.get("aoi_mapping_refs", ()))
                != list(confirmed_metadata["aoi_mapping_refs"])):
            raise RuntimeError("AOI provenance or individual confirmation did not survive results-only archive readback")
        return {
            "status": "pass", "entry": "ImportPage AOI button -> MainWindow editor -> export -> explicit import",
            "old_revision_id": old_revision.revision_id, "new_revision_id": revision.revision_id,
            "aoi_rows": len(dataset.aois), "exported_csv_path": str(output_path),
            "manifest_path": str(manifest_path), "csv_sha256": csv_hash,
            "manifest_sha256": manifest_hash, "image_metadata": provenance.get("verified_stimuli", {}),
            "coordinate_mapping_status": provenance.get("coordinate_mapping_status"),
            "unconfirmed_preflight_blocked": True,
            "unconfirmed_preflight_reasons": sorted(set(unconfirmed_rows["reason_code"].astype(str))),
            "confirmed_preflight_ready_rows": int(len(aoi_ready_rows)),
            "mismatched_frame_preflight_reason_code": str(blocked.iloc[0]["reason_code"]),
            "mismatched_frame_canonical_field": issue.get("canonical_field"),
            "individual_mapping_confirmation": {
                "checked_by_gui": bool(confirmation.isChecked()),
                "refs": confirmed_metadata["aoi_mapping_refs"],
                "revision_fingerprint": confirmed_metadata["aoi_mapping_revision_fingerprint"],
                "snapshot_group_metadata_unchanged": group_metadata.get("aoi_mapping_confirmed") is not True,
            },
            "aoi_metric_values_by_stream": observed_values,
            "run_selection": selection.to_dict(),
            "previous_revision_aois_unchanged": old_revision.dataset_copy().aois.empty,
            "run_id": record.run_id, "run_status": record.status,
            "results_only_archive_path": str(archive_path),
            "archive_aoi_provenance_readback": {
                "csv_sha256": archived_provenance.get("csv_sha256"),
                "manifest_sha256": archived_provenance.get("manifest_sha256"),
                "verification_status": archived_provenance.get("verification_status"),
                "individual_aoi_mapping_confirmed": archived_individual_metadata.get("aoi_mapping_confirmed"),
                "individual_aoi_mapping_refs": list(archived_individual_metadata.get("aoi_mapping_refs", ())),
            },
            "synthetic_data_only": True,
        }
    finally:
        if window._aoi_editor_dialog is not None:
            window._aoi_editor_dialog.close()
        window._confirm_unarchived_close = lambda _records: "discard"
        window.close()
        app.processEvents()


def _run_batch_valid_bad_valid(app: Any, window: Any, workspace: Path,
                               evidence: dict[str, Any]) -> None:
    """Drive three real CSV files through import, preflight, analysis and export."""
    from copy import deepcopy
    import json
    import math
    import pandas as pd
    import tempfile
    from unittest.mock import patch

    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
    from pyeyedata.archive_session import load_run_archive
    from pyeyedata.gui.models import DatasetRevision, SelectionSpec
    from pyeyedata.io import read_table
    from . import import_page as import_page_module
    from .import_profiles import list_import_profiles as read_profile_listing

    fixture_dir = workspace / "batch-valid-bad-valid"
    fixture_dir.mkdir()
    output_root = workspace / "batch-output"

    def make_source(path: Path, *, subject: str, session: str, duration_s: float,
                    pupil_value: float, blank_subject: bool = False) -> None:
        count = 201
        interval_s = duration_s / (count - 1)
        times = [round(index * interval_s, 6) for index in range(count)]
        subject_values = [""] * count if blank_subject else [subject] * count
        pd.DataFrame({
            "subject_id": subject_values,
            "session_id": [session] * count,
            "time_s": times,
            "gaze_x": [float(index) for index in range(count)],
            "gaze_y": [float(index + 2) for index in range(count)],
            "gaze_valid": ["ok"] * count,
            "pupil_left": [pupil_value] * count,
            "pupil_left_valid": ["ok"] * count,
        }).to_csv(path, index=False, encoding="utf-8-sig")

    source_a = fixture_dir / "valid-a.csv"
    source_bad = fixture_dir / "bad.csv"
    source_b = fixture_dir / "valid-b.csv"
    make_source(source_a, subject="subject-a", session="session-a", duration_s=2.0, pupil_value=3.3)
    make_source(source_bad, subject="", session="session-b", duration_s=2.0,
                pupil_value=3.3, blank_subject=True)
    make_source(source_b, subject="subject-b", session="session-b", duration_s=4.0, pupil_value=4.4)

    page = window.import_page
    signal_panel = page.sources["signals"]
    signal_panel.path_edit.setText(str(source_a))
    imported = read_table(source_a, role="signals", csv_options=signal_panel.csv_options())
    signal_panel._on_preview(signal_panel._generation, {
        "frame": imported.frame,
        "manifest": imported.manifest_entry(),
        "read_path": str(source_a),
        "read_csv_options": signal_panel.csv_options(),
        "sheets": (),
    })
    editor = signal_panel.mapping
    if editor is None:
        raise RuntimeError("Batch integration fixture has no signal mapping editor")
    mapping = {
        "subject_id": "subject_id", "session_id": "session_id", "time_s": "time_s",
        "gaze_x": "gaze_x", "gaze_y": "gaze_y", "gaze_valid": "gaze_valid",
        "pupil_left": "pupil_left", "pupil_left_valid": "pupil_left_valid",
    }
    for field_name, column in mapping.items():
        row = next((index for index, spec in enumerate(editor.fields) if spec.name == field_name), None)
        if row is None:
            raise RuntimeError(f"Batch integration fixture lacks mapping field {field_name}")
        choice = editor._table.cellWidget(row, 2)
        selected = choice.findData(column)
        if selected < 0:
            raise RuntimeError(f"Batch integration fixture cannot map {field_name} to {column}")
        choice.setCurrentIndex(selected)
    for field_name, unit in {"gaze_x": "px", "gaze_y": "px", "pupil_left": "mm"}.items():
        row = next(index for index, spec in enumerate(editor.fields) if spec.name == field_name)
        unit_choice = editor._table.cellWidget(row, 5)
        if unit_choice.findText(unit) < 0:
            raise RuntimeError(f"Batch integration fixture lacks unit {unit} for {field_name}")
        unit_choice.setCurrentText(unit)

    coordinate = page.coordinate
    time_index = coordinate.time_unit.findData("s")
    if time_index < 0:
        raise RuntimeError("Batch integration fixture has no seconds time unit")
    coordinate.time_unit.setCurrentIndex(time_index)
    coordinate.time_confirmed.setChecked(True)
    coordinate.precision.setValue(0.01)
    coordinate.coordinate_unit.setText("px")
    coordinate.coordinate_frame.setText("synthetic_top_left")
    coordinate.pupil_left_unit.setText("mm")
    coordinate.subject_id.clear()
    coordinate.session_id.clear()
    representation_index = coordinate.representation.findData("2d_point")
    if representation_index < 0:
        raise RuntimeError("Batch integration fixture has no explicit 2D point representation")
    coordinate.representation.setCurrentIndex(representation_index)
    coordinate.eye_fields["left_point_unit"].setText("px")
    coordinate.eye_fields["left_point_frame"].setText("synthetic_top_left")
    page._validity_codes.configure_validity_fields(editor.mapping(), signal_panel._preview)
    page._validity_codes.set_values({
        "gaze_valid": {"ok": True, "lost": False},
        "pupil_left_valid": {"ok": True, "lost": False},
    })

    with tempfile.TemporaryDirectory(prefix="hexiplan-batch-profiles-", dir=workspace) as profile_temp:
        profile_root = Path(profile_temp) / "profiles"
        real_save_profile = import_page_module.save_import_profile
        with (
            patch.object(import_page_module, "save_import_profile",
                         lambda profile: real_save_profile(profile, directory=profile_root)),
            patch.object(import_page_module, "list_import_profiles",
                         lambda: read_profile_listing(directory=profile_root)),
            patch.object(QInputDialog, "getText", return_value=("Fixed batch mapping", True)),
            patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok),
        ):
            page.refresh_import_profiles()
            page.save_current_import_profile()
            if "已保存“Fixed batch mapping”" not in page.import_profile_status.text():
                raise RuntimeError("Could not save isolated batch import profile: "
                                   + page.import_profile_status.text())
            # Exercise the user-facing profile application path before freezing the batch.
            pupil_row = next(index for index, spec in enumerate(editor.fields) if spec.name == "pupil_left")
            pupil_choice = editor._table.cellWidget(pupil_row, 2)
            pupil_choice.setCurrentIndex(pupil_choice.findData("gaze_x"))

            def accept_profile_review(dialog: Any) -> int:
                dialog.accept()
                return dialog.result()

            with patch.object(import_page_module._ProfileStageDialog, "exec", accept_profile_review):
                page.apply_selected_import_profile()
            if (editor.mapping().get("pupil_left") != "pupil_left"
                    or editor.units().get("pupil_left") != "mm"
                    or page._batch_profile_confirmation is None):
                raise RuntimeError("Batch integration did not restore and confirm the selected profile")

            selection = SelectionSpec(
                individual_metric_ids=("individual.pupil_left.mean",), run_individual=True,
            )
            window.set_selection(selection)
            window.set_parameters({"metadata": {"time_confirmed": True}})
            profile, request = page.batch_profile_and_request()
            if profile.name != "Fixed batch mapping" or request.signal_subject_id or request.signal_session_id:
                raise RuntimeError("Batch did not retain explicit source identity mapping and profile")

            window._open_batch_queue()
            window._batch_add_files([str(source_a), str(source_bad), str(source_b)])
            _wait_for_condition(app, lambda: window._batch_inspector is None,
                                "batch input hashing and structure inspection")
            queue = window._batch_queue
            if queue is None or len(queue.items) != 3:
                raise RuntimeError("Batch integration did not create exactly three queue items")
            if any(not item.structure_confirmed or not item.source_sha256 for item in queue.items):
                raise RuntimeError("Valid/bad/valid sources did not pass explicit structure and hash checks")
            expected_hashes = {
                str(source.resolve()): _sha256_bytes(source.read_bytes())
                for source in (source_a, source_bad, source_b)
            }
            if len(set(expected_hashes.values())) != 3:
                raise RuntimeError("Synthetic input fixtures do not have distinct source hashes")

            dialog = window._batch_dialog
            if dialog is None:
                raise RuntimeError("Batch dialog was not created for the prepared-start check")
            dispatch_calls: list[str | None] = []
            real_dispatch_next = window._batch_dispatch_next

            def counted_dispatch_next() -> None:
                dispatch_calls.append(window._batch_current_item_id)
                real_dispatch_next()

            window._batch_dispatch_next = counted_dispatch_next
            with patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_root)):
                dialog.start_button.click()
            app.processEvents()
            prepare_visible = (
                window._batch_prepared and not window._batch_active
                and dialog.start_button.text() == "按此配置开始"
                and "subject_id_default" in dialog.configuration_label.text()
                and output_root.name in dialog.configuration_label.text()
                and not dispatch_calls
                and window._batch_queue.configuration_fingerprint is None
                and window._batch_output_store is not None
                and window._batch_output_store.queue_directory is None
                and all(not item.attempts for item in window._batch_queue.items)
                and not page.import_active and not window.task_manager.active
                and not window.operation_manager.active
            )
            if not prepare_visible:
                raise RuntimeError(
                    "Preparing the batch must show the summary without freezing, writing output, or starting a worker: "
                    + json.dumps({
                        "prepared": window._batch_prepared,
                        "active": window._batch_active,
                        "button": dialog.start_button.text(),
                        "summary": dialog.configuration_label.text(),
                        "dispatches": dispatch_calls,
                        "configuration_fingerprint": window._batch_queue.configuration_fingerprint,
                        "queue_directory": (str(window._batch_output_store.queue_directory)
                                            if window._batch_output_store is not None
                                            and window._batch_output_store.queue_directory else None),
                        "attempts": [len(item.attempts) for item in window._batch_queue.items],
                        "import_active": page.import_active,
                        "task_active": window.task_manager.active,
                        "operation_active": window.operation_manager.active,
                        "status": window.status_label.text(),
                    }, ensure_ascii=False, default=str)
                )

            page.coordinate.subject_id.setText("prepared-only-default")
            app.processEvents()
            if window._batch_prepared:
                raise RuntimeError("Changing a default subject ID did not invalidate the prepared summary")
            page.coordinate.subject_id.clear()
            app.processEvents()

            with patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_root)):
                dialog.start_button.click()
            app.processEvents()
            if not window._batch_prepared or dispatch_calls:
                raise RuntimeError("The queue could not be prepared again after a default-ID change")

            extra_source = fixture_dir / "list-change.csv"
            make_source(extra_source, subject="subject-extra", session="session-extra",
                        duration_s=2.0, pupil_value=5.5)
            dialog.addFilesRequested.emit([str(extra_source)])
            _wait_for_condition(app, lambda: window._batch_inspector is None,
                                "inspect the added file after preparation invalidation")
            extra_item = next(
                (item for item in window._batch_queue.items
                 if str(Path(item.source_path).resolve()) == str(extra_source.resolve())),
                None,
            )
            if extra_item is None or window._batch_prepared:
                raise RuntimeError("Changing the batch input list did not invalidate its prepared summary")
            if not dialog.select_item(extra_item.item_id):
                raise RuntimeError("The added input was not selectable for list-change cleanup")
            dialog.remove_button.click()
            app.processEvents()
            if len(window._batch_queue.items) != 3:
                raise RuntimeError("The prepared-start check did not restore its original three inputs")

            with patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_root)):
                dialog.start_button.click()
            app.processEvents()
            if not window._batch_prepared or dispatch_calls:
                raise RuntimeError("Re-preparing the batch dispatched work before explicit continuation")
            dialog.cancel_button.click()
            app.processEvents()
            if (window._batch_prepared or dispatch_calls or window._batch_active
                    or window._batch_queue.configuration_fingerprint is not None
                    or any(item.attempts for item in window._batch_queue.items)):
                raise RuntimeError("Cancelling a prepared batch started work or changed queue attempts")

            alternate_output_root = workspace / "batch-output-alternate"
            with patch.object(QFileDialog, "getExistingDirectory", return_value=str(alternate_output_root)):
                dialog.start_button.click()
            app.processEvents()
            if not window._batch_prepared or dispatch_calls:
                raise RuntimeError("Selecting a new output root did not prepare without dispatch")
            old_prepared_signature = window._batch_prepared_signature
            # Re-emit the dialog's host intent with a changed output root to prove
            # the second-stage check refreshes the visible summary before dispatch.
            dialog.startRequested.emit(str(output_root))
            app.processEvents()
            if (not window._batch_prepared or dispatch_calls
                    or window._batch_prepared_signature == old_prepared_signature
                    or output_root.name not in dialog.configuration_label.text()):
                raise RuntimeError("Changing the prepared output root did not refresh its summary before start")
            dialog.start_button.click()
            app.processEvents()
            window._batch_dispatch_next = real_dispatch_next
            if not dispatch_calls or not window._batch_active:
                raise RuntimeError("Explicit continuation did not dispatch the first batch item")
            evidence["batch_prepare_gate"] = {
                "status": "pass",
                "checks": {
                    "prepare_shows_summary_without_dispatch": prepare_visible,
                    "default_id_change_invalidates": True,
                    "input_list_change_invalidates": True,
                    "cancel_prepared_does_not_create_attempt_or_run": True,
                    "output_root_change_refreshes_summary_before_dispatch": True,
                    "explicit_continue_dispatches_first_item": bool(dispatch_calls),
                },
                "dispatches_at_prepare": 0,
                "dispatches_after_explicit_continue": len(dispatch_calls),
                "selected_output_root": str(output_root.resolve()),
            }
            _wait_for_condition(
                app,
                lambda: (not window._batch_active and not page.import_active
                         and not window.task_manager.active and not window.operation_manager.active),
                "complete real valid/bad/valid queue",
                timeout_seconds=180.0,
            )
            items = list(window._batch_queue.items if window._batch_queue is not None else ())
            by_path = {str(Path(item.source_path).resolve()): item for item in items}
            if set(by_path) != set(expected_hashes):
                raise RuntimeError("Completed queue does not retain all three source paths")
            item_a = by_path[str(source_a.resolve())]
            item_bad = by_path[str(source_bad.resolve())]
            item_b = by_path[str(source_b.resolve())]
            records_by_id = {record.run_id: record for record in window.session.history}
            if (item_a.status not in {"complete", "partial"}
                    or item_b.status not in {"complete", "partial"}
                    or item_bad.status != "failed" or not item_bad.error or item_bad.run_id is not None
                    or item_a.export_status != "complete" or item_b.export_status != "complete"):
                def failed_metric_evidence(record: Any, table_name: str) -> list[dict[str, Any]]:
                    try:
                        frame = record.table_copy(table_name)
                    except (KeyError, ValueError):
                        return []
                    if not isinstance(frame, pd.DataFrame) or frame.empty:
                        return []
                    if "metric_id" in frame.columns:
                        selected = frame["metric_id"].astype("string").eq("individual.pupil_left.mean")
                        if selected.any():
                            frame = frame.loc[selected]
                    columns = [name for name in (
                        "metric_id", "value", "unit", "status", "reason_code", "reason",
                        "available", "required_fields", "required_metadata", "source_role",
                    ) if name in frame.columns]
                    return frame.loc[:, columns].head(12).to_dict(orient="records")

                run_failures = {
                    run_id: {
                        "status": records_by_id[run_id].status,
                        "metadata": dict(records_by_id[run_id].metadata()),
                        "logs": list(records_by_id[run_id].logs()),
                        "pupil_metric_rows": failed_metric_evidence(records_by_id[run_id], "metrics"),
                        "pupil_availability_rows": failed_metric_evidence(records_by_id[run_id], "availability"),
                    }
                    for run_id in (item_a.run_id, item_b.run_id)
                    if run_id and run_id in records_by_id
                }
                raise RuntimeError(
                    "Batch did not preserve separate analysis/export state or continue after the bad middle file: "
                    + json.dumps({
                        "items": [item.to_dict() for item in items],
                        "run_failures": run_failures,
                    }, ensure_ascii=False, default=str)
                )
            if (item_a.source_sha256 != expected_hashes[str(source_a.resolve())]
                    or item_bad.source_sha256 != expected_hashes[str(source_bad.resolve())]
                    or item_b.source_sha256 != expected_hashes[str(source_b.resolve())]
                    or item_a.run_id is None or item_b.run_id is None or item_a.run_id == item_b.run_id):
                raise RuntimeError("Batch source hashes or per-file run identities are not independent")

            record_a = records_by_id.get(str(item_a.run_id))
            record_b = records_by_id.get(str(item_b.run_id))
            if record_a is None or record_b is None or record_a.snapshot is None or record_b.snapshot is None:
                raise RuntimeError("Successful items did not retain immutable RunRecord snapshots")
            source_snapshot_parameters_a = record_a.snapshot.parameter_values()
            parameter_a = deepcopy(source_snapshot_parameters_a)
            parameter_b = record_b.snapshot.parameter_values()
            for source_name, parameter_values in (("valid-a.csv", parameter_a), ("valid-b.csv", parameter_b)):
                metadata = parameter_values.get("metadata", {})
                if (not isinstance(metadata, dict)
                        or metadata.get("pupil_unit") != "mm"
                        or metadata.get("pupil_units_by_eye") != {"left": "mm"}):
                    raise RuntimeError(
                        f"{source_name} RunSnapshot did not retain its confirmed source pupil units: "
                        + json.dumps(metadata, ensure_ascii=False, default=str)
                    )
            if any("H00001: inconsistent_units" in log
                   for record in (record_a, record_b) for log in record.logs()):
                raise RuntimeError("A confirmed pupil unit was lost before statistics consumed the metric")
            hashes_a = dict(record_a.snapshot.dataset_revision.source_hashes)
            hashes_b = dict(record_b.snapshot.dataset_revision.source_hashes)
            if (expected_hashes[str(source_a.resolve())] not in hashes_a.values()
                    or expected_hashes[str(source_b.resolve())] not in hashes_b.values()
                    or hashes_a == hashes_b):
                raise RuntimeError("RunSnapshot source provenance does not match each input independently")
            if (record_a.snapshot.selection.to_dict() != selection.to_dict()
                    or record_b.snapshot.selection.to_dict() != selection.to_dict()
                    or record_a.snapshot.parameter_values() != record_b.snapshot.parameter_values()):
                raise RuntimeError("Batch runs did not retain one shared frozen selection and parameter set")
            if (set(record_a.snapshot.dataset_copy().signals["subject_id"].astype(str)) != {"subject-a"}
                    or set(record_b.snapshot.dataset_copy().signals["subject_id"].astype(str)) != {"subject-b"}):
                raise RuntimeError("Batch RunSnapshot reused a prior file's normalized signal data")

            def metric_mean(record: Any) -> float:
                metrics = record.table_copy("metrics")
                rows = metrics.loc[metrics["metric_id"].astype("string").eq("individual.pupil_left.mean")]
                values = pd.to_numeric(rows["value"], errors="coerce").dropna().tolist()
                if len(values) != 1 or not math.isfinite(float(values[0])):
                    raise RuntimeError("Batch pupil mean is missing or ambiguous")
                return float(values[0])

            mean_a, mean_b = metric_mean(record_a), metric_mean(record_b)
            if math.isclose(mean_a, mean_b, rel_tol=1e-9, abs_tol=1e-9):
                raise RuntimeError("Valid fixtures did not produce distinct source-specific pupil means")
            if (not math.isclose(mean_a, 3.3, rel_tol=1e-9, abs_tol=1e-9)
                    or not math.isclose(mean_b, 4.4, rel_tol=1e-9, abs_tol=1e-9)):
                raise RuntimeError(f"Batch pupil means differ from the source-declared fixtures: {mean_a}, {mean_b}")

            from .main_window import _batch_parameters_for_revision
            prior_source_parameters = deepcopy(parameter_a)
            prior_source_parameters.setdefault("metadata", {}).update({
                "pupil_units_by_eye": {"left": "mm", "right": "mm"},
                "device_validation_evidence": [{"source": "prior-item-only"}],
            })
            unknown_dataset = record_b.snapshot.dataset_copy()
            unknown_analysis_metadata = dict(unknown_dataset.metadata.get("analysis_metadata", {}))
            unknown_analysis_metadata.update({
                "pupil_unit": "unknown",
                "pupil_units_by_eye": {"left": "unknown"},
                "device_validation_status": "unknown",
                "device_validation_method": None,
                "device_validation_evidence": [],
            })
            unknown_dataset.metadata["analysis_metadata"] = unknown_analysis_metadata
            unknown_revision = DatasetRevision.create(
                unknown_dataset, raw_sources=record_b.snapshot.dataset_revision.raw_source_bytes(),
            )
            unknown_parameters = _batch_parameters_for_revision(prior_source_parameters, unknown_revision)
            unknown_metadata = unknown_parameters.get("metadata", {})
            if (unknown_metadata.get("pupil_unit") != "unknown"
                    or unknown_metadata.get("pupil_units_by_eye") != {"left": "unknown"}
                    or unknown_metadata.get("device_validation_status") != "unknown"
                    or unknown_metadata.get("device_validation_evidence") != []):
                raise RuntimeError("A following source without confirmed units/evidence inherited a prior item's values")
            if record_a.snapshot.parameter_values() != source_snapshot_parameters_a:
                raise RuntimeError("Testing a following source mutated the earlier immutable RunSnapshot parameters")

            output_paths = [Path(path) for item in (item_a, item_b) for path in item.output_paths]
            if (any(not path.is_file() for path in output_paths)
                    or any(not any(str(path).lower().endswith(suffix) for path in item.output_paths)
                           for item in (item_a, item_b) for suffix in (".pyea", ".csv"))
                    or len({path.resolve() for path in output_paths}) != len(output_paths)):
                raise RuntimeError("Per-attempt results-only archives/CSV outputs are missing or collide")
            for item in (item_a, item_b):
                archive_path = next(Path(path) for path in item.output_paths
                                    if str(path).lower().endswith(".pyea"))
                archived = load_run_archive(archive_path)
                if (archived.archive_mode != "results_only" or archived.record.run_id != item.run_id
                        or "metrics" not in archived.record.table_names()):
                    raise RuntimeError("A results-only archive cannot be read back with its run identity/results")
                archived_parameters = archived.record.snapshot_details().get("parameters", {})
                archived_metadata = archived_parameters.get("metadata", {}) if isinstance(archived_parameters, dict) else {}
                if (archived_metadata.get("pupil_unit") != "mm"
                        or archived_metadata.get("pupil_units_by_eye") != {"left": "mm"}):
                    raise RuntimeError("A results-only archive lost the source-confirmed pupil unit parameters")

            store = window._batch_output_store
            if store is None or store.queue_directory is None:
                raise RuntimeError("Batch queue did not reserve its exclusive output directory")
            manifest_path = store.queue_directory / "manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError("Batch queue did not write its final manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_items = {str(row["item_id"]): row for row in manifest.get("items", [])}
            for item in (item_a, item_bad, item_b):
                row = manifest_items.get(item.item_id)
                if (row is None or row.get("source_sha256") != item.source_sha256
                        or row.get("analysis_status", row.get("status")) != item.status
                        or row.get("export_status") != item.export_status or row.get("run_id") != item.run_id):
                    raise RuntimeError("Final manifest lost source, run, or separate status information")

            dialog = window._batch_dialog
            if dialog is None or dialog.table.rowCount() != 3:
                raise RuntimeError("Batch queue UI did not display all three inputs")
            dialog.filter_choice.setCurrentIndex(dialog.filter_choice.findData("failed"))
            app.processEvents()
            visible_failures = sum(not dialog.table.isRowHidden(row) for row in range(dialog.table.rowCount()))
            if visible_failures != 1:
                raise RuntimeError("Batch failure filter did not isolate the bad source")
            dialog.filter_choice.setCurrentIndex(dialog.filter_choice.findData("all"))
            if not dialog.select_item(item_a.item_id) or not dialog.view_result_button.isEnabled():
                raise RuntimeError("Batch UI did not expose a successful result for navigation")
            window._batch_open_result(str(item_a.run_id))
            current_record = window.results_page.current_record()
            if current_record is None or current_record.run_id != item_a.run_id:
                raise RuntimeError("Batch result navigation did not open the selected RunRecord")

            evidence["batch_queue_integration"] = {
                "status": "pass",
                "workflow": "three real CSV sources through ImportPage normalization, preflight, pipeline, CSV export, and results-only archive",
                "profile": {"name": profile.name, "revision": profile.revision},
                "selection": selection.to_dict(),
                "per_file": [
                    {
                        "source": Path(item.source_path).name,
                        "source_sha256": item.source_sha256,
                        "analysis_status": item.status,
                        "analysis_attempts": len(item.attempts),
                        "run_id": item.run_id,
                        "dataset_revision": records_by_id[item.run_id].revision_id if item.run_id else None,
                        "run_source_hashes": dict(records_by_id[item.run_id].snapshot.dataset_revision.source_hashes)
                        if item.run_id else {},
                        "export_status": item.export_status,
                        "output_paths": list(item.output_paths),
                        "error": item.error,
                    }
                    for item in items
                ],
                "valid_source_pupil_means_mm": {"valid-a.csv": mean_a, "valid-b.csv": mean_b},
                "source_metadata_bound_per_snapshot": True,
                "unknown_following_source_does_not_inherit_units_or_device_evidence": True,
                "results_only_archives_preserve_source_metadata": True,
                "valid_runs_share_frozen_parameters": True,
                "bad_middle_input_did_not_block_last_input": item_b.run_id is not None,
                "manifest": str(manifest_path),
                "manifest_rows": len(manifest_items),
                "results_only_archives_read_back": 2,
                "ui_failure_filter_visible_rows": visible_failures,
                "ui_result_navigation_run_id": current_record.run_id,
                "output_collision_count": 0,
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
    remediation_evidence = _run_remediation_navigation_checks(app, window, record)
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
        "results_remediation_host": remediation_evidence,
    }


def _run_remediation_navigation_checks(app: Any, window: Any, record: Any) -> dict[str, Any]:
    """Verify Results and legacy Metrics actions reach real controls read-only."""
    from collections.abc import Mapping
    from copy import deepcopy
    from PySide6.QtCore import QTimer
    from pyeyedata.gui.metrics_page import MetricReasonDialog
    from .remediation import NavigationTarget

    revision = window.session.dataset_revision
    if revision is None:
        raise RuntimeError("Remediation navigation check requires the just-imported practice revision")
    revision_id = revision.revision_id

    def plain_copy(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): plain_copy(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain_copy(item) for item in value]
        return value

    generation = window.session.generation
    history_before = tuple(item.run_id for item in window.session.history)
    configuration_before = plain_copy(window.analysis_plan()["configuration"])
    record_snapshot_before = plain_copy(record.snapshot_details())
    metrics_before = record.table_copy("metrics")
    availability_before = record.table_copy("availability") if "availability" in record.table_names() else None
    checks: dict[str, Any] = {}

    def emit_from_results(target: NavigationTarget, diagnostic: dict[str, Any], *, requested_revision: str) -> None:
        window.results_page.remediationNavigationRequested.emit({
            "target": target,
            "run_id": str(record.run_id),
            "revision_id": requested_revision,
            "diagnostic": deepcopy(diagnostic),
        })
        app.processEvents()

    # Exercise the real ResultsPage signal and the exact ImportPage role field.
    emit_from_results(
        NavigationTarget("import", section="signals", field="pupil_left",
                         source_role="signals", metric_id="individual.pupil_left.mean"),
        {"metric_id": "individual.pupil_left.mean", "reason_code": "missing_fields:pupil_left"},
        requested_revision=revision_id,
    )
    signal_panel = window.import_page.sources["signals"]
    signal_row = next(index for index, spec in enumerate(signal_panel.mapping.fields)
                      if spec.name == "pupil_left")
    checks["result_signal_routes_to_exact_import_mapping"] = bool(
        window.step_list.currentRow() == 0
        and window.import_page.source_tabs.currentWidget() is signal_panel
        and signal_panel.mapping._table.currentRow() == signal_row
    )

    emit_from_results(
        NavigationTarget("import", section="context", field="pupil_unit",
                         metric_id="individual.pupil_left.mean"),
        {"metric_id": "individual.pupil_left.mean", "required_metadata": ["pupil_unit"]},
        requested_revision=revision_id,
    )
    checks["result_metadata_routes_to_eye_specific_unit"] = bool(
        "已定位到导入元数据字段 pupil_unit" in window.status_label.text()
        and window.import_page._coordinate_layout.currentIndex() == 0
    )

    emit_from_results(
        NavigationTarget("quality", section="processing", field="baseline_min_length_s"),
        {"metric_id": "quality.example", "parameter_path": "processing.baseline_min_length_s"},
        requested_revision=revision_id,
    )
    app.processEvents()
    checks["result_processing_parameter_routes_to_control"] = bool(
        window.step_list.currentRow() == 1
        and "已定位到数据预处理参数 baseline_min_length_s" in window.status_label.text()
        and window.quality_page._parameter_error_name == "baseline_min_length_s"
    )

    metric_form = window.metrics_page._forms.get("individual")
    metric_field = next(iter(metric_form.editors), None) if metric_form is not None else None
    if not metric_field:
        raise RuntimeError("The live MetricsPage schema has no individual parameter editor for navigation")
    emit_from_results(
        NavigationTarget("metrics", section="individual", field=metric_field,
                         metric_id="individual.pupil_left.mean"),
        {"metric_id": "individual.pupil_left.mean", "parameter_path": f"individual.{metric_field}"},
        requested_revision=revision_id,
    )
    checks["result_metric_parameter_routes_to_exact_control"] = bool(
        window.step_list.currentRow() == 2
        and "已定位到 individual 参数" in window.status_label.text()
        and window.metrics_page.tabs.currentWidget() is window.metrics_page.parameter_tabs
        and window.metrics_page.parameter_tabs.currentIndex() == 0
        and metric_field in window.metrics_page._forms["individual"].editors
    )

    from .method_registry import group_parameter_fields

    group_definitions = group_parameter_fields()
    group_field = next((str(item.get("name")) for item in group_definitions if item.get("name")), None)
    if not group_field:
        raise RuntimeError("The product GroupPage registry has no structured parameter for navigation")
    emit_from_results(
        NavigationTarget("group", section="group", field=group_field),
        {"metric_id": "group.example", "required_fields": [group_field]},
        requested_revision=revision_id,
    )
    app.processEvents()
    checks["result_group_requirement_routes_to_known_page_fallback"] = bool(
        window.step_list.currentRow() == 3
        and "已打开多人分析页" in window.status_label.text()
        and "没有可精确定位的参数" in window.status_label.text()
    )

    emit_from_results(
        NavigationTarget("design", section="aois", source_role="aois", aoi_id=0),
        {"reason_code": "aoi_geometry_zero_area", "aoi_id": 0},
        requested_revision=revision_id,
    )
    checks["result_aoi_issue_routes_to_editor_without_guessing_binding"] = bool(
        window.step_list.currentRow() == 0
        and window.import_page.source_tabs.currentWidget() is window.import_page.sources["aois"]
        and "AOI=0" in window.status_label.text()
        and "旧运行图片不会自动绑定" in window.status_label.text()
    )

    old_metric_reason = MetricReasonDialog(
        {"metric_id": "individual.pupil_left.mean", "label": "瞳孔均值",
         "status": "unavailable", "reason_code": "missing_fields"},
        "individual",
        [{"metric_id": "individual.pupil_left.mean", "required_fields": ["pupil_left"]}],
        window.metrics_page,
    )
    old_metric_reason.edit_requested.connect(window._on_edit_requirements)
    QTimer.singleShot(10, old_metric_reason.edit_button.click)
    modal_result = old_metric_reason.exec()
    checks["legacy_metrics_reason_routes_and_closes_modal"] = bool(
        int(modal_result) == 1 and window.step_list.currentRow() == 0
        and window.import_page.source_tabs.currentWidget() is signal_panel
    )

    stale_revision = "archived-revision-for-diagnostic"
    emit_from_results(
        NavigationTarget("import", section="signals", field="pupil_left", source_role="signals"),
        {"reason_code": "missing_fields:pupil_left"},
        requested_revision=stale_revision,
    )
    checks["old_revision_is_explained_without_loading_data"] = bool(
        stale_revision in window.status_label.text()
        and str(revision_id) in window.status_label.text()
        and window.session.dataset_revision is revision
    )

    row_before_unknown = window.step_list.currentRow()
    window._route_remediation_navigation({
        "target": None, "run_id": str(record.run_id), "revision_id": revision_id,
        "diagnostic": {"reason_code": "future_reason_42", "reason": "preserve original"},
    })
    checks["unknown_reason_keeps_text_without_fake_target"] = bool(
        window.step_list.currentRow() == row_before_unknown
        and "没有可确认的设置入口" in window.status_label.text()
        and "未推测目标位置" in window.status_label.text()
    )

    checks["navigation_does_not_start_or_mutate_analysis"] = bool(
        window.session.generation == generation
        and tuple(item.run_id for item in window.session.history) == history_before
        and window.session.dataset_revision is revision
        and window.analysis_plan()["configuration"] == configuration_before
        and plain_copy(record.snapshot_details()) == record_snapshot_before
        and record.table_copy("metrics").equals(metrics_before)
        and (availability_before is None or record.table_copy("availability").equals(availability_before))
    )
    window.step_list.setCurrentRow(6)
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise RuntimeError("Results remediation host checks failed: " + ", ".join(failures))
    return {"passed": True, "checks": checks, "revision_id": revision_id, "run_id": record.run_id}


def _capture_timing_ui_screenshots(
    app: Any, window: Any, workspace: Path, evidence: dict[str, Any],
) -> None:
    """Capture synthetic live timing labels from the actual offscreen widgets."""
    if str(app.platformName()).casefold() != "offscreen":
        evidence["task_timing_screenshots"] = {
            "captured": False, "reason": "offscreen-only evidence",
        }
        return
    if window.task_manager.active or window.operation_manager.active or window.import_page.import_active:
        evidence["task_timing_screenshots"] = {
            "captured": False, "reason": "a real background task is still active",
        }
        return

    previous_timing = window._active_timing
    previous_identity = window._timing_identity
    previous_label = window.timing_label.text()
    timer_was_active = window._timing_timer.isActive()
    window._timing_timer.stop()
    screenshot_identity = ("timing-diagnostic-screenshot", id(window))
    window._open_batch_queue()
    window._start_timing(
        "run", "合成诊断：后台分析阶段",
        identity=screenshot_identity, workload_key="diagnostic-screenshot",
    )
    app.processEvents()
    main_path = workspace / "timing-main-window-running.png"
    if not window.grab().save(str(main_path), "PNG"):
        raise RuntimeError("Unable to save the offscreen timing main-window screenshot")
    dialog = window._batch_dialog
    queue_label = "队列剩余：2 项 · 预计约 1分20秒（合成诊断显示）"
    dialog.set_timing_status(window.timing_label.text(), queue_label)
    app.processEvents()
    batch_path = workspace / "timing-batch-dialog-running.png"
    if not dialog.grab().save(str(batch_path), "PNG"):
        raise RuntimeError("Unable to save the offscreen batch timing screenshot")
    evidence["task_timing_screenshots"] = {
        "captured": True,
        "synthetic_only": True,
        "main_window": main_path.name,
        "batch_dialog": batch_path.name,
        "current_label": window.timing_label.text(),
        "queue_label": queue_label,
    }
    window._finish_timing("cancelled", train=False, identity=screenshot_identity)
    dialog.hide()
    window._active_timing = previous_timing
    window._timing_identity = previous_identity
    window.timing_label.setText(previous_label)
    if timer_was_active:
        window._timing_timer.start()


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
                    "application_icon_available": not app.windowIcon().isNull(),
                    "window_icon_available": not window.windowIcon().isNull(),
                    "icon_sizes": [size.width() for size in app.windowIcon().availableSizes()],
                    "task_manager": type(window.task_manager).__name__,
                    "operation_manager": type(window.operation_manager).__name__},
        })
        from pyeyedata_bootstrap import verify_runtime_sources
        evidence["process"]["module_origins"] = verify_runtime_sources()
        if config["scenario"] == "synthetic-pipeline":
            _run_pipeline_scenario(app, window, workspace, evidence)
        elif config["scenario"] == "open-archive":
            _run_open_archive(app, window, config, evidence)
        elif config["scenario"] == "reliability-smoke":
            from .diagnostic_ui import run_ui_diagnostics

            ui_evidence = run_ui_diagnostics(app, window)
            if not ui_evidence.get("passed"):
                raise RuntimeError(
                    "main-window UI diagnostics failed: "
                    + ", ".join(str(item) for item in ui_evidence.get("failures", ()))
                )
            evidence["main_window_ui_diagnostics"] = ui_evidence
            from .diagnostic_catalog_ui import run_catalogue_ui_diagnostics

            catalogue_ui_evidence = run_catalogue_ui_diagnostics(app, window, workspace)
            if catalogue_ui_evidence.get("status") != "passed":
                raise RuntimeError(
                    "metric catalogue UI diagnostics failed: "
                    + ", ".join(str(item) for item in catalogue_ui_evidence.get("failures", ()))
                )
            evidence["metric_catalogue_ui_diagnostics"] = catalogue_ui_evidence
            from .diagnostic_runtime import run_runtime_checks
            evidence["runtime_resolution"] = run_runtime_checks()
            _run_reliability_smoke(app, window, workspace, evidence)
            from .diagnostic_common_metrics import run_common_metric_checks
            evidence["common_eye_metrics"] = run_common_metric_checks(workspace / "common-eye-metrics")
            evidence["aoi_editor_host_integration"] = _run_aoi_editor_host_integration(app, workspace)
            _run_batch_valid_bad_valid(app, window, workspace, evidence)
            _capture_timing_ui_screenshots(app, window, workspace, evidence)
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
