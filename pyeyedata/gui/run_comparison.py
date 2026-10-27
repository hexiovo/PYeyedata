"""Conservative comparison helpers for immutable analysis run records."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .models import RunRecord


# Run-specific and result-description fields deliberately stay out of this key.
# Unit, algorithm, status, and parameter_ref are displayed as compatibility
# evidence; they must not prevent the two rows from meeting for review.
_IDENTITY_COLUMNS = (
    "metric_id", "subject_id", "session_id", "interaction_id", "pair_id",
    "segment_id", "segment_instance_id", "trial", "condition",
    "analysis_stream_id", "eye_label", "stream_parent_refs",
    "physical_partition_id", "support_ref", "dependence_ref",
    "source_id", "stimulus_id", "signal", "role", "event_type",
    "event_id", "observation_id", "window_id", "window_start_s",
    "window_end_s", "range_start_s", "range_end_s", "start_s", "end_s",
    "time_domain_start_s", "time_domain_end_s",
)
_SCOPE_COLUMNS = tuple(column for column in _IDENTITY_COLUMNS if column != "metric_id")
_RANGE_PAIRS = (
    ("range_start_s", "range_end_s"),
    ("window_start_s", "window_end_s"),
    ("time_domain_start_s", "time_domain_end_s"),
    ("start_s", "end_s"),
)
_USABLE_ROW_STATUSES = frozenset({"complete", "available"})
_MISSING = object()


@dataclass(frozen=True)
class ComparisonResult:
    """Detached tables for a read-only comparison of two recorded runs."""

    left_run_id: str
    right_run_id: str
    parameter_differences: pd.DataFrame
    source_status_summary: pd.DataFrame
    metrics: pd.DataFrame
    warnings: tuple[str, ...] = ()

    @property
    def parameter_changes(self) -> pd.DataFrame:
        """Compatibility alias used by hosts that label this table as changes."""
        return self.parameter_differences.copy(deep=True)

    @property
    def metric_comparison(self) -> pd.DataFrame:
        return self.metrics.copy(deep=True)


@dataclass(frozen=True)
class _SourceIdentity:
    fingerprint: str | None
    hashes: tuple[tuple[str, str], ...]
    label: str


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if np.ndim(result) == 0 else False
    except (TypeError, ValueError):
        return False


def _canonical(value: Any) -> Any:
    """Return a stable, type-aware JSON value for identity/equality checks."""
    if _missing(value):
        return {"$missing": True}
    if isinstance(value, np.generic):
        return _canonical(value.item())
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return {"$datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest(), "$length": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return {"$sequence": [_canonical(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_canonical(item) for item in value]
        return {"$set": sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))}
    if isinstance(value, bool):
        return {"$bool": value}
    if isinstance(value, int):
        return {"$int": str(value)}
    if isinstance(value, float):
        return {"$float": value.hex() if math.isfinite(value) else str(value)}
    if isinstance(value, str):
        return {"$str": value}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, pd.DataFrame):
        payload = value.to_json(orient="split", date_format="iso", double_precision=15)
        return {"$dataframe_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
    return {"$type": type(value).__name__, "$value": str(value)}


def _value_key(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _display(value: Any) -> Any:
    if _missing(value):
        return "NA"
    if isinstance(value, np.generic):
        return _display(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return "NA"
    if isinstance(value, Mapping):
        return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, (tuple, list, set, frozenset)):
        return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, pd.DataFrame):
        return f"DataFrame ({len(value):,} 行 × {len(value.columns):,} 列)"
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _record_details(record: RunRecord, warnings: list[str]) -> dict[str, Any]:
    try:
        value = record.snapshot_details()
    except Exception as exc:  # keep archive inspection failures visible, not fatal to UI
        warnings.append(f"运行 {record.run_id} 的参数/来源快照读取失败：{type(exc).__name__}: {exc}")
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _record_metadata(record: RunRecord, warnings: list[str]) -> dict[str, Any]:
    try:
        value = record.metadata()
    except Exception as exc:
        warnings.append(f"运行 {record.run_id} 的元数据读取失败：{type(exc).__name__}: {exc}")
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _metadata_snapshot_details(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Read the documented detached archive snapshot envelope when present."""
    parameter_snapshot = _mapping(metadata.get("parameter_snapshot"))
    metadata_layer = _mapping(parameter_snapshot.get("metadata"))
    archive_snapshot = _mapping(metadata_layer.get("app_archive_snapshot"))
    details = _mapping(archive_snapshot.get("details"))
    return dict(details)


