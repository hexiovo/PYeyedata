"""The single GUI boundary to PYeyedata's public pipeline API."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import math
import numbers
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

import pandas as pd

from pyeyedata import (
    AnalysisBundle,
    AnalysisConfig,
    AnalysisPreflight,
    DesignSpec,
    EventDetectionConfig,
    EyeSelectionConfig,
    LoadedAnalysis,
    ProcessingConfig,
    get_analysis_schema,
    load_analysis,
    preflight_analysis,
    run_analysis,
    save_analysis,
)
from pyeyedata.statistics import run_statistics
from pyeyedata.pipeline import _statistics_completion_status, _mixed_descriptives_are_local_unavailable
from pyeyedata.scopes import SegmentScopeConfig
from pyeyedata.execution_cache import ExecutionCache

from .models import RunRecord, RunSnapshot, SelectionSpec


class PipelinePhase(str, Enum):
    PREFLIGHT = "preflight"
    RUN = "run"
    SAVE = "save"
    LOAD = "load"


class PipelineStatus(str, Enum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


def _without_statistics_subset_logs(
    logs: list[str], module_status: Any
) -> tuple[list[str], Mapping[str, Any]]:
    """Remove the exact statistics-log suffix emitted for the calculated subset."""
    statuses = module_status if isinstance(module_status, list) else []
    previous = next(
        (item for item in reversed(statuses)
         if isinstance(item, Mapping) and item.get("module") == "statistics"),
        {},
    )
    previous_logs = previous.get("statistics_logs", [])
    output = list(logs)
    if isinstance(previous_logs, list) and previous_logs:
        count = len(previous_logs)
        if output[-count:] == previous_logs:
            output = output[:-count]
    return output, previous


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    """Typed task envelope that preserves the backend status and returned payload."""

    phase: PipelinePhase
    status: PipelineStatus
    backend_status: str
    run_id: str
    generation: int
    metadata: Mapping[str, Any]
    payload: Any

    @classmethod
    def from_payload(
        cls,
        *,
        phase: PipelinePhase,
        run_id: str,
        generation: int,
        payload: Any,
    ) -> "PipelineOutcome":
        raw_metadata = getattr(payload, "metadata", {})
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        backend_status = str(metadata.get("execution_status", "unknown"))
        try:
            status = PipelineStatus(backend_status)
        except ValueError:
            status = PipelineStatus.UNKNOWN
        return cls(
            phase=phase,
            status=status,
            backend_status=backend_status,
            run_id=run_id,
            generation=generation,
            metadata=MappingProxyType(deepcopy(metadata)),
            payload=payload,
        )

    @property
    def availability(self) -> pd.DataFrame | None:
        if isinstance(self.payload, AnalysisPreflight):
            return self.payload.availability.copy(deep=True)
        table = getattr(self.payload, "tables", {}).get("availability")
        return table.copy(deep=True) if isinstance(table, pd.DataFrame) else None

    @property
    def result_tables(self) -> Mapping[str, pd.DataFrame]:
        tables = getattr(self.payload, "tables", {})
        if not isinstance(tables, Mapping):
            return MappingProxyType({})
        return MappingProxyType({
            str(name): frame.copy(deep=True)
            for name, frame in tables.items()
            if isinstance(frame, pd.DataFrame)
        })


@dataclass(frozen=True, slots=True)
class MetricReusePlan:
    """GUI-owned plan for reusing rows from one earlier complete run.

    The public pipeline remains responsible for shared processing and for any
    metrics in ``calculation_selection``.  Reused rows are merged only after
    that public run completes, then statistics are recomputed through the
    existing public statistics entrypoint.
    """

    source_run_id: str
    metrics: pd.DataFrame
    reused_selection: Mapping[str, tuple[str, ...]]
    calculation_selection: SelectionSpec
    source_record: RunRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_run_id, str) or not self.source_run_id:
            raise ValueError("source_run_id must be a nonempty string")
        if not isinstance(self.metrics, pd.DataFrame):
            raise TypeError("reusable metrics must be a DataFrame")
        if not isinstance(self.calculation_selection, SelectionSpec):
            raise TypeError("calculation_selection must be a SelectionSpec")
        object.__setattr__(self, "metrics", self.metrics.copy(deep=True))
        object.__setattr__(
            self,
            "reused_selection",
            {str(name): tuple(str(metric_id) for metric_id in ids)
             for name, ids in self.reused_selection.items()},
        )

def _typed_config(value: Any, constructor: type[Any], name: str) -> Any:
    if isinstance(value, constructor):
        return deepcopy(value)
    if value is None:
        return constructor()
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping or {constructor.__name__}")
    prepared = dict(value)
    if name == "processing" and isinstance(prepared.get("pupil_baseline_window_s"), list):
        prepared["pupil_baseline_window_s"] = tuple(prepared["pupil_baseline_window_s"])
    return constructor(**prepared)


class PipelineBridge:
    """Builds only real public AnalysisConfig objects and delegates all science to A."""

    def bind_execution_cache(
        self, cache: ExecutionCache, is_current: Callable[[str, int], bool],
    ) -> None:
        """Bind one host session without requiring subclass constructor changes."""
        self._execution_cache = cache
        self._cache_is_current = is_current

    def _cache_options(self, snapshot: RunSnapshot) -> dict[str, Any]:
        cache = getattr(self, "_execution_cache", None)
        current = getattr(self, "_cache_is_current", None)
        if cache is None or current is None:
            return {}
        return {"execution_cache": cache,
                "cache_eligible": lambda: current(snapshot.run_id, snapshot.generation)}

    def schema(self) -> dict[str, Any]:
        """Return the complete live schema without collapsing catalogue hierarchy."""
        return deepcopy(get_analysis_schema())

    def build_config(
        self,
        snapshot: RunSnapshot,
        *,
        selection: SelectionSpec | None = None,
    ) -> AnalysisConfig:
        values = snapshot.parameter_values()
        selected = selection or snapshot.selection
        processing = _typed_config(values.get("processing"), ProcessingConfig, "processing")
        eye_selection = _typed_config(values.get("eye_selection"), EyeSelectionConfig, "eye_selection")
        event_detection = _typed_config(values.get("event_detection"), EventDetectionConfig, "event_detection")
        scope = _typed_config(values.get("scope"), SegmentScopeConfig, "scope")

        individual = dict(values.get("individual", {}))
        group = dict(values.get("group", {}))
        advanced_values = dict(values.get("advanced", {}))
        individual["metric_ids"] = list(selected.individual_metric_ids)
        group["metric_ids"] = list(selected.group_metric_ids)

        advanced: dict[str, Any] = {}
        for key, value in advanced_values.items():
            if key in {"metric_ids", "metadata", "provenance"}:
                continue
            if key.startswith("advanced."):
                advanced[key.removeprefix("advanced.")] = value
            elif key == "advanced" and isinstance(value, Mapping):
                advanced.update(value)
            else:
                advanced[key] = value
        advanced["metric_ids"] = list(selected.advanced_metric_ids)

        statistics = values.get("statistics_design", values.get("statistics"))
        if isinstance(statistics, Mapping):
            statistics_values = deepcopy(dict(statistics))
            options = statistics_values.get("options")
            if isinstance(options, Mapping):
                options_values = deepcopy(dict(options))
                selected_levels = options_values.get("selected_levels")
                if isinstance(selected_levels, Mapping):
                    options_values["selected_levels"] = {
                        column: list(levels) if isinstance(levels, tuple) else deepcopy(levels)
                        for column, levels in selected_levels.items()
                    }
                statistics_values["options"] = options_values
            statistics = DesignSpec(**statistics_values)

        metadata = values.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        context = dict(values.get("context", {}))
        context.update(snapshot.context_values())
        if not isinstance(context, Mapping):
            raise TypeError("context must be a mapping of named tables and values")

        return AnalysisConfig(
            run_id=snapshot.run_id,
            processing=processing,
            eye_selection=eye_selection,
            event_detection=event_detection,
            scope=scope,
            individual=individual,
            group=group,
            advanced=advanced,
            statistics=statistics,
            metadata=dict(metadata),
            context=context,
            run_individual=selected.run_individual,
            run_group=selected.run_group,
            run_advanced=selected.run_advanced,
        )

    def preflight(
        self,
        snapshot: RunSnapshot,
        *,
        cancel: Callable[[], bool] | None = None,
        progress: Callable[[float, str], None] | None = None,
    ) -> PipelineOutcome:
        payload = preflight_analysis(
            snapshot.dataset_copy(),
            self.build_config(snapshot),
            cancel=cancel,
            progress=progress,
            **self._cache_options(snapshot),
        )
        return PipelineOutcome.from_payload(
            phase=PipelinePhase.PREFLIGHT,
            run_id=snapshot.run_id,
            generation=snapshot.generation,
            payload=payload,
        )

    def run(
        self,
        snapshot: RunSnapshot,
        *,
        cancel: Callable[[], bool] | None = None,
        progress: Callable[[float, str], None] | None = None,
        reuse_plan: MetricReusePlan | None = None,
    ) -> PipelineOutcome:
        config = self.build_config(
            snapshot,
            selection=reuse_plan.calculation_selection if reuse_plan is not None else None,
        )
        pipeline_progress = progress
        if reuse_plan is not None and progress is not None:
            pipeline_progress = lambda fraction, message: progress(0.82 * float(fraction), message)
        cache_options = self._cache_options(snapshot)
        transaction = (cache_options["execution_cache"].begin(
            run_id=snapshot.run_id, phase="run", cancel=cancel, eligible=cache_options["cache_eligible"])
            if cache_options else None)
        payload = None
        succeeded = False
        try:
            payload = run_analysis(snapshot.dataset_copy(), config, cancel=cancel, progress=pipeline_progress,
                                   **({"_cache_transaction": transaction} if transaction is not None else {}))
            if not isinstance(payload, AnalysisBundle):
                raise TypeError("run_analysis returned an unexpected payload type")
            if reuse_plan is not None and not reuse_plan.metrics.empty:
                payload = self._finish_metric_reuse(
                    snapshot, config, payload, reuse_plan, cancel=cancel, progress=progress)
            if transaction is not None:
                payload.metadata["execution_cache"] = transaction.audit
                # Historical row-reuse log predates actual shared-stage reuse.
                payload.logs = [line for line in payload.logs
                                if line != "Shared preprocessing was rerun for this analysis."]
            outcome = PipelineOutcome.from_payload(
                phase=PipelinePhase.RUN, run_id=snapshot.run_id,
                generation=snapshot.generation, payload=payload)
            if transaction is not None:
                # Construct/copy the envelope before publication. This private
                # audit is finalized below and never mutated after return.
                object.__setattr__(outcome, "metadata", MappingProxyType({
                    **dict(outcome.metadata), "execution_cache": transaction.audit}))
            succeeded = True
            return outcome
        finally:
            if transaction is not None:
                transaction.finish(complete=succeeded and payload is not None
                                   and payload.metadata.get("execution_status") == "complete")

    @staticmethod
    def _metric_identity_columns(frame: pd.DataFrame) -> list[str]:
        candidates = (
            "analysis_stream_id", "scope", "metric_id", "subject_id", "session_id",
            "interaction_id", "pair_id", "segment_id", "segment_instance_id",
            "physical_partition_id", "trial", "condition", "eye_label",
        )
        return [column for column in candidates if column in frame.columns]

    @classmethod
    def _merge_metric_rows(
        cls,
        calculated: pd.DataFrame,
        cached: pd.DataFrame,
        *,
        source_run_id: str,
    ) -> pd.DataFrame:
        calculated_rows = calculated.copy(deep=True)
        cached_rows = cached.copy(deep=True)
        if "cache_origin_run_id" not in calculated_rows:
            calculated_rows["cache_origin_run_id"] = None
        cached_rows["cache_origin_run_id"] = source_run_id
        columns = list(dict.fromkeys([*calculated_rows.columns, *cached_rows.columns]))
        combined = pd.concat(
            [calculated_rows.reindex(columns=columns), cached_rows.reindex(columns=columns)],
            ignore_index=True,
            sort=False,
        )
        identity = cls._metric_identity_columns(combined)
        if {"metric_id", "analysis_stream_id"}.issubset(identity):
            combined = combined.drop_duplicates(subset=identity, keep="first", ignore_index=True)
        return combined

    @classmethod
    def _finish_metric_reuse(
        cls,
        snapshot: RunSnapshot,
        config: AnalysisConfig,
        payload: AnalysisBundle,
        plan: MetricReusePlan,
        *,
        cancel: Callable[[], bool] | None,
        progress: Callable[[float, str], None] | None,
    ) -> AnalysisBundle:
        metadata = dict(payload.metadata)
        tables = {name: frame.copy(deep=True) for name, frame in payload.tables.items()}
        current_metrics = tables.get("metrics", pd.DataFrame())
        if not isinstance(current_metrics, pd.DataFrame):
            current_metrics = pd.DataFrame()
        module_status_before_merge = metadata.get("module_status", [])
        subset_logs, subset_statistics = _without_statistics_subset_logs(
            payload.logs, module_status_before_merge
        )
        pre_statistics_status = subset_statistics.get("pipeline_status_before_statistics")
        if pre_statistics_status not in {"complete", "partial", "failed", "cancelled"}:
            pre_statistics_status = metadata.get("execution_status", "unknown")

        # The first public run has already completed shared processing and any
        # metrics that changed.  A cancellation here must not borrow cached
        # rows into a cancelled run.
        if cancel is not None and cancel():
            return payload
        if str(metadata.get("execution_status", "unknown")) in {"failed", "cancelled"}:
            if not cls._can_resume_local_unavailable_subset(snapshot, payload, plan, pre_statistics_status,
                                                            subset_statistics):
                return payload
            metadata["metric_reuse_subset"] = {
                "resume_reason": "local_unavailable_subset_pending_complete_cached_merge",
                "execution_status": "failed",
                "statistics_runner_execution_status": subset_statistics.get("statistics_runner_execution_status"),
                "source_run_id": plan.source_run_id,
                "source_status": plan.source_record.status,
            }
        source_rows = plan.metrics.copy(deep=True)
        reused_ids = {
            metric_id
            for ids in plan.reused_selection.values()
            for metric_id in ids
        }
        if "metric_id" in source_rows:
            source_rows = source_rows.loc[source_rows["metric_id"].astype("string").isin(reused_ids)].copy()
        merged_metrics = cls._merge_metric_rows(
            current_metrics,
            source_rows,
            source_run_id=plan.source_run_id,
        )
        if cancel is not None and cancel():
            return payload

        try:
            if progress:
                progress(0.82, "按当前完整指标表重新计算统计结果")
            statistics_result = run_statistics(
                merged_metrics,
                config.statistics if config.statistics is not None else DesignSpec(),
                performance=snapshot.dataset_copy().performance,
                cancel=cancel,
                progress=(
                    (lambda fraction, message: progress(0.82 + 0.18 * float(fraction), message))
                    if progress else None
                ),
            )
        except Exception as exc:
            for name in tuple(tables):
                if name.startswith("statistics_"):
                    tables.pop(name)
            metadata["execution_status"] = "failed"
            metadata["metric_reuse"] = {
                "cache_origin_run_id": plan.source_run_id,
                "requested_selection": snapshot.selection.to_dict(),
                "reused_selection": {key: list(value) for key, value in plan.reused_selection.items()},
                "calculation_selection": plan.calculation_selection.to_dict(),
                "statistics_execution_status": "failed",
                "statistics_runner_execution_status": "raised",
                "design_supplied": config.statistics is not None,
                "statistics_failure_type": type(exc).__name__,
                "statistics_failure_reason": str(exc),
                "reused_rows_committed": False,
            }
            statuses = [item for item in metadata.get("module_status", [])
                        if not isinstance(item, Mapping) or item.get("module") != "statistics"]
            statuses.append({"module": "statistics", "execution_status": "failed",
                             "pipeline_completion_status": "failed",
                             "statistics_runner_execution_status": "raised",
                             "pipeline_status_before_statistics": pre_statistics_status,
                             "design_supplied": config.statistics is not None,
                             "metrics_row_count": len(merged_metrics),
                             "failure_type": type(exc).__name__, "failure_reason": str(exc)})
            metadata["module_status"] = statuses
            return AnalysisBundle(tables=tables, metadata=metadata,
                                  logs=[*subset_logs, f"statistics_failed:{type(exc).__name__}:{exc}"])

        statistics_status, runner_status = _statistics_completion_status(
            statistics_result,
            design_supplied=config.statistics is not None,
            metrics=merged_metrics,
        )
        if statistics_status in {"cancelled", "failed"}:
            # Preserve only work actually returned for this run; incomplete
            # runs are never a cache source.
            for name in tuple(tables):
                if name.startswith("statistics_"):
                    tables.pop(name)
            tables["metrics"] = current_metrics
            metadata["execution_status"] = statistics_status
            metadata["metric_reuse"] = {
                "cache_origin_run_id": plan.source_run_id,
                "requested_selection": snapshot.selection.to_dict(),
                "reused_selection": {key: list(value) for key, value in plan.reused_selection.items()},
                "calculation_selection": plan.calculation_selection.to_dict(),
                "statistics_execution_status": statistics_status,
                "statistics_runner_execution_status": runner_status,
                "design_supplied": config.statistics is not None,
                "reused_rows_committed": False,
            }
            statuses = [item for item in metadata.get("module_status", [])
                        if not isinstance(item, Mapping) or item.get("module") != "statistics"]
            statuses.append({
                "module": "statistics",
                **dict(statistics_result.metadata),
                "pipeline_completion_status": statistics_status,
                "statistics_runner_execution_status": runner_status,
                "pipeline_status_before_statistics": pre_statistics_status,
                "statistics_logs": list(statistics_result.logs),
                "design_supplied": config.statistics is not None,
                "metrics_row_count": len(merged_metrics),
                "reused_metric_ids": sorted(reused_ids),
                "calculated_metric_ids": sorted({
                    str(value) for values in (
                        plan.calculation_selection.individual_metric_ids,
                        plan.calculation_selection.group_metric_ids,
                        plan.calculation_selection.advanced_metric_ids,
                    ) for value in values
                }),
            })
            metadata["module_status"] = statuses
            return AnalysisBundle(tables=tables, metadata=metadata,
                                  logs=[*subset_logs, *statistics_result.logs])

        # run_analysis has already emitted statistics for the calculated subset.
        # Replace every statistics table so a table omitted by the current
        # design cannot survive from that subset alongside the recomputed result.
        for name in tuple(tables):
            if name.startswith("statistics_"):
                tables.pop(name)
        for name, frame in statistics_result.tables.items():
            tables[f"statistics_{name}"] = frame.copy(deep=True)
        tables["metrics"] = merged_metrics
        module_status = [item for item in metadata.get("module_status", [])
                         if not isinstance(item, Mapping) or item.get("module") != "statistics"]
        module_status.append({
            "module": "statistics",
            **dict(statistics_result.metadata),
            "pipeline_completion_status": statistics_status,
            "statistics_runner_execution_status": runner_status,
            "pipeline_status_before_statistics": pre_statistics_status,
            "statistics_logs": list(statistics_result.logs),
            "design_supplied": config.statistics is not None,
            "metrics_row_count": len(merged_metrics),
            "reused_metric_ids": sorted(reused_ids),
            "calculated_metric_ids": sorted({
                str(value) for values in (
                    plan.calculation_selection.individual_metric_ids,
                    plan.calculation_selection.group_metric_ids,
                    plan.calculation_selection.advanced_metric_ids,
                ) for value in values
            }),
        })
        metadata["module_status"] = module_status
        if statistics_status == "partial" or pre_statistics_status == "partial":
            metadata["execution_status"] = "partial"
        elif statistics_status == "complete" and pre_statistics_status == "complete":
            metadata["execution_status"] = "complete"
        metadata["metric_reuse"] = {
            "cache_origin_run_id": plan.source_run_id,
            "requested_selection": snapshot.selection.to_dict(),
            "reused_selection": {key: list(value) for key, value in plan.reused_selection.items()},
            "calculation_selection": plan.calculation_selection.to_dict(),
            "statistics_execution_status": statistics_status,
            "statistics_runner_execution_status": runner_status,
            "design_supplied": config.statistics is not None,
            "reused_rows_committed": True,
            "reused_row_count": len(source_rows),
            "metrics_row_count": len(merged_metrics),
            "availability_source": "current shared preprocessing for the full method catalogue",
        }
        reused_modules = set(plan.reused_selection)
        logs = [line for line in subset_logs
                if not any(line == f"{module}_module_disabled" for module in reused_modules)]
        logs.extend(["Shared preprocessing was rerun for this analysis.", *statistics_result.logs])
        return AnalysisBundle(tables=tables, metadata=metadata, logs=logs)

    @classmethod
    def _can_resume_local_unavailable_subset(
        cls, snapshot: RunSnapshot, payload: AnalysisBundle, plan: MetricReusePlan,
        pre_statistics_status: Any, statistics_metadata: Mapping[str, Any],
    ) -> bool:
        """Only a validated complete source may complete a blocked metric subset."""
        source = plan.source_record
        if (str(payload.metadata.get("execution_status")) != "failed" or pre_statistics_status != "partial"
                or any(payload.metadata.get(name) for name in ("failure_type", "failure_reason", "exception"))
                or not isinstance(source, RunRecord) or source.status != "complete" or source.phase != "run"
                or source.run_id != plan.source_run_id or source.snapshot is None
                or source.snapshot.dataset_revision.fingerprint != snapshot.dataset_revision.fingerprint):
            return False
        if any(not isinstance(item, Mapping) or item.get("execution_status") not in {"complete", "partial"}
               for item in payload.metadata.get("module_status", [])
               if not isinstance(item, Mapping) or item.get("module") != "statistics"):
            return False
        current = payload.tables.get("metrics")
        inference = payload.tables.get("statistics_statistics")
        descriptives = payload.tables.get("statistics_descriptives")
        if (not isinstance(current, pd.DataFrame) or current.empty
                or not {"status", "value", "reason_code"}.issubset(current)
                or not current.status.eq("unavailable").all() or not current.value.isna().all()
                or not isinstance(inference, pd.DataFrame) or inference.empty or "status" not in inference
                or not inference.status.eq("blocked").all()
                or not isinstance(descriptives, pd.DataFrame) or descriptives.empty
                or statistics_metadata.get("statistics_runner_execution_status") not in {"failed", "partial"}):
            return False
        selected = {metric for values in plan.reused_selection.values() for metric in values}
        requested = {metric for values in (snapshot.selection.individual_metric_ids,
                                           snapshot.selection.group_metric_ids,
                                           snapshot.selection.advanced_metric_ids) for metric in values}
        cached = plan.metrics
        if (not selected or not selected.issubset(requested) or cached.empty
                or not {"metric_id", "value", "unit", "status"}.issubset(cached)
                or set(cached.metric_id) != selected or not cached.status.eq("complete").all()):
            return False
        calculated = {metric for values in (plan.calculation_selection.individual_metric_ids,
                                             plan.calculation_selection.group_metric_ids,
                                             plan.calculation_selection.advanced_metric_ids) for metric in values}
        if ("metric_id" not in current or set(current.metric_id) != calculated
                or calculated.intersection(selected) or calculated.union(selected) != requested):
            return False
        for value, unit in zip(cached.value, cached.unit):
            if (isinstance(value, bool) or not isinstance(value, numbers.Real)
                    or not math.isfinite(float(value)) or not isinstance(unit, str) or not unit.strip()):
                return False
        try:
            original = source.table_copy("metrics")
            original = original.loc[original.metric_id.isin(selected)]
            pd.testing.assert_frame_equal(cached, original, check_exact=True)
        except (KeyError, AttributeError, AssertionError, TypeError, ValueError):
            return False
        subset = AnalysisBundle(
            tables={"statistics": inference, "descriptives": descriptives,
                    "diagnostics": payload.tables.get("statistics_diagnostics", pd.DataFrame())},
            metadata=dict(statistics_metadata), logs=list(payload.logs),
        )
        return _mixed_descriptives_are_local_unavailable(subset, inference, descriptives, current,
                                                        require_complete=False)

    def save(
        self,
        snapshot: RunSnapshot,
        bundle: AnalysisBundle,
        path: str | Path,
        *,
        mode: str = "full",
        cancel: Callable[[], bool] | None = None,
    ) -> PipelineOutcome:
        from pyeyedata.archive_session import save_run_archive
        from .models import RunRecord
        archive = save_run_archive(path, RunRecord.from_bundle(snapshot, bundle),
                                   mode=mode, cancel=cancel)
        return PipelineOutcome.from_payload(
            phase=PipelinePhase.SAVE,
            run_id=snapshot.run_id,
            generation=snapshot.generation,
            payload=archive,
        )

    def load(self, path: str | Path, *, run_id: str = "archive-load", generation: int = 0) -> LoadedAnalysis:
        """Load using the public API; the backend has no physical cancel/progress hook."""
        return load_analysis(path)
