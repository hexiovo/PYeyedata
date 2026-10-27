"""Public, no-GUI orchestration for the PYeyedata analysis engine."""
from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import os
import re
import uuid
from typing import Any, Callable, Literal, Mapping
import math

import numpy as np
import pandas as pd

from .contracts import (
    AVAILABILITY_COLUMNS,
    CONTRACT_VERSION,
    METRIC_COLUMNS,
    AnalysisBundle,
    DesignSpec,
    NormalizedDataset,
)
from .events import EventDetectionConfig
from .eye_streams import EyeSelectionConfig
from .execution_cache import CacheTransaction, ExecutionCache, CacheIdentityError
from .group import get_group_catalogue
from .individual import get_individual_catalogue
from .processing import ProcessingConfig, ProcessingResult, process_signals
from .scopes import SegmentScopeConfig
from .store import (
    ArchiveError,
    PyeaArchive,
    dataframe_from_csv_member,
    dataframe_to_csv_member,
    read_pyea,
    write_pyea,
)


PIPELINE_SCHEMA_VERSION = "1.0.0"


class PipelineError(ValueError):
    """Invalid typed configuration or incompatible pipeline input."""


@dataclass(frozen=True)
class AnalysisConfig:
    """Typed orchestration config; method-specific options retain their owning module schema."""

    run_id: str = field(default_factory=lambda: "run-" + uuid.uuid4().hex)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    eye_selection: EyeSelectionConfig = field(default_factory=EyeSelectionConfig)
    event_detection: EventDetectionConfig = field(default_factory=EventDetectionConfig)
    scope: SegmentScopeConfig = field(default_factory=SegmentScopeConfig)
    individual: Mapping[str, Any] = field(default_factory=dict)
    group: Mapping[str, Any] = field(default_factory=dict)
    advanced: Mapping[str, Any] = field(default_factory=dict)
    statistics: DesignSpec | Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    run_individual: bool = True
    run_group: bool = True
    run_advanced: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise PipelineError("run_id must be a nonempty string")
        expected = (
            ("processing", self.processing, ProcessingConfig),
            ("eye_selection", self.eye_selection, EyeSelectionConfig),
            ("event_detection", self.event_detection, EventDetectionConfig),
            ("scope", self.scope, SegmentScopeConfig),
        )
        for name, value, expected_type in expected:
            if not isinstance(value, expected_type):
                raise PipelineError(f"{name} must be {expected_type.__name__}")
        for name, value in (
            ("individual", self.individual),
            ("group", self.group),
            ("advanced", self.advanced),
            ("metadata", self.metadata),
            ("context", self.context),
        ):
            if not isinstance(value, Mapping):
                raise PipelineError(f"{name} must be a mapping")
        if self.statistics is not None and not isinstance(self.statistics, (DesignSpec, Mapping)):
            raise PipelineError("statistics must be DesignSpec, a design mapping, or None")
        if any(not isinstance(value, bool) for value in (self.run_individual, self.run_group, self.run_advanced)):
            raise PipelineError("module enable flags must be booleans")


def _json_safe(value: Any) -> Any:
    """Convert metadata to strict JSON values without serializing DataFrames implicitly."""
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, pd.DataFrame):
        raise PipelineError("DataFrames must be persisted as named table members")
    raise PipelineError(f"Unsupported metadata value: {type(value).__name__}")


