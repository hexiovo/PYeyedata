"""Schema-aware selection presets for the GUI.

Presets contain public metric IDs and typed values only.  They never enable a
metric that the live catalogue or current preflight marks unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping


MODULES = ("individual", "group", "advanced")
_MODULE_LABELS = {
    "individual": "個體分析",
    "group": "群體分析",
    "advanced": "高級方法",
}


def schema_version(schema: Mapping[str, Any]) -> str:
    return str(schema.get("pipeline_schema_version") or schema.get("contract_version") or "unknown")


def catalogue_for_module(schema: Mapping[str, Any], module: str) -> list[dict[str, Any]]:
    """Return a detached, de-duplicated view of the live catalogue."""
    if module == "individual":
        source = schema.get("individual", {}).get("catalogue", ())
    elif module == "group":
        source = schema.get("group", {}).get("catalogue", ())
    elif module == "advanced":
        advanced = schema.get("advanced", {})
        source = list(advanced.get("methods", ())) + list(advanced.get("parent_child_specs", ()))
    else:
        return []
    output: dict[str, dict[str, Any]] = {}
    for entry in source:
        if not isinstance(entry, Mapping):
            continue
        metric_id = entry.get("metric_id")
        if not isinstance(metric_id, str) or not metric_id:
            continue
        # The child-specific record carries the most complete parameter set.
        output[metric_id] = dict(entry)
    return list(output.values())


def catalogue_index(schema: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    return {module: {entry["metric_id"]: entry for entry in catalogue_for_module(schema, module)}
            for module in MODULES}


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "available", "complete", "ready"}:
        return True
    if text in {"false", "0", "no", "unavailable", "blocked", "failed"}:
        return False
    return None


def availability_records(availability: Any, metric_id: str, module: str) -> list[dict[str, Any]]:
    if availability is None:
        return []
    if hasattr(availability, "to_dict"):
        try:
            rows = availability.to_dict("records")
        except TypeError:
            rows = []
    elif isinstance(availability, (list, tuple)):
        rows = [dict(row) for row in availability if isinstance(row, Mapping)]
    else:
        rows = []
    matches: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("metric_id", "")) != metric_id:
            continue
        row_module = row.get("module")
        if row_module not in (None, "", module):
            continue
        matches.append(dict(row))
    return matches


def availability_row_ready(row: Mapping[str, Any]) -> bool | None:
    status = str(row.get("status", "")).strip().casefold()
    if status == "preflight_ready":
        return True
    explicit = _as_bool(row.get("available"))
    if explicit is not None:
        return explicit
    if status in {"available", "complete", "ready"}:
        return True
    if status in {"unavailable", "blocked", "failed", "missing"}:
        return False
    return None


def metric_selectable(
    entry: Mapping[str, Any],
    module: str,
    *,
    availability: Any = None,
    allow_experimental: bool = False,
) -> tuple[bool, str]:
    if entry.get("implemented") is False:
        detail = str(entry.get("note") or entry.get("description") or "此指标在当前公共 schema 中标记为未实现。")
        return False, detail
    if bool(entry.get("experimental")) and not allow_experimental:
        return False, "这是研究/实验方法；请显式启用该模块的实验方法后再选择。"
    rows = availability_records(availability, str(entry.get("metric_id", "")), module)
    if not rows:
        # Before preflight, implemented catalogue rows remain selectable as
        # candidates; the real preflight remains authoritative.
        return True, "尚无该指标的前检行；选择后须运行前检确认条件。"
    ready = [availability_row_ready(row) for row in rows]
    if any(value is True for value in ready):
        return True, "至少一个 scope/stream 前检满足；其他条件行仍会单独显示。"
    if all(value is False for value in ready):
        reasons = [str(row.get("reason") or row.get("reason_code") or "前检条件未满足") for row in rows]
        return False, "；".join(dict.fromkeys(reasons))
    return True, "前检状态不完整；须以完整前检结果复核。"


@dataclass(frozen=True, slots=True)
class PresetDefinition:
    preset_id: str
    label: str
    description: str = ""
    kind: str = "fixed"
    schema_version: str = "unknown"
    target_modules: tuple[str, ...] = MODULES
    selections: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    module: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class PresetResolution:
    selections: Mapping[str, tuple[str, ...]]
    blocked: Mapping[str, tuple[str, ...]]
    unknown: Mapping[str, tuple[str, ...]]
    parameters: Mapping[str, Any]
    incompatible_schema: bool = False


def builtin_presets(schema: Mapping[str, Any]) -> list[PresetDefinition]:
    version = schema_version(schema)
    result = [
        PresetDefinition(
            "basic-r1",
            "基础默认 · R1",
            "选择当前公共目录中已实现的 R1 基础指标；前检或实验方法仍按当前状态过滤。",
            kind="basic_r1",
            schema_version=version,
            target_modules=MODULES,
        ),
        PresetDefinition(
            "all-available",
            "全部可用",
            "按当前完整目录与前检选择可用项；不会勾选灰色或未实现条目。",
            kind="all_available",
            schema_version=version,
            target_modules=MODULES,
        ),
    ]
    for module in MODULES:
        categories = sorted({
            str(entry.get("category"))
            for entry in catalogue_for_module(schema, module)
            if entry.get("category") not in (None, "")
        })
        for category in categories:
            result.append(PresetDefinition(
                f"theme:{module}:{category}",
                f"主題 · {_MODULE_LABELS[module]} · {category}",
                f"只替換{_MODULE_LABELS[module]}中的“{category}”指标；其他面板保持原选择。",
                kind="theme",
                schema_version=version,
                target_modules=(module,),
                module=module,
                category=category,
            ))
    return result


def _requested_ids(
    preset: PresetDefinition,
    schema: Mapping[str, Any],
    *,
    availability: Any,
    allow_experimental: bool | Mapping[str, bool],
) -> dict[str, tuple[str, ...]]:
    index = catalogue_index(schema)
    selected: dict[str, tuple[str, ...]] = {}
    for module in preset.target_modules:
        module_allow_experimental = (
            bool(allow_experimental.get(module, False))
            if isinstance(allow_experimental, Mapping)
            else bool(allow_experimental)
        )
        if preset.kind == "basic_r1":
            candidates = [
                metric_id for metric_id, entry in index[module].items()
                if str(entry.get("tier", "")).upper() == "R1"
            ]
        elif preset.kind == "all_available":
            candidates = list(index[module])
        elif preset.kind == "theme" and preset.module == module:
            candidates = [
                metric_id for metric_id, entry in index[module].items()
                if str(entry.get("category", "")) == str(preset.category)
            ]
        else:
            candidates = list(preset.selections.get(module, ()))
        selected[module] = tuple(sorted(dict.fromkeys(candidates)))
    return selected


def resolve_preset(
    preset: PresetDefinition,
    schema: Mapping[str, Any],
    *,
    current_selections: Mapping[str, tuple[str, ...] | list[str]] | None = None,
    availability: Any = None,
    allow_experimental: bool | Mapping[str, bool] = False,
) -> PresetResolution:
    """Resolve a preset against the live catalogue and current availability."""
    current = current_selections or {}
    index = catalogue_index(schema)
    requested = _requested_ids(
        preset, schema, availability=availability, allow_experimental=allow_experimental
    )
    selections = {module: tuple(current.get(module, ())) for module in MODULES}
    blocked: dict[str, tuple[str, ...]] = {}
    unknown: dict[str, tuple[str, ...]] = {}
    for module in preset.target_modules:
        module_allow_experimental = (
            bool(allow_experimental.get(module, False))
            if isinstance(allow_experimental, Mapping)
            else bool(allow_experimental)
        )
        target_ids = requested.get(module, ())
        accepted: list[str] = []
        rejected: list[str] = []
        missing: list[str] = []
        for metric_id in target_ids:
            entry = index[module].get(metric_id)
            if entry is None:
                missing.append(metric_id)
                continue
            can_select, _ = metric_selectable(
                entry, module, availability=availability,
                allow_experimental=module_allow_experimental
            )
            (accepted if can_select else rejected).append(metric_id)
        # User-selected unavailable IDs in a saved custom preset are reported
        # instead of silently restored.
        if preset.kind == "custom":
            raw_ids = tuple(preset.selections.get(module, ()))
            for metric_id in raw_ids:
                if metric_id not in index[module]:
                    missing.append(metric_id)
                elif metric_id not in rejected and metric_id not in accepted:
                    can_select, _ = metric_selectable(
                        index[module][metric_id], module, availability=availability,
                        allow_experimental=module_allow_experimental,
                    )
                    (accepted if can_select else rejected).append(metric_id)
        selections[module] = tuple(sorted(dict.fromkeys(accepted)))
        if rejected:
            blocked[module] = tuple(sorted(dict.fromkeys(rejected)))
        if missing:
            unknown[module] = tuple(sorted(dict.fromkeys(missing)))
    return PresetResolution(
        selections=selections,
        blocked=blocked,
        unknown=unknown,
        parameters=dict(preset.parameters),
        incompatible_schema=preset.schema_version not in ("unknown", schema_version(schema)),
    )


def selection_diff(
    current: Mapping[str, tuple[str, ...] | list[str]],
    target: Mapping[str, tuple[str, ...] | list[str]],
    modules: tuple[str, ...] = MODULES,
) -> dict[str, dict[str, tuple[str, ...]]]:
    diff: dict[str, dict[str, tuple[str, ...]]] = {}
    for module in modules:
        before = set(current.get(module, ()))
        after = set(target.get(module, ()))
        diff[module] = {
            "added": tuple(sorted(after - before)),
            "removed": tuple(sorted(before - after)),
            "retained": tuple(sorted(before & after)),
        }
    return diff


def save_custom_preset(
    path: str | Path,
    *,
    name: str,
    selections: Mapping[str, tuple[str, ...] | list[str]],
    parameters: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> None:
    payload = {
        "format_version": 1,
        "name": name,
        "schema_version": schema_version(schema),
        "selections": {module: list(selections.get(module, ())) for module in MODULES},
        "parameters": dict(parameters),
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
                          encoding="utf-8")


def load_custom_preset(path: str | Path) -> PresetDefinition:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("format_version") != 1:
        raise ValueError("不支持的自定义预设文件格式。")
    selections_value = payload.get("selections", {})
    if not isinstance(selections_value, Mapping):
        raise ValueError("自定义预设的 selections 必须是对象。")
    selections = {
        module: tuple(str(value) for value in selections_value.get(module, ()) if isinstance(value, str))
        for module in MODULES
    }
    parameters = payload.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise ValueError("自定义预设的 parameters 必须是对象。")
    return PresetDefinition(
        preset_id="custom:" + str(payload.get("name") or Path(path).stem),
        label=str(payload.get("name") or Path(path).stem),
        description="自定义预设；应用前会按当前 schema 和前检检查兼容项。",
        kind="custom",
        schema_version=str(payload.get("schema_version", "unknown")),
        target_modules=MODULES,
        selections=selections,
        parameters=dict(parameters),
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot store custom preset value of type {type(value).__name__}")
