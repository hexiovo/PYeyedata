"""Gap-aware I-VT/I-DT fixation detection and a bounded local pulse rule."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Callable, Literal, Mapping

import numpy as np
import pandas as pd

from .contracts import EVENT_COLUMNS


class EventDetectionError(ValueError):
    """An event configuration or external event table is ambiguous or unsafe."""


class _DetectionCancelled(Exception):
    """Internal unwind after a cooperative cancellation callback fires."""


EVENT_SCHEMA_VERSION = "1.1.0"
EVENT_METHOD_VALIDATION_REF = "method-registry:pyeyedata.events/1.1.0#a07-analytic-fixtures"
ANGULAR_ROUNDOFF_TOL_DEG = 1e-9
EVENT_EXTRA_COLUMNS = (
    "source_id", "source_event_id", "interaction_id", "trial", "stimulus_id",
    "analysis_stream_id", "physical_partition_id", "timebase_id", "clock_id", "event_evidence", "classification_method",
    "method_validation_ref", "source_validity_status", "source_refs", "contributing_observation_ids",
    "support_ref", "original_start_s", "original_end_s", "amplitude_deg", "path_length_deg",
    "path_efficiency", "bracketing_fixation_event_ids", "device_validation_status", "method_limitations",
)
EVENT_RESULT_COLUMNS = tuple(dict.fromkeys((*EVENT_COLUMNS, *EVENT_EXTRA_COLUMNS)))
EVENT_INTERVAL_AUDIT_COLUMNS = (
    "source_id", "subject_id", "session_id", "interaction_id", "trial", "analysis_stream_id",
    "physical_partition_id", "timebase_id", "clock_id", "start_observation_id", "end_observation_id", "start_s", "end_s",
    "dt_s", "angle_deg", "velocity_deg_s", "decision", "reason_code", "method",
)


@dataclass(frozen=True)
class EventDetectionConfig:
    fixation_method: Literal["ivt", "idt", "external_only"] = "ivt"
    velocity_threshold_deg_s: float = 30.0
    min_duration_s: float = 0.060
    min_raw_points: int = 3
    idt_min_duration_s: float = 0.100
    idt_min_raw_points: int = 3
    idt_dispersion_threshold: float | None = None
    idt_threshold_unit: str | None = None
    adjacency_max_gap_s: float | None = None
    saccade_method: Literal["bounded_velocity_pulse_v1", "external_only", "disabled"] = "bounded_velocity_pulse_v1"
    high_velocity_deg_s: float = 60.0
    saccade_min_duration_s: float = 0.010
    saccade_max_duration_s: float = 0.100
    saccade_min_raw_points: int = 3
    saccade_min_amplitude_deg: float = 0.5
    saccade_min_path_efficiency: float = 0.8
    required_pre_post_fixation_duration_s: float = 0.060
    side_by_side_external: bool = False

    def __post_init__(self) -> None:
        if self.fixation_method not in {"ivt", "idt", "external_only"}:
            raise EventDetectionError("fixation_method must be ivt, idt, or external_only")
        if self.saccade_method not in {"bounded_velocity_pulse_v1", "external_only", "disabled"}:
            raise EventDetectionError("Unsupported saccade_method")
        for name in (
            "velocity_threshold_deg_s", "min_duration_s", "idt_min_duration_s", "high_velocity_deg_s",
            "saccade_min_duration_s", "saccade_max_duration_s", "saccade_min_amplitude_deg",
            "required_pre_post_fixation_duration_s",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
                raise EventDetectionError(f"{name} must be finite and positive")
        if self.saccade_max_duration_s < self.saccade_min_duration_s:
            raise EventDetectionError("saccade_max_duration_s must be >= saccade_min_duration_s")
        for name in ("min_raw_points", "idt_min_raw_points", "saccade_min_raw_points"):
            value = getattr(self, name)
            minimum = 3
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise EventDetectionError(f"{name} must be an integer >= {minimum}")
        if self.idt_dispersion_threshold is not None and (
            isinstance(self.idt_dispersion_threshold, bool)
            or not isinstance(self.idt_dispersion_threshold, (int, float))
            or not math.isfinite(float(self.idt_dispersion_threshold)) or self.idt_dispersion_threshold <= 0
        ):
            raise EventDetectionError("idt_dispersion_threshold must be finite and positive")
        if self.idt_threshold_unit is not None and (not isinstance(self.idt_threshold_unit, str) or not self.idt_threshold_unit.strip()):
            raise EventDetectionError("idt_threshold_unit must be a nonempty string when supplied")
        if self.adjacency_max_gap_s is not None and (
            isinstance(self.adjacency_max_gap_s, bool) or not math.isfinite(float(self.adjacency_max_gap_s)) or self.adjacency_max_gap_s <= 0
        ):
            raise EventDetectionError("adjacency_max_gap_s must be finite and positive when supplied")
        if not 0 < self.saccade_min_path_efficiency <= 1:
            raise EventDetectionError("saccade_min_path_efficiency must be within (0, 1]")
        if self.fixation_method == "ivt" and self.high_velocity_deg_s <= self.velocity_threshold_deg_s:
            raise EventDetectionError("high_velocity_deg_s must exceed the I-VT fixation threshold")
        if not isinstance(self.side_by_side_external, bool):
            raise EventDetectionError("side_by_side_external must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventDetectionResult:
    events: pd.DataFrame
    interval_audit: pd.DataFrame
    execution_status: Literal["complete", "partial", "cancelled"] = "complete"
    logs: list[str] | None = None


def detect_events(
    analysis_streams: pd.DataFrame,
    config: EventDetectionConfig | None = None,
    *,
    coordinate_metadata: Mapping[str, Any] | None = None,
    external_events: pd.DataFrame | None = None,
    max_gap_s: float | None = None,
    run_id: str = "run:local",
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> EventDetectionResult:
    """Detect within each one-eye/physical run; labels and pupil validity are never barriers."""
    settings = config or EventDetectionConfig()
    raw_geometry: Any = coordinate_metadata or {}
    if hasattr(raw_geometry, "to_dict"):
        raw_geometry = raw_geometry.to_dict()
    geometry = dict(raw_geometry)
    if max_gap_s is None:
        max_gap_s = settings.adjacency_max_gap_s
    elif settings.adjacency_max_gap_s is not None and not math.isclose(float(max_gap_s), float(settings.adjacency_max_gap_s), rel_tol=1e-9, abs_tol=1e-12):
        raise EventDetectionError("adjacency_max_gap_s conflicts with the processing stream gap limit")
    if settings.adjacency_max_gap_s is not None:
        max_gap_s = settings.adjacency_max_gap_s
    if external_events is not None and settings.fixation_method != "external_only" and not settings.side_by_side_external:
        raise EventDetectionError("Local and external event tables require explicit side_by_side_external=True")
    events: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    logs: list[str] = []
    if settings.fixation_method == "external_only":
        if external_events is not None:
            events.extend(_adapt_external_events(external_events, analysis_streams, run_id))
        return EventDetectionResult(pd.DataFrame.from_records(events, columns=EVENT_RESULT_COLUMNS), pd.DataFrame(columns=EVENT_INTERVAL_AUDIT_COLUMNS), logs=logs)
    if analysis_streams.empty:
        return EventDetectionResult(pd.DataFrame(columns=EVENT_RESULT_COLUMNS), pd.DataFrame(columns=EVENT_INTERVAL_AUDIT_COLUMNS), logs=["no_analysis_streams"])
    groups = _stream_groups(analysis_streams)
    try:
        for group_number, (key, group) in enumerate(groups):
            if cancel and cancel():
                raise _DetectionCancelled
            group = group.sort_values(["time_s", "original_index"], kind="stable", na_position="last")
            meta = _group_metadata(group, key)
            meta["config"] = settings.to_dict()
            has_physical_partition = (
                "physical_partition_id" in group and group["physical_partition_id"].notna().any()
            )
            if max_gap_s is None and not has_physical_partition:
                for row in group.to_dict("records"):
                    audit.append(_interval_record(row, row, None, None, None, "blocked",
                                                  "reliable_stream_gap_limit_required", "preflight"))
                continue
            runs, run_audit = _eligible_runs(
                group, geometry, max_gap_s, allow_raw_idt=(settings.fixation_method == "idt"),
                raw_threshold=settings.idt_dispersion_threshold, raw_threshold_unit=settings.idt_threshold_unit,
                cancel=cancel,
            )
            audit.extend(run_audit)
            fixation_events: list[dict[str, Any]] = []
            if settings.fixation_method == "ivt":
                stream_events, stream_audit = _detect_ivt(runs, meta, settings, run_id, max_gap_s, cancel=cancel)
                fixation_events.extend(stream_events)
                audit.extend(stream_audit)
            elif settings.fixation_method == "idt":
                stream_events, stream_audit = _detect_idt(group, runs, meta, settings, run_id, cancel=cancel)
                fixation_events.extend(stream_events)
                audit.extend(stream_audit)
            events.extend(fixation_events)
            if settings.saccade_method == "bounded_velocity_pulse_v1":
                pulse_events, pulse_audit = _detect_pulses(runs, fixation_events, meta, settings, run_id, cancel=cancel)
                events.extend(pulse_events)
                audit.extend(pulse_audit)
            if progress:
                progress((group_number + 1) / max(1, len(groups)), "检测 fixation 与限定 pulse candidates")
    except _DetectionCancelled:
        return EventDetectionResult(pd.DataFrame.from_records(events, columns=EVENT_RESULT_COLUMNS),
                                    pd.DataFrame.from_records(audit, columns=EVENT_INTERVAL_AUDIT_COLUMNS),
                                    "cancelled", logs + ["cancelled during event stream, IDT extension, or pulse scan"])
    if external_events is not None:
        events.extend(_adapt_external_events(external_events, analysis_streams, run_id))
    return EventDetectionResult(
        pd.DataFrame.from_records(events, columns=EVENT_RESULT_COLUMNS),
        pd.DataFrame.from_records(audit, columns=EVENT_INTERVAL_AUDIT_COLUMNS),
        "complete", logs,
    )


def _eligible_runs(
    group: pd.DataFrame, geometry: Mapping[str, Any], max_gap_s: float | None, *,
    allow_raw_idt: bool = False, raw_threshold: float | None = None, raw_threshold_unit: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = group.to_dict("records")
    runs: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row_number, row in enumerate(rows):
        if cancel and row_number % 256 == 0 and cancel():
            raise _DetectionCancelled
        t = _finite(row.get("time_s"))
        validity = row.get("source_validity_status")
        valid = ((row.get("analysis_gaze_valid") is True or isinstance(row.get("analysis_gaze_valid"), np.bool_) and bool(row.get("analysis_gaze_valid")))
                 and validity == "valid")
        vector, xy, reason = _geometry_values(row, geometry)
        if (reason == "angular_geometry_unavailable" and allow_raw_idt and raw_threshold is not None
                and raw_threshold_unit is not None and str(row.get("coordinate_unit", "unknown")).casefold() == raw_threshold_unit.casefold()
                and str(row.get("coordinate_frame", "unknown")).casefold() != "unknown"
                and _finite(row.get("gaze_z")) is None
                and _finite(row.get("gaze_x")) is not None and _finite(row.get("gaze_y")) is not None):
            xy = np.asarray([_finite(row.get("gaze_x")), _finite(row.get("gaze_y"))], dtype=float)
            reason = None
        elif reason == "angular_geometry_unavailable" and allow_raw_idt:
            if raw_threshold is None:
                reason = "idt_explicit_threshold_required_for_nonangular_unit"
            elif raw_threshold_unit is None:
                reason = "idt_explicit_threshold_unit_required"
            elif str(row.get("coordinate_unit", "unknown")).casefold() != raw_threshold_unit.casefold():
                reason = "idt_threshold_unit_mismatch"
        has_time = t is not None and _is_true(row.get("time_valid", True))
        partition_id = row.get("physical_partition_id")
        if max_gap_s is None and not _nonmissing(partition_id) and reason is None:
            reason = "reliable_stream_gap_limit_required"
        eligible = bool(valid and has_time and reason is None)
        boundary = _is_true(row.get("event_barrier_before", False))
        if previous is not None:
            dt = t - _finite(previous.get("time_s")) if t is not None and _finite(previous.get("time_s")) is not None else None
            scope_changed = any(_scope_changed(row.get(name), previous.get(name))
                                for name in ("physical_partition_id", "timebase_id", "clock_id"))
            if boundary or scope_changed:
                _flush_run(active, runs)
            elif dt is None or dt <= 0 or (max_gap_s is not None and dt > max_gap_s + 1e-12):
                _flush_run(active, runs)
            elif not eligible:
                _flush_run(active, runs)
            elif not _row_eligible(previous, geometry, allow_raw_idt=allow_raw_idt, raw_threshold=raw_threshold, raw_threshold_unit=raw_threshold_unit):
                _flush_run(active, runs)
        if eligible:
            item = dict(row)
            item["_t"] = t
            item["_vector"] = vector
            item["_xy"] = xy
            item["_eligibility_reason"] = None
            active.append(item)
        else:
            audit.append(_interval_record(row, row, None, None, None, "ineligible_observation", reason or _invalid_reason(validity, has_time), "geometry"))
            _flush_run(active, runs)
        previous = row
    _flush_run(active, runs)
    return runs, audit


def _row_eligible(row: Mapping[str, Any], geometry: Mapping[str, Any], *, allow_raw_idt: bool = False,
                  raw_threshold: float | None = None, raw_threshold_unit: str | None = None) -> bool:
    valid = ((row.get("analysis_gaze_valid") is True or isinstance(row.get("analysis_gaze_valid"), np.bool_) and bool(row.get("analysis_gaze_valid")))
             and row.get("source_validity_status") == "valid")
    reason = _geometry_values(row, geometry)[2]
    raw_supported = (reason == "angular_geometry_unavailable" and allow_raw_idt and raw_threshold is not None
                     and raw_threshold_unit is not None
                     and str(row.get("coordinate_unit", "unknown")).casefold() == raw_threshold_unit.casefold()
                     and str(row.get("coordinate_frame", "unknown")).casefold() != "unknown"
                     and _finite(row.get("gaze_z")) is None
                     and _finite(row.get("gaze_x")) is not None and _finite(row.get("gaze_y")) is not None)
    return bool(valid and _finite(row.get("time_s")) is not None and _is_true(row.get("time_valid", True))
                and (reason is None or raw_supported))


def _flush_run(active: list[dict[str, Any]], runs: list[dict[str, Any]]) -> None:
    if active:
        runs.append({"rows": active.copy(), "partition_id": active[0].get("physical_partition_id")})
        active.clear()


def _geometry_values(row: Mapping[str, Any], coordinate_metadata: Mapping[str, Any]) -> tuple[np.ndarray | None, np.ndarray | None, str | None]:
    rep = str(row.get("representation", "unknown"))
    unit = str(row.get("coordinate_unit", "unknown")).casefold()
    frame = str(row.get("coordinate_frame", "unknown"))
    if frame.casefold() == "unknown":
        return None, None, "coordinate_frame_unverified"
    if rep == "direction":
        values = np.array([_finite(row.get(f"gaze_direction_{axis}")) for axis in "xyz"], dtype=float)
        if not np.isfinite(values).all() or np.linalg.norm(values) <= 0:
            return None, None, "direction_geometry_unavailable"
        norm = float(np.linalg.norm(values))
        return values / norm, None, None
    if rep != "point":
        return None, None, "unsupported_coordinate_representation"
    x, y, z = (_finite(row.get(f"gaze_{axis}")) for axis in "xyz")
    if x is None or y is None:
        return None, None, "point_geometry_unavailable"
    if unit in {"deg", "degree", "degrees", "°"}:
        if z is not None:
            return None, None, "unsupported_3d_angular_point_without_transform"
        az, el = np.radians([x, y])
        vector = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)], dtype=float)
        return vector / np.linalg.norm(vector), np.array([x, y], dtype=float), None
    geometry = coordinate_metadata.get("geometry", {})
    if z is not None:
        return None, None, "unsupported_3d_point_without_transform"
    if not isinstance(geometry, Mapping) or geometry.get("schema") != "fixed-screen-v1":
        return None, None, "angular_geometry_unavailable"
    if str(geometry.get("coordinate_frame", "unknown")) != frame or str(geometry.get("coordinate_unit", "unknown")).casefold() != unit:
        return None, None, "fixed_screen_geometry_mismatch"
    try:
        screen_origin = np.asarray(geometry["screen_origin"], dtype=float)
        axis_x = np.asarray(geometry["horizontal_axis"], dtype=float)
        axis_y = np.asarray(geometry["vertical_axis"], dtype=float)
        eye_origin = np.asarray(geometry["eye_origin"], dtype=float)
        xy = np.array([x, y], dtype=float)
        if screen_origin.shape != (3,) or axis_x.shape != (3,) or axis_y.shape != (3,) or eye_origin.shape != (3,):
            raise ValueError
        if not all(np.isfinite(arr).all() for arr in (screen_origin, axis_x, axis_y, eye_origin)):
            raise ValueError
        nx, ny = float(np.linalg.norm(axis_x)), float(np.linalg.norm(axis_y))
        if nx <= 0 or ny <= 0 or abs(float(np.dot(axis_x / nx, axis_y / ny))) > 1e-6:
            raise ValueError
        point = screen_origin + x * (axis_x / nx) + y * (axis_y / ny)
        ray = point - eye_origin
        norm = float(np.linalg.norm(ray))
        if norm <= 0:
            raise ValueError
        return ray / norm, xy, None
    except (KeyError, TypeError, ValueError):
        return None, None, "fixed_screen_geometry_invalid"


def _detect_ivt(runs: list[dict[str, Any]], meta: Mapping[str, Any], settings: EventDetectionConfig, run_id: str,
                max_gap_s: float | None, *, cancel: Callable[[], bool] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for run in runs:
        if cancel and cancel():
            raise _DetectionCancelled
        rows = run["rows"]
        if len(rows) < 2:
            continue
        velocities: list[float] = []
        for i in range(len(rows) - 1):
            if cancel and i % 256 == 0 and cancel():
                raise _DetectionCancelled
            dt = rows[i + 1]["_t"] - rows[i]["_t"]
            angle = _angle_deg(rows[i]["_vector"], rows[i + 1]["_vector"])
            speed = angle / dt if dt > 0 else math.inf
            velocities.append(speed)
            within = speed <= settings.velocity_threshold_deg_s + ANGULAR_ROUNDOFF_TOL_DEG
            audit.append(_interval_record(rows[i], rows[i + 1], dt, angle, speed,
                                          "fixation_interval" if within else "above_fixation_threshold",
                                          "velocity_within_threshold" if within else "velocity_above_threshold", "ivt"))
        interval = 0
        while interval < len(velocities):
            if cancel and interval % 256 == 0 and cancel():
                raise _DetectionCancelled
            is_fix = velocities[interval] <= settings.velocity_threshold_deg_s + ANGULAR_ROUNDOFF_TOL_DEG
            end = interval + 1
            while end < len(velocities) and (velocities[end] <= settings.velocity_threshold_deg_s + ANGULAR_ROUNDOFF_TOL_DEG) == is_fix:
                end += 1
            selected = rows[interval:end + 1]
            duration = selected[-1]["_t"] - selected[0]["_t"]
            if is_fix:
                reason = None
                if len(selected) < settings.min_raw_points: reason = "insufficient_raw_points"
                elif duration < settings.min_duration_s - 1e-12: reason = "insufficient_duration"
                events.append(_event_record(selected, meta, run_id, "fixation" if reason is None else "fixation_candidate",
                                            "complete" if reason is None else "candidate", "ivt",
                                            reason or "ivt_threshold_met", EVENT_METHOD_VALIDATION_REF if reason is None else None))
            else:
                events.append(_event_record(selected, meta, run_id, "unclassified_interval", "candidate", "ivt",
                                            "velocity_above_fixation_threshold", None))
            interval = end
    return events, audit


def _detect_idt(group: pd.DataFrame, runs: list[dict[str, Any]], meta: Mapping[str, Any], settings: EventDetectionConfig, run_id: str,
                *, cancel: Callable[[], bool] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for run in runs:
        rows = run["rows"]
        if not rows:
            continue
        xy_values = [item.get("_xy") for item in rows]
        if any(value is None or len(value) != 2 for value in xy_values):
            events.append(_event_record(rows[:1], meta, run_id, "fixation_candidate", "candidate", "idt",
                                        "idt_requires_supported_2d_coordinates", None))
            continue
        unit = str(rows[0].get("coordinate_unit", "unknown"))
        frame = str(rows[0].get("coordinate_frame", "unknown"))
        angular = unit.casefold() in {"deg", "degree", "degrees", "°"}
        if settings.idt_dispersion_threshold is None:
            if not angular:
                events.append(_event_record(rows[:1], meta, run_id, "fixation_candidate", "candidate", "idt",
                                            "idt_explicit_threshold_required_for_nonangular_unit", None))
                continue
            threshold, threshold_unit = 1.0, "deg"
        else:
            threshold = float(settings.idt_dispersion_threshold)
            threshold_unit = settings.idt_threshold_unit
            if threshold_unit is None or threshold_unit.casefold() != unit.casefold():
                events.append(_event_record(rows[:1], meta, run_id, "fixation_candidate", "candidate", "idt",
                                            "idt_threshold_unit_mismatch", None))
                continue
        if frame.casefold() == "unknown" or unit.casefold() == "unknown":
            events.append(_event_record(rows[:1], meta, run_id, "fixation_candidate", "candidate", "idt",
                                        "idt_coordinate_frame_or_unit_unverified", None))
            continue
        xy = np.asarray(xy_values, dtype=float)
        n = len(rows)
        extrema = _RangeExtrema2D(xy, cancel=cancel)
        times = np.asarray([row["_t"] for row in rows], dtype=float)
        i = 0
        while i < n:
            if cancel and cancel():
                raise _DetectionCancelled
            j = max(i + settings.idt_min_raw_points - 1,
                    int(np.searchsorted(times, times[i] + settings.idt_min_duration_s - 1e-12, side="left")))
            if j >= n:
                tail = rows[i:]
                reason = "idt_tail_below_min_duration_or_raw_points"
                if tail:
                    events.append(_event_record(tail, meta, run_id, "fixation_candidate", "candidate", "idt", reason, None))
                audit.append(_interval_record(rows[i], rows[-1], rows[-1]["_t"] - rows[i]["_t"], None, None, "candidate", reason, "idt"))
                break
            dispersion = extrema.dispersion(i, j)
            if dispersion <= threshold + ANGULAR_ROUNDOFF_TOL_DEG:
                low, high = j, n - 1
                while low < high:
                    if cancel and cancel():
                        raise _DetectionCancelled
                    mid = (low + high + 1) // 2
                    if extrema.dispersion(i, mid) <= threshold + ANGULAR_ROUNDOFF_TOL_DEG:
                        low = mid
                    else:
                        high = mid - 1
                accepted = low
                if accepted + 1 < n:
                    audit.append(_interval_record(rows[i], rows[accepted + 1], rows[accepted + 1]["_t"] - rows[i]["_t"], None, None,
                                                  "extension_rejected", "dispersion_above_threshold", "idt"))
                selected = rows[i:accepted + 1]
                duration = selected[-1]["_t"] - selected[0]["_t"]
                event_reason = "idt_dispersion_threshold_met"
                events.append(_event_record(selected, meta, run_id, "fixation", "complete", "idt", event_reason,
                                            EVENT_METHOD_VALIDATION_REF))
                audit.append(_interval_record(selected[0], selected[-1], duration, extrema.dispersion(i, accepted), None, "fixation",
                                              f"dispersion<={threshold:g}{threshold_unit};maximal_extension", "idt"))
                i = accepted + 1
            else:
                audit.append(_interval_record(rows[i], rows[j], rows[j]["_t"] - rows[i]["_t"], dispersion, None,
                                              "candidate_rejected", "dispersion_above_threshold", "idt"))
                events.append(_event_record(rows[i:j + 1], meta, run_id, "fixation_candidate", "candidate", "idt",
                                            "dispersion_above_threshold", None))
                i += 1
    return events, audit


def _detect_pulses(runs: list[dict[str, Any]], fixation_events: list[dict[str, Any]], meta: Mapping[str, Any], settings: EventDetectionConfig, run_id: str,
                   *, cancel: Callable[[], bool] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    fixations = [e for e in fixation_events if e.get("event_type") == "fixation" and e.get("status") == "complete"]
    for run in runs:
        if cancel and cancel():
            raise _DetectionCancelled
        rows = run["rows"]
        if len(rows) < 2:
            continue
        if any(row.get("_vector") is None for row in rows):
            continue
        speeds = [
            _angle_deg(rows[i]["_vector"], rows[i + 1]["_vector"]) / (rows[i + 1]["_t"] - rows[i]["_t"])
            for i in range(len(rows) - 1)
        ]
        i = 0
        while i < len(speeds):
            if cancel and cancel():
                raise _DetectionCancelled
            if speeds[i] < settings.high_velocity_deg_s - ANGULAR_ROUNDOFF_TOL_DEG:
                i += 1
                continue
            j = i + 1
            while j < len(speeds) and speeds[j] >= settings.high_velocity_deg_s - ANGULAR_ROUNDOFF_TOL_DEG:
                j += 1
            selected = rows[i:j + 1]
            duration = selected[-1]["_t"] - selected[0]["_t"]
            amplitude = _angle_deg(selected[0]["_vector"], selected[-1]["_vector"])
            path = sum(_angle_deg(selected[k]["_vector"], selected[k + 1]["_vector"]) for k in range(len(selected) - 1))
            efficiency = amplitude / path if path > 0 and math.isfinite(path) else math.nan
            if math.isfinite(efficiency) and 1.0 < efficiency <= 1.0 + ANGULAR_ROUNDOFF_TOL_DEG:
                efficiency = 1.0
            pre: list[dict[str, Any]] = []
            post: list[dict[str, Any]] = []
            reason = "bounded_pulse_criteria_met"
            if len(selected) < settings.saccade_min_raw_points: reason = "insufficient_raw_points"
            elif duration < settings.saccade_min_duration_s - 1e-12: reason = "below_min_duration"
            elif duration > settings.saccade_max_duration_s + 1e-12: reason = "above_max_duration"
            elif amplitude < settings.saccade_min_amplitude_deg - ANGULAR_ROUNDOFF_TOL_DEG: reason = "below_min_amplitude"
            elif not math.isfinite(efficiency) or efficiency < settings.saccade_min_path_efficiency - 1e-12: reason = "path_efficiency_below_threshold"
            elif _overlaps_trusted_tracking(selected): reason = "overlaps_trusted_pursuit_or_target_tracking"
            else:
                pre = [e for e in fixations if _same_partition_event(e, selected[0]) and math.isclose(float(e["end_s"]), selected[0]["_t"], abs_tol=1e-12)
                       and float(e["duration_s"]) >= settings.required_pre_post_fixation_duration_s - 1e-12]
                post = [e for e in fixations if _same_partition_event(e, selected[-1]) and math.isclose(float(e["start_s"]), selected[-1]["_t"], abs_tol=1e-12)
                        and float(e["duration_s"]) >= settings.required_pre_post_fixation_duration_s - 1e-12]
                if not pre or not post: reason = "missing_required_fixation_bracket"
            accepted = reason == "bounded_pulse_criteria_met"
            event = _event_record(selected, meta, run_id, "saccade" if accepted else "saccade_candidate",
                                  "complete" if accepted else "candidate", "bounded_velocity_pulse_v1", reason,
                                  EVENT_METHOD_VALIDATION_REF if accepted else None)
            event["amplitude_deg"] = amplitude
            event["path_length_deg"] = path
            event["path_efficiency"] = efficiency if math.isfinite(efficiency) else None
            event["bracketing_fixation_event_ids"] = tuple(dict.fromkeys(str(e["event_id"]) for e in (*pre, *post)))
            events.append(event)
            audit.append(_interval_record(selected[0], selected[-1], duration, amplitude, None,
                                          "saccade" if accepted else "saccade_candidate", reason, "bounded_velocity_pulse_v1"))
            i = j
    return events, audit


def _adapt_external_events(external: pd.DataFrame, analysis_streams: pd.DataFrame, run_id: str) -> list[dict[str, Any]]:
    required = {"event_id", "event_type", "start_s", "end_s"}
    missing = required - set(external.columns)
    if missing:
        raise EventDetectionError("External events missing: " + ", ".join(sorted(missing)))
    if external["event_id"].isna().any() or external["event_id"].astype("string").str.strip().eq("").any():
        raise EventDetectionError("External event_id must be explicit and nonempty")
    if external["event_id"].astype(str).duplicated().any():
        raise EventDetectionError("Duplicate external event_id values are ambiguous")
    rows: list[dict[str, Any]] = []
    for record in external.to_dict("records"):
        start, end = _finite(record.get("start_s")), _finite(record.get("end_s"))
        if start is None or end is None or end <= start:
            raise EventDetectionError(f"External event {record.get('event_id')!r} requires finite start_s < end_s")
        if not isinstance(record.get("event_type"), str) or not record["event_type"].strip():
            raise EventDetectionError(f"External event {record.get('event_id')!r} requires a nonempty event_type")
        declared_time_unit = record.get("time_unit")
        if _nonmissing(declared_time_unit) and str(declared_time_unit).casefold() not in {"s", "sec", "second", "seconds"}:
            raise EventDetectionError(f"External event {record.get('event_id')!r} uses non-second start_s/end_s")
        declared_refs = _refs(record.get("source_refs"))
        source_kind = str(record.get("event_evidence", "unknown"))
        version = _external_text_field(record, "algorithm_version") or _external_text_field(record, "source_version")
        method = _external_text_field(record, "classification_method") or _external_text_field(record, "algorithm")
        vendor_ok = source_kind == "vendor_observed" and bool(declared_refs) and bool(version) and bool(method)
        source_confirmed_ok = source_kind == "source_confirmed" and bool(declared_refs)
        evidence = ("vendor_observed" if vendor_ok else
                    "source_confirmed" if source_confirmed_ok else
                    "unknown" if source_kind in {"vendor_observed", "source_confirmed"} else
                    source_kind if source_kind == "unknown" else "unknown")
        reason = record.get("reason_code")
        if source_kind in {"vendor_observed", "source_confirmed"} and evidence == "unknown":
            reason = reason or "external_provenance_incomplete"
        scope_reason = _external_scope_reason(record, analysis_streams, start, end)
        if scope_reason:
            evidence = "unknown"
            reason = scope_reason
        event_id = str(record["event_id"])
        if event_id.startswith("event:"):
            event_id = "external:" + event_id
        item = _empty_event_row(record, event_id, str(record["event_type"]), start, end, run_id)
        status = record.get("status", "unknown")
        if not _nonmissing(status):
            status = "unknown"
        if not isinstance(status, str) or not status.strip():
            raise EventDetectionError(f"External event {record.get('event_id')!r} status must be a nonempty string")
        item.update({
            "status": status, "reason_code": reason or "legacy_external_event",
            "n_observed": _external_nonnegative_int(record, "n_observed"),
            "n_interpolated": _external_nonnegative_int(record, "n_interpolated"),
            "algorithm_version": str(version) if version else None, "parameter_ref": record.get("parameter_ref"),
            "source_event_id": str(record["event_id"]), "event_evidence": evidence,
            "classification_method": str(method) if method else None, "source_validity_status": str(record.get("source_validity_status", "unknown")),
            "source_refs": declared_refs, "contributing_observation_ids": _refs(record.get("contributing_observation_ids")),
            "method_validation_ref": None,
            "device_validation_status": "NOT_RUN",
            "method_limitations": ("external_provenance_is_unverified_by_this_adapter",),
        })
        rows.append(item)
    return rows


def _external_scope_reason(record: Mapping[str, Any], analysis_streams: pd.DataFrame, start: float, end: float) -> str | None:
    if not _nonmissing(record.get("subject_id")) or not _nonmissing(record.get("session_id")):
        return "legacy_external_scope_incomplete"
    if "time_s" not in analysis_streams:
        return "external_event_scope_field_unavailable"
    required_clock_fields = ("source_id", "analysis_stream_id", "timebase_id", "clock_id", "time_unit")
    if any(not _nonmissing(record.get(key)) for key in required_clock_fields):
        missing_clock_reason = "legacy_external_clock_scope_limited"
    else:
        missing_clock_reason = None
    subset = analysis_streams
    keys = ("source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id",
            "analysis_stream_id", "physical_partition_id", "timebase_id", "clock_id")
    for key in keys:
        value = record.get(key)
        if _nonmissing(value) and key not in subset:
            return "external_event_scope_field_unavailable"
        if _nonmissing(value):
            subset = subset.loc[subset[key].astype("string").eq(str(value)).fillna(False)]
        elif key in {"source_id", "analysis_stream_id", "timebase_id", "clock_id"} and not _nonmissing(value):
            missing_clock_reason = missing_clock_reason or "legacy_external_clock_scope_limited"
    if subset.empty:
        return "external_event_scope_not_matched_to_analysis_stream"
    times = pd.to_numeric(subset.get("time_s"), errors="coerce")
    lo, hi = times.min(), times.max()
    if pd.isna(lo) or pd.isna(hi) or start < float(lo) - 1e-12 or end > float(hi) + 1e-12:
        return "external_event_outside_stream_time_support"
    supported = subset.loc[times.between(start, end, inclusive="both")]
    if supported.empty:
        before = times.loc[times <= start]
        after = times.loc[times >= end]
        neighbor_indices = []
        if not before.empty: neighbor_indices.append(before.idxmax())
        if not after.empty: neighbor_indices.append(after.idxmin())
        supported = subset.loc[list(dict.fromkeys(neighbor_indices))]
    streams = supported.get("analysis_stream_id", pd.Series(dtype="string")).dropna().astype(str).unique()
    if "analysis_stream_id" not in record or not _nonmissing(record.get("analysis_stream_id")):
        if len(streams) > 1:
            return "external_event_stream_scope_ambiguous"
    partitions = supported.get("physical_partition_id", pd.Series(dtype="string")).dropna().astype(str).unique()
    if len(partitions) > 1:
        return "external_event_crosses_physical_partition"
    return missing_clock_reason


def _event_record(rows: list[dict[str, Any]], meta: Mapping[str, Any], run_id: str, event_type: str, status: str,
                  method: str, reason: str, method_ref: str | None) -> dict[str, Any]:
    first, last = rows[0], rows[-1]
    start, end = float(first["_t"]), float(last["_t"])
    ids = tuple(str(row.get("observation_id")) for row in rows)
    source_ids = tuple(dict.fromkeys(str(row.get("source_id")) for row in rows if _nonmissing(row.get("source_id"))))
    parameter_ref = _parameter_ref(meta.get("config"))
    event_id = _stable_event_id(str(meta.get("analysis_stream_id", "unknown")), event_type, method, ids, parameter_ref)
    item = _empty_event_row(first, event_id, event_type, start, end, run_id)
    item.update({
        "status": status, "reason_code": reason, "n_observed": len(set(ids)), "n_interpolated": 0,
        "algorithm_version": EVENT_SCHEMA_VERSION, "parameter_ref": parameter_ref,
        "source_id": source_ids[0] if len(source_ids) == 1 else None,
        "interaction_id": first.get("interaction_id"), "trial": first.get("trial"), "stimulus_id": first.get("stimulus_id"),
        "analysis_stream_id": first.get("analysis_stream_id"), "physical_partition_id": first.get("physical_partition_id"),
        "event_evidence": "local_method_derived" if status == "complete" else "candidate",
        "classification_method": method, "method_validation_ref": method_ref,
        "source_validity_status": "valid", "source_refs": tuple(source_ids),
        "contributing_observation_ids": ids,
        "support_ref": "observation-support:" + _stable_hash(ids),
        "original_start_s": start, "original_end_s": end,
        "device_validation_status": "NOT_RUN",
        "method_limitations": (
            "numeric_method_validation_does_not_verify_device_or_hardware",
            "local_event_classification_is_not_physiological_ground_truth",
            *(('short_ballistic_pursuit_or_valid_artifact_may_be_indistinguishable_without_target_truth',)
              if method == "bounded_velocity_pulse_v1" else ()),
        ),
    })
    return item


def _empty_event_row(scope: Mapping[str, Any], event_id: str, event_type: str, start: float, end: float, run_id: str) -> dict[str, Any]:
    item = {column: None for column in EVENT_RESULT_COLUMNS}
    item.update({
        "run_id": run_id, "subject_id": scope.get("subject_id"), "session_id": scope.get("session_id"),
        "segment_id": scope.get("segment_id"), "event_id": event_id, "event_type": event_type,
        "start_s": start, "end_s": end, "duration_s": end - start,
        "status": "unknown", "reason_code": None, "n_observed": 0, "n_interpolated": 0,
        "algorithm_version": None, "parameter_ref": None, "interaction_id": scope.get("interaction_id"),
        "trial": scope.get("trial"), "stimulus_id": scope.get("stimulus_id"),
        "source_id": scope.get("source_id"), "source_event_id": None,
        "analysis_stream_id": scope.get("analysis_stream_id"),
        "physical_partition_id": scope.get("physical_partition_id"),
        "timebase_id": scope.get("timebase_id"), "clock_id": scope.get("clock_id"),
    })
    return item


def _interval_record(a: Mapping[str, Any], b: Mapping[str, Any], dt: float | None, angle: float | None,
                     speed: float | None, decision: str, reason: str, method: str) -> dict[str, Any]:
    return {
        "source_id": a.get("source_id"), "subject_id": a.get("subject_id"), "session_id": a.get("session_id"),
        "interaction_id": a.get("interaction_id"), "trial": a.get("trial"), "analysis_stream_id": a.get("analysis_stream_id"),
        "physical_partition_id": a.get("physical_partition_id"), "timebase_id": a.get("timebase_id"),
        "clock_id": a.get("clock_id"), "start_observation_id": a.get("observation_id"),
        "end_observation_id": b.get("observation_id"), "start_s": _finite(a.get("_t", a.get("time_s"))),
        "end_s": _finite(b.get("_t", b.get("time_s"))), "dt_s": dt, "angle_deg": angle,
        "velocity_deg_s": speed, "decision": decision, "reason_code": reason, "method": method,
    }


def _group_metadata(group: pd.DataFrame, key: tuple[Any, ...]) -> dict[str, Any]:
    row = group.iloc[0]
    return {"analysis_stream_id": row.get("analysis_stream_id"), "config": None}


def _stream_groups(frame: pd.DataFrame) -> list[tuple[tuple[Any, ...], pd.DataFrame]]:
    keys = [column for column in ("analysis_stream_id", "source_id", "subject_id", "session_id", "interaction_id",
                                  "trial", "timebase_id", "clock_id") if column in frame]
    if not keys:
        return [((), frame)]
    result = []
    for key, index in frame.groupby(keys, dropna=False, sort=False).indices.items():
        result.append((key if isinstance(key, tuple) else (key,), frame.iloc[np.asarray(index, dtype=int)]))
    return result


def _invalid_reason(validity: Any, has_time: bool) -> str:
    if not has_time: return "time_support_invalid"
    if validity == "unknown": return "source_validity_unknown"
    if validity == "invalid": return "source_gaze_invalid"
    return "analysis_stream_invalid"


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    dot = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    return float(np.degrees(np.arccos(np.clip(dot, -1.0, 1.0))))


class _RangeExtrema2D:
    """Sparse-table range queries for exact x-range + y-range I-DT dispersion."""

    def __init__(self, values: np.ndarray, *, cancel: Callable[[], bool] | None = None) -> None:
        self._logs = np.zeros(len(values) + 1, dtype=np.int32)
        for i in range(2, len(values) + 1):
            self._logs[i] = self._logs[i // 2] + 1
        self._min_x = [values[:, 0].copy()]
        self._max_x = [values[:, 0].copy()]
        self._min_y = [values[:, 1].copy()]
        self._max_y = [values[:, 1].copy()]
        level = 1
        while (1 << level) <= len(values):
            if cancel and cancel():
                raise _DetectionCancelled
            half = 1 << (level - 1)
            for tables, op in ((self._min_x, np.minimum), (self._max_x, np.maximum),
                               (self._min_y, np.minimum), (self._max_y, np.maximum)):
                previous = tables[-1]
                tables.append(op(previous[:-half], previous[half:]))
            level += 1

    def dispersion(self, start: int, end: int) -> float:
        length = end - start + 1
        level = int(self._logs[length])
        span = 1 << level
        other = end - span + 1
        x_range = max(float(self._max_x[level][start]), float(self._max_x[level][other])) - min(float(self._min_x[level][start]), float(self._min_x[level][other]))
        y_range = max(float(self._max_y[level][start]), float(self._max_y[level][other])) - min(float(self._min_y[level][start]), float(self._min_y[level][other]))
        return x_range + y_range


def _overlaps_trusted_tracking(rows: list[Mapping[str, Any]]) -> bool:
    for row in rows:
        valid = _is_true(row.get("tracking_valid"))
        state = str(row.get("tracking_state", "")).casefold()
        if valid and state in {"pursuit", "target_tracking", "smooth_pursuit", "reliable_pursuit"}:
            return True
    return False


def _same_partition_event(event: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    event_partition = event.get("physical_partition_id")
    row_partition = row.get("physical_partition_id")
    return event_partition is None or pd.isna(event_partition) or row_partition is None or pd.isna(row_partition) or str(event_partition) == str(row_partition)


def _stable_event_id(stream_id: str, event_type: str, method: str, ids: tuple[str, ...], parameter_ref: str) -> str:
    return "event:" + _stable_hash((stream_id, event_type, method, ids, parameter_ref))


def _parameter_ref(config: Any) -> str:
    if config is None:
        payload = {"schema": EVENT_SCHEMA_VERSION}
    elif hasattr(config, "to_dict"):
        payload = config.to_dict()
    else:
        payload = config
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "parameter:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _refs(value: Any) -> tuple[str, ...]:
    if not _nonmissing(value):
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    try:
        return tuple(str(item) for item in value if isinstance(item, str) and item.strip())
    except TypeError:
        return ()


def _external_text_field(record: Mapping[str, Any], name: str) -> str | None:
    value = record.get(name)
    if not _nonmissing(value):
        return None
    if not isinstance(value, str):
        raise EventDetectionError(f"External event field {name} must be text")
    return value.strip() or None


def _external_nonnegative_int(record: Mapping[str, Any], name: str) -> int:
    value = record.get(name)
    if not _nonmissing(value):
        return 0
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise EventDetectionError(f"External event field {name} must be a nonnegative integer")
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise EventDetectionError(f"External event field {name} must be a nonnegative integer")
    return int(number)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _nonmissing(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)):
            return not bool(missing)
    except (TypeError, ValueError):
        pass
    return True


def _scope_changed(current: Any, previous: Any) -> bool:
    current_known, previous_known = _nonmissing(current), _nonmissing(previous)
    if current_known != previous_known:
        return True
    return current_known and str(current) != str(previous)


def _is_true(value: Any) -> bool:
    return value is True or isinstance(value, np.bool_) and bool(value)
