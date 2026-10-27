"""Deterministic checks and conservative row mapping for ResultsPage actions."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


_SCOPE_IDENTITY_COLUMNS = (
    "subject_id", "session_id", "interaction_id", "pair_id", "segment_id",
    "segment_instance_id", "trial", "condition", "analysis_stream_id",
    "eye_label", "source_id", "physical_partition_id", "stimulus_id",
    "timebase_id", "source_role", "signal", "event_id", "observation_id",
    "window_id", "window_start_s", "window_end_s", "range_start_s",
    "range_end_s", "start_s", "end_s",
)
_POSITIVE_STATUSES = frozenset({
    "complete", "completed", "available", "ready", "valid", "ok", "passed",
    "success", "successful", "computed",
})
_POSITIVE_REASON_CODES = frozenset({
    "available", "ready", "complete", "ok", "success", "computed", "passed",
})
_DIAGNOSTIC_FIELDS = (
    "reason_code", "reason", "error", "failure_type", "failure_reason",
    "required_fields", "required_metadata", "parameter_path", "issue",
    "aoi_geometry_issue", "preflight_aoi_issue", "issues", "details",
)
_MISSING = object()


@dataclass(frozen=True)
class AvailabilityMatch:
    """A unique availability row, or a factual reason why none was attached."""

    row: Mapping[str, Any] | None
    reason: str
    candidate_count: int

    @property
    def matched(self) -> bool:
        return self.row is not None


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if np.ndim(result) == 0 else False
    except (TypeError, ValueError):
        return False


def _same_value(left: Any, right: Any) -> bool:
    if _missing(left) or _missing(right):
        return _missing(left) and _missing(right)
    if isinstance(left, np.generic):
        left = left.item()
    if isinstance(right, np.generic):
        right = right.item()
    try:
        result = left == right
        return bool(result) if np.ndim(result) == 0 else False
    except (TypeError, ValueError):
        return False


def match_availability_row(
    diagnostic: Mapping[str, Any], availability: pd.DataFrame | None,
) -> AvailabilityMatch:
    """Match metric plus every available scope identity; ambiguity fails closed.

    A metric-only match is not enough. Every nonmissing scope value on either
    side must be present and equal on the other side, and at least one scope
    field must be jointly verifiable. Duplicate matching rows remain ambiguous.
    """
    if not isinstance(diagnostic, Mapping):
        return AvailabilityMatch(None, "诊断行不是结构化映射。", 0)
    metric_id = diagnostic.get("metric_id", _MISSING)
    if _missing(metric_id):
        return AvailabilityMatch(None, "诊断行没有 metric_id，无法关联可用情况。", 0)
    if not isinstance(availability, pd.DataFrame) or availability.empty:
        return AvailabilityMatch(None, "此运行没有可用情况表。", 0)
    if "metric_id" not in availability.columns:
        return AvailabilityMatch(None, "可用情况表缺少 metric_id。", 0)

    metric_mask = availability["metric_id"].map(lambda value: _same_value(value, metric_id))
    metric_rows = availability.loc[metric_mask]
    if metric_rows.empty:
        return AvailabilityMatch(None, "没有相同 metric_id 的可用情况行。", 0)

    compatible: list[dict[str, Any]] = []
    for _, candidate in metric_rows.iterrows():
        jointly_verified = 0
        matches = True
        for column in _SCOPE_IDENTITY_COLUMNS:
            if column not in diagnostic and column not in availability.columns:
                continue
            left = diagnostic.get(column, _MISSING)
            right = candidate.get(column, _MISSING) if column in candidate.index else _MISSING
            left_missing = left is _MISSING or _missing(left)
            right_missing = right is _MISSING or _missing(right)
            if left_missing != right_missing:
                matches = False
                break
            if left_missing:
                continue
            if not _same_value(left, right):
                matches = False
                break
            jointly_verified += 1
        if matches and jointly_verified:
            compatible.append(candidate.to_dict())

    if len(compatible) == 1:
        return AvailabilityMatch(deepcopy(compatible[0]), "metric_id 与完整可核验范围唯一匹配。", 1)
    if len(compatible) > 1:
        return AvailabilityMatch(None, "多个可用情况行具有相同 metric_id 和范围；没有选取其中任一行。", len(compatible))
    return AvailabilityMatch(None, "范围身份缺失或不匹配；未附加可用情况原因。", 0)


def has_actionable_diagnostic(row: Mapping[str, Any] | pd.Series | None) -> bool:
    """Whether a row contains a status or structured fact worth reviewing."""
    if isinstance(row, pd.Series):
        row = row.to_dict()
    if not isinstance(row, Mapping):
        return False
    status = row.get("status")
    status_text = "" if _missing(status) else str(getattr(status, "value", status)).strip().casefold()
    reason_code = row.get("reason_code")
    code_text = "" if _missing(reason_code) else str(reason_code).strip().casefold()
    details_present = False
    for name in _DIAGNOSTIC_FIELDS:
        value = row.get(name)
        if _missing(value):
            continue
        if isinstance(value, (str, bytes)):
            if str(value).strip():
                details_present = True
                break
        elif isinstance(value, (Mapping, list, tuple, set, frozenset, np.ndarray, pd.Series)):
            if len(value):
                details_present = True
                break
        else:
            details_present = True
            break
    if status_text in _POSITIVE_STATUSES and (not code_text or code_text in _POSITIVE_REASON_CODES) and not details_present:
        return False
    if details_present:
        return True
    return bool(status_text and status_text not in _POSITIVE_STATUSES)


def run_results_actions_checks() -> dict[str, Any]:
    """Exercise the real ResultsPage selection, comparison, and navigation path.

    This compact helper is designed for the packaged fixed diagnostic. It uses
    detached RunRecords and an offscreen Qt page, and never mutates a session or
    invokes the analysis pipeline.
    """
    from PySide6.QtCore import QItemSelectionModel, QThread, QTimer, Qt
    from PySide6.QtWidgets import QApplication

    from .models import RunRecord
    from .remediation import NavigationTarget, build_remediation
    from .results_page import ResultsPage

    app = QApplication.instance() or QApplication([])
    failures: list[str] = []
    checks: dict[str, bool] = {}
    metrics = pd.DataFrame([
        {
            "metric_id": "individual.latency", "subject_id": "s1", "session_id": "ss1",
            "analysis_stream_id": "stream-B", "value": 12.0, "unit": "ms",
            "algorithm_version": "v1", "status": "partial", "reason_code": "missing_precondition",
            "reason": "missing_fields:pupil_left", "parameter_path": "processing.filter",
        },
        {
            "metric_id": "individual.coverage", "subject_id": "s1", "session_id": "ss1",
            "analysis_stream_id": "stream-A", "value": 0.8, "unit": "ratio",
            "algorithm_version": "v1", "status": "complete",
        },
        {
            "metric_id": "individual.unknown", "subject_id": "s1", "session_id": "ss1",
            "analysis_stream_id": "stream-A", "value": np.nan, "unit": "ratio",
            "algorithm_version": "v1", "status": "unavailable", "reason_code": "future_reason_42",
            "reason": "unrecognized source-specific cause",
        },
    ])
    availability = pd.DataFrame([
        {"metric_id": "individual.latency", "subject_id": "s1", "session_id": "ss1",
         "analysis_stream_id": "stream-A", "status": "unavailable", "reason_code": "wrong_stream", "reason": "A"},
        {"metric_id": "individual.latency", "subject_id": "s1", "session_id": "ss1",
         "analysis_stream_id": "stream-B", "status": "unavailable", "reason_code": "missing_precondition",
         "reason": "missing_fields:pupil_left", "required_fields": ["pupil_left"]},
    ])
    details = {"parameters": {"processing": {"filter": "none"}}, "context": {"coordinate_unit": "px"}}

    def make_record(run_id: str, status: str, table_map: Mapping[str, pd.DataFrame], metadata=None) -> RunRecord:
        return RunRecord.from_archive(
            run_id=run_id, generation=1, revision_id=f"revision-{run_id}",
            config_fingerprint=f"fingerprint-{run_id}", phase="run", status=status,
            created_at="2026-10-23T12:00:00Z", metadata=metadata or {"execution_status": status},
            tables=table_map, logs=(), snapshot=None, snapshot_details=details,
        )

    record = make_record("run-main", "partial", {"metrics": metrics, "availability": availability})
    left = make_record("run-left", "complete", {"metrics": metrics.iloc[[1]].copy()})
    failed = make_record("run-failed", "failed", {}, {
        "execution_status": "failed", "failure_type": "PreflightError",
        "failure_reason": "structured preflight failure", "reason_code": "missing_precondition",
    })
    page = ResultsPage()
    try:
        page.set_history([left, failed, record])
        checks["comparison_button_requires_two_history_records"] = page.compare_runs_button.isEnabled()
        comparison_dialog = page._build_comparison_dialog()
        comparison = comparison_dialog.comparison_result
        checks["comparison_uses_two_run_records"] = bool(
            comparison is not None and comparison.left_run_id == "run-failed" and comparison.right_run_id == "run-main"
        )
        page._comparison_left_run_id = "run-left"
        page._comparison_right_run_id = "run-main"
        page.set_history([record, left, failed])
        comparison_dialog = page._build_comparison_dialog()
        comparison = comparison_dialog.comparison_result
        checks["comparison_history_selection_follows_run_ids"] = bool(
            comparison is not None and comparison.left_run_id == "run-left" and comparison.right_run_id == "run-main"
        )

        page.show_record(record)
        page.set_history([left, failed, record])
        page.set_history([record, left, failed])
        selected_history_record = page.history_choice.currentData()
        checks["results_history_selection_follows_run_id"] = bool(
            isinstance(selected_history_record, RunRecord) and selected_history_record.run_id == "run-main"
        )
        tab = page.overview_tab
        metric_column = tab._source_model._frame.columns.get_loc("metric_id")
        tab.table_view.sortByColumn(metric_column, Qt.SortOrder.DescendingOrder)
        tab.search.setText("latency")
        tab._start_filter()
        for _ in range(100):
            app.processEvents()
            if tab._proxy_model._mask is not None:
                break
            QThread.msleep(5)
        app.processEvents()
        visible_index = tab._proxy_model.index(0, metric_column)
        tab.table_view.selectionModel().select(
            visible_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
        )
        selected = page._build_selected_remediation(tab)
        guide, payload = selected if selected is not None else (None, None)
        checks["filtered_sorted_selection_maps_to_true_source_row"] = bool(
            payload is not None and payload["diagnostic"].get("metric_id") == "individual.latency"
        )
        match = match_availability_row(payload["diagnostic"], availability) if payload else AvailabilityMatch(None, "no payload", 0)
        checks["availability_matches_full_stream_scope"] = bool(
            match.matched and match.row.get("analysis_stream_id") == "stream-B"
        )
        missing_stream_match = match_availability_row(
            {"metric_id": "individual.latency", "subject_id": "s1", "session_id": "ss1"}, availability,
        )
        duplicated_scope = pd.concat([availability, availability.iloc[[1]]], ignore_index=True)
        duplicate_match = match_availability_row(payload["diagnostic"], duplicated_scope) if payload else None
        checks["missing_scope_does_not_pick_stream"] = not missing_stream_match.matched
        checks["duplicate_scope_does_not_pick_first"] = bool(duplicate_match is not None and not duplicate_match.matched)
        checks["guide_keeps_availability_separate_from_signal_diagnostic"] = bool(
            guide is not None and "availability" not in payload["diagnostic"]
            and guide.original_diagnostic.get("availability", {}).get("analysis_stream_id") == "stream-B"
        )
        sent: list[dict[str, Any]] = []
        page.remediationNavigationRequested.connect(sent.append)
        target = guide.navigation_target if guide is not None else None
        if not isinstance(target, NavigationTarget):
            target = NavigationTarget("import", section="signals", field="pupil_left", source_role="signals")
        page._emit_remediation_navigation(payload, target)
        checks["navigation_payload_has_exact_contract"] = bool(
            sent and set(sent[-1]) == {"target", "run_id", "revision_id", "diagnostic"}
            and sent[-1]["run_id"] == "run-main"
            and sent[-1]["revision_id"] == "revision-run-main"
            and sent[-1]["diagnostic"].get("metric_id") == "individual.latency"
        )
        checks["navigation_payload_preserves_original_reason"] = bool(
            sent and sent[-1]["diagnostic"].get("reason_code") == "missing_precondition"
            and sent[-1]["diagnostic"].get("reason") == "missing_fields:pupil_left"
            and "availability" not in sent[-1]["diagnostic"]
        )

        row_before = record.table_copy("metrics")
        page.show_record(record)
        tab = page.overview_tab
        tab._proxy_model.set_mask(np.asarray([False, True, False], dtype=bool))
        normal_index = tab._proxy_model.index(0, metric_column)
        tab.table_view.selectionModel().select(
            normal_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
        )
        checks["valid_row_without_diagnosis_disabled"] = not tab.remediation_button.isEnabled()

        unknown_row = metrics.iloc[2].to_dict()
        unknown_guide = build_remediation(record, unknown_row)
        checks["unknown_reason_is_preserved_without_guessed_target"] = bool(
            unknown_guide.original_diagnostic.get("reason_code") == "future_reason_42"
            and unknown_guide.navigation_target is None
        )

        duplicate_index_metrics = metrics.copy(deep=True)
        duplicate_index_metrics.index = [7, 7, 7]
        duplicate_index_record = make_record("run-duplicate-index", "partial", {
            "metrics": duplicate_index_metrics, "availability": availability,
        })
        page.show_record(duplicate_index_record)
        individual_tab = page.individual_tab
        source_table, source_positions = individual_tab.source_binding()
        checks["duplicate_dataframe_index_uses_row_positions"] = bool(
            source_table == "metrics" and source_positions == (0, 1, 2)
        )
        duplicate_metric_column = individual_tab._source_model._frame.columns.get_loc("metric_id")
        individual_tab.table_view.sortByColumn(duplicate_metric_column, Qt.SortOrder.DescendingOrder)
        individual_tab.search.setText("latency")
        individual_tab._start_filter()
        for _ in range(100):
            app.processEvents()
            if individual_tab._proxy_model._mask is not None:
                break
            QThread.msleep(5)
        app.processEvents()
        duplicate_visible_index = individual_tab._proxy_model.index(0, duplicate_metric_column)
        individual_tab.table_view.selectionModel().select(
            duplicate_visible_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
        )
        duplicate_selected = page._build_selected_remediation(individual_tab)
        checks["duplicate_index_sorted_filtered_action_keeps_real_row"] = bool(
            duplicate_selected is not None
            and duplicate_selected[1]["diagnostic"].get("metric_id") == "individual.latency"
            and duplicate_selected[1]["diagnostic"].get("analysis_stream_id") == "stream-B"
        )

        page.show_record(failed)
        checks["failed_no_table_has_run_level_action"] = bool(
            page.run_remediation_button.isEnabled()
        )

        # Exercise the actual modal execution path: the navigation signal must
        # fire and close the dialog so the host destination is interactive.
        failed_payload = page._build_run_level_remediation()
        if failed_payload is not None:
            failed_guide, payload = failed_payload
            modal_sent: list[dict[str, Any]] = []
            page.remediationNavigationRequested.connect(modal_sent.append)
            QTimer.singleShot(0, lambda: page._active_remediation_dialog.navigate_button.click()
                              if getattr(page, "_active_remediation_dialog", None) is not None else None)
            page._show_remediation_dialog(failed_guide, payload)
            checks["modal_navigation_closes_dialog"] = bool(
                modal_sent and not page._active_remediation_dialog.isVisible()
            )
        else:
            checks["modal_navigation_closes_dialog"] = False

        checks["run_record_tables_remain_unchanged"] = row_before.equals(record.table_copy("metrics"))
    except Exception as exc:  # fixed diagnostic reports a concrete failure rather than aborting startup
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
    failures.extend(name for name, passed in checks.items() if not passed)
    return {"passed": not failures, "checks": checks, "failures": failures}


__all__ = [
    "AvailabilityMatch", "match_availability_row", "has_actionable_diagnostic",
    "run_results_actions_checks",
]
