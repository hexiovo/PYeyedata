"""Source-checked bindings from GUI method fields to public backend consumers.

The backend bridge accepts typed values under ``AnalysisConfig.individual`` and
``AnalysisConfig.group`` and named DataFrames under ``AnalysisConfig.context``.
This registry documents those exact paths; it is not a second availability
engine and never overrides the backend's conditions.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


def _field(
    name: str,
    type_name: str,
    default: Any,
    description: str,
    *,
    unit: str | None = None,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    minimum_exclusive: float | int | None = None,
    maximum_exclusive: float | int | None = None,
    choices: tuple[Any, ...] = (),
    label: str | None = None,
    config_path: str | None = None,
    consumer: str,
    methods: tuple[str, ...],
    availability_relation: str,
    editable: bool = True,
) -> dict[str, Any]:
    """Build a typed field plus its actual backend binding."""
    record: dict[str, Any] = {
        "name": name,
        "type": type_name,
        "default": default,
        "description": description,
        "label": label or name,
        "module": "individual" if (config_path or f"individual.{name}").startswith("individual.") else "group",
        "config_path": config_path or f"individual.{name}",
        "consumer": consumer,
        "methods": list(methods),
        "availability_relation": availability_relation,
        "editable": editable,
    }
    if unit is not None:
        record["unit"] = unit
    if minimum is not None:
        record["minimum"] = minimum
    if maximum is not None:
        record["maximum"] = maximum
    if minimum_exclusive is not None:
        record["minimum_exclusive"] = minimum_exclusive
    if maximum_exclusive is not None:
        record["maximum_exclusive"] = maximum_exclusive
    if choices:
        record["choices"] = list(choices)
    return record


_CALIBRATION = "individual.calibration.accuracy"
_STABILITY = "individual.fixation.stability"
_MEAN_VELOCITY = "individual.saccade.mean_velocity"
_GAIN = "individual.saccade.gain"
_ENDPOINT = "individual.saccade.endpoint_error"
_REGRESSION = "individual.saccade.regression_rate"
_HEATMAP = "individual.heatmap"
_MM_CHILDREN = tuple(f"individual.scanpath.multimatch.{name}" for name in
                     ("shape", "direction", "length", "position", "duration"))
_SM_CHILDREN = ("individual.scanpath.scanmatch.raw_score",
                "individual.scanpath.scanmatch.normalized_similarity")


_INDIVIDUAL_FIELDS: tuple[dict[str, Any], ...] = (
    _field("calibration_window_s", "tuple[float, float] | None", None,
           "校准分析的半开时间窗；须为两个递增秒数。", unit="s",
           config_path="individual.calibration_window_s", consumer="individual._required_metadata / _append_calibration_metric",
           methods=(_CALIBRATION,), availability_relation="calibration_target_confirmed + matching target unit/frame"),
    _field("stable_fixation_window_s", "tuple[float, float] | None", None,
           "稳定注视分析的半开时间窗；须为两个递增秒数。", unit="s",
           config_path="individual.stable_fixation_window_s", consumer="individual._required_metadata / analyze_individual BCEA branch",
           methods=(_STABILITY,), availability_relation="stable_target_confirmed + valid coordinate unit/frame"),
    _field("bcea_probability", "optional_float", None,
           "BCEA 等值概率；须处于 (0, 1)。留空为 None，后端会阻断，不会改用 0。", 
           minimum_exclusive=0.0, maximum_exclusive=1.0, label="BCEA 概率",
           config_path="individual.bcea_probability", consumer="individual._required_metadata / analyze_individual BCEA branch via _meta_value",
           methods=(_STABILITY,), availability_relation="0 < bcea_probability < 1"),
    _field("minimum_saccade_sample_rate_hz", "float", 250.0,
           "扫视速度要求的最低实测采样率。阈值只能保持或提高；设备与高速验证证据仍须独立提供。",
           unit="Hz", minimum=250.0, label="最低扫视采样率",
           config_path="individual.minimum_saccade_sample_rate_hz", consumer="individual._required_metadata high_rate_validation gate",
           methods=(_MEAN_VELOCITY,), availability_relation="device_verified + high_rate_validation source evidence", editable=True),
    _field("reading_direction", "Literal[None, 'ltr', 'rtl']", None,
           "阅读方向；需与显式阅读范围及逐事件行对应一起确认。", label="阅读方向",
           config_path="individual.reading_direction", consumer="individual._required_metadata / analyze_individual regression branch",
           methods=(_REGRESSION,), availability_relation="reading_scope_confirmed + reading_scope_id + reliable saccade events"),
    _field("reading_scope_id", "optional_str", None,
           "明确的阅读文本/行范围 ID；不得从文件名推断。", label="阅读范围 ID",
           config_path="individual.reading_scope_id", consumer="individual._required_metadata / analyze_individual regression branch",
           methods=(_REGRESSION,), availability_relation="reading_scope_confirmed + event line assignments"),
    _field("reading_min_amplitude", "positive_float", None,
           "纳入回视率的最小扫视幅度；单位与已确认的坐标单位一致。", unit="coordinate_unit",
           minimum_exclusive=0.0, label="最小扫视幅度",
           config_path="individual.reading_min_amplitude", consumer="individual._required_metadata / analyze_individual regression branch",
           methods=(_REGRESSION,), availability_relation="reading_scope_confirmed + reading direction + coordinate semantics"),
    _field("heatmap_grid", "dict[str, Any] | None", None,
           "网格定义 JSON：xmin、xmax、ymin、ymax、columns、rows；边界递增且行列为正整数。", 
           unit="coordinate_unit", label="热图网格",
           config_path="individual.heatmap_grid", consumer="individual._required_metadata / _heatmap_rows",
           methods=(_HEATMAP,), availability_relation="finite increasing bounds + positive integer grid dimensions"),
    _field("screen_size_px", "tuple[float, float] | None", None,
           "显示屏像素宽高；MultiMatch/ScanMatch 均使用该值并要求坐标单位为 px。", unit="px",
           label="屏幕像素尺寸", config_path="individual.screen_size_px",
           consumer="individual._required_metadata + multimatch_dimensions / scanmatch_cell_tokens",
           methods=(*_MM_CHILDREN, *_SM_CHILDREN), availability_relation="positive width/height + matching px/frame metadata"),
    _field("multimatch.grouping", "boolean", False,
           "是否启用 MultiMatch 注视分组。False 是明确关闭值，会按 bool 保留。", label="MultiMatch 注视分组",
           config_path="individual.multimatch.grouping", consumer="individual.analyze_individual → multimatch_dimensions",
           methods=_MM_CHILDREN, availability_relation="valid reference_fixations + reliable fixation events"),
    _field("multimatch.TDir", "optional_float", None, "MultiMatch 方向阈值；None 交由库默认处理。", unit="degree",
           minimum=0.0, label="MultiMatch TDir", config_path="individual.multimatch.TDir",
           consumer="individual.analyze_individual → multimatch_dimensions", methods=_MM_CHILDREN,
           availability_relation="valid reference_fixations + reliable fixation events"),
    _field("multimatch.TDur", "optional_float", None, "MultiMatch 时长阈值；None 交由库默认处理。", unit="s",
           minimum=0.0, label="MultiMatch TDur", config_path="individual.multimatch.TDur",
           consumer="individual.analyze_individual → multimatch_dimensions", methods=_MM_CHILDREN,
           availability_relation="valid reference_fixations + reliable fixation events"),
    _field("multimatch.TAmp", "optional_float", None, "MultiMatch 幅度阈值；None 交由库默认处理。", unit="px",
           minimum=0.0, label="MultiMatch TAmp", config_path="individual.multimatch.TAmp",
           consumer="individual.analyze_individual → multimatch_dimensions", methods=_MM_CHILDREN,
           availability_relation="valid reference_fixations + reliable fixation events"),
    _field("scanmatch.grid_cols", "optional_int", None, "ScanMatch 网格列数；须为正整数。", minimum=1,
           label="ScanMatch 网格列数", config_path="individual.scanmatch.grid_cols",
           consumer="individual._required_metadata / scanmatch_cell_tokens", methods=_SM_CHILDREN,
           availability_relation="positive grid dimensions + valid reference_trajectory"),
    _field("scanmatch.grid_rows", "optional_int", None, "ScanMatch 网格行数；须为正整数。", minimum=1,
           label="ScanMatch 网格行数", config_path="individual.scanmatch.grid_rows",
           consumer="individual._required_metadata / scanmatch_cell_tokens", methods=_SM_CHILDREN,
           availability_relation="positive grid dimensions + valid reference_trajectory"),
    _field("scanmatch.time_bin_s", "optional_float", None, "ScanMatch 时间箱宽度；须为正数。None 保持缺失。", unit="s",
           minimum_exclusive=0.0, label="ScanMatch 时间箱", config_path="individual.scanmatch.time_bin_s",
           consumer="individual._required_metadata / scanmatch_cell_tokens", methods=_SM_CHILDREN,
           availability_relation="positive time bin + overlapping source/reference time support"),
    _field("scanmatch.substitution_matrix", "dict[str, float] | None", None,
           "ScanMatch 对称字符替代矩阵 JSON；必须非空且有限。", label="ScanMatch 替代矩阵",
           config_path="individual.scanmatch.substitution_matrix", consumer="individual._symmetric_substitution_matrix / needleman_wunsch_score",
           methods=_SM_CHILDREN, availability_relation="nonempty finite symmetric matrix"),
    _field("scanmatch.gap_penalty", "optional_float", None, "ScanMatch 间隙罚分；须小于或等于 0。", maximum=0.0,
           label="ScanMatch 间隙罚分", config_path="individual.scanmatch.gap_penalty",
           consumer="individual._required_metadata / needleman_wunsch_score", methods=_SM_CHILDREN,
           availability_relation="finite gap penalty <= 0"),
)


_TARGET_CONTEXT = {
    "key": "target_movement_intervals",
    "type": "pandas.DataFrame",
    "consumer": "individual._required_metadata / _target_movement_for_event",
    "required_columns": ["target_movement_id", "target_id", "start_s", "end_s", "target_start_x", "target_start_y", "target_end_x", "target_end_y", "unit", "coordinate_frame"],
    "methods": [_GAIN, _ENDPOINT],
    "availability_relation": "confirmed target reference with matching unit/frame and source SHA-256; event target IDs must join exactly",
}
_REFERENCE_FIXATIONS = {
    "key": "reference_fixations",
    "type": "pandas.DataFrame",
    "consumer": "individual.analyze_individual → multimatch_dimensions",
    "required_columns": ["reference_id", "fixation_index", "start_x", "start_y", "duration_s"],
    "methods": list(_MM_CHILDREN),
    "availability_relation": "stable reference ID/hash, matching px/frame, installed multimatch-gaze, and reliable fixation events",
}
_REFERENCE_TRAJECTORY = {
    "key": "reference_trajectory",
    "type": "pandas.DataFrame",
    "consumer": "individual.analyze_individual → scanmatch_cell_tokens / needleman_wunsch_score",
    "required_columns": ["reference_id", "time_s", "gaze_x", "gaze_y"],
    "methods": list(_SM_CHILDREN),
    "availability_relation": "stable reference ID/hash, matching px/frame, valid optional boolean reference_valid, and common time support",
}


_INDIVIDUAL_METHODS: dict[str, dict[str, Any]] = {
    _CALIBRATION: {"implemented": True, "validated": True,
                   "evidence_ref": "a02-boundary-recheck-20261013.json#calibration_accuracy",
                   "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "target_id", "target_x", "target_y", "time_s"],
                   "required_metadata": ["coordinate_unit", "coordinate_frame", "calibration_target_confirmed"],
                   "parameters": ["calibration_window_s"], "context": [],
                   "consumer": "individual._append_calibration_metric"},
    _STABILITY: {"implemented": True, "validated": False, "evidence_ref": None,
                 "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "time_s"],
                 "required_metadata": ["coordinate_unit", "coordinate_frame", "stable_fixation_window", "bcea_probability"],
                 "parameters": ["stable_fixation_window_s", "bcea_probability"], "context": [],
                 "consumer": "individual.analyze_individual BCEA branch"},
    _MEAN_VELOCITY: {"implemented": True, "validated": False, "evidence_ref": None,
                     "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "time_s"],
                     "required_metadata": ["angle_geometry", "reliable_saccade_events", "high_rate_validation"],
                     "parameters": ["minimum_saccade_sample_rate_hz"], "context": [],
                     "consumer": "individual.analyze_individual mean-velocity event branch"},
    _GAIN: {"implemented": True, "validated": True,
            "evidence_ref": "a02-boundary-recheck-20261013.json#saccade_gain_and_endpoint_error",
            "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "time_s"],
            "required_metadata": ["target_displacement", "coordinate_unit", "coordinate_frame", "reliable_saccade_events"],
            "parameters": [], "context": ["target_movement_intervals"],
            "consumer": "individual._target_movement_for_event + _event_gaze_endpoints"},
    _ENDPOINT: {"implemented": True, "validated": True,
                "evidence_ref": "a02-boundary-recheck-20261013.json#saccade_gain_and_endpoint_error",
                "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "time_s"],
                "required_metadata": ["target_position", "target_reference", "coordinate_unit", "coordinate_frame", "reliable_saccade_events"],
                "parameters": [], "context": ["target_movement_intervals"],
                "consumer": "individual._target_movement_for_event + _event_gaze_endpoints"},
    _REGRESSION: {"implemented": True, "validated": True,
                  "evidence_ref": "a02-boundary-recheck-20261013.json#rtl_regression_rate",
                  "required_fields": ["time_s"],
                  "required_metadata": ["reading_direction", "reading_scope", "reliable_saccade_events", "coordinate_unit", "coordinate_frame"],
                  "parameters": ["reading_direction", "reading_scope_id", "reading_min_amplitude"], "context": [],
                  "consumer": "individual.analyze_individual within-line regression branch"},
    _HEATMAP: {"implemented": True, "validated": True,
               "evidence_ref": "a02-boundary-recheck-20261013.json#heatmap_boundaries",
               "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "time_s"],
               "required_metadata": ["coordinate_unit", "coordinate_frame", "heatmap_grid", "time_confirmed"],
               "parameters": ["heatmap_grid"], "context": [],
               "consumer": "individual._heatmap_rows"},
}

for metric_id in _MM_CHILDREN:
    _INDIVIDUAL_METHODS[metric_id] = {
        "implemented": True, "validated": True,
        "evidence_ref": "a02-boundary-recheck-20261013.json#multimatch_children",
        "required_fields": [],
        "required_metadata": ["multimatch_reference", "reliable_fixation_events", "coordinate_unit", "coordinate_frame"],
        "parameters": ["screen_size_px", "multimatch.grouping", "multimatch.TDir", "multimatch.TDur", "multimatch.TAmp"],
        "context": ["reference_fixations"],
        "consumer": "individual.analyze_individual → multimatch_dimensions",
    }
for metric_id in _SM_CHILDREN:
    _INDIVIDUAL_METHODS[metric_id] = {
        "implemented": True, "validated": True,
        "evidence_ref": "a02-boundary-recheck-20261013.json#scanmatch_boundaries",
        "required_fields": ["gaze_x", "gaze_y", "gaze_valid", "time_s"],
        "required_metadata": ["scanmatch_grid", "scanmatch_reference", "coordinate_unit", "coordinate_frame"],
        "parameters": ["screen_size_px", "scanmatch.grid_cols", "scanmatch.grid_rows", "scanmatch.time_bin_s",
                       "scanmatch.substitution_matrix", "scanmatch.gap_penalty"],
        "context": ["reference_trajectory"],
        "consumer": "individual.analyze_individual → scanmatch_cell_tokens / needleman_wunsch_score",
    }

from ..common_metrics import metric_definitions as _common_definitions, AOI_IDS as _COMMON_AOI_IDS, READING_PREFIX

_COMMON_DEFINITIONS = _common_definitions()
_COMMON_IDS = tuple(item["metric_id"] for item in _COMMON_DEFINITIONS)
_READING_IDS = tuple(mid for mid in _COMMON_IDS if mid.startswith(READING_PREFIX))
_READING_CONTEXT = {
    "key": "reading_annotations", "type": "pandas.DataFrame",
    "consumer": "common_metrics._reading_table / reading_measures",
    "required_columns": ["reading_scope_id", "stimulus_id", "aoi_id", "reading_order"],
    "methods": list(_READING_IDS),
    "availability_relation": "explicit reading order/direction confirmation; unique positive order in each stimulus/scope",
}
for _definition in _COMMON_DEFINITIONS:
    _mid = _definition["metric_id"]
    _parameters = ["event_source_confirmed", "event_source", "event_algorithm_version", "event_source_types"]
    if ".blink." in _mid:
        _parameters.append("blink_source_confirmed")
    if _mid in _COMMON_AOI_IDS:
        _parameters.append("fixation_observation_confirmed")
    if _mid.endswith((".fixation_ttff", ".fixations_before_first")):
        _parameters.append("reference_event_s")
    if _mid.startswith(READING_PREFIX):
        _parameters.extend(["reading_direction", "reading_scope_id", "reading_order_confirmed"])
    _INDIVIDUAL_METHODS[_mid] = {
        "implemented": True, "validated": True, "evidence_ref": _definition["evidence_ref"],
        "required_fields": list(_definition["fields"]), "required_metadata": list(_definition["metadata"]),
        "parameters": _parameters,
        "context": ["reading_annotations"] if _mid.startswith(READING_PREFIX) else [],
        "consumer": "common_metrics.append_distributions / append_aoi_fixations / append_hit_rates",
    }
for _definition in _INDIVIDUAL_FIELDS:
    if _definition["name"] in {"reading_direction", "reading_scope_id"}:
        _definition["methods"].extend(_READING_IDS)
_INDIVIDUAL_FIELDS += (
    _field("reference_event_s", "optional_float", None,
           "AOI 首次注视的参考起点（秒），需位于当前分析窗内；不得将未命中置为 0。", unit="s",
           label="首次注视参考时刻", consumer="common_metrics.append_aoi_fixations",
           methods=tuple(mid for mid in _COMMON_IDS if mid.endswith((".fixation_ttff", ".fixations_before_first"))),
           availability_relation="finite reference inside current window"),
    _field("fixation_observation_confirmed", "boolean", False,
           "明确确认：已提供的注视分类覆盖所有已观察试次/时间窗；仅提供选中的注视事件时不要勾选。数据缺口仍保留未知状态。",
           label="注视分类覆盖观察窗已确认", consumer="common_metrics.append_aoi_fixations",
           methods=tuple(_COMMON_AOI_IDS), availability_relation="required for known misses and complete reading observations"),
    _field("reading_order_confirmed", "boolean", False,
           "确认阅读标注的 reading_order 按阅读推进递增；ltr/rtl 均以显式顺序为准，不从坐标猜测。",
           label="AOI 阅读顺序已确认", consumer="common_metrics._reading_table",
           methods=_READING_IDS, availability_relation="reading_annotations + direction/scope + unique positive order"),
    _field("event_source_confirmed", "boolean", False,
           "仅对明确外部事件来源进行确认；本地合格事件沿用自己的方法证据。",
           label="外部事件来源已确认", consumer="individual._event_consumer_rows / common_metrics.precondition",
           methods=_COMMON_IDS, availability_relation="legacy/external source declaration; never certifies hardware"),
    _field("event_source", "optional_str", None, "外部事件的实际设备/软件来源名称。",
           label="外部事件来源", consumer="individual._event_consumer_rows", methods=_COMMON_IDS,
           availability_relation="nonempty source name for legacy declarations"),
    _field("event_algorithm_version", "optional_str", None, "外部事件分类方法/软件版本。",
           label="外部事件分类版本", consumer="individual._event_consumer_rows", methods=_COMMON_IDS,
           availability_relation="nonempty source version for legacy declarations"),
    _field("event_source_types", "list[str] | None", None,
           '来源明确覆盖的事件类型 JSON，例如 ["fixation", "saccade", "blink"]；不自动从缺少事件推断覆盖。',
           label="来源覆盖事件类型", consumer="common_metrics.precondition", methods=_COMMON_IDS,
           availability_relation="explicit declaration allows empty classified event sets"),
    _field("blink_source_confirmed", "boolean", False, "确认眨眼事件有可靠来源；丢失采样不作为眨眼。",
           label="眨眼事件来源已确认", consumer="individual._event_consumer_rows",
           methods=tuple(mid for mid in _COMMON_IDS if ".blink." in mid),
           availability_relation="reliable vendor blink evidence"),
)

# Only additive, non-scientific defaults may be filled when opening an old v1 plan.
COMMON_PLAN_DEFAULTS = {field["name"]: deepcopy(field["default"]) for field in _INDIVIDUAL_FIELDS
                        if field["name"] in {"reference_event_s", "fixation_observation_confirmed",
                            "reading_order_confirmed", "event_source_confirmed", "event_source",
                            "event_algorithm_version", "event_source_types", "blink_source_confirmed"}}

_INDIVIDUAL_FIELDS_BY_NAME = {field["name"]: field for field in _INDIVIDUAL_FIELDS}
_INDIVIDUAL_FAMILY_SELECTORS = {
    "individual.scanpath.multimatch": {
        "selection_only": True, "children": list(_MM_CHILDREN),
        "selection_behavior": "批量选择五个独立child；不生成family总分。",
    },
    "individual.scanpath.scanmatch": {
        "selection_only": True, "children": list(_SM_CHILDREN),
        "selection_behavior": "批量选择raw_score与normalized_similarity；两者保留各自单位和状态。",
    },
}


def individual_method_registry() -> dict[str, dict[str, Any]]:
    """Return leaf bindings whose implementation state comes from the public A catalogue."""
    from ..individual import get_individual_catalogue

    methods = deepcopy(_INDIVIDUAL_METHODS)
    public_catalogue = {item["metric_id"]: item for item in get_individual_catalogue()}
    for metric_id, record in methods.items():
        public_entry = public_catalogue.get(metric_id)
        if public_entry is None:
            raise RuntimeError(f"Registered individual leaf is missing from the public catalogue: {metric_id}")
        record["implemented"] = public_entry.get("implemented") is True
        record["validated"] = public_entry.get("validated") is True
        record["validation_status"] = public_entry.get("validation_status")
        record["experimental"] = public_entry.get("experimental") is True
        record["tier"] = public_entry.get("tier")
        record["unit"] = public_entry.get("unit")
        record["device_requirement"] = public_entry.get("device_requirement")
        record["evidence_ref"] = public_entry.get("evidence_ref")
        record["note"] = public_entry.get("note", "")
        record["metric_id"] = metric_id
        record["module"] = "individual"
        record["parameter_definitions"] = [
            deepcopy(_INDIVIDUAL_FIELDS_BY_NAME[name]) for name in record["parameters"]
        ]
        record["named_context"] = [
            deepcopy(context) for context in (_TARGET_CONTEXT, _REFERENCE_FIXATIONS, _REFERENCE_TRAJECTORY, _READING_CONTEXT)
            if context["key"] in record["context"]
        ]
    return methods


def individual_parameter_fields(metric_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Return unique typed definitions used by selected leaves/family selectors, in UI order."""
    methods = individual_method_registry()
    selectors = family_selector_registry()
    if metric_ids is None:
        selected = set(methods)
    else:
        selected: set[str] = set()
        for metric_id in metric_ids:
            if metric_id in methods:
                selected.add(metric_id)
            elif metric_id in selectors:
                selected.update(child for child in selectors[metric_id]["children"] if child in methods)
    used = {name for metric_id in selected for name in methods[metric_id]["parameters"]}
    return [deepcopy(field) for field in _INDIVIDUAL_FIELDS if field["name"] in used]