def _snapshot_parameters(details: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    value = details.get("parameters")
    if isinstance(value, Mapping):
        return dict(value)
    archive_details = _metadata_snapshot_details(metadata)
    value = archive_details.get("parameters")
    return dict(value) if isinstance(value, Mapping) else None


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping) and item:
            out.update(_flatten(item, path))
        else:
            out[path] = item
    return out


def _parameter_differences(
    left_params: Mapping[str, Any] | None,
    right_params: Mapping[str, Any] | None,
) -> pd.DataFrame:
    columns = ("parameter_path", "left_value", "right_value", "comparison")
    if left_params is None or right_params is None:
        side = "左侧" if left_params is None else "右侧"
        return pd.DataFrame([{
            "parameter_path": "参数快照",
            "left_value": "未知" if left_params is None else "可用",
            "right_value": "未知" if right_params is None else "可用",
            "comparison": f"{side}参数快照不可用",
        }], columns=columns)
    left_flat, right_flat = _flatten(left_params), _flatten(right_params)
    rows: list[dict[str, Any]] = []
    for path in sorted(set(left_flat) | set(right_flat)):
        left_value = left_flat.get(path, _MISSING)
        right_value = right_flat.get(path, _MISSING)
        same = left_value is not _MISSING and right_value is not _MISSING and _value_key(left_value) == _value_key(right_value)
        if not same:
            rows.append({
                "parameter_path": path,
                "left_value": "未记录" if left_value is _MISSING else _display(left_value),
                "right_value": "未记录" if right_value is _MISSING else _display(right_value),
                "comparison": "不同",
            })
    if not rows:
        rows.append({"parameter_path": "全部参数", "left_value": "相同", "right_value": "相同", "comparison": "无差异"})
    return pd.DataFrame(rows, columns=columns)


def _source_identity(details: Mapping[str, Any], metadata: Mapping[str, Any]) -> _SourceIdentity:
    archived_details = _metadata_snapshot_details(metadata)
    fingerprint = details.get("dataset_fingerprint") or archived_details.get("dataset_fingerprint")
    fingerprint = str(fingerprint).strip() if isinstance(fingerprint, str) and fingerprint.strip() else None
    raw_hashes = details.get("source_hashes")
    if not isinstance(raw_hashes, Mapping):
        raw_hashes = archived_details.get("source_hashes")
    hashes: tuple[tuple[str, str], ...] = ()
    if isinstance(raw_hashes, Mapping):
        hashes = tuple(sorted((str(name), str(digest)) for name, digest in raw_hashes.items()
                              if isinstance(digest, str) and digest.strip()))
    if fingerprint:
        label = f"dataset SHA-256 {fingerprint}"
    elif hashes:
        label = f"{len(hashes)} 个来源文件哈希：" + ", ".join(f"{name}={digest[:12]}…" for name, digest in hashes)
    else:
        label = "来源身份未记录"
    return _SourceIdentity(fingerprint, hashes, label)