def _typed_fields(dataclass_type: type[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in fields(dataclass_type):
        default = None
        default_factory = None
        if item.default is not MISSING:
            default = item.default
        elif item.default_factory is not MISSING:
            factory_name = getattr(item.default_factory, "__name__", "factory")
            default_factory = factory_name
            if factory_name in {"dict", "list", "tuple", "set"}:
                default = item.default_factory()
            try:
                default = _json_safe(default) if default is not None else None
            except PipelineError:
                default = None
        record = {
            "name": item.name,
            "type": str(item.type),
            "default": _json_safe(default) if default is not None else None,
        }
        if default_factory is not None:
            record["default_factory"] = default_factory
        result.append(record)
    return result


def get_analysis_schema() -> dict[str, Any]:
    """Return the live public method catalogues and typed parameter definitions."""
    from .advanced_signals import ADVANCED_CHILD_SPECS, ADVANCED_METHODS
    from .contracts import CONTRACT_VERSION
    from .aoi_geometry import AOI_DOMAIN_SCHEMA_VERSION, AOI_GEOMETRY_VERSION, AOI_BOUNDARY_POLICY

    return {
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "aoi_geometry": {"domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION,
                         "algorithm_version": AOI_GEOMETRY_VERSION,
                         "boundary_policy": AOI_BOUNDARY_POLICY,
                         "shape_values": ["rectangle", "polygon"],
                         "vertices_format": "JSON Nx2 finite numeric points; simple single ring",
                         "aoi_name_missing_policy": ["require_source", "allow_legacy_missing"]},
        "run": {
            "input": "NormalizedDataset",
            "output": "AnalysisBundle",
            "cancellation": "Callable[[], bool]",
            "progress": "Callable[[float, str], None]",
            "stages": ["shared_processing", "individual", "group", "advanced", "statistics"],
        },
        "typed_parameters": {
            "processing": _typed_fields(ProcessingConfig),
            "eye_selection": _typed_fields(EyeSelectionConfig),
            "event_detection": _typed_fields(EventDetectionConfig),
            "scope": _typed_fields(SegmentScopeConfig),
            "statistics_design": _typed_fields(DesignSpec),
        },
        "individual": {
            "catalogue": _json_safe(get_individual_catalogue()),
            "config_keys": ["metric_ids", "enable_experimental", "max_gap_s", "provenance", "metadata"],
        },
        "group": {
            "catalogue": _json_safe(get_group_catalogue()),
            "config_keys": ["metric_ids", "enable_experimental", "alignment_tolerance_s", "provenance", "metadata"],
        },
        "advanced": {
            "methods": _json_safe(ADVANCED_METHODS),
            "parent_child_specs": _json_safe(ADVANCED_CHILD_SPECS),
            "typed_parameters": _json_safe(
                {item["name"]: item for method in ADVANCED_METHODS
                 for item in method.get("parameter_definitions", [])}
            ),
            "config_keys": ["metric_ids", "advanced", "metadata", "provenance"],
        },
        "statistics": {
            "entrypoint": "run_statistics(metrics, design, performance=None, cancel=None, progress=None)",
            "design_type": "DesignSpec | mapping",
            "analysis_unit_warning": "Statistical routes enforce explicit independent-unit and overlap dependencies.",
        },
        "context_tables": ["external_events", "target_movement_intervals", "eyelid_intervals"],
        "individual_context_inputs": ["target_movement_intervals", "reference_fixations", "reference_trajectory", "reading_annotations"],
    }


@dataclass
class AnalysisPreflight:
    """Shared processed tables and explicit availability checks, without metric calculations."""

    processing: ProcessingResult
    availability: pd.DataFrame
    metadata: dict[str, Any]


@dataclass
class LoadedAnalysis:
    """Self-contained archive contents; results-only archives have no dataset."""

    archive: PyeaArchive
    dataset: NormalizedDataset | None
    result: AnalysisBundle
    config: dict[str, Any]
    raw_sources: dict[str, bytes] = field(default_factory=dict)


def _plain(value: Any) -> Any:
    """Materialize typed settings as ordinary mappings while retaining table objects."""
    if is_dataclass(value):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _coerce_config(config: AnalysisConfig | Mapping[str, Any] | None) -> AnalysisConfig:
    if config is None:
        return AnalysisConfig()
    if isinstance(config, AnalysisConfig):
        return config
    if not isinstance(config, Mapping):
        raise PipelineError("config must be AnalysisConfig or a mapping")
    data = dict(config)
    constructors = {
        "processing": ProcessingConfig,
        "eye_selection": EyeSelectionConfig,
        "event_detection": EventDetectionConfig,
        "scope": SegmentScopeConfig,
    }
    for name, constructor in constructors.items():
        value = data.get(name)
        if isinstance(value, Mapping):
            prepared = dict(value)
            if name == "processing" and isinstance(prepared.get("pupil_baseline_window_s"), list):
                prepared["pupil_baseline_window_s"] = tuple(prepared["pupil_baseline_window_s"])
            data[name] = constructor(**prepared)
    allowed_config_fields = {item.name for item in fields(AnalysisConfig)}
    extra = set(data) - allowed_config_fields
    if extra:
        raise PipelineError("Unknown AnalysisConfig fields: " + ", ".join(sorted(extra)))
    return AnalysisConfig(**data)


def _cancelled(cancel: Callable[[], bool] | None) -> bool:
    return bool(cancel and cancel())


def _stage_progress(
    callback: Callable[[float, str], None] | None, start: float, width: float,
) -> Callable[[float, str], None] | None:
    if callback is None:
        return None
    def report(fraction: float, message: str) -> None:
        value = min(1.0, max(0.0, float(fraction)))
        callback(start + width * value, message)
    return report


def _dataset_for_run(dataset: NormalizedDataset, config: AnalysisConfig) -> NormalizedDataset:
    dataset.validate()
    metadata = dict(dataset.metadata)
    metadata["run_id"] = config.run_id
    return NormalizedDataset(
        signals=dataset.signals,
        metadata=metadata,
        segments=dataset.segments,
        aois=dataset.aois,
        performance=dataset.performance,
        source_manifest=list(dataset.source_manifest),
    )


def _module_metadata(dataset: NormalizedDataset, config: AnalysisConfig) -> dict[str, Any]:
    supplied = _plain(config.metadata)
    metadata = dict(dataset.metadata)
    if isinstance(supplied, Mapping):
        metadata.update(supplied)
    coordinate = metadata.get("coordinate_metadata", {})
    if is_dataclass(coordinate):
        coordinate = asdict(coordinate)
    if not isinstance(coordinate, Mapping):
        coordinate = {}
    semantic = {**dict(coordinate), **metadata}
    source_time_unit = coordinate.get(
        "time_unit", metadata.get("source_time_unit", metadata.get("time_unit")),
    )
    # NormalizedDataset.time_s is always expressed in seconds. Keep the source
    # clock's declared unit beside it for provenance, but tell analysis modules
    # the unit of the values they actually receive. Confirmation remains a
    # separate metadata gate and is never inferred here.
    semantic["source_time_unit"] = source_time_unit
    semantic["normalized_time_unit"] = "s"
    semantic["time_unit"] = "s"
    return semantic


def _module_config(
    dataset: NormalizedDataset,
    config: AnalysisConfig,
    overrides: Mapping[str, Any],
    events: pd.DataFrame,
    *,
    stream_id: str | None = None,
) -> dict[str, Any]:
    semantic = _module_metadata(dataset, config)
    module_metadata: dict[str, Any] = {}
    supplied_module_metadata = _plain(overrides.get("metadata", {}))
    if isinstance(supplied_module_metadata, Mapping):
        module_metadata = dict(supplied_module_metadata)
    semantic.update({key: value for key, value in module_metadata.items() if key != "coordinate_metadata"})
    coordinate_metadata = semantic.get("coordinate_metadata", {})
    if is_dataclass(coordinate_metadata):
        coordinate_metadata = asdict(coordinate_metadata)
    coordinate_metadata = dict(coordinate_metadata) if isinstance(coordinate_metadata, Mapping) else {}
    module_coordinate_metadata = module_metadata.get("coordinate_metadata", {})
    if is_dataclass(module_coordinate_metadata):
        module_coordinate_metadata = asdict(module_coordinate_metadata)
    if isinstance(module_coordinate_metadata, Mapping):
        coordinate_metadata.update(module_coordinate_metadata)
    semantic["coordinate_metadata"] = coordinate_metadata
    result = dict(semantic)
    if overrides is config.advanced:
        advanced_options = dict(_plain(overrides))
        result["advanced"] = {key: value for key, value in advanced_options.items() if key != "metric_ids"}
        if "metric_ids" in advanced_options:
            result["metric_ids"] = advanced_options["metric_ids"]
    else:
        result.update({key: value for key, value in _plain(overrides).items() if key != "metadata"})
    result.update({key: value for key, value in module_metadata.items() if key != "coordinate_metadata"})
    result.update({
        "run_id": config.run_id,
        "metadata": semantic,
        "coordinate_metadata": coordinate_metadata,
        "source_manifest": list(dataset.source_manifest),
        "provenance": {"source_manifest": list(dataset.source_manifest)},
        "events": events,
        "segments": dataset.segments,
        "aois": dataset.aois,
    })
    for name, table in config.context.items():
        if isinstance(table, pd.DataFrame):
            result[name] = table
    if stream_id is not None:
        result["analysis_stream_id"] = stream_id
    return result


def _join_analysis_stream(signals: pd.DataFrame, stream: pd.DataFrame) -> pd.DataFrame:
    if "observation_id" not in signals or "observation_id" not in stream:
        raise PipelineError("processed gaze streams require observation_id to join source samples")
    if signals["observation_id"].duplicated().any():
        raise PipelineError("processed signal observation_id values are not unique")
    indexed = signals.set_index("observation_id", drop=False)
    ids = stream["observation_id"].tolist()
    missing = [value for value in ids if value not in indexed.index]
    if missing:
        raise PipelineError("analysis stream refers to an observation absent from processed signals")
    joined = indexed.loc[ids].copy().reset_index(drop=True)
    stream = stream.reset_index(drop=True)
    for column in stream.columns:
        # Stream fields carry dispatch identity and eye-selection metadata that
        # need not exist on the source signal table. Preserve every field after
        # joining by observation ID so downstream metrics and event queries use
        # the exact stream they were dispatched for.
        joined[column] = stream[column].to_numpy()
    joined["analysis_gaze_valid"] = stream["analysis_gaze_valid"].astype("boolean").to_numpy()
    joined["gaze_valid"] = joined["analysis_gaze_valid"].copy()
    if "source_validity_status" in stream:
        states = stream["source_validity_status"].astype("string")
        source_valid = states.map({"valid": True, "invalid": False}).astype("boolean")
        joined["source_gaze_valid"] = source_valid.to_numpy()
    for column in ("gaze_interpolated", "gaze_x_interpolated", "gaze_y_interpolated"):
        joined[column] = False
    return joined


def _event_stream(events: pd.DataFrame, stream_id: str | None) -> pd.DataFrame:
    if stream_id is None or "analysis_stream_id" not in events:
        return events.copy(deep=True)
    return events.loc[events["analysis_stream_id"].astype("string").eq(stream_id).fillna(False)].copy()


_EVENT_PARTITION_BINDING_VERSION = "physical-support-binding-v1"
_EVENT_REQUIRED_IDENTITY = (
    "source_id", "subject_id", "session_id", "analysis_stream_id", "timebase_id", "clock_id",
)
_EVENT_OPTIONAL_IDENTITY = ("interaction_id", "trial", "stimulus_id")


def _binding_value_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _event_support_in_one_physical_partition(
    scoped: pd.DataFrame, start: float, end: float,
) -> tuple[str | None, str | None]:
    """Validate an event's complete interval against one continuous gaze support run."""
    if "time_s" not in scoped or "physical_partition_id" not in scoped:
        return None, "external_event_physical_support_fields_unavailable"
    times = pd.to_numeric(scoped["time_s"], errors="coerce")
    finite = np.isfinite(times.to_numpy(dtype=float, na_value=np.nan))
    if not finite.any():
        return None, "external_event_time_support_unavailable"
    ordered = scoped.loc[finite].copy()
    ordered["_binding_time"] = times.loc[ordered.index].to_numpy(dtype=float)
    ordered["_binding_order"] = np.arange(len(ordered), dtype=np.int64)
    ordered = ordered.sort_values(["_binding_time", "_binding_order"], kind="stable").reset_index(drop=True)
    values = ordered["_binding_time"].to_numpy(dtype=float)
    if start < values[0] - 1e-12 or end > values[-1] + 1e-12:
        return None, "external_event_outside_observed_time_support"

    left = int(np.searchsorted(values, start, side="left"))
    if left >= len(values) or not math.isclose(float(values[left]), start, rel_tol=0.0, abs_tol=1e-12):
        left -= 1
    right = int(np.searchsorted(values, end, side="left"))
    if right >= len(values) or not math.isclose(float(values[right]), end, rel_tol=0.0, abs_tol=1e-12):
        right = int(np.searchsorted(values, end, side="right"))
    else:
        right = int(np.searchsorted(values, end, side="right")) - 1
    if left < 0 or right >= len(values) or right <= left:
        return None, "external_event_continuous_support_unavailable"
    support = ordered.iloc[left:right + 1]
    if len(support) < 2:
        return None, "external_event_continuous_support_unavailable"

    if "time_valid" not in support:
        return None, "external_event_time_validity_unavailable"
    if not support["time_valid"].map(
        lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
    ).all():
        return None, "external_event_contains_invalid_time_support"
    if "analysis_gaze_valid" not in support or not support["analysis_gaze_valid"].map(
        lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
    ).all():
        return None, "external_event_contains_invalid_sample"
    if "source_validity_status" not in support or not support["source_validity_status"].astype("string").eq("valid").fillna(False).all():
        return None, "external_event_contains_invalid_sample"

    partition_values = support["physical_partition_id"]
    if partition_values.map(_binding_value_missing).any():
        return None, "external_event_physical_partition_unavailable"
    partitions = partition_values.astype("string").unique()
    if len(partitions) != 1:
        return None, "external_event_crosses_physical_partition"
    same_partition = scoped["physical_partition_id"].astype("string").eq(str(partitions[0])).fillna(False)
    all_times = pd.to_numeric(scoped["time_s"], errors="coerce")
    if (~np.isfinite(all_times.to_numpy(dtype=float, na_value=np.nan)) & same_partition.to_numpy()).any():
        return str(partitions[0]), "external_event_contains_invalid_time_support"
    deltas = np.diff(support["_binding_time"].to_numpy(dtype=float))
    if np.any(~np.isfinite(deltas)) or np.any(deltas <= 0):
        return str(partitions[0]), "external_event_noncontinuous_physical_support"
    if "event_barrier_before" in support:
        barriers = support["event_barrier_before"].iloc[1:].map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
        )
        if barriers.any():
            return str(partitions[0]), "external_event_crosses_physical_boundary"
    return str(partitions[0]), None


def _external_event_partition_binding_reason(
    event: pd.Series, streams: pd.DataFrame,
) -> tuple[str | None, str | None]:
    for key in _EVENT_REQUIRED_IDENTITY:
        if _binding_value_missing(event.get(key)):
            return None, f"external_event_identity_missing_{key}"
        if key not in streams:
            return None, f"external_event_scope_field_unavailable_{key}"
    if "time_s" not in streams or "physical_partition_id" not in streams:
        return None, "external_event_physical_support_fields_unavailable"

    start = pd.to_numeric(pd.Series([event.get("start_s")]), errors="coerce").iloc[0]
    end = pd.to_numeric(pd.Series([event.get("end_s")]), errors="coerce").iloc[0]
    if pd.isna(start) or pd.isna(end) or not np.isfinite(float(start)) or not np.isfinite(float(end)) or end <= start:
        return None, "external_event_interval_invalid"

    scoped = streams
    for key in (*_EVENT_REQUIRED_IDENTITY, *_EVENT_OPTIONAL_IDENTITY):
        value = event.get(key)
        if _binding_value_missing(value):
            continue
        if key not in scoped:
            return None, f"external_event_scope_field_unavailable_{key}"
        scoped = scoped.loc[scoped[key].astype("string").eq(str(value)).fillna(False)]
    if scoped.empty:
        return None, "external_event_scope_not_matched_to_analysis_stream"

    declared_partition = event.get("physical_partition_id")
    has_declared_partition = not _binding_value_missing(declared_partition)
    unknown_identity = [
        key for key in _EVENT_OPTIONAL_IDENTITY
        if key in scoped and _binding_value_missing(event.get(key))
    ]
    if unknown_identity:
        candidates = [group for _, group in scoped.groupby(unknown_identity, dropna=False, sort=False)]
    else:
        candidates = [scoped]

    checked = [
        _event_support_in_one_physical_partition(group, float(start), float(end))
        for group in candidates
    ]
    valid = [(partition, reason) for partition, reason in checked if partition is not None and reason is None]
    if has_declared_partition:
        declared = str(declared_partition)
        matching = [item for item in valid if item[0] == declared]
        if len(matching) == 1:
            return declared, None
        if len(matching) > 1:
            return None, "external_event_scope_ambiguous_for_declared_partition"
        if valid:
            return None, "external_event_declared_partition_conflict"
        reasons = [reason for _, reason in checked if reason]
        if "external_event_crosses_physical_partition" in reasons:
            return None, "external_event_crosses_physical_partition"
        if "external_event_contains_invalid_sample" in reasons:
            return None, "external_event_contains_invalid_sample"
        if "external_event_crosses_physical_boundary" in reasons:
            return None, "external_event_crosses_physical_boundary"
        if not scoped["physical_partition_id"].astype("string").eq(declared).any():
            return None, "external_event_declared_partition_not_found"
        return None, reasons[0] if reasons else "external_event_declared_partition_unsupported"

    if len(valid) == 1:
        return valid[0][0], None
    if len(valid) > 1:
        return None, "external_event_physical_partition_ambiguous"
    reasons = [reason for _, reason in checked if reason]
    for reason in (
        "external_event_crosses_physical_partition",
        "external_event_contains_invalid_sample",
        "external_event_contains_invalid_time_support",
        "external_event_crosses_physical_boundary",
        "external_event_noncontinuous_physical_support",
        "external_event_physical_partition_unavailable",
        "external_event_outside_observed_time_support",
        "external_event_continuous_support_unavailable",
    ):
        if reason in reasons:
            return None, reason
    return None, reasons[0] if reasons else "external_event_continuous_support_unavailable"


def _bind_single_partition_events(events: pd.DataFrame, streams: pd.DataFrame) -> pd.DataFrame:
    """Audit and bind external events only when their entire scope is physically supported."""
    output = events.copy(deep=True)
    audit_columns = {
        "partition_binding_source": pd.Series(index=output.index, dtype="string"),
        "partition_binding_version": pd.Series(index=output.index, dtype="string"),
        "partition_binding_original_event_id": pd.Series(index=output.index, dtype="string"),
        "partition_binding_status": pd.Series(index=output.index, dtype="string"),
        "partition_binding_reason": pd.Series(index=output.index, dtype="string"),
    }
    for column, values in audit_columns.items():
        output[column] = values
    if output.empty:
        return output
    if "physical_partition_id" not in output:
        output["physical_partition_id"] = pd.NA
    for index, event in output.iterrows():
        original_event_id = event.get("source_event_id")
        if _binding_value_missing(original_event_id):
            output.at[index, "partition_binding_status"] = "not_applicable"
            continue
        output.at[index, "partition_binding_source"] = "external_events"
        output.at[index, "partition_binding_version"] = _EVENT_PARTITION_BINDING_VERSION
        output.at[index, "partition_binding_original_event_id"] = str(original_event_id)
        partition, reason = _external_event_partition_binding_reason(event, streams)
        if reason is None and partition is not None:
            if _binding_value_missing(event.get("physical_partition_id")):
                output.at[index, "physical_partition_id"] = partition
                output.at[index, "partition_binding_status"] = "bound"
            else:
                output.at[index, "partition_binding_status"] = "validated"
            output.at[index, "partition_binding_reason"] = "unique_continuous_physical_support"
            continue
        output.at[index, "partition_binding_status"] = "unmatched"
        output.at[index, "partition_binding_reason"] = reason or "external_event_partition_unmatched"
        if "event_evidence" in output:
            output.at[index, "event_evidence"] = "unknown"
        if "reason_code" in output:
            output.at[index, "reason_code"] = reason or "external_event_partition_unmatched"
    return output


def _records_with_scope(
    frame: pd.DataFrame, *, module: str, stream_id: str | None, scope: str,
) -> pd.DataFrame:
    output = frame.copy(deep=True)
    output["module"] = module
    output["scope"] = scope
    output = _assign_analysis_stream(output, stream_id)
    return output


def _assign_analysis_stream(frame: pd.DataFrame, stream_id: str | None) -> pd.DataFrame:
    """Normalize a module result to the stream used for its dispatch.

    Source-level work (notably pupil metrics) is deliberately not a gaze stream;
    clear only synthetic legacy hashes produced by standalone module fallbacks.
    Explicit identities are preserved, while null/blank values are filled from
    the dispatch that selected a named stream.
    """
    output = frame.copy(deep=True)
    if stream_id is None:
        if "analysis_stream_id" not in output:
            output["analysis_stream_id"] = None
        else:
            current = output["analysis_stream_id"].astype("string")
            synthetic = current.str.startswith("legacy:", na=False) | current.str.strip().eq("").fillna(False)
            output["analysis_stream_id"] = current.mask(synthetic, pd.NA)
    elif "analysis_stream_id" not in output:
        output["analysis_stream_id"] = stream_id
    else:
        current = output["analysis_stream_id"].astype("string")
        missing = current.isna() | current.str.strip().eq("").fillna(True)
        output["analysis_stream_id"] = current.mask(missing, stream_id)
    return output


def _preflight_aoi_availability(
    availability: pd.DataFrame, frame: pd.DataFrame, aois: pd.DataFrame,
    module_config: dict[str, Any], metric_ids: set[str], budget: Any,
    cancel: Callable[[], bool] | None,
) -> pd.DataFrame:
    """Validate ready AOI entries without replacing earlier catalogue blockers.

    Geometry-reason parity with a run applies to otherwise ready AOI entries.
    Missing fields/metadata and disabled or unimplemented methods retain their
    existing catalogue reasons; no geometry scan is needed for those entries.
    """
    from .individual import _prepare_aoi_geometry_for_frame
    from .aoi_geometry import AOIGeometryError

    ready = availability["metric_id"].isin(metric_ids) & availability["available"].eq(True)
    if not ready.any():
        return availability
    module_config["_aoi_geometry_budget"] = budget
    module_config["_aoi_cancel_check"] = cancel
    try:
        _prepare_aoi_geometry_for_frame(frame, aois, module_config)
    except AOIGeometryError as exc:
        if exc.code == "aoi_geometry_cancelled":
            raise
        issue = exc.issue.as_dict()
        availability = availability.copy(deep=True)
        availability.loc[ready, "available"] = False
        availability.loc[ready, "status"] = "blocked"
        availability.loc[ready, "reason_code"] = issue["code"]
        availability.loc[ready, "reason"] = issue["message"]
        availability["aoi_geometry_issue"] = pd.Series(None, index=availability.index, dtype=object)
        for index in availability.index[ready]:
            availability.at[index, "aoi_geometry_issue"] = dict(issue)
    return availability


def _preflight_availability(
    dataset: NormalizedDataset,
    config: AnalysisConfig,
    processing: ProcessingResult,
    *, cancel: Callable[[], bool] | None = None,
) -> pd.DataFrame:
    from . import advanced_signals, group, individual
    from .aoi_geometry import AOIGeometryBudget

    aoi_budget = AOIGeometryBudget(cancel_check=cancel)
    aoi_budget.checkpoint()
    rows: list[pd.DataFrame] = []
    events = processing.events.copy(deep=True)
    stream_ids = (
        processing.analysis_streams["analysis_stream_id"].dropna().astype(str).drop_duplicates().tolist()
        if "analysis_stream_id" in processing.analysis_streams else []
    )
    joined_streams = {
        stream_id: _join_analysis_stream(
            processing.signals,
            processing.analysis_streams.loc[
                processing.analysis_streams["analysis_stream_id"].astype("string").eq(stream_id).fillna(False)
            ],
        )
        for stream_id in stream_ids
    }
    base_config = _module_config(dataset, config, {}, events)
    base = processing.signals.copy(deep=True)

    for stream_id, frame in joined_streams.items():
        module_config = _module_config(dataset, config, config.individual, _event_stream(events, stream_id), stream_id=stream_id)
        aoi_budget.checkpoint()
        available = individual._availability(frame, module_config)
        available = _preflight_aoi_availability(
            available, frame, dataset.aois, module_config,
            {row["metric_id"] for row in individual.INDIVIDUAL_CATALOGUE
             if row["metric_id"].startswith("individual.aoi.")}, aoi_budget, cancel)
        rows.append(_records_with_scope(available, module="individual", stream_id=stream_id, scope="gaze_stream"))
    pupil_or_shared = individual._availability(base, base_config)
    pupil_ids = [row["metric_id"] for row in pupil_or_shared.to_dict("records")
                 if "pupil" in row["metric_id"] or row["metric_id"].startswith("individual.sampling.")]
    shared_ids = [row["metric_id"] for row in pupil_or_shared.to_dict("records")
                  if row["metric_id"] not in pupil_ids and
                  not any(token in str(row.get("required_fields", [])) for token in ("gaze", "fixation"))]
    base_selected = pupil_or_shared.loc[pupil_or_shared["metric_id"].isin(set(pupil_ids + shared_ids))]
    rows.append(_records_with_scope(base_selected, module="individual", stream_id=None, scope="source_stream"))

    for stream_id, frame in [(key, value) for key, value in joined_streams.items()] + [(None, base)]:
        module_config = _module_config(dataset, config, config.group, _event_stream(events, stream_id), stream_id=stream_id)
        aoi_budget.checkpoint()
        catalogue = get_group_catalogue()
        group_rows = []
        for spec in catalogue:
            row = group._available(spec, frame, module_config)
            if "interaction_id" not in frame or frame["interaction_id"].isna().any():
                row = {**row, "available": False, "status": "blocked", "reason_code": "explicit_interaction_id_required",
                       "reason": "Group analysis requires an explicit interaction_id for every row."}
            group_rows.append(row)
        group_availability = _preflight_aoi_availability(
            pd.DataFrame(group_rows, columns=AVAILABILITY_COLUMNS), frame, dataset.aois, module_config,
            {"group.shared_aoi_proportion", "group.aoi_agreement", "group.transition_similarity",
             "group.all_member_aoi_agreement", "group.majority_aoi_agreement"}, aoi_budget, cancel)
        rows.append(_records_with_scope(group_availability, module="group", stream_id=stream_id,
                                        scope="gaze_stream" if stream_id else "source_stream"))

    try:
        advanced_module_config = _module_config(dataset, config, config.advanced, events)
        advanced_signals._params(advanced_module_config)
        advanced_signals._metadata_boundary(advanced_module_config)
        advanced_config_error = None
    except Exception as exc:
        advanced_config_error = f"invalid_advanced_config:{type(exc).__name__}:{exc}"
    advanced_specs = advanced_signals.ADVANCED_METHODS + advanced_signals.ADVANCED_CHILD_SPECS
    for spec in advanced_specs:
        metric_id = spec["metric_id"]
        parent = spec.get("parent_metric_id") or metric_id
        if parent.startswith("individual.pupil") or parent.startswith("individual.blink") or parent.startswith("individual.microsaccade"):
            candidate_frames = [(None, base, "source_stream")]
        else:
            candidate_frames = [(key, value, "gaze_stream") for key, value in joined_streams.items()]
        if not candidate_frames:
            candidate_frames = [(None, base, "source_stream")]
        for stream_id, frame, scope in candidate_frames:
            module_config = _module_config(dataset, config, config.advanced, _event_stream(events, stream_id), stream_id=stream_id)
            fields_needed = list(spec.get("requires_fields", []))
            if parent == "individual.pupil.spectral_power":
                eye = module_config.get("advanced", {}).get("pupil_psd_eye", "left")
                fields_needed.extend([f"pupil_{eye}", f"pupil_{eye}_valid"])
            missing_fields = [name for name in fields_needed if name not in frame]
            missing_metadata = []
            for name in spec.get("requires_metadata", []):
                value = advanced_signals._meta(module_config, name)
                if name.endswith("_confirmed") or name == "device_verified":
                    if value is not True:
                        missing_metadata.append(name)
                elif name.endswith("_evidence"):
                    if not advanced_signals._evidence(value):
                        missing_metadata.append(name)
                elif value is None or value == "unknown" or value == "":
                    missing_metadata.append(name)
            if advanced_config_error:
                reason = advanced_config_error
            elif missing_fields:
                reason = "missing_fields:" + ",".join(missing_fields)
            elif missing_metadata:
                reason = "missing_metadata:" + ",".join(missing_metadata)
            else:
                reason = "numeric_support_not_evaluated"
            ready = reason == "numeric_support_not_evaluated"
            rows.append(_records_with_scope(pd.DataFrame([{
                "metric_id": metric_id,
                "available": ready,
                "status": "preflight_ready" if ready else "blocked",
                "reason_code": "preconditions_met" if ready else "missing_precondition",
                "reason": "Declared fields and metadata are present; numeric support is evaluated during the advanced run." if ready else reason,
                "required_fields": fields_needed,
                "required_metadata": list(spec.get("requires_metadata", [])),
            }]), module="advanced", stream_id=stream_id, scope=scope))

    return pd.concat(rows, ignore_index=True, sort=False) if rows else pd.DataFrame(
        columns=[*AVAILABILITY_COLUMNS, "module", "scope", "analysis_stream_id"]
    )


def _prepare_preflight(
    dataset: NormalizedDataset,
    config: AnalysisConfig,
    *,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    _cache_transaction: CacheTransaction | None = None,
) -> AnalysisPreflight:
    if not isinstance(dataset, NormalizedDataset):
        raise PipelineError("dataset must be a NormalizedDataset")
    if _cache_transaction is not None:
        _cache_transaction.audit.setdefault("algorithm_versions", {}).update({
            "pipeline": PIPELINE_SCHEMA_VERSION,
            "event_partition_binding": _EVENT_PARTITION_BINDING_VERSION,
        })
    if progress:
        progress(0.0, "开始共享预处理与条件可用性检查")
    run_dataset = _dataset_for_run(dataset, config)
    external_events = config.context.get("external_events")
    if external_events is not None and not isinstance(external_events, pd.DataFrame):
        raise PipelineError("context.external_events must be a DataFrame")
    process_progress = _stage_progress(progress, 0.02, 0.34)
    processing = process_signals(
        run_dataset,
        config.processing,
        segments=run_dataset.segments,
        overlap_policy=config.scope.overlap_policy,
        eye_selection=config.eye_selection,
        event_detection=config.event_detection,
        external_events=external_events,
        coordinate_metadata=run_dataset.metadata.get("coordinate_metadata", {}),
        cancel=cancel,
        progress=process_progress,
        **({"_cache_transaction": _cache_transaction} if _cache_transaction is not None else {}),
    )
    processing.events = _bind_single_partition_events(processing.events, processing.analysis_streams)
    if processing.execution_status == "cancelled":
        if progress:
            progress(0.36, "共享处理已取消，跳过条件可用性检查")
        availability = pd.DataFrame(columns=[*AVAILABILITY_COLUMNS, "module", "scope", "analysis_stream_id"])
    else:
        if progress:
            progress(0.36, "正在检查指标字段与分析条件可用性")
        from .aoi_geometry import AOIGeometryError
        try:
            availability = _preflight_availability(run_dataset, config, processing, cancel=cancel)
        except AOIGeometryError as exc:
            if exc.code != "aoi_geometry_cancelled":
                raise
            processing.execution_status = "cancelled"
            processing.audit = {**processing.audit, "execution_status": "cancelled",
                                "preflight_aoi_issue": exc.issue.as_dict()}
            processing.logs = [*processing.logs, "preflight_aoi_geometry_cancelled"]
            availability = pd.DataFrame(columns=[*AVAILABILITY_COLUMNS, "module", "scope", "analysis_stream_id"])
        if _cancelled(cancel):
            processing.execution_status = "cancelled"
            processing.audit = {**processing.audit, "execution_status": "cancelled"}
            availability = pd.DataFrame(columns=[*AVAILABILITY_COLUMNS, "module", "scope", "analysis_stream_id"])
    if progress:
        if processing.execution_status == "complete":
            message = "共享处理与条件可用性前检完成"
        elif processing.execution_status == "cancelled":
            message = "共享处理与条件可用性前检已取消"
        else:
            message = f"共享处理与条件可用性前检结束：{processing.execution_status}"
        progress(0.36, message)
    return AnalysisPreflight(
        processing=processing,
        availability=availability,
        metadata={
            "run_id": config.run_id,
            "contract_version": CONTRACT_VERSION,
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "execution_status": processing.execution_status,
            "processing_audit": processing.audit,
            "preflight_only": True,
            "metric_calculations_run": False,
            "device_verified": False,
        },
    )


def preflight_analysis(
    dataset: NormalizedDataset,
    config: AnalysisConfig | Mapping[str, Any] | None = None,
    *,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    execution_cache: ExecutionCache | None = None,
    cache_eligible: Callable[[], bool] | None = None,
) -> AnalysisPreflight:
    """Run shared processing and condition checks without calculating metric results."""
    settings = _coerce_config(config)
    if execution_cache is None:
        return _prepare_preflight(dataset, settings, cancel=cancel, progress=progress)
    if not isinstance(execution_cache, ExecutionCache):
        raise PipelineError("execution_cache must be ExecutionCache or None")
    transaction = execution_cache.begin(run_id=settings.run_id, phase="preflight", cancel=cancel,
                                        eligible=cache_eligible)
    result = None
    try:
        result = _prepare_preflight(dataset, settings, cancel=cancel, progress=progress,
                                     _cache_transaction=transaction)
        return result
    finally:
        transaction.finish(complete=result is not None and result.processing.execution_status == "complete")
        if result is not None:
            result.metadata["execution_cache"] = transaction.audit


def _catalogue_selection(
    catalogue: list[dict[str, Any]], requested: Any, *, default_tiers: set[str] | None = None,
) -> list[str]:
    ids = {row["metric_id"] for row in catalogue}
    if isinstance(requested, (list, tuple, set, frozenset)):
        return [str(value) for value in requested if str(value) in ids]
    if default_tiers is not None:
        return [row["metric_id"] for row in catalogue if row.get("tier") in default_tiers]
    return [row["metric_id"] for row in catalogue if row.get("implemented")]


def _individual_groups(requested: list[str], catalogue: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_id = {row["metric_id"]: row for row in catalogue}
    groups = {"gaze": [], "pupil": [], "shared": []}
    for metric_id in requested:
        spec = by_id.get(metric_id, {})
        fields_needed = spec.get("requires_fields", [])
        metadata_needed = spec.get("requires_metadata", [])
        if "pupil" in metric_id or any("pupil" in name for name in fields_needed):
            groups["pupil"].append(metric_id)
        elif any("gaze" in name for name in fields_needed) or any("event" in name for name in metadata_needed):
            groups["gaze"].append(metric_id)
        else:
            groups["shared"].append(metric_id)
    return groups


def _collect_module(
    result: AnalysisBundle,
    *,
    module: str,
    stream_id: str | None,
    module_metadata: list[dict[str, Any]],
    tables: dict[str, list[pd.DataFrame]],
    metrics: list[pd.DataFrame],
    availability: list[pd.DataFrame],
) -> str:
    module_metadata.append({"module": module, "analysis_stream_id": stream_id, **result.metadata})
    if "metrics" in result.tables:
        metrics.append(_assign_analysis_stream(result.tables["metrics"], stream_id))
    if "availability" in result.tables:
        availability.append(_records_with_scope(result.tables["availability"], module=module,
                                                stream_id=stream_id, scope="gaze_stream" if stream_id else "source_stream"))
    for name, frame in result.tables.items():
        if name not in {"metrics", "availability"}:
            tables.setdefault(f"{module}_{name}", []).append(frame.copy(deep=True))
    return str(result.metadata.get("execution_status", "complete"))


def _mixed_descriptives_are_local_unavailable(
    result: AnalysisBundle, inference: pd.DataFrame, descriptives: pd.DataFrame,
    metrics: pd.DataFrame,
    *, require_complete: bool = True,
) -> bool:
    """Prove that missing descriptives belong only to local unavailable groups.

    This deliberately accepts only unfiltered populations. Ambiguous selection,
    typed strata, units, missing rows or backend failures keep the raw status.
    """
    audit = result.metadata.get("audit", {})
    design = audit.get("design", {}) if isinstance(audit, Mapping) else {}
    if not isinstance(design, Mapping):
        return False
    hypotheses = audit.get("hypotheses")
    if (not isinstance(hypotheses, list) or not hypotheses
            or any(not isinstance(item, Mapping) or item.get("status") != "blocked" for item in hypotheses)):
        return False
    strata = design.get("strata_columns", [])
    if (not isinstance(strata, (list, tuple))
            or any(not isinstance(name, str) or not name for name in strata)
            or len(set(strata)) != len(strata)):
        return False
    grouping = ["metric_id", *strata]
    required = {*grouping, "value", "unit", "status", "reason_code"}
    if not required.issubset(metrics.columns) or any(
        not set(grouping).issubset(table.columns) for table in (inference, descriptives)
    ):
        return False
    if any(result.metadata.get(name) for name in ("failure_type", "failure_reason", "exception")):
        return False
    diagnostics = result.tables.get("diagnostics")
    if isinstance(diagnostics, pd.DataFrame) and not diagnostics.empty:
        if "status" not in diagnostics or not diagnostics.status.isin({"complete", "blocked", "not_applicable"}).all():
            return False

    def identity(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
        output = []
        for column in grouping:
            value = row[column]
            if isinstance(value, np.generic):
                value = value.item()
            if value is None or value is pd.NA or value is pd.NaT or type(value) is float and math.isnan(value):
                output.append(("missing",))
            elif type(value) in {str, bool, int}:
                output.append((type(value).__name__, value))
            elif type(value) is float and math.isfinite(value):
                output.append(("float", value.hex()))
            else:
                return None
        return tuple(output)

    populations: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in metrics.to_dict("records"):
        key = identity(row)
        if key is None or row["status"] not in {"complete", "partial", "unavailable"}:
            return False
        value = row["value"]
        missing = (value is None or value is pd.NA or value is pd.NaT
                   or isinstance(value, (float, np.floating)) and math.isnan(float(value)))
        if not missing:
            if (isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating))
                    or not math.isfinite(float(value)) or row["status"] == "unavailable"
                    or not isinstance(row["unit"], str) or not row["unit"].strip()):
                return False
        if row["status"] != "complete":
            if not isinstance(row["reason_code"], str) or not row["reason_code"].strip():
                return False
        elif missing:
            return False
        populations.setdefault(key, []).append(row)

    seen: set[tuple[Any, ...]] = set()
    has_complete = has_local_missing = False
    for row in descriptives.to_dict("records"):
        key = identity(row)
        if key is None or key not in populations or key in seen:
            return False
        seen.add(key)
        population = populations[key]
        if (row.get("population") != "all_levels" or row.get("selection_mode") != "all"
                or row.get("requested_levels") is not None
                or row.get("n_input_rows") != len(population)
                or row.get("n_selected_rows") != len(population)):
            return False
        finite = [item for item in population if isinstance(item["value"], (int, float, np.integer, np.floating))
                  and math.isfinite(float(item["value"]))]
        if row.get("status") == "complete":
            units = {item["unit"] for item in finite}
            if (not finite or len(units) != 1 or row.get("unit") not in units
                    or row.get("input_unit") not in units or row.get("n_finite") != len(finite)):
                return False
            has_complete = True
        elif row.get("status") == "no_finite_selected_values":
            if (finite or row.get("n_finite") != 0
                    or any(item["status"] not in {"partial", "unavailable"} for item in population)
                    or row.get("unit") is not None or row.get("input_unit") is not None):
                return False
            has_local_missing = True
        else:
            return False
    if (seen != set(populations) or not has_local_missing
            or require_complete and not has_complete
            or not require_complete and has_complete):
        return False
    inference_groups = set()
    for row in inference.to_dict("records"):
        key = identity(row)
        if key is None or key not in populations:
            return False
        units = {item["unit"] for item in populations[key]
                 if isinstance(item["value"], (int, float, np.integer, np.floating))
                 and math.isfinite(float(item["value"]))}
        expected_unit = next(iter(units)) if len(units) == 1 else None
        if row.get("unit") != expected_unit or row.get("input_unit") != expected_unit:
            return False
        inference_groups.add(key)
    return inference_groups == set(populations)


def _statistics_completion_status(
    result: AnalysisBundle,
    *,
    design_supplied: bool,
    metrics: pd.DataFrame,
) -> tuple[str, str]:
    """Separate blocked inference from failed statistics work.

    ``run_statistics`` reports a failed module status when an unknown design
    blocks every hypothesis, even though its metric descriptives completed.
    At the pipeline boundary, that is a completed descriptive-only run when no
    design was supplied, and a partial run when an explicit design was blocked.
    Real failed rows, missing descriptives, and cancellation keep their status.
    """
    runner_status = str(result.metadata.get("execution_status", "unknown"))
    if runner_status not in {"complete", "partial", "failed", "cancelled"}:
        # Unknown backend state cannot be upgraded to a completed or partial
        # result merely because some tables happen to be present.
        return "failed", runner_status
    if runner_status == "cancelled" or not isinstance(metrics, pd.DataFrame):
        return runner_status, runner_status
    if metrics.empty:
        empty_input = any(str(message).startswith("empty_metrics:") for message in result.logs)
        if empty_input and runner_status in {"failed", "partial"}:
            # A cache-reuse run may have no newly calculated rows yet. Keep
            # this first subset non-complete but allow the bridge to recompute
            # after merging the legal cached rows.
            return "partial", runner_status
        return runner_status, runner_status

    inference = result.tables.get("statistics")
    descriptives = result.tables.get("descriptives")
    if (
        not isinstance(inference, pd.DataFrame)
        or inference.empty
        or "status" not in inference.columns
        or not inference["status"].astype("string").eq("blocked").all()
        or not isinstance(descriptives, pd.DataFrame)
        or descriptives.empty
        or "status" not in descriptives.columns
    ):
        return runner_status, runner_status
    if not descriptives["status"].astype("string").eq("complete").all():
        if _mixed_descriptives_are_local_unavailable(result, inference, descriptives, metrics):
            return "partial", runner_status
        return runner_status, runner_status
    return ("partial" if design_supplied else "complete"), runner_status


def _input_tables(dataset: NormalizedDataset, config: AnalysisConfig) -> dict[str, pd.DataFrame]:
    tables = {
        "input_signals": dataset.signals.copy(deep=True),
        "input_segments": dataset.segments.copy(deep=True),
        "input_aois": dataset.aois.copy(deep=True),
        "input_performance": dataset.performance.copy(deep=True),
    }
    for name, table in config.context.items():
        if isinstance(table, pd.DataFrame):
            tables[f"input_context_{name}"] = table.copy(deep=True)
    return tables


def _bundle_from_tables(
    *,
    tables: dict[str, pd.DataFrame],
    metadata: dict[str, Any],
    logs: list[str],
) -> AnalysisBundle:
    tables.setdefault("metrics", pd.DataFrame(columns=METRIC_COLUMNS))
    tables.setdefault("availability", pd.DataFrame(columns=[*AVAILABILITY_COLUMNS, "module", "scope", "analysis_stream_id"]))
    return AnalysisBundle(tables=tables, metadata=metadata, logs=logs)


_ARTIFACT_CACHE_VERSION = "complete-module-artifact/v1"
# These are the only group calculations that read config.events in group.py.
# Other selections still key the freshly evaluated event availability predicates
# because the returned availability table contains the whole group catalogue.
_GROUP_EVENT_ARTIFACT_METRICS = frozenset({
    "group.fixation_cooccurrence", "group.blink_synchrony", "group.multimatch.shape",
    "group.multimatch.direction", "group.multimatch.length", "group.multimatch.position",
    "group.multimatch.duration",
})


def _artifact_config_identity(module: str, config: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in config.items() if key != "run_id"}
    result["metadata"] = {key: value for key, value in config.get("metadata", {}).items() if key != "run_id"}
    events = config.get("events")
    if isinstance(events, pd.DataFrame):
        result["events"] = events.drop(columns=["run_id"], errors="ignore")
    if module == "group":
        from .group import _meta_ok, GROUP_CATALOGUE
        requested = set(config.get("metric_ids", []))
        known = {item["metric_id"] for item in GROUP_CATALOGUE}
        if requested.issubset(known) and not requested.intersection(_GROUP_EVENT_ARTIFACT_METRICS):
            result["events"] = {
                "dependency_contract": "group-non-event-calculations/full-availability-v1",
                "reliable_events": _meta_ok("reliable_events", config),
                "validated_blink_events": _meta_ok("validated_blink_events", config),
            }
    return result


def _rebind_module_artifact(result: AnalysisBundle, module: str, source_run_id: str,
                            config: dict[str, Any], context: Mapping[str, Any]) -> AnalysisBundle:
    """Rebind declared run columns/JSON parameters; never replace arbitrary text."""
    run_id = config["run_id"]
    parameter_ref = None
    parameter_owned_tables: set[str] = set()
    source_context_tables: set[str] = set()
    if module == "advanced":
        from .advanced_signals import _clean, _DETAIL_SCHEMAS
        parameter_owned_tables = {"metrics", *_DETAIL_SCHEMAS}
        source_context_tables = {"source_context_" + str(name) for name, value in context.items()
                                 if isinstance(value, pd.DataFrame)}
        snapshot = result.metadata.get("parameter_snapshot")
        if not isinstance(snapshot, dict):
            raise CacheIdentityError("advanced artifact has no declared parameter snapshot")
        snapshot["metadata"] = _clean(config.get("metadata", {}))
        events = config.get("events")
        snapshot["event_table_sha256"] = (hashlib.sha256(events.to_json(
            orient="split", double_precision=15, date_unit="ns").encode("utf-8")).hexdigest()
            if isinstance(events, pd.DataFrame) else None)
        raw = json.dumps(_clean(snapshot), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        parameter_ref = "advanced:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        result.metadata["parameter_ref"] = parameter_ref
    for name, table in result.tables.items():
        if name in source_context_tables:
            # Raw context is returned verbatim by its declared input name.
            # Its run/parameter columns can describe an external acquisition.
            continue
        if "run_id" in table:
            previous = table["run_id"]
            present = previous.dropna().astype(str)
            if not present.eq(source_run_id).all():
                raise CacheIdentityError("artifact contains foreign run column")
            # Assignment through the original series retains nullable/object dtype.
            table["run_id"] = previous.where(previous.isna(), run_id)
        if "parameter_ref" not in table:
            continue
        if parameter_ref is not None and name in parameter_owned_tables:
            table["parameter_ref"] = table["parameter_ref"].where(table["parameter_ref"].isna(), parameter_ref)
        elif module in {"individual", "group"}:
            def rebind(value: Any) -> Any:
                if not isinstance(value, str) or not value.startswith("{"):
                    return value
                parameters = json.loads(value)
                if isinstance(parameters, dict) and parameters.get("run_id") == source_run_id:
                    parameters["run_id"] = run_id
                    return json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                return value
            table["parameter_ref"] = table["parameter_ref"].map(rebind)
    return result


def _invoke_module_artifact(
    transaction: CacheTransaction | None, module: str, frame: pd.DataFrame,
    config: dict[str, Any], context: Mapping[str, Any], calculate: Callable[[], AnalysisBundle],
) -> AnalysisBundle:
    if transaction is None:
        return calculate()
    from . import individual, group, advanced_signals
    namespace = "artifact:" + module
    version = {"individual": individual.ALGORITHM_VERSION, "group": group.ALGORITHM_VERSION,
               "advanced": advanced_signals.ALGORITHM_VERSION}[module]
    key = transaction.key(namespace, {
        "schema": _ARTIFACT_CACHE_VERSION, "module": module, "algorithm_version": version,
        "individual_dependency_version": individual.ALGORITHM_VERSION,
        "contract": CONTRACT_VERSION, "frame": frame,
        "config": _artifact_config_identity(module, config), "context": context,
    })
    cached = transaction.get(namespace, key)
    if cached is not None:
        try:
            if not isinstance(cached, dict) or not isinstance(cached.get("bundle"), AnalysisBundle):
                raise CacheIdentityError("invalid module artifact envelope")
            result = _rebind_module_artifact(cached["bundle"], module, cached["source_run_id"], config, context)
            transaction.audit["stages"].append({
                "stage": namespace, "outcome": "artifact_reused", "key": key,
                "source_run_id": cached["source_run_id"], "metric_ids": list(config.get("metric_ids", [])),
                "tables": list(result.tables), "algorithm_version": version,
            })
            return result
        except (CacheIdentityError, ValueError, TypeError, KeyError) as exc:
            transaction.audit["stages"].append({"stage": namespace, "outcome": "miss", "reason": str(exc)})
    result = calculate()
    # AOI artifacts currently include shared geometry-budget counters scoped by
    # run. Their complete audit is retained in results but not cached by guessing
    # counter ownership. All other complete artifacts retain every named table.
    eligible = (result.metadata.get("execution_status") == "complete"
                and "aoi_method_audit" not in result.metadata)
    if eligible:
        transaction.put(namespace, key, {"source_run_id": config["run_id"], "bundle": result})
    transaction.audit["stages"].append({
        "stage": namespace, "outcome": "artifact_calculated", "key": key,
        "metric_ids": list(config.get("metric_ids", [])), "tables": list(result.tables),
        "algorithm_version": version,
        "retention_eligible": eligible,
        "reason": "complete_artifact" if eligible else "incomplete_or_shared_aoi_budget_audit",
    })
    return result


def run_analysis(
    dataset: NormalizedDataset,
    config: AnalysisConfig | Mapping[str, Any] | None = None,
    *,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    execution_cache: ExecutionCache | None = None,
    cache_eligible: Callable[[], bool] | None = None,
    _cache_transaction: CacheTransaction | None = None,
) -> AnalysisBundle:
    """Run current selected work; optionally retain only complete bounded artifacts."""
    if execution_cache is None and _cache_transaction is None:
        return _run_analysis_impl(dataset, config, cancel=cancel, progress=progress)
    settings = _coerce_config(config)
    if execution_cache is not None and not isinstance(execution_cache, ExecutionCache):
        raise PipelineError("execution_cache must be ExecutionCache or None")
    transaction = _cache_transaction or execution_cache.begin(
        run_id=settings.run_id, phase="run", cancel=cancel, eligible=cache_eligible)
    result = None
    try:
        result = _run_analysis_impl(dataset, settings, cancel=cancel, progress=progress,
                                     _cache_transaction=transaction)
        return result
    finally:
        if _cache_transaction is None:
            transaction.finish(complete=result is not None and result.metadata.get("execution_status") == "complete")
        if result is not None:
            result.metadata["execution_cache"] = transaction.audit


def _run_analysis_impl(
    dataset: NormalizedDataset,
    config: AnalysisConfig | Mapping[str, Any] | None = None,
    *,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    _cache_transaction: CacheTransaction | None = None,
) -> AnalysisBundle:
    """Run shared processing, individual/group/advanced analysis, and protected statistics."""
    from .advanced_signals import ADVANCED_CHILD_SPECS, ADVANCED_METHODS, compute_advanced_metrics
    from .group import analyze_group
    from .individual import analyze_individual
    from .statistics import run_statistics
    from .aoi_geometry import AOIGeometryBudget

    settings = _coerce_config(config)
    aoi_geometry_budget = AOIGeometryBudget(cancel_check=cancel)
    try:
        prepared = _prepare_preflight(dataset, settings, cancel=cancel,
                                      progress=_stage_progress(progress, 0.0, 0.35),
                                      **({"_cache_transaction": _cache_transaction} if _cache_transaction is not None else {}))
    except Exception as exc:
        result = _bundle_from_tables(
            tables={"metrics": pd.DataFrame(columns=METRIC_COLUMNS)},
            metadata={"run_id": settings.run_id, "contract_version": CONTRACT_VERSION,
                      "execution_status": "failed", "failure_stage": "shared_processing",
                      "failure_type": type(exc).__name__, "failure_reason": str(exc),
                      "device_verified": False},
            logs=[f"shared_processing_failed:{type(exc).__name__}:{exc}"],
        )
        if progress:
            progress(1.0, "共享预处理失败；分析已结束")
        return result

    processing = prepared.processing
    events = processing.events.copy(deep=True)
    if progress:
        progress(0.35, "正在整理共享处理与条件可用性结果")
    run_dataset = _dataset_for_run(dataset, settings)
    result_tables = _input_tables(run_dataset, settings)
    result_tables.update({
        "processed_signals": processing.signals.copy(deep=True),
        "quality": processing.quality.copy(deep=True),
        "analysis_streams": processing.analysis_streams.copy(deep=True),
        "events": events.copy(deep=True),
        "processing_partitions": processing.processing_partitions.copy(deep=True),
        "analysis_memberships": processing.analysis_memberships.copy(deep=True),
        "segment_overlap": processing.segment_overlap.copy(deep=True),
        "segment_scope_audit": processing.segment_scope_audit.copy(deep=True),
        "label_derivations": processing.label_derivations.copy(deep=True),
        "event_interval_audit": processing.event_interval_audit.copy(deep=True),
        "availability": prepared.availability.copy(deep=True),
    })
    if progress:
        progress(0.35, "共享处理与条件可用性结果已收集")
    logs = list(processing.logs)
    module_metadata: list[dict[str, Any]] = []
    collected: dict[str, list[pd.DataFrame]] = {}
    metric_frames: list[pd.DataFrame] = []
    availability_frames: list[pd.DataFrame] = [prepared.availability.copy(deep=True)]
    run_status = processing.execution_status

    if processing.execution_status == "cancelled":
        metadata = {**prepared.metadata, "execution_status": "cancelled", "module_status": [],
                    "run_id": settings.run_id, "source_manifest": list(dataset.source_manifest),
                    "dataset_metadata": _json_safe(dataset.metadata), "config": _config_snapshot(settings),
                    "device_verified": False}
        if progress:
            progress(0.35, "整理取消后保留的共享处理审计资料")
        result = _bundle_from_tables(tables=result_tables, metadata=metadata, logs=logs)
        if progress:
            progress(1.0, "分析已取消；保留共享处理产生的审计资料")
        return result

    stream_ids = (
        processing.analysis_streams["analysis_stream_id"].dropna().astype(str).drop_duplicates().tolist()
        if "analysis_stream_id" in processing.analysis_streams else []
    )
    joined_streams = {
        stream_id: _join_analysis_stream(
            processing.signals,
            processing.analysis_streams.loc[
                processing.analysis_streams["analysis_stream_id"].astype("string").eq(stream_id).fillna(False)
            ],
        )
        for stream_id in stream_ids
    }
    selected_individual = _catalogue_selection(
        get_individual_catalogue(), settings.individual.get("metric_ids"), default_tiers=None,
    )
    grouped_individual = _individual_groups(selected_individual, get_individual_catalogue())
    if settings.run_individual:
        if progress:
            progress(0.36, "准备个体指标分析")
        work: list[tuple[str | None, pd.DataFrame, list[str]]] = [
            (stream_id, frame, grouped_individual["gaze"])
            for stream_id, frame in joined_streams.items() if grouped_individual["gaze"]
        ]
        base_ids = grouped_individual["pupil"] + grouped_individual["shared"]
        if base_ids:
            work.append((None, processing.signals.copy(deep=True), base_ids))
        elif not work and not selected_individual:
            # Preserve the individual engine's empty-selection contract, but
            # invoke it only for an actually empty request, not for each
            # unrelated stream in a pupil-only dispatch.
            work.append((None, processing.signals.copy(deep=True), []))
        for index, (stream_id, frame, metric_ids) in enumerate(work):
            if _cancelled(cancel):
                run_status = "cancelled"
                break
            if progress:
                phase_start = 0.36 + .12 * index / max(1, len(work))
                progress(phase_start, f"准备个体指标：{stream_id or '全部数据'}")
            module_config = _module_config(run_dataset, settings, settings.individual,
                                           _event_stream(events, stream_id), stream_id=stream_id)
            module_config["metric_ids"] = metric_ids
            try:
                result = _invoke_module_artifact(_cache_transaction, "individual", frame, module_config, {},
                    lambda: analyze_individual(frame, module_config, run_dataset.segments, run_dataset.aois,
                                            cancel=cancel,
                                            aoi_geometry_budget=aoi_geometry_budget,
                                            progress=_stage_progress(progress, 0.36 + .12 * index / max(1, len(work)),
                                                                     .12 / max(1, len(work)))))
            except Exception as exc:
                run_status = "failed"
                logs.append(f"individual_failed:{stream_id or 'source'}:{type(exc).__name__}:{exc}")
                module_metadata.append({"module": "individual", "analysis_stream_id": stream_id,
                                        "execution_status": "failed", "failure_type": type(exc).__name__,
                                        "failure_reason": str(exc)})
                break
            status = _collect_module(result, module="individual", stream_id=stream_id,
                                     module_metadata=module_metadata, tables=collected,
                                     metrics=metric_frames, availability=availability_frames)
            if status in {"cancelled", "failed"}:
                run_status = status
                break
            if status == "partial" and run_status == "complete":
                run_status = "partial"
        if progress:
            progress(0.48, f"个体分析阶段结束：{run_status}")
    else:
        logs.append("individual_module_disabled")
        if progress:
            progress(0.48, "个体分析未启用，跳过该阶段")

    if run_status not in {"cancelled", "failed"} and settings.run_group:
        if progress:
            progress(0.52, "准备群体分析")
        group_catalogue = get_group_catalogue()
        selected_group = _catalogue_selection(group_catalogue, settings.group.get("metric_ids"), default_tiers={"R1"})
        comparison_signal = settings.group.get("comparison_signal")
        if isinstance(comparison_signal, str) and comparison_signal.startswith("pupil_"):
            group_work = [(None, processing.signals.copy(deep=True), selected_group)]
        else:
            group_work = [(stream_id, frame, selected_group) for stream_id, frame in joined_streams.items()]
        for index, (stream_id, frame, metric_ids) in enumerate(group_work):
            if _cancelled(cancel):
                run_status = "cancelled"
                break
            if "interaction_id" not in frame or frame["interaction_id"].isna().any():
                logs.append(f"group_skipped_explicit_interaction_id_required:{stream_id or 'source'}")
                if progress:
                    phase_end = .52 + .10 * (index + 1) / max(1, len(group_work))
                    progress(phase_end, f"群体分析跳过：缺少明确 interaction_id（{stream_id or '全部数据'}）")
                continue
            if progress:
                phase_start = .52 + .10 * index / max(1, len(group_work))
                progress(phase_start, f"准备群体指标：{stream_id or '全部数据'}")
            module_config = _module_config(run_dataset, settings, settings.group,
                                           _event_stream(events, stream_id), stream_id=stream_id)
            module_config["metric_ids"] = metric_ids
            try:
                result = _invoke_module_artifact(_cache_transaction, "group", frame, module_config, {},
                    lambda: analyze_group(frame, module_config, run_dataset.segments, run_dataset.aois,
                                       cancel=cancel,
                                       aoi_geometry_budget=aoi_geometry_budget,
                                       progress=_stage_progress(progress, .52 + .10 * index / max(1, len(group_work)),
                                                                .10 / max(1, len(group_work)))))
            except Exception as exc:
                run_status = "failed"
                logs.append(f"group_failed:{stream_id or 'source'}:{type(exc).__name__}:{exc}")
                module_metadata.append({"module": "group", "analysis_stream_id": stream_id,
                                        "execution_status": "failed", "failure_type": type(exc).__name__,
                                        "failure_reason": str(exc)})
                break
            status = _collect_module(result, module="group", stream_id=stream_id,
                                     module_metadata=module_metadata, tables=collected,
                                     metrics=metric_frames, availability=availability_frames)
            if status in {"cancelled", "failed"}:
                run_status = status
                break
            if status == "partial" and run_status == "complete":
                run_status = "partial"
        if progress:
            progress(.62, f"群体分析阶段结束：{run_status}")
    elif not settings.run_group:
        logs.append("group_module_disabled")
        if run_status not in {"cancelled", "failed"} and progress:
            progress(.62, "群体分析未启用，跳过该阶段")

    if run_status not in {"cancelled", "failed"} and settings.run_advanced:
        if progress:
            progress(.63, "准备高级眼动分析")
        requested_advanced = settings.advanced.get("metric_ids")
        advanced_selection = _catalogue_selection(list(ADVANCED_METHODS) + list(ADVANCED_CHILD_SPECS),
                                                  requested_advanced, default_tiers={"R1", "R2"})
        by_id = {spec["metric_id"]: spec for spec in list(ADVANCED_METHODS) + list(ADVANCED_CHILD_SPECS)}
        selected_gaze: list[str] = []
        selected_base: list[str] = []
        for metric_id in advanced_selection:
            parent = by_id[metric_id].get("parent_metric_id") or metric_id
            if parent.startswith("individual.saccade") or parent.startswith("individual.pursuit"):
                selected_gaze.append(metric_id)
            else:
                selected_base.append(metric_id)
        advanced_work = [(stream_id, frame, selected_gaze) for stream_id, frame in joined_streams.items() if selected_gaze]
        if selected_base:
            advanced_work.append((None, processing.signals.copy(deep=True), selected_base))
        for index, (stream_id, frame, metric_ids) in enumerate(advanced_work):
            if _cancelled(cancel):
                run_status = "cancelled"
                break
            if progress:
                phase_start = .63 + .11 * index / max(1, len(advanced_work))
                progress(phase_start, f"准备高级眼动指标：{stream_id or '全部数据'}")
            module_config = _module_config(run_dataset, settings, settings.advanced,
                                           _event_stream(events, stream_id), stream_id=stream_id)
            module_config["metric_ids"] = metric_ids
            module_context = {
                "segments": run_dataset.segments,
                "target_movement_intervals": settings.context.get("target_movement_intervals", pd.DataFrame()),
                "eyelid_intervals": settings.context.get("eyelid_intervals", pd.DataFrame()),
            }
            try:
                result = _invoke_module_artifact(_cache_transaction, "advanced", frame, module_config, module_context,
                    lambda: compute_advanced_metrics(frame, module_config, events=_event_stream(events, stream_id),
                                                  context=module_context, cancel=cancel,
                                                  progress=_stage_progress(progress, .63 + .11 * index / max(1, len(advanced_work)),
                                                                           .11 / max(1, len(advanced_work)))))
            except Exception as exc:
                run_status = "failed"
                logs.append(f"advanced_failed:{stream_id or 'source'}:{type(exc).__name__}:{exc}")
                module_metadata.append({"module": "advanced", "analysis_stream_id": stream_id,
                                        "execution_status": "failed", "failure_type": type(exc).__name__,
                                        "failure_reason": str(exc)})
                break
            status = _collect_module(result, module="advanced", stream_id=stream_id,
                                     module_metadata=module_metadata, tables=collected,
                                     metrics=metric_frames, availability=availability_frames)
            if status in {"cancelled", "failed"}:
                run_status = status
                break
            if status == "partial" and run_status == "complete":
                run_status = "partial"
        if progress:
            progress(.74, f"高级眼动分析阶段结束：{run_status}")
    elif not settings.run_advanced:
        logs.append("advanced_module_disabled")
        if run_status not in {"cancelled", "failed"} and progress:
            progress(.74, "高级眼动分析未启用，跳过该阶段")

    if run_status not in {"cancelled", "failed"}:
        if progress:
            progress(.76, "正在进行统计计算")
        combined_metrics = pd.concat(metric_frames, ignore_index=True, sort=False) if metric_frames else pd.DataFrame(columns=METRIC_COLUMNS)
        stats_design: DesignSpec | Mapping[str, Any] = (
            settings.statistics if settings.statistics is not None else DesignSpec()
        )
        status_before_statistics = run_status
        try:
            statistics_result = run_statistics(
                combined_metrics,
                stats_design,
                performance=run_dataset.performance,
                cancel=cancel,
                progress=_stage_progress(progress, .76, .22),
            )
            status, runner_status = _statistics_completion_status(
                statistics_result,
                design_supplied=settings.statistics is not None,
                metrics=combined_metrics,
            )
            if progress and status not in {"cancelled", "failed"}:
                progress(.98, "统计计算结束，正在整理统计结果")
            module_metadata.append({
                "module": "statistics",
                **statistics_result.metadata,
                "pipeline_completion_status": status,
                "statistics_runner_execution_status": runner_status,
                "pipeline_status_before_statistics": status_before_statistics,
                "statistics_logs": list(statistics_result.logs),
                "design_supplied": settings.statistics is not None,
            })
            for name, frame in statistics_result.tables.items():
                collected.setdefault(f"statistics_{name}", []).append(frame.copy(deep=True))
            if status == "cancelled":
                run_status = "cancelled"
            elif status == "failed":
                run_status = "failed"
            elif status == "partial" and run_status == "complete":
                run_status = "partial"
            logs.extend(statistics_result.logs)
        except Exception as exc:
            run_status = "failed"
            logs.append(f"statistics_failed:{type(exc).__name__}:{exc}")
            module_metadata.append({"module": "statistics", "execution_status": "failed",
                                    "pipeline_completion_status": "failed",
                                    "statistics_runner_execution_status": "raised",
                                    "pipeline_status_before_statistics": status_before_statistics,
                                    "design_supplied": settings.statistics is not None,
                                    "failure_type": type(exc).__name__, "failure_reason": str(exc)})

    if progress:
        progress(.99, "正在汇总个体、群体、高级与统计结果表")
    for name, frames in collected.items():
        if frames:
            result_tables[name] = pd.concat(frames, ignore_index=True, sort=False)
    if metric_frames:
        result_tables["metrics"] = pd.concat(metric_frames, ignore_index=True, sort=False)
    else:
        result_tables["metrics"] = pd.DataFrame(columns=METRIC_COLUMNS)
    if availability_frames:
        result_tables["availability"] = pd.concat(availability_frames, ignore_index=True, sort=False)

    if progress:
        progress(.99, "分析结果表已整理，正在构建运行记录")
    metadata = {
        "run_id": settings.run_id,
        "contract_version": CONTRACT_VERSION,
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "execution_status": run_status,
        "processing_audit": processing.audit,
        "module_status": module_metadata,
        "parameter_snapshot": _config_snapshot(settings),
        "source_manifest": list(dataset.source_manifest),
        "dataset_metadata": _json_safe(dataset.metadata),
        "device_verified": _module_metadata(dataset, settings).get("device_verified") is True,
        "data_device_evidence_status": "declared" if _module_metadata(dataset, settings).get("device_verified") is True else "unverified",
        "context_table_names": sorted(name for name, value in settings.context.items() if isinstance(value, pd.DataFrame)),
    }
    if settings.statistics is None:
        metadata["statistics_design_status"] = "unknown_design_descriptive_only"
    result = _bundle_from_tables(tables=result_tables, metadata=metadata, logs=logs)
    if progress:
        if run_status == "complete":
            message = "分析完成"
        elif run_status == "failed":
            message = "分析失败；请检查日志和失败模块"
        else:
            message = f"分析结束：{run_status}"
        progress(1.0, message)
    return result


def _config_snapshot(
    config: AnalysisConfig,
    config_tables: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    config_tables = config_tables if config_tables is not None else {}
    snapshot = {
        "run_id": config.run_id,
        "processing": config.processing.to_dict(),
        "eye_selection": config.eye_selection.to_dict(),
        "event_detection": _plain(config.event_detection),
        "scope": _plain(config.scope),
        "individual": _snapshot_value(config.individual, "individual", config_tables),
        "group": _snapshot_value(config.group, "group", config_tables),
        "advanced": _snapshot_value(config.advanced, "advanced", config_tables),
        "statistics": config.statistics.to_dict() if isinstance(config.statistics, DesignSpec) else _plain(config.statistics),
        "metadata": _snapshot_value(config.metadata, "metadata", config_tables),
        "context": _snapshot_value(config.context, "context", config_tables),
        "run_individual": config.run_individual,
        "run_group": config.run_group,
        "run_advanced": config.run_advanced,
    }
    snapshot["_config_tables"] = sorted(config_tables)
    return snapshot


def _snapshot_value(value: Any, prefix: str, tables: dict[str, pd.DataFrame]) -> Any:
    if isinstance(value, pd.DataFrame):
        name = f"config.{prefix}"
        tables[name] = value.copy(deep=True)
        return {"__pyea_table_ref__": name}
    if is_dataclass(value):
        return {item.name: _snapshot_value(getattr(value, item.name), f"{prefix}.{item.name}", tables)
                for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _snapshot_value(item, f"{prefix}.{key}", tables) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item, f"{prefix}.{index}", tables) for index, item in enumerate(value)]
    return _json_safe(value)


def _safe_table_stem(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("._")
    return cleaned[:120] or "table"


def _software_hashes() -> dict[str, str]:
    import sys
    if getattr(sys, "frozen", False):
        from .app_info import APP_NAME, APP_VERSION, PRD_VERSION
        from .build_identity import load_build_identity
        return load_build_identity(
            application={"name": APP_NAME, "version": APP_VERSION, "prd_version": PRD_VERSION},
            contracts={"data_contract_version": CONTRACT_VERSION,
                       "pipeline_schema_version": PIPELINE_SCHEMA_VERSION},
        ).software_sha256()
    package_dir = Path(__file__).resolve().parent
    output: dict[str, str] = {}
    for source in sorted(package_dir.glob("*.py")):
        output[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    for source in sorted((package_dir / "methods").glob("*.py")):
        output[f"methods/{source.name}"] = hashlib.sha256(source.read_bytes()).hexdigest()
    return output


def _add_table_member(
    name: str, frame: pd.DataFrame, members: dict[str, bytes], schemas: dict[str, dict[str, Any]],
    table_index: dict[str, dict[str, Any]],
) -> str:
    token = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:10]
    member = f"tables/{_safe_table_stem(name)}.{token}.csv"
    if len(frame.columns) == 0:
        member = f"tables/{_safe_table_stem(name)}.{token}.json"
        content = json.dumps({"columns": [], "rows": len(frame)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        members[member] = content
        table_index[name] = {"member": member, "encoding": "json-empty-frame",
                             "sha256": hashlib.sha256(content).hexdigest(), "row_count": len(frame)}
        return member
    content, schema = dataframe_to_csv_member(frame)
    members[member] = content
    schemas[member] = schema
    table_index[name] = {"member": member, "encoding": "csv", "sha256": hashlib.sha256(content).hexdigest()}
    return member


def _archive_config_value(
    value: Any, prefix: str, members: dict[str, bytes], schemas: dict[str, dict[str, Any]],
    table_index: dict[str, dict[str, Any]],
) -> Any:
    if isinstance(value, pd.DataFrame):
        name = f"config.{prefix}"
        _add_table_member(name, value, members, schemas, table_index)
        return {"__pyea_table_ref__": name}
    if is_dataclass(value):
        return {item.name: _archive_config_value(getattr(value, item.name), f"{prefix}.{item.name}", members, schemas, table_index)
                for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _archive_config_value(item, f"{prefix}.{key}", members, schemas, table_index)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_archive_config_value(item, f"{prefix}.{index}", members, schemas, table_index)
                for index, item in enumerate(value)]
    return _json_safe(value)


def _resolve_config_tables(value: Any, tables: Mapping[str, pd.DataFrame]) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {"__pyea_table_ref__"}:
            name = value["__pyea_table_ref__"]
            if name not in tables:
                raise ArchiveError(f"Config references missing table {name!r}")
            return tables[name].copy(deep=True)
        return {str(key): _resolve_config_tables(item, tables) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_config_tables(item, tables) for item in value]
    return value


def save_analysis(
    path: str | os.PathLike[str],
    result: AnalysisBundle,
    *,
    dataset: NormalizedDataset | None = None,
    config: AnalysisConfig | Mapping[str, Any] | None = None,
    mode: Literal["full", "results_only"] = "full",
    raw_sources: Mapping[str, bytes | bytearray | memoryview | str | os.PathLike[str]] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> PyeaArchive:
    """Persist an integrated run using the checked atomic `.pyea` archive bridge."""
    if not isinstance(result, AnalysisBundle):
        raise PipelineError("result must be an AnalysisBundle")
    if mode not in {"full", "results_only"}:
        raise PipelineError("mode must be full or results_only")
    settings = _coerce_config(config) if config is not None else None
    if mode == "full" and dataset is None:
        raise PipelineError("full archives require the original NormalizedDataset")
    members: dict[str, bytes | str | os.PathLike[str]] = {}
    schemas: dict[str, dict[str, Any]] = {}
    table_index: dict[str, dict[str, Any]] = {}
    input_index: dict[str, dict[str, Any]] = {}
    if mode == "full" and dataset is not None:
        dataset.validate()
        for name, frame in (("signals", dataset.signals), ("segments", dataset.segments),
                            ("aois", dataset.aois), ("performance", dataset.performance)):
            member = _add_table_member(f"input.{name}", frame, members, schemas, table_index)
            input_index[name] = table_index[f"input.{name}"]
    for name, frame in result.tables.items():
        if not isinstance(frame, pd.DataFrame):
            raise PipelineError(f"result table {name!r} is not a DataFrame")
        _add_table_member(f"result.{name}", frame, members, schemas, table_index)
    config_tables: dict[str, pd.DataFrame] = {}
    config_snapshot = (
        _config_snapshot(settings, config_tables)
        if settings is not None else result.metadata.get("parameter_snapshot", {})
    )
    for name, frame in config_tables.items():
        _add_table_member(name, frame, members, schemas, table_index)
    config_snapshot = _archive_config_value(config_snapshot, "root", members, schemas, table_index)
    raw_index: list[dict[str, Any]] = []
    for source_name, source in (raw_sources or {}).items():
        if cancel and cancel():
            raise ArchiveError("Archive save cancelled before writing; existing destination is untouched")
        basename = Path(str(source_name)).name
        if not basename or basename in {".", ".."}:
            raise PipelineError("raw source names must have a safe basename")
        member = f"raw_sources/{_safe_table_stem(basename)}"
        if isinstance(source, (bytes, bytearray, memoryview)):
            payload = bytes(source)
        elif isinstance(source, os.PathLike):
            payload = Path(source).read_bytes()
        elif isinstance(source, str):
            candidate = Path(source)
            payload = candidate.read_bytes() if candidate.exists() else source.encode("utf-8")
        else:
            raise PipelineError(f"Unsupported raw source value for {source_name!r}")
        if member in members:
            raise PipelineError(f"Duplicate raw source archive name: {basename}")
        members[member] = payload
        raw_index.append({"name": basename, "member": member, "sha256": hashlib.sha256(payload).hexdigest(),
                          "byte_size": len(payload)})

    source_manifest = list(dataset.source_manifest) if dataset is not None else list(result.metadata.get("source_manifest", []))
    table_fingerprints = {name: record["sha256"] for name, record in table_index.items()}
    metadata = {
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "run_id": result.metadata.get("run_id"),
        "execution_status": result.metadata.get("execution_status"),
        "recomputable": mode == "full",
        "mode_note": "results_only archives do not include inputs and cannot be recomputed" if mode == "results_only" else None,
        "dataset_metadata": _json_safe(dataset.metadata) if dataset is not None else result.metadata.get("dataset_metadata", {}),
        "source_manifest": _json_safe(source_manifest),
        "original_raw_sha256": [entry.get("sha256", entry.get("content_sha256")) for entry in source_manifest if isinstance(entry, Mapping)],
        "raw_sources_included": bool(raw_index),
        "raw_sources": raw_index,
        "inputs": input_index,
        "result_tables": sorted(name for name in result.tables),
        "table_index": table_index,
        "table_fingerprints": table_fingerprints,
        "software_sha256": _software_hashes(),
        "analysis_metadata": _json_safe(result.metadata),
        "analysis_logs": list(result.logs),
        "config": config_snapshot,
    }
    if cancel and cancel():
        raise ArchiveError("Archive save cancelled before writing; existing destination is untouched")
    destination = Path(path).expanduser().absolute()
    if destination.suffix.casefold() != ".pyea":
        raise ArchiveError("Archive filename must use the .pyea suffix")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.stage.pyea")
    try:
        write_pyea(staging, members, mode=mode, metadata=metadata, member_schemas=schemas)
        if cancel and cancel():
            raise ArchiveError("Archive save cancelled before replacement; existing destination is untouched")
        os.replace(staging, destination)
        return read_pyea(destination)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


def load_analysis(path: str | os.PathLike[str]) -> LoadedAnalysis:
    """Validate and reopen a `.pyea` run, including typed tables and full inputs."""
    archive = read_pyea(path)
    metadata = archive.manifest.get("metadata", {})
    table_index = metadata.get("table_index", {})
    if not isinstance(table_index, Mapping):
        raise ArchiveError("Integrated archive table index is missing or invalid")
    tables: dict[str, pd.DataFrame] = {}
    for name, record in table_index.items():
        if not isinstance(record, Mapping) or not isinstance(record.get("member"), str):
            raise ArchiveError(f"Invalid table index entry {name!r}")
        member = record["member"]
        member_info = archive.manifest.get("members", {}).get(member)
        if not isinstance(member_info, Mapping):
            raise ArchiveError(f"Missing table member {member!r}")
        content = archive.read_member(member)
        encoding = record.get("encoding", "csv")
        if encoding == "json-empty-frame":
            try:
                empty_frame = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArchiveError(f"Invalid empty-frame table member {member!r}") from exc
            if (
                not isinstance(empty_frame, dict)
                or set(empty_frame) != {"columns", "rows"}
                or empty_frame.get("columns") != []
                or isinstance(empty_frame.get("rows"), bool)
                or not isinstance(empty_frame.get("rows"), int)
                or empty_frame["rows"] < 0
                or ("row_count" in record and record["row_count"] != empty_frame["rows"])
            ):
                raise ArchiveError(f"Invalid empty-frame table descriptor {member!r}")
            table = pd.DataFrame(index=range(empty_frame["rows"]))
        elif encoding == "csv":
            if not isinstance(member_info.get("schema"), Mapping):
                raise ArchiveError(f"Missing typed schema for table member {member!r}")
            table = dataframe_from_csv_member(content, member_info["schema"])
        else:
            raise ArchiveError(f"Unsupported table encoding {encoding!r} for {member!r}")
        fingerprint = record.get("sha256")
        if fingerprint and hashlib.sha256(content).hexdigest() != fingerprint:
            raise ArchiveError(f"Table fingerprint mismatch: {name}")
        tables[str(name)] = table
    result_tables: dict[str, pd.DataFrame] = {}
    for name in metadata.get("result_tables", []):
        key = f"result.{name}"
        if key not in tables:
            raise ArchiveError(f"Required result table is missing: {name}")
        result_tables[str(name)] = tables[key]
    analysis_metadata = metadata.get("analysis_metadata", {})
    if not isinstance(analysis_metadata, dict):
        raise ArchiveError("Integrated archive analysis metadata is invalid")
    analysis_metadata = dict(analysis_metadata)
    analysis_metadata.setdefault("execution_status", metadata.get("execution_status", "failed"))
    result = AnalysisBundle(result_tables, analysis_metadata, list(metadata.get("analysis_logs", [])))

    dataset: NormalizedDataset | None = None
    if archive.mode == "full":
        inputs = metadata.get("inputs", {})
        try:
            input_frames = {name: tables[f"input.{name}"] for name in ("signals", "segments", "aois", "performance")}
        except KeyError as exc:
            raise ArchiveError(f"Full archive is missing input table {exc.args[0]}") from exc
        dataset_metadata = metadata.get("dataset_metadata", {})
        source_manifest = metadata.get("source_manifest", [])
        if not isinstance(dataset_metadata, dict) or not isinstance(source_manifest, list):
            raise ArchiveError("Full archive dataset metadata or source manifest is invalid")
        dataset = NormalizedDataset(
            signals=input_frames["signals"], metadata=dataset_metadata,
            segments=input_frames["segments"], aois=input_frames["aois"],
            performance=input_frames["performance"], source_manifest=source_manifest,
        )
        dataset.validate()
    resolved_config = _resolve_config_tables(metadata.get("config", {}), tables)
    if isinstance(resolved_config, dict):
        # This private write-time index is archive machinery, not a public
        # AnalysisConfig option. Resolved DataFrames remain in the config.
        resolved_config.pop("_config_tables", None)
    raw_sources: dict[str, bytes] = {}
    for record in metadata.get("raw_sources", []):
        if not isinstance(record, Mapping) or not isinstance(record.get("member"), str):
            raise ArchiveError("Invalid raw-source index entry")
        payload = archive.read_member(record["member"])
        if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
            raise ArchiveError("Raw-source checksum differs from the integrated raw manifest")
        raw_sources[str(record.get("name", record["member"]))] = payload
    return LoadedAnalysis(archive=archive, dataset=dataset, result=result,
                          config=resolved_config if isinstance(resolved_config, dict) else {},
                          raw_sources=raw_sources)
