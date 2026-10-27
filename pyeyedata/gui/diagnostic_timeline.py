"""Identity-safe navigation helpers for quality-page timeline diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


_STREAM_SCOPE = (
    "analysis_stream_id", "source_id", "subject_id", "session_id", "interaction_id", "trial",
    "physical_partition_id", "processing_partition_id", "timebase_id", "clock_id",
)
_STRONG_SCOPE = {
    "analysis_stream_id", "source_id", "physical_partition_id", "processing_partition_id",
    "timebase_id", "clock_id",
}
_SOURCE_VALUES = (
    "gaze_x", "gaze_y", "gaze_z", "pupil_left", "pupil_right",
    "gaze_valid", "pupil_left_valid", "pupil_right_valid",
)
_NUMERIC_SOURCE_VALUES = {"gaze_x", "gaze_y", "gaze_z", "pupil_left", "pupil_right", "time_s"}
_DERIVED_SCOPE_VALUES = {"analysis_stream_id", "physical_partition_id", "processing_partition_id"}


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if np.ndim(result) == 0 else False
    except (TypeError, ValueError):
        return False


def _token(value: Any) -> tuple[str, str]:
    if _missing(value):
        return ("<missing>", "")
    if isinstance(value, np.generic):
        value = value.item()
    return type(value).__name__, str(value)


def _same_value(left: Any, right: Any, column: str) -> bool:
    if _missing(left) or _missing(right):
        return _missing(left) and _missing(right)
    if column in _NUMERIC_SOURCE_VALUES:
        try:
            return float(left) == float(right)
        except (TypeError, ValueError, OverflowError):
            return False
    # Validity values may be strings decoded by an explicit import profile.
    # Python truthiness would make every nonempty code (including "lost") true;
    # compare the recorded value exactly and leave interpretation to import.
    return _token(left) == _token(right)


@dataclass(frozen=True)
class RevisionMatch:
    matches: bool
    reason: str


def validate_outcome_revision(
    revision: Any,
    raw_signals: Any,
    processing: Any,
    outcome: Any,
) -> RevisionMatch:
    """Fail closed unless processing samples prove they came from this revision.

    The host also guards PipelineOutcome.run_id/generation through AnalysisSession.
    This local check covers the revision boundary using stable sample identity and
    unchanged source columns, since PipelineOutcome does not carry a revision ID.
    """
    revision_id = getattr(revision, "revision_id", None)
    payload = getattr(outcome, "payload", outcome)
    hints: list[Any] = [getattr(outcome, "revision_id", None), getattr(outcome, "dataset_revision_id", None)]
    for owner in (getattr(outcome, "metadata", None), getattr(payload, "metadata", None),
                  getattr(processing, "audit", None)):
        if isinstance(owner, Mapping):
            hints.extend((owner.get("revision_id"), owner.get("dataset_revision_id")))
    explicit = [str(value) for value in hints if isinstance(value, str) and value]
    if revision_id is not None and any(value != str(revision_id) for value in explicit):
        return RevisionMatch(False, "结果显式声明的数据修订与当前修订不一致。")

    raw = raw_signals if isinstance(raw_signals, pd.DataFrame) else pd.DataFrame()
    processed = getattr(processing, "signals", None)
    if not isinstance(processed, pd.DataFrame) or processed.empty:
        return RevisionMatch(False, "返回结果没有可核对的 processing.signals。")
    if raw.empty:
        return RevisionMatch(False, "当前修订没有信号行，无法核对返回结果来源。")
    if "observation_id" not in raw or "observation_id" not in processed:
        return RevisionMatch(False, "当前修订或 processing.signals 缺少 observation_id，无法唯一核对样本。")
    if "time_s" not in raw or "time_s" not in processed:
        return RevisionMatch(False, "当前修订或 processing.signals 缺少 time_s，无法核对时间基。")
    if "source_id" not in raw or "source_id" not in processed:
        return RevisionMatch(False, "当前修订或 processing.signals 缺少 source_id，无法核对来源。")

    raw_ids = raw["observation_id"]
    processed_ids = processed["observation_id"]
    if raw_ids.isna().any() or processed_ids.isna().any() or not raw_ids.is_unique or not processed_ids.is_unique:
        return RevisionMatch(False, "observation_id 缺失或重复，无法做唯一的修订匹配。")
    raw_keys = {_token(value) for value in raw_ids}
    processed_keys = {_token(value) for value in processed_ids}
    if raw_keys != processed_keys:
        return RevisionMatch(False, "processing.signals 的 observation_id 集合与当前修订不一致。")

    raw_by_id = {_token(row["observation_id"]): row for row in raw.to_dict(orient="records")}
    processed_by_id = {_token(row["observation_id"]): row for row in processed.to_dict(orient="records")}
    required_columns = {"observation_id", "time_s", "source_id"}
    eligible_columns = set(_SOURCE_VALUES) | (set(_STREAM_SCOPE) - _DERIVED_SCOPE_VALUES) | {
        "observation_id", "time_s", "original_index", "source_row", "stimulus_id", "frame_id",
    }
    compare_columns: list[tuple[str, str]] = []
    for column in raw.columns:
        if column not in eligible_columns:
            continue
        backup_column = f"raw__{column}"
        processed_column = backup_column if column in _SOURCE_VALUES and backup_column in processed else column
        if processed_column in processed.columns:
            compare_columns.append((column, processed_column))
    compared_raw_columns = {raw_column for raw_column, _ in compare_columns}
    if not required_columns.issubset(compared_raw_columns):
        return RevisionMatch(False, "缺少完整的样本身份、来源或时间字段，无法核对当前修订。")
    for key in raw_keys:
        left, right = raw_by_id[key], processed_by_id[key]
        for raw_column, processed_column in compare_columns:
            if not _same_value(left.get(raw_column), right.get(processed_column), raw_column):
                return RevisionMatch(False, f"样本 {left.get('observation_id')} 的 {raw_column} 与当前修订不一致。")
    return RevisionMatch(True, "processing.signals 的样本身份、来源、秒时间和原始源值备份与当前修订一致。")


@dataclass(frozen=True)
class DiagnosticLocation:
    stream_key: Any | None
    start_s: float | None
    end_s: float | None
    record_kind: str
    message: str
    row: Mapping[str, Any]

    @property
    def has_range(self) -> bool:
        return self.start_s is not None and self.end_s is not None and self.end_s > self.start_s

    @property
    def has_point(self) -> bool:
        return self.start_s is not None and self.end_s is not None and self.start_s == self.end_s


def resolve_diagnostic_location(
    record: Mapping[str, Any],
    stream_options: Iterable[Any],
    *,
    record_kind: str | None = None,
) -> DiagnosticLocation:
    """Resolve an event/audit row to one exact stream key and time in seconds."""
    row_value = record.get("row") if isinstance(record.get("row"), Mapping) else record
    row = dict(row_value)
    kind = str(record_kind or record.get("kind") or row.get("record_type") or "diagnostic")
    strong = [name for name in _STRONG_SCOPE if name in row and not _missing(row.get(name))]
    if not strong:
        return DiagnosticLocation(None, None, None, kind,
                                  "记录没有可确认的 stream/source/partition/timebase 身份，无法定位。", row)

    candidate_options: list[Any] = []
    for option in stream_options:
        selectors = tuple(getattr(option, "selectors", ()))
        selector_map = dict(selectors)
        stream_id = row.get("analysis_stream_id")
        has_stream_match = not _missing(stream_id) and str(stream_id) == str(getattr(option, "stream_id", ""))
        # Any strong identity present on the record must be verifiable by this
        # option. Never ignore a foreign clock/partition because a weaker
        # subject or session selector happens to match.
        unverified_scope = [
            name for name in _STREAM_SCOPE
            if name != "analysis_stream_id" and name in row and not _missing(row.get(name))
            and (name not in selector_map or _missing(selector_map.get(name)))
        ]
        if unverified_scope:
            continue
        compared = [
            name for name in _STREAM_SCOPE
            if name != "analysis_stream_id" and name in row and name in selector_map
            and not _missing(row.get(name)) and not _missing(selector_map.get(name))
        ]
        if not has_stream_match and not any(name in _STRONG_SCOPE for name in compared):
            continue
        if any(_token(row[name]) != _token(selector_map[name]) for name in compared):
            continue
        # When the record carries a stream ID, it must agree with the option.
        if not _missing(stream_id) and not has_stream_match:
            continue
        candidate_options.append(option)
    if len(candidate_options) != 1:
        detail = "没有匹配的稳定数据流。" if not candidate_options else "多个数据流符合该记录，身份不足以唯一定位。"
        return DiagnosticLocation(None, None, None, kind, detail, row)

    start = record.get("start_s", row.get("start_s"))
    end = record.get("end_s", row.get("end_s"))
    try:
        start_value, end_value = float(start), float(end)
    except (TypeError, ValueError, OverflowError):
        start_value = end_value = math.nan
    if math.isfinite(start_value) and math.isfinite(end_value) and end_value >= start_value:
        time_start, time_end = start_value, end_value
    else:
        try:
            point = float(row.get("time_s"))
        except (TypeError, ValueError, OverflowError):
            point = math.nan
        if not math.isfinite(point):
            return DiagnosticLocation(None, None, None, kind,
                                      "数据流身份可唯一匹配，但记录没有有效的秒时间或区间。", row)
        time_start = time_end = point
    option = candidate_options[0]
    return DiagnosticLocation(
        getattr(option, "key", None), time_start, time_end, kind,
        "记录已唯一映射到来源、分区和时间基。", row,
    )


def diagnostic_rows_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare event/audit records without relying on their row positions."""
    left_kind = str(left.get("record_type", left.get("kind", "")))
    right_kind = str(right.get("record_type", right.get("kind", "")))
    if left_kind and right_kind and left_kind != right_kind:
        return False
    left_row = left.get("row") if isinstance(left.get("row"), Mapping) else left
    right_row = right.get("row") if isinstance(right.get("row"), Mapping) else right
    left_row, right_row = dict(left_row), dict(right_row)
    identity_columns = (
        "event_id", "event_instance_id", "interval_id", "source_event_id", "observation_id",
        "start_observation_id", "end_observation_id", "analysis_stream_id", "source_id",
        "physical_partition_id", "processing_partition_id", "timebase_id", "clock_id",
        "start_s", "end_s", "time_s", "event_type", "decision", "reason_code",
    )
    compared = [name for name in identity_columns if name in left_row and name in right_row]
    strong = {"event_id", "event_instance_id", "interval_id", "source_event_id", "observation_id",
              "analysis_stream_id", "source_id", "physical_partition_id", "processing_partition_id",
              "timebase_id", "clock_id"}
    if not any(name in strong and not _missing(left_row.get(name)) and not _missing(right_row.get(name))
               for name in compared):
        return False
    return all(_same_value(left_row.get(name), right_row.get(name), name) for name in compared)


