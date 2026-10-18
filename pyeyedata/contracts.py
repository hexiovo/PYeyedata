"""Versioned, fail-closed contracts shared by the analysis engines and desktop app."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Literal

import pandas as pd

from .aoi_geometry import AOI_DOMAIN_SCHEMA_VERSION

CONTRACT_VERSION = "1.3.0"
SUPPORTED_CONTRACT_VERSIONS = frozenset({"1.0.0", "1.1.0", "1.2.0", CONTRACT_VERSION})

SIGNAL_REQUIRED_COLUMNS = (
    "subject_id", "session_id", "time_s", "time_raw", "original_index",
)
SIGNAL_OPTIONAL_COLUMNS = (
    "interaction_id", "stimulus_id", "condition", "trial",
    "gaze_x", "gaze_y", "gaze_z", "pupil_left", "pupil_right",
    "gaze_valid", "pupil_left_valid", "pupil_right_valid", "device_id", "sample_index",
    "confidence_left", "confidence_right", "vendor_event", "eyelid", "target_x", "target_y",
    "timebase_id",
    "target_z", "gaze_left_x", "gaze_left_y", "gaze_left_z", "gaze_right_x",
    "gaze_right_y", "gaze_right_z", "gaze_left_valid", "gaze_right_valid",
    "eyelid_closure_left", "eyelid_closure_right", "blink_left_event",
    "blink_right_event", "microsaccade_event", "tracking_state", "tracking_valid",
    "calibration_error", "target_vx", "target_vy", "target_vz", "target_azimuth",
    "target_elevation", "target_distance", "target_valid", "cue_id", "cue_start",
    "cue_end",
    # Per-eye gaze rays retain each representation instead of overloading gaze_xyz.
    "gaze_left_direction_x", "gaze_left_direction_y", "gaze_left_direction_z",
    "gaze_right_direction_x", "gaze_right_direction_y", "gaze_right_direction_z",
    "gaze_left_origin_x", "gaze_left_origin_y", "gaze_left_origin_z",
    "gaze_right_origin_x", "gaze_right_origin_y", "gaze_right_origin_z",
    "eyelid_closed_left", "eyelid_closed_right", "eyelid_source_left", "eyelid_source_right",
    "blink_source_left", "blink_source_right",
    # Explicit target/reference identities and trajectories.
    "target_id", "reference_id", "reference_x", "reference_y", "reference_z",
    "reference_vx", "reference_vy", "reference_vz", "reference_valid",
    "reading_direction", "reading_scope_id",
    # Event-time annotations are kept in source form and normalized to *_s columns.
    "cue_time", "cue_type", "reference_time", "reference_event_id",
    # Per-row acquisition/frame provenance; global units and clock precision live in metadata.
    "clock_id", "frame_id", "frame_number", "timecode", "device_validation_status",
    "device_validation_method", "device_validation_evidence",
)
SIGNAL_DERIVED_COLUMNS = (
    "cue_time_s", "cue_start_s", "cue_end_s", "reference_time_s",
)
SIGNAL_TEMPLATE_COLUMNS = SIGNAL_REQUIRED_COLUMNS + SIGNAL_OPTIONAL_COLUMNS
SEGMENT_REQUIRED_COLUMNS = (
    "subject_id", "session_id", "segment_id", "segment_name", "start_s", "end_s",
)
LEGACY_SEGMENT_REQUIRED_COLUMNS = ("subject_id", "session_id", "segment_id", "start_s", "end_s")
SEGMENT_OPTIONAL_COLUMNS = (
    "interaction_id", "name", "condition", "trial", "stimulus_id", "scope_kind",
    "segment_instance_id", "segment_kind", "timebase_id", "scope_definition_ref",
)
AOI_REQUIRED_COLUMNS = ("aoi_id", "stimulus_id", "shape")
AOI_OPTIONAL_COLUMNS = (
    "xmin", "ymin", "xmax", "ymax", "vertices", "unit", "coordinate_frame",
    "overlap_policy", "priority", "aoi_name", "aoi_name_status",
    "aoi_geometry_version", "aoi_domain_schema_version", "aoi_boundary_policy",
    "aoi_input_closure", "aoi_geometry_fingerprint", "aoi_source",
    "aoi_geometry_raw", "aoi_geometry_audit",
)
METRIC_COLUMNS = (
    "run_id", "subject_id", "session_id", "interaction_id", "pair_id",
    "segment_id", "trial", "condition", "metric_id", "value", "unit",
    "status", "reason_code", "n_observed", "n_interpolated",
    "effective_duration_s", "algorithm_version", "parameter_ref",
    "analysis_stream_id", "eye_label", "stream_parent_refs",
    "segment_instance_id", "physical_partition_id", "support_ref", "dependence_ref",
    "n_window_observed", "n_support_observed",
)
LEGACY_METRIC_COLUMNS = METRIC_COLUMNS[:18]
AVAILABILITY_COLUMNS = (
    "metric_id", "available", "status", "reason_code", "reason",
    "required_fields", "required_metadata",
)
EVENT_COLUMNS = (
    "run_id", "subject_id", "session_id", "segment_id", "event_id",
    "event_type", "start_s", "end_s", "duration_s", "status",
    "reason_code", "n_observed", "n_interpolated", "algorithm_version",
    "parameter_ref",
)
DESIGN_ID_COLUMNS = (
    "subject_column", "session_column", "interaction_column", "team_column",
    "analysis_unit_column", "group_column", "condition_column", "trial_column",
)
EXECUTION_STATUSES = frozenset({"complete", "partial", "cancelled", "failed"})


class ContractError(ValueError):
    """Raised when data or results violate a public contract."""


class AnalysisError(RuntimeError):
    """A failed analysis; callers must report it as failed, never as complete."""


class AvailabilityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    EXPERIMENTAL = "experimental"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ParameterSet:
    """A versioned parameter snapshot. The same values must reproduce a run."""

    parameter_id: str
    version: str
    values: dict[str, Any]
    units: dict[str, str] = field(default_factory=dict)
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.parameter_id.strip() or not self.version.strip():
            raise ContractError("ParameterSet requires a nonempty id and version")
        _assert_json_safe(self.values, "ParameterSet.values")
        _assert_json_safe(self.units, "ParameterSet.units")
        if any(not isinstance(ref, str) or not ref.strip() for ref in self.source_refs):
            raise ContractError("ParameterSet.source_refs must contain nonempty strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_id": self.parameter_id,
            "version": self.version,
            "values": dict(self.values),
            "units": dict(self.units),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class EyeCoordinateMetadata:
    """Units and frames for the three distinct per-eye gaze representations."""

    point_unit: str = "unknown"
    point_frame: str = "unknown"
    direction_unit: str = "unknown"
    direction_frame: str = "unknown"
    origin_unit: str = "unknown"
    origin_frame: str = "unknown"

    def __post_init__(self) -> None:
        for name in ("point_unit", "point_frame", "direction_unit", "direction_frame", "origin_unit", "origin_frame"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"EyeCoordinateMetadata.{name} must be a nonempty string; use 'unknown'")

    def to_dict(self) -> dict[str, str]:
        return {
            "point_unit": self.point_unit,
            "point_frame": self.point_frame,
            "direction_unit": self.direction_unit,
            "direction_frame": self.direction_frame,
            "origin_unit": self.origin_unit,
            "origin_frame": self.origin_frame,
        }


@dataclass(frozen=True)
class CoordinateMetadata:
    """User-confirmed temporal/spatial semantics; unknown values are never guessed.

    Device validation references record the source for a validation declaration; they do
    not certify device accuracy or scientific validity.
    """

    time_unit: Literal["s", "ms", "datetime", "frame", "unknown"] = "unknown"
    time_origin: str | None = None
    timezone: str | None = None
    clock_source: str | None = None
    sync_confirmed: bool = False
    clock_offsets_s: dict[str, float] = field(default_factory=dict)
    time_precision_s: float | None = None
    frame_id: str | None = None
    frame_rate_hz: float | None = None
    timecode_format: str | None = None
    coordinate_unit: str = "unknown"
    coordinate_frame: str = "unknown"
    coordinate_representation: Literal[
        "2d_point", "3d_point", "direction", "origin", "unknown"
    ] = "unknown"
    left_eye: EyeCoordinateMetadata = field(default_factory=EyeCoordinateMetadata)
    right_eye: EyeCoordinateMetadata = field(default_factory=EyeCoordinateMetadata)
    target_coordinate_unit: str = "unknown"
    target_coordinate_frame: str = "unknown"
    reference_coordinate_unit: str = "unknown"
    reference_coordinate_frame: str = "unknown"
    geometry: dict[str, Any] = field(default_factory=dict)
    device_verified: bool = False
    device_validation_status: Literal["unknown", "unverified", "verified"] = "unknown"
    device_validation_method: str | None = None
    device_validation_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.time_unit, str):
            raise ContractError("time_unit must be a string")
        if self.time_unit not in {"s", "ms", "datetime", "frame", "unknown"}:
            raise ContractError(f"Unsupported time_unit: {self.time_unit}")
        if self.time_unit == "frame" and (
            self.frame_rate_hz is None or isinstance(self.frame_rate_hz, bool)
            or not isinstance(self.frame_rate_hz, (int, float))
            or not math.isfinite(float(self.frame_rate_hz)) or self.frame_rate_hz <= 0
        ):
            raise ContractError("frame timestamps require an explicit positive frame_rate_hz")
        if self.frame_rate_hz is not None and (
            isinstance(self.frame_rate_hz, bool) or not isinstance(self.frame_rate_hz, (int, float))
            or not math.isfinite(float(self.frame_rate_hz)) or self.frame_rate_hz <= 0
        ):
            raise ContractError("frame_rate_hz must be finite and positive")
        if self.time_precision_s is not None and (
            isinstance(self.time_precision_s, bool) or not isinstance(self.time_precision_s, (int, float))
            or not math.isfinite(float(self.time_precision_s)) or self.time_precision_s <= 0
        ):
            raise ContractError("time_precision_s must be finite and positive")
        if not isinstance(self.coordinate_representation, str) or self.coordinate_representation not in {
            "2d_point", "3d_point", "direction", "origin", "unknown"
        }:
            raise ContractError(
                f"Unsupported coordinate_representation: {self.coordinate_representation}"
            )
        for name in ("coordinate_unit", "coordinate_frame", "target_coordinate_unit",
                     "target_coordinate_frame", "reference_coordinate_unit", "reference_coordinate_frame"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"CoordinateMetadata.{name} must be nonempty; use 'unknown'")
        for name in ("time_origin", "timezone", "clock_source", "frame_id", "timecode_format"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ContractError(f"CoordinateMetadata.{name} must be a nonempty string when supplied")
        if not isinstance(self.left_eye, EyeCoordinateMetadata) or not isinstance(self.right_eye, EyeCoordinateMetadata):
            raise ContractError("left_eye and right_eye must use EyeCoordinateMetadata")
        if not isinstance(self.sync_confirmed, bool):
            raise ContractError("sync_confirmed must be a boolean")
        if not isinstance(self.device_verified, bool):
            raise ContractError("device_verified must be a boolean")
        if not isinstance(self.device_validation_status, str):
            raise ContractError("device_validation_status must be a string")
        if self.device_validation_status not in {"unknown", "unverified", "verified"}:
            raise ContractError("Unsupported device_validation_status")
        if self.device_validation_method is not None and (
            not isinstance(self.device_validation_method, str) or not self.device_validation_method.strip()
        ):
            raise ContractError("device_validation_method must be a nonempty string when supplied")
        if not isinstance(self.device_validation_evidence, (tuple, list)) or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.device_validation_evidence
        ):
            raise ContractError("device_validation_evidence must contain nonempty references")
        if isinstance(self.device_validation_evidence, list):
            object.__setattr__(self, "device_validation_evidence", tuple(self.device_validation_evidence))
        if self.device_validation_status == "verified" and (
            not self.device_validation_method or not self.device_validation_evidence
        ):
            raise ContractError("verified device status requires a method and evidence references")
        if self.device_verified and (
            self.device_validation_status != "verified"
            or not self.device_validation_method or not self.device_validation_method.strip()
            or not self.device_validation_evidence
        ):
            raise ContractError("device_verified requires verified status, a method, and evidence references")
        if not isinstance(self.clock_offsets_s, dict):
            raise ContractError("clock_offsets_s must be a mapping of clock ids to offsets")
        for clock, offset in self.clock_offsets_s.items():
            if not isinstance(clock, str) or not clock.strip():
                raise ContractError("Clock offset keys must name an explicit clock source")
            if not isinstance(offset, (int, float)) or isinstance(offset, bool) or not math.isfinite(float(offset)):
                raise ContractError(f"Clock offset for {clock!r} must be finite numeric seconds")
        if not isinstance(self.geometry, dict):
            raise ContractError("CoordinateMetadata.geometry must be an object")
        _assert_json_safe(self.geometry, "CoordinateMetadata.geometry")

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_unit": self.time_unit,
            "time_origin": self.time_origin,
            "timezone": self.timezone,
            "clock_source": self.clock_source,
            "sync_confirmed": self.sync_confirmed,
            "clock_offsets_s": dict(self.clock_offsets_s),
            "time_precision_s": self.time_precision_s,
            "frame_id": self.frame_id,
            "frame_rate_hz": self.frame_rate_hz,
            "timecode_format": self.timecode_format,
            "coordinate_unit": self.coordinate_unit,
            "coordinate_frame": self.coordinate_frame,
            "coordinate_representation": self.coordinate_representation,
            "left_eye": self.left_eye.to_dict(),
            "right_eye": self.right_eye.to_dict(),
            "target_coordinate_unit": self.target_coordinate_unit,
            "target_coordinate_frame": self.target_coordinate_frame,
            "reference_coordinate_unit": self.reference_coordinate_unit,
            "reference_coordinate_frame": self.reference_coordinate_frame,
            "geometry": dict(self.geometry),
            "device_verified": self.device_verified,
            "device_validation_status": self.device_validation_status,
            "device_validation_method": self.device_validation_method,
            "device_validation_evidence": list(self.device_validation_evidence),
        }


@dataclass(frozen=True)
class MetricSpec:
    """Registry record for one output metric; validation is opt-in and explicit."""

    metric_id: str
    label: str
    category: str
    unit: str
    algorithm_id: str
    algorithm_version: str
    requires_fields: tuple[str, ...] = ()
    requires_metadata: tuple[str, ...] = ()
    analysis_modes: tuple[str, ...] = ("individual",)
    reference_ids: tuple[str, ...] = ()
    validated: bool = False
    experimental: bool = False

    def __post_init__(self) -> None:
        for name in ("metric_id", "label", "category", "algorithm_id", "algorithm_version"):
            if not getattr(self, name).strip():
                raise ContractError(f"MetricSpec.{name} must be nonempty")
        if not self.unit.strip():
            raise ContractError("MetricSpec.unit must be explicit; use 'unknown' when unresolved")


@dataclass(frozen=True)
class MetricAvailability:
    metric_id: str
    available: bool
    status: AvailabilityStatus
    reason_code: str
    reason: str
    required_fields: tuple[str, ...] = ()
    required_metadata: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            status = AvailabilityStatus(self.status)
        except ValueError as exc:
            raise ContractError(f"Unsupported metric availability status: {self.status}") from exc
        object.__setattr__(self, "status", status)
        if not self.metric_id.strip() or not self.reason_code.strip() or not self.reason.strip():
            raise ContractError("MetricAvailability requires a metric id and explicit status reason")
        if self.available != (status is AvailabilityStatus.AVAILABLE):
            raise ContractError("Only AVAILABLE metrics may be marked available")

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "available": self.available,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "required_fields": list(self.required_fields),
            "required_metadata": list(self.required_metadata),
        }


@dataclass(frozen=True)
class ResultRow:
    """One auditable metric result in the stable long-table schema."""

    run_id: str
    subject_id: Any
    session_id: Any
    interaction_id: Any
    pair_id: Any
    segment_id: Any
    trial: Any
    condition: Any
    metric_id: str
    value: float | int | None
    unit: str | None
    status: str
    reason_code: str | None
    n_observed: int
    n_interpolated: int
    effective_duration_s: float | None
    algorithm_version: str
    parameter_ref: str
    analysis_stream_id: str | None = None
    eye_label: str | None = None
    stream_parent_refs: tuple[str, ...] | None = None
    segment_instance_id: str | None = None
    physical_partition_id: str | None = None
    support_ref: str | None = None
    dependence_ref: tuple[str, ...] | str | None = None
    n_window_observed: int | None = None
    n_support_observed: int | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "metric_id", "status", "algorithm_version", "parameter_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"ResultRow.{name} must be a nonempty string")
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(float(self.value)):
                raise ContractError("ResultRow.value must be a finite number or None")
            if not isinstance(self.unit, str) or not self.unit.strip():
                raise ContractError("A computed ResultRow.value requires an explicit unit")
        elif not self.reason_code:
            raise ContractError("A ResultRow without a value requires a reason_code")
        if self.value is None and self.status == "complete":
            raise ContractError("A ResultRow with status complete must contain a value")
        if isinstance(self.n_observed, bool) or not isinstance(self.n_observed, int) or self.n_observed < 0:
            raise ContractError("ResultRow.n_observed must be a nonnegative integer")
        if isinstance(self.n_interpolated, bool) or not isinstance(self.n_interpolated, int) or self.n_interpolated < 0:
            raise ContractError("ResultRow.n_interpolated must be a nonnegative integer")
        if self.effective_duration_s is not None and (
            isinstance(self.effective_duration_s, bool)
            or not isinstance(self.effective_duration_s, (int, float))
            or not math.isfinite(float(self.effective_duration_s))
            or self.effective_duration_s < 0
        ):
            raise ContractError("ResultRow.effective_duration_s must be finite and nonnegative")
        for name in ("subject_id", "session_id", "interaction_id", "pair_id", "segment_id", "trial", "condition"):
            _assert_json_safe(getattr(self, name), f"ResultRow.{name}")
        for name in ("analysis_stream_id", "eye_label", "stream_parent_refs", "segment_instance_id",
                     "physical_partition_id", "support_ref", "dependence_ref"):
            _assert_json_safe(getattr(self, name), f"ResultRow.{name}")
        for name in ("n_window_observed", "n_support_observed"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ContractError(f"ResultRow.{name} must be a nonnegative integer or None")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in METRIC_COLUMNS}


@dataclass
class NormalizedDataset:
    """Canonical tables plus provenance; IDs are never inferred from file order."""

    signals: pd.DataFrame
    metadata: dict[str, Any]
    segments: pd.DataFrame = field(default_factory=pd.DataFrame)
    aois: pd.DataFrame = field(default_factory=pd.DataFrame)
    performance: pd.DataFrame = field(default_factory=pd.DataFrame)
    source_manifest: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        missing = [column for column in SIGNAL_REQUIRED_COLUMNS if column not in self.signals]
        if missing:
            raise ContractError("signals missing required columns: " + ", ".join(missing))
        if self.signals["subject_id"].isna().any() or self.signals["session_id"].isna().any():
            raise ContractError("subject_id/session_id must be explicit for every signal row")
        if self.signals["original_index"].isna().any():
            raise ContractError("original_index must retain source row order")
        if not isinstance(self.metadata, dict):
            raise ContractError("metadata must be an object")
        declared_version = self.metadata.get("contract_version", "1.0.0")
        if declared_version not in SUPPORTED_CONTRACT_VERSIONS:
            raise ContractError(f"Unsupported normalized-data contract version: {declared_version}")
        if declared_version == CONTRACT_VERSION:
            missing_identity = [column for column in ("source_id", "observation_id") if column not in self.signals]
            if missing_identity:
                raise ContractError("signals missing 1.2 identity columns: " + ", ".join(missing_identity))
            if self.signals["observation_id"].isna().any() or self.signals["observation_id"].astype("string").str.strip().eq("").any():
                raise ContractError("observation_id must be explicit for every signal row")
            if self.signals["observation_id"].duplicated().any():
                raise ContractError("observation_id must be unique within a normalized signal table")
        _assert_json_safe(self.metadata, "NormalizedDataset.metadata")
        segment_required = SEGMENT_REQUIRED_COLUMNS if declared_version == CONTRACT_VERSION else LEGACY_SEGMENT_REQUIRED_COLUMNS
        for frame, required, name in (
            (self.segments, segment_required, "segments"),
            (self.aois, AOI_REQUIRED_COLUMNS, "aois"),
        ):
            if not frame.empty:
                missing = [column for column in required if column not in frame]
                if missing:
                    raise ContractError(f"{name} missing required columns: " + ", ".join(missing))
        _assert_json_safe(self.source_manifest, "NormalizedDataset.source_manifest")


@dataclass(frozen=True)
class DesignSpec:
    """Explicit statistical design metadata; unknown design is not auto-inferred."""

    design_type: str = "unknown"
    outcome_type: str = "unknown"
    analysis_unit: str = "unknown"
    subject_column: str | None = "subject_id"
    session_column: str | None = "session_id"
    interaction_column: str | None = "interaction_id"
    team_column: str | None = None
    analysis_unit_column: str | None = None
    group_column: str | None = "between_group"
    condition_column: str | None = "condition"
    trial_column: str | None = "trial"
    performance_columns: tuple[str, ...] = ()
    performance_join_keys: tuple[str, ...] = ()
    independence_confirmed: bool = False
    independence_note: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.design_type == "unknown" and self.independence_confirmed:
            raise ContractError("Unknown design cannot claim independence was confirmed")
        if len(set(self.performance_join_keys)) != len(self.performance_join_keys):
            raise ContractError("performance_join_keys must be unique")
        if self.performance_columns and not self.performance_join_keys:
            raise ContractError("Performance columns require explicit one-to-one join keys")
        _assert_json_safe(self.options, "DesignSpec.options")

    def to_dict(self) -> dict[str, Any]:
        return {
            "design_type": self.design_type,
            "outcome_type": self.outcome_type,
            "analysis_unit": self.analysis_unit,
            "subject_column": self.subject_column,
            "session_column": self.session_column,
            "interaction_column": self.interaction_column,
            "team_column": self.team_column,
            "analysis_unit_column": self.analysis_unit_column,
            "group_column": self.group_column,
            "condition_column": self.condition_column,
            "trial_column": self.trial_column,
            "performance_columns": list(self.performance_columns),
            "performance_join_keys": list(self.performance_join_keys),
            "independence_confirmed": self.independence_confirmed,
            "independence_note": self.independence_note,
            "options": dict(self.options),
        }


@dataclass
class AnalysisBundle:
    """Common engine result; status is mandatory so cancellation cannot look successful."""

    tables: dict[str, pd.DataFrame]
    metadata: dict[str, Any]
    logs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.tables, dict) or any(
            not isinstance(name, str) or not isinstance(table, pd.DataFrame)
            for name, table in self.tables.items()
        ):
            raise ContractError("tables must map names to pandas DataFrames")
        if not isinstance(self.metadata, dict):
            raise ContractError("metadata must be an object")
        status = self.metadata.get("execution_status")
        if status not in EXECUTION_STATUSES:
            raise ContractError(
                "metadata.execution_status must be one of " + ", ".join(sorted(EXECUTION_STATUSES))
            )
        if not isinstance(self.logs, list) or any(not isinstance(line, str) for line in self.logs):
            raise ContractError("logs must be a list of strings")
        _assert_json_safe(self.metadata, "AnalysisBundle.metadata")
        metric_table = self.tables.get("metrics")
        if metric_table is not None:
            validate_metric_table(metric_table)


@dataclass
class StatisticsResult:
    """Statistical output before conversion into the shared analysis bundle."""

    table: pd.DataFrame
    audit: dict[str, Any]
    diagnostics: pd.DataFrame = field(default_factory=pd.DataFrame)

    def to_bundle(self, *, status: str = "complete", logs: list[str] | None = None) -> AnalysisBundle:
        return AnalysisBundle(
            tables={"statistics": self.table, "diagnostics": self.diagnostics},
            metadata={"contract_version": CONTRACT_VERSION, "execution_status": status,
                      "audit": dict(self.audit)},
            logs=list(logs or []),
        )


def validate_metric_table(frame: pd.DataFrame) -> None:
    missing = [column for column in LEGACY_METRIC_COLUMNS if column not in frame]
    if missing:
        raise ContractError("metrics missing required columns: " + ", ".join(missing))
    if (frame["run_id"].isna() | frame["run_id"].astype("string").str.strip().eq("")).any():
        raise ContractError("run_id must be present for every metric row")
    if (frame["metric_id"].isna() | frame["metric_id"].astype("string").str.strip().eq("")).any():
        raise ContractError("metric_id must be present for every row")
    if (frame["status"].isna() | frame["status"].astype("string").str.strip().eq("")).any():
        raise ContractError("status must be present for every row")
    for column in ("algorithm_version", "parameter_ref"):
        if (frame[column].isna() | frame[column].astype("string").str.strip().eq("")).any():
            raise ContractError(f"{column} must be explicit for every metric row")
    numeric_values = pd.to_numeric(frame["value"], errors="coerce")
    if (frame["value"].notna() & numeric_values.isna()).any():
        raise ContractError("metric value must be numeric or missing")
    if numeric_values.dropna().map(lambda value: not math.isfinite(float(value))).any():
        raise ContractError("metric values must be finite; encode unavailable values as NA with a reason")
    if frame["value"].notna().any() and (
        frame.loc[frame["value"].notna(), "unit"].isna()
        | frame.loc[frame["value"].notna(), "unit"].astype("string").str.strip().eq("")
    ).any():
        raise ContractError("computed values require explicit units")
    if (frame["value"].isna() & frame["reason_code"].isna()).any():
        raise ContractError("missing metric values require a reason_code")
    if (frame["status"].astype(str).isin({"unavailable", "blocked", "failed", "cancelled"}) & frame["value"].notna()).any():
        raise ContractError("unavailable, blocked, failed, and cancelled metric rows cannot contain computed values")
    not_computed = frame["status"].astype(str).isin({"unavailable", "blocked", "failed", "cancelled"})
    if frame.loc[not_computed, "reason_code"].isna().any():
        raise ContractError("non-computed metrics require a reason_code")
    for column in ("n_observed", "n_interpolated"):
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if numeric.isna().any() or (numeric < 0).any() or (numeric % 1 != 0).any():
            raise ContractError(f"{column} must contain nonnegative integers")
    duration = pd.to_numeric(frame["effective_duration_s"], errors="coerce")
    if (frame["effective_duration_s"].notna() & duration.isna()).any() or (duration.dropna() < 0).any():
        raise ContractError("effective_duration_s must be nonnegative numeric seconds or missing")
    if duration.dropna().map(lambda value: not math.isfinite(float(value))).any():
        raise ContractError("effective_duration_s values must be finite")


def _assert_json_safe(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{label} contains NaN or infinity")
        return
    if isinstance(value, list) or isinstance(value, tuple):
        for item in value:
            _assert_json_safe(item, label)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{label} keys must be strings")
            _assert_json_safe(item, label)
        return
    raise ContractError(f"{label} contains non-JSON-safe value {type(value).__name__}")


ProgressCallback = Any  # Callable[[float, str], None]
CancelCallback = Any  # Callable[[], bool]