def _source_compatibility(left: _SourceIdentity, right: _SourceIdentity) -> tuple[bool | None, str]:
    if left.fingerprint and right.fingerprint:
        if left.fingerprint != right.fingerprint:
            return False, "规范化输入指纹不同"
        if left.hashes and right.hashes and left.hashes != right.hashes:
            return False, "规范化输入指纹相同但来源文件哈希冲突"
        return True, "规范化输入指纹相同"
    if left.hashes and right.hashes:
        if left.hashes == right.hashes:
            return True, "来源文件哈希完全相同；规范化输入指纹未同时记录"
        return False, "来源文件哈希不同"
    return None, "来源身份不完整，无法确认同源"


def _get_table(record: RunRecord, name: str, warnings: list[str]) -> pd.DataFrame:
    try:
        frame = record.table_copy(name)
    except KeyError:
        return pd.DataFrame()
    except Exception as exc:
        warnings.append(f"运行 {record.run_id} 的 {name} 表读取失败：{type(exc).__name__}: {exc}")
        return pd.DataFrame()
    if not isinstance(frame, pd.DataFrame):
        warnings.append(f"运行 {record.run_id} 的 {name} 表不是 DataFrame。")
        return pd.DataFrame()
    return frame.copy(deep=True)


def _status(record: RunRecord, metadata: Mapping[str, Any]) -> str:
    # RunRecord.status is the effective history status (for example, stale at
    # return); prefer it over the original backend metadata when gating deltas.
    value = record.status
    if _missing(value) or str(getattr(value, "value", value)).strip().casefold() in {"", "unknown", "none"}:
        value = metadata.get("execution_status", "unknown")
    value = getattr(value, "value", value)
    text = str(value or "unknown").strip().casefold()
    return text or "unknown"


def _availability_summary(frame: pd.DataFrame) -> str:
    if frame.empty or "status" not in frame:
        return "未记录"
    counts = Counter(str(value) for value in frame["status"].dropna())
    return ", ".join(f"{name}: {counts[name]}" for name in sorted(counts)) or "未记录"


def _result_status_summary(frame: pd.DataFrame) -> str:
    if frame.empty or "status" not in frame:
        return "未记录"
    counts = Counter(str(value) for value in frame["status"].dropna())
    return ", ".join(f"{name}: {counts[name]}" for name in sorted(counts)) or "未记录"


