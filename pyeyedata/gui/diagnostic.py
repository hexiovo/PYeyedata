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
SCENARIOS = frozenset({"gui-startup", "synthetic-pipeline", "open-archive"})
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
        raise DiagnosticConfigError("scenario must be gui-startup, synthetic-pipeline, or open-archive")
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
        window._open_help("diagnostic")
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
        evidence["status"] = "pass"
    except Exception as exc:
        failure = exc
        evidence["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            if "window" in locals():
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