# These are the existing GroupDesignPage fields moved into the common registry.
# Defaults and types intentionally match the existing form; no backend default
# or gate is inferred from the UI definition.
GROUP_PARAMETER_FIELD_GROUPS: dict[str, list[dict[str, Any]]] = {
    "lag": [
        {"name": "alignment_tolerance_s", "type": "nonnegative_float", "default": 0.0, "unit": "s", "label": "时间对齐容差", "config_path": "group.alignment_tolerance_s", "consumer": "group._align / group._lag_result", "methods": ["group.*"], "availability_relation": "nearest-neighbor time matching; 0 requires equal timestamps", "description": "成员采样时点的最近邻匹配容差；0 表示只匹配相同时间。"},
        {"name": "max_gap_s", "type": "optional_float", "default": None, "unit": "s", "minimum_exclusive": 0.0, "label": "最大连续间隔", "config_path": "group.max_gap_s", "consumer": "individual._gap_limit_s reused by group alignment", "methods": ["group.*"], "availability_relation": "None retains backend support-gap rule", "description": "可选的最大连续支持间隔；留空沿用后端规则。"},
        {"name": "co_gaze_threshold", "type": "optional_float", "default": None, "unit": "coordinate_unit", "minimum_exclusive": 0.0, "label": "共同注视距离阈值", "config_path": "group.co_gaze_threshold", "consumer": "group._available / co-gaze calculation", "methods": ["group.co_gaze_proportion"], "availability_relation": "finite positive threshold in confirmed comparable coordinates", "description": "共同注视的显式距离阈值；只有正数才满足该指标条件。"},
        {"name": "pupil_baseline_window_s", "type": "tuple[float, float] | None", "default": None, "unit": "s", "label": "瞳孔基线窗口", "config_path": "group.pupil_baseline_window_s", "consumer": "group._available / group pupil synchrony branch", "methods": ["group.pupil_synchrony"], "availability_relation": "two finite increasing bounds", "description": "瞳孔同步基线窗口 [起点, 终点]；必须递增。"},
        {"name": "event_match_tolerance_s", "type": "optional_float", "default": None, "unit": "s", "minimum": 0.0, "label": "事件匹配容差", "config_path": "group.event_match_tolerance_s", "consumer": "group._available / _event_match_rate", "methods": ["group.fixation_cooccurrence", "group.blink_synchrony"], "availability_relation": "finite tolerance >= 0 and reliable events", "description": "可靠事件一对一 onset 匹配容差。"},
        {"name": "lag_signal", "type": "Literal['pupil_left', 'pupil_right', 'gaze_x', 'gaze_y']", "default": "pupil_left", "label": "lag 比较信号", "choices": ["pupil_left", "pupil_right", "gaze_x", "gaze_y"], "config_path": "group.lag_signal", "consumer": "group._lag_result", "methods": ["group.lagged_correlation", "group.best_lag", "group.role_following.max_correlation", "group.role_following.best_lag"], "availability_relation": "selected signal fields, confirmed clocks, and applicable signal comparability", "description": "延迟相关使用的实际信号；空间信号仍需空间可比证据。"},
        {"name": "lag_max_s", "type": "positive_float", "default": 2.0, "unit": "s", "minimum_exclusive": 0.0, "label": "lag 最大绝对值", "config_path": "group.lag_max_s", "consumer": "group._available / group._lag_result", "methods": ["group.lagged_correlation", "group.best_lag", "group.role_following.max_correlation", "group.role_following.best_lag"], "availability_relation": "finite positive search bound", "description": "lag 搜索范围为 [-lag_max_s, +lag_max_s]；后端默认值为 2 秒。"},
        {"name": "lag_step_s", "type": "optional_float", "default": None, "unit": "s", "minimum_exclusive": 0.0, "label": "lag 步长", "config_path": "group.lag_step_s", "consumer": "group._lag_result", "methods": ["group.lagged_correlation", "group.best_lag", "group.role_following.max_correlation", "group.role_following.best_lag"], "availability_relation": "None uses observed median sample interval; explicit step respects time precision", "description": "留空时按实际采样间隔中位数确定；显式步长不能低于后端时间分辨率。"},
        {"name": "lag_window_s", "type": "optional_float", "default": None, "unit": "s", "minimum_exclusive": 0.0, "label": "共同窗口长度", "config_path": "group.lag_window_s", "consumer": "group._lag_result", "methods": ["group.lagged_correlation", "group.best_lag", "group.role_following.max_correlation", "group.role_following.best_lag"], "availability_relation": "None uses the actual common time span", "description": "共同分析窗口长度；留空时使用双方共同时间范围。"},
        {"name": "lag_min_window_s", "type": "positive_float", "default": 10.0, "unit": "s", "minimum_exclusive": 0.0, "label": "最短共同支持", "config_path": "group.lag_min_window_s", "consumer": "group._lag_result", "methods": ["group.lagged_correlation", "group.best_lag", "group.role_following.max_correlation", "group.role_following.best_lag"], "availability_relation": "common supported duration >= configured minimum", "description": "后端要求的最短有效共同支持长度。"},
        {"name": "lag_min_coverage", "type": "float", "default": 0.8, "minimum": 0.0, "maximum": 1.0, "label": "最低共同覆盖率", "config_path": "group.lag_min_coverage", "consumer": "group._lag_result", "methods": ["group.lagged_correlation", "group.best_lag", "group.role_following.max_correlation", "group.role_following.best_lag"], "availability_relation": "finite proportion in [0,1]", "description": "所有候选 lag 共用支持范围的最低覆盖率。"},
    ],
    "consensus": [
        {"name": "window_length_s", "type": "optional_float", "default": None, "unit": "s", "minimum_exclusive": 0.0, "label": "分窗长度", "config_path": "group.window_length_s", "consumer": "group.windowed_synchrony branch", "methods": ["group.windowed_synchrony"], "availability_relation": "finite positive length with finite positive step", "description": "分窗同步窗口长度；只在选择该方法时填写。"},
        {"name": "window_step_s", "type": "optional_float", "default": None, "unit": "s", "minimum_exclusive": 0.0, "label": "分窗步长", "config_path": "group.window_step_s", "consumer": "group.windowed_synchrony branch", "methods": ["group.windowed_synchrony"], "availability_relation": "finite positive step with finite positive length", "description": "分窗同步步长；只在选择该方法时填写。"},
        {"name": "majority_fraction", "type": "optional_float", "default": None, "minimum_exclusive": 0.0, "maximum": 1.0, "label": "多数比例", "config_path": "group.majority_fraction", "consumer": "group.majority_aoi_agreement branch", "methods": ["group.majority_aoi_agreement"], "availability_relation": "finite fraction in (0,1]", "description": "多数共识比例，范围 (0, 1]。"},
        {"name": "majority_min_members", "type": "optional_int", "default": None, "minimum": 2, "label": "最少多数成员数", "config_path": "group.majority_min_members", "consumer": "group.majority_aoi_agreement branch", "methods": ["group.majority_aoi_agreement"], "availability_relation": "integer >= 2", "description": "每个时点计算多数共识所需的最少成员数。"},
        {"name": "network_threshold", "type": "optional_float", "default": None, "label": "网络边阈值", "config_path": "group.network_threshold", "consumer": "group.network edge/strength branches", "methods": ["group.network.edge", "group.network.strength"], "availability_relation": "finite threshold in the selected signal scale", "description": "网络边阈值；其含义随选定的信号量纲而变。"},
        {"name": "network_signal", "type": "Literal[None, 'gaze_x', 'gaze_y']", "default": None, "label": "网络信号轴", "choices": [None, "gaze_x", "gaze_y"], "config_path": "group.network_signal", "consumer": "group.network edge/strength branches", "methods": ["group.network.edge", "group.network.strength"], "availability_relation": "explicit gaze axis and confirmed spatial comparability", "description": "相似网络的单一注视轴。"},
    ],
    "signal": [
        {"name": "comparison_signal", "type": "Literal[None, 'gaze_xy', 'gaze_x', 'gaze_y', 'pupil_left', 'pupil_right']", "default": None, "label": "多变量比较信号", "choices": [None, "gaze_xy", "gaze_x", "gaze_y", "pupil_left", "pupil_right"], "config_path": "group.comparison_signal", "consumer": "group._available / DTW and CRQA branches", "methods": ["group.dtw_distance", "group.crqa.*"], "availability_relation": "gaze requires spatial comparability; pupil requires pupil-unit and preprocessing comparability", "description": "显式选择 DTW/CRQA 比较信号；信号类别分别要求空间或瞳孔可比声明。"},
        {"name": "dtw_window_fraction", "type": "optional_float", "default": None, "minimum": 0.0, "maximum": 1.0, "label": "DTW Sakoe–Chiba 带宽比例", "config_path": "group.dtw_window_fraction", "consumer": "group._available / _dtw", "methods": ["group.dtw_distance"], "availability_relation": "finite fraction in [0,1]", "description": "DTW Sakoe–Chiba 带宽占序列长度比例，范围 [0, 1]。"},
        {"name": "crqa.embedding_dimension", "type": "optional_int", "default": None, "minimum": 1, "label": "CRQA 嵌入维数", "config_path": "group.crqa.embedding_dimension", "consumer": "group._available / _crqa", "methods": ["group.crqa.*"], "availability_relation": "integer >= 1", "description": "CRQA 嵌入维数。"},
        {"name": "crqa.embedding_lag_samples", "type": "optional_int", "default": None, "minimum": 1, "label": "CRQA 嵌入时延（样本）", "config_path": "group.crqa.embedding_lag_samples", "consumer": "group._available / _crqa", "methods": ["group.crqa.*"], "availability_relation": "integer >= 1", "description": "CRQA 嵌入时延（样本数）。"},
        {"name": "crqa.threshold", "type": "optional_float", "default": None, "minimum_exclusive": 0.0, "label": "CRQA 复现阈值", "config_path": "group.crqa.threshold", "consumer": "group._available / _crqa", "methods": ["group.crqa.*"], "availability_relation": "finite threshold > 0 in selected signal units", "description": "CRQA 复现阈值，单位随比较信号。"},
        {"name": "crqa.min_diagonal_line", "type": "optional_int", "default": None, "minimum": 1, "label": "CRQA 最短对角线（样本）", "config_path": "group.crqa.min_diagonal_line", "consumer": "group._available / _crqa", "methods": ["group.crqa.*"], "availability_relation": "integer >= 1", "description": "CRQA 最短对角线长度（样本数）。"},
        {"name": "crqa.min_vertical_line", "type": "optional_int", "default": None, "minimum": 1, "label": "CRQA 最短垂直线（样本）", "config_path": "group.crqa.min_vertical_line", "consumer": "group._available / _crqa", "methods": ["group.crqa.*"], "availability_relation": "integer >= 1", "description": "CRQA 最短垂直线长度（样本数）。"},
        {"name": "crqa.theiler_window_samples", "type": "optional_int", "default": None, "minimum": 0, "label": "CRQA Theiler 窗口（样本）", "config_path": "group.crqa.theiler_window_samples", "consumer": "group._available / _crqa", "methods": ["group.crqa.*"], "availability_relation": "optional integer >= 0; None disables exclusion", "description": "可选 Theiler 排除窗口（样本数）。"},
    ],
    "sequence": [
        {"name": "screen_size_px", "type": "tuple[float, float] | None", "default": None, "unit": "px", "label": "屏幕像素尺寸", "config_path": "group.screen_size_px", "consumer": "group._available / multimatch_dimensions / scanmatch", "methods": ["group.multimatch.*", "group.scanmatch.score"], "availability_relation": "two positive dimensions in confirmed px coordinates", "description": "MultiMatch/ScanMatch 使用的显示屏像素宽高，均须为正数。"},
        {"name": "multimatch.grouping", "type": "boolean", "default": False, "label": "MultiMatch 合并相邻注视", "config_path": "group.multimatch.grouping", "consumer": "group.multimatch_dimensions", "methods": ["group.multimatch.*"], "availability_relation": "False preserves the backend option as disabled", "description": "是否启用 MultiMatch fixation grouping。"},
        {"name": "multimatch.TDir", "type": "optional_float", "default": None, "unit": "degree", "minimum": 0.0, "label": "MultiMatch TDir", "config_path": "group.multimatch.TDir", "consumer": "group.multimatch_dimensions", "methods": ["group.multimatch.*"], "availability_relation": "None preserves library default; explicit value >= 0", "description": "MultiMatch 方向维度阈值。"},
        {"name": "multimatch.TDur", "type": "optional_float", "default": None, "unit": "s", "minimum": 0.0, "label": "MultiMatch TDur", "config_path": "group.multimatch.TDur", "consumer": "group.multimatch_dimensions", "methods": ["group.multimatch.*"], "availability_relation": "None preserves library default; explicit value >= 0", "description": "MultiMatch 时长维度阈值。"},
        {"name": "multimatch.TAmp", "type": "optional_float", "default": None, "unit": "px", "minimum": 0.0, "label": "MultiMatch TAmp", "config_path": "group.multimatch.TAmp", "consumer": "group.multimatch_dimensions", "methods": ["group.multimatch.*"], "availability_relation": "None preserves library default; explicit value >= 0", "description": "MultiMatch 幅度维度阈值。"},
        {"name": "scanmatch.grid_cols", "type": "optional_int", "default": None, "minimum": 1, "label": "ScanMatch 网格列数", "config_path": "group.scanmatch.grid_cols", "consumer": "group._available / scanmatch branch", "methods": ["group.scanmatch.score"], "availability_relation": "positive integer grid size", "description": "ScanMatch 网格列数。"},
        {"name": "scanmatch.grid_rows", "type": "optional_int", "default": None, "minimum": 1, "label": "ScanMatch 网格行数", "config_path": "group.scanmatch.grid_rows", "consumer": "group._available / scanmatch branch", "methods": ["group.scanmatch.score"], "availability_relation": "positive integer grid size", "description": "ScanMatch 网格行数。"},
        {"name": "scanmatch.time_bin_s", "type": "optional_float", "default": None, "unit": "s", "minimum_exclusive": 0.0, "label": "ScanMatch 时间箱", "config_path": "group.scanmatch.time_bin_s", "consumer": "group._available / scanmatch_cell_tokens", "methods": ["group.scanmatch.score"], "availability_relation": "finite positive time bin", "description": "ScanMatch 时间箱宽度。"},
        {"name": "scanmatch.substitution_matrix", "type": "dict[str, float] | None", "default": None, "label": "ScanMatch 替代矩阵 JSON", "config_path": "group.scanmatch.substitution_matrix", "consumer": "group._available / needleman_wunsch_score", "methods": ["group.scanmatch.score"], "availability_relation": "nonempty finite symmetric substitution matrix", "description": "ScanMatch 对称字符替代矩阵；请以 JSON 对象填写。"},
        {"name": "scanmatch.gap_penalty", "type": "optional_float", "default": None, "maximum": 0.0, "label": "ScanMatch 间隙罚分", "config_path": "group.scanmatch.gap_penalty", "consumer": "group._available / needleman_wunsch_score", "methods": ["group.scanmatch.score"], "availability_relation": "finite gap penalty <= 0", "description": "ScanMatch 间隙罚分，必须小于或等于 0。"},
    ],
}