def run_timeline_integration_checks() -> dict[str, Any]:
    """Run bounded offscreen assertions for the QualityPage/timeline integration."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication
    from matplotlib.backend_bases import MouseEvent
    from pyeyedata.processing import ProcessingConfig, process_signals
    from pyeyedata.scopes import observation_identity_frame

    from pyeyedata.gui.quality_page import QualityPage
    from pyeyedata.gui.timeline_model import status_values

    app = QApplication.instance() or QApplication([])
    raw = pd.DataFrame([
        {"observation_id": "b-1", "source_id": "source-B", "subject_id": "s2", "session_id": "run",
         "physical_partition_id": "pB", "processing_partition_id": "bp", "timebase_id": "tbB",
         "clock_id": "clockB", "original_index": 1, "time_s": 1.0, "gaze_x": 21.0, "gaze_y": 2.0,
         "gaze_valid": True},
        {"observation_id": "a-0", "source_id": "source-A", "subject_id": "s1", "session_id": "run",
         "physical_partition_id": "pA", "processing_partition_id": "ap", "timebase_id": "tbA",
         "clock_id": "clockA", "original_index": 0, "time_s": 0.0, "gaze_x": 10.0, "gaze_y": 1.0,
         "gaze_valid": True},
        {"observation_id": "b-0", "source_id": "source-B", "subject_id": "s2", "session_id": "run",
         "physical_partition_id": "pB", "processing_partition_id": "bp", "timebase_id": "tbB",
         "clock_id": "clockB", "original_index": 0, "time_s": 0.0, "gaze_x": 20.0, "gaze_y": 2.0,
         "gaze_valid": True},
        {"observation_id": "a-1", "source_id": "source-A", "subject_id": "s1", "session_id": "run",
         "physical_partition_id": "pA", "processing_partition_id": "ap", "timebase_id": "tbA",
         "clock_id": "clockA", "original_index": 1, "time_s": 1.0, "gaze_x": 11.0, "gaze_y": 1.0,
         "gaze_valid": False},
        {"observation_id": "a-2", "source_id": "source-A", "subject_id": "s1", "session_id": "run",
         "physical_partition_id": "pA", "processing_partition_id": "ap", "timebase_id": "tbA",
         "clock_id": "clockA", "original_index": 2, "time_s": 2.0, "gaze_x": np.nan, "gaze_y": np.nan,
         "gaze_valid": pd.NA},
    ])
    processed = raw.iloc[[4, 2, 3, 0, 1]].copy().reset_index(drop=True)
    processed["analysis_stream_id"] = processed["source_id"].map({"source-A": "gaze:A", "source-B": "gaze:B"})
    processed["source_gaze_valid"] = processed["gaze_valid"].astype("boolean")
    processed["gaze_x_interpolated"] = False
    processed.loc[processed["observation_id"].eq("a-1"), "gaze_x_interpolated"] = True
    analysis_streams = processed[["observation_id", "analysis_stream_id", "source_id", "subject_id", "session_id",
                                  "physical_partition_id", "processing_partition_id", "timebase_id", "clock_id"]].copy()
    events = pd.DataFrame([{
        "event_id": "event-a", "event_type": "fixation", "analysis_stream_id": "gaze:A",
        "source_id": "source-A", "subject_id": "s1", "session_id": "run", "physical_partition_id": "pA",
        "processing_partition_id": "ap", "timebase_id": "tbA", "clock_id": "clockA",
        "start_s": 0.5, "end_s": 0.8, "status": "valid",
    }])
    audit = pd.DataFrame([{
        "interval_id": "audit-a", "analysis_stream_id": "gaze:A", "source_id": "source-A",
        "subject_id": "s1", "session_id": "run", "physical_partition_id": "pA",
        "processing_partition_id": "ap", "timebase_id": "tbA", "clock_id": "clockA",
        "start_s": 0.5, "end_s": 0.8, "decision": "accepted", "reason_code": "within_support",
    }])
    processing = SimpleNamespace(
        signals=processed, quality=pd.DataFrame([{"source_id": "source-A", "status": "available"}]),
        analysis_streams=analysis_streams, events=events, event_interval_audit=audit,
        analysis_memberships=pd.DataFrame(), segment_overlap=pd.DataFrame(), execution_status="partial",
    )

    class _Revision:
        revision_id = "revision-a"

        def __init__(self, frame: pd.DataFrame) -> None:
            self._frame = frame.copy(deep=True)

        def dataset_copy(self) -> Any:
            return SimpleNamespace(signals=self._frame.copy(deep=True))

    revision = _Revision(raw)
    source_before = revision.dataset_copy().signals.copy(deep=True)
    outcome = SimpleNamespace(
        payload=SimpleNamespace(processing=processing, metadata={"metric_calculations_run": False}),
        metadata={}, phase="preflight", backend_status="partial", generation=2, run_id="run-a",
    )
    page = QualityPage()
    page.set_dataset(revision)
    configuration_before = page.processing_config()
    page.set_outcome(outcome)
    if page.timeline.model.stream_options:
        first_key = page.timeline.model.stream_options[0].key
        page.timeline.set_stream(first_key)
    prepared = page.timeline.model.prepare(page.timeline.stream_selector.currentData())
    stream_isolated = (not prepared.raw.empty and prepared.raw["source_id"].astype(str).nunique() == 1
                       and prepared.processed["source_id"].astype(str).nunique() == 1
                       and set(prepared.raw["source_id"].astype(str)) == set(prepared.processed["source_id"].astype(str)))
    order_paired = (not prepared.raw.empty and "observation_id" in prepared.raw and "observation_id" in prepared.processed
                    and prepared.raw["observation_id"].tolist() == prepared.processed["observation_id"].tolist()
                    and pd.to_numeric(prepared.raw["time_s"], errors="coerce").tolist()
                    == pd.to_numeric(prepared.processed["time_s"], errors="coerce").tolist())

    range_hits: list[tuple[float, float]] = []
    page.timeline.intervalSelected.connect(lambda start, end: range_hits.append((start, end)))
    page.timeline._on_span_selected(0.2, 0.4)
    range_selection = bool(range_hits and page.timeline.selected_interval == (0.2, 0.4))

    event_hits: list[Any] = []
    page.timeline.diagnosticSelected.connect(event_hits.append)
    page.timeline.canvas.draw()
    clicked_event = False
    if page.timeline._hit_artists:
        artist, _payload = page.timeline._hit_artists[0]
        bounds = artist.get_window_extent(page.timeline.canvas.get_renderer())
        x, y = bounds.x0 + max(bounds.width / 2.0, 1.0), bounds.y0 + max(bounds.height / 2.0, 1.0)
        mouse = MouseEvent("button_press_event", page.timeline.canvas, x, y, button=1)
        page.timeline._on_mouse_press(mouse)
        clicked_event = bool(event_hits)
    event_range_selected = page.timeline.selected_interval == (0.5, 0.8)
    selected_indexes = page.diagnostic_view.selectionModel().selectedIndexes()
    event_row_reselected = bool(selected_indexes)
    selected_diagnostic_rows = [
        page.diagnostic_model.frame.iloc[index.row()].to_dict()
        for index in selected_indexes if index.column() == 0 and index.row() < len(page.diagnostic_model.frame)
    ]
    interval_after_event_click = page.timeline.selected_interval
    locator_after_event_click = page.locator_label.text()
    event_reason_preserved = any(
        not _missing(row.get(column)) and str(row.get(column)) in page.locator_label.text()
        for row in selected_diagnostic_rows
        for column in ("reason_code", "reason", "decision", "status", "event_type")
    )

    source_states, _ = status_values(prepared.raw, ("source_gaze_valid", "gaze_valid"))
    interpolated_states, _ = status_values(prepared.processed, ("gaze_x_interpolated",))
    validity_preserved = bool(np.any(source_states == 0.0) and np.any(~np.isfinite(source_states))
                              and np.any(interpolated_states == 1.0))

    old_revision_processed = not page.timeline.model.processed_signals.empty
    replacement = _Revision(raw.assign(source_id=raw["source_id"].astype(str) + "-new"))
    page.set_dataset(replacement)
    revision_clears = (page._last_outcome is None and page.timeline.selected_interval is None
                       and page.timeline.model.processed_signals.empty and old_revision_processed)
    page.set_outcome(outcome)
    stale_rejected = page.timeline.model.processed_signals.empty and "修订" in page.status.text()

    page.set_dataset(revision)
    missing_outcome = SimpleNamespace(payload=SimpleNamespace(processing=None, metadata={}), metadata={}, generation=3)
    page.set_outcome(missing_outcome)
    missing_explained = page.timeline.model.processed_signals.empty and "不可用" in page.timeline_status.text()
    object_unchanged = revision.dataset_copy().signals.equals(source_before)
    configuration_unchanged = page.processing_config() == configuration_before
    unknown_location = resolve_diagnostic_location(
        {"kind": "event", "row": {"event_type": "fixation", "reason_code": "missing_scope",
                                    "start_s": 0.2, "end_s": 0.3}},
        page.timeline.model.stream_options,
    )
    unknown_location_rejected = unknown_location.stream_key is None

    foreign_clock_record = {
        "kind": "event",
        "row": {
            "event_type": "fixation", "source_id": "source-A", "subject_id": "s1",
            "session_id": "run", "clock_id": "foreign-clock", "start_s": 0.2, "end_s": 0.3,
        },
    }
    weaker_option = SimpleNamespace(
        selectors=(("source_id", "source-A"), ("subject_id", "s1"), ("session_id", "run")),
        stream_id="gaze:A", key=("gaze:A", "source-A"),
    )
    foreign_clock_location = resolve_diagnostic_location(foreign_clock_record, (weaker_option,))
    empty_clock_option = SimpleNamespace(
        selectors=(
            ("source_id", "source-A"), ("subject_id", "s1"), ("session_id", "run"),
            ("clock_id", pd.NA),
        ),
        stream_id="gaze:A", key=("gaze:A", "source-A"),
    )
    empty_clock_location = resolve_diagnostic_location(foreign_clock_record, (empty_clock_option,))

    # Exercise the revision guard against real processing output that changes a
    # source value but retains the source value in raw__* for traceability.
    real_raw = pd.DataFrame({
        "subject_id": ["real-subject"] * 9,
        "session_id": ["real-session"] * 9,
        "source_id": ["real-source"] * 9,
        "physical_partition_id": ["source-partition"] * 9,
        "processing_partition_id": ["source-processing-partition"] * 9,
        "timebase_id": ["real-timebase"] * 9,
        "clock_id": ["real-clock"] * 9,
        "time_s": np.arange(9, dtype=float) * 0.01,
        "original_index": np.arange(9),
        "gaze_x": [0.0, 1.0, 2.0, 3.0, np.nan, 5.0, 6.0, 7.0, 8.0],
        "gaze_y": [1.0] * 9,
        "gaze_valid": [True] * 9,
        "pupil_left": [2.0] * 9,
        "pupil_right": [2.0] * 9,
    })
    real_raw, _ = observation_identity_frame(real_raw, source_id="real-source")
    real_processing = process_signals(
        real_raw,
        ProcessingConfig(interpolate_gaze=True, gaze_max_gap_s=0.03,
                         allow_unverified_interpolation_bounds=True),
    )
    real_outcome = SimpleNamespace(payload=SimpleNamespace(processing=real_processing), metadata={})
    real_revision = _Revision(real_raw)
    real_revision_match = validate_outcome_revision(
        real_revision, real_raw, real_processing, real_outcome,
    )
    edited_real_raw = real_raw.copy(deep=True)
    edited_real_raw.loc[
        edited_real_raw["observation_id"].eq(real_raw.iloc[4]["observation_id"]), "gaze_x"
    ] = 99.0
    edited_revision = _Revision(edited_real_raw)
    edited_revision_match = validate_outcome_revision(
        edited_revision, edited_real_raw, real_processing, real_outcome,
    )
    source_value_revision_rejected = not edited_revision_match.matches
    interpolated_row = real_processing.signals.loc[
        real_processing.signals["observation_id"].eq(real_raw.iloc[4]["observation_id"])
    ].iloc[0]
    interpolation_flag = interpolated_row.get("gaze_x_interpolated")
    actual_value_changed = bool(
        pd.notna(interpolated_row.get("gaze_x"))
        and pd.isna(interpolated_row.get("raw__gaze_x"))
        and not _missing(interpolation_flag)
        and bool(interpolation_flag)
    )

    # Validity strings are opaque unless import explicitly decoded them. The
    # revision guard must never equate distinct nonempty codes by truthiness.
    code_raw = raw.copy(deep=True)
    code_raw["gaze_valid"] = code_raw["gaze_valid"].map({True: "ok", False: "lost"}).astype("string")
    code_processed = processed.copy(deep=True)
    code_processed["gaze_valid"] = code_processed["gaze_valid"].map({True: "ok", False: "lost"}).astype("string")
    code_processing = SimpleNamespace(signals=code_processed)
    code_outcome = SimpleNamespace(payload=SimpleNamespace(processing=code_processing), metadata={})
    code_revision = _Revision(code_raw)
    same_codes_match = validate_outcome_revision(code_revision, code_raw, code_processing, code_outcome).matches
    changed_codes = code_processed.copy(deep=True)
    changed_codes.loc[changed_codes["observation_id"].eq("a-1"), "gaze_valid"] = "ok"
    changed_processing = SimpleNamespace(signals=changed_codes)
    changed_outcome = SimpleNamespace(payload=SimpleNamespace(processing=changed_processing), metadata={})
    changed_codes_rejected = not validate_outcome_revision(
        code_revision, code_raw, changed_processing, changed_outcome,
    ).matches
    mapped_validity = pd.DataFrame({
        "gaze_valid": pd.Series([True, False, pd.NA], dtype="boolean"),
        "source_gaze_valid": ["ok", "lost", "unmapped"],
    })
    mapped_states, mapped_column = status_values(mapped_validity, ("gaze_valid", "source_gaze_valid"))
    source_code_states, _ = status_values(mapped_validity, ("source_gaze_valid",))
    mapped_validity_is_conservative = bool(
        mapped_column == "gaze_valid"
        and np.array_equal(mapped_states[:2], np.array([1.0, 0.0]))
        and np.isnan(mapped_states[2])
        and np.isnan(source_code_states[:2]).all()
        and np.isnan(source_code_states[2])
    )
    raw_numeric_validity = pd.DataFrame({"source_gaze_valid": pd.Series([0, 1, True, False], dtype=object)})
    raw_numeric_states, _ = status_values(raw_numeric_validity, ("source_gaze_valid",))
    raw_numeric_codes_are_conservative = bool(
        np.isnan(raw_numeric_states[:2]).all()
        and np.array_equal(raw_numeric_states[2:], np.array([1.0, 0.0]))
    )

    checks = {
        "raw_processed_identity_pairing_after_reordering": bool(order_paired),
        "same_time_sources_remain_isolated": bool(stream_isolated),
        "interval_selection_uses_seconds": range_selection,
        "event_click_emits_scope_and_interval": clicked_event,
        "event_click_selects_reason_row_and_interval": bool(
            event_range_selected and event_row_reselected and event_reason_preserved
        ),
        "invalid_unknown_and_interpolated_states_preserved": validity_preserved,
        "revision_change_clears_outcome_and_selection": revision_clears,
        "stale_revision_processing_rejected": stale_rejected,
        "missing_processing_explained_and_raw_unchanged": bool(missing_explained and object_unchanged),
        "unknown_scope_cannot_navigate": unknown_location_rejected,
        "unverified_clock_cannot_navigate": foreign_clock_location.stream_key is None,
        "missing_clock_selector_cannot_navigate": empty_clock_location.stream_key is None,
        "categorical_validity_codes_not_coerced_to_bool": bool(same_codes_match and changed_codes_rejected),
        "canonical_validity_preferred_unknown_codes_preserved": mapped_validity_is_conservative,
        "raw_numeric_validity_codes_remain_unknown": raw_numeric_codes_are_conservative,
        "real_interpolation_preserves_revision_match": bool(real_revision_match.matches and actual_value_changed),
        "same_identity_source_edit_invalidates_result": source_value_revision_rejected,
        "viewing_does_not_change_processing_configuration": configuration_unchanged,
    }
    page.close()
    app.processEvents()
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "details": {
            "event_payloads": event_hits,
            "selected_interval_after_event_click": interval_after_event_click,
            "selected_diagnostic_rows": selected_diagnostic_rows,
            "locator_label_after_event_click": locator_after_event_click,
            "foreign_clock_message": foreign_clock_location.message,
            "real_processing_revision_match": {
                "matches": bool(real_revision_match.matches), "reason": str(real_revision_match.reason),
            },
            "real_interpolated_row": interpolated_row.to_dict(),
            "edited_source_revision_rejection": {
                "matches": bool(edited_revision_match.matches), "reason": str(edited_revision_match.reason),
            },
        },
    }