def _source_status_summary(
    left: RunRecord,
    right: RunRecord,
    left_details: Mapping[str, Any],
    right_details: Mapping[str, Any],
    left_metadata: Mapping[str, Any],
    right_metadata: Mapping[str, Any],
    left_metrics: pd.DataFrame,
    right_metrics: pd.DataFrame,
    left_availability: pd.DataFrame,
    right_availability: pd.DataFrame,
    source_state: bool | None,
    source_reason: str,
) -> pd.DataFrame:
    left_source = _source_identity(left_details, left_metadata)
    right_source = _source_identity(right_details, right_metadata)
    rows: list[dict[str, Any]] = [
        {"category": "运行状态", "metric_id": "", "left": f"{left.phase} · {_status(left, left_metadata)}",
         "right": f"{right.phase} · {_status(right, right_metadata)}", "comparison": "仅展示"},
        {"category": "输入来源", "metric_id": "", "left": left_source.label,
         "right": right_source.label,
         "comparison": "相同" if source_state is True else "不同" if source_state is False else "未知",
         "reason": source_reason},
        {"category": "指标状态", "metric_id": "", "left": _result_status_summary(left_metrics),
         "right": _result_status_summary(right_metrics), "comparison": "仅展示"},
        {"category": "前提状态", "metric_id": "", "left": _availability_summary(left_availability),
         "right": _availability_summary(right_availability), "comparison": "仅展示"},
    ]

    def by_metric(frame: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
        if "metric_id" not in frame:
            return {}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            if not _missing(row.get("metric_id")):
                groups[str(row["metric_id"])].append(row)
        return groups

    left_by, right_by = by_metric(left_availability), by_metric(right_availability)
    for metric_id in sorted(set(left_by) | set(right_by)):
        def summarize(values: list[dict[str, Any]]) -> str:
            if not values:
                return "未记录"
            return "; ".join(
                "/".join(str(row.get(name)) for name in ("status", "reason_code")
                         if not _missing(row.get(name))) or "未知"
                for row in values
            )

        left_value, right_value = summarize(left_by.get(metric_id, [])), summarize(right_by.get(metric_id, []))
        rows.append({"category": "指标前提", "metric_id": metric_id, "left": left_value,
                     "right": right_value, "comparison": "相同" if left_value == right_value else "不同"})
    return pd.DataFrame(rows)


def _identity_key(row: Mapping[str, Any], columns: tuple[str, ...]) -> tuple[Any, ...] | None:
    if _missing(row.get("metric_id")):
        return None
    if not any(not _missing(row.get(column)) for column in _SCOPE_COLUMNS if column in columns):
        return None
    return tuple(_value_key(row.get(column)) for column in columns)


def _identity_display(row: Mapping[str, Any], columns: tuple[str, ...]) -> str:
    values = {column: _display(row.get(column)) for column in columns if not _missing(row.get(column))}
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _row_range_compatibility(
    left_row: Mapping[str, Any],
    right_row: Mapping[str, Any],
    source_state: bool | None,
    columns: tuple[str, ...],
) -> tuple[bool | None, str]:
    observed_range = False
    for start_column, end_column in _RANGE_PAIRS:
        if start_column not in columns or end_column not in columns:
            continue
        left_start, left_end = left_row.get(start_column), left_row.get(end_column)
        right_start, right_end = right_row.get(start_column), right_row.get(end_column)
        if any(_missing(value) for value in (left_start, left_end, right_start, right_end)):
            continue
        observed_range = True
        if _value_key(left_start) != _value_key(right_start) or _value_key(left_end) != _value_key(right_end):
            return False, f"分析范围 {start_column}/{end_column} 不同"
    if observed_range:
        return True, "显式分析范围相同"
    if source_state is True:
        return True, "规范化数据相同且完整语义范围身份相同"
    if source_state is False:
        return False, "来源不同，不能确认分析范围可比"
    return None, "缺少显式范围或同源指纹，无法确认分析范围可比"


def _metric_rows(
    left: RunRecord,
    right: RunRecord,
    left_metrics: pd.DataFrame,
    right_metrics: pd.DataFrame,
    left_metadata: Mapping[str, Any],
    right_metadata: Mapping[str, Any],
    source_state: bool | None,
    source_reason: str,
    warnings: list[str],
) -> pd.DataFrame:
    columns = (
        "identity", "metric_id", "left_value", "left_unit", "right_value", "right_unit", "delta",
        "delta_unit", "left_status", "right_status", "left_reason_code", "right_reason_code",
        "left_algorithm_version", "right_algorithm_version", "range_compatibility",
        "comparison_status", "reason",
    )
    if left_metrics.empty and right_metrics.empty:
        warnings.append("两次运行都没有可读取的 metrics 表。")
        return pd.DataFrame(columns=columns)
    if "metric_id" not in left_metrics or "metric_id" not in right_metrics:
        reason = "至少一侧 metrics 表缺少 metric_id；保留单侧行但不执行指标匹配。"
        warnings.append(reason)
        rows: list[dict[str, Any]] = []
        for side, frame in (("left", left_metrics), ("right", right_metrics)):
            available_identity = tuple(name for name in _IDENTITY_COLUMNS if name in frame)
            for raw in frame.to_dict("records"):
                row = {name: "" for name in columns}
                row.update({
                    "identity": _identity_display(raw, available_identity),
                    "metric_id": _display(raw.get("metric_id")),
                    "delta": np.nan,
                    f"{side}_value": _display(raw.get("value")),
                    f"{side}_unit": _display(raw.get("unit")),
                    f"{side}_status": _display(raw.get("status")),
                    f"{side}_reason_code": _display(raw.get("reason_code")),
                    f"{side}_algorithm_version": _display(raw.get("algorithm_version")),
                    "comparison_status": "identity_schema_mismatch",
                    "reason": reason,
                })
                rows.append(row)
        return pd.DataFrame(rows, columns=columns)

    left_identity_set = {name for name in _IDENTITY_COLUMNS if name in left_metrics}
    right_identity_set = {name for name in _IDENTITY_COLUMNS if name in right_metrics}
    if left_identity_set != right_identity_set:
        missing_left = sorted(right_identity_set - left_identity_set)
        missing_right = sorted(left_identity_set - right_identity_set)
        reason = "两侧结果缺少相同的语义身份列；为避免错配，未计算差值。"
        warnings.append(reason + (f" 左侧缺少 {missing_left}。" if missing_left else "")
                        + (f" 右侧缺少 {missing_right}。" if missing_right else ""))
        rows: list[dict[str, Any]] = []
        for side, frame in (("left", left_metrics), ("right", right_metrics)):
            for raw in frame.to_dict("records"):
                row = {name: "" for name in columns}
                row.update({
                    "identity": _identity_display(raw, tuple(name for name in _IDENTITY_COLUMNS if name in frame)),
                    "metric_id": _display(raw.get("metric_id")),
                    "delta": np.nan,
                    f"{side}_value": _display(raw.get("value")),
                    f"{side}_unit": _display(raw.get("unit")),
                    f"{side}_status": _display(raw.get("status")),
                    f"{side}_reason_code": _display(raw.get("reason_code")),
                    f"{side}_algorithm_version": _display(raw.get("algorithm_version")),
                    "comparison_status": "identity_schema_mismatch",
                    "reason": reason,
                })
                rows.append(row)
        return pd.DataFrame(rows, columns=columns)

    identity_columns = tuple(name for name in _IDENTITY_COLUMNS if name in left_metrics and name in right_metrics)
    if "metric_id" not in identity_columns:
        return pd.DataFrame(columns=columns)
    left_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    right_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    invalid_left: list[dict[str, Any]] = []
    invalid_right: list[dict[str, Any]] = []
    for raw in left_metrics.to_dict("records"):
        key = _identity_key(raw, identity_columns)
        (left_groups[key] if key is not None else invalid_left).append(raw)
    for raw in right_metrics.to_dict("records"):
        key = _identity_key(raw, identity_columns)
        (right_groups[key] if key is not None else invalid_right).append(raw)

    if invalid_left or invalid_right:
        warnings.append(f"存在缺少 metric_id 或有效范围身份的指标行：左侧 {len(invalid_left)} 行，右侧 {len(invalid_right)} 行；这些行不会按位置匹配。")

    rows: list[dict[str, Any]] = []
    for key in sorted(set(left_groups) | set(right_groups)):
        left_rows, right_rows = left_groups.get(key, []), right_groups.get(key, [])
        left_row = left_rows[0] if len(left_rows) == 1 else None
        right_row = right_rows[0] if len(right_rows) == 1 else None
        identity_row = left_row or right_row or (left_rows or right_rows)[0]
        base = {name: "" for name in columns}
        base.update({"identity": _identity_display(identity_row, identity_columns),
                     "metric_id": _display(identity_row.get("metric_id")),
                     "delta": np.nan, "delta_unit": ""})
        if len(left_rows) > 1 or len(right_rows) > 1:
            base.update({
                "left_value": _display([row.get("value") for row in left_rows]) if left_rows else "NA",
                "left_unit": _display([row.get("unit") for row in left_rows]) if left_rows else "NA",
                "left_status": _display([row.get("status") for row in left_rows]) if left_rows else "NA",
                "left_reason_code": _display([row.get("reason_code") for row in left_rows]) if left_rows else "NA",
                "left_algorithm_version": _display([row.get("algorithm_version") for row in left_rows]) if left_rows else "NA",
                "right_value": _display([row.get("value") for row in right_rows]) if right_rows else "NA",
                "right_unit": _display([row.get("unit") for row in right_rows]) if right_rows else "NA",
                "right_status": _display([row.get("status") for row in right_rows]) if right_rows else "NA",
                "right_reason_code": _display([row.get("reason_code") for row in right_rows]) if right_rows else "NA",
                "right_algorithm_version": _display([row.get("algorithm_version") for row in right_rows]) if right_rows else "NA",
                "comparison_status": "duplicate_identity",
                "reason": "至少一侧存在重复语义身份；保留双方值，不计算差值。",
            })
            rows.append(base)
            continue
        if left_row is None or right_row is None:
            present = right_row if left_row is None else left_row
            side = "right" if left_row is None else "left"
            base.update({
                f"{side}_value": _display(present.get("value")),
                f"{side}_unit": _display(present.get("unit")),
                f"{side}_status": _display(present.get("status")),
                f"{side}_reason_code": _display(present.get("reason_code")),
                f"{side}_algorithm_version": _display(present.get("algorithm_version")),
                "comparison_status": "missing_left" if left_row is None else "missing_right",
                "reason": "完整语义身份仅存在于一侧；未寻找近似行或按位置配对。",
            })
            rows.append(base)
            continue

        left_value, right_value = left_row.get("value"), right_row.get("value")
        left_unit, right_unit = left_row.get("unit"), right_row.get("unit")
        left_status, right_status = left_row.get("status"), right_row.get("status")
        left_algorithm, right_algorithm = left_row.get("algorithm_version"), right_row.get("algorithm_version")
        range_state, range_reason = _row_range_compatibility(left_row, right_row, source_state, identity_columns)
        delta: float | None = None
        reason = ""
        comparison_status = "comparable"
        if source_state is False:
            comparison_status, reason = "source_identity_mismatch", source_reason
        elif source_state is None:
            comparison_status, reason = "source_identity_unknown", source_reason
        elif _missing(left_unit) or _missing(right_unit):
            comparison_status, reason = "unit_unknown", "至少一侧未记录单位。"
        elif _value_key(left_unit) != _value_key(right_unit):
            comparison_status, reason = "unit_mismatch", "单位不同。"
        elif _missing(left_algorithm) or _missing(right_algorithm):
            comparison_status, reason = "algorithm_unknown", "至少一侧未记录算法版本。"
        elif _value_key(left_algorithm) != _value_key(right_algorithm):
            comparison_status, reason = "algorithm_mismatch", "算法版本不同。"
        elif range_state is False:
            comparison_status, reason = "range_mismatch", range_reason
        elif range_state is None:
            comparison_status, reason = "range_unknown", range_reason
        elif _status(left, left_metadata) != "complete" or _status(right, right_metadata) != "complete":
            comparison_status, reason = "run_status_not_complete", "至少一侧运行状态不是 complete。"
        elif str(getattr(left_status, "value", left_status) or "").casefold() not in _USABLE_ROW_STATUSES:
            comparison_status, reason = "left_metric_status_unavailable", f"左侧指标状态为 {_display(left_status)}。"
        elif str(getattr(right_status, "value", right_status) or "").casefold() not in _USABLE_ROW_STATUSES:
            comparison_status, reason = "right_metric_status_unavailable", f"右侧指标状态为 {_display(right_status)}。"
        else:
            left_number, right_number = _finite_number(left_value), _finite_number(right_value)
            if left_number is None or right_number is None:
                comparison_status, reason = "value_not_finite", "至少一侧数值不是有限数。"
            else:
                candidate_delta = right_number - left_number
                if not math.isfinite(candidate_delta):
                    comparison_status, reason = "delta_not_finite", "两侧数值有限，但差值溢出为非有限数；未给出差值。"
                else:
                    delta = candidate_delta
                    reason = "单位、算法、来源、范围、运行和指标状态均满足直接比较条件。"
        base.update({
            "left_value": _display(left_value), "left_unit": _display(left_unit),
            "right_value": _display(right_value), "right_unit": _display(right_unit),
            "delta": delta if delta is not None else np.nan,
            "delta_unit": _display(left_unit) if delta is not None else "",
            "left_status": _display(left_status), "right_status": _display(right_status),
            "left_reason_code": _display(left_row.get("reason_code")),
            "right_reason_code": _display(right_row.get("reason_code")),
            "left_algorithm_version": _display(left_algorithm),
            "right_algorithm_version": _display(right_algorithm),
            "range_compatibility": "相同" if range_state is True else "不同" if range_state is False else "未知",
            "comparison_status": comparison_status,
            "reason": reason,
        })
        rows.append(base)

    for side, invalid_rows in (("left", invalid_left), ("right", invalid_right)):
        for raw in invalid_rows:
            row = {name: "" for name in columns}
            row.update({
                "identity": _identity_display(raw, identity_columns),
                "metric_id": _display(raw.get("metric_id")),
                "delta": np.nan,
                f"{side}_value": _display(raw.get("value")),
                f"{side}_unit": _display(raw.get("unit")),
                f"{side}_status": _display(raw.get("status")),
                f"{side}_reason_code": _display(raw.get("reason_code")),
                f"{side}_algorithm_version": _display(raw.get("algorithm_version")),
                "comparison_status": "identity_incomplete",
                "reason": "缺少 metric_id 或至少一个非空范围身份字段；未配对。",
            })
            rows.append(row)
    rows.sort(key=lambda row: (str(row.get("identity", "")), str(row.get("comparison_status", ""))))
    return pd.DataFrame(rows, columns=columns)


def compare_runs(left: RunRecord, right: RunRecord) -> ComparisonResult:
    """Compare two immutable runs without matching by table position.

    A delta is produced only for a unique full semantic key, known identical
    source, equal units and algorithm versions, equal/confirmed analysis range,
    complete run status, complete row status, and finite values. Every rejected
    pairing remains visible with a reason.
    """
    if not isinstance(left, RunRecord) or not isinstance(right, RunRecord):
        raise TypeError("compare_runs expects two RunRecord values")
    warnings: list[str] = []
    left_details, right_details = _record_details(left, warnings), _record_details(right, warnings)
    left_metadata, right_metadata = _record_metadata(left, warnings), _record_metadata(right, warnings)
    left_params = _snapshot_parameters(left_details, left_metadata)
    right_params = _snapshot_parameters(right_details, right_metadata)
    if left_params is None or right_params is None:
        warnings.append("至少一侧没有可读取的参数快照；参数差异显示为未知。")
    left_metrics = _get_table(left, "metrics", warnings)
    right_metrics = _get_table(right, "metrics", warnings)
    left_availability = _get_table(left, "availability", warnings)
    right_availability = _get_table(right, "availability", warnings)
    left_source = _source_identity(left_details, left_metadata)
    right_source = _source_identity(right_details, right_metadata)
    source_state, source_reason = _source_compatibility(left_source, right_source)
    if source_state is not True:
        warnings.append(f"指标直接差值已受限：{source_reason}。")
    metrics = _metric_rows(left, right, left_metrics, right_metrics, left_metadata,
                           right_metadata, source_state, source_reason, warnings)
    summary = _source_status_summary(
        left, right, left_details, right_details, left_metadata, right_metadata,
        left_metrics, right_metrics, left_availability, right_availability,
        source_state, source_reason,
    )
    return ComparisonResult(
        left_run_id=str(left.run_id), right_run_id=str(right.run_id),
        parameter_differences=_parameter_differences(left_params, right_params),
        source_status_summary=summary,
        metrics=metrics,
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = ["ComparisonResult", "compare_runs"]