def group_parameter_field_groups() -> dict[str, list[dict[str, Any]]]:
    """Return existing GroupDesignPage typed fields with their source bindings."""
    return deepcopy(GROUP_PARAMETER_FIELD_GROUPS)


def group_parameter_fields(metric_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Return group fields relevant to the selected group IDs; ``None`` means all."""
    groups = group_parameter_field_groups()
    if metric_ids is None:
        return [field for records in groups.values() for field in records]
    selected = set(metric_ids)
    return [field for records in groups.values() for field in records
            if any(method in selected or method.endswith(".*") and
                   any(metric.startswith(method[:-1]) for metric in selected)
                   for method in field.get("methods", ()))]


def named_context_registry() -> dict[str, dict[str, Any]]:
    """Return exact public DataFrame context keys consumed by the B GUI methods."""
    return {
        record["key"]: deepcopy(record)
        for record in (_TARGET_CONTEXT, _REFERENCE_FIXATIONS, _REFERENCE_TRAJECTORY)
    }


def family_selector_registry() -> dict[str, dict[str, Any]]:
    """Return the two selection-only families from the public catalogue."""
    from ..individual import get_individual_catalogue

    return {
        metric_id: {
            "selection_only": entry.get("selection_only") is True,
            "children": list(entry.get("child_metric_ids", ())),
            "selection_behavior": str(entry.get("note", "")),
            "implemented": entry.get("implemented") is True,
            "validated": entry.get("validated") is True,
            "experimental": entry.get("experimental") is True,
            "evidence_ref": entry.get("evidence_ref"),
        }
        for entry in get_individual_catalogue()
        if (metric_id := entry.get("metric_id")) in _INDIVIDUAL_FAMILY_SELECTORS
    }


__all__ = [
    "GROUP_PARAMETER_FIELD_GROUPS",
    "family_selector_registry",
    "group_parameter_field_groups",
    "group_parameter_fields",
    "individual_method_registry",
    "individual_parameter_fields",
    "named_context_registry",
]
