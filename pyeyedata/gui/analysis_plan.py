"""Portable analysis settings; never a container for data or run results."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from pyeyedata.app_info import APP_VERSION
from .models import SelectionSpec

PLAN_SCHEMA = "pyeyedata.analysis-plan/v1"
MAX_PLAN_BYTES = 2 * 1024 * 1024
CONFIG_KEYS = {"selection", "experimental", "processing", "metrics", "group", "statistics_design"}
MODULES = ("individual", "group", "advanced")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} 应为具名设置，文件内容不正确。")
    return dict(value)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"方案中重复出现 {key}，请重新保存方案。")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"方案中含有无效数字 {value}。")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("方案中含有超出范围的数字。")
    return number


def make_plan(configuration: Mapping[str, Any], *, pipeline_schema: str, name: str) -> dict[str, Any]:
    payload = {"schema": PLAN_SCHEMA, "application_version": APP_VERSION,
               "pipeline_schema_version": pipeline_schema, "name": name,
               "configuration": deepcopy(dict(configuration))}
    # This also refuses NaN/Inf and values that cannot be represented as JSON.
    return json.loads(_json(payload), object_pairs_hook=_pairs)


def read_plan(path: str | os.PathLike[str]) -> dict[str, Any]:
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_PLAN_BYTES + 1)
    if len(content) > MAX_PLAN_BYTES:
        raise ValueError("方案文件超过 2 MB；请确认选择的是分析方案，而不是数据或档案。")
    try:
        return _object(json.loads(content.decode("utf-8-sig"), object_pairs_hook=_pairs,
                                  parse_constant=_reject_constant, parse_float=_float), "方案")
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("无法读懂此方案文件。请选用本软件保存的 .pyeplan 文件。") from exc


def write_plan(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> Path:
    destination = Path(path)
    content = (_json(payload) + "\n").encode("utf-8")
    if len(content) > MAX_PLAN_BYTES:
        raise ValueError("方案内容超过 2 MB，未保存。")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".pyeplan-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return destination


def validate_plan(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    """Stage all page setters off-screen before any live page can be changed."""
    from .design_page import DesignPage, _OPTION_SPECS
    from .forms import TypedParameterForm
    from .group_page import GroupDesignPage
    from .method_registry import group_parameter_fields
    from .metrics_page import MetricsPage
    from .presets import catalogue_for_module
    from .quality_page import QualityPage
    from pyeyedata import ProcessingConfig

    raw = _object(payload, "方案")
    if set(raw) != {"schema", "application_version", "pipeline_schema_version", "name", "configuration"}:
        raise ValueError("方案项目不完整或含有当前版本不支持的项目。")
    if raw["schema"] != PLAN_SCHEMA:
        raise ValueError("这不是受支持的分析方案文件；数据请到“数据导入”打开，档案请到“结果与追溯”打开。")
    if raw["pipeline_schema_version"] != str(schema["pipeline_schema_version"]):
        raise ValueError("此方案的分析格式与当前软件不同，未应用。请用匹配版本重新保存方案。")
    if not isinstance(raw["name"], str) or not raw["name"].strip() or len(raw["name"]) > 200:
        raise ValueError("方案名称须为 1 至 200 个字符。")
    if not isinstance(raw["application_version"], str):
        raise ValueError("方案缺少保存时的软件版本。")
    config = deepcopy(_object(raw["configuration"], "方案设置"))
    if set(config) != CONFIG_KEYS:
        raise ValueError("方案设置不完整或含有未知项目，未应用。")
    from .method_registry import COMMON_PLAN_DEFAULTS
    metric_settings = _object(config["metrics"], "方法参数")
    individual_settings = _object(metric_settings.get("individual", {}), "个体方法参数")
    for name, default in COMMON_PLAN_DEFAULTS.items():
        individual_settings.setdefault(name, deepcopy(default))
    metric_settings["individual"] = individual_settings
    config["metrics"] = metric_settings
    processing_settings = _object(config["processing"], "预处理")
    processing_defaults = ProcessingConfig().to_dict()
    for name in (
        "pupil_interpolation_method", "pupil_speed_mad_enabled", "pupil_speed_mad_n",
        "pupil_blink_padding_enabled", "pupil_blink_padding_before_s", "pupil_blink_padding_after_s",
    ):
        processing_settings.setdefault(name, deepcopy(processing_defaults[name]))
    config["processing"] = processing_settings
    # Enforce finite JSON even for callers that supply an in-memory object.
    _json(config)
    selection_values = _object(config["selection"], "指标选择")
    if set(selection_values) != set(asdict(SelectionSpec())):
        raise ValueError("指标选择项目不完整。")
    for module in MODULES:
        ids = selection_values[f"{module}_metric_ids"]
        if not isinstance(ids, (list, tuple)):
            raise ValueError("指标列表应为列表，不能是单段文字。")
    selection = SelectionSpec(**selection_values)
    experimental = _object(config["experimental"], "研究方法开关")
    if set(experimental) != set(MODULES) or any(type(value) is not bool for value in experimental.values()):
        raise ValueError("研究方法开关只能为开或关。")
    for module in MODULES:
        catalogue = {item["metric_id"]: item for item in catalogue_for_module(schema, module)}
        for metric_id in getattr(selection, f"{module}_metric_ids"):
            item = catalogue.get(metric_id)
            if item is None or item.get("implemented") is False:
                raise ValueError(f"当前软件没有可用的指标：{metric_id}。未应用方案。")
            if item.get("experimental") and not experimental[module]:
                raise ValueError(f"指标 {metric_id} 需要显式启用研究方法。")

    pages: list[Any] = []
    try:
        quality = QualityPage(); pages.append(quality)
        quality.set_processing_config(_object(config["processing"], "预处理"))
        # A missing field would otherwise silently acquire a different default.
        if _json(quality.processing_config()) != _json(config["processing"]):
            raise ValueError("预处理设置不完整或不能原样恢复。")
        metrics = MetricsPage(schema=schema); pages.append(metrics)
        metrics.set_parameter_values(_object(config["metrics"], "方法参数"), emit=False, strict=True)
        metrics._allow_experimental = dict(experimental)
        if _json(metrics.all_parameter_values()) != _json(config["metrics"]):
            raise ValueError("方法参数不完整或不能原样恢复。")
        # Group settings contain only method fields, never member/clock evidence.
        group_values = _object(config["group"], "群体方法参数")
        definitions = group_parameter_fields(selection.group_metric_ids)
        form = TypedParameterForm(definitions); pages.append(form)
        known = form.editors
        flattened: dict[str, Any] = {}

        def collect(part: Mapping[str, Any], prefix: str = "") -> None:
            for key, value in part.items():
                name = f"{prefix}.{key}" if prefix else key
                if name in known:
                    flattened[name] = known[name].normalize_serialized_value(value)
                elif isinstance(value, Mapping) and any(field.startswith(name + ".") for field in known):
                    collect(value, name)
                else:
                    raise ValueError(f"未知的群体方案参数：{name}。")

        collect(group_values)
        if set(flattened) != set(known):
            raise ValueError("所选群体方法的参数不完整。")
        form.set_values(flattened, strict=True)
        group = GroupDesignPage(); pages.append(group)
        group.set_group_metric_ids(selection.group_metric_ids)
        group.set_parameter_values(form.values())
        design = DesignPage(schema=schema); pages.append(design)
        design_values = _object(config["statistics_design"], "统计设计")
        options = _object(design_values.get("options"), "统计设计选项")
        if set(options) - set(_OPTION_SPECS):
            raise ValueError("统计设计包含当前软件不支持的选项。")
        design.set_design_spec(design_values)
        if _json(design.configuration_values()) != _json(config["statistics_design"]):
            raise ValueError("统计设计含有未知字段或不能原样恢复的值。")
        config = deepcopy(config)
        config["processing"] = quality.processing_config()
        config["group"] = group.parameter_values()
        return config
    finally:
        for page in pages:
            page.deleteLater()


def plan_summary(payload: Mapping[str, Any]) -> str:
    config = payload["configuration"]
    counts = [f"{label} {len(config['selection'][module + '_metric_ids'])} 项"
              for module, label in zip(MODULES, ("个体", "多人", "高级"))]
    return (f"方案：{payload['name']}\n" + "；".join(counts) +
            "\n将替换：预处理、指标和方法参数、研究方法开关、统计设计。"
            "\n原始数据、历史结果、多人范围及同步依据保持当前设置，请按本次数据重新核对。"
            "\n应用后先点“检查可用条件”，不会自动运行分析。")
