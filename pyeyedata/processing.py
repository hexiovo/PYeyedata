"""Shared quality control and conservative preprocessing for eye-tracking signals."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib
import json
from numbers import Integral, Real
from typing import Any, Callable, Literal, Mapping

import numpy as np
import pandas as pd
from scipy.signal import butter, sosfiltfilt
from scipy.interpolate import PchipInterpolator

from .contracts import CONTRACT_VERSION, ContractError, NormalizedDataset
from .eye_streams import EyeSelectionConfig, derive_eye_streams, _snapshot_eye_stream_inputs
from .events import EVENT_SCHEMA_VERSION, EventDetectionConfig, detect_events
from .execution_cache import CacheIdentityError, CacheTransaction, _clone
from .scopes import (
    DERIVATION_COLUMNS,
    ScopeRelations,
    SCOPE_SCHEMA_VERSION,
    MEMBERSHIP_COLUMNS,
    OVERLAP_COLUMNS,
    PARTITION_COLUMNS,
    SEGMENT_AUDIT_COLUMNS,
    ScopeError,
    derive_scope_relations,
    _derive_scope_relations_identified,
    _identity_values_are_immutable,
    empty_table,
    _observation_identity_frame_owned,
)


class ProcessingError(ValueError):
    """Processing cannot continue without an explicit, scientifically safe choice."""


@dataclass(frozen=True)
class ProcessingConfig:
    order_policy: Literal["require_monotonic", "sort_by_time"] = "require_monotonic"
    duplicate_policy: Literal["reject", "retain_with_flag"] = "reject"
    support_gap_factor: float = 3.0
    support_gap_max_s: float | None = None
    interpolate_gaze: bool = False
    interpolate_pupil: bool = False
    pupil_interpolation_method: Literal["linear", "pchip"] = "linear"
    pupil_speed_mad_enabled: bool = False
    pupil_speed_mad_n: int = 20
    pupil_blink_padding_enabled: bool = False
    pupil_blink_padding_before_s: float = 0.050
    pupil_blink_padding_after_s: float = 0.150
    gaze_max_gap_s: float = 0.075
    pupil_max_gap_s: float = 0.200
    max_missing_samples: int = 3
    allow_unverified_interpolation_bounds: bool = False
    lowpass_cutoff_hz: float | None = None
    lowpass_order: int = 4
    pupil_baseline_window_s: tuple[float, float] | None = None
    baseline_min_coverage: float = 0.80
    baseline_min_length_s: float = 0.200

    def __post_init__(self) -> None:
        def finite_real(name: str, value: Any) -> None:
            # Validate without casting: legitimate NumPy scalars and real
            # numbers retain their exact value and type. Ordered comparisons
            # also cover NaN/Inf without narrowing extended-precision reals.
            if (isinstance(value, bool) or not isinstance(value, Real)
                    or not -np.inf < value < np.inf):
                raise ProcessingError(f"{name} must be a finite real number (not bool)")

        for name, choices in (
            ("order_policy", ("require_monotonic", "sort_by_time")),
            ("duplicate_policy", ("reject", "retain_with_flag")),
            ("pupil_interpolation_method", ("linear", "pchip")),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or value not in choices:
                raise ProcessingError(f"{name} must be one of {choices}")
        for name in ("interpolate_gaze", "interpolate_pupil", "pupil_speed_mad_enabled",
                     "pupil_blink_padding_enabled", "allow_unverified_interpolation_bounds"):
            if not isinstance(getattr(self, name), bool):
                raise ProcessingError(f"{name} must be bool (True or False)")
        finite_real("support_gap_factor", self.support_gap_factor)
        if self.support_gap_factor <= 1:
            raise ProcessingError("support_gap_factor must be greater than 1")
        for name in ("support_gap_max_s", "gaze_max_gap_s", "pupil_max_gap_s", "lowpass_cutoff_hz"):
            value = getattr(self, name)
            if value is None and name in ("support_gap_max_s", "lowpass_cutoff_hz"):
                continue
            finite_real(name, value)
            if value <= 0:
                raise ProcessingError(f"{name} must be positive")
        for name in ("max_missing_samples", "lowpass_order", "pupil_speed_mad_n"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
                raise ProcessingError(f"{name} must be a positive integer (not bool)")
        for name in ("pupil_blink_padding_before_s", "pupil_blink_padding_after_s"):
            finite_real(name, getattr(self, name))
            if getattr(self, name) < 0:
                raise ProcessingError(f"{name} must be zero or greater")
        finite_real("baseline_min_coverage", self.baseline_min_coverage)
        if not 0 < self.baseline_min_coverage <= 1:
            raise ProcessingError("baseline_min_coverage must be within (0, 1]")
        finite_real("baseline_min_length_s", self.baseline_min_length_s)
        if self.baseline_min_length_s < 0:
            raise ProcessingError("baseline_min_length_s must be zero or greater")
        if self.pupil_baseline_window_s is not None:
            window = self.pupil_baseline_window_s
            if isinstance(window, (str, bytes, bytearray)) or not isinstance(window, Sequence):
                raise ProcessingError("pupil_baseline_window_s must be a sequence of two finite real endpoints")
            try:
                if len(window) != 2:
                    raise ProcessingError("pupil_baseline_window_s must contain exactly two endpoints")
                start, end = window
            except (TypeError, ValueError, IndexError, OverflowError) as exc:
                raise ProcessingError("pupil_baseline_window_s must contain exactly two endpoints") from exc
            finite_real("pupil_baseline_window_s start", start)
            finite_real("pupil_baseline_window_s end", end)
            if end <= start:
                raise ProcessingError("pupil_baseline_window_s end must be greater than start (positive duration)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_policy": self.order_policy,
            "duplicate_policy": self.duplicate_policy,
            "support_gap_factor": self.support_gap_factor,
            "support_gap_max_s": self.support_gap_max_s,
            "interpolate_gaze": self.interpolate_gaze,
            "interpolate_pupil": self.interpolate_pupil,
            "pupil_interpolation_method": self.pupil_interpolation_method,
            "pupil_speed_mad_enabled": self.pupil_speed_mad_enabled,
            "pupil_speed_mad_n": self.pupil_speed_mad_n,
            "pupil_blink_padding_enabled": self.pupil_blink_padding_enabled,
            "pupil_blink_padding_before_s": self.pupil_blink_padding_before_s,
            "pupil_blink_padding_after_s": self.pupil_blink_padding_after_s,
            "gaze_max_gap_s": self.gaze_max_gap_s,
            "pupil_max_gap_s": self.pupil_max_gap_s,
            "max_missing_samples": self.max_missing_samples,
            "allow_unverified_interpolation_bounds": self.allow_unverified_interpolation_bounds,
            "lowpass_cutoff_hz": self.lowpass_cutoff_hz,
            "lowpass_order": self.lowpass_order,
            "pupil_baseline_window_s": list(self.pupil_baseline_window_s)
            if self.pupil_baseline_window_s else None,
            "baseline_min_coverage": self.baseline_min_coverage,
            "baseline_min_length_s": self.baseline_min_length_s,
        }


@dataclass
class ProcessingResult:
    signals: pd.DataFrame
    quality: pd.DataFrame
    audit: dict[str, Any]
    execution_status: Literal["complete", "partial", "cancelled", "failed"] = "complete"
    logs: list[str] = field(default_factory=list)
    analysis_streams: pd.DataFrame = field(default_factory=pd.DataFrame)
    events: pd.DataFrame = field(default_factory=pd.DataFrame)
    processing_partitions: pd.DataFrame = field(default_factory=lambda: empty_table(PARTITION_COLUMNS))
    analysis_memberships: pd.DataFrame = field(default_factory=lambda: empty_table(MEMBERSHIP_COLUMNS))
    segment_overlap: pd.DataFrame = field(default_factory=lambda: empty_table(OVERLAP_COLUMNS))
    segment_scope_audit: pd.DataFrame = field(default_factory=lambda: empty_table(SEGMENT_AUDIT_COLUMNS))
    label_derivations: pd.DataFrame = field(default_factory=lambda: empty_table(DERIVATION_COLUMNS))
    event_interval_audit: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class _BaseProcessingArtifact:
    frame: pd.DataFrame
    quality: pd.DataFrame
    source_metadata: dict[str, Any]
    logs: list[str]
    relations: ScopeRelations
    stream_keys: list[str]
    raw_event_frame: pd.DataFrame
    baseline_audit: list[dict[str, Any]]
    pupil_preprocessing_audit: list[dict[str, Any]]
    label_derivations: pd.DataFrame


PROCESSING_STAGE_VERSION = "base-processing-1.3.0/v1"
EYE_STAGE_VERSION = "eye-stream-derivation/v1"

PUPIL_PREPROCESSING_SOURCE_REFS = (
    {"method": "speed_mad", "citation": "Ong et al. (2025), Sensors 25(15), 4737",
     "doi": "10.3390/s25154737"},
    {"method": "pchip", "citation": "Ong et al. (2025), Sensors 25(15), 4737",
     "doi": "10.3390/s25154737"},
    {"method": "pchip_and_blink_loss_padding", "citation": "Scientific Reports (2026), s41598-026-48654-5",
     "doi": "10.1038/s41598-026-48654-5"},
    {"method": "provenance_and_quality_reporting", "citation": "Schwartz et al. (2026), Psychophysiology 63(8), e70375",
     "doi": "10.1111/psyp.70375"},
)


def _record_cache_calculation(transaction, stage, key, version, operation, status_for):
    """Record an actual stage invocation, including failed/cancelled attempts."""
    try:
        value = operation()
        status = status_for(value)
    except Exception as exc:
        transaction.audit["stages"].append({"stage": stage, "outcome": "calculated", "key": key,
                                              "algorithm_version": version, "execution_status": "failed",
                                              "failure_type": type(exc).__name__})
        raise
    transaction.audit["stages"].append({"stage": stage, "outcome": "calculated", "key": key,
                                          "algorithm_version": version, "execution_status": status})
    return value


def _shared_base_key(transaction, signals, settings, segments, overlap_policy, coordinate_metadata):
    try:
        metadata = dict(signals.metadata) if isinstance(signals, NormalizedDataset) else {}
        # Audit-only metadata is validated too, through the same early input
        # budget as every scientific key; no unbudgeted metadata hash bypass.
        if transaction.key("base_metadata", metadata) is None:
            return None
        source = signals.signals if isinstance(signals, NormalizedDataset) else signals
        effective_segments = signals.segments if segments is None and isinstance(signals, NormalizedDataset) else segments
        manifest = signals.source_manifest if isinstance(signals, NormalizedDataset) else []
        upstream_sources = [item for item in manifest if item.get("role") in {"signals", "segments"}]
        return transaction.key("base", {
            "contract": CONTRACT_VERSION, "processing": PROCESSING_STAGE_VERSION,
            "scope": SCOPE_SCHEMA_VERSION, "signals": source, "segments": effective_segments,
            "settings": settings, "overlap_policy": overlap_policy,
            "coordinate_declarations": coordinate_metadata or metadata.get("coordinate_metadata", {}),
            "source_manifest": upstream_sources,
        })
    except (CacheIdentityError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        transaction.audit["stages"].append({"stage": "base", "outcome": "miss", "reason": str(exc)})
        return None


def _process_base(signals, config, *, segments=None, overlap_policy="require_resolution", cancel=None, progress=None):
    settings = config or ProcessingConfig()
    if isinstance(signals, NormalizedDataset):
        signals.validate()
        if segments is None:
            segments = signals.segments
        frame = signals.signals.copy(deep=True)
        source_metadata = dict(signals.metadata)
    else:
        frame = signals.copy(deep=True)
        source_metadata = {}
    required = {"subject_id", "session_id", "time_s", "original_index"}
    missing = required - set(frame.columns)
    if missing:
        raise ProcessingError("Signal table missing: " + ", ".join(sorted(missing)))
    if frame["subject_id"].isna().any() or frame["session_id"].isna().any():
        raise ProcessingError("Every row needs an explicit subject_id and session_id")
    if "interaction_id" not in frame:
        frame["interaction_id"] = pd.NA
    frame["time_s"] = pd.to_numeric(frame["time_s"], errors="coerce").astype("Float64")
    frame["time_valid"] = frame["time_s"].notna()
    frame["source_gaze_valid"] = _boolean_column(frame, "gaze_valid")
    frame["source_pupil_left_valid"] = _boolean_column(frame, "pupil_left_valid")
    frame["source_pupil_right_valid"] = _boolean_column(frame, "pupil_right_valid")
    for column in ("gaze_x", "gaze_y", "gaze_z", "pupil_left", "pupil_right"):
        if column not in frame:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Float64")
    frame.index = pd.RangeIndex(len(frame))
    frame["processing_order"] = np.arange(len(frame), dtype=np.int64)

    keys = _stream_keys(frame)
    logs: list[str] = []
    if progress:
        progress(0.05, "检查时间顺序、重复时间与分段")
    if cancel and cancel():
        return _cancelled(frame, settings, source_metadata, logs, "Cancelled before QC")

    # Time policy is evaluated within each explicit stream; no rows are joined across IDs.
    duplicate_mask = pd.Series(False, index=frame.index)
    backwards: list[tuple[str, str, str]] = []
    for group_key, positions in _groups(frame, keys):
        times = frame.loc[positions, "time_s"].astype("float64").to_numpy()
        finite = np.isfinite(times)
        diff = np.diff(times[finite])
        if np.any(diff < 0):
            backwards.append(tuple(str(value) for value in group_key))
        stream_times = frame.loc[positions, "time_s"]
        duplicate_mask.loc[positions] = (
            stream_times.notna() & stream_times.duplicated(keep=False)
        ).to_numpy(dtype=bool)
    if backwards and settings.order_policy == "require_monotonic":
        raise ProcessingError("Time is out of order within a stream; explicitly choose sort_by_time")
    if duplicate_mask.any() and settings.duplicate_policy == "reject":
        raise ProcessingError("Duplicate timestamps found; choose retain_with_flag or resolve source rows")
    if backwards and settings.order_policy == "sort_by_time":
        frame = frame.sort_values(keys + ["time_s", "processing_order"], kind="stable", na_position="last").reset_index(drop=True)
        logs.append("rows_sorted_within_subject_session_interaction; original_index preserved")
        keys = _stream_keys(frame)
        duplicate_mask = _duplicate_times(frame, keys)
    frame["time_duplicate"] = duplicate_mask.to_numpy(dtype=bool)

    segment_frame = segments.copy(deep=True) if segments is not None else pd.DataFrame()
    if not segment_frame.empty and "overlap_policy" in segment_frame:
        declared = segment_frame["overlap_policy"].dropna().astype(str).unique()
        if len(declared) > 1:
            raise ProcessingError("Segments declare conflicting overlap policies")
        if len(declared) == 1:
            overlap_policy = declared[0]  # type: ignore[assignment]
    try:
        frame, _ = _observation_identity_frame_owned(frame)
        keys = _stream_keys(frame)
        # No identity column changed since the full validation above. Public
        # calls and structured/dynamic identity values retain full validation.
        reuse_identity = _identity_values_are_immutable(frame)
        derive_relations = _derive_scope_relations_identified if reuse_identity else derive_scope_relations
        relation_frame = frame.copy(deep=True) if reuse_identity and not segment_frame.empty else frame
        relations = derive_relations(
            relation_frame,
            segment_frame,
            overlap_policy=overlap_policy,
            gap_threshold_for=lambda values: _support_gap_threshold(np.asarray(values, dtype=float), settings),
            stream_keys=keys,
        )
    except ScopeError as exc:
        raise ProcessingError(str(exc)) from exc
    assignments = relations.assignments
    for column in assignments:
        frame[column] = assignments[column].to_numpy()
    for column in ("processing_barrier_before", "processing_barrier_reasons", "event_barrier_before", "event_barrier_reasons"):
        if column in assignments:
            frame[column] = assignments[column].to_numpy()
    if "segment_id" not in frame:
        frame["segment_id"] = pd.NA
    if "analysis_membership_ids" not in frame:
        frame["analysis_membership_ids"] = [tuple() for _ in range(len(frame))]
    if relations.blocked:
        logs.append(f"segment_scope_blocked:{relations.reason}")
    stream_keys = _stream_keys(frame)
    support_keys = list(dict.fromkeys([*stream_keys, "processing_partition_id"]))
    raw_event_frame = _snapshot_eye_stream_inputs(frame)
    for col in (
        "gaze_interpolated", "gaze_x_interpolated", "gaze_y_interpolated",
        "pupil_left_interpolated", "pupil_right_interpolated",
    ):
        frame[col] = False
    frame["analysis_gaze_valid"] = _signal_valid(frame, ("gaze_x", "gaze_y"), "source_gaze_valid")
    frame["analysis_pupil_left_valid"] = _signal_valid(frame, ("pupil_left",), "source_pupil_left_valid")
    frame["analysis_pupil_right_valid"] = _signal_valid(frame, ("pupil_right",), "source_pupil_right_valid")
    frame["analysis_gaze_status"] = _validity_status(frame, ("gaze_x", "gaze_y"), "source_gaze_valid")
    frame["analysis_pupil_left_status"] = _validity_status(frame, ("pupil_left",), "source_pupil_left_valid")
    frame["analysis_pupil_right_status"] = _validity_status(frame, ("pupil_right",), "source_pupil_right_valid")

    pupil_preprocessing_audit = _apply_pupil_artifact_masks(frame, support_keys, settings)
    for eye in ("left", "right"):
        valid_column = f"analysis_pupil_{eye}_valid"
        status_column = f"analysis_pupil_{eye}_status"
        source_column = f"source_pupil_{eye}_valid"
        value_column = f"pupil_{eye}"
        rejected_column = f"pupil_{eye}_speed_mad_rejected"
        padded_column = f"pupil_{eye}_blink_loss_padded"
        interpolation_status_column = f"pupil_{eye}_interpolation_status"
        frame[interpolation_status_column] = pd.Series("not_requested", index=frame.index, dtype="string")
        frame[valid_column] = _signal_valid(frame, (value_column,), source_column)
        frame[status_column] = _validity_status(frame, (value_column,), source_column)
        frame.loc[frame[padded_column], status_column] = "blink_loss_padded"
        frame.loc[frame[rejected_column], status_column] = "speed_mad_rejected"

    if settings.interpolate_gaze:
        _backup_raw(frame, ("gaze_x", "gaze_y"))
        for _, positions in _groups(frame, support_keys):
            for column in ("gaze_x", "gaze_y"):
                _interpolate_column(
                    frame, positions, column, "analysis_gaze_valid", f"{column}_interpolated",
                    settings.gaze_max_gap_s, settings, "source_gaze_valid",
                )
        axis_interpolated = frame["gaze_x_interpolated"] & frame["gaze_y_interpolated"]
        any_interpolated = frame["gaze_x_interpolated"] | frame["gaze_y_interpolated"]
        source_allows_partial = frame["source_gaze_valid"].ne(False).fillna(True)
        frame["gaze_interpolated"] = any_interpolated & (source_allows_partial | axis_interpolated)
        frame["analysis_gaze_valid"] = _signal_valid(frame, ("gaze_x", "gaze_y"), "source_gaze_valid", allow_interpolated="gaze_interpolated")
        frame.loc[frame["gaze_interpolated"], "analysis_gaze_status"] = "interpolated"
        logs.append("gaze_short_gap_interpolation_enabled; source values retained in raw__ columns")
    if settings.interpolate_pupil:
        _backup_raw(frame, ("pupil_left", "pupil_right"))
        for group_key, positions in _groups(frame, support_keys):
            key_tuple = group_key if isinstance(group_key, tuple) else (group_key,)
            stream_id = {name: _audit_scalar(value) for name, value in zip(support_keys, key_tuple)}
            for column, valid_column, flag_column, source_valid in (
                ("pupil_left", "analysis_pupil_left_valid", "pupil_left_interpolated", "source_pupil_left_valid"),
                ("pupil_right", "analysis_pupil_right_valid", "pupil_right_interpolated", "source_pupil_right_valid"),
            ):
                stats = _interpolate_column(frame, positions, column, valid_column, flag_column,
                                            settings.pupil_max_gap_s, settings, source_valid,
                                            method=settings.pupil_interpolation_method,
                                            status_column=f"{column}_interpolation_status")
                pupil_preprocessing_audit.append({
                    **stream_id, "stage": "interpolation", "eye": column.removeprefix("pupil_"),
                    "method": settings.pupil_interpolation_method,
                    "max_gap_s": settings.pupil_max_gap_s,
                    "max_missing_samples": settings.max_missing_samples,
                    **stats,
                })
        frame["analysis_pupil_left_valid"] = _signal_valid(frame, ("pupil_left",), "source_pupil_left_valid", allow_interpolated="pupil_left_interpolated")
        frame["analysis_pupil_right_valid"] = _signal_valid(frame, ("pupil_right",), "source_pupil_right_valid", allow_interpolated="pupil_right_interpolated")
        for eye in ("left", "right"):
            status_column = f"analysis_pupil_{eye}_status"
            source_column = f"source_pupil_{eye}_valid"
            value_column = f"pupil_{eye}"
            interpolated_column = f"pupil_{eye}_interpolated"
            frame[status_column] = _validity_status(frame, (value_column,), source_column)
            frame.loc[frame[f"pupil_{eye}_blink_loss_padded"], status_column] = "blink_loss_padded"
            frame.loc[frame[f"pupil_{eye}_speed_mad_rejected"], status_column] = "speed_mad_rejected"
            frame.loc[frame[interpolated_column], status_column] = "interpolated"
            interpolation_status = frame[f"pupil_{eye}_interpolation_status"]
            explanatory_missing = (
                frame[value_column].isna()
                & ~frame[f"pupil_{eye}_blink_loss_padded"]
                & ~frame[f"pupil_{eye}_speed_mad_rejected"]
                & interpolation_status.isin((
                    "pchip_insufficient_anchors", "pchip_discontinuous_support",
                    "interpolation_sample_bound", "interpolation_untrusted_bounds",
                    "interpolation_duration_bound", "interpolation_invalid_target_time",
                ))
            )
            frame.loc[explanatory_missing, status_column] = interpolation_status.loc[explanatory_missing]
        logs.append("pupil_interpolation_enabled; source values retained in raw__ columns")

    if settings.lowpass_cutoff_hz is not None:
        _backup_raw(frame, ("gaze_x", "gaze_y", "pupil_left", "pupil_right"))
        for column, valid_column, group in (
            ("gaze_x", "analysis_gaze_valid", "gaze"),
            ("gaze_y", "analysis_gaze_valid", "gaze"),
            ("pupil_left", "analysis_pupil_left_valid", "pupil_left"),
            ("pupil_right", "analysis_pupil_right_valid", "pupil_right"),
        ):
            _apply_lowpass(frame, column, valid_column, support_keys, settings, logs)
        logs.append(f"lowpass_enabled; cutoff_hz={settings.lowpass_cutoff_hz:g}; order={settings.lowpass_order}")

    baseline_audit: list[dict[str, Any]] = []
    label_derivations = empty_table(DERIVATION_COLUMNS)
    has_label_baselines = any(
        column in segment_frame and segment_frame[column].notna().any()
        for column in ("baseline_start_s", "baseline_end_s")
    )
    if settings.pupil_baseline_window_s is not None or has_label_baselines:
        baseline_audit, label_derivations = _apply_pupil_baseline(
            frame, stream_keys, settings, relations.analysis_memberships,
            segment_frame, logs,
        )

    if progress:
        progress(0.80, "汇总每个明确数据流的质量与时间支持")
    quality = _quality_summary(frame, stream_keys, settings)
    return _BaseProcessingArtifact(frame, quality, source_metadata, logs, relations, stream_keys,
                                   raw_event_frame, baseline_audit, pupil_preprocessing_audit,
                                   label_derivations)


def _process_eyes(raw_event_frame, selected_eye_config, coordinate_contract, cancel, progress):
    return derive_eye_streams(
        raw_event_frame, selected_eye_config, coordinate_metadata=coordinate_contract,
        cancel=cancel, progress=(lambda fraction, message: progress(0.80 + 0.05 * fraction, message)) if progress else None,
    )


def _process_events(analysis_streams, selected_event_config, coordinate_contract, external_events,
                    source_metadata, settings, cancel, progress):
    return detect_events(
        analysis_streams, selected_event_config, coordinate_metadata=coordinate_contract,
        external_events=external_events, run_id=str(source_metadata.get("run_id", "run:local")),
        max_gap_s=settings.support_gap_max_s, cancel=cancel,
        progress=(lambda fraction, message: progress(0.86 + 0.10 * fraction, message)) if progress else None,
    )


def process_signals(
    signals: pd.DataFrame | NormalizedDataset,
    config: ProcessingConfig | None = None,
    *,
    segments: pd.DataFrame | None = None,
    overlap_policy: Literal["require_resolution", "keep_labels", "corrected", "reject"] = "require_resolution",
    eye_selection: EyeSelectionConfig | None = None,
    event_detection: EventDetectionConfig | None = None,
    external_events: pd.DataFrame | None = None,
    coordinate_metadata: Mapping[str, Any] | None = None,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    _cache_transaction: CacheTransaction | None = None,
) -> ProcessingResult:
    """Return a derived copy; input frame and its source validity flags are never modified."""
    settings = config or ProcessingConfig()
    transaction = _cache_transaction
    if transaction is not None:
        transaction.audit.setdefault("algorithm_versions", {}).update({
            "contract": CONTRACT_VERSION, "processing": PROCESSING_STAGE_VERSION,
            "eyes": EYE_STAGE_VERSION, "events": EVENT_SCHEMA_VERSION, "scope": SCOPE_SCHEMA_VERSION,
        })
    base_key = None
    if transaction is None:
        base = _process_base(signals, settings, segments=segments, overlap_policy=overlap_policy,
                             cancel=cancel, progress=progress)
    else:
        if isinstance(signals, NormalizedDataset):
            signals.validate()
        base_key = _shared_base_key(transaction, signals, settings, segments, overlap_policy, coordinate_metadata)
        base = transaction.get("base", base_key)
        if base is None:
            base = _record_cache_calculation(
                transaction, "base", base_key, PROCESSING_STAGE_VERSION,
                lambda: _process_base(signals, settings, segments=segments, overlap_policy=overlap_policy,
                                       cancel=cancel, progress=progress),
                lambda value: value.execution_status if isinstance(value, ProcessingResult)
                else "partial" if value.relations.blocked else "complete")
            if isinstance(base, _BaseProcessingArtifact) and not base.relations.blocked:
                transaction.put("base", base_key, base)
        elif isinstance(signals, NormalizedDataset):
            # The source run is audit identity, not semantic equivalence.
            base.source_metadata = dict(signals.metadata)
            base.frame.attrs = _clone(signals.signals.attrs, cancel)
    if isinstance(base, ProcessingResult):
        return base
    frame, quality, source_metadata, logs = base.frame, base.quality, base.source_metadata, base.logs
    relations, stream_keys = base.relations, base.stream_keys
    raw_event_frame, baseline_audit = base.raw_event_frame, base.baseline_audit
    pupil_preprocessing_audit = base.pupil_preprocessing_audit
    label_derivations = base.label_derivations
    selected_eye_config = eye_selection or EyeSelectionConfig(mode="legacy_source")
    selected_event_config = event_detection or EventDetectionConfig()
    coordinate_contract: Mapping[str, Any] = coordinate_metadata or source_metadata.get("coordinate_metadata", {})
    if cancel and cancel():
        logs.append("cancelled before eye stream expansion")
        audit = {
            "contract_version": CONTRACT_VERSION, "processing_config": settings.to_dict(),
            "source_metadata": source_metadata, "execution_status": "cancelled",
            "processing_schema_version": "1.3.0",
            "pupil_preprocessing": _pupil_preprocessing_audit(settings, pupil_preprocessing_audit),
        }
        return ProcessingResult(frame, quality, audit, "cancelled", logs,
                                processing_partitions=relations.processing_partitions,
                                analysis_memberships=relations.analysis_memberships,
                                segment_overlap=relations.segment_overlap,
                                segment_scope_audit=relations.segment_scope_audit,
                                label_derivations=label_derivations)
    eye_key = None
    eye_artifact = None
    if transaction is not None and base_key is not None:
        eye_key = transaction.key("eyes", {"base_key": base_key, "eye_version": EYE_STAGE_VERSION,
                                            "selection": selected_eye_config, "coordinate": coordinate_contract})
        eye_artifact = transaction.get("eyes", eye_key)
    if eye_artifact is None:
        if transaction is None:
            eye_artifact = _process_eyes(raw_event_frame, selected_eye_config, coordinate_contract, cancel, progress)
        else:
            eye_artifact = _record_cache_calculation(
                transaction, "eyes", eye_key, EYE_STAGE_VERSION,
                lambda: _process_eyes(raw_event_frame, selected_eye_config, coordinate_contract, cancel, progress),
                lambda value: "cancelled" if value[2] else "complete")
        if transaction is not None and not eye_artifact[2]:
            transaction.put("eyes", eye_key, eye_artifact)
    analysis_streams, eye_logs, eye_cancelled = eye_artifact
    logs.extend(eye_logs)
    if eye_cancelled:
        audit = {
            "contract_version": CONTRACT_VERSION, "processing_config": settings.to_dict(),
            "eye_selection_config": selected_eye_config.to_dict(), "source_metadata": source_metadata,
            "execution_status": "cancelled", "processing_schema_version": "1.3.0",
            "pupil_preprocessing": _pupil_preprocessing_audit(settings, pupil_preprocessing_audit),
        }
        return ProcessingResult(frame, quality, audit, "cancelled", logs, analysis_streams=analysis_streams,
                                processing_partitions=relations.processing_partitions,
                                analysis_memberships=relations.analysis_memberships,
                                segment_overlap=relations.segment_overlap,
                                segment_scope_audit=relations.segment_scope_audit,
                                label_derivations=label_derivations)
    if progress:
        progress(0.86, "按具名方法与物理分区检测事件")
    event_key = None
    event_result = None
    if transaction is not None and eye_key is not None:
        event_key = transaction.key("events", {"eye_key": eye_key, "version": EVENT_SCHEMA_VERSION,
                                                "config": selected_event_config, "coordinate": coordinate_contract,
                                                "external_events": external_events, "max_gap_s": settings.support_gap_max_s})
        event_result = transaction.get("events", event_key)
    if event_result is None:
        if transaction is None:
            event_result = _process_events(analysis_streams, selected_event_config, coordinate_contract,
                                           external_events, source_metadata, settings, cancel, progress)
        else:
            event_result = _record_cache_calculation(
                transaction, "events", event_key, EVENT_SCHEMA_VERSION,
                lambda: _process_events(analysis_streams, selected_event_config, coordinate_contract,
                                          external_events, source_metadata, settings, cancel, progress),
                lambda value: value.execution_status)
        if transaction is not None and event_result.execution_status == "complete":
            transaction.put("events", event_key, event_result)
    elif "run_id" in event_result.events:
        # Event IDs depend on stream/method/observations/parameters, not run_id.
        # Only this declared schema column is rebound; source_event_id is intact.
        event_result.events["run_id"] = str(source_metadata.get("run_id", "run:local"))
    logs.extend(event_result.logs or [])
    audit = {
        "contract_version": CONTRACT_VERSION,
        "processing_config": settings.to_dict(),
        "source_metadata": source_metadata,
        "stream_keys": list(stream_keys),
        "row_count_input": int(len(signals.signals if isinstance(signals, NormalizedDataset) else signals)),
        "row_count_output": int(len(frame)),
        "duplicate_time_rows": int(frame["time_duplicate"].sum()),
        "time_order_policy": settings.order_policy,
        "duplicate_policy": settings.duplicate_policy,
        "quality_streams": int(len(quality)),
        "baseline_windows": baseline_audit,
        "processing_schema_version": "1.3.0",
        "pupil_preprocessing": _pupil_preprocessing_audit(settings, pupil_preprocessing_audit),
        "scope_blocked": relations.blocked,
        "scope_block_reason": relations.reason,
        "n_processing_partitions": int(len(relations.processing_partitions)),
        "n_analysis_memberships": int(len(relations.analysis_memberships)),
        "n_segment_overlaps": int(len(relations.segment_overlap)),
        "source_raw_columns_retained": sorted(column for column in frame if column.startswith("raw__")),
        "eye_selection_config": selected_eye_config.to_dict(),
        "event_detection_config": selected_event_config.to_dict(),
        "n_analysis_stream_rows": int(len(analysis_streams)),
        "n_events": int(len(event_result.events)),
        "n_event_interval_audit_rows": int(len(event_result.interval_audit)),
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event_execution_status": event_result.execution_status,
    }
    if progress:
        progress(1.0, "基础质量控制完成")
    status: Literal["complete", "partial", "cancelled"] = (
        "cancelled" if event_result.execution_status == "cancelled"
        else "partial" if relations.blocked or event_result.execution_status == "partial"
        else "complete"
    )
    return ProcessingResult(
        frame, quality, audit, status, logs,
        analysis_streams=analysis_streams,
        events=event_result.events,
        processing_partitions=relations.processing_partitions,
        analysis_memberships=relations.analysis_memberships,
        segment_overlap=relations.segment_overlap,
        segment_scope_audit=relations.segment_scope_audit,
        label_derivations=label_derivations,
        event_interval_audit=event_result.interval_audit,
    )


def _stream_keys(frame: pd.DataFrame) -> list[str]:
    return [name for name in ("subject_id", "session_id", "interaction_id", "source_id", "timebase_id", "trial") if name in frame]


def _groups(frame: pd.DataFrame, keys: list[str]):
    if not keys:
        return [((), np.arange(len(frame)))]
    for group_key, positions in frame.groupby(keys, dropna=False, sort=False).indices.items():
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        yield group_key, np.asarray(positions, dtype=np.int64)


def _duplicate_times(frame: pd.DataFrame, keys: list[str]) -> pd.Series:
    duplicated = pd.Series(False, index=frame.index)
    for _, positions in _groups(frame, keys):
        group = frame.loc[positions]
        mask = group["time_s"].notna() & group.duplicated("time_s", keep=False)
        duplicated.loc[group.index[mask]] = True
    return duplicated


def _boolean_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(pd.array([pd.NA] * len(frame), dtype="boolean"), index=frame.index)
    try:
        return frame[name].astype("boolean")
    except (TypeError, ValueError) as exc:
        raise ProcessingError(f"{name} must use nullable boolean values") from exc


def _signal_valid(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
    source_flag: str,
    *,
    allow_interpolated: str | None = None,
) -> pd.Series:
    values = frame[list(columns)].apply(pd.to_numeric, errors="coerce")
    present = values.notna().all(axis=1)
    finite = pd.Series(
        np.isfinite(values.to_numpy(dtype=float, na_value=np.nan)).all(axis=1),
        index=frame.index,
    )
    source = frame[source_flag]
    # Unknown source validity remains visible in the status column, but finite values can be
    # described; explicitly invalid source rows are never silently accepted.
    result = present & finite & source.ne(False).fillna(True)
    if allow_interpolated and allow_interpolated in frame:
        result = result | (frame[allow_interpolated].astype(bool) & present & finite)
    return result.astype("boolean")


def _validity_status(frame: pd.DataFrame, columns: tuple[str, ...], source_flag: str) -> pd.Series:
    values = frame[list(columns)].apply(pd.to_numeric, errors="coerce")
    present = values.notna().all(axis=1)
    finite = pd.Series(
        np.isfinite(values.to_numpy(dtype=float, na_value=np.nan)).all(axis=1),
        index=frame.index,
    )
    nonfinite = present & ~finite
    for column in columns:
        marker = "nonfinite__" + column
        if marker in frame:
            nonfinite |= frame[marker].astype("boolean").fillna(False)
    source = frame[source_flag]
    status = pd.Series("observed_validity_unknown", index=frame.index, dtype="string")
    status.loc[~present & ~nonfinite] = "missing_value"
    status.loc[nonfinite] = "nonfinite_value"
    status.loc[source.eq(False).fillna(False)] = "source_invalid"
    status.loc[present & finite & source.eq(True).fillna(False)] = "source_valid"
    return status


def _backup_raw(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    for column in columns:
        name = "raw__" + column
        if name not in frame:
            frame[name] = frame[column].copy(deep=True)


def _pupil_loss_seeds(frame: pd.DataFrame, positions: np.ndarray, eye: str,
                      values: np.ndarray, source_valid: pd.Series) -> np.ndarray:
    finite = np.isfinite(values)
    seeds = ~finite | source_valid.eq(False).fillna(False).to_numpy(dtype=bool)
    # Some trackers encode loss as zero. Respect an explicit source-valid flag
    # when present; otherwise a zero cannot be distinguished from a sentinel.
    seeds |= finite & (values == 0) & source_valid.isna().to_numpy(dtype=bool)
    for column in (f"blink_{eye}_event", f"eyelid_closed_{eye}"):
        if column in frame:
            seeds |= frame.loc[positions, column].eq(True).fillna(False).to_numpy(dtype=bool)
    return seeds


def _apply_pupil_artifact_masks(
    frame: pd.DataFrame,
    support_keys: list[str],
    settings: ProcessingConfig,
) -> list[dict[str, Any]]:
    eyes = ("left", "right")
    for eye in eyes:
        for suffix in ("speed_mad_rejected", "blink_loss_seed", "blink_loss_padded"):
            frame[f"pupil_{eye}_{suffix}"] = False
    if not (settings.pupil_blink_padding_enabled or settings.pupil_speed_mad_enabled):
        return []

    _backup_raw(frame, ("pupil_left", "pupil_right"))
    audit: list[dict[str, Any]] = []
    for group_key, positions in _groups(frame, support_keys):
        times = frame.loc[positions, "time_s"].to_numpy(dtype=np.float64, na_value=np.nan)
        finite_time_positions = np.flatnonzero(np.isfinite(times))
        finite_times = times[finite_time_positions]
        support_gap = _support_gap_threshold(times, settings)
        stream_id = {name: _audit_scalar(value) for name, value in
                     zip(support_keys, group_key if isinstance(group_key, tuple) else (group_key,))}
        for eye in eyes:
            column = f"pupil_{eye}"
            source_valid_column = f"source_pupil_{eye}_valid"
            source_valid = frame.loc[positions, source_valid_column]
            values = pd.to_numeric(frame.loc[positions, column], errors="coerce").to_numpy(
                dtype=np.float64, na_value=np.nan
            )
            local_valid = source_valid.eq(True).fillna(False).to_numpy(dtype=bool)
            row_duplicates = frame.loc[positions, "time_duplicate"].fillna(False).to_numpy(dtype=bool)
            seed = (_pupil_loss_seeds(frame, positions, eye, values, source_valid)
                    if settings.pupil_blink_padding_enabled else np.zeros(len(positions), dtype=bool))
            padded = seed.copy()
            if settings.pupil_blink_padding_enabled and len(finite_times):
                finite_deltas = np.diff(finite_times)
                if support_gap is None:
                    split_at = np.arange(1, len(finite_times), dtype=np.int64)
                else:
                    discontinuity = ((np.diff(finite_time_positions) != 1) | ~np.isfinite(finite_deltas)
                                     | (finite_deltas <= 0) | (finite_deltas > support_gap))
                    split_at = np.flatnonzero(discontinuity) + 1
                for support_run in np.split(finite_time_positions, split_at):
                    run_times = times[support_run]
                    run_seed_times = run_times[seed[support_run]]
                    if not len(run_seed_times):
                        continue
                    range_starts = np.searchsorted(
                        run_times,
                        np.nextafter(run_seed_times - settings.pupil_blink_padding_before_s, -np.inf),
                        side="left",
                    )
                    range_ends = np.searchsorted(
                        run_times,
                        np.nextafter(run_seed_times + settings.pupil_blink_padding_after_s, np.inf),
                        side="right",
                    )
                    range_delta = np.zeros(len(support_run) + 1, dtype=np.int64)
                    np.add.at(range_delta, range_starts, 1)
                    np.add.at(range_delta, range_ends, -1)
                    padded[support_run] |= np.cumsum(range_delta[:-1]) > 0
            rejected = np.zeros(len(positions), dtype=bool)
            speed_values = np.array([], dtype=np.float64)
            median_speed = mad_speed = threshold = None
            mad_zero = False
            rejection_status = "disabled"
            if settings.pupil_speed_mad_enabled:
                finite_value = np.isfinite(values) & np.isfinite(times)
                speed_anchors = finite_value & local_valid & ~row_duplicates & ~padded
                if len(positions) > 1 and support_gap is not None:
                    delta_time = np.diff(times)
                    edge_valid = (speed_anchors[:-1] & speed_anchors[1:] & np.isfinite(delta_time)
                                  & (delta_time > 0) & (delta_time <= support_gap))
                    edge_speed = np.full(len(delta_time), np.nan, dtype=np.float64)
                    delta_value = np.diff(values)
                    edge_speed[edge_valid] = np.abs(delta_value[edge_valid] / delta_time[edge_valid])
                    sample_speed = np.full(len(positions), np.nan, dtype=np.float64)
                    sample_speed[:-1] = np.where(edge_valid, np.fmax(sample_speed[:-1], edge_speed), sample_speed[:-1])
                    sample_speed[1:] = np.where(edge_valid, np.fmax(sample_speed[1:], edge_speed), sample_speed[1:])
                    speed_values = sample_speed[np.isfinite(sample_speed)]
                if len(speed_values):
                    median_speed = float(np.median(speed_values))
                    mad_speed = float(np.median(np.abs(speed_values - median_speed)))
                    threshold = float(median_speed + settings.pupil_speed_mad_n * mad_speed)
                    mad_zero = mad_speed == 0.0
                    rejected = (speed_anchors & np.isfinite(sample_speed) & (sample_speed > threshold))
                    rejection_status = "computed_zero_mad" if mad_zero else "computed"
                else:
                    rejection_status = "no_valid_adjacent_source_samples"

            frame.loc[positions[seed], f"pupil_{eye}_blink_loss_seed"] = True
            frame.loc[positions[padded], f"pupil_{eye}_blink_loss_padded"] = True
            frame.loc[positions[rejected], f"pupil_{eye}_speed_mad_rejected"] = True
            masked = padded | rejected
            if masked.any():
                frame.loc[positions[masked], column] = pd.NA
            audit.append({
                **stream_id,
                "eye": eye,
                "blink_loss_padding_enabled": settings.pupil_blink_padding_enabled,
                "n_blink_loss_seed_samples": int(seed.sum()),
                "n_blink_loss_padded_samples": int(padded.sum()),
                "speed_mad_enabled": settings.pupil_speed_mad_enabled,
                "speed_mad_n": settings.pupil_speed_mad_n,
                "speed_unit": "source pupil unit per second; unspecified when source pupil unit is unknown",
                "n_speed_samples": int(len(speed_values)),
                "median_speed": median_speed,
                "mad_speed": mad_speed,
                "speed_threshold": threshold,
                "mad_zero": mad_zero,
                "mad_zero_manual_review_recommended": bool(settings.pupil_speed_mad_enabled and mad_zero),
                "speed_mad_status": rejection_status,
                "n_speed_mad_rejected_samples": int(rejected.sum()),
                "n_masked_samples": int(masked.sum()),
                "row_flag_columns": {
                    "speed_mad_rejected": f"pupil_{eye}_speed_mad_rejected",
                    "blink_loss_seed": f"pupil_{eye}_blink_loss_seed",
                    "blink_loss_padded": f"pupil_{eye}_blink_loss_padded",
                },
            })
    return audit


def _assign_segments(frame: pd.DataFrame, segments: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if "segment_id" not in frame:
        frame["segment_id"] = pd.NA
    if segments.empty:
        return frame
    required = {"subject_id", "session_id", "segment_id", "start_s", "end_s"}
    if required - set(segments):
        raise ProcessingError("segments missing: " + ", ".join(sorted(required - set(segments))))
    interaction_count = frame["interaction_id"].nunique(dropna=True)
    if "interaction_id" not in segments and (interaction_count > 1 or (interaction_count > 0 and frame["interaction_id"].isna().any())):
        raise ProcessingError("Segments need interaction_id when multiple interactions share a session")
    if "interaction_id" in segments and "interaction_id" not in frame:
        raise ProcessingError("Segment interaction_id cannot be matched to signals")
    ids = segments[["subject_id", "session_id", "segment_id"]]
    if ids.isna().any().any() or ids.astype("string").apply(lambda col: col.str.strip().eq("")).any().any():
        raise ProcessingError("Segment subject_id/session_id/segment_id must be nonempty")
    starts = pd.to_numeric(segments["start_s"], errors="coerce")
    ends = pd.to_numeric(segments["end_s"], errors="coerce")
    if starts.isna().any() or ends.isna().any() or not np.isfinite(starts.to_numpy(dtype=float)).all() or not np.isfinite(ends.to_numpy(dtype=float)).all():
        raise ProcessingError("Segment boundaries must be finite numeric seconds")
    if (ends <= starts).any():
        raise ProcessingError("Segments must be nonempty left-closed, right-open intervals")
    matched = pd.Series(False, index=frame.index)
    for segment_position, (_, segment) in enumerate(segments.iterrows()):
        mask = (
            frame["subject_id"].astype("string").eq(str(segment["subject_id"]))
            & frame["session_id"].astype("string").eq(str(segment["session_id"]))
            & frame["time_s"].ge(float(starts.iloc[segment_position]))
            & frame["time_s"].lt(float(ends.iloc[segment_position]))
        )
        if "interaction_id" in segments:
            if pd.isna(segment["interaction_id"]) or not str(segment["interaction_id"]).strip():
                raise ProcessingError("Segment interaction_id must be explicit when the column is present")
            mask &= frame["interaction_id"].astype("string").eq(str(segment["interaction_id"]))
        mask = mask.fillna(False).astype(bool)
        if (matched & mask).any():
            raise ProcessingError("Overlapping segments make sample assignment ambiguous")
        frame.loc[mask, "segment_id"] = segment["segment_id"]
        matched |= mask
    return frame


def _interpolate_column(
    frame: pd.DataFrame,
    positions: np.ndarray,
    column: str,
    valid_column: str,
    flag_column: str,
    max_gap_s: float,
    settings: ProcessingConfig,
    source_valid_column: str,
    *,
    method: Literal["linear", "pchip"] = "linear",
    status_column: str | None = None,
) -> dict[str, Any]:
    stats = {
        "candidate_gap_runs": 0, "interpolated_gap_runs": 0, "interpolated_samples": 0,
        "skipped_sample_bound": 0, "skipped_untrusted_bounds": 0,
        "skipped_duration_bound": 0, "skipped_invalid_target_time": 0,
        "skipped_pchip_insufficient_anchors": 0, "skipped_pchip_discontinuous_support": 0,
    }
    if status_column:
        stats["insertion_provenance"] = []
    pos = np.asarray(positions, dtype=np.int64)
    values = pd.to_numeric(frame.loc[pos, column], errors="coerce").astype("float64").to_numpy()
    times = frame.loc[pos, "time_s"].astype("float64").to_numpy()
    validity = frame.loc[pos, valid_column].fillna(False).to_numpy(dtype=bool)
    source_valid = frame.loc[pos, source_valid_column]
    trusted = validity & np.isfinite(values) & np.isfinite(times)
    if not settings.allow_unverified_interpolation_bounds:
        unknown = source_valid.isna().to_numpy()
        trusted &= ~unknown
    observation_column = None
    if status_column:
        if "observation_id" in frame:
            observation_column = "observation_id"
        elif "original_index" in frame:
            observation_column = "original_index"
    pchip_support_gap = _support_gap_threshold(times, settings) if method == "pchip" else None
    # Interpolation only fills absent/non-finite samples. A finite source value stays intact
    # even when the device marked it invalid or its validity semantics are unknown. A rejection
    # or padding flag records the source artifact decision; a short bounded gap can still be
    # reconstructed, while its flag and raw source value remain available for review.
    missing = ~np.isfinite(values)
    if status_column and status_column in frame:
        frame.loc[pos, status_column] = "not_missing"
    i = 0
    while i < len(pos):
        if not missing[i]:
            i += 1
            continue
        begin = i
        while i < len(pos) and missing[i]:
            i += 1
        end = i
        left, right = begin - 1, end
        count = end - begin
        stats["candidate_gap_runs"] += 1
        if count > settings.max_missing_samples:
            stats["skipped_sample_bound"] += 1
            if status_column:
                frame.loc[pos[begin:end], status_column] = "interpolation_sample_bound"
            continue
        if left < 0 or right >= len(pos):
            stats["skipped_untrusted_bounds"] += 1
            if status_column:
                frame.loc[pos[begin:end], status_column] = "interpolation_untrusted_bounds"
            continue
        if not (trusted[left] and trusted[right]):
            stats["skipped_untrusted_bounds"] += 1
            if status_column:
                frame.loc[pos[begin:end], status_column] = "interpolation_untrusted_bounds"
            continue
        duration = times[right] - times[left]
        if not np.isfinite(duration) or duration <= 0 or duration > max_gap_s:
            stats["skipped_duration_bound"] += 1
            if status_column:
                frame.loc[pos[begin:end], status_column] = "interpolation_duration_bound"
            continue
        if np.any(~np.isfinite(times[begin:end])):
            stats["skipped_invalid_target_time"] += 1
            if status_column:
                frame.loc[pos[begin:end], status_column] = "interpolation_invalid_target_time"
            continue
        target_times = times[begin:end]
        if method == "pchip":
            anchor_positions = np.asarray([left - 1, left, right, right + 1], dtype=np.int64)
            if (left < 1 or right + 1 >= len(pos) or not trusted[anchor_positions].all()):
                stats["skipped_pchip_insufficient_anchors"] += 1
                if status_column:
                    frame.loc[pos[begin:end], status_column] = "pchip_insufficient_anchors"
                continue
            anchor_times = times[anchor_positions]
            anchor_values = values[anchor_positions]
            local_times = times[left - 1:right + 2]
            local_deltas = np.diff(local_times)
            if (not np.isfinite(anchor_times).all() or np.any(np.diff(anchor_times) <= 0)
                    or pchip_support_gap is None or not np.isfinite(local_deltas).all()
                    or np.any(local_deltas <= 0) or np.any(local_deltas > pchip_support_gap)):
                stats["skipped_pchip_discontinuous_support"] += 1
                if status_column:
                    frame.loc[pos[begin:end], status_column] = "pchip_discontinuous_support"
                continue
            replacement = PchipInterpolator(anchor_times, anchor_values, extrapolate=False)(target_times)
        else:
            replacement = np.interp(target_times, [times[left], times[right]], [values[left], values[right]])
        frame.loc[pos[begin:end], column] = replacement
        frame.loc[pos[begin:end], flag_column] = True
        if status_column:
            frame.loc[pos[begin:end], status_column] = f"interpolated_{method}"
        if status_column:
            anchor_offsets = (left - 1, left, right, right + 1) if method == "pchip" else (left, right)
            target_positions = pos[begin:end]
            anchor_rows = pos[np.asarray(anchor_offsets, dtype=np.int64)]
            stats["insertion_provenance"].append({
                "method": method,
                "replacement_source": "derived_interpolation; target rows are not observed source measurements",
                "target_observation_ids": (
                    [_audit_scalar(value) for value in frame.loc[target_positions, observation_column].tolist()]
                    if observation_column else target_positions.astype(int).tolist()
                ),
                "target_times_s": [float(value) for value in target_times],
                "anchor_observation_ids": (
                    [_audit_scalar(value) for value in frame.loc[anchor_rows, observation_column].tolist()]
                    if observation_column else anchor_rows.astype(int).tolist()
                ),
                "anchor_times_s": [float(value) for value in times[np.asarray(anchor_offsets, dtype=np.int64)]],
            })
        stats["interpolated_gap_runs"] += 1
        stats["interpolated_samples"] += count
    return stats


def _apply_lowpass(
    frame: pd.DataFrame,
    column: str,
    valid_column: str,
    stream_keys: list[str],
    settings: ProcessingConfig,
    logs: list[str],
) -> None:
    assert settings.lowpass_cutoff_hz is not None
    for group_key, positions in _groups(frame, stream_keys):
        sub = frame.loc[positions]
        if "segment_id" in sub:
            segment_groups = sub.groupby("segment_id", dropna=False, sort=False).indices
        else:
            segment_groups = {None: np.arange(len(sub))}
        for segment_id, local_positions in segment_groups.items():
            base = np.asarray(positions)[np.asarray(local_positions, dtype=np.int64)]
            finite = frame.loc[base, valid_column].fillna(False).to_numpy(dtype=bool)
            values = pd.to_numeric(frame.loc[base, column], errors="coerce").astype("float64").to_numpy()
            times = frame.loc[base, "time_s"].astype("float64").to_numpy()
            finite &= np.isfinite(values) & np.isfinite(times)
            for run in _contiguous_runs(finite):
                if len(run) < max(8, 2 * settings.lowpass_order + 3):
                    logs.append(f"lowpass_skipped_short_run:{column}:{group_key}:{segment_id}")
                    continue
                dt = np.diff(times[run])
                if np.any(dt <= 0):
                    logs.append(f"lowpass_skipped_nonpositive_dt:{column}:{group_key}:{segment_id}")
                    continue
                sample_rate = 1.0 / float(np.median(dt))
                if settings.lowpass_cutoff_hz >= sample_rate / 2:
                    logs.append(f"lowpass_skipped_cutoff_at_or_above_nyquist:{column}:{group_key}:{segment_id}")
                    continue
                sos = butter(settings.lowpass_order, settings.lowpass_cutoff_hz,
                             btype="lowpass", fs=sample_rate, output="sos")
                try:
                    filtered = sosfiltfilt(sos, values[run])
                except ValueError:
                    logs.append(f"lowpass_skipped_filter_length:{column}:{group_key}:{segment_id}")
                    continue
                frame.loc[base[run], column] = filtered


def _contiguous_runs(mask: np.ndarray) -> list[np.ndarray]:
    indices = np.flatnonzero(mask)
    if not len(indices):
        return []
    split_at = np.flatnonzero(np.diff(indices) != 1) + 1
    return [chunk for chunk in np.split(indices, split_at) if len(chunk)]


def time_weighted_mean(
    times: np.ndarray,
    values: np.ndarray,
    valid: np.ndarray,
    max_gap_s: float | None,
    *,
    window: tuple[float, float] | None = None,
) -> tuple[float | None, float]:
    """Integrate linear segments between adjacent original valid samples.

    valid must retain the full ordered row grid and mark only original observed
    values as true; callers must not drop invalid/interpolated rows before calling.
    The caller scopes arrays to one stream and, where applicable, one segment/trial.
    A requested window clips each supported interval by linear interpolation at its
    boundaries. There is no extrapolation and no support across long gaps.
    Returns a pair of mean and effective_duration_s; no support is represented as (None, 0).
    """
    try:
        time_values = np.asarray(times, dtype=np.float64)
        signal_values = np.asarray(values, dtype=np.float64)
        valid_mask = pd.Series(valid).astype("boolean").fillna(False).to_numpy(dtype=bool)
    except (TypeError, ValueError) as exc:
        raise ProcessingError(
            "Time-weighted mean inputs must be numeric arrays and nullable booleans"
        ) from exc
    if time_values.ndim != 1 or signal_values.ndim != 1 or valid_mask.ndim != 1:
        raise ProcessingError("Time-weighted mean inputs must be one-dimensional")
    if not (len(time_values) == len(signal_values) == len(valid_mask)):
        raise ProcessingError("Time-weighted mean inputs must have equal lengths")
    if max_gap_s is None:
        return None, 0.0
    if not np.isfinite(max_gap_s) or max_gap_s <= 0:
        raise ProcessingError("max_gap_s must be finite and positive")
    if window is None:
        window_start, window_end = -np.inf, np.inf
    else:
        try:
            window_start, window_end = float(window[0]), float(window[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise ProcessingError("Time-weighted mean window must contain two finite seconds") from exc
        if not np.isfinite(window_start) or not np.isfinite(window_end) or window_end <= window_start:
            raise ProcessingError("Time-weighted mean window must have finite increasing boundaries")

    area = 0.0
    duration = 0.0
    for right_idx in range(1, len(time_values)):
        left_idx = right_idx - 1
        if not (valid_mask[left_idx] and valid_mask[right_idx]):
            continue
        t0, t1 = time_values[left_idx], time_values[right_idx]
        y0, y1 = signal_values[left_idx], signal_values[right_idx]
        delta = t1 - t0
        if not (np.isfinite(t0) and np.isfinite(t1) and np.isfinite(y0) and np.isfinite(y1)):
            continue
        if delta <= 0 or delta > max_gap_s:
            continue
        clipped_start = max(t0, window_start)
        clipped_end = min(t1, window_end)
        if clipped_end <= clipped_start:
            continue
        start_fraction = (clipped_start - t0) / delta
        end_fraction = (clipped_end - t0) / delta
        y_start = y0 + (y1 - y0) * start_fraction
        y_end = y0 + (y1 - y0) * end_fraction
        interval = clipped_end - clipped_start
        area += interval * (y_start + y_end) / 2.0
        duration += interval
    if duration <= 0:
        return None, 0.0
    return float(area / duration), float(duration)


def _apply_pupil_baseline(
    frame: pd.DataFrame,
    stream_keys: list[str],
    settings: ProcessingConfig,
    memberships: pd.DataFrame,
    segments: pd.DataFrame,
    logs: list[str],
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    """Keep the global pupil branch intact and emit label-specific values relationally."""
    records: list[dict[str, Any]] = []
    derivations: list[dict[str, Any]] = []
    for eye, valid_column in (("left", "analysis_pupil_left_valid"),
                              ("right", "analysis_pupil_right_valid")):
        source_column = f"pupil_{eye}"
        interpolated_column = f"{source_column}_interpolated"
        output_column = f"pupil_{eye}_baseline_corrected"
        frame[output_column] = pd.NA
        if settings.pupil_baseline_window_s is not None:
            start, end = map(float, settings.pupil_baseline_window_s)
            if end - start < settings.baseline_min_length_s:
                raise ProcessingError("Selected baseline window is shorter than the configured minimum")
            for group_key, positions in _groups(frame, stream_keys):
                group = frame.loc[positions]
                baseline, observed_duration, observed_n, interpolated_n = _partitioned_baseline(
                    group, source_column, valid_column, interpolated_column, (start, end), settings
                )
                coverage = min(1.0, observed_duration / (end - start))
                reason = None if baseline is not None else "no_original_time_support"
                if reason is None and coverage < settings.baseline_min_coverage:
                    reason = "insufficient_baseline_coverage"
                key_tuple = group_key if isinstance(group_key, tuple) else (group_key,)
                stream_ids = {key: _audit_scalar(value) for key, value in zip(stream_keys, key_tuple)}
                records.append({
                    **stream_ids, "segment_id": None, "segment_instance_id": None,
                    "eye": eye, "window_s": [start, end], "baseline_value": baseline,
                    "effective_duration_s": float(observed_duration), "coverage": float(coverage),
                    "n_observed": observed_n, "n_interpolated": interpolated_n,
                    "status": "available" if reason is None else "unavailable", "reason_code": reason,
                    "parameter_ref": _baseline_parameter_ref(eye, (start, end), settings),
                })
                if reason is None:
                    frame.loc[group.index, output_column] = pd.to_numeric(group[source_column], errors="coerce") - baseline
                else:
                    logs.append(f"baseline_unavailable:{eye}:{group_key}:coverage={coverage:.3f}:reason={reason}")

    if memberships.empty:
        return records, empty_table(DERIVATION_COLUMNS)
    segment_rows = {
        str(row.get("segment_instance_id")): row
        for row in segments.to_dict("records")
        if not pd.isna(row.get("segment_instance_id"))
    }
    for membership in memberships.to_dict("records"):
        segment = segment_rows.get(str(membership["segment_instance_id"]), {})
        if not pd.isna(segment.get("baseline_start_s")) and not pd.isna(segment.get("baseline_end_s")):
            window = (float(segment["baseline_start_s"]), float(segment["baseline_end_s"]))
        else:
            window = settings.pupil_baseline_window_s
        if window is None:
            continue
        start, end = map(float, window)
        if end - start < settings.baseline_min_length_s:
            raise ProcessingError("Selected label baseline window is shorter than the configured minimum")
        target_ids = set(membership.get("support_observation_ids") or ())
        scope_mask = pd.Series(True, index=frame.index)
        for key in ("subject_id", "session_id", "interaction_id", "timebase_id", "trial", "condition", "stimulus_id"):
            value = membership.get(key)
            if key == "timebase_id" and str(value or "") in {"unconfirmed", "legacy_unconfirmed"}:
                continue
            if key in frame and value is not None and not pd.isna(value):
                scope_mask &= frame[key].astype("string").eq(str(value)).fillna(False)
        scoped_all = frame.loc[scope_mask]
        targets_all = scoped_all.loc[scoped_all["observation_id"].astype(str).isin(target_ids)]
        if "source_id" in targets_all:
            target_groups = list(targets_all.groupby("source_id", dropna=False, sort=False))
        else:
            target_groups = [(None, targets_all)]
        for source_id, targets in target_groups:
            scoped = scoped_all
            if source_id is not None and "source_id" in scoped:
                scoped = scoped.loc[scoped["source_id"].astype("string").eq(str(source_id)).fillna(False)]
            for eye, valid_column in (("left", "analysis_pupil_left_valid"),
                                      ("right", "analysis_pupil_right_valid")):
                source_column = f"pupil_{eye}"
                interpolated_column = f"{source_column}_interpolated"
                baseline, observed_duration, _, _ = _partitioned_baseline(
                    scoped, source_column, valid_column, interpolated_column, (start, end), settings
                )
                coverage = min(1.0, observed_duration / (end - start))
                reason = None if baseline is not None else "no_original_time_support"
                if reason is None and coverage < settings.baseline_min_coverage:
                    reason = "insufficient_baseline_coverage"
                parameter_ref = _baseline_parameter_ref(eye, (start, end), settings)
                for _, sample in targets.iterrows():
                    raw_value = pd.to_numeric(pd.Series([sample.get(source_column)]), errors="coerce").iloc[0]
                    sample_reason = reason
                    if pd.isna(raw_value):
                        sample_reason = "missing_original_pupil_value"
                    derived_value = None if sample_reason is not None else float(raw_value) - float(baseline)
                    derivations.append({
                        "analysis_stream_id": f"pupil_{eye}",
                        "segment_id": membership["segment_id"],
                        "segment_instance_id": membership["segment_instance_id"],
                        "observation_id": sample["observation_id"], "eye": eye,
                        "raw_value": None if pd.isna(raw_value) else float(raw_value),
                        "baseline_value": None if sample_reason is not None else float(baseline),
                        "derived_value": derived_value, "parameter_ref": parameter_ref,
                        "status": "available" if sample_reason is None else "unavailable",
                        "reason_code": sample_reason,
                    })
    result = pd.DataFrame.from_records(derivations, columns=DERIVATION_COLUMNS)
    return records, result if not result.empty else empty_table(DERIVATION_COLUMNS)


def _partitioned_baseline(
    frame: pd.DataFrame,
    source_column: str,
    valid_column: str,
    interpolated_column: str,
    window: tuple[float, float],
    settings: ProcessingConfig,
) -> tuple[float | None, float, int, int]:
    if frame.empty:
        return None, 0.0, 0, 0
    data = frame.copy(deep=False)
    times = pd.to_numeric(data["time_s"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    values = pd.to_numeric(data[source_column], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    valid = data[valid_column].fillna(False).to_numpy(dtype=bool)
    interpolated = data[interpolated_column].fillna(False).to_numpy(dtype=bool)
    original_valid = valid & ~interpolated & np.isfinite(times) & np.isfinite(values)
    within = np.isfinite(times) & (times >= window[0]) & (times < window[1])
    observed_n = int(np.sum(original_valid & within))
    interpolated_n = int(np.sum(valid & interpolated & np.isfinite(times) & np.isfinite(values) & within))
    if "processing_partition_id" in data:
        partition_positions = data.groupby("processing_partition_id", dropna=False, sort=False).indices.values()
    else:
        partition_positions = (np.arange(len(data)),)
    weighted_total = 0.0
    duration_total = 0.0
    for positions in partition_positions:
        positions = np.asarray(positions, dtype=np.int64)
        partition_times = times[positions]
        gap = _support_gap_threshold(partition_times, settings)
        mean, duration = time_weighted_mean(
            partition_times, values[positions], original_valid[positions], gap, window=window
        )
        if mean is not None and duration > 0:
            weighted_total += mean * duration
            duration_total += duration
    baseline = weighted_total / duration_total if duration_total > 0 else None
    return baseline, float(duration_total), observed_n, interpolated_n


def _baseline_parameter_ref(eye: str, window: tuple[float, float], settings: ProcessingConfig) -> str:
    payload = {
        "schema": "pupil-baseline-v1", "eye": eye,
        "window_s": [float(window[0]).hex(), float(window[1]).hex()],
        "minimum_coverage": float(settings.baseline_min_coverage).hex(),
        "minimum_length_s": float(settings.baseline_min_length_s).hex(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return "parameter:baseline:" + digest


def _audit_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _quality_summary(frame: pd.DataFrame, keys: list[str], settings: ProcessingConfig) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for group_key, positions in _groups(frame, keys):
        group = frame.loc[positions]
        times = group["time_s"].astype("float64").to_numpy()
        time_valid = np.isfinite(times)
        time_diffs = np.diff(times) if len(times) > 1 else np.array([], dtype=float)
        positive = time_diffs[np.isfinite(time_diffs) & (time_diffs > 0)]
        gap_threshold = _support_gap_threshold(times, settings)
        group_id = {key: value for key, value in zip(keys, group_key if isinstance(group_key, tuple) else (group_key,))}
        for signal, valid_column, interp_column, source_column, mad_column, padded_column, seed_column in (
            ("gaze", "analysis_gaze_valid", "gaze_interpolated", "source_gaze_valid", None, None, None),
            ("pupil_left", "analysis_pupil_left_valid", "pupil_left_interpolated", "source_pupil_left_valid",
             "pupil_left_speed_mad_rejected", "pupil_left_blink_loss_padded", "pupil_left_blink_loss_seed"),
            ("pupil_right", "analysis_pupil_right_valid", "pupil_right_interpolated", "source_pupil_right_valid",
             "pupil_right_speed_mad_rejected", "pupil_right_blink_loss_padded", "pupil_right_blink_loss_seed"),
        ):
            valid = group[valid_column].fillna(False).to_numpy(dtype=bool) & time_valid
            interpolated = group[interp_column].to_numpy(dtype=bool) & time_valid
            observed = valid & ~interpolated
            support = _valid_support_duration(times, valid, gap_threshold)
            records.append({
                **group_id,
                "signal": signal,
                "n_rows": int(len(group)),
                "n_time_valid": int(time_valid.sum()),
                "n_source_valid": int(group[source_column].eq(True).sum()),
                "n_source_invalid": int(group[source_column].eq(False).sum()),
                "n_source_validity_unknown": int(group[source_column].isna().sum()),
                "n_observed": int(observed.sum()),
                "n_interpolated": int(interpolated.sum()),
                "n_invalid_or_missing": int((~valid & time_valid).sum()),
                "n_speed_mad_rejected": int(group[mad_column].fillna(False).sum()) if mad_column else 0,
                "n_blink_loss_seed": int(group[seed_column].fillna(False).sum()) if seed_column else 0,
                "n_blink_loss_padded": int(group[padded_column].fillna(False).sum()) if padded_column else 0,
                "effective_duration_s": support,
                "support_gap_threshold_s": gap_threshold,
                "largest_positive_gap_s": float(np.max(positive)) if len(positive) else None,
            })
    return pd.DataFrame.from_records(records)


def _pupil_preprocessing_audit(settings: ProcessingConfig, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "pupil-preprocessing-audit/v1",
        "enabled": bool(settings.pupil_speed_mad_enabled or settings.pupil_blink_padding_enabled
                         or settings.interpolate_pupil),
        "speed_mad": {
            "enabled": settings.pupil_speed_mad_enabled,
            "n": settings.pupil_speed_mad_n,
            "threshold_formula": "median(speed) + n * median(abs(speed - median(speed)))",
            "rejection_comparison": "speed > threshold",
            "speed_formula": "max(abs(previous pupil delta / delta_s), abs(next pupil delta / delta_s))",
        },
        "blink_loss_padding": {
            "enabled": settings.pupil_blink_padding_enabled,
            "before_s": settings.pupil_blink_padding_before_s,
            "after_s": settings.pupil_blink_padding_after_s,
            "seed_rule": "nonfinite or explicitly invalid pupil sample, explicit per-eye blink/closed marker, or zero with unknown source-validity",
        },
        "interpolation": {
            "enabled": settings.interpolate_pupil,
            "method": settings.pupil_interpolation_method,
            "max_gap_s": settings.pupil_max_gap_s,
            "max_missing_samples": settings.max_missing_samples,
            "pchip_valid_anchors_each_side": 2,
            "pchip_extrapolation": False,
        },
        "source_references": [dict(item) for item in PUPIL_PREPROCESSING_SOURCE_REFS],
        "stream_audit": records,
    }


def _valid_support_duration(times: np.ndarray, valid: np.ndarray, max_gap_s: float | None) -> float | None:
    if max_gap_s is None:
        return None
    supports = []
    for idx in range(1, len(times)):
        delta = times[idx] - times[idx - 1]
        if valid[idx - 1] and valid[idx] and np.isfinite(delta) and 0 < delta <= max_gap_s:
            supports.append(float(delta))
    return float(sum(supports))


def _support_gap_threshold(times: np.ndarray, settings: ProcessingConfig) -> float | None:
    if settings.support_gap_max_s is not None:
        return settings.support_gap_max_s
    diffs = np.diff(np.asarray(times, dtype=float))
    positive = diffs[np.isfinite(diffs) & (diffs > 0)]
    if not len(positive):
        return None
    return settings.support_gap_factor * float(np.median(positive))


def _cancelled(frame: pd.DataFrame, config: ProcessingConfig, metadata: dict[str, Any],
               logs: list[str], message: str) -> ProcessingResult:
    return ProcessingResult(
        signals=frame,
        quality=pd.DataFrame(),
        audit={"contract_version": "1.0.0", "processing_config": config.to_dict(),
               "source_metadata": metadata, "execution_status": "cancelled",
               "processing_schema_version": "1.3.0",
               "pupil_preprocessing": _pupil_preprocessing_audit(config, [])},
        execution_status="cancelled",
        logs=logs + [message],
    )
