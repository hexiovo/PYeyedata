"""Evidence-based navigation hints for recorded diagnostics.

Guides are deliberately descriptive: they expose exact structured facts and
targets, but never edit configuration or infer a destination from free text.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..contracts import AOI_REQUIRED_COLUMNS, SEGMENT_REQUIRED_COLUMNS, SIGNAL_OPTIONAL_COLUMNS, SIGNAL_REQUIRED_COLUMNS
from .models import RunRecord


VALID_PAGES = frozenset({"import", "quality", "metrics", "group", "design", "results"})


@dataclass(frozen=True)
class NavigationTarget:
    """A precise destination request for a host page to resolve."""

    page: str
    section: str | None = None
    field: str | None = None
    source_role: str | None = None
    aoi_id: Any = None
    metric_id: str | None = None


@dataclass(frozen=True)
class RemediationAction:
    title: str
    detail: str
    target: NavigationTarget | None = None


@dataclass(frozen=True)
class RemediationGuide:
    run_id: str
    original_diagnostic: Mapping[str, Any]
    affected_metrics: tuple[str, ...]
    actions: tuple[RemediationAction, ...]
    navigation_target: NavigationTarget | None
    limitations: tuple[str, ...] = ()


_SIGNAL_FIELDS = frozenset(SIGNAL_REQUIRED_COLUMNS) | frozenset(SIGNAL_OPTIONAL_COLUMNS)
_SEGMENT_FIELDS = frozenset(SEGMENT_REQUIRED_COLUMNS) | frozenset({"segment_name", "baseline_start_s", "baseline_end_s"})
_AOI_FIELDS = frozenset(AOI_REQUIRED_COLUMNS) | frozenset({
    "aoi_name", "xmin", "ymin", "xmax", "ymax", "vertices", "priority", "overlap_policy",
})
_METADATA_FIELDS = frozenset({
    "coordinate_unit", "coordinate_frame", "time_unit", "timestamp_unit", "timebase_id",
    "pupil_unit", "pupil_measure", "device_verified", "high_rate_validation",
    "calibration_target_confirmed", "stable_target_confirmed", "reading_scope_confirmed",
    "reference_fixations_confirmed", "reference_trajectory_confirmed", "spatial_comparability",
    "pupil_comparability", "screen_size_px", "stimulus_mapping_confirmed",
})

_AOI_REASON_TARGETS: dict[str, tuple[str, str | None]] = {
    "aoi_geometry_missing": ("design", "aois"),
    "aoi_geometry_unavailable": ("design", "aois"),
    "aoi_stimulus_mapping_unavailable": ("import", "signals"),
    "aoi_stimulus_definition_missing": ("design", "aois"),
    "aoi_coordinate_semantics_mismatch": ("design", "aois"),
    "aoi_name_required": ("design", "aois"),
    "aoi_overlap_policy_unavailable": ("design", "aois"),
    "aoi_overlap_priority_unavailable": ("design", "aois"),
    "aoi_mapping_unconfirmed": ("design", "aois"),
}

_AOI_GEOMETRY_CODES = frozenset({
    "aoi_duplicate_definition", "aoi_geometry_adjacent_backtrack",
    "aoi_geometry_coordinate_invalid", "aoi_geometry_coordinate_nonfinite",
    "aoi_geometry_json_invalid", "aoi_geometry_point_invalid",
    "aoi_geometry_rectangle_extent_invalid", "aoi_geometry_repeated_vertex",
    "aoi_geometry_resource_limit", "aoi_geometry_self_intersection",
    "aoi_geometry_shape_invalid", "aoi_geometry_too_few_vertices",
    "aoi_geometry_vertices_shape_invalid", "aoi_geometry_zero_area",
    "aoi_identity_missing", "aoi_row_invalid",
})


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if np.ndim(result) == 0 else False
    except (TypeError, ValueError):
        return False


def _json_safe(value: Any) -> Any:
    if _missing(value):
        return None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, pd.DataFrame):
        return [_json_safe(row) for row in value.to_dict("records")]
    if isinstance(value, pd.Series):
        return _json_safe(value.to_dict())
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row(value: Any) -> dict[str, Any]:
    if isinstance(value, pd.Series):
        value = value.to_dict()
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any) -> tuple[str, ...]:
    if _missing(value):
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (Sequence, np.ndarray, pd.Series, pd.Index)):
        return tuple(str(item) for item in value if not _missing(item) and str(item))
    return ()


def _first_present(*values: Any) -> Any:
    return next((value for value in values if not _missing(value)), None)


def _known_metric_id(row: Mapping[str, Any], availability_row: Mapping[str, Any]) -> str | None:
    for item in (row.get("metric_id"), availability_row.get("metric_id")):
        if not _missing(item):
            return str(item)
    return None


def _source_role(field_name: str, explicit_role: Any = None) -> str | None:
    if isinstance(explicit_role, str) and explicit_role in {"signals", "segments", "aois"}:
        return explicit_role
    # These structured fields identify their table without relying on prose.
    if field_name in {"aoi_id", "aoi_name", "shape", "vertices", "xmin", "ymin", "xmax", "ymax", "priority", "overlap_policy"}:
        return "aois"
    if field_name in {"segment_name", "baseline_start_s", "baseline_end_s"}:
        return "segments"
    if field_name in _SIGNAL_FIELDS:
        return "signals"
    if field_name in _AOI_FIELDS:
        return "aois"
    if field_name in _SEGMENT_FIELDS:
        return "segments"
    return None


def _field_target(field_name: str, *, metric_id: str | None, explicit_role: Any = None) -> NavigationTarget | None:
    role = _source_role(field_name, explicit_role)
    if role is not None:
        return NavigationTarget("import", section=role, field=field_name, source_role=role, metric_id=metric_id)
    if field_name in _METADATA_FIELDS:
        return NavigationTarget("import", section="context", field=field_name, metric_id=metric_id)
    return None


def _metadata_target(field_name: str, metric_id: str | None) -> NavigationTarget:
    if field_name in {"aoi_mapping", "aoi_mapping_confirmed", "stimulus_mapping_confirmed"}:
        return NavigationTarget("design", section="aois", field=field_name, source_role="aois", metric_id=metric_id)
    return NavigationTarget("import", section="context", field=field_name, metric_id=metric_id)


def _parameter_target(path: Any, metric_id: str | None) -> NavigationTarget | None:
    if not isinstance(path, str) or not path:
        return None
    parts = path.split(".")
    if len(parts) < 2:
        return None
    root, field = parts[0], ".".join(parts[1:])
    if root == "metadata":
        return _metadata_target(field, metric_id)
    page_by_root = {
        "individual": "metrics", "advanced": "metrics", "group": "group",
        "statistics_design": "design", "processing": "quality",
    }
    page = page_by_root.get(root)
    if page is None:
        return None
    section = root if root in {"individual", "advanced", "group", "statistics_design", "processing"} else None
    return NavigationTarget(page, section=section, field=field, metric_id=metric_id)


def _reason_field_targets(reason: Any, metric_id: str | None) -> list[NavigationTarget]:
    if not isinstance(reason, str):
        return []
    targets: list[NavigationTarget] = []
    for prefix, role in (("missing_fields:", None), ("missing_metadata:", "metadata")):
        for part in reason.split(";"):
            part = part.strip()
            if not part.startswith(prefix):
                continue
            for name in part[len(prefix):].split(","):
                field_name = name.strip()
                if not field_name:
                    continue
                target = (_metadata_target(field_name, metric_id)
                          if role == "metadata" else _field_target(field_name, metric_id=metric_id))
                if target is not None and target not in targets:
                    targets.append(target)
    return targets


def _target_for_diagnostic(
    row: Mapping[str, Any], availability: Mapping[str, Any], metric_id: str | None,
) -> tuple[NavigationTarget | None, list[NavigationTarget], str]:
    details = _row(row.get("details"))
    issue = _row(row.get("issue"))
    if not issue:
        issue = _row(row.get("aoi_geometry_issue"))
    if not issue:
        issue = _row(row.get("preflight_aoi_issue"))
    if not issue:
        issue = _row(availability.get("aoi_geometry_issue"))
    reason_code = row.get("reason_code")
    if _missing(reason_code):
        reason_code = availability.get("reason_code")
    if _missing(reason_code):
        reason_code = issue.get("code", details.get("code"))
    code = str(reason_code) if not _missing(reason_code) else ""
    for candidate in (details, issue):
        if candidate:
            details = {**candidate, **details}
    aoi_id = _first_present(row.get("aoi_id"), details.get("aoi_id"))

    if code in _AOI_REASON_TARGETS or code in _AOI_GEOMETRY_CODES or code.startswith("aoi_geometry_"):
        page, section = _AOI_REASON_TARGETS.get(code, ("design", "aois"))
        canonical_field = _first_present(row.get("canonical_field"), details.get("canonical_field"))
        source_role = "signals" if code == "aoi_stimulus_mapping_unavailable" else "aois"
        target = NavigationTarget(page, section=section, field=_optional_text(canonical_field), source_role=source_role,
                                  aoi_id=None if _missing(aoi_id) else _json_safe(aoi_id), metric_id=metric_id)
        return target, [target], code

    if code == "explicit_interaction_id_required":
        target = NavigationTarget("design", section="identifiers", field="interaction_id", metric_id=metric_id)
        return target, [target], code
    if code == "ambiguous_segment_scope":
        target = NavigationTarget("import", section="segments", field="segment_id", source_role="segments",
                                  metric_id=metric_id)
        return target, [target], code

    direct = _parameter_target(row.get("parameter_path", details.get("parameter_path")), metric_id)
    if direct is not None:
        return direct, [direct], code

    targets: list[NavigationTarget] = []
    explicit_role = row.get("source_role")
    if _missing(explicit_role):
        explicit_role = availability.get("source_role")
    required_fields = _items(_first_present(availability.get("required_fields"), row.get("required_fields")))
    for field_name in required_fields:
        target = _field_target(field_name, metric_id=metric_id, explicit_role=explicit_role)
        if target is not None and target not in targets:
            targets.append(target)
    required_metadata = _items(_first_present(availability.get("required_metadata"), row.get("required_metadata")))
    for field_name in required_metadata:
        target = _metadata_target(field_name, metric_id)
        if target not in targets:
            targets.append(target)
    for target in _reason_field_targets(_first_present(availability.get("reason"), row.get("reason")), metric_id):
        if target not in targets:
            targets.append(target)
    for target in _reason_field_targets(row.get("reason"), metric_id):
        if target not in targets:
            targets.append(target)

    if code in {"experimental_opt_in_required", "method_not_implemented", "method_not_validated"}:
        target = NavigationTarget("metrics", section="catalogue", metric_id=metric_id)
        if target not in targets:
            targets.append(target)
    elif code in {"missing_precondition", "preconditions_met", "missing_metadata"} and not targets:
        target = NavigationTarget("metrics", section="catalogue", metric_id=metric_id)
        targets.append(target)

    return (targets[0] if targets else None), targets, code


def _optional_text(value: Any) -> str | None:
    return None if _missing(value) else str(value)


def _diagnostic_copy(row: Mapping[str, Any], availability: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    merged = dict(row)
    if availability:
        merged["availability"] = dict(availability)
    issue = (_row(row.get("issue")) or _row(row.get("aoi_geometry_issue"))
             or _row(row.get("preflight_aoi_issue")) or _row(availability.get("aoi_geometry_issue")))
    for key, candidates in (
        ("status", (row.get("status"), availability.get("status"))),
        ("reason_code", (row.get("reason_code"), availability.get("reason_code"), issue.get("code"))),
        ("reason", (row.get("reason"), availability.get("reason"), issue.get("message"))),
        ("aoi_id", (row.get("aoi_id"), issue.get("aoi_id"))),
        ("canonical_field", (row.get("canonical_field"), issue.get("canonical_field"))),
    ):
        value = _first_present(*candidates)
        if value is not None:
            merged[key] = value
    merged.setdefault("run_id", run_id)
    return _json_safe(merged)


def build_remediation(
    record: RunRecord,
    row: Mapping[str, Any] | pd.Series,
    *,
    availability_row: Mapping[str, Any] | pd.Series | None = None,
) -> RemediationGuide:
    """Build a factual guide from one recorded result/diagnostic row.

    Only exact reason codes, field lists, AOI issue attributes, and parameter
    paths create a navigation target. Unknown codes remain visible as facts.
    """
    if not isinstance(record, RunRecord):
        raise TypeError("build_remediation expects a RunRecord")
    diagnostic = _row(row)
    availability = _row(availability_row)
    metric_id = _known_metric_id(diagnostic, availability)
    target, targets, code = _target_for_diagnostic(diagnostic, availability, metric_id)
    affected = (metric_id,) if metric_id is not None else ()
    status_value = diagnostic.get("status")
    reason_value = diagnostic.get("reason")
    if _missing(status_value):
        status_value = availability.get("status")
    if _missing(reason_value):
        reason_value = availability.get("reason")
    status = _optional_text(status_value) or "未记录"
    reason = _optional_text(reason_value)
    reason_code = code or "未记录"

    actions: list[RemediationAction] = []
    if targets:
        for item in targets:
            label = item.field or item.section or item.page
            actions.append(RemediationAction(
                title=f"检查{label}",
                detail=(f"根据结构化原因定位到 {item.page} 页面；请核对对应字段和实际证据。"
                        "导航只打开位置，不会修改设置。"),
                target=item,
            ))
    elif code:
        actions.append(RemediationAction(
            title="查看原始诊断",
            detail="此原因代码没有已登记的确定性导航规则；保留原始状态和原因供人工核对。",
        ))
    else:
        actions.append(RemediationAction(
            title="查看原始记录",
            detail="该记录没有结构化原因代码或可定位字段；未推断处理位置。",
        ))

    limitations: list[str] = ["此指引只根据记录中的结构化字段生成，不会自动更改分析设置。"]
    if status.casefold() in {"partial", "unavailable", "blocked", "failed", "cancelled"}:
        limitations.append(f"原状态为 {status}；此状态不会被提升为 complete。")
    if reason is None:
        limitations.append("原始 reason 未记录。")
    if code and target is None:
        limitations.append(f"原因代码 {code} 没有确定性页面映射。")

    return RemediationGuide(
        run_id=str(record.run_id),
        original_diagnostic=_diagnostic_copy(diagnostic, availability, str(record.run_id)),
        affected_metrics=affected,
        actions=tuple(actions),
        navigation_target=target,
        limitations=tuple(dict.fromkeys(limitations)),
    )


__all__ = ["NavigationTarget", "RemediationAction", "RemediationGuide", "build_remediation"]
