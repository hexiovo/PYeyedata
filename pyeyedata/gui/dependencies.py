"""GUI-owned dependency fingerprints and precise stale-state reporting.

This module does not expose a backend DAG, reuse prepared pipeline stages, or cache
science. It tracks which displayed results no longer match the current immutable
inputs and parameters. Named table references should be passed at their narrowest
real support (for example ``pupil.right`` rather than only one whole-table hash).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class DependencyChange:
    """Result of comparing current GUI configuration with its prior state."""

    changed_nodes: tuple[str, ...]
    newly_stale_nodes: tuple[str, ...]
    stale_nodes: tuple[str, ...]
    changed_inputs: Mapping[str, tuple[str, ...]]
    current_fingerprint: str


@dataclass(frozen=True, slots=True)
class RunDependencyRecord:
    """Read-only dependency identity for a historical run."""

    run_id: str
    status: str
    current_fingerprint: str
    node_fingerprints: Mapping[str, str]
    created_order: int


def _value_for_hash(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return {"$missing": type(value).__name__}
    if isinstance(value, np.generic):
        return _value_for_hash(value.item())
    if isinstance(value, pd.Timestamp):
        return {"$datetime": value.isoformat()}
    if isinstance(value, (datetime, date)):
        return {"$datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": str(value)}
    if isinstance(value, pd.DataFrame):
        digest = hashlib.sha256()
        digest.update(json.dumps([str(column) for column in value.columns], ensure_ascii=False).encode("utf-8"))
        digest.update(json.dumps([str(dtype) for dtype in value.dtypes], ensure_ascii=False).encode("utf-8"))
        for row in value.itertuples(index=True, name=None):
            digest.update(json.dumps([_value_for_hash(item) for item in row], sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            digest.update(b"\n")
        return {"$dataframe_sha256": digest.hexdigest(), "rows": len(value), "columns": list(map(str, value.columns))}
    if isinstance(value, pd.Series):
        return _value_for_hash(value.to_frame())
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            mapped = value.to_dict()
        except (TypeError, ValueError):
            mapped = None
        if isinstance(mapped, Mapping):
            return _value_for_hash(mapped)
    if isinstance(value, Mapping):
        return {str(key): _value_for_hash(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_value_for_hash(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_value_for_hash(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    return {"$type": type(value).__qualname__, "$repr": str(value)}


def _digest(value: Any) -> str:
    packed = json.dumps(_value_for_hash(value), sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _flatten(value: Any, prefix: str) -> dict[str, Any]:
    """Flatten mappings while keeping DataFrames/typed objects as atomic leaves."""
    result: dict[str, Any] = {}
    if hasattr(value, "to_dict") and callable(value.to_dict) and not isinstance(value, (pd.DataFrame, pd.Series)):
        try:
            mapped = value.to_dict()
        except (TypeError, ValueError):
            mapped = None
        if isinstance(mapped, Mapping):
            value = mapped
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, Mapping) or (hasattr(item, "to_dict") and callable(item.to_dict)):
                result.update(_flatten(item, path))
            else:
                result[path] = item
    elif prefix:
        result[prefix] = value
    return result


def _path_matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + ".") or (prefix.endswith("_") and path.startswith(prefix))


def _selected(flat: Mapping[str, Any], prefixes: Iterable[str], *, exact: Iterable[str] = ()) -> dict[str, Any]:
    prefix_values = tuple(prefixes)
    exact_values = set(exact)
    return {
        path: value
        for path, value in flat.items()
        if path in exact_values or any(_path_matches(path, prefix) for prefix in prefix_values)
    }


def _under(flat: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    return {path[len(prefix) + 1:]: value for path, value in flat.items() if _path_matches(path, prefix)}


def _choice_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if item is not None and str(item))
    return ()


def _signal_projection(frame: pd.DataFrame, branch: str) -> pd.DataFrame:
    """Hash only fields consumed by one stream when a real normalized table is supplied."""
    id_fields = {
        "source_id", "observation_id", "original_index", "subject_id", "session_id", "interaction_id",
        "trial", "stimulus_id", "segment_id", "segment_instance_id", "physical_partition_id",
        "time_s", "time_valid", "clock_id", "timebase_id", "frame_id", "frame_id_source",
        "condition", "between_group", "source_manifest_ref",
    }
    if branch == "gaze":
        keep = id_fields | {name for name in frame.columns if "gaze" in str(name).casefold() or "tracking" in str(name).casefold()}
    elif branch == "pupil.left":
        keep = id_fields | {name for name in frame.columns if "pupil_left" in str(name).casefold() or "left_pupil" in str(name).casefold()}
    elif branch == "pupil.right":
        keep = id_fields | {name for name in frame.columns if "pupil_right" in str(name).casefold() or "right_pupil" in str(name).casefold()}
    else:
        keep = set(frame.columns)
    columns = [column for column in frame.columns if column in keep]
    return frame.loc[:, columns].copy(deep=True)


def _branch_selected(flat: Mapping[str, Any], prefixes: Iterable[str], branch: str) -> dict[str, Any]:
    """Select stream inputs and project a whole signal DataFrame if available."""
    result: dict[str, Any] = {}
    for path, value in flat.items():
        if not any(_path_matches(path, prefix) for prefix in prefixes):
            continue
        if isinstance(value, pd.DataFrame) and path.casefold().endswith(("signals", "signal_table")):
            value = _signal_projection(value, branch)
        result[path] = value
    return result


def _canonical_metric_id_list(parameters: Mapping[str, Any], table_refs: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    groups: dict[str, tuple[str, ...]] = {}
    selection = parameters.get("selection", {}) if isinstance(parameters, Mapping) else {}
    if hasattr(selection, "to_dict") and callable(selection.to_dict):
        selection = selection.to_dict()
    for category in ("individual", "group", "advanced"):
        candidates = [
            selection.get(f"{category}_metric_ids") if isinstance(selection, Mapping) else None,
            parameters.get(f"{category}_metric_ids"),
        ]
        local = parameters.get(category)
        if isinstance(local, Mapping):
            candidates.append(local.get("metric_ids"))
        selected = next((_choice_values(candidate) for candidate in candidates if candidate is not None), ())
        groups[category] = tuple(dict.fromkeys(selected))
    ref_ids = table_refs.get("metric_ids") if isinstance(table_refs, Mapping) else None
    if isinstance(ref_ids, Mapping):
        for category in groups:
            if not groups[category] and category in ref_ids:
                groups[category] = _choice_values(ref_ids[category])
    return groups


def _metric_kind(metric_id: str, namespace: str, parameters: Mapping[str, Any]) -> tuple[str, ...]:
    """Return backend-stage nodes consumed by a known metric; conservative for unknown IDs."""
    key = metric_id.casefold()
    if namespace == "group":
        if "pupil" in key:
            parents = ["processing.pupil.left", "processing.pupil.right", "group.membership"]
            if "lag" in key:
                parents.append("context.lag")
            return tuple(parents)
        if "lag" in key:
            return ("processing.gaze", "group.membership", "context.lag")
        if "synchron" in key:
            return ("processing.gaze", "group.membership", "context.sync")
        if "aoi" in key or "transition" in key:
            return ("processing.gaze", "events", "context.aois", "group.membership")
        return ("processing.gaze", "group.membership")

    if "pupil" in key:
        if "interocular" in key or "bilateral" in key or "spectral_power" in key:
            if "spectral_power" in key:
                advanced = parameters.get("advanced", {})
                eye = advanced.get("pupil_psd_eye", "left") if isinstance(advanced, Mapping) else "left"
                if isinstance(advanced, Mapping):
                    for name, value in advanced.items():
                        if str(name).endswith("pupil_psd_eye"):
                            eye = value
                eye = str(eye).casefold()
                if eye == "right":
                    return ("processing.pupil.right",)
                return ("processing.pupil.left",)
            return ("processing.pupil.left", "processing.pupil.right")
        if "pupil_left" in key or "_left" in key:
            return ("processing.pupil.left",)
        if "pupil_right" in key or "_right" in key:
            return ("processing.pupil.right",)
        return ("processing.pupil.left", "processing.pupil.right")
    if "aoi" in key or "transition" in key or "heatmap" in key:
        return ("processing.gaze", "events", "context.aois")
    if any(token in key for token in ("fixation", "saccade", "scanpath", "blink", "microsaccade", "pursuit")):
        return ("processing.gaze", "events")
    if "gaze" in key or "calibration" in key or "precision" in key or "coverage" in key:
        return ("processing.gaze",)
    return ("source.identity", "source.time", "source.segments")


class DependencyTracker:
    """Compare immutable GUI input states and report exactly which result views are stale.

    ``update`` accepts semantic input/parameter namespaces. Useful narrow references
    include ``inputs.pupil.left/right``, ``inputs.gaze``, ``inputs.time`` and
    ``table_refs.pupil.left/right``. A whole-dataset digest used as the only science
    input is a safe broad fallback; raw byte hashes are provenance/archive inputs and do not
    stand in for normalized signal fingerprints.
    """

    def __init__(self) -> None:
        self._node_fingerprints: dict[str, str] = {}
        self._node_parents: dict[str, tuple[str, ...]] = {}
        self._baseline: dict[str, str] | None = None
        self._baseline_run_id: str | None = None
        self._records: list[RunDependencyRecord] = []
        self._inputs: dict[str, Any] = {}
        self._parameters: dict[str, Any] = {}
        self._table_refs: dict[str, Any] = {}
        self._algorithm_versions: dict[str, Any] = {}
        self._node_direct_fingerprints: dict[str, dict[str, str]] = {}

    @property
    def node_fingerprints(self) -> Mapping[str, str]:
        return dict(self._node_fingerprints)

    @property
    def stale_nodes(self) -> tuple[str, ...]:
        if self._baseline is None:
            return ()
        return tuple(sorted(
            node for node in set(self._node_fingerprints) | set(self._baseline)
            if self._node_fingerprints.get(node) != self._baseline.get(node)
        ))

    @property
    def history(self) -> tuple[RunDependencyRecord, ...]:
        return tuple(self._records)

    @property
    def baseline_run_id(self) -> str | None:
        return self._baseline_run_id

    def update(
        self,
        inputs: Mapping[str, Any],
        parameters: Mapping[str, Any],
        algorithm_versions: Mapping[str, Any] | None = None,
        table_refs: Mapping[str, Any] | None = None,
    ) -> DependencyChange:
        """Set current inputs/configuration and return changed/stale GUI nodes.

        Presentation state (search, filtering, chart zoom and logs) is deliberately not
        selected into any scientific node fingerprint.
        """
        if not isinstance(inputs, Mapping) or not isinstance(parameters, Mapping):
            raise TypeError("inputs and parameters must be mappings")
        self._inputs = dict(inputs)
        self._parameters = dict(parameters)
        self._algorithm_versions = dict(algorithm_versions or {})
        self._table_refs = dict(table_refs or {})
        input_flat = _flatten(self._inputs, "inputs")
        parameter_flat = _flatten(self._parameters, "parameters")
        version_flat = _flatten(self._algorithm_versions, "algorithm_versions")
        table_flat = _flatten(self._table_refs, "table_refs")
        ids = _canonical_metric_id_list(self._parameters, self._table_refs)
        nodes: dict[str, str] = {}
        parents: dict[str, tuple[str, ...]] = {}
        direct_fingerprints: dict[str, dict[str, str]] = {}

        def add_node(name: str, parent_names: Sequence[str], direct: Any, source_paths: Iterable[str] = ()) -> None:
            parents[name] = tuple(parent_names)
            parent_values = {parent: nodes[parent] for parent in parent_names if parent in nodes}
            nodes[name] = _digest({"node": name, "parents": parent_values, "inputs": direct})
            if isinstance(direct, Mapping):
                direct_fingerprints[name] = {str(path): _digest(value) for path, value in direct.items()}
            else:
                direct_fingerprints[name] = {str(path): _digest(direct) for path in source_paths}

        # Data sources are split into the semantically separate branches of the public pipeline.
        # Raw bytes are provenance/archive inputs; the science graph consumes the normalized
        # values and mapping. If only a coarse revision hash is available, use it as a safe fallback.
        broad_source_prefixes = ("inputs.mappings", "inputs.source_identity", "table_refs.normalized.identity",
                                 "table_refs.normalized.schema")
        time_prefixes = ("inputs.time", "inputs.time_metadata", "inputs.clock", "inputs.timebase", "table_refs.time")
        segment_prefixes = ("inputs.segments", "inputs.scope", "inputs.segment_scope", "table_refs.segments", "table_refs.normalized.segments")
        aois_prefixes = ("inputs.aois", "inputs.aoi", "table_refs.aois", "table_refs.normalized.aois")
        performance_prefixes = ("inputs.performance", "table_refs.performance", "table_refs.normalized.performance")
        gaze_prefixes = ("inputs.gaze", "inputs.gaze_signals", "inputs.signals.gaze", "inputs.normalized.gaze",
                         "table_refs.gaze", "table_refs.normalized.gaze")
        left_prefixes = ("inputs.pupil.left", "inputs.pupil_left", "inputs.left_pupil", "inputs.signals.pupil_left",
                         "inputs.normalized.pupil.left", "table_refs.pupil.left", "table_refs.normalized.pupil.left")
        right_prefixes = ("inputs.pupil.right", "inputs.pupil_right", "inputs.right_pupil", "inputs.signals.pupil_right",
                          "inputs.normalized.pupil.right", "table_refs.pupil.right", "table_refs.normalized.pupil.right")
        full_signal_paths = {
            path: value for path, value in input_flat.items()
            if path.endswith((".signals", ".signal_table"))
        }
        full_signal_table_ref_paths = {
            path: value for path, value in table_flat.items()
            if path.endswith((".signals", ".signal_table"))
        }
        # Narrow column projection for a real normalized signals DataFrame. A scalar whole-table
        # digest is retained in every branch and therefore invalidates all dependents if changed.
        def branch_direct(branch: str, prefixes: Sequence[str]) -> dict[str, Any]:
            selected = _branch_selected(input_flat, prefixes, branch)
            for path, value in full_signal_paths.items():
                selected[path] = _signal_projection(value, branch) if isinstance(value, pd.DataFrame) else value
            for path, value in full_signal_table_ref_paths.items():
                selected[path] = _signal_projection(value, branch) if isinstance(value, pd.DataFrame) else value
            return selected

        normalized_direct = _selected(input_flat, broad_source_prefixes)
        normalized_direct.update(_selected(table_flat, ("table_refs.normalized.identity", "table_refs.normalized.schema")))
        has_semantic_data_refs = bool(full_signal_paths or full_signal_table_ref_paths or any(
            _path_matches(path, prefix)
            for path in input_flat
            for prefix in ("inputs.gaze", "inputs.pupil", "inputs.pupil_left", "inputs.pupil_right")
        ) or any(_path_matches(path, prefix) for path in table_flat for prefix in (
            "table_refs.gaze", "table_refs.pupil.left", "table_refs.pupil.right")))
        if not has_semantic_data_refs:
            normalized_direct.update(_selected(input_flat, (), exact=("inputs.dataset_fingerprint", "inputs.normalized.fingerprint")))
        normalized_direct.update(_selected(version_flat, ("algorithm_versions.normalization", "algorithm_versions.contract")))
        add_node("source.identity", (), normalized_direct, normalized_direct.keys())

        time_direct = _selected(input_flat, time_prefixes, exact=("inputs.time_unit", "inputs.source_time_unit", "inputs.normalized_time_unit"))
        time_direct.update(_selected(table_flat, ("table_refs.time", "table_refs.normalized.time")))
        time_direct.update(_selected(version_flat, ("algorithm_versions.time", "algorithm_versions.normalization")))
        add_node("source.time", ("source.identity",), time_direct, time_direct.keys())

        add_node("source.segments", ("source.identity",),
                 _selected(input_flat, segment_prefixes) | _selected(table_flat, ("table_refs.segments", "table_refs.normalized.segments")),
                 segment_prefixes)
        add_node("source.gaze", ("source.identity", "source.time", "source.segments"),
                 branch_direct("gaze", gaze_prefixes), gaze_prefixes)
        add_node("source.pupil.left", ("source.identity", "source.time", "source.segments"),
                 branch_direct("pupil.left", left_prefixes), left_prefixes)
        add_node("source.pupil.right", ("source.identity", "source.time", "source.segments"),
                 branch_direct("pupil.right", right_prefixes), right_prefixes)
        add_node("source.aois", ("source.identity", "source.gaze", "source.segments"),
                 _selected(input_flat, aois_prefixes) | _selected(table_flat, aois_prefixes), aois_prefixes)
        add_node("source.performance", ("source.identity",),
                 _selected(input_flat, performance_prefixes) | _selected(table_flat, performance_prefixes), performance_prefixes)

        add_node("quality.raw", ("source.gaze", "source.pupil.left", "source.pupil.right"),
                 _selected(input_flat, ("inputs.quality", "inputs.validity")) |
                 _selected(parameter_flat, ("parameters.quality",)), ("inputs.quality", "inputs.validity"))

        processing = self._parameters.get("processing", {})
        processing_map = dict(processing) if isinstance(processing, Mapping) else {}
        gaze_processing_keys = {key: value for key, value in processing_map.items()
                                if key not in {"interpolate_pupil", "pupil_max_gap_s", "pupil_baseline_window_s"}}
        gaze_direct = {
            "parameters.processing": gaze_processing_keys,
            "parameters.eye_selection": self._parameters.get("eye_selection", {}),
        }
        gaze_direct.update(_selected(version_flat, ("algorithm_versions.processing.gaze", "algorithm_versions.eye_streams")))
        add_node("processing.gaze", ("source.gaze",), gaze_direct, gaze_direct.keys())

        pupil_processing_keys = {key: value for key, value in processing_map.items()
                                 if key in {"support_gap_factor", "support_gap_max_s", "interpolate_pupil", "pupil_max_gap_s",
                                            "max_missing_samples", "lowpass_cutoff_hz", "lowpass_order", "pupil_baseline_window_s",
                                            "baseline_min_coverage", "baseline_min_length_s", "order_policy", "duplicate_policy"}}
        for eye in ("left", "right"):
            branch = f"pupil.{eye}"
            direct = {
                "parameters.processing": pupil_processing_keys,
                f"inputs.{branch}": self._inputs.get("pupil", {}).get(eye, {}) if isinstance(self._inputs.get("pupil"), Mapping) else {},
            }
            direct.update(_selected(parameter_flat, (f"parameters.processing.pupil.{eye}", f"parameters.pupil.{eye}")))
            direct.update(_selected(version_flat, (f"algorithm_versions.processing.pupil.{eye}", "algorithm_versions.processing.pupil")))
            add_node(f"processing.{branch}", (f"source.{branch}",), direct, direct.keys())

        event_direct = _selected(parameter_flat, ("parameters.event_detection", "parameters.events"))
        event_direct.update(_selected(input_flat, ("inputs.external_events", "inputs.context.external_events")))
        event_direct.update(_selected(table_flat, ("table_refs.external_events", "table_refs.context.external_events")))
        event_direct.update(_selected(version_flat, ("algorithm_versions.events", "algorithm_versions.event_detection")))
        add_node("events", ("processing.gaze",), event_direct, event_direct.keys())

        aoi_direct = _selected(input_flat, aois_prefixes) | _selected(table_flat, aois_prefixes)
        aoi_direct.update(_selected(parameter_flat, ("parameters.aois", "parameters.group.aoi", "parameters.individual.aoi")))
        aoi_direct.update(_selected(version_flat, ("algorithm_versions.aoi_geometry",)))
        add_node("context.aois", ("source.aois",), aoi_direct, aoi_direct.keys())
        member_direct = _selected(input_flat, ("inputs.members", "inputs.member_map", "inputs.team_members",
                                               "inputs.pair_members", "inputs.group_scope"))
        member_direct.update(_selected(parameter_flat, ("parameters.group.members", "parameters.statistics_design.options.team_members",
                                                         "parameters.statistics_design.options.pair_members",
                                                         "parameters.group.roles", "parameters.group.metadata")))
        member_direct.update(_selected(table_flat, ("table_refs.members", "table_refs.member_map")))
        add_node("group.membership", ("source.segments",), member_direct, member_direct.keys())
        sync_direct = _selected(input_flat, ("inputs.synchronization", "inputs.clock_mapping", "inputs.time_alignment"))
        sync_direct.update(_selected(parameter_flat, ("parameters.group.sync",)))
        sync_direct.update(_selected(table_flat, ("table_refs.synchronization", "table_refs.clock_mapping")))
        add_node("context.sync", ("source.time", "group.membership"), sync_direct, sync_direct.keys())
        lag_direct = _selected(input_flat, ("inputs.lag", "inputs.lag_alignment"))
        lag_direct.update(_selected(parameter_flat, ("parameters.group.lag",)))
        lag_direct.update(_selected(table_flat, ("table_refs.lag", "table_refs.lag_alignment")))
        add_node("context.lag", ("source.time", "group.membership"), lag_direct, lag_direct.keys())

        nodes_for_stats: list[str] = []
        dependency_maps = self._table_refs.get("metric_dependencies", {}) if isinstance(self._table_refs, Mapping) else {}
        metric_node_map: dict[str, list[str]] = defaultdict(list)
        for namespace, metric_ids in ids.items():
            for metric_id in metric_ids:
                node = f"metric.{namespace}.{metric_id}"
                parents_for_metric = list(_metric_kind(metric_id, namespace, self._parameters))
                direct: dict[str, Any] = {"selected": True}
                # Namespace-specific parameters are narrow. If the bridge supplies explicit
                # registry references they take precedence over the safe builtin defaults.
                explicit = dependency_maps.get(metric_id, {}) if isinstance(dependency_maps, Mapping) else {}
                if not isinstance(explicit, Mapping):
                    explicit = {}
                parameter_paths = explicit.get("parameter_prefixes", ())
                table_paths = explicit.get("table_refs", ())
                input_paths = explicit.get("input_prefixes", ())
                if parameter_paths or table_paths or input_paths:
                    direct.update(_selected(parameter_flat, tuple("parameters." + str(path).removeprefix("parameters.") for path in parameter_paths)))
                    direct.update(_selected(table_flat, tuple("table_refs." + str(path).removeprefix("table_refs.") for path in table_paths)))
                    direct.update(_selected(input_flat, tuple("inputs." + str(path).removeprefix("inputs.") for path in input_paths)))
                    extra_parents = explicit.get("parents", ())
                    if isinstance(extra_parents, Sequence) and not isinstance(extra_parents, str):
                        parents_for_metric.extend(str(parent) for parent in extra_parents if str(parent) in nodes)
                else:
                    if namespace == "advanced":
                        prefix = "parameters.advanced."
                        if "pupil.spectral" in metric_id or "psd" in metric_id.casefold():
                            direct.update(_selected(parameter_flat, (prefix + "pupil_psd_", prefix + "advanced.pupil_psd_",
                                                                     prefix + "support_gap_s", prefix + "advanced.support_gap_s",
                                                                     prefix + "uniform_tolerance", prefix + "advanced.uniform_tolerance")))
                        else:
                            direct.update(_selected(parameter_flat, (prefix + metric_id,)))
                    else:
                        direct.update(_selected(parameter_flat, (f"parameters.{namespace}.{metric_id}",
                                                                  f"parameters.{namespace}.metric_parameters.{metric_id}")))
                direct.update(_selected(table_flat, (f"table_refs.metrics.{metric_id}",
                                                      f"table_refs.metric_support.{metric_id}",
                                                      f"table_refs.support.{metric_id}",
                                                      f"table_refs.metric_dependencies.{metric_id}",
                                                      "table_refs.context")))
                direct.update(_selected(parameter_flat, ("parameters.metadata",)))
                if namespace == "individual":
                    # The legacy shared cap affects time-supported individual
                    # metrics (including pupil means/coverage) but is not a
                    # leaf-specific registry field.
                    direct.update(_selected(parameter_flat, ("parameters.individual.max_gap_s",)))
                if namespace == "group":
                    direct.update(_selected(parameter_flat, ("parameters.group.metadata", "parameters.group.roles")))
                direct.update(_selected(version_flat, (f"algorithm_versions.{namespace}.{metric_id}",
                                                        f"algorithm_versions.metrics.{metric_id}",
                                                        f"algorithm_versions.{metric_id}",
                                                        "algorithm_versions.pipeline", "algorithm_versions.contract")))
                add_node(node, parents_for_metric, direct, direct.keys())
                metric_node_map[metric_id].append(node)
                nodes_for_stats.append(node)

        stats_design_direct = _selected(parameter_flat, ("parameters.statistics_design", "parameters.statistics"))
        stats_design_direct.update(_selected(input_flat, performance_prefixes))
        stats_design_direct.update(_selected(table_flat, performance_prefixes))
        stats_direct_parents = tuple(dict.fromkeys(nodes_for_stats))
        stats_design_direct.update(_selected(version_flat, ("algorithm_versions.statistics", "algorithm_versions.statistics_method",
                                                            "algorithm_versions.pipeline", "algorithm_versions.contract")))
        add_node("statistics", stats_direct_parents, stats_design_direct, stats_design_direct.keys())
        figure_parents = tuple(dict.fromkeys((*nodes_for_stats, "statistics")))
        figure_direct = _selected(parameter_flat, ("parameters.figure_data", "parameters.plot_data"))
        figure_direct.update(_selected(version_flat, ("algorithm_versions.figures", "algorithm_versions.visualization")))
        add_node("figures", figure_parents, figure_direct, figure_direct.keys())
        add_node("archive", ("source.identity", "source.time", "source.segments", "source.aois", "source.performance",
                              *stats_direct_parents, "statistics", "figures"),
                 _selected(input_flat, ("inputs.raw_sources", "inputs.source_manifest")) |
                 _selected(parameter_flat, ("parameters.archive",)) |
                 _selected(table_flat, ("table_refs.archive", "table_refs.raw_sources", "table_refs.source_manifest")) |
                 _selected(version_flat, ("algorithm_versions.archive", "algorithm_versions.serialization")),
                 ("inputs.raw_sources", "inputs.source_manifest", "parameters.archive", "table_refs.archive"))

        # Reconcile direct fingerprint differences against the previous GUI state.
        old_fingerprints = self._node_fingerprints
        old_direct_fingerprints = self._node_direct_fingerprints
        changed = tuple(sorted(
            node for node, fingerprint in nodes.items()
            if old_fingerprints.get(node) != fingerprint
        ))
        changed_inputs: dict[str, tuple[str, ...]] = {}
        for node in changed:
            old_paths = old_direct_fingerprints.get(node, {})
            new_paths = direct_fingerprints.get(node, {})
            changed_inputs[node] = tuple(sorted(
                path for path in set(old_paths) | set(new_paths)
                if old_paths.get(path) != new_paths.get(path)
            ))
        self._node_fingerprints = nodes
        self._node_parents = parents
        self._node_direct_fingerprints = direct_fingerprints
        all_nodes = {**nodes}
        current_fingerprint = _digest(all_nodes)
        stale = self.stale_nodes
        prior_stale: set[str] = set()
        if self._baseline is not None:
            prior_stale = {node for node, fingerprint in old_fingerprints.items()
                           if self._baseline.get(node) != fingerprint}
        newly_stale = tuple(sorted(set(stale) - prior_stale))
        return DependencyChange(
            changed_nodes=changed,
            newly_stale_nodes=newly_stale,
            stale_nodes=stale,
            changed_inputs=MappingProxyType(changed_inputs),
            current_fingerprint=current_fingerprint,
        )

    def record_run(self, run_id: str, status: str) -> RunDependencyRecord:
        """Record a historical run; only a genuinely complete run resets the baseline."""
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id must be a nonempty string")
        if status not in {"complete", "partial", "failed", "cancelled", "unknown"}:
            raise ValueError(f"Unsupported execution status: {status}")
        record = RunDependencyRecord(
            run_id=run_id,
            status=status,
            current_fingerprint=_digest(self._node_fingerprints),
            node_fingerprints=MappingProxyType(dict(self._node_fingerprints)),
            created_order=len(self._records),
        )
        self._records.append(record)
        if status == "complete":
            self._baseline = dict(self._node_fingerprints)
            self._baseline_run_id = run_id
        return record

    def is_run_stale(self, run_id: str) -> bool:
        """Historical runs stay immutable; this reports whether they match current config."""
        record = next((item for item in reversed(self._records) if item.run_id == run_id), None)
        if record is None:
            raise KeyError(run_id)
        return any(self._node_fingerprints.get(node) != fingerprint
                   for node, fingerprint in record.node_fingerprints.items()) or any(
                       node not in record.node_fingerprints for node in self._node_fingerprints
                   )

    def changed_nodes_since(self, run_id: str) -> tuple[str, ...]:
        record = next((item for item in reversed(self._records) if item.run_id == run_id), None)
        if record is None:
            raise KeyError(run_id)
        return tuple(sorted(
            node for node in set(record.node_fingerprints) | set(self._node_fingerprints)
            if record.node_fingerprints.get(node) != self._node_fingerprints.get(node)
        ))

    def node_support(self, node: str) -> Mapping[str, Any]:
        """Describe GUI dependency edges without claiming a backend cache boundary."""
        if node not in self._node_fingerprints:
            raise KeyError(node)
        return {
            "node": node,
            "fingerprint": self._node_fingerprints[node],
            "depends_on": self._node_parents.get(node, ()),
            "baseline_run_id": self._baseline_run_id,
            "stale": node in self.stale_nodes,
            "scope": "GUI stale-state tracking only; pipeline recomputes through public run_analysis",
        }

