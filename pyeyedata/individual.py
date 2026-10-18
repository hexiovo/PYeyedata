"""Fail-closed individual eye-tracking features and catalogue.

The public entry point consumes canonical signal rows. It never edits those rows;
validity and interpolation flags are read as supplied and are reflected in the
result provenance. Methods whose evidence or operational definitions are not
verified remain in the availability catalogue as blocked/unavailable.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import importlib.util
import json
import math
import re
from typing import Any, Callable

import numpy as np
import pandas as pd

from .aoi_geometry import (
    AOI_BOUNDARY_POLICY,
    AOI_DOMAIN_SCHEMA_VERSION,
    AOI_GEOMETRY_VERSION,
    AOIGeometryBudget,
    AOIGeometryError,
    AOIGeometryIssue,
    CanonicalGeometry,
    PreparedStaticAOIs,
    point_in_geometry,
    prepare_static_aois,
    validate_geometry,
)

from .contracts import (
    AOI_REQUIRED_COLUMNS,
    AVAILABILITY_COLUMNS,
    EVENT_COLUMNS,
    METRIC_COLUMNS,
    AnalysisBundle,
    ContractError,
    MetricAvailability,
    AvailabilityStatus,
)
from .processing import time_weighted_mean
from .event_queries import onset_events as query_event_onsets, overlapping_events as query_event_overlaps
from .events import EVENT_METHOD_VALIDATION_REF, EVENT_RESULT_COLUMNS
from .methods.scanpath import (
    MULTIMATCH_DIMENSIONS,
    fixation_vectors,
    multimatch_dimensions,
    needleman_wunsch_score,
    scanmatch_cell_tokens,
)

ALGORITHM_VERSION = "individual-0.2.0"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ID_COLUMNS = ("subject_id", "session_id")
_MULTIMATCH_FAMILY = "individual.scanpath.multimatch"
_MULTIMATCH_CHILDREN = tuple(f"{_MULTIMATCH_FAMILY}.{name}" for name in
                              ("shape", "direction", "length", "position", "duration"))
_SCANMATCH_FAMILY = "individual.scanpath.scanmatch"
_SCANMATCH_CHILDREN = (f"{_SCANMATCH_FAMILY}.raw_score",
                       f"{_SCANMATCH_FAMILY}.normalized_similarity")
_FAMILY_SELECTIONS = {
    _MULTIMATCH_FAMILY: _MULTIMATCH_CHILDREN,
    _SCANMATCH_FAMILY: _SCANMATCH_CHILDREN,
}
_EXTENDED_FIELDS = (
    "gaze_left_x", "gaze_left_y", "gaze_left_z", "gaze_right_x", "gaze_right_y", "gaze_right_z",
    "gaze_left_valid", "gaze_right_valid", "eyelid_closure_left", "eyelid_closure_right",
    "blink_left_event", "blink_right_event", "microsaccade_event", "tracking_state",
    "tracking_valid", "calibration_error", "target_vx", "target_vy", "target_vz",
    "target_azimuth", "target_elevation", "target_distance", "target_valid", "cue_id",
    "cue_start", "cue_end",
    "target_id", "target_x", "target_y", "target_movement_id", "line_id",
    "start_line_id", "end_line_id", "reading_scope_id", "event_role",
)


def _spec(metric_id: str, label: str, category: str, unit: str,
          fields: tuple[str, ...] = (), metadata: tuple[str, ...] = (),
          tier: str = "R1", algorithm: str = "descriptive-v1",
          refs: tuple[str, ...] = ("PRD-v0.9:7",), implemented: bool = False,
          note: str = "", validated: bool | None = None,
          validation_status: str | None = None,
          device_requirement: str = "not_required",
          evidence_ref: str | None = None,
          method_version: str | None = None) -> dict[str, Any]:
    validation_flag = bool(implemented) if validated is None else bool(validated)
    return {
        "metric_id": metric_id, "label": label, "category": category,
        "unit": unit, "algorithm_id": algorithm,
        "algorithm_version": method_version or ALGORITHM_VERSION, "requires_fields": fields,
        "requires_metadata": metadata, "analysis_modes": ("individual",),
        "reference_ids": refs, "validated": validation_flag,
        "validation_status": validation_status or ("validated" if validation_flag else "not_validated"),
        "device_requirement": device_requirement,
        "experimental": tier == "R2", "tier": tier, "implemented": implemented,
        "note": note,
        "evidence_ref": evidence_ref,
    }


# Every feature row in PRD chapter 7 has a stable catalogue entry. The A02
# boundary evidence validates only the listed software cases; device evidence
# remains a separate runtime gate and is not inferred from these flags.
INDIVIDUAL_CATALOGUE: tuple[dict[str, Any], ...] = (
    _spec("individual.coverage.gaze", "有效覆盖率（注视）", "quality", "proportion", ("time_s", "gaze_valid"), ("time_confirmed",), implemented=True),
    _spec("individual.coverage.pupil_left", "有效覆盖率（左瞳孔）", "quality", "proportion", ("time_s", "pupil_left", "pupil_left_valid"), ("time_confirmed",), implemented=True),
    _spec("individual.coverage.pupil_right", "有效覆盖率（右瞳孔）", "quality", "proportion", ("time_s", "pupil_right", "pupil_right_valid"), ("time_confirmed",), implemented=True),
    _spec("individual.data_loss.gaps", "丢失率与缺口", "quality", "s/count", ("time_s", "gaze_valid"), ("time_confirmed",), implemented=True),
    _spec("individual.sampling.regularity", "实测采样与抖动", "quality", "Hz/s", ("time_s",), ("time_confirmed",), implemented=True),
    _spec("individual.calibration.accuracy", "校准准确度", "quality", "coordinate_unit", ("gaze_x", "gaze_y", "gaze_valid", "target_id", "target_x", "target_y", "time_s"), ("coordinate_unit", "coordinate_frame", "calibration_target_confirmed"), "R2", "equal_target_mean_radial_error-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#calibration_accuracy" , note="等权汇总各目标内平均径向误差；需要逐样本目标映射、明确校准窗口、匹配的坐标单位与 frame。软件数值边界已定向验证；不代表设备校准认证。"),
    _spec("individual.signal.precision", "信号精密度", "quality", "coordinate_unit", ("gaze_x", "gaze_y", "gaze_valid"), ("coordinate_unit", "stable_target_confirmed"), implemented=True),
    _spec("individual.usable_duration", "有效分析时长", "quality", "s", ("time_s",), ("time_confirmed",), implemented=True),
    _spec("individual.fixation.count", "注视次数", "fixation", "count", (), ("reliable_events",), implemented=True),
    _spec("individual.fixation.rate", "注视频率", "fixation", "count/min", (), ("reliable_events", "time_confirmed"), implemented=True),
    _spec("individual.fixation.duration.mean", "注视时长均值", "fixation", "s", (), ("reliable_events",), implemented=True),
    _spec("individual.fixation.duration.median", "注视时长中位数", "fixation", "s", (), ("reliable_events",), implemented=True),
    _spec("individual.fixation.total_duration", "总注视时间", "fixation", "s", (), ("reliable_events",), implemented=True),
    _spec("individual.fixation.proportion", "注视时间比例", "fixation", "proportion", ("gaze_valid",), ("reliable_events", "time_confirmed"), implemented=True),
    _spec("individual.fixation.first_duration", "首个注视时长", "fixation", "s", (), ("reliable_events",), implemented=True),
    _spec("individual.fixation.last_duration", "末个注视时长", "fixation", "s", (), ("reliable_events",), implemented=True),
    _spec("individual.fixation.centroid_x", "注视位置中心 X", "fixation", "coordinate_unit", ("gaze_x", "gaze_valid"), ("coordinate_unit", "reliable_events"), implemented=True),
    _spec("individual.fixation.centroid_y", "注视位置中心 Y", "fixation", "coordinate_unit", ("gaze_y", "gaze_valid"), ("coordinate_unit", "reliable_events"), implemented=True),
    _spec("individual.fixation.dispersion_x", "注视离散度 X 范围", "fixation", "coordinate_unit", ("gaze_x", "gaze_valid"), ("coordinate_unit", "reliable_events"), implemented=True),
    _spec("individual.fixation.dispersion_y", "注视离散度 Y 范围", "fixation", "coordinate_unit", ("gaze_y", "gaze_valid"), ("coordinate_unit", "reliable_events"), implemented=True),
    _spec("individual.fixation.latency", "首个注视潜伏期", "fixation", "s", (), ("reliable_events", "reference_event_s"), implemented=True),
    _spec("individual.fixation.stability", "注视稳定性 BCEA", "fixation", "coordinate_unit²", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "stable_fixation_window", "bcea_probability"), "R2", "time_weighted_bcea-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="b02-registration-owner-check-20261014-v2.json#bcea_numeric_oracle", note="时间加权协方差与 BCEA 面积公式已用解析方形轨迹定向核对；窗口、稳定目标声明、坐标语义仍为必需条件。该软件数值检验不代表生理有效性。"),
    _spec("individual.saccade.count", "扫视次数", "saccade", "count", (), ("reliable_events",), implemented=True),
    _spec("individual.saccade.rate", "扫视频率", "saccade", "count/min", (), ("reliable_events", "time_confirmed"), implemented=True),
    _spec("individual.saccade.duration.mean", "扫视持续时间均值", "saccade", "s", (), ("reliable_events",), implemented=True),
    _spec("individual.saccade.amplitude.mean", "扫视振幅均值", "saccade", "coordinate_unit", (), ("coordinate_unit", "reliable_events"), implemented=True),
    _spec("individual.saccade.direction.mean_sin", "扫视方向平均正弦", "saccade", "unitless", (), ("reliable_events",), implemented=True),
    _spec("individual.saccade.direction.mean_cos", "扫视方向平均余弦", "saccade", "unitless", (), ("reliable_events",), implemented=True),
    _spec("individual.saccade.latency", "扫视潜伏期", "saccade", "s", (), ("reliable_events", "target_onset_s"), implemented=True),
    _spec("individual.saccade.mean_velocity", "平均扫视路径速度", "saccade", "deg/s", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("angle_geometry", "reliable_saccade_events", "high_rate_validation"), "R2", "event_angular_path_over_supported_duration-v1", implemented=True, validated=True, validation_status="targeted_software_validation", device_requirement="device_verified_and_high_rate", evidence_ref="b02-registration-owner-check-20261014-v2.json#mean_angular_velocity_numeric_oracle", note="角度几何路径计算已用解析针孔投影轨迹定向核对；运行仍要求真实角度几何、可靠扫视事件及设备/高速采样证据。软件数值检验不构成设备或生理验证。"),
    _spec("individual.saccade.peak_velocity", "峰值扫视速度", "saccade", "deg/s", (), ("angle_geometry", "high_rate_validation", "reliable_events"), "R2"),
    _spec("individual.saccade.peak_acceleration", "峰值加速度", "saccade", "deg/s²", (), ("angle_geometry", "high_rate_validation", "reliable_events"), "R2"),
    _spec("individual.saccade.gain", "扫视增益", "saccade", "ratio", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("target_displacement", "coordinate_unit", "coordinate_frame", "reliable_saccade_events"), "R2", "signed-projection-gain-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#saccade_gain_and_endpoint_error", note="逐事件有符号投影增益 dot(注视位移,目标位移)/目标位移平方；零目标位移不可用。需要具名目标移动表及事件目标对应。"),
    _spec("individual.saccade.endpoint_error", "扫视终点误差", "saccade", "coordinate_unit", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("target_position", "target_reference", "coordinate_unit", "coordinate_frame", "reliable_saccade_events"), "R2", "same_frame_target_endpoint_distance-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#saccade_gain_and_endpoint_error", note="同一显式坐标系中的注视终点至对应目标终点欧氏距离；需要具名目标移动表及事件目标对应。"),
    _spec("individual.saccade.main_sequence", "主序关系", "saccade", "model", (), ("angle_geometry", "model_specification"), "R2"),
    _spec("individual.saccade.regression_rate", "回视率", "saccade", "proportion", ("time_s",), ("reading_direction", "reading_scope", "reliable_saccade_events", "coordinate_unit", "coordinate_frame"), "R2", "within-line-opposite-direction-rate-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#rtl_regression_rate", note="仅统计行内、与阅读方向相反的扫视；回行扫视排除。需要明确阅读方向、范围与逐事件行对应。"),
    _spec("individual.microsaccade.features", "微扫视特征", "saccade", "event", (), ("validated_microsaccade_detector", "device_verified"), "R2"),
    _spec("individual.pursuit.features", "平滑追踪增益与误差", "pursuit", "ratio", (), ("target_trajectory", "validated_pursuit_classifier"), "R2"),
    _spec("individual.pupil_left.mean", "左瞳孔均值", "pupil", "pupil_unit", ("pupil_left", "pupil_left_valid", "time_s"), ("pupil_unit", "time_confirmed"), implemented=True),
    _spec("individual.pupil_left.median", "左瞳孔中位数", "pupil", "pupil_unit", ("pupil_left", "pupil_left_valid"), ("pupil_unit",), implemented=True),
    _spec("individual.pupil_left.sd", "左瞳孔标准差", "pupil", "pupil_unit", ("pupil_left", "pupil_left_valid"), ("pupil_unit",), implemented=True),
    _spec("individual.pupil_left.iqr", "左瞳孔 IQR", "pupil", "pupil_unit", ("pupil_left", "pupil_left_valid"), ("pupil_unit",), implemented=True),
    _spec("individual.pupil_right.mean", "右瞳孔均值", "pupil", "pupil_unit", ("pupil_right", "pupil_right_valid", "time_s"), ("pupil_unit", "time_confirmed"), implemented=True),
    _spec("individual.pupil_right.median", "右瞳孔中位数", "pupil", "pupil_unit", ("pupil_right", "pupil_right_valid"), ("pupil_unit",), implemented=True),
    _spec("individual.pupil_right.sd", "右瞳孔标准差", "pupil", "pupil_unit", ("pupil_right", "pupil_right_valid"), ("pupil_unit",), implemented=True),
    _spec("individual.pupil_right.iqr", "右瞳孔 IQR", "pupil", "pupil_unit", ("pupil_right", "pupil_right_valid"), ("pupil_unit",), implemented=True),
    _spec("individual.pupil.baseline_left", "左瞳孔基线差", "pupil", "pupil_unit", ("pupil_left", "pupil_left_valid", "time_s"), ("pupil_unit", "baseline_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.baseline_right", "右瞳孔基线差", "pupil", "pupil_unit", ("pupil_right", "pupil_right_valid", "time_s"), ("pupil_unit", "baseline_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.relative_left", "左瞳孔相对变化", "pupil", "percent", ("pupil_left", "pupil_left_valid", "time_s"), ("pupil_unit", "baseline_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.relative_right", "右瞳孔相对变化", "pupil", "percent", ("pupil_right", "pupil_right_valid", "time_s"), ("pupil_unit", "baseline_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.peak_left", "左瞳孔峰值与峰时", "pupil", "pupil_unit", ("pupil_left", "pupil_left_valid"), ("pupil_unit", "response_window", "reference_event_s"), implemented=True),
    _spec("individual.pupil.peak_right", "右瞳孔峰值与峰时", "pupil", "pupil_unit", ("pupil_right", "pupil_right_valid"), ("pupil_unit", "response_window", "reference_event_s"), implemented=True),
    _spec("individual.pupil.peak_latency_left", "左瞳孔峰时潜伏期", "pupil", "s", ("time_s", "pupil_left", "pupil_left_valid"), ("time_confirmed", "response_window", "reference_event_s"), implemented=True),
    _spec("individual.pupil.peak_latency_right", "右瞳孔峰时潜伏期", "pupil", "s", ("time_s", "pupil_right", "pupil_right_valid"), ("time_confirmed", "response_window", "reference_event_s"), implemented=True),
    _spec("individual.pupil.auc_left", "左瞳孔曲线面积", "pupil", "pupil_unit*s", ("pupil_left", "pupil_left_valid", "time_s"), ("pupil_unit", "response_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.auc_right", "右瞳孔曲线面积", "pupil", "pupil_unit*s", ("pupil_right", "pupil_right_valid", "time_s"), ("pupil_unit", "response_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.slope_left", "左瞳孔变化斜率", "pupil", "pupil_unit/s", ("pupil_left", "pupil_left_valid", "time_s"), ("pupil_unit", "response_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.slope_right", "右瞳孔变化斜率", "pupil", "pupil_unit/s", ("pupil_right", "pupil_right_valid", "time_s"), ("pupil_unit", "response_window", "time_confirmed"), implemented=True),
    _spec("individual.pupil.interocular_difference", "双眼瞳孔差", "pupil", "pupil_unit", ("pupil_left", "pupil_right", "pupil_left_valid", "pupil_right_valid", "time_s"), ("pupil_unit", "time_confirmed"), implemented=True),
    _spec("individual.pupil.interocular_difference.median", "双眼瞳孔差中位数", "pupil", "pupil_unit", ("pupil_left", "pupil_right", "pupil_left_valid", "pupil_right_valid", "time_s"), ("pupil_unit", "time_confirmed"), implemented=True),
    _spec("individual.pupil.interocular_difference.sd", "双眼瞳孔差标准差", "pupil", "pupil_unit", ("pupil_left", "pupil_right", "pupil_left_valid", "pupil_right_valid", "time_s"), ("pupil_unit", "time_confirmed"), implemented=True),
    _spec("individual.pupil.interocular_difference.iqr", "双眼瞳孔差 IQR", "pupil", "pupil_unit", ("pupil_left", "pupil_right", "pupil_left_valid", "pupil_right_valid", "time_s"), ("pupil_unit", "time_confirmed"), implemented=True),
    _spec("individual.pupil.spectral_power", "瞳孔频域功率", "pupil", "pupil_unit²/Hz", ("time_s", "pupil_left", "pupil_left_valid"), ("pupil_unit", "uniform_grid", "stationarity_confirmed"), "R2"),
    _spec("individual.blink.count", "可靠眨眼次数", "blink", "count", (), ("validated_blink_events",), implemented=True),
    _spec("individual.blink.rate", "可靠眨眼频率", "blink", "count/min", (), ("validated_blink_events", "time_confirmed"), implemented=True),
    _spec("individual.blink.duration.mean", "眨眼持续时间", "blink", "s", (), ("validated_blink_events",), implemented=True),
    _spec("individual.blink.interval.mean", "眨眼间隔", "blink", "s", (), ("validated_blink_events", "time_confirmed"), implemented=True),
    _spec("individual.blink.perclos", "PERCLOS", "blink", "proportion", ("eyelid", "time_s"), ("eyelid_threshold", "eyelid_unit", "time_confirmed"), "R2"),
    _spec("individual.aoi.dwell_time", "AOI 驻留时长", "aoi", "s", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "time_confirmed"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.dwell_proportion", "AOI 驻留比例", "aoi", "proportion", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "time_confirmed"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.fixation_count", "AOI 注视次数", "aoi", "count", (), ("reliable_events", "aoi_mapping", "aoi_event_assignment"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.ttff", "首次进入时间", "aoi", "s", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "reference_event_s"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.first_visit_duration", "首次访问时长", "aoi", "s", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "time_confirmed"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.visits", "AOI 访问与重访次数", "aoi", "count", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "time_confirmed"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.mean_visit_duration", "AOI 平均访问时长", "aoi", "s", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "time_confirmed"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.transition_matrix", "AOI 转移矩阵", "aoi", "count/proportion", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping", "time_confirmed"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.coverage", "AOI 覆盖", "aoi", "proportion", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.stationary_entropy", "驻留熵", "aoi", "bit", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.aoi.transition_entropy", "AOI 条件转移熵", "aoi", "bit", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "aoi_mapping"), algorithm="static-aoi-membership", implemented=True, method_version=AOI_GEOMETRY_VERSION),
    _spec("individual.scanpath.length_samples", "扫描路径长度（采样点）", "scanpath", "coordinate_unit", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame"), implemented=True),
    _spec("individual.scanpath.length_fixations", "扫描路径长度（注视中心）", "scanpath", "coordinate_unit", (), ("coordinate_unit", "coordinate_frame", "reliable_events"), implemented=True),
    _spec("individual.scanpath.efficiency", "扫描路径效率", "scanpath", "ratio", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "start_target", "end_target"), implemented=True),
    _spec("individual.scanpath.spatial_spread_x", "空间分散 X", "scanpath", "coordinate_unit", ("gaze_x", "gaze_valid"), ("coordinate_unit", "coordinate_frame"), implemented=True),
    _spec("individual.scanpath.spatial_spread_y", "空间分散 Y", "scanpath", "coordinate_unit", ("gaze_y", "gaze_valid"), ("coordinate_unit", "coordinate_frame"), implemented=True),
    _spec("individual.heatmap", "时间加权热图网格", "scanpath", "s", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("coordinate_unit", "coordinate_frame", "heatmap_grid", "time_confirmed"), "R1", "time_weighted_grid_traversal-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#heatmap_boundaries", note="返回可审计的格点时长/比例表，标量值为网格内支持秒数；忽略插值样本并不跨越长缺口或不相连片段。"),
    _spec("individual.scanpath.multimatch.shape", "MultiMatch 形状相似性", "scanpath", "similarity", (), ("multimatch_reference", "reliable_fixation_events", "coordinate_unit", "coordinate_frame"), "R2", "adswa-multimatch-gaze-0.1.3", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#multimatch_children", note="MultiMatch 形状维度；可单独选择或由五维family批量选择。只返回所选维度。"),
    _spec("individual.scanpath.multimatch.direction", "MultiMatch 方向相似性", "scanpath", "similarity", (), ("multimatch_reference", "reliable_fixation_events", "coordinate_unit", "coordinate_frame"), "R2", "adswa-multimatch-gaze-0.1.3", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#multimatch_children"),
    _spec("individual.scanpath.multimatch.length", "MultiMatch 长度相似性", "scanpath", "similarity", (), ("multimatch_reference", "reliable_fixation_events", "coordinate_unit", "coordinate_frame"), "R2", "adswa-multimatch-gaze-0.1.3", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#multimatch_children"),
    _spec("individual.scanpath.multimatch.position", "MultiMatch 位置相似性", "scanpath", "similarity", (), ("multimatch_reference", "reliable_fixation_events", "coordinate_unit", "coordinate_frame"), "R2", "adswa-multimatch-gaze-0.1.3", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#multimatch_children"),
    _spec("individual.scanpath.multimatch.duration", "MultiMatch 时长相似性", "scanpath", "similarity", (), ("multimatch_reference", "reliable_fixation_events", "coordinate_unit", "coordinate_frame"), "R2", "adswa-multimatch-gaze-0.1.3", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#multimatch_children"),
    _spec("individual.scanpath.scanmatch.raw_score", "ScanMatch Needleman–Wunsch 原始分数", "scanpath", "substitution_score", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("scanmatch_grid", "scanmatch_reference", "coordinate_unit", "coordinate_frame"), "R2", "global-needleman-wunsch-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#scanmatch_boundaries", note="原始全局比对分数；尺度取决于记录的替代矩阵和间隙罚分。"),
    _spec("individual.scanpath.scanmatch.normalized_similarity", "ScanMatch 参考自对齐归一化相似度", "scanpath", "proportion", ("gaze_x", "gaze_y", "gaze_valid", "time_s"), ("scanmatch_grid", "scanmatch_reference", "coordinate_unit", "coordinate_frame"), "R2", "self-alignment-ratio-v1", implemented=True, validated=True, validation_status="targeted_software_validation", evidence_ref="a02-boundary-recheck-20261013.json#scanmatch_boundaries", note="原始分数除以观察/参考自对齐分数的较大值；仅在分母为正且比值处于 [0,1] 时输出。"),
)


def get_individual_catalogue() -> list[dict[str, Any]]:
    """Return a JSON-safe copy of the complete chapter-7 catalogue."""
    entries = [dict(x, requires_fields=list(x["requires_fields"]),
                    requires_metadata=list(x["requires_metadata"]),
                    analysis_modes=list(x["analysis_modes"]),
                    reference_ids=list(x["reference_ids"])) for x in INDIVIDUAL_CATALOGUE]
    selectors = (
        (_MULTIMATCH_FAMILY, "MultiMatch 五维参考相似性", "R2",
         "Selecting this family expands to five independently keyed dimensions; no aggregate score is emitted."),
        (_SCANMATCH_FAMILY, "ScanMatch 参考序列分数与相似度", "R2",
         "Selecting this family expands to a raw score and a separately normalized self-alignment ratio."),
    )
    for metric_id, label, tier, note in selectors:
        entries.append({"metric_id": metric_id, "label": label, "category": "scanpath_selector",
                        "unit": "family", "algorithm_id": "expanded-selection-v1",
                        "algorithm_version": ALGORITHM_VERSION, "requires_fields": [],
                        "requires_metadata": [], "analysis_modes": ["individual"],
                        "reference_ids": ["PRD-v0.9:7"], "validated": True,
                        "validation_status": "selector", "device_requirement": "not_required",
                        "experimental": tier == "R2", "tier": tier, "implemented": True,
                        "selection_only": True, "child_metric_ids": list(_FAMILY_SELECTIONS[metric_id]),
                        "note": note, "evidence_ref": None})
    return entries


def _metadata(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("metadata", {})
    return value if isinstance(value, dict) else {}


def _extension_field_context(frame: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(config)
    raw_columns = metadata.get("raw_source_columns", [])
    semantics = metadata.get("extension_field_semantics", {})
    return {
        "canonical_fields_present": [name for name in _EXTENDED_FIELDS if name in frame],
        "raw_source_columns_declared": sorted({x for x in raw_columns if isinstance(x, str)}) if isinstance(raw_columns, (list, tuple, set)) else [],
        "field_semantics_declared_for": sorted(x for x in semantics if isinstance(x, str)) if isinstance(semantics, dict) else [],
        "units_inferred": False,
    }


def _nested_meta(config: dict[str, Any]) -> dict[str, Any]:
    m = _metadata(config)
    cm = m.get("coordinate_metadata", config.get("coordinate_metadata", {}))
    return cm if isinstance(cm, dict) else {}


def _meta_value(config: dict[str, Any], key: str, *aliases: str) -> Any:
    m = _metadata(config)
    cm = _nested_meta(config)
    for source in (config, m, cm):
        for name in (key, *aliases):
            if name in source:
                return source[name]
    return None


def _provenance(config: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Require source identity and a real content hash before a result is complete."""
    raw = config.get("provenance", {})
    if not isinstance(raw, dict):
        raw = {}
    manifest = raw.get("source_manifest", config.get("source_manifest"))
    if manifest is None:
        manifest = _metadata(config).get("source_manifest")
    if not isinstance(manifest, list) or not manifest:
        return False, {"state": "missing", "source_manifest": []}
    accepted: list[dict[str, Any]] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            return False, {"state": "invalid", "source_manifest": []}
        source_id = entry.get("source_id") or entry.get("source_name")
        digest = entry.get("sha256") or entry.get("content_sha256")
        if not isinstance(source_id, str) or not source_id.strip() or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            return False, {"state": "invalid", "source_manifest": []}
        accepted.append({"source_id": source_id, "sha256": digest.lower()})
    refs = raw.get("processing_refs", config.get("processing_refs", []))
    return True, {"state": "verified", "source_manifest": accepted,
                  "processing_refs": [str(x) for x in refs] if isinstance(refs, (list, tuple)) else []}


def _field_condition(frame: pd.DataFrame, fields: tuple[str, ...]) -> tuple[bool, str | None]:
    absent = [f for f in fields if f not in frame]
    return (not absent, "missing_fields:" + ",".join(absent) if absent else None)


def _required_metadata(spec: dict[str, Any], config: dict[str, Any]) -> tuple[bool, str | None]:
    missing: list[str] = []
    for key in spec["requires_metadata"]:
        if key == "time_confirmed":
            okay = _meta_value(config, "time_unit") == "s" and _meta_value(config, "time_confirmed", "time_source_confirmed", "sync_confirmed") is True
        elif key == "coordinate_unit":
            val = _meta_value(config, "coordinate_unit")
            okay = isinstance(val, str) and bool(val.strip()) and val.lower() != "unknown"
        elif key == "coordinate_frame":
            val = _meta_value(config, "coordinate_frame")
            okay = isinstance(val, str) and bool(val.strip()) and val.lower() != "unknown"
        elif key == "pupil_unit":
            val = _meta_value(config, "pupil_unit")
            okay = isinstance(val, str) and bool(val.strip()) and val.lower() not in {"unknown", ""}
        elif key == "reliable_events":
            ev = config.get("events")
            okay = isinstance(ev, pd.DataFrame) and not ev.empty and config.get("event_source_confirmed") is True
        elif key in {"reliable_saccade_events", "reliable_fixation_events"}:
            ev=config.get("events")
            event_type="saccade" if key=="reliable_saccade_events" else "fixation"
            source=_meta_value(config,"event_source")
            version=_meta_value(config,"event_algorithm_version")
            okay=(isinstance(ev,pd.DataFrame) and not ev.empty
                  and {"event_id","event_type","start_s","end_s","status","subject_id","session_id"}.issubset(ev.columns)
                  and ev["event_type"].astype(str).str.lower().str.contains(event_type,regex=False).any()
                  and config.get("event_source_confirmed") is True
                  and isinstance(source,str) and bool(source.strip())
                  and isinstance(version,str) and bool(version.strip()))
        elif key == "validated_blink_events":
            ev = config.get("events")
            okay = isinstance(ev, pd.DataFrame) and "blink" in set(ev.get("event_type", pd.Series(dtype=str)).astype(str).str.lower()) and config.get("blink_source_confirmed") is True
        elif key == "aoi_mapping":
            okay = bool(config.get("aoi_mapping_confirmed", False))
        elif key == "baseline_window":
            w = config.get("baseline_window_s")
            okay = isinstance(w, (list, tuple)) and len(w) == 2 and all(_finite_number(x) for x in w) and float(w[0]) < float(w[1])
        elif key == "response_window":
            w = config.get("response_window_s")
            okay = isinstance(w, (list, tuple)) and len(w) == 2 and all(_finite_number(x) for x in w) and float(w[0]) < float(w[1])
        elif key in {"reference_event_s", "target_onset_s"}:
            okay = _finite_number(config.get(key))
        elif key == "stable_target_confirmed":
            okay = config.get("stable_target_confirmed") is True
        elif key == "stable_fixation_window":
            w = config.get("stable_fixation_window_s")
            okay = (config.get("stable_target_confirmed") is True and isinstance(w, (list, tuple))
                    and len(w) == 2 and all(_finite_number(x) for x in w) and float(w[0])<float(w[1]))
        elif key == "bcea_probability":
            p=_meta_value(config,"bcea_probability")
            okay=_finite_number(p) and 0<float(p)<1
        elif key == "start_target" or key == "end_target":
            okay = key.replace("_target", "_point") in config
        elif key == "calibration_target_confirmed":
            target_unit=_meta_value(config,"target_coordinate_unit") or _meta_value(config,"coordinate_unit")
            target_frame=_meta_value(config,"target_coordinate_frame") or _meta_value(config,"coordinate_frame")
            okay = (config.get(key) is True
                    and isinstance(config.get("calibration_window_s"),(list,tuple))
                    and len(config["calibration_window_s"])==2
                    and all(_finite_number(x) for x in config["calibration_window_s"])
                    and float(config["calibration_window_s"][0])<float(config["calibration_window_s"][1])
                    and target_unit==_meta_value(config,"coordinate_unit")
                    and target_frame==_meta_value(config,"coordinate_frame"))
        elif key == "target_reference":
            table=config.get("target_movement_intervals")
            required={"target_movement_id","target_id","start_s","end_s","target_start_x","target_start_y","target_end_x","target_end_y","unit","coordinate_frame"}
            digest=_meta_value(config,"target_reference_sha256")
            target_unit=_meta_value(config,"target_coordinate_unit") or _meta_value(config,"coordinate_unit")
            target_frame=_meta_value(config,"target_coordinate_frame") or _meta_value(config,"coordinate_frame")
            okay=(isinstance(table,pd.DataFrame) and not table.empty and required.issubset(table.columns)
                  and isinstance(digest,str) and bool(_SHA256.fullmatch(digest))
                  and isinstance(target_unit,str) and target_unit not in {"","unknown"}
                  and isinstance(target_frame,str) and target_frame not in {"","unknown"}
                  and table["unit"].astype(str).eq(target_unit).all()
                  and table["coordinate_frame"].astype(str).eq(target_frame).all()
                  and table[["target_movement_id","target_id"]].notna().all().all()
                  and np.isfinite(table[["start_s","end_s","target_start_x","target_start_y","target_end_x","target_end_y"]].apply(pd.to_numeric,errors="coerce").to_numpy(dtype=float)).all()
                  and (pd.to_numeric(table["end_s"],errors="coerce")>pd.to_numeric(table["start_s"],errors="coerce")).all()
                  and not table.duplicated(["target_movement_id","target_id"]).any()
                  and target_unit==_meta_value(config,"coordinate_unit")
                  and target_frame==_meta_value(config,"coordinate_frame"))
        elif key == "target_displacement" or key == "target_position":
            table=config.get("target_movement_intervals")
            required={"target_movement_id","target_id","start_s","end_s","target_start_x","target_start_y","target_end_x","target_end_y","unit","coordinate_frame"}
            digest=_meta_value(config,"target_reference_sha256")
            target_unit=_meta_value(config,"target_coordinate_unit") or _meta_value(config,"coordinate_unit")
            target_frame=_meta_value(config,"target_coordinate_frame") or _meta_value(config,"coordinate_frame")
            okay=(isinstance(table,pd.DataFrame) and not table.empty and required.issubset(table.columns)
                  and config.get("target_reference_confirmed") is True
                  and isinstance(digest,str) and bool(_SHA256.fullmatch(digest))
                  and table["unit"].astype(str).eq(str(target_unit)).all()
                  and table["coordinate_frame"].astype(str).eq(str(target_frame)).all()
                  and table[["target_movement_id","target_id"]].notna().all().all()
                  and np.isfinite(table[["start_s","end_s","target_start_x","target_start_y","target_end_x","target_end_y"]].apply(pd.to_numeric,errors="coerce").to_numpy(dtype=float)).all()
                  and (pd.to_numeric(table["end_s"],errors="coerce")>pd.to_numeric(table["start_s"],errors="coerce")).all()
                  and not table.duplicated(["target_movement_id","target_id"]).any()
                  and target_unit==_meta_value(config,"coordinate_unit")
                  and target_frame==_meta_value(config,"coordinate_frame"))
        elif key == "angle_geometry":
            g=config.get("angle_geometry")
            if not isinstance(g,dict):
                g=_nested_meta(config).get("angle_geometry",_nested_meta(config).get("geometry"))
            required=("screen_size_px","screen_size_mm","viewing_distance_mm","screen_origin")
            okay=(isinstance(g,dict) and g.get("confirmed") is True and g.get("screen_origin")=="top_left"
                  and all(isinstance(g.get(k),(list,tuple)) and len(g[k])==2 and all(_finite_number(v) and float(v)>0 for v in g[k]) for k in required[:2])
                  and _finite_number(g.get("viewing_distance_mm")) and float(g["viewing_distance_mm"])>0
                  and bool(_meta_value(config,"coordinate_frame"))
                  and g.get("coordinate_frame")==_meta_value(config,"coordinate_frame")
                  and str(_meta_value(config,"coordinate_unit")).lower()=="px")
        elif key == "high_rate_validation":
            evidence=_meta_value(config,"high_rate_validation")
            device_status=_meta_value(config,"device_validation_status")
            device_method=_meta_value(config,"device_validation_method")
            device_evidence=_meta_value(config,"device_validation_evidence")
            minimum=config.get("minimum_saccade_sample_rate_hz",250)
            okay=(isinstance(evidence,dict) and (evidence.get("confirmed") is True or evidence.get("validated") is True)
                  and isinstance(evidence.get("source_refs"),(list,tuple)) and len(evidence["source_refs"])>0
                  and all(isinstance(x,str) and x.strip() for x in evidence["source_refs"])
                  and _finite_number(evidence.get("sample_rate_hz")) and _finite_number(minimum)
                  and float(evidence["sample_rate_hz"])>=float(minimum)
                  and _meta_value(config,"device_verified") is True
                  and device_status=="verified"
                  and isinstance(device_method,str) and bool(device_method.strip())
                  and isinstance(device_evidence,(list,tuple)) and len(device_evidence)>0
                  and all(isinstance(x,str) and x.strip() for x in device_evidence))
        elif key == "reading_direction":
            okay=config.get("reading_direction") in {"ltr","rtl"} and config.get("reading_scope_confirmed") is True
        elif key == "reading_scope":
            minimum=config.get("reading_min_amplitude")
            okay=(config.get("reading_scope_confirmed") is True and isinstance(config.get("reading_scope_id"),str)
                  and bool(config["reading_scope_id"].strip()) and _finite_number(minimum) and float(minimum)>0)
        elif key == "heatmap_grid":
            g=config.get("heatmap_grid")
            okay=(isinstance(g,dict) and all(_finite_number(g.get(k)) for k in ("xmin","xmax","ymin","ymax"))
                  and float(g["xmax"])>float(g["xmin"]) and float(g["ymax"])>float(g["ymin"])
                  and isinstance(g.get("columns"),(int,np.integer)) and not isinstance(g.get("columns"),bool) and g["columns"]>0
                  and isinstance(g.get("rows"),(int,np.integer)) and not isinstance(g.get("rows"),bool) and g["rows"]>0)
        elif key == "multimatch_reference":
            ref=config.get("reference_fixations")
            required={"reference_id","fixation_index","start_x","start_y","duration_s"}
            digest=_meta_value(config,"reference_trajectory_sha256")
            reference_id=config.get("reference_trajectory_id")
            ref_unit=_meta_value(config,"reference_trajectory_unit","reference_coordinate_unit")
            ref_frame=_meta_value(config,"reference_trajectory_coordinate_frame","reference_coordinate_frame")
            okay=(isinstance(ref,pd.DataFrame) and not ref.empty and required.issubset(ref.columns)
                  and isinstance(reference_id,str) and bool(reference_id.strip())
                  and ref["reference_id"].astype(str).eq(reference_id).all()
                  and pd.to_numeric(ref["fixation_index"],errors="coerce").notna().all()
                  and pd.to_numeric(ref["fixation_index"],errors="coerce").is_unique
                  and isinstance(digest,str) and bool(_SHA256.fullmatch(digest))
                  and isinstance(ref_unit,str) and ref_unit==_meta_value(config,"coordinate_unit")=="px"
                  and isinstance(ref_frame,str) and ref_frame==_meta_value(config,"coordinate_frame")
                  and isinstance(config.get("screen_size_px"),(list,tuple)) and len(config["screen_size_px"])==2
                  and all(_finite_number(v) and float(v)>0 for v in config["screen_size_px"])
                  and importlib.util.find_spec("multimatch_gaze") is not None)
        elif key == "scanmatch_reference":
            ref=config.get("reference_trajectory")
            required={"reference_id","time_s","gaze_x","gaze_y"}
            valid_flags_ok=("reference_valid" not in ref if isinstance(ref,pd.DataFrame) else True)
            if isinstance(ref,pd.DataFrame) and "reference_valid" in ref:
                valid_flags_ok=all(pd.isna(value) or isinstance(value,(bool,np.bool_))
                                   for value in ref["reference_valid"].tolist())
            digest=_meta_value(config,"reference_trajectory_sha256")
            reference_id=config.get("reference_trajectory_id")
            ref_unit=_meta_value(config,"reference_trajectory_unit","reference_coordinate_unit")
            ref_frame=_meta_value(config,"reference_trajectory_coordinate_frame","reference_coordinate_frame")
            okay=(isinstance(ref,pd.DataFrame) and not ref.empty and required.issubset(ref.columns)
                  and isinstance(reference_id,str) and bool(reference_id.strip())
                  and ref["reference_id"].astype(str).eq(reference_id).all()
                  and valid_flags_ok
                  and isinstance(digest,str) and bool(_SHA256.fullmatch(digest))
                  and isinstance(ref_unit,str) and ref_unit==_meta_value(config,"coordinate_unit")=="px"
                  and isinstance(ref_frame,str) and ref_frame==_meta_value(config,"coordinate_frame")
                  and isinstance(config.get("screen_size_px"),(list,tuple)) and len(config["screen_size_px"])==2
                  and all(_finite_number(v) and float(v)>0 for v in config["screen_size_px"]))
        elif key == "scanmatch_grid":
            p=config.get("scanmatch")
            okay=(isinstance(p,dict) and all(isinstance(p.get(k),(int,np.integer)) and not isinstance(p.get(k),bool) and p[k]>0 for k in ("grid_cols","grid_rows"))
                  and _finite_number(p.get("time_bin_s")) and float(p["time_bin_s"])>0
                  and isinstance(p.get("substitution_matrix"),dict) and bool(p["substitution_matrix"])
                  and all(isinstance(k,str) and _finite_number(v) for k,v in p.get("substitution_matrix",{}).items())
                  and _finite_number(p.get("gap_penalty")) and float(p["gap_penalty"])<=0)
        elif key == "aoi_event_assignment":
            okay = config.get("aoi_event_assignment") in {"center", "time_intersection"}
        elif key in config:
            okay = config[key] is not None and config[key] is not False
        elif key in _metadata(config):
            okay = _metadata(config)[key] is not None and _metadata(config)[key] is not False
        else:
            okay = False
        if not okay:
            missing.append(key)
    return (not missing, "missing_metadata:" + ",".join(missing) if missing else None)


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float, np.number)) and not isinstance(value, bool) and math.isfinite(float(value))


def _availability(frame: pd.DataFrame, config: dict[str, Any], specs: tuple[dict[str, Any], ...] = INDIVIDUAL_CATALOGUE) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        fields_ok, field_reason = _field_condition(frame, spec["requires_fields"])
        meta_ok, meta_reason = _required_metadata(spec, config)
        if not fields_ok or not meta_ok:
            reason_code = "missing_precondition"
            reason = "; ".join(x for x in (field_reason, meta_reason) if x)
            status = AvailabilityStatus.BLOCKED.value
            available = False
        elif spec.get("experimental") and config.get("enable_experimental") is not True:
            reason_code = "experimental_opt_in_required"
            reason = "R2 研究方法须显式启用，并保留实验状态。"
            status = AvailabilityStatus.EXPERIMENTAL.value
            available = False
        elif not spec.get("implemented"):
            reason_code = "method_not_implemented"
            reason = spec.get("note") or "此方法目录项尚无已验证实现。"
            status = AvailabilityStatus.UNAVAILABLE.value
            available = False
        elif not spec.get("validated"):
            reason_code = "method_not_validated"
            reason = spec.get("note") or "算法实现尚未通过所需数值验证。"
            status = AvailabilityStatus.EXPERIMENTAL.value
            available = False
        elif spec.get("implemented"):
            reason_code = "experimental_method" if spec.get("experimental") else "ready"
            reason = "R2 方法已实现，已显式启用并保留实验状态。" if spec.get("experimental") else "实现、数值验证与所需数据条件满足。"
            status = AvailabilityStatus.EXPERIMENTAL.value if spec.get("experimental") else AvailabilityStatus.AVAILABLE.value
            available = True
        rows.append({"metric_id": spec["metric_id"], "available": available,
                     "status": status, "reason_code": reason_code, "reason": reason,
                     "required_fields": list(spec["requires_fields"]),
                     "required_metadata": list(spec["requires_metadata"])})
    by_id = {row["metric_id"]: row for row in rows}
    for family_id, child_ids in _FAMILY_SELECTIONS.items():
        children = [by_id[child_id] for child_id in child_ids if child_id in by_id]
        if not children:
            continue
        ready = all(row["available"] for row in children)
        blocked = any(row["status"] == AvailabilityStatus.BLOCKED.value for row in children)
        reason_rows = [row for row in children if not row["available"]]
        rows.append({"metric_id": family_id, "available": ready,
                     "status": (AvailabilityStatus.EXPERIMENTAL.value if ready else
                                AvailabilityStatus.BLOCKED.value if blocked else
                                reason_rows[0]["status"]),
                     "reason_code": ("expanded_selection_ready" if ready else
                                     "expanded_children_unavailable"),
                     "reason": ("Selecting this family emits each child metric as a separate row; no aggregate score is emitted."
                                if ready else "Expanded child outputs are unavailable: " + "; ".join(
                                    f"{row['metric_id']}={row['reason_code']}" for row in reason_rows)),
                     "required_fields": sorted({field for row in children for field in row["required_fields"]}),
                     "required_metadata": sorted({name for row in children for name in row["required_metadata"]})})
    return pd.DataFrame(rows, columns=AVAILABILITY_COLUMNS)


def _segment_timebase_unset(value: Any) -> bool:
    return pd.isna(value) or str(value).strip() in {"", "unconfirmed", "legacy_unconfirmed"}


def _segments_for_group(frame: pd.DataFrame, segments: pd.DataFrame | None,
                        overlap_policy: str | None = None, *,
                        scope_frame: pd.DataFrame | None = None) -> list[tuple[Any, pd.DataFrame, dict[str, Any]]]:
    if segments is None or segments.empty:
        part = frame.copy()
        part.attrs["_window_support_frame"] = frame.copy()
        return [(None, part, {})]
    required = {"subject_id", "session_id", "segment_id", "start_s", "end_s"}
    if not required.issubset(segments.columns):
        raise ContractError("segments missing required columns: " + ", ".join(sorted(required - set(segments.columns))))
    for column in ("subject_id", "session_id"):
        if (segments[column].isna() | segments[column].astype("string").str.strip().eq("")).any():
            raise ContractError(f"segment {column} must be explicit")
    if "scope_kind" in segments:
        shared = segments["scope_kind"].astype("string").eq("shared_interaction").fillna(False)
        interactions = segments.get("interaction_id", pd.Series(pd.NA, index=segments.index, dtype="string"))
        if (shared & (interactions.isna() | interactions.astype("string").str.strip().eq(""))).any():
            raise ContractError("shared interaction segment needs an explicit interaction_id")
        timebases = segments.get("timebase_id", pd.Series(pd.NA, index=segments.index, dtype="string"))
        if (shared & timebases.map(_segment_timebase_unset)).any():
            raise ContractError("shared interaction segment needs an explicit timebase_id")
    subject = frame.iloc[0]["subject_id"]
    session = frame.iloc[0]["session_id"]
    relevant = segments.loc[(segments["subject_id"] == subject) & (segments["session_id"] == session)].copy()
    reference = frame if scope_frame is None else scope_frame
    reference = reference.loc[(reference["subject_id"] == subject) & (reference["session_id"] == session)]
    bounds: list[tuple[float, float, Any, int]] = []
    for row_index, seg in relevant.iterrows():
        try:
            start, end = float(seg["start_s"]), float(seg["end_s"])
        except (TypeError, ValueError):
            raise ContractError("segment boundaries must be finite numeric seconds") from None
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ContractError("segments must be nonempty finite left-closed, right-open intervals")
        bounds.append((start, end, seg.get("interaction_id") if "interaction_id" in relevant else None, row_index))
    dependency_by_row: dict[int, set[str]] = {row_index: set() for *_, row_index in bounds}
    scope_keys = tuple(key for key in (
        "source_id", "subject_id", "session_id", "interaction_id", "trial",
        "stimulus_id", "analysis_stream_id", "physical_partition_id", "timebase_id", "clock_id",
    ) if key in relevant.columns)
    for i, (start_a, end_a, interaction_a, row_a) in enumerate(bounds):
        for start_b, end_b, interaction_b, row_b in bounds[i + 1:]:
            records = relevant.loc[[row_a, row_b]]
            same_stream = True
            for key in scope_keys:
                left, right = records.iloc[0].get(key), records.iloc[1].get(key)
                if pd.notna(left) and pd.notna(right) and str(left) != str(right):
                    same_stream = False
                    break
            if same_stream and max(start_a, start_b) < min(end_a, end_b):
                policies = records.get("overlap_policy", pd.Series(index=records.index, dtype="object"))
                keep_labels = (str(overlap_policy).casefold() == "keep_labels" if overlap_policy is not None
                               else len(policies) == 2 and policies.astype("string").str.casefold().eq("keep_labels").all())
                if not keep_labels:
                    raise ContractError("overlapping segment membership requires explicit overlap_policy='keep_labels'")
                identities = sorted(str(value) for value in records.get("segment_instance_id", records["segment_id"]))
                overlap_ref = "segment-overlap:" + hashlib.sha256(
                    json.dumps({"instances": identities, "start_s": max(start_a, start_b),
                                "end_s": min(end_a, end_b)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                dependency_by_row[row_a].add(overlap_ref)
                dependency_by_row[row_b].add(overlap_ref)
    out = []
    for row_index, seg in relevant.iterrows():
        start, end = float(seg["start_s"]), float(seg["end_s"])
        support = frame.copy()
        candidate_scope = reference
        timebase_unset = _segment_timebase_unset(seg.get("timebase_id"))
        for key in ("trial", "timebase_id", "analysis_stream_id", "source_id"):
            if key == "timebase_id" and timebase_unset:
                continue
            if key in segments.columns and key in support and pd.notna(seg.get(key)):
                support = support.loc[support[key].astype("string").eq(str(seg.get(key))).fillna(False)].copy()
                if key in candidate_scope:
                    candidate_scope = candidate_scope.loc[candidate_scope[key].astype("string").eq(str(seg.get(key))).fillna(False)]
        interaction = seg.get("interaction_id")
        if pd.notna(interaction) and str(interaction).strip():
            if "interaction_id" not in support:
                raise ContractError("segment interaction_id cannot be matched to signals")
            support = support.loc[support["interaction_id"].astype("string").eq(str(interaction)).fillna(False)].copy()
            if "interaction_id" in candidate_scope:
                candidate_scope = candidate_scope.loc[candidate_scope["interaction_id"].astype("string").eq(str(interaction)).fillna(False)]
        elif "interaction_id" in candidate_scope:
            candidates = candidate_scope.loc[(candidate_scope["time_s"] >= start) & (candidate_scope["time_s"] < end), "interaction_id"].astype("string")
            candidates = candidates.mask(candidates.str.strip().eq(""), pd.NA)
            if candidates.nunique(dropna=False) > 1:
                raise ContractError("segments need interaction_id when multiple interactions share a subject/session interval")
        if timebase_unset:
            window = candidate_scope.loc[(candidate_scope["time_s"] >= start) & (candidate_scope["time_s"] < end)]
            for key in ("timebase_id", "source_id"):
                if key not in window:
                    continue
                candidates = window[key].astype("string")
                absent = candidates.isna() | candidates.str.strip().eq("")
                if key == "timebase_id":
                    absent |= candidates.str.strip().isin({"unconfirmed", "legacy_unconfirmed"})
                if candidates.mask(absent, pd.NA).nunique(dropna=False) > 1:
                    raise ContractError(f"segments need explicit {key} when an undeclared timebase spans multiple or mixed scopes")
        part = support.loc[(support["time_s"] >= start) & (support["time_s"] < end)].copy()
        meta = seg.to_dict()
        if "segment_instance_id" not in meta or pd.isna(meta.get("segment_instance_id")):
            instance_payload = {key: meta.get(key) for key in ("segment_id", "subject_id", "session_id", "interaction_id", "start_s", "end_s")}
            meta["segment_instance_id"] = "legacy-segment-instance:" + hashlib.sha256(
                json.dumps(instance_payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        refs = set(meta.get("shared_observation_group_id") or ())
        refs.update(dependency_by_row.get(row_index, set()))
        meta["dependence_ref"] = tuple(sorted(str(ref) for ref in refs)) or None
        part.attrs["_window_support_frame"] = support.copy()
        out.append((seg["segment_id"], part, meta))
    return out


def _flag_mask(frame: pd.DataFrame, value_col: str, valid_col: str) -> np.ndarray:
    analysis_columns = {
        "gaze_x": "analysis_gaze_valid", "gaze_y": "analysis_gaze_valid",
        "pupil_left": "analysis_pupil_left_valid", "pupil_right": "analysis_pupil_right_valid",
    }
    selected_valid_col = analysis_columns.get(value_col, valid_col)
    if selected_valid_col not in frame:
        selected_valid_col = valid_col
    if value_col not in frame or selected_valid_col not in frame:
        return np.zeros(len(frame), dtype=bool)
    values = pd.to_numeric(frame[value_col], errors="coerce").to_numpy(dtype=float)
    flags = frame[selected_valid_col].fillna(False).astype(bool).to_numpy()
    return flags & np.isfinite(values)


def _gap_limit_s(times: np.ndarray, config: dict[str, Any], median_dt: float | None=None) -> float | None:
    """Return the explicit support-gap cap shared by individual and group metrics."""
    for key in ("max_gap_s", "support_gap_max_s"):
        value=config.get(key)
        if _finite_number(value) and float(value)>0:
            return float(value)
    if median_dt is None or not _finite_number(median_dt) or float(median_dt)<=0:
        t=np.asarray(times,dtype=float)
        dt=np.diff(t)
        positive=dt[np.isfinite(dt)&(dt>0)]
        if not len(positive):
            return None
        median_dt=float(np.median(positive))
    factor=config.get("support_gap_factor",3.0)
    if not _finite_number(factor) or float(factor)<=1:
        factor=3.0
    return float(factor)*float(median_dt)


def _time_weighted_mean_values(times: np.ndarray, values: np.ndarray, valid: np.ndarray,
                               config: dict[str, Any], start: float | None = None,
                               end: float | None = None) -> tuple[float | None, float]:
    """Use the shared processing helper so individual summaries match preprocessing."""
    t = np.asarray(times, dtype=float)
    max_gap = _gap_limit_s(t,config)
    window = (float(start), float(end)) if start is not None and end is not None else None
    return time_weighted_mean(t, values, valid, max_gap, window=window)


def _interp_mask(frame: pd.DataFrame, signal_name: str) -> np.ndarray:
    names = (f"{signal_name}_interpolated", "is_interpolated")
    for name in names:
        if name in frame:
            return frame[name].fillna(False).astype(bool).to_numpy()
    return np.zeros(len(frame), dtype=bool)


def _ordered(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["time_s", "original_index"], kind="mergesort").reset_index(drop=True)


def _interval_weights(frame: pd.DataFrame, valid: np.ndarray, config: dict[str, Any], start: float | None = None, end: float | None = None) -> tuple[np.ndarray, np.ndarray, float]:
    """Left-held sample support; invalids and long gaps contribute no duration."""
    t = pd.to_numeric(frame["time_s"], errors="coerce").to_numpy(dtype=float)
    weights = np.zeros(len(t), dtype=float)
    if len(t) < 2:
        return weights, np.zeros(max(0, len(t) - 1), dtype=bool), 0.0
    dt = np.diff(t)
    positive = dt[np.isfinite(dt) & (dt > 0)]
    if positive.size == 0:
        return weights, np.zeros(len(dt), dtype=bool), 0.0
    median_dt = float(np.median(positive))
    max_gap = config.get("max_gap_s")
    if not _finite_number(max_gap) or float(max_gap) <= 0:
        max_gap = config.get("support_gap_max_s")
    if not _finite_number(max_gap) or float(max_gap) <= 0:
        factor = config.get("support_gap_factor", 3.0)
        if not _finite_number(factor) or float(factor) <= 1:
            factor = 3.0
        max_gap = float(factor) * median_dt
    ok = (dt > 0) & (dt <= float(max_gap)) & valid[:-1] & valid[1:]
    left = t[:-1].copy()
    right = t[1:].copy()
    if start is not None:
        left = np.maximum(left, start)
    if end is not None:
        right = np.minimum(right, end)
    widths = np.maximum(0.0, right - left)
    weights[:-1] = np.where(ok, widths, 0.0)
    return weights, ok, float(weights.sum())


def _window_support_observation_count(frame: pd.DataFrame, valid: np.ndarray,
                                      config: dict[str, Any], start: float, end: float) -> int:
    """Count original endpoint samples used by real adjacent clipped support."""
    times = pd.to_numeric(frame["time_s"], errors="coerce").to_numpy(dtype=float)
    if len(times) < 2 or len(valid) != len(times):
        return 0
    gap = _gap_limit_s(times, config)
    if gap is None:
        return 0
    used: set[int] = set()
    for index, delta in enumerate(np.diff(times)):
        if (valid[index] and valid[index + 1] and 0 < delta <= gap
                and min(times[index + 1], end) > max(times[index], start)):
            used.add(index)
            used.add(index + 1)
    return len(used)


def _events(config: dict[str, Any]) -> pd.DataFrame:
    ev = config.get("events")
    if not isinstance(ev, pd.DataFrame) or ev.empty:
        return pd.DataFrame(columns=EVENT_RESULT_COLUMNS)
    required = {"event_id", "event_type", "start_s", "end_s", "status", "reason_code", "n_observed", "n_interpolated"}
    if not required.issubset(ev.columns):
        return pd.DataFrame(columns=EVENT_RESULT_COLUMNS)
    out = ev.copy()
    starts = pd.to_numeric(out["start_s"], errors="coerce")
    ends = pd.to_numeric(out["end_s"], errors="coerce")
    out["duration_s"] = ends - starts
    if "original_start_s" not in out:
        out["original_start_s"] = starts
    if "original_end_s" not in out:
        out["original_end_s"] = ends
    for c in EVENT_RESULT_COLUMNS:
        if c not in out:
            if c == "algorithm_version":
                out[c] = str(config.get("event_algorithm_version", "source-event-table"))
            elif c == "parameter_ref":
                out[c] = "source-event-table"
            else:
                out[c] = None
    valid = np.isfinite(starts.to_numpy(dtype=float, na_value=np.nan))
    valid &= np.isfinite(ends.to_numpy(dtype=float, na_value=np.nan))
    valid &= ends.to_numpy(dtype=float, na_value=np.nan) > starts.to_numpy(dtype=float, na_value=np.nan)
    valid &= out["event_id"].notna().to_numpy() & out["event_type"].notna().to_numpy()
    out = out.loc[valid].copy()
    identity = ["analysis_stream_id", "source_id", "event_id"]
    identity = [column for column in identity if column in out]
    if identity and out.duplicated(identity, keep=False).any():
        raise ContractError("event IDs must be unique within their source analysis stream")
    return out.reset_index(drop=True)


def _event_consumer_rows(events: pd.DataFrame, event_type: str,
                         config: dict[str, Any]) -> tuple[pd.DataFrame, bool]:
    """Apply consumer-specific event provenance gates without certifying hardware."""
    if events.empty:
        return events.copy(), False
    kinds = events["event_type"].astype("string").str.casefold().str.contains(event_type, regex=False).fillna(False)
    rows = events.loc[kinds].copy()
    if rows.empty:
        return rows, False
    status_ok = rows["status"].astype("string").str.casefold().isin({"reliable", "complete", "source_confirmed"})
    evidence = rows.get("event_evidence", pd.Series(pd.NA, index=rows.index)).astype("string").str.casefold()
    local = (evidence.eq("local_method_derived")
             & rows.get("method_validation_ref", pd.Series(pd.NA, index=rows.index)).astype("string").eq(EVENT_METHOD_VALIDATION_REF)
             & rows.get("source_validity_status", pd.Series("valid", index=rows.index)).astype("string").eq("valid"))
    source_refs = rows.get("source_refs", pd.Series([None] * len(rows), index=rows.index))
    has_refs = source_refs.map(lambda value: isinstance(value, (list, tuple, set)) and bool(value))
    legacy_declared = (evidence.isna()
                       & bool(config.get("event_source_confirmed"))
                       & bool(isinstance(config.get("event_source"), str) and config.get("event_source", "").strip())
                       & bool(isinstance(config.get("event_algorithm_version"), str) and config.get("event_algorithm_version", "").strip()))
    vendor = evidence.eq("vendor_observed") & has_refs & status_ok
    if event_type == "blink":
        vendor &= bool(config.get("blink_source_confirmed"))
        vendor &= isinstance(config.get("blink_source", config.get("event_source")), str)
        vendor &= bool(config.get("blink_algorithm_version", config.get("event_algorithm_version")))
        keep = (vendor | (legacy_declared & bool(config.get("blink_source_confirmed")))) & status_ok
    else:
        keep = (local | vendor | legacy_declared) & status_ok
    mode = str(config.get("event_execution_mode", "")).casefold()
    if mode in {"local", "local_only"}:
        keep &= local
    elif mode in {"vendor", "external", "external_only"}:
        keep &= vendor | legacy_declared
    elif mode in {"side_by_side", "compare"}:
        # Side-by-side results need separate source keys; legacy scalar IDs cannot encode that.
        keep &= False
    # If caller did not choose a side, mixed vendor/local sources are not pooled.
    selected_evidence = set(evidence.loc[keep].dropna().astype(str).str.casefold())
    if mode not in {"local", "local_only", "vendor", "external", "external_only"} and len(selected_evidence - {"vendor_observed", "local_method_derived"}) > 0:
        return rows.iloc[0:0].copy(), False
    if not mode and {"vendor_observed", "local_method_derived"} <= selected_evidence:
        return rows.iloc[0:0].copy(), False
    legacy_limited = bool((legacy_declared & keep).any()) if isinstance(legacy_declared, pd.Series) else bool(legacy_declared)
    return rows.loc[keep].copy(), legacy_limited


def _scope_metric_fields(frame: pd.DataFrame, seg_meta: dict[str, Any] | None = None,
                         start: float | None = None, end: float | None = None) -> dict[str, Any]:
    seg_meta = seg_meta or {}
    support = frame.attrs.get("_window_support_frame", frame)
    stream_id = None
    if "analysis_stream_id" in frame and frame["analysis_stream_id"].notna().any():
        values = frame["analysis_stream_id"].dropna().astype(str).unique()
        stream_id = str(values[0]) if len(values) == 1 else None
    if stream_id is None:
        source = frame.iloc[0].get("source_id") if len(frame) and "source_id" in frame else "legacy"
        subject = frame.iloc[0].get("subject_id") if len(frame) else "unknown"
        session = frame.iloc[0].get("session_id") if len(frame) else "unknown"
        payload = json.dumps([str(source), str(subject), str(session)], ensure_ascii=False, separators=(",", ":"))
        stream_id = "legacy:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    eye = None
    for column in ("selected_eye", "eye_label", "eye"):
        if column in frame and frame[column].notna().any():
            values = frame[column].dropna().astype(str).unique()
            if len(values) == 1:
                eye = str(values[0])
                break
    parent_refs = None
    if "contributing_eyes" in frame:
        values = frame["contributing_eyes"].dropna().tolist()
        refs = sorted({str(value) for item in values for value in (item if isinstance(item, (list, tuple, set)) else (item,))})
        parent_refs = tuple(refs) or None
    sample_ids = []
    if len(frame):
        id_column = "observation_id" if "observation_id" in frame else "original_index"
        sample_ids = sorted(str(value) for value in frame[id_column].dropna().unique())
    support_ids = []
    if len(support):
        id_column = "observation_id" if "observation_id" in support else "original_index"
        support_ids = sorted(str(value) for value in support[id_column].dropna().unique())
    support_payload = json.dumps({"stream": stream_id, "window_ids": sample_ids, "support_ids": support_ids,
                                  "window": [start, end]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    support_ref = "support:" + hashlib.sha256(support_payload.encode("utf-8")).hexdigest()
    dependency = seg_meta.get("dependence_ref")
    if dependency is None:
        dependency = seg_meta.get("shared_observation_group_id")
    if isinstance(dependency, str):
        dependence_ref = (dependency,)
    elif isinstance(dependency, (tuple, list, set)):
        dependence_ref = tuple(sorted(str(value) for value in dependency if value is not None and not pd.isna(value))) or None
    else:
        dependence_ref = None
    partition = None
    if "physical_partition_id" in frame:
        values = frame["physical_partition_id"].dropna().astype(str).unique()
        partition = str(values[0]) if len(values) == 1 else None
    instance_id = seg_meta.get("segment_instance_id")
    if instance_id is not None and pd.isna(instance_id):
        instance_id = None
    return {"analysis_stream_id": stream_id, "eye_label": eye,
            "stream_parent_refs": parent_refs, "segment_instance_id": instance_id,
            "physical_partition_id": partition, "support_ref": support_ref,
            "dependence_ref": dependence_ref}


def _param_ref(config: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
    values = {k: v for k, v in config.items()
              if k not in {"events", "metadata", "provenance", "source_manifest"}
              and not k.startswith("_aoi_") and not isinstance(v, pd.DataFrame)}
    if extra:
        values.update(extra)
    def clean(v: Any) -> Any:
        if is_dataclass(v):
            return asdict(v)
        if isinstance(v, (np.integer, np.floating)):
            return v.item()
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, dict):
            return {str(k): clean(x) for k, x in v.items() if not isinstance(x, pd.DataFrame)}
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        return str(v)
    raw = json.dumps(clean(values), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return raw


def _row(spec: dict[str, Any], config: dict[str, Any], prov_ok: bool,
         subject: Any = None, session: Any = None, segment: Any = None,
         value: Any = None, unit: str | None = None, reason: str | None = None,
         n_obs: int = 0, n_interp: int = 0, duration: float | None = None,
         interaction: Any = None, condition: Any = None, trial: Any = None,
         extra_params: dict[str, Any] | None = None,
         analysis_stream_id: str | None = None, eye_label: str | None = None,
         stream_parent_refs: tuple[str, ...] | None = None,
         segment_instance_id: str | None = None, physical_partition_id: str | None = None,
         support_ref: str | None = None, dependence_ref: tuple[str, ...] | str | None = None,
         n_window_observed: int | None = None, n_support_observed: int | None = None) -> dict[str, Any]:
    computed = value is not None and _finite_number(value)
    experimental = bool(spec.get("experimental"))
    status = "complete" if computed and prov_ok and reason is None and not experimental else ("partial" if computed else "unavailable")
    code = None if status == "complete" else (reason or ("experimental_method" if computed and experimental else ("provenance_missing" if computed else "precondition_not_met")))
    return {
        "run_id": config.get("run_id"), "subject_id": subject, "session_id": session,
        "interaction_id": interaction, "pair_id": None, "segment_id": segment,
        "trial": trial, "condition": condition, "metric_id": spec["metric_id"],
        "value": float(value) if computed else np.nan, "unit": unit or spec["unit"],
        "status": status, "reason_code": code, "n_observed": int(n_obs),
        "n_interpolated": int(n_interp), "effective_duration_s": float(duration) if _finite_number(duration) else np.nan,
        "algorithm_version": spec["algorithm_version"],
        "parameter_ref": _param_ref(config, extra_params),
        "analysis_stream_id": analysis_stream_id, "eye_label": eye_label,
        "stream_parent_refs": stream_parent_refs, "segment_instance_id": segment_instance_id,
        "physical_partition_id": physical_partition_id, "support_ref": support_ref,
        "dependence_ref": dependence_ref,
        "n_window_observed": int(n_obs if n_window_observed is None else n_window_observed),
        "n_support_observed": (None if n_support_observed is None else int(n_support_observed)),
    }


def _simple_event_metrics(frame: pd.DataFrame, events: pd.DataFrame, config: dict[str, Any], prov_ok: bool, segment_id: Any, seg_meta: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
    if not isinstance(events, pd.DataFrame) or events.empty:
        return
    subject, session = frame.iloc[0]["subject_id"], frame.iloc[0]["session_id"]
    start, end = float(seg_meta.get("start_s", frame["time_s"].min())), float(seg_meta.get("end_s", frame["time_s"].max()))
    declared_types = {str(x).lower() for x in config.get("event_source_types", [])} if isinstance(config.get("event_source_types"), (list, tuple, set)) else set()
    identities = {key: frame.iloc[0].get(key) for key in (
        "source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id",
        "analysis_stream_id", "physical_partition_id", "timebase_id", "clock_id")
        if key in frame.columns and pd.notna(frame.iloc[0].get(key))}
    allow_legacy_stream = "analysis_stream_id" not in identities
    scope_fields = _scope_metric_fields(frame, seg_meta, start, end)
    for typ, prefix in (("fixation", "fixation"), ("saccade", "saccade"), ("blink", "blink")):
        eligible, legacy_limited = _event_consumer_rows(events, typ, config)
        if eligible.empty and typ not in declared_types:
            continue
        onset_rows = query_event_onsets(
            eligible, start, end, identities=identities,
            allow_unspecified_stream=allow_legacy_stream)
        if not onset_rows.empty:
            onset_rows = onset_rows.loc[
                onset_rows["event_type"].astype("string").str.casefold().str.contains(typ, regex=False)
            ].copy()
        sub = query_event_overlaps(
            eligible, start, end, identities=identities,
            allow_unspecified_stream=allow_legacy_stream)
        if not sub.empty:
            sub = sub.loc[sub["event_type"].astype("string").str.casefold().str.contains(typ, regex=False)].copy()
        # Window queries retain original source bounds and IDs; only the selected
        # duration contribution is clipped to this label.
        onset_rows = onset_rows.sort_values("start_s", kind="mergesort")
        sub = sub.sort_values("intersection_start_s", kind="mergesort")
        sub["source_start_s"] = pd.to_numeric(sub.get("start_s"), errors="coerce")
        sub["source_end_s"] = pd.to_numeric(sub.get("end_s"), errors="coerce")
        sub["duration_in_window_s"] = pd.to_numeric(sub.get("intersection_duration_s"), errors="coerce")
        onset_rows["source_start_s"] = pd.to_numeric(onset_rows.get("start_s"), errors="coerce")
        onset_rows["source_end_s"] = pd.to_numeric(onset_rows.get("end_s"), errors="coerce")
        count_id = f"individual.{prefix}.count"
        count_spec = next((s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == count_id), None)
        if count_spec:
            metrics.append(_row(count_spec, config, prov_ok, subject, session, segment_id, len(onset_rows), "count",
                n_obs=int(pd.to_numeric(onset_rows.get("n_observed", pd.Series(dtype=float)),errors="coerce").fillna(0).sum()),
                n_interp=int(pd.to_numeric(onset_rows.get("n_interpolated", pd.Series(dtype=float)),errors="coerce").fillna(0).sum()),
                duration=end-start, interaction=frame.iloc[0].get("interaction_id"),
                condition=seg_meta.get("condition", frame.iloc[0].get("condition")),
                trial=seg_meta.get("trial", frame.iloc[0].get("trial")),
                reason="legacy_event_provenance_limited" if legacy_limited else None,
                **scope_fields))
        if sub.empty:
            continue
        durations = pd.to_numeric(sub["duration_in_window_s"], errors="coerce").to_numpy(dtype=float)
        durations = durations[np.isfinite(durations) & (durations >= 0)]
        if len(durations) == 0:
            continue
        if prefix == "fixation":
            ids = (("individual.fixation.duration.mean", float(np.mean(durations))), ("individual.fixation.duration.median", float(np.median(durations))), ("individual.fixation.total_duration", float(np.sum(durations))), ("individual.fixation.first_duration", float(durations[0])), ("individual.fixation.last_duration", float(durations[-1])))
        else:
            ids = ((f"individual.{prefix}.duration.mean", float(np.mean(durations))),)
        for metric_id, val in ids:
            spec = next((s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == metric_id), None)
            if spec:
                metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, val, "s", reason="legacy_event_provenance_limited" if legacy_limited else None, n_obs=len(durations), duration=end-start, interaction=frame.iloc[0].get("interaction_id"), condition=seg_meta.get("condition", frame.iloc[0].get("condition")), trial=seg_meta.get("trial", frame.iloc[0].get("trial")), **scope_fields))
        if prefix in {"fixation", "saccade"}:
            rate_id = f"individual.{prefix}.rate"
            rate_spec = next((s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == rate_id), None)
            if rate_spec and end > start:
                metrics.append(_row(rate_spec, config, prov_ok, subject, session, segment_id, len(onset_rows) / ((end-start)/60), "count/min", reason="legacy_event_provenance_limited" if legacy_limited else None, n_obs=len(onset_rows), duration=end-start, interaction=frame.iloc[0].get("interaction_id"), condition=seg_meta.get("condition", frame.iloc[0].get("condition")), trial=seg_meta.get("trial", frame.iloc[0].get("trial")), **scope_fields))
        if prefix == "blink" and len(onset_rows) > 1:
            gap = np.diff(pd.to_numeric(onset_rows["source_start_s"], errors="coerce").to_numpy(dtype=float))
            spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.blink.interval.mean")
            metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, float(np.mean(gap)), "s", n_obs=len(gap), duration=end-start, **scope_fields))
        if prefix == "fixation" and not onset_rows.empty:
            latency = config.get("reference_event_s")
            if _finite_number(latency):
                spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.fixation.latency")
                first = float(pd.to_numeric(onset_rows["source_start_s"], errors="coerce").min())
                if first >= float(latency):
                    metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, first-float(latency), "s", n_obs=len(sub), duration=end-start))
            # Fixation occupancy is divided by valid, non-interpolated gaze support.
            support_frame = frame.attrs.get("_window_support_frame", frame)
            support_gaze = _flag_mask(support_frame, "gaze_x", "gaze_valid") & _flag_mask(support_frame, "gaze_y", "gaze_valid") & ~_interp_mask(support_frame, "gaze")
            gaze = _flag_mask(frame, "gaze_x", "gaze_valid") & _flag_mask(frame, "gaze_y", "gaze_valid") & ~_interp_mask(frame, "gaze")
            _, _, gaze_duration = _interval_weights(support_frame, support_gaze, config, start, end)
            spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.fixation.proportion")
            union_duration = 0.0
            intervals = sorted((float(a), float(b)) for a,b in zip(sub["intersection_start_s"],sub["intersection_end_s"]) if float(b)>float(a))
            merged: list[list[float]] = []
            for a,b in intervals:
                if not merged or a > merged[-1][1]: merged.append([a,b])
                else: merged[-1][1] = max(merged[-1][1],b)
            union_duration = sum(b-a for a,b in merged)
            proportion = union_duration/gaze_duration if gaze_duration>0 else None
            metrics.append(_row(spec,config,prov_ok,subject,session,segment_id,proportion,"proportion",reason=("legacy_event_provenance_limited" if legacy_limited else None) if proportion is not None else "zero_gaze_support",n_obs=int(gaze.sum()),duration=gaze_duration,interaction=frame.iloc[0].get("interaction_id"),condition=seg_meta.get("condition",frame.iloc[0].get("condition")),trial=seg_meta.get("trial",frame.iloc[0].get("trial")),extra_params={"overlap_handling":"union_of_event_intervals","n_window_observed":int(gaze.sum()),"n_support_observed":_window_support_observation_count(support_frame,support_gaze,config,start,end)},n_window_observed=int(gaze.sum()),n_support_observed=_window_support_observation_count(support_frame,support_gaze,config,start,end),**scope_fields))
            # Position summaries use explicit valid raw gaze samples within reliable event windows.
            if all(c in frame for c in ("gaze_x","gaze_y","gaze_valid")):
                ts=pd.to_numeric(frame["time_s"],errors="coerce").to_numpy(dtype=float)
                x=pd.to_numeric(frame["gaze_x"],errors="coerce").to_numpy(dtype=float)
                y=pd.to_numeric(frame["gaze_y"],errors="coerce").to_numpy(dtype=float)
                inside=np.zeros(len(frame),dtype=bool)
                for _, er in sub.iterrows(): inside|=(ts>=float(er["intersection_start_s"]))&(ts<=float(er["intersection_end_s"]))
                use=gaze&inside
                if use.any() and _required_metadata(next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.fixation.centroid_x"),config)[0]:
                    for axis, vals, metric_id in (("x",x,"individual.fixation.centroid_x"),("y",y,"individual.fixation.centroid_y")):
                        spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                        metrics.append(_row(spec,config,prov_ok,subject,session,segment_id,float(np.mean(vals[use])),str(_meta_value(config,"coordinate_unit")),n_obs=int(use.sum()),duration=gaze_duration,interaction=frame.iloc[0].get("interaction_id"),condition=seg_meta.get("condition",frame.iloc[0].get("condition")),trial=seg_meta.get("trial",frame.iloc[0].get("trial")),extra_params={"centroid":"arithmetic mean of observed in-event samples","axis":axis}))
                    for axis, vals, metric_id in (("x",x,"individual.fixation.dispersion_x"),("y",y,"individual.fixation.dispersion_y")):
                        spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                        metrics.append(_row(spec,config,prov_ok,subject,session,segment_id,float(np.ptp(vals[use])),str(_meta_value(config,"coordinate_unit")),n_obs=int(use.sum()),duration=gaze_duration,interaction=frame.iloc[0].get("interaction_id"),condition=seg_meta.get("condition",frame.iloc[0].get("condition")),trial=seg_meta.get("trial",frame.iloc[0].get("trial")),extra_params={"dispersion":"range","axis":axis}))
        if prefix == "saccade" and not onset_rows.empty and _finite_number(config.get("target_onset_s")):
            spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.saccade.latency")
            first = float(pd.to_numeric(onset_rows["source_start_s"], errors="coerce").min())
            if first >= float(config["target_onset_s"]):
                metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, first-float(config["target_onset_s"]), "s", n_obs=len(sub), duration=end-start))
        if prefix == "saccade":
            unit=str(_meta_value(config,"coordinate_unit") or "unknown")
            amp_col="amplitude" if "amplitude" in sub else None
            direction_col="direction_rad" if "direction_rad" in sub else None
            event_values = onset_rows if not onset_rows.empty else sub
            if amp_col is None and all(c in event_values for c in ("start_x","start_y","end_x","end_y")):
                sx=pd.to_numeric(event_values["start_x"],errors="coerce").to_numpy(dtype=float); sy=pd.to_numeric(event_values["start_y"],errors="coerce").to_numpy(dtype=float)
                ex=pd.to_numeric(event_values["end_x"],errors="coerce").to_numpy(dtype=float); ey=pd.to_numeric(event_values["end_y"],errors="coerce").to_numpy(dtype=float)
                amplitudes=np.hypot(ex-sx,ey-sy); directions=np.arctan2(ey-sy,ex-sx)
            elif amp_col is not None:
                amplitudes=pd.to_numeric(event_values[amp_col],errors="coerce").to_numpy(dtype=float)
                directions=pd.to_numeric(event_values[direction_col],errors="coerce").to_numpy(dtype=float) if direction_col else np.full(len(event_values),np.nan)
            else:
                amplitudes=np.array([],dtype=float); directions=np.array([],dtype=float)
            good=np.isfinite(amplitudes)
            if good.any() and unit!="unknown":
                spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.saccade.amplitude.mean")
                metrics.append(_row(spec,config,prov_ok,subject,session,segment_id,float(np.mean(amplitudes[good])),unit,n_obs=int(good.sum()),duration=end-start,extra_params={"amplitude_source":"event values or endpoint displacement"}))
            dg=np.isfinite(directions)
            if dg.any():
                for metric_id,value in (("individual.saccade.direction.mean_sin",float(np.mean(np.sin(directions[dg])))),("individual.saccade.direction.mean_cos",float(np.mean(np.cos(directions[dg]))))):
                    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                    metrics.append(_row(spec,config,prov_ok,subject,session,segment_id,value,"unitless",n_obs=int(dg.sum()),duration=end-start,extra_params={"direction_unit":"radian","screen_y_sign":config.get("screen_y_sign","source_coordinate"),"summary":"mean_sin_cos"}))


def _aoi_shapes(aois: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    prepared = config.get("_aoi_geometry_prepared")
    if not isinstance(prepared, PreparedStaticAOIs):
        return []
    shapes: list[dict[str, Any]] = []
    for row in prepared.rows:
        item = dict(row)
        geometry = item.pop("_canonical_geometry", None)
        if isinstance(geometry, CanonicalGeometry):
            item["geometry"] = geometry
            shapes.append(item)
    return shapes


def _inside_polygon(x: float, y: float, pts: np.ndarray) -> bool:
    geometry = validate_geometry("polygon", pts)
    return point_in_geometry((x, y), geometry)


def _aoi_geometry_error(code: str, message: str, *, source: dict[str, Any] | None = None,
                         canonical_field: str | None = None) -> AOIGeometryError:
    return AOIGeometryError(AOIGeometryIssue(
        code, message, source=source or {}, canonical_field=canonical_field,
    ))


def _prepare_aoi_geometry_for_frame(
    frame: pd.DataFrame,
    aois: pd.DataFrame | None,
    config: dict[str, Any],
) -> PreparedStaticAOIs:
    if aois is None or aois.empty or not set(AOI_REQUIRED_COLUMNS).issubset(aois.columns):
        raise _aoi_geometry_error("aoi_geometry_missing", "No usable static AOI table is available")
    if "stimulus_id" not in frame:
        raise _aoi_geometry_error("aoi_stimulus_mapping_unavailable",
                                  "Signal rows have no stimulus_id for AOI binding",
                                  canonical_field="stimulus_id")
    budget = config.get("_aoi_geometry_budget")
    if not isinstance(budget, AOIGeometryBudget):
        budget = AOIGeometryBudget(cancel_check=config.get("_aoi_cancel_check"))
        config["_aoi_geometry_budget"] = budget
    budget.checkpoint()
    signal_stimuli = []
    for value in frame["stimulus_id"].dropna().tolist():
        budget.checkpoint()
        signal_stimuli.append(value)

    def contains_stimulus(values: list[Any], target: Any) -> bool:
        for value in values:
            budget.checkpoint()
            if _aoi_id_equal(target, value):
                return True
        return False
    if not signal_stimuli:
        raise _aoi_geometry_error("aoi_stimulus_mapping_unavailable",
                                  "Signal rows have no nonmissing stimulus_id for AOI binding",
                                  canonical_field="stimulus_id")
    records: list[dict[str, Any]] = []
    selected_stimuli: list[Any] = []
    for row_label, row in aois.iterrows():
        budget.checkpoint()
        record = row.to_dict()
        record.setdefault("_aoi_direct_row_label", row_label)
        if contains_stimulus(signal_stimuli, record.get("stimulus_id")):
            records.append(record)
            if not contains_stimulus(selected_stimuli, record.get("stimulus_id")):
                selected_stimuli.append(record.get("stimulus_id"))
    missing_stimuli = [stimulus for stimulus in signal_stimuli
                       if not contains_stimulus(selected_stimuli, stimulus)]
    if missing_stimuli:
        raise _aoi_geometry_error(
            "aoi_stimulus_definition_missing",
            "At least one signal stimulus has no AOI definition; AOI outputs are blocked",
            canonical_field="stimulus_id",
            source={"missing_stimuli": [_aoi_audit_scalar(x) for x in missing_stimuli]},
        )
    context = aois.attrs.get("aoi_source_context", {"origin": "typed"})
    try:
        prepared = prepare_static_aois(
            records,
            source=context if isinstance(context, dict) else {"origin": "typed"},
            missing_name_policy=config.get("aoi_missing_name_policy", "require_source"),
            budget=budget,
            cancel_check=config.get("_aoi_cancel_check"),
            allow_legacy_shape_aliases=config.get("aoi_allow_legacy_shape_aliases") is True,
        )
    except AOIGeometryError:
        raise
    unit = _meta_value(config, "coordinate_unit")
    coordinate_frame = _meta_value(config, "coordinate_frame")
    for row in prepared.rows:
        row_unit = row["unit"] if "unit" in row else unit
        row_frame = row["coordinate_frame"] if "coordinate_frame" in row else coordinate_frame
        unit_mismatch = row_unit is not None and (_aoi_value_missing(row_unit) or row_unit != unit)
        frame_mismatch = row_frame is not None and (_aoi_value_missing(row_frame) or row_frame != coordinate_frame)
        if unit_mismatch or frame_mismatch:
            raise _aoi_geometry_error(
                "aoi_coordinate_semantics_mismatch",
                "AOI coordinate unit/frame does not match the declared signal frame",
                source=dict(row.get("aoi_source", {})),
                canonical_field="unit" if unit_mismatch else "coordinate_frame",
            )
    return prepared


def _aoi_id_equal(left: Any, right: Any) -> bool:
    try:
        result = left == right
        return bool(result) if not pd.isna(result) else False
    except (TypeError, ValueError):
        return False


def _aoi_between(a: float, b: float, value: float) -> bool:
    return min(a, b) <= value <= max(a, b)


def _aoi_value_missing(value: Any) -> bool:
    try:
        result = pd.isna(value)
        return bool(result) if isinstance(result, (bool, np.bool_)) else False
    except (TypeError, ValueError):
        return False


def _aoi_audit_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _aoi_labels(frame: pd.DataFrame, aois: pd.DataFrame | None, config: dict[str, Any]) -> tuple[list[set[Any]], list[dict[str, Any]], str | None]:
    issue = config.get("_aoi_geometry_issue")
    if isinstance(issue, dict):
        return [set() for _ in range(len(frame))], [], str(issue.get("code", "aoi_geometry_unavailable"))
    prepared = config.get("_aoi_geometry_prepared")
    if not isinstance(prepared, PreparedStaticAOIs):
        try:
            prepared = _prepare_aoi_geometry_for_frame(frame, aois, config)
            config["_aoi_geometry_prepared"] = prepared
        except AOIGeometryError as exc:
            config["_aoi_geometry_issue"] = exc.issue.as_dict()
            return [set() for _ in range(len(frame))], [], exc.code
    all_shapes = _aoi_shapes(aois, config)
    frame_stimuli = frame["stimulus_id"].tolist() if "stimulus_id" in frame else []
    stimuli_in_frame = []
    for stimulus in frame_stimuli:
        if not any(_aoi_id_equal(stimulus, seen) for seen in stimuli_in_frame):
            stimuli_in_frame.append(stimulus)
    shapes = [shape for shape in all_shapes
              if any(_aoi_id_equal(shape["stimulus_id"], stimulus) for stimulus in stimuli_in_frame)]
    if not shapes:
        return [set() for _ in range(len(frame))], [], "aoi_stimulus_definition_missing"
    if not _meta_value(config, "coordinate_unit") or not _meta_value(config, "coordinate_frame"):
        return [set() for _ in range(len(frame))], shapes, "unknown_coordinate_semantics"
    if config.get("aoi_mapping_confirmed") is not True:
        return [set() for _ in range(len(frame))], shapes, "aoi_mapping_unconfirmed"
    labels: list[set[Any]] = []
    xs = pd.to_numeric(frame.get("gaze_x"), errors="coerce").to_numpy(dtype=float) if "gaze_x" in frame else np.full(len(frame), np.nan)
    ys = pd.to_numeric(frame.get("gaze_y"), errors="coerce").to_numpy(dtype=float) if "gaze_y" in frame else np.full(len(frame), np.nan)
    valid = _flag_mask(frame, "gaze_x", "gaze_valid") & np.isfinite(ys)
    stim = frame["stimulus_id"].tolist() if "stimulus_id" in frame else [None] * len(frame)
    budget = config.get("_aoi_geometry_budget")
    if not isinstance(budget, AOIGeometryBudget):
        budget = AOIGeometryBudget(cancel_check=config.get("_aoi_cancel_check"))
        config["_aoi_geometry_budget"] = budget
    membership_scope_id = config.get("_aoi_membership_scope_id")
    budget.begin_membership_scope(
        membership_scope_id if isinstance(membership_scope_id, str) else "unscoped"
    )
    edge_upper_bound = 0
    exact_upper_bound = 0
    for i, (x, y) in enumerate(zip(xs, ys)):
        if not valid[i]:
            continue
        for shape in shapes:
            if not _aoi_id_equal(shape["stimulus_id"], stim[i]):
                continue
            geometry = shape["geometry"]
            if geometry.shape == "rectangle":
                edge_upper_bound += 4
                continue
            for edge_index, a in enumerate(geometry.vertices):
                b = geometry.vertices[(edge_index + 1) % len(geometry.vertices)]
                edge_upper_bound += 1
                if (_aoi_between(a[0], b[0], x) and _aoi_between(a[1], b[1], y)) or (
                    (a[1] <= y < b[1]) or (b[1] <= y < a[1])
                ):
                    exact_upper_bound += 1
    try:
        budget.preflight_membership_scope(
            edge_upper_bound, exact_upper_bound,
            source={"origin": "typed", "scope": "individual_aoi_membership"},
        )
    except AOIGeometryError as exc:
        config["_aoi_geometry_issue"] = exc.issue.as_dict()
        return [set() for _ in range(len(frame))], shapes, exc.code
    for i, (x, y) in enumerate(zip(xs, ys)):
        hit: list[dict[str, Any]] = []
        if valid[i]:
            for shp in shapes:
                if shp["stimulus_id"] != stim[i]:
                    continue
                try:
                    inside = point_in_geometry(
                        (x, y), shp["geometry"],
                        budget=config.get("_aoi_geometry_budget"),
                        source=shp.get("aoi_source"),
                    )
                except AOIGeometryError as exc:
                    config["_aoi_geometry_issue"] = exc.issue.as_dict()
                    return [set() for _ in range(len(frame))], shapes, exc.code
                if inside:
                    hit.append(shp)
        if len(hit) > 1:
            policies = {s.get("overlap_policy") for s in hit}
            if policies == {"exclusive"}:
                priorities = [s.get("priority") for s in hit]
                if any(not _finite_number(p) for p in priorities) or len(set(priorities)) != len(priorities):
                    config["_aoi_geometry_issue"] = AOIGeometryIssue(
                        "aoi_overlap_priority_unavailable",
                        "Exclusive AOI overlap needs explicit finite unique priorities",
                        source=dict(hit[0].get("aoi_source", {})),
                        canonical_field="priority",
                    ).as_dict()
                    return [set() for _ in range(len(frame))], shapes, "aoi_overlap_priority_unavailable"
                hit = [min(hit, key=lambda s: float(s["priority"]))]
            elif policies != {"multi_label"}:
                config["_aoi_geometry_issue"] = AOIGeometryIssue(
                    "aoi_overlap_policy_unavailable",
                    "Overlapping AOIs require one consistent explicit policy",
                    source=dict(hit[0].get("aoi_source", {})),
                    canonical_field="overlap_policy",
                ).as_dict()
                return [set() for _ in range(len(frame))], shapes, "aoi_overlap_policy_unavailable"
        labels.append({x["aoi_id"] for x in hit})
    return labels, shapes, None


def _aoi_metric_audit(shape: dict[str, Any]) -> dict[str, Any]:
    return {
        "aoi_id": str(_aoi_audit_scalar(shape.get("aoi_id"))),
        "stimulus_id": _aoi_audit_scalar(shape.get("stimulus_id")),
        "aoi_name": _aoi_audit_scalar(shape.get("aoi_name")),
        "aoi_name_status": shape.get("aoi_name_status", "legacy_missing"),
        "aoi_domain_schema_version": shape.get("aoi_domain_schema_version", AOI_DOMAIN_SCHEMA_VERSION),
        "aoi_geometry_version": shape.get("aoi_geometry_version", AOI_GEOMETRY_VERSION),
        "aoi_boundary_policy": shape.get("aoi_boundary_policy", AOI_BOUNDARY_POLICY),
        "aoi_input_closure": shape.get("aoi_input_closure"),
        "aoi_geometry_fingerprint": shape.get("aoi_geometry_fingerprint"),
        "aoi_source": shape.get("aoi_source"),
    }


def _compute_aoi(frame: pd.DataFrame, aois: pd.DataFrame | None, config: dict[str, Any], prov_ok: bool, segment_id: Any, seg_meta: dict[str, Any], metrics: list[dict[str, Any]], events_out: list[dict[str, Any]]) -> None:
    if frame.empty:
        return
    ordered = _ordered(frame)
    labels, shapes, err = _aoi_labels(ordered, aois, config)
    if err or not shapes:
        return
    mask = _flag_mask(ordered, "gaze_x", "gaze_valid") & np.isfinite(pd.to_numeric(ordered["gaze_y"], errors="coerce").to_numpy(dtype=float))
    w, interval_ok, total = _interval_weights(ordered, mask & ~_interp_mask(ordered, "gaze"), config)
    ids = [s["aoi_id"] for s in shapes if any(s["stimulus_id"] == stim for stim in (ordered["stimulus_id"].unique() if "stimulus_id" in ordered else [None]))]
    if not ids:
        return
    observed = int(mask.sum())
    interped = int(_interp_mask(ordered, "gaze").sum())
    subject, session = ordered.iloc[0]["subject_id"], ordered.iloc[0]["session_id"]
    for aid in ids:
        aoi_shape = next((s for s in shapes if _aoi_id_equal(s["aoi_id"], aid)
                          and any(_aoi_id_equal(s["stimulus_id"], stim) for stim in
                                  (ordered["stimulus_id"].unique() if "stimulus_id" in ordered else [None]))), shapes[0])
        aoi_audit = _aoi_metric_audit(aoi_shape)
        dwell = sum(w[i] for i in range(max(0, len(w)-1)) if aid in labels[i])
        base = {"subject": subject, "session": session, "segment": segment_id,
                "interaction": ordered.iloc[0].get("interaction_id"),
                "trial": seg_meta.get("trial", ordered.iloc[0].get("trial")),
                "condition": seg_meta.get("condition", ordered.iloc[0].get("condition"))}
        for metric_id, value, unit in (("individual.aoi.dwell_time", dwell, "s"), ("individual.aoi.dwell_proportion", dwell / total if total > 0 else None, "proportion")):
            spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == metric_id)
            metrics.append(_row(spec, config, prov_ok, **base, value=value if total > 0 else None, unit=unit, reason=None if total > 0 else "zero_effective_duration", n_obs=observed, n_interp=interped, duration=total, extra_params={**aoi_audit}))
        # Each continuous AOI visit is determined from adjacent valid intervals.
        visits: list[tuple[float, float]] = []
        active_start = None
        active_end = None
        for i in range(max(0, len(ordered)-1)):
            on = bool(interval_ok[i] and aid in labels[i] and w[i] > 0)
            t0 = float(ordered.iloc[i]["time_s"])
            t1 = t0 + float(w[i])
            if on:
                if active_start is None:
                    active_start, active_end = t0, t1
                elif active_end is not None and abs(t0-active_end) <= 1e-12:
                    active_end = t1
                else:
                    visits.append((active_start, active_end))
                    active_start, active_end = t0, t1
            elif active_start is not None:
                visits.append((active_start, active_end if active_end is not None else active_start))
                active_start = active_end = None
        if active_start is not None:
            visits.append((active_start, active_end if active_end is not None else active_start))
        # Events are the durable visit trace; event ids are source-derived from the first sample index.
        for idx, (vstart, vend) in enumerate(visits):
            sample_pos = int(np.searchsorted(ordered["time_s"].to_numpy(dtype=float), vstart, side="left"))
            orig = ordered.iloc[min(sample_pos, len(ordered)-1)].get("original_index")
            events_out.append({"run_id": config.get("run_id"), "subject_id": subject, "session_id": session,
                "segment_id": segment_id, "event_id": f"aoi-visit:{aid}:{orig}", "event_type": "aoi_visit",
                "start_s": vstart, "end_s": vend, "duration_s": max(0.0, vend-vstart), "status": "complete" if prov_ok else "partial",
                "reason_code": None if prov_ok else "provenance_missing", "n_observed": 0, "n_interpolated": 0,
                "algorithm_version": AOI_GEOMETRY_VERSION,
                "parameter_ref": _param_ref(config, {**aoi_audit, "visit_index": idx, "interval_rule": "left-held-valid-adjacent"})})
        count = len(visits)
        for metric_id, val, unit in (("individual.aoi.visits", count, "count"),
                                     ("individual.aoi.first_visit_duration", visits[0][1]-visits[0][0] if visits else None, "s"),
                                     ("individual.aoi.mean_visit_duration", float(np.mean([b-a for a,b in visits])) if visits else None, "s")):
            spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == metric_id)
            metrics.append(_row(spec, config, prov_ok, **base, value=val, unit=unit, reason=None if visits else "no_aoi_visit", n_obs=observed, n_interp=interped, duration=total, extra_params={**aoi_audit}))
        ref = config.get("reference_event_s")
        spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.aoi.ttff")
        first = visits[0][0] if visits else None
        val = first-float(ref) if visits and _finite_number(ref) and first >= float(ref) else None
        metrics.append(_row(spec, config, prov_ok, **base, value=val, unit="s", reason=None if val is not None else ("not_entered_or_reference_missing"), n_obs=observed, n_interp=interped, duration=total, extra_params={**aoi_audit, "entry_definition": "first_valid_sample_interval"}))
    # Static AOI coverage uses the predeclared AOI denominator, not a post-hoc set.
    unique_ids = list(dict.fromkeys(ids))
    visited = {aid for aid in unique_ids if any(aid in labels[i] for i in range(len(labels)))}
    spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.aoi.coverage")
    metrics.append(_row(spec, config, prov_ok, ordered.iloc[0]["subject_id"], ordered.iloc[0]["session_id"], segment_id,
        len(visited)/len(unique_ids), "proportion", n_obs=observed, n_interp=interped, duration=total,
        interaction=ordered.iloc[0].get("interaction_id"), condition=seg_meta.get("condition", ordered.iloc[0].get("condition")), trial=seg_meta.get("trial", ordered.iloc[0].get("trial")), extra_params={"aoi_denominator": len(unique_ids), "aoi_geometry_version": AOI_GEOMETRY_VERSION, "aoi_domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION, "aoi_boundary_policy": AOI_BOUNDARY_POLICY, "aoi_geometry_fingerprints": sorted({str(s.get("aoi_geometry_fingerprint")) for s in shapes})}))
    # Transition counts/probabilities and Shannon entropies are kept as separate metric IDs.
    sequence: list[Any] = []
    for i, labs in enumerate(labels):
        current = next(iter(labs)) if len(labs) == 1 else None
        if current is not None and (not sequence or sequence[-1] != current):
            sequence.append(current)
    transitions: dict[tuple[Any, Any], int] = {}
    for a, b in zip(sequence, sequence[1:]):
        transitions[(a, b)] = transitions.get((a, b), 0) + 1
        events_out.append({"run_id": config.get("run_id"), "subject_id": subject, "session_id": session,
            "segment_id": segment_id, "event_id": f"aoi-transition:{a}:{b}:{len(events_out)}", "event_type": "aoi_transition",
            "start_s": np.nan, "end_s": np.nan, "duration_s": np.nan,
            "status": "complete" if prov_ok else "partial", "reason_code": None if prov_ok else "provenance_missing",
            "n_observed": 0, "n_interpolated": 0, "algorithm_version": AOI_GEOMETRY_VERSION,
            "parameter_ref": _param_ref(config, {"from_aoi": str(a), "to_aoi": str(b), "aoi_geometry_version": AOI_GEOMETRY_VERSION, "aoi_domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION, "aoi_boundary_policy": AOI_BOUNDARY_POLICY, "aoi_geometry_fingerprints": sorted({str(s.get("aoi_geometry_fingerprint")) for s in shapes})})})
    total_trans = sum(transitions.values())
    for (a, b), count in transitions.items():
        spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.aoi.transition_matrix")
        metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, count, "count", n_obs=observed, duration=total, extra_params={"from_aoi": str(a), "to_aoi": str(b), "normalization": "row", "aoi_geometry_version": AOI_GEOMETRY_VERSION, "aoi_domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION, "aoi_boundary_policy": AOI_BOUNDARY_POLICY, "aoi_geometry_fingerprints": sorted({str(s.get("aoi_geometry_fingerprint")) for s in shapes})}))
    stationary = np.array([sum(w[i] for i in range(max(0, len(w)-1)) if aid in labels[i]) for aid in unique_ids], dtype=float)
    if stationary.sum() > 0 and all(s.get("overlap_policy") != "multi_label" for s in shapes):
        p = stationary[stationary > 0] / stationary.sum()
        h = float(-np.sum(p*np.log2(p)))
        spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.aoi.stationary_entropy")
        metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, h, "bit", n_obs=observed, duration=total, extra_params={"log_base": 2, "aoi_geometry_version": AOI_GEOMETRY_VERSION, "aoi_domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION, "aoi_boundary_policy": AOI_BOUNDARY_POLICY, "aoi_geometry_fingerprints": sorted({str(s.get("aoi_geometry_fingerprint")) for s in shapes})}))
    if total_trans > 0:
        by_from: dict[Any, list[int]] = {}
        for (a,b), n in transitions.items():
            by_from.setdefault(a, []).append(n)
        row_counts = {a: sum(v) for a,v in by_from.items()}
        h = 0.0
        for a, ns in by_from.items():
            pa = row_counts[a]/total_trans
            ps = np.asarray(ns,dtype=float)/row_counts[a]
            h -= pa * float(np.sum(ps*np.log2(ps)))
        spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.aoi.transition_entropy")
        metrics.append(_row(spec, config, prov_ok, subject, session, segment_id, h, "bit", n_obs=observed, duration=total, extra_params={"definition": "weighted_conditional_entropy_bits", "aoi_geometry_version": AOI_GEOMETRY_VERSION, "aoi_domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION, "aoi_boundary_policy": AOI_BOUNDARY_POLICY, "aoi_geometry_fingerprints": sorted({str(s.get("aoi_geometry_fingerprint")) for s in shapes})}))


def _source_events_for_segment(config: dict[str, Any], frame: pd.DataFrame,
                               event_type: str, start: float, end: float) -> pd.DataFrame:
    """Return eligible full source events overlapping a label, with separate clipped bounds."""
    events = _events(config)
    required = {"event_id", "event_type", "start_s", "end_s", "status", "subject_id", "session_id"}
    if events.empty or not required.issubset(events.columns):
        return pd.DataFrame()
    eligible, _ = _event_consumer_rows(events, event_type, config)
    if eligible.empty:
        return eligible
    identity_keys = (
        "source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id",
        "analysis_stream_id", "physical_partition_id", "timebase_id", "clock_id",
    )
    identities = {
        key: frame.iloc[0].get(key)
        for key in identity_keys
        if key in frame.columns and pd.notna(frame.iloc[0].get(key))
    }
    out = query_event_overlaps(
        eligible, start, end, identities=identities,
        allow_unspecified_stream="analysis_stream_id" not in identities,
        event_types=(event_type,),
    )
    if out.empty:
        return out
    out["source_start_s"] = pd.to_numeric(out["start_s"], errors="coerce")
    out["source_end_s"] = pd.to_numeric(out["end_s"], errors="coerce")
    out["clipped_start_s"] = out["intersection_start_s"]
    out["clipped_end_s"] = out["intersection_end_s"]
    out["duration_in_window_s"] = out["intersection_duration_s"]
    return out.sort_values(["source_start_s"], kind="mergesort").reset_index(drop=True)


def _gaze_arrays(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    times = pd.to_numeric(frame["time_s"], errors="coerce").to_numpy(dtype=float)
    x = pd.to_numeric(frame.get("gaze_x"), errors="coerce").to_numpy(dtype=float) if "gaze_x" in frame else np.full(len(frame), np.nan)
    y = pd.to_numeric(frame.get("gaze_y"), errors="coerce").to_numpy(dtype=float) if "gaze_y" in frame else np.full(len(frame), np.nan)
    valid = (_flag_mask(frame, "gaze_x", "gaze_valid") & _flag_mask(frame, "gaze_y", "gaze_valid")
             & ~_interp_mask(frame, "gaze"))
    return times, x, y, valid


def _supported_gaze_segments(times: np.ndarray, x: np.ndarray, y: np.ndarray,
                             valid: np.ndarray, config: dict[str, Any],
                             start: float, end: float) -> list[tuple[float, float, float, float, float]]:
    """Create clipped valid adjacent gaze supports as (dt,x0,y0,x1,y1)."""
    if len(times) < 2 or not (math.isfinite(start) and math.isfinite(end) and end > start):
        return []
    gap_limit = _gap_limit_s(times, config)
    if gap_limit is None or gap_limit <= 0:
        return []
    result: list[tuple[float, float, float, float, float]] = []
    for i, dt in enumerate(np.diff(times)):
        if not valid[i] or not valid[i + 1] or not (0 < dt <= gap_limit):
            continue
        left, right = max(float(times[i]), start), min(float(times[i + 1]), end)
        if right <= left:
            continue
        f0 = (left - float(times[i])) / float(dt)
        f1 = (right - float(times[i])) / float(dt)
        x0, y0 = float(x[i] + f0*(x[i+1]-x[i])), float(y[i] + f0*(y[i+1]-y[i]))
        x1, y1 = float(x[i] + f1*(x[i+1]-x[i])), float(y[i] + f1*(y[i+1]-y[i]))
        result.append((right-left, x0, y0, x1, y1))
    return result


def _time_weighted_covariance(segments: list[tuple[float, float, float, float, float]]) -> tuple[float, float, float, float, float, float] | None:
    """Integrate first and second moments exactly for piecewise-linear gaze."""
    support = sum(row[0] for row in segments)
    if support <= 0:
        return None
    sx = sy = sxx = syy = sxy = 0.0
    for h, x0, y0, x1, y1 in segments:
        sx += h*(x0+x1)/2.0
        sy += h*(y0+y1)/2.0
        sxx += h*(x0*x0+x0*x1+x1*x1)/3.0
        syy += h*(y0*y0+y0*y1+y1*y1)/3.0
        sxy += h*(2*x0*y0+x0*y1+x1*y0+2*x1*y1)/6.0
    mx, my = sx/support, sy/support
    return (support, mx, my, sxx/support-mx*mx, syy/support-my*my, sxy/support-mx*my)


def _bcea_measurement(
    segments: list[tuple[float, float, float, float, float]],
    probability: float,
    window_duration_s: float,
) -> tuple[float | None, str | None, float, float, list[list[float]] | None]:
    """Return the time-weighted BCEA, its gate reason, support, coverage, and covariance."""
    moments = _time_weighted_covariance(segments)
    support_s = moments[0] if moments is not None else 0.0
    coverage = support_s / window_duration_s if window_duration_s > 0 else 0.0
    if moments is None:
        return None, "insufficient_valid_stable_fixation_support", support_s, coverage, None
    _, _mean_x, _mean_y, var_x, var_y, cov_xy = moments
    covariance = np.asarray([[var_x, cov_xy], [cov_xy, var_y]], dtype=float)
    eigenvalues = np.linalg.eigvalsh(covariance)
    determinant = float(np.linalg.det(covariance))
    covariance_summary = [[float(var_x), float(cov_xy)], [float(cov_xy), float(var_y)]]
    if (not np.isfinite(eigenvalues).all() or eigenvalues[-1] <= 0
            or eigenvalues[0] <= max(np.finfo(float).tiny, 16 * np.finfo(float).eps * eigenvalues[-1])
            or determinant <= 0):
        return None, "degenerate_stable_fixation_covariance", support_s, coverage, covariance_summary
    area = 2.0 * math.pi * (-math.log1p(-probability)) * math.sqrt(determinant)
    return float(area), None, support_s, coverage, covariance_summary


def _angular_rays(x: np.ndarray, y: np.ndarray, geometry: dict[str, Any]) -> np.ndarray:
    px_w, px_h = map(float, geometry["screen_size_px"])
    mm_w, mm_h = map(float, geometry["screen_size_mm"])
    distance = float(geometry["viewing_distance_mm"])
    physical_x = (np.asarray(x,dtype=float)-px_w/2.0)*(mm_w/px_w)
    physical_y = -(np.asarray(y,dtype=float)-px_h/2.0)*(mm_h/px_h)
    vectors = np.column_stack((physical_x,physical_y,np.full(len(physical_x),distance)))
    return vectors/np.linalg.norm(vectors,axis=1,keepdims=True)


def _angular_distance_deg(x0: float, y0: float, x1: float, y1: float,
                          geometry: dict[str, Any]) -> float:
    rays = _angular_rays(np.asarray([x0,x1]),np.asarray([y0,y1]),geometry)
    return float(np.degrees(np.arccos(np.clip(float(np.dot(rays[0],rays[1])),-1.0,1.0))))


def _mean_angular_velocity(
    times: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    valid: np.ndarray,
    config: dict[str, Any],
    start_s: float,
    end_s: float,
    geometry: dict[str, Any],
) -> tuple[float | None, float, float]:
    """Calculate one event's angular path speed over valid, gap-bounded supports."""
    supports = _supported_gaze_segments(times, x, y, valid, config, start_s, end_s)
    effective_duration_s = sum(part[0] for part in supports)
    path_length_deg = sum(
        _angular_distance_deg(part[1], part[2], part[3], part[4], geometry)
        for part in supports
    )
    value = path_length_deg / effective_duration_s if effective_duration_s > 0 else None
    return value, path_length_deg, effective_duration_s


def _event_gaze_endpoints(frame: pd.DataFrame, start: float, end: float) -> tuple[float,float,float,float,int] | None:
    times,x,y,valid = _gaze_arrays(frame)
    selected = np.flatnonzero((times>=start)&(times<end)&valid)
    if len(selected)<2:
        return None
    first,last=int(selected[0]),int(selected[-1])
    if times[last]<=times[first]:
        return None
    return float(x[first]),float(y[first]),float(x[last]),float(y[last]),int(len(selected))


def _finite_or_nan(value: Any) -> float:
    return float(value) if _finite_number(value) else math.nan


def _symmetric_substitution_matrix(parameters: dict[str, Any]) -> bool:
    matrix=parameters.get("substitution_matrix")
    if not isinstance(matrix,dict) or not matrix:
        return False
    for key,value in matrix.items():
        if not isinstance(key,str) or "|" not in key or not _finite_number(value):
            return False
        left,right=key.split("|",1)
        reverse=f"{right}|{left}"
        if reverse in matrix and not math.isclose(float(value),float(matrix[reverse]),rel_tol=0.0,abs_tol=1e-12):
            return False
    return True


def _target_movement_for_event(config: dict[str, Any], event: pd.Series) -> tuple[pd.Series | None,str | None]:
    table = config.get("target_movement_intervals")
    movement_id,target_id = event.get("target_movement_id"),event.get("target_id")
    if pd.isna(movement_id) or pd.isna(target_id):
        return None,"event_target_mapping_missing"
    if not isinstance(table,pd.DataFrame) or table.empty:
        return None,"target_reference_missing"
    candidates=table.loc[table["target_movement_id"].astype(str).eq(str(movement_id))
                         & table["target_id"].astype(str).eq(str(target_id))]
    if len(candidates)!=1:
        return None,"target_movement_mapping_not_unique"
    target=candidates.iloc[0]
    start,end=_finite_or_nan(target.get("start_s")),_finite_or_nan(target.get("end_s"))
    es,ee=_finite_or_nan(event.get("source_start_s")),_finite_or_nan(event.get("source_end_s"))
    if not(math.isfinite(start) and math.isfinite(end) and end>start and max(start,es)<min(end,ee)):
        return None,"event_target_interval_mismatch"
    return target,None


def _clip_line_to_grid(x0: float,y0: float,x1: float,y1: float,
                       xmin: float,xmax: float,ymin: float,ymax: float) -> tuple[float,float] | None:
    """Liang-Barsky clipping parameters for a line segment and closed bounds."""
    dx,dy=x1-x0,y1-y0
    u0,u1=0.0,1.0
    for p,q in ((-dx,x0-xmin),(dx,xmax-x0),(-dy,y0-ymin),(dy,ymax-y0)):
        if p==0:
            if q<0:return None
            continue
        r=q/p
        if p<0:
            if r>u1:return None
            u0=max(u0,r)
        else:
            if r<u0:return None
            u1=min(u1,r)
    return (u0,u1) if u1>u0 else None


def _heatmap_rows(frame: pd.DataFrame,config: dict[str,Any],start: float,end: float
                  ) -> tuple[list[dict[str,Any]],float,float,int]:
    grid=config["heatmap_grid"]
    cols,rows=int(grid["columns"]),int(grid["rows"])
    xmin,xmax=float(grid["xmin"]),float(grid["xmax"])
    ymin,ymax=float(grid["ymin"]),float(grid["ymax"])
    cell_w,cell_h=(xmax-xmin)/cols,(ymax-ymin)/rows
    times,x,y,valid=_gaze_arrays(frame)
    segments=_supported_gaze_segments(times,x,y,valid,config,start,end)
    total_support=sum(part[0] for part in segments)
    dwell=np.zeros((rows,cols),dtype=float)
    in_grid_support=0.0
    for h,x0,y0,x1,y1 in segments:
        clipped=_clip_line_to_grid(x0,y0,x1,y1,xmin,xmax,ymin,ymax)
        if clipped is None:continue
        u0,u1=clipped
        in_grid_support+=h*(u1-u0)
        dx,dy=x1-x0,y1-y0
        cuts=[u0,u1]
        if dx!=0:
            for col in range(1,cols):
                u=(xmin+col*cell_w-x0)/dx
                if u0<u<u1:cuts.append(u)
        if dy!=0:
            for row in range(1,rows):
                u=(ymin+row*cell_h-y0)/dy
                if u0<u<u1:cuts.append(u)
        cuts.sort()
        for left,right in zip(cuts[:-1],cuts[1:]):
            if right<=left:continue
            mid=(left+right)/2.0
            mx,my=x0+mid*dx,y0+mid*dy
            col=min(cols-1,max(0,int(math.floor((mx-xmin)/cell_w))))
            row=min(rows-1,max(0,int(math.floor((my-ymin)/cell_h))))
            dwell[row,col]+=h*(right-left)
    rows_out=[]
    for row in range(rows):
        for col in range(cols):
            rows_out.append({"row":row,"column":col,
                "xmin":xmin+col*cell_w,"xmax":xmin+(col+1)*cell_w,
                "ymin":ymin+row*cell_h,"ymax":ymin+(row+1)*cell_h,
                "dwell_s":float(dwell[row,col]),
                "proportion":float(dwell[row,col]/in_grid_support) if in_grid_support>0 else 0.0,
                "total_support_s":float(total_support),
                "in_grid_support_s":float(in_grid_support),
                "grid_coverage_proportion":float(in_grid_support/total_support) if total_support>0 else 0.0})
    return rows_out,total_support,in_grid_support,int(valid.sum())


def _append_calibration_metric(frame: pd.DataFrame,config: dict[str,Any],prov_ok: bool,
                               base: dict[str,Any],start: float,end: float,
                               metrics: list[dict[str,Any]],target_rows: list[dict[str,Any]]) -> None:
    metric_id="individual.calibration.accuracy"
    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
    window=config.get("calibration_window_s")
    if not isinstance(window,(tuple,list)) or len(window)!=2:
        metrics.append(_row(spec,config,prov_ok,**base,value=None,reason="calibration_window_missing"));return
    ws,we=max(float(window[0]),start),min(float(window[1]),end)
    if we<=ws:
        metrics.append(_row(spec,config,prov_ok,**base,value=None,reason="calibration_window_outside_analysis_segment"));return
    times,x,y,valid=_gaze_arrays(frame)
    tx=pd.to_numeric(frame["target_x"],errors="coerce").to_numpy(dtype=float)
    ty=pd.to_numeric(frame["target_y"],errors="coerce").to_numpy(dtype=float)
    ids=frame["target_id"]
    mask=valid&(times>=ws)&(times<we)&np.isfinite(tx)&np.isfinite(ty)&ids.notna().to_numpy()
    target_errors=[]
    invalid_target=False
    for target_id in pd.unique(ids[mask]):
        ix=np.flatnonzero(mask&ids.astype(str).eq(str(target_id)).to_numpy())
        if not len(ix):continue
        target_xy=np.column_stack((tx[ix],ty[ix]))
        if not np.allclose(target_xy,target_xy[0],rtol=0.0,atol=1e-9):
            invalid_target=True
            target_rows.append({**base,"target_id":target_id,"n_observed":len(ix),
                "mean_gaze_x":None,"mean_gaze_y":None,"target_x":None,"target_y":None,
                "mean_radial_error":None,"unit":_meta_value(config,"coordinate_unit"),
                "reason_code":"target_position_varies_within_target"})
            continue
        mean_x,mean_y=float(np.mean(x[ix])),float(np.mean(y[ix]))
        error=float(math.hypot(mean_x-target_xy[0,0],mean_y-target_xy[0,1]))
        target_errors.append((target_id,error,len(ix)))
        target_rows.append({**base,"target_id":target_id,"n_observed":len(ix),
            "mean_gaze_x":mean_x,"mean_gaze_y":mean_y,
            "target_x":float(target_xy[0,0]),"target_y":float(target_xy[0,1]),
            "mean_radial_error":error,"unit":_meta_value(config,"coordinate_unit"),
            "reason_code":None})
    value=float(np.mean([row[1] for row in target_errors])) if target_errors and not invalid_target else None
    reason=None if value is not None else ("target_position_varies_within_target" if invalid_target else "no_valid_calibration_targets")
    metrics.append(_row(spec,config,prov_ok,**base,value=value,
        unit=str(_meta_value(config,"coordinate_unit") or "unknown"),reason=reason,
        n_obs=sum(row[2] for row in target_errors),duration=we-ws,
        extra_params={"calibration_window_s":[float(window[0]),float(window[1])],
            "analyzed_window_s":[ws,we],"target_count":len(target_errors),
            "summary":"equal_target_mean_of_target_mean_radial_errors",
            "interpolated_samples_excluded":True}))


def analyze_individual(signals: pd.DataFrame, config: dict[str, Any], segments: pd.DataFrame | None = None,
                       aois: pd.DataFrame | None = None, cancel: Callable[[], bool] | None = None,
                       progress: Callable[[float, str], None] | None = None, *,
                       aoi_geometry_budget: AOIGeometryBudget | None = None) -> AnalysisBundle:
    """Analyze individuals; source flags are honored and absent evidence stays unavailable.

    Configuration required for a metric is explicit in `methods/catalogue.md`. At a
    minimum, `config.provenance.source_manifest` must contain source_id and SHA-256
    fields before any result can have status `complete`.

    `aoi_geometry_budget` is a transient task-local counter. A pipeline may share
    one instance with group analysis so exact AOI predicates remain bounded across
    the complete run; the budget is not added to the input config or result metadata.
    """
    if not isinstance(signals, pd.DataFrame):
        raise ContractError("signals must be a pandas DataFrame")
    required = {"subject_id", "session_id", "time_s", "time_raw", "original_index"}
    if not required.issubset(signals.columns):
        raise ContractError("signals missing required columns: " + ", ".join(sorted(required - set(signals.columns))))
    if signals["subject_id"].isna().any() or signals["session_id"].isna().any() or signals["original_index"].isna().any():
        raise ContractError("subject_id, session_id and original_index must be explicit; no IDs are inferred")
    if not isinstance(config, dict):
        raise ContractError("config must be a dictionary")
    config = dict(config)
    config["_aoi_cancel_check"] = cancel
    run_id = config.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ContractError("config.run_id must be a nonempty string")
    frame = signals.copy()
    frame["time_s"] = pd.to_numeric(frame["time_s"], errors="coerce")
    frame = frame.loc[np.isfinite(frame["time_s"].to_numpy(dtype=float))].copy()
    if "analysis_stream_id" in frame:
        requested_stream = config.get("analysis_stream_id")
        if requested_stream is not None:
            frame = frame.loc[frame["analysis_stream_id"].astype("string").eq(str(requested_stream)).fillna(False)].copy()
            if frame.empty:
                raise ContractError("requested analysis_stream_id has no signal rows")
        stream_ids = frame["analysis_stream_id"].dropna().astype(str).unique()
        if len(stream_ids) != 1 or frame["analysis_stream_id"].isna().any():
            raise ContractError("individual analysis requires one explicit analysis_stream_id per call")
    provenance_ok, provenance = _provenance(config)
    req = config.get("metric_ids")
    requested_raw = set(req) if isinstance(req, (list, tuple, set)) else {s["metric_id"] for s in INDIVIDUAL_CATALOGUE if s.get("implemented")}
    specs_by_id = {s["metric_id"]: s for s in INDIVIDUAL_CATALOGUE}
    requested_methods = {x for x in requested_raw if x in specs_by_id or x in _FAMILY_SELECTIONS}
    expanded_requested = {child for metric_id in requested_methods
                          for child in _FAMILY_SELECTIONS.get(metric_id, (metric_id,))}
    requested_all = {x for x in expanded_requested if x in specs_by_id}
    if any(metric_id.startswith("individual.aoi.") for metric_id in requested_all):
        if aoi_geometry_budget is not None and not isinstance(aoi_geometry_budget, AOIGeometryBudget):
            raise ContractError("aoi_geometry_budget must be an AOIGeometryBudget or None")
        if aoi_geometry_budget is None:
            aoi_geometry_budget = AOIGeometryBudget(cancel_check=cancel)
        elif cancel is not None and aoi_geometry_budget.cancel_check is None:
            aoi_geometry_budget.cancel_check = cancel
        config["_aoi_geometry_budget"] = aoi_geometry_budget
        config["_aoi_membership_scope_id"] = f"individual:{run_id}"
        try:
            config["_aoi_geometry_prepared"] = _prepare_aoi_geometry_for_frame(frame, aois, config)
        except AOIGeometryError as exc:
            config["_aoi_geometry_issue"] = exc.issue.as_dict()
    availability = _availability(frame, config)
    geometry_issue = config.get("_aoi_geometry_issue")
    if isinstance(geometry_issue, dict):
        aoi_mask = availability["metric_id"].astype("string").str.startswith("individual.aoi.")
        availability.loc[aoi_mask, "available"] = False
        availability.loc[aoi_mask, "status"] = AvailabilityStatus.BLOCKED.value
        availability.loc[aoi_mask, "reason_code"] = geometry_issue.get("code", "aoi_geometry_unavailable")
        availability.loc[aoi_mask, "reason"] = geometry_issue.get("message", "AOI geometry is unavailable")
    availability_by_id = {row["metric_id"]: row for row in availability.to_dict("records")}
    requested = {x for x in requested_all if availability_by_id[x]["available"]}
    event_frame = _events(config)
    metrics: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    target_audit_rows: list[dict[str, Any]] = []
    saccade_detail_rows: list[dict[str, Any]] = []
    heatmap_grid_rows: list[dict[str, Any]] = []
    scanpath_audit_rows: list[dict[str, Any]] = []
    status = "complete"
    identity_columns = ["subject_id", "session_id"] + [
        column for column in ("interaction_id", "trial", "analysis_stream_id", "timebase_id", "clock_id", "source_id", "physical_partition_id")
        if column in frame
    ]
    groups = list(frame.groupby(identity_columns, sort=False, dropna=False))
    total_groups = max(1, len(groups))
    for gi, (group_identity, raw_group) in enumerate(groups):
        identity_values = group_identity if isinstance(group_identity, tuple) else (group_identity,)
        group_ids = dict(zip(identity_columns, identity_values))
        subject, session = group_ids["subject_id"], group_ids["session_id"]
        if cancel and cancel():
            status = "cancelled"
            break
        group = _ordered(raw_group)
        segs = _segments_for_group(group, segments, config.get("overlap_policy"), scope_frame=frame)
        if not segs:
            # No explicit overlap means no invented segment; feature rows are omitted.
            continue
        for segment_id, subframe, seg_meta in segs:
            if subframe.empty:
                continue
            support_frame = subframe.attrs.get("_window_support_frame", subframe)
            subframe = _ordered(subframe)
            support_frame = _ordered(support_frame)
            subframe.attrs["_window_support_frame"] = support_frame
            local_start = float(seg_meta.get("start_s", subframe["time_s"].min()))
            local_end = float(seg_meta.get("end_s", subframe["time_s"].max()))
            scope_fields = _scope_metric_fields(subframe, seg_meta, local_start, local_end)
            base = {"subject": subject, "session": session, "segment": segment_id,
                    "interaction": seg_meta.get("interaction_id", subframe.iloc[0].get("interaction_id")),
                    "condition": seg_meta.get("condition", subframe.iloc[0].get("condition")),
                    "trial": seg_meta.get("trial", subframe.iloc[0].get("trial")),
                    **scope_fields}
            t = pd.to_numeric(subframe["time_s"], errors="coerce").to_numpy(dtype=float)
            start = local_start
            end = local_end
            support_t = pd.to_numeric(support_frame["time_s"], errors="coerce").to_numpy(dtype=float)
            for sig, valcol, validcol in (("gaze", "gaze_x", "gaze_valid"), ("pupil_left", "pupil_left", "pupil_left_valid"), ("pupil_right", "pupil_right", "pupil_right_valid")):
                if sig != "gaze" and valcol not in subframe:
                    continue
                if sig == "gaze":
                    valid = _flag_mask(subframe, "gaze_x", "gaze_valid") & np.isfinite(pd.to_numeric(subframe.get("gaze_y"), errors="coerce").to_numpy(dtype=float)) if "gaze_y" in subframe else np.zeros(len(subframe), dtype=bool)
                else:
                    valid = _flag_mask(subframe, valcol, validcol)
                valid &= ~_interp_mask(subframe, sig)
                if sig == "gaze":
                    support_valid = (_flag_mask(support_frame, "gaze_x", "gaze_valid")
                                     & np.isfinite(pd.to_numeric(support_frame.get("gaze_y"), errors="coerce").to_numpy(dtype=float))
                                     if "gaze_y" in support_frame else np.zeros(len(support_frame), dtype=bool))
                else:
                    support_valid = _flag_mask(support_frame, valcol, validcol)
                support_valid &= ~_interp_mask(support_frame, sig)
                w, interval_ok, effective = _interval_weights(support_frame, support_valid, config, start, end)
                n = int(valid.sum())
                interp_n = int(_interp_mask(subframe, sig).sum())
                support_n = _window_support_observation_count(support_frame, support_valid, config, start, end)
                spec_ids = {"gaze": ("individual.coverage.gaze",), "pupil_left": ("individual.coverage.pupil_left", "individual.pupil_left.mean", "individual.pupil_left.median", "individual.pupil_left.sd", "individual.pupil_left.iqr"), "pupil_right": ("individual.coverage.pupil_right", "individual.pupil_right.mean", "individual.pupil_right.median", "individual.pupil_right.sd", "individual.pupil_right.iqr")}[sig]
                if sig == "gaze":
                    values = [("individual.coverage.gaze", float(effective / max(0.0, end-start)) if end > start else None, "proportion", n)]
                else:
                    signal_values = pd.to_numeric(subframe[valcol], errors="coerce").to_numpy(dtype=float)
                    support_values = pd.to_numeric(support_frame[valcol], errors="coerce").to_numpy(dtype=float)
                    pupil_mean, _ = _time_weighted_mean_values(support_t, support_values, support_valid, config, start, end)
                    x = signal_values[valid]
                    qs = np.quantile(x, [0.25, 0.5, 0.75]) if len(x) else [np.nan]*3
                    values = [(spec_ids[0], float(effective/max(0.0,end-start)) if end>start else None, "proportion", n),
                              (spec_ids[1], pupil_mean, str(_meta_value(config,"pupil_unit") or "unknown"), n),
                              (spec_ids[2], float(np.median(x)) if len(x) else None, str(_meta_value(config,"pupil_unit") or "unknown"), n),
                              (spec_ids[3], float(np.std(x, ddof=1)) if len(x)>1 else None, str(_meta_value(config,"pupil_unit") or "unknown"), n),
                              (spec_ids[4], float(qs[2]-qs[0]) if len(x) else None, str(_meta_value(config,"pupil_unit") or "unknown"), n)]
                for metric_id, value, unit, count in values:
                    spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == metric_id)
                    fields_ok, fields_reason = _field_condition(subframe, spec["requires_fields"])
                    meta_ok, meta_reason = _required_metadata(spec, config)
                    value_allowed = fields_ok and meta_ok
                    metrics.append(_row(spec, config, provenance_ok, **base, value=value if value_allowed else None, unit=unit,
                        reason=None if value_allowed else (fields_reason or meta_reason), n_obs=count, n_interp=interp_n, duration=effective,
                        extra_params={"validity_column": ("analysis_gaze_valid" if sig == "gaze" and "analysis_gaze_valid" in subframe else "analysis_pupil_left_valid" if sig == "pupil_left" and "analysis_pupil_left_valid" in subframe else "analysis_pupil_right_valid" if sig == "pupil_right" and "analysis_pupil_right_valid" in subframe else validcol), "interpolated_samples_excluded": True, "mean_rule": "trapezoid over valid adjacent support" if metric_id in {"individual.pupil_left.mean", "individual.pupil_right.mean"} else None, "max_gap_s": config.get("max_gap_s", "1.5*median_dt"), "n_window_observed": n, "n_support_observed": support_n}, n_window_observed=n, n_support_observed=support_n))
                if sig == "gaze" and "individual.data_loss.gaps" in requested:
                    spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.data_loss.gaps")
                    dt = np.diff(t)
                    positive = dt[dt > 0]
                    threshold = float(config.get("max_gap_s", 1.5*np.median(positive))) if positive.size else np.nan
                    gaps = dt[dt > threshold] if np.isfinite(threshold) else np.array([])
                    valid_seconds = effective
                    duration_total = max(0.0, end-start)
                    loss = 1.0-valid_seconds/duration_total if duration_total > 0 else None
                    metrics.append(_row(spec, config, provenance_ok, **base, value=loss, unit="proportion", reason=None if duration_total > 0 else "zero_duration", n_obs=n, n_interp=interp_n, duration=effective,
                        extra_params={"gap_threshold_s": threshold, "gap_count": len(gaps), "max_gap_s": float(np.max(gaps)) if len(gaps) else 0.0, "median_gap_s": float(np.median(gaps)) if len(gaps) else 0.0}))
                if sig == "gaze" and "individual.usable_duration" in requested:
                    spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.usable_duration")
                    metrics.append(_row(spec, config, provenance_ok, **base, value=effective if _required_metadata(spec,config)[0] else None, unit="s", reason=None if _required_metadata(spec,config)[0] else "time_not_confirmed", n_obs=n, n_interp=interp_n, duration=effective))
            # Sampling diagnostics use observed positive time deltas only.
            if "individual.sampling.regularity" in requested:
                dt = np.diff(t)
                dt = dt[np.isfinite(dt) & (dt > 0)]
                spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"] == "individual.sampling.regularity")
                med = float(np.median(dt)) if len(dt) else None
                meta_ok = _required_metadata(spec,config)[0]
                value = 1.0/med if med and med > 0 and meta_ok else None
                metrics.append(_row(spec, config, provenance_ok, **base, value=value, unit="Hz", reason=None if value is not None else "insufficient_or_unconfirmed_time", n_obs=len(dt), duration=end-start,
                    extra_params={"median_dt_s": med, "dt_iqr_s": float(np.quantile(dt,.75)-np.quantile(dt,.25)) if len(dt)>1 else None, "dt_min_s": float(np.min(dt)) if len(dt) else None, "dt_max_s": float(np.max(dt)) if len(dt) else None}))
            # Stable-target precision is signal spread, not calibration accuracy.
            if "individual.signal.precision" in requested and config.get("stable_target_confirmed") is True:
                mask = _flag_mask(subframe,"gaze_x","gaze_valid") & _flag_mask(subframe,"gaze_y","gaze_valid") & ~_interp_mask(subframe,"gaze")
                x = pd.to_numeric(subframe["gaze_x"],errors="coerce").to_numpy(dtype=float)[mask]
                y = pd.to_numeric(subframe["gaze_y"],errors="coerce").to_numpy(dtype=float)[mask]
                spec = next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.signal.precision")
                if len(x)>1 and _required_metadata(spec,config)[0]:
                    value=float(np.sqrt(np.mean((x-x.mean())**2+(y-y.mean())**2)))
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value,unit=str(_meta_value(config,"coordinate_unit")),n_obs=len(x),n_interp=int(_interp_mask(subframe,"gaze").sum()),duration=end-start,extra_params={"definition":"2d_rms_about_mean","stable_target_confirmed":True}))
            # Interocular summaries are paired row-by-row before aggregation;
            # distribution summaries use the empirical paired sample values.
            interocular_ids=("individual.pupil.interocular_difference",
                             "individual.pupil.interocular_difference.median",
                             "individual.pupil.interocular_difference.sd",
                             "individual.pupil.interocular_difference.iqr")
            if requested.intersection(interocular_ids):
                lm = _flag_mask(subframe,"pupil_left","pupil_left_valid") & ~_interp_mask(subframe,"pupil_left")
                rm = _flag_mask(subframe,"pupil_right","pupil_right_valid") & ~_interp_mask(subframe,"pupil_right")
                lm &= rm
                left = pd.to_numeric(subframe.get("pupil_left"),errors="coerce").to_numpy(dtype=float)
                right = pd.to_numeric(subframe.get("pupil_right"),errors="coerce").to_numpy(dtype=float)
                difference = left-right
                support_left = _flag_mask(support_frame,"pupil_left","pupil_left_valid") & ~_interp_mask(support_frame,"pupil_left")
                support_right = _flag_mask(support_frame,"pupil_right","pupil_right_valid") & ~_interp_mask(support_frame,"pupil_right")
                support_pair = support_left & support_right
                support_difference = (pd.to_numeric(support_frame.get("pupil_left"),errors="coerce").to_numpy(dtype=float)
                                      - pd.to_numeric(support_frame.get("pupil_right"),errors="coerce").to_numpy(dtype=float))
                difference_mean, difference_duration = _time_weighted_mean_values(support_t, support_difference, support_pair, config, start, end)
                difference_support_n = _window_support_observation_count(support_frame,support_pair,config,start,end)
                vals = difference[lm]
                q25,median,q75=np.quantile(vals,[.25,.5,.75]) if len(vals) else (np.nan,np.nan,np.nan)
                summaries={
                    "individual.pupil.interocular_difference":(difference_mean,"time_weighted_mean_left_minus_right",len(vals)>0),
                    "individual.pupil.interocular_difference.median":(float(median) if len(vals) else None,"paired_sample_median_left_minus_right",len(vals)>0),
                    "individual.pupil.interocular_difference.sd":(float(np.std(vals,ddof=1)) if len(vals)>1 else None,"paired_sample_sd_left_minus_right",len(vals)>1),
                    "individual.pupil.interocular_difference.iqr":(float(q75-q25) if len(vals) else None,"paired_sample_iqr_left_minus_right",len(vals)>0),
                }
                pupil_unit=str(_meta_value(config,"pupil_unit") or "unknown")
                for metric_id in interocular_ids:
                    if metric_id not in requested: continue
                    value,summary,has_support=summaries[metric_id]
                    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                    meta_ok,_meta_reason=_required_metadata(spec,config)
                    valid=has_support and value is not None and meta_ok
                    if valid:
                        reason=None
                    elif not meta_ok:
                        reason=_meta_reason or "pupil_unit_or_time_not_confirmed"
                    elif metric_id.endswith(".sd") and not has_support:
                        reason="insufficient_paired_valid_samples"
                    else:
                        reason="no_paired_valid_pupil_time_support" if metric_id=="individual.pupil.interocular_difference" else "no_paired_valid_samples"
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value if valid else None,unit=pupil_unit,
                        reason=reason,n_obs=len(vals),duration=difference_duration,
                        extra_params={"summary":summary,"paired_same_rows":True,
                                      "distribution_weighting":"unweighted paired source samples" if metric_id.endswith((".median",".sd",".iqr")) else None,
                                      "mean_weighting":"trapezoid over valid adjacent paired support" if metric_id=="individual.pupil.interocular_difference" else None,
                                      "validity_rule":"both eye analysis-valid flags; interpolated samples excluded",
                                      "n_window_observed":len(vals),"n_support_observed":difference_support_n},
                        n_window_observed=len(vals),n_support_observed=difference_support_n,**scope_fields))
            # Baseline correction and response-window metrics.
            for eye, col, validcol in (("left","pupil_left","pupil_left_valid"),("right","pupil_right","pupil_right_valid")):
                ids={"baseline":f"individual.pupil.baseline_{eye}","relative":f"individual.pupil.relative_{eye}","peak":f"individual.pupil.peak_{eye}","auc":f"individual.pupil.auc_{eye}","slope":f"individual.pupil.slope_{eye}"}
                mask=_flag_mask(subframe,col,validcol)&~_interp_mask(subframe,col)
                ts=pd.to_numeric(subframe["time_s"],errors="coerce").to_numpy(dtype=float)
                vals=pd.to_numeric(subframe[col],errors="coerce").to_numpy(dtype=float) if col in subframe else np.full(len(subframe),np.nan)
                bw=config.get("baseline_window_s")
                rw=config.get("response_window_s")
                if isinstance(bw,(list,tuple)) and len(bw)==2:
                    support_mask=_flag_mask(support_frame,col,validcol)&~_interp_mask(support_frame,col)
                    support_vals=pd.to_numeric(support_frame[col],errors="coerce").to_numpy(dtype=float) if col in support_frame else np.full(len(support_frame),np.nan)
                    base_mask=support_mask&(support_t>=float(bw[0]))&(support_t<float(bw[1]))
                    response_mask=mask&(ts>=start)&(ts<end)
                    base_val,base_duration=_time_weighted_mean_values(support_t,support_vals,support_mask,config,float(bw[0]),float(bw[1]))
                    response_mean,response_duration=_time_weighted_mean_values(support_t,support_vals,support_mask,config,start,end)
                    baseline_support_n=_window_support_observation_count(support_frame,support_mask,config,float(bw[0]),float(bw[1]))
                    response_support_n=_window_support_observation_count(support_frame,support_mask,config,start,end)
                    for key in ("baseline","relative"):
                        spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==ids[key])
                        if base_val is None or response_mean is None:
                            reason="no_valid_baseline_time_support" if base_val is None else "no_valid_response_time_support"
                            metrics.append(_row(spec,config,provenance_ok,**base,value=None,reason=reason,n_obs=int(base_mask.sum()),duration=base_duration if base_val is None else response_duration,n_window_observed=int(base_mask.sum()) if base_val is None else int(response_mask.sum()),n_support_observed=baseline_support_n if base_val is None else response_support_n))
                        else:
                            if key=="baseline": v=float(response_mean-base_val); unit=str(_meta_value(config,"pupil_unit") or "unknown")
                            else: v=float(100*(response_mean-base_val)/base_val) if base_val!=0 else None; unit="percent"
                            metrics.append(_row(spec,config,provenance_ok,**base,value=v,unit=unit,reason=None if v is not None else "zero_baseline",n_obs=int(response_mask.sum()),duration=response_duration,extra_params={"baseline_window_s":list(bw),"baseline_mean":base_val,"baseline_duration_s":base_duration,"response_mean":response_mean,"weighting":"trapezoid over valid adjacent support","baseline_n_window_observed":int(base_mask.sum()),"baseline_n_support_observed":baseline_support_n,"response_n_window_observed":int(response_mask.sum()),"response_n_support_observed":response_support_n},n_window_observed=int(response_mask.sum()),n_support_observed=response_support_n))
                if isinstance(rw,(list,tuple)) and len(rw)==2:
                    rm=mask&(ts>=float(rw[0]))&(ts<=float(rw[1]))
                    spec_peak=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==ids["peak"])
                    if rm.any():
                        ix=np.flatnonzero(rm)[int(np.argmax(vals[rm]))]
                        ref=config.get("reference_event_s")
                        metrics.append(_row(spec_peak,config,provenance_ok,**base,value=float(vals[ix]),unit=str(_meta_value(config,"pupil_unit") or "unknown"),n_obs=int(rm.sum()),duration=end-start,extra_params={"peak_time_s":float(ts[ix]),"latency_s":float(ts[ix]-ref) if _finite_number(ref) else None,"peak_rule":"maximum"}))
                        latency_id=f"individual.pupil.peak_latency_{eye}"
                        spec_latency=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==latency_id)
                        latency_value=float(ts[ix]-ref) if _finite_number(ref) and ts[ix]>=float(ref) else None
                        metrics.append(_row(spec_latency,config,provenance_ok,**base,value=latency_value,unit="s",reason=None if latency_value is not None else "reference_event_missing_or_after_peak",n_obs=int(rm.sum()),duration=end-start,extra_params={"peak_time_s":float(ts[ix]),"peak_rule":"maximum"}))
                        spec_auc=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==ids["auc"])
                        order=np.argsort(ts[rm]); tx=ts[rm][order]; vx=vals[rm][order]
                        # Integrate only adjacent valid observations without crossing long gaps.
                        dtx=np.diff(tx); med=float(np.median(np.diff(ts)[np.diff(ts)>0])) if np.any(np.diff(ts)>0) else 0
                        lim=float(config.get("max_gap_s",1.5*med)) if med>0 else 0
                        keep=(dtx>0)&(dtx<=lim)
                        auc=float(np.sum((vx[:-1][keep]+vx[1:][keep])*.5*dtx[keep])) if keep.any() else None
                        metrics.append(_row(spec_auc,config,provenance_ok,**base,value=auc,unit=f"{_meta_value(config,'pupil_unit')}*s",reason=None if auc is not None else "no_contiguous_response_intervals",n_obs=int(rm.sum()),duration=float(dtx[keep].sum()) if keep.any() else 0))
                        spec_slope=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==ids["slope"])
                        slope=float(np.polyfit(tx,vx,1)[0]) if rm.sum()>=2 and np.ptp(tx)>0 else None
                        metrics.append(_row(spec_slope,config,provenance_ok,**base,value=slope,unit=f"{_meta_value(config,'pupil_unit')}/s",reason=None if slope is not None else "insufficient_response_points",n_obs=int(rm.sum()),duration=float(np.ptp(tx)) if rm.sum()>1 else 0))
                    else:
                        for key in ("peak","peak_latency","auc","slope"):
                            metric_id=ids["peak"] if key=="peak_latency" else ids[key]
                            spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==(f"individual.pupil.peak_latency_{eye}" if key=="peak_latency" else metric_id))
                            metrics.append(_row(spec,config,provenance_ok,**base,value=None,reason="no_valid_response_samples",n_obs=0,duration=0))
            _simple_event_metrics(subframe,event_frame,config,provenance_ok,segment_id,seg_meta,metrics)
            if "individual.calibration.accuracy" in requested:
                _append_calibration_metric(subframe,config,provenance_ok,base,start,end,metrics,target_audit_rows)
            if "individual.fixation.stability" in requested:
                spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.fixation.stability")
                window=config.get("stable_fixation_window_s")
                if not isinstance(window,(list,tuple)) or len(window)!=2:
                    metrics.append(_row(spec,config,provenance_ok,**base,value=None,reason="stable_fixation_window_missing"))
                else:
                    ws,we=max(float(window[0]),start),min(float(window[1]),end)
                    times,x,y,valid=_gaze_arrays(subframe)
                    sample_mask=valid&(times>=ws)&(times<we) if we>ws else np.zeros(len(times),dtype=bool)
                    support_times,support_x,support_y,support_valid=_gaze_arrays(support_frame)
                    supports=_supported_gaze_segments(support_times,support_x,support_y,support_valid,config,ws,we) if we>ws else []
                    moments=_time_weighted_covariance(supports)
                    reason=None
                    value=None
                    covariance_summary=None
                    support_s=moments[0] if moments is not None else 0.0
                    if we<=ws:
                        reason="stable_window_outside_analysis_segment"
                    elif int(sample_mask.sum())<3 or moments is None:
                        reason="insufficient_valid_stable_fixation_support"
                    else:
                        probability=float(_meta_value(config,"bcea_probability"))
                        value,reason,support_s,coverage,covariance_summary=_bcea_measurement(
                            supports,probability,we-ws)
                    if we<=ws:
                        coverage=0.0
                    elif moments is None:
                        coverage=support_s/(we-ws)
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value,unit=f"{_meta_value(config,'coordinate_unit')}²",
                        reason=reason,n_obs=int(sample_mask.sum()),n_interp=int(_interp_mask(subframe,"gaze").sum()),duration=support_s,
                        extra_params={"stable_fixation_window_s":[float(window[0]),float(window[1])],
                            "analyzed_window_s":[ws,we],"bcea_probability":_meta_value(config,"bcea_probability"),
                            "coverage_proportion":coverage,"support_s":support_s,"covariance_xy":covariance_summary,
                            "covariance_weighting":"exact time integral over adjacent piecewise-linear valid observations",
                            "interpolated_samples_excluded":True,
                            "n_window_observed":int(sample_mask.sum()),"n_support_observed":_window_support_observation_count(support_frame,support_valid,config,ws,we)},
                        n_window_observed=int(sample_mask.sum()),n_support_observed=_window_support_observation_count(support_frame,support_valid,config,ws,we)))
            if "individual.heatmap" in requested:
                spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.heatmap")
                cells,total_support,in_grid_support,nobs=_heatmap_rows(support_frame,config,start,end)
                heatmap_grid_rows.extend({"run_id":config.get("run_id"),"subject_id":subject,"session_id":session,
                    "interaction_id":base["interaction"],"segment_id":segment_id,"trial":base["trial"],
                    "condition":base["condition"],"metric_id":"individual.heatmap",**cell} for cell in cells)
                reason=None if in_grid_support>0 else "no_valid_in_grid_path_support"
                metrics.append(_row(spec,config,provenance_ok,**base,value=in_grid_support if in_grid_support>0 else None,
                    unit="s",reason=reason,n_obs=nobs,duration=total_support,
                    extra_params={"grid":config["heatmap_grid"],"total_valid_path_support_s":total_support,
                        "in_grid_support_s":in_grid_support,
                        "grid_coverage_proportion":in_grid_support/total_support if total_support>0 else 0.0,
                        "traversal":"time-weighted linear segment split at every grid boundary",
                        "interpolated_samples_excluded":True,"no_gap_bridging":True,
                        "n_window_observed":nobs,"n_support_observed":_window_support_observation_count(support_frame,_gaze_arrays(support_frame)[3],config,start,end)},
                    n_window_observed=nobs,n_support_observed=_window_support_observation_count(support_frame,_gaze_arrays(support_frame)[3],config,start,end)))
            saccade_metric_ids={"individual.saccade.mean_velocity","individual.saccade.gain",
                                "individual.saccade.endpoint_error","individual.saccade.regression_rate"}
            saccade_requested=requested&saccade_metric_ids
            if saccade_requested:
                saccade_events=_source_events_for_segment(config,subframe,"saccade",start,end)
                saccade_values={mid:[] for mid in saccade_requested}
                saccade_reasons={mid:[] for mid in saccade_requested}
                regression_denominator=0
                regression_numerator=0
                regression_missing_assignment=False
                times,x,y,valid=_gaze_arrays(support_frame)
                geometry=config.get("angle_geometry")
                if not isinstance(geometry,dict):
                    geometry=_nested_meta(config).get("angle_geometry",{})
                minimum=float(config.get("reading_min_amplitude",0) or 0)
                expected_sign=1.0 if config.get("reading_direction")=="ltr" else -1.0
                for _,event in saccade_events.iterrows():
                    ev_start=float(event["clipped_start_s"])
                    ev_end=float(event["clipped_end_s"])
                    event_base={"run_id":config.get("run_id"),"subject_id":subject,"session_id":session,
                        "interaction_id":base["interaction"],"segment_id":segment_id,"trial":base["trial"],
                        "condition":base["condition"],"event_id":event.get("event_id"),
                        "source_event_start_s":float(event["source_start_s"]),
                        "source_event_end_s":float(event["source_end_s"]),
                        "analysis_event_start_s":ev_start,"analysis_event_end_s":ev_end,
                        "target_id":event.get("target_id"),"target_movement_id":event.get("target_movement_id")}
                    raw_event=event.to_dict()
                    if "individual.saccade.mean_velocity" in saccade_requested:
                        value,angular_path,supported_s=_mean_angular_velocity(
                            times,x,y,valid,config,ev_start,ev_end,geometry)
                        reason=None if value is not None else "no_valid_saccade_path_support"
                        event_window_n=int(((times>=ev_start)&(times<ev_end)&valid).sum())
                        event_support_n=_window_support_observation_count(support_frame,valid,config,ev_start,ev_end)
                        saccade_values["individual.saccade.mean_velocity"].append((value,event_window_n,supported_s,reason))
                        saccade_detail_rows.append({**event_base,**raw_event,"metric_id":"individual.saccade.mean_velocity",
                            "value":value,"unit":"deg/s","calculation_reason_code":reason,
                            "n_observed":event_window_n,"n_window_observed":event_window_n,"n_support_observed":event_support_n,
                            "path_length_deg":angular_path,"effective_duration_s":supported_s,
                            "interpolated_samples_excluded":True,"gap_limit_s":_gap_limit_s(times,config)})
                    if {"individual.saccade.gain","individual.saccade.endpoint_error"}&saccade_requested:
                        target,target_reason=_target_movement_for_event(config,event)
                        endpoints=_event_gaze_endpoints(subframe,ev_start,ev_end)
                        target_reason=target_reason or (None if endpoints is not None else "no_valid_saccade_endpoint")
                        gaze_dx=gaze_dy=gain=endpoint_error=None
                        target_dx=target_dy=None
                        if target is not None and endpoints is not None:
                            gx0,gy0,gx1,gy1,endpoint_n=endpoints
                            gaze_dx,gaze_dy=gx1-gx0,gy1-gy0
                            target_dx=float(target["target_end_x"])-float(target["target_start_x"])
                            target_dy=float(target["target_end_y"])-float(target["target_start_y"])
                            target_norm2=target_dx*target_dx+target_dy*target_dy
                            endpoint_error=math.hypot(gx1-float(target["target_end_x"]),gy1-float(target["target_end_y"]))
                            if target_norm2>0:
                                gain=(gaze_dx*target_dx+gaze_dy*target_dy)/target_norm2
                            else:
                                target_reason="zero_target_displacement"
                        else:
                            endpoint_n=0
                        for metric_id,value,unit in (
                            ("individual.saccade.gain",gain,"ratio"),
                            ("individual.saccade.endpoint_error",endpoint_error,str(_meta_value(config,"coordinate_unit") or "unknown"))
                        ):
                            if metric_id not in saccade_requested:continue
                            reason=target_reason if metric_id=="individual.saccade.gain" else (None if endpoint_error is not None else (target_reason or "no_valid_saccade_endpoint"))
                            saccade_values[metric_id].append((value,endpoint_n if endpoints is not None else 0,ev_end-ev_start,reason))
                            saccade_detail_rows.append({**event_base,**raw_event,"metric_id":metric_id,"value":value,"unit":unit,
                                "calculation_reason_code":reason,"gaze_dx":gaze_dx,"gaze_dy":gaze_dy,
                                "target_dx":target_dx,"target_dy":target_dy,"endpoint_error":endpoint_error,
                                "target_reference_sha256":_meta_value(config,"target_reference_sha256"),
                                "target_reference_unit":target.get("unit") if target is not None else None,
                                "target_reference_coordinate_frame":target.get("coordinate_frame") if target is not None else None,
                                "n_observed":endpoint_n if endpoints is not None else 0,"effective_duration_s":ev_end-ev_start})
                    if "individual.saccade.regression_rate" in saccade_requested:
                        scope_id=event.get("reading_scope_id")
                        line_start=event.get("start_line_id",event.get("line_id"))
                        line_end=event.get("end_line_id",event.get("line_id"))
                        role=str(event.get("event_role","")).strip().lower().replace("-","_").replace(" ","_")
                        is_return=role in {"return","return_sweep","line_transition","return_line_sweep"}
                        has_assignment=(pd.notna(scope_id) and str(scope_id)==str(config.get("reading_scope_id"))
                                        and pd.notna(line_start) and pd.notna(line_end))
                        endpoints=_event_gaze_endpoints(subframe,ev_start,ev_end) if has_assignment else None
                        reason=None
                        within_line=bool(has_assignment and str(line_start)==str(line_end) and not is_return)
                        amplitude=dx=None
                        included=False
                        is_regression=False
                        if not has_assignment:
                            reason="missing_or_mismatched_reading_scope_or_line_assignment"
                            regression_missing_assignment=True
                        elif not within_line:
                            reason="excluded_return_sweep_or_cross_line_event"
                        elif endpoints is None:
                            reason="no_valid_saccade_endpoint"
                        else:
                            gx0,gy0,gx1,gy1,endpoint_n=endpoints
                            dx=gx1-gx0
                            amplitude=math.hypot(dx,gy1-gy0)
                            included=amplitude>=minimum
                            is_regression=bool(included and expected_sign*dx<0)
                            if included:regression_denominator+=1
                            if is_regression:regression_numerator+=1
                            if not included:reason="below_minimum_saccade_amplitude"
                        saccade_detail_rows.append({**event_base,**raw_event,"metric_id":"individual.saccade.regression_rate",
                            "reading_scope_id":config.get("reading_scope_id"),"line_start_id":line_start,
                            "line_end_id":line_end,"event_role":event.get("event_role"),
                            "amplitude":amplitude,"horizontal_displacement":dx,
                            "minimum_amplitude":minimum,"included_in_denominator":included,
                            "is_regression":is_regression,"is_return_sweep":is_return,
                            "calculation_reason_code":reason,"n_observed":endpoints[4] if endpoints else 0,
                            "effective_duration_s":ev_end-ev_start})
                        if reason=="missing_or_mismatched_reading_scope_or_line_assignment":
                            saccade_reasons["individual.saccade.regression_rate"].append(reason)
                for metric_id in saccade_requested:
                    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                    if metric_id=="individual.saccade.regression_rate":
                        value=regression_numerator/regression_denominator if regression_denominator>0 else None
                        reason=("some_events_unclassified" if regression_missing_assignment and value is not None
                                else (None if value is not None else "zero_regression_denominator"))
                        nobs=regression_denominator
                        duration=end-start
                        unit="proportion"
                        extra={"reading_direction":config.get("reading_direction"),"reading_scope_id":config.get("reading_scope_id"),
                            "minimum_amplitude":minimum,"denominator_within_line_events":regression_denominator,
                            "regression_events":regression_numerator,"return_sweeps_excluded":True,
                            "reading_axis":"horizontal gaze displacement in configured coordinate unit"}
                    else:
                        values=saccade_values[metric_id]
                        supported=[row for row in values if row[0] is not None and _finite_number(row[0])]
                        value=float(np.mean([row[0] for row in supported])) if supported else None
                        has_missing=any(row[3] is not None for row in values)
                        reason=("some_saccade_events_unavailable" if has_missing and value is not None
                                else (None if value is not None else (values[0][3] if values and values[0][3] else "no_reliable_saccade_events_in_segment")))
                        nobs=sum(row[1] for row in supported)
                        duration=sum(row[2] for row in supported)
                        unit=("deg/s" if metric_id=="individual.saccade.mean_velocity" else
                              ("ratio" if metric_id=="individual.saccade.gain" else str(_meta_value(config,"coordinate_unit") or "unknown")))
                        extra={"aggregation":"unweighted mean across qualifying reliable source events",
                            "event_count":len(values),"computed_event_count":len(supported),
                            "event_values":[row[0] for row in supported]}
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value,unit=unit,reason=reason,
                        n_obs=nobs,duration=duration,extra_params=extra))
            if segment_id is None and not event_frame.empty:
                pass
            if aois is not None:
                _compute_aoi(subframe,aois,config,provenance_ok,segment_id,seg_meta,metrics,event_rows)
            mm_requested=requested.intersection(_MULTIMATCH_CHILDREN)
            if mm_requested:
                fixation_events=_source_events_for_segment(config,subframe,"fixation",start,end)
                reference=config["reference_fixations"].sort_values("fixation_index",kind="mergesort")
                fail_reason=None
                dimensions=None
                if len(fixation_events)<3 or len(reference)<3:
                    fail_reason="insufficient_fixations_for_multimatch"
                else:
                    observed_x=pd.to_numeric(fixation_events.get("start_x"),errors="coerce").to_numpy(dtype=float)
                    observed_y=pd.to_numeric(fixation_events.get("start_y"),errors="coerce").to_numpy(dtype=float)
                    observed_duration=pd.to_numeric(fixation_events["duration_s"],errors="coerce").to_numpy(dtype=float)
                    reference_x=pd.to_numeric(reference["start_x"],errors="coerce").to_numpy(dtype=float)
                    reference_y=pd.to_numeric(reference["start_y"],errors="coerce").to_numpy(dtype=float)
                    reference_duration=pd.to_numeric(reference["duration_s"],errors="coerce").to_numpy(dtype=float)
                    if not (np.isfinite(observed_x).all() and np.isfinite(observed_y).all()
                            and np.isfinite(observed_duration).all() and np.isfinite(reference_x).all()
                            and np.isfinite(reference_y).all() and np.isfinite(reference_duration).all()
                            and (observed_duration>0).all() and (reference_duration>0).all()):
                        fail_reason="invalid_multimatch_fixation_trajectory"
                    elif max(len(fixation_events),len(reference))>10_000 or len(fixation_events)*len(reference)>1_000_000:
                        fail_reason="resource_limit_exceeded"
                    elif cancel and cancel():
                        status="cancelled"
                        fail_reason="cancelled"
                    else:
                        try:
                            observed=fixation_vectors(observed_x,observed_y,observed_duration)
                            expected=fixation_vectors(reference_x,reference_y,reference_duration)
                            mm_params=config.get("multimatch",{}) if isinstance(config.get("multimatch"),dict) else {}
                            dimensions=multimatch_dimensions(observed,expected,config["screen_size_px"],mm_params)
                            if dimensions.shape!=(5,) or not np.isfinite(dimensions).all():
                                dimensions=None
                                fail_reason="multimatch_returned_invalid_dimensions"
                        except Exception:
                            fail_reason="multimatch_execution_failed"
                for index,dimension in enumerate(MULTIMATCH_DIMENSIONS):
                    metric_id=_MULTIMATCH_CHILDREN[index]
                    if metric_id not in mm_requested:
                        continue
                    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                    value=float(dimensions[index]) if dimensions is not None else None
                    reason="experimental_method" if value is not None else fail_reason
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value,unit="similarity",
                        reason=reason,n_obs=min(len(fixation_events),len(reference)),duration=end-start,
                        extra_params={"dimension":dimension,"package":"multimatch-gaze","version":"0.1.3",
                            "reference_trajectory_id":config.get("reference_trajectory_id"),
                            "reference_trajectory_sha256":_meta_value(config,"reference_trajectory_sha256"),
                            "coordinate_unit":"px","coordinate_frame":_meta_value(config,"coordinate_frame"),
                            "screen_size_px":config.get("screen_size_px"),
                            "observed_fixation_count":len(fixation_events),"reference_fixation_count":len(reference),
                            "grouping":bool((config.get("multimatch") or {}).get("grouping",False)) if isinstance(config.get("multimatch"),dict) else False}))
                    scanpath_audit_rows.append({"run_id":config.get("run_id"),"subject_id":subject,"session_id":session,
                        "segment_id":segment_id,"metric_id":metric_id,"dimension":dimension,"value":value,
                        "reason_code":None if value is not None else fail_reason,
                        "reference_trajectory_id":config.get("reference_trajectory_id"),
                        "reference_trajectory_sha256":_meta_value(config,"reference_trajectory_sha256"),
                        "observed_fixation_count":len(fixation_events),"reference_fixation_count":len(reference),
                        "algorithm":"multimatch-gaze docomparison 0.1.3"})
            scanmatch_requested=requested.intersection(_SCANMATCH_CHILDREN)
            if scanmatch_requested:
                reference=config["reference_trajectory"]
                params=config["scanmatch"]
                ref_times=pd.to_numeric(reference["time_s"],errors="coerce").to_numpy(dtype=float)
                ref_x=pd.to_numeric(reference["gaze_x"],errors="coerce").to_numpy(dtype=float)
                ref_y=pd.to_numeric(reference["gaze_y"],errors="coerce").to_numpy(dtype=float)
                ref_valid=np.isfinite(ref_times)&np.isfinite(ref_x)&np.isfinite(ref_y)
                if "reference_valid" in reference:
                    ref_valid &= reference["reference_valid"].fillna(False).astype(bool).to_numpy()
                times,x,y,valid=_gaze_arrays(subframe)
                valid_source=valid&np.isfinite(times)
                ref_order=np.argsort(ref_times,kind="mergesort")
                source_order=np.argsort(times,kind="mergesort")
                ref_times,ref_x,ref_y,ref_valid=ref_times[ref_order],ref_x[ref_order],ref_y[ref_order],ref_valid[ref_order]
                times,x,y,valid_source=times[source_order],x[source_order],y[source_order],valid_source[source_order]
                ref_start=float(np.min(ref_times[ref_valid])) if ref_valid.any() else math.nan
                ref_end=float(np.max(ref_times[ref_valid])) if ref_valid.any() else math.nan
                source_start=float(np.min(times[valid_source])) if valid_source.any() else math.nan
                source_end=float(np.max(times[valid_source])) if valid_source.any() else math.nan
                common_start=max(start,ref_start,source_start) if all(map(math.isfinite,(ref_start,source_start))) else math.nan
                common_end=min(end,ref_end,source_end) if all(map(math.isfinite,(ref_end,source_end))) else math.nan
                fail_reason=None
                score=None
                normalized_score=None
                normalization_reason="scanmatch_self_alignment_unavailable"
                self_score_observed=None
                self_score_reference=None
                token_count=0
                if not (math.isfinite(common_start) and math.isfinite(common_end) and common_end>common_start):
                    fail_reason="no_common_reference_time_support"
                else:
                    size=config.get("screen_size_px")
                    bins_a=scanmatch_cell_tokens(times,x,y,valid_source,size,int(params["grid_cols"]),int(params["grid_rows"]),
                        float(params["time_bin_s"]),common_start,common_end,cancel)
                    bins_b=scanmatch_cell_tokens(ref_times,ref_x,ref_y,ref_valid,size,int(params["grid_cols"]),int(params["grid_rows"]),
                        float(params["time_bin_s"]),common_start,common_end,cancel)
                    if cancel and cancel():
                        status="cancelled"
                        fail_reason="cancelled"
                    elif bins_a is None or bins_b is None or len(bins_a)!=len(bins_b) or len(bins_a)<2:
                        fail_reason="incomplete_or_out_of_grid_scanmatch_bins"
                    elif not _symmetric_substitution_matrix(params):
                        fail_reason="nonsymmetric_or_invalid_scanmatch_substitution_matrix"
                    elif max(len(bins_a),len(bins_b))>10_000 or len(bins_a)*len(bins_b)>1_000_000:
                        fail_reason="resource_limit_exceeded"
                    else:
                        score,was_cancelled=needleman_wunsch_score(bins_a,bins_b,params,cancel)
                        token_count=len(bins_a)
                        if was_cancelled:
                            status="cancelled"
                            fail_reason="cancelled"
                        elif score is None:
                            fail_reason="invalid_scanmatch_substitution_matrix_or_score"
                        else:
                            self_score_observed,cancelled_observed=needleman_wunsch_score(bins_a,bins_a,params,cancel)
                            self_score_reference,cancelled_reference=needleman_wunsch_score(bins_b,bins_b,params,cancel)
                            if cancelled_observed or cancelled_reference:
                                status="cancelled"
                                normalization_reason="cancelled_during_self_alignment_normalization"
                            elif (self_score_observed is None or self_score_reference is None
                                  or not _finite_number(self_score_observed) or not _finite_number(self_score_reference)
                                  or max(float(self_score_observed),float(self_score_reference))<=0):
                                normalization_reason="scanmatch_self_alignment_unavailable_or_nonpositive"
                            else:
                                denominator=max(float(self_score_observed),float(self_score_reference))
                                ratio=float(score)/denominator
                                if -1e-12<=ratio<=1.0+1e-12:
                                    normalized_score=float(min(1.0,max(0.0,ratio)))
                                    normalization_reason=None
                                else:
                                    normalization_reason="scanmatch_normalization_out_of_bounds"
                shared_params={"reference_trajectory_id":config.get("reference_trajectory_id"),
                    "reference_trajectory_sha256":_meta_value(config,"reference_trajectory_sha256"),
                    "reference_trajectory_unit":_meta_value(config,"reference_trajectory_unit"),
                    "reference_trajectory_coordinate_frame":_meta_value(config,"reference_trajectory_coordinate_frame"),
                    "time_domain_s":[common_start,common_end],"grid_cols":int(params["grid_cols"]),
                    "grid_rows":int(params["grid_rows"]),"time_bin_s":float(params["time_bin_s"]),
                    "substitution_matrix":params["substitution_matrix"],"gap_penalty":float(params["gap_penalty"]),
                    "alignment":"global Needleman-Wunsch","tokenization":"first valid observed position per complete half-open time bin"}
                score_rows=((_SCANMATCH_CHILDREN[0],score,"substitution_score",
                             "experimental_method" if score is not None else fail_reason,
                             {"score_kind":"raw_global_alignment_score"}),
                            (_SCANMATCH_CHILDREN[1],normalized_score,"proportion",
                             "experimental_method" if normalized_score is not None else (fail_reason or normalization_reason),
                             {"normalization":"raw_score/max(observed_self_alignment_score,reference_self_alignment_score)",
                              "observed_self_alignment_score":self_score_observed,
                              "reference_self_alignment_score":self_score_reference}))
                for metric_id,value,unit,reason,details in score_rows:
                    if metric_id not in scanmatch_requested:
                        continue
                    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]==metric_id)
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value,unit=unit,
                        reason=reason,n_obs=token_count,duration=token_count*float(params["time_bin_s"]),
                        extra_params={**shared_params,**details}))
                    scanpath_audit_rows.append({"run_id":config.get("run_id"),"subject_id":subject,"session_id":session,
                        "segment_id":segment_id,"metric_id":metric_id,"value":value,
                        "reason_code":None if value is not None else (fail_reason or normalization_reason),
                        "reference_trajectory_id":config.get("reference_trajectory_id"),
                        "reference_trajectory_sha256":_meta_value(config,"reference_trajectory_sha256"),
                        "time_domain_start_s":common_start,"time_domain_end_s":common_end,
                        "token_count":token_count,"grid_cols":int(params["grid_cols"]),"grid_rows":int(params["grid_rows"]),
                        "time_bin_s":float(params["time_bin_s"]),"substitution_matrix":params["substitution_matrix"],
                        "gap_penalty":float(params["gap_penalty"]),**details,
                        "algorithm":"shared cell tokenization + global Needleman-Wunsch"})
            # Sampling-level path metrics honor gaps and explicit gaze validity.
            if "individual.scanpath.length_samples" in requested:
                x=pd.to_numeric(subframe.get("gaze_x"),errors="coerce").to_numpy(dtype=float) if "gaze_x" in subframe else np.full(len(subframe),np.nan)
                y=pd.to_numeric(subframe.get("gaze_y"),errors="coerce").to_numpy(dtype=float) if "gaze_y" in subframe else np.full(len(subframe),np.nan)
                valid=_flag_mask(subframe,"gaze_x","gaze_valid")&_flag_mask(subframe,"gaze_y","gaze_valid")&~_interp_mask(subframe,"gaze")
                dt=np.diff(t); pos=dt[dt>0]; maxgap=float(config.get("max_gap_s",1.5*np.median(pos))) if len(pos) else 0
                good=valid[:-1]&valid[1:]&(dt>0)&(dt<=maxgap)
                dist=np.hypot(np.diff(x),np.diff(y))
                val=float(np.sum(dist[good])) if good.any() and _required_metadata(next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.scanpath.length_samples"),config)[0] else None
                spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.scanpath.length_samples")
                metrics.append(_row(spec,config,provenance_ok,**base,value=val,unit=str(_meta_value(config,"coordinate_unit") or "unknown"),reason=None if val is not None else "no_valid_adjacent_points_or_coordinate_semantics",n_obs=int(valid.sum()),n_interp=int(_interp_mask(subframe,"gaze").sum()),duration=end-start,extra_params={"no_gap_bridging":True,"max_gap_s":maxgap}))
            for axis, col in (("x","gaze_x"),("y","gaze_y")):
                if col in subframe and "individual.scanpath.spatial_spread_"+axis in requested:
                    mask=_flag_mask(subframe,col,"gaze_valid")&~_interp_mask(subframe,"gaze")
                    vals=pd.to_numeric(subframe[col],errors="coerce").to_numpy(dtype=float)[mask]
                    spec=next(s for s in INDIVIDUAL_CATALOGUE if s["metric_id"]=="individual.scanpath.spatial_spread_"+axis)
                    value=float(np.ptp(vals)) if len(vals)>0 and _required_metadata(spec,config)[0] else None
                    metrics.append(_row(spec,config,provenance_ok,**base,value=value,unit=str(_meta_value(config,"coordinate_unit") or "unknown"),reason=None if value is not None else "no_valid_points_or_coordinate_semantics",n_obs=len(vals),duration=end-start))
        if progress:
            progress((gi+1)/total_groups, f"individual {subject}/{session}")
    # Return only requested, currently available calculations; preserve an explicit
    # unavailable row for requested entries whose preconditions are not met.
    geometry_issue = config.get("_aoi_geometry_issue")
    if isinstance(geometry_issue, dict):
        aoi_mask = availability["metric_id"].astype("string").str.startswith("individual.aoi.")
        availability.loc[aoi_mask, "available"] = False
        availability.loc[aoi_mask, "status"] = AvailabilityStatus.BLOCKED.value
        availability.loc[aoi_mask, "reason_code"] = geometry_issue.get("code", "aoi_geometry_unavailable")
        availability.loc[aoi_mask, "reason"] = geometry_issue.get("message", "AOI geometry is unavailable")
        metrics = [m for m in metrics if not m["metric_id"].startswith("individual.aoi.")]
    metrics = [m for m in metrics if m["metric_id"] in requested]
    seen = {m["metric_id"] for m in metrics}
    for metric_id in sorted(requested_all - seen):
        spec = specs_by_id[metric_id]
        av = availability_by_id[metric_id]
        reason = av["reason_code"] if not av["available"] else "no_qualifying_analysis_unit"
        metrics.append(_row(spec,config,provenance_ok,value=None,reason=reason))
        status = "partial" if status != "cancelled" else status
    if not provenance_ok and metrics and status != "cancelled":
        status = "partial"
    if any(row.get("status") in {"partial", "unavailable"} for row in metrics) and status == "complete":
        status = "partial"
    # If nothing was requested or no inputs were available, preserve that as partial.
    if not metrics and status != "cancelled":
        status = "partial"
    metrics_frame = pd.DataFrame(metrics, columns=METRIC_COLUMNS)
    evout = pd.DataFrame(event_rows,columns=EVENT_COLUMNS)
    aoi_prepared = config.get("_aoi_geometry_prepared")
    aoi_budget = config.get("_aoi_geometry_budget")
    aoi_method_audit = None
    if isinstance(aoi_prepared, PreparedStaticAOIs):
        aoi_method_audit = {
            **dict(aoi_prepared.audit),
            "rows": [{
                "aoi_id": _aoi_audit_scalar(row.get("aoi_id")),
                "stimulus_id": _aoi_audit_scalar(row.get("stimulus_id")),
                "aoi_name": row.get("aoi_name"),
                "aoi_name_status": row.get("aoi_name_status"),
                "fingerprint": row.get("aoi_geometry_fingerprint"),
                "source": row.get("aoi_source"),
            } for row in aoi_prepared.rows],
        }
        if isinstance(aoi_budget, AOIGeometryBudget):
            aoi_method_audit["counts"].update({
                "membership_edge_tests": aoi_budget.membership_edge_tests,
                "membership_edge_tests_by_scope": dict(aoi_budget.membership_edge_tests_by_scope),
                "exact_predicate_evaluations": aoi_budget.exact_predicates,
            })
    if isinstance(config.get("_aoi_geometry_issue"), dict):
        aoi_method_audit = {
            "geometry_version": AOI_GEOMETRY_VERSION,
            "domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION,
            "boundary_policy": AOI_BOUNDARY_POLICY,
            "status": "blocked",
            "issue": config["_aoi_geometry_issue"],
        }
    meta = {"contract_version":"1.0.0", "execution_status":status,
            "engine":"individual", "algorithm_version":ALGORITHM_VERSION,
            "provenance":provenance, "provenance_verified":provenance_ok,
            "source_conditions":{"time_confirmed":_required_metadata(INDIVIDUAL_CATALOGUE[0],config)[0],
                                 "coordinate_unit":_meta_value(config,"coordinate_unit"),
                                 "coordinate_frame":_meta_value(config,"coordinate_frame"),
                                 "validity_columns":{c:(c in frame.columns) for c in ("gaze_valid","pupil_left_valid","pupil_right_valid","analysis_gaze_valid","analysis_pupil_left_valid","analysis_pupil_right_valid")},
                                 "raw_flags_preserved":True,
                                 "extended_fields":_extension_field_context(frame,config)},
            "requested_metric_ids":sorted(requested_all),
            "selected_method_ids":sorted(requested_methods),
            "expanded_method_selections":{family_id:list(children) for family_id,children in _FAMILY_SELECTIONS.items()
                                          if family_id in requested_methods},
            "metric_definitions":{
                _SCANMATCH_CHILDREN[0]:"Raw global Needleman-Wunsch substitution score; its scale is defined by the recorded matrix and gap penalty.",
                _SCANMATCH_CHILDREN[1]:"raw_score / max(observed self-alignment score, reference self-alignment score); emitted only for a positive denominator and a finite ratio in [0,1]. This operational ratio is not asserted to be a published ScanMatch normalization.",
            },
            "metric_count":len(metrics),
            "unavailable_metric_ids":sorted({r["metric_id"] for r in metrics if r["status"]=="unavailable"}),
            "partial_metric_ids":sorted({r["metric_id"] for r in metrics if r["status"]=="partial"})}
    if aoi_method_audit is not None:
        meta["aoi_method_audit"] = aoi_method_audit
    logs=[]
    if not provenance_ok:
        logs.append("Results are partial: verified source_id and SHA-256 provenance are missing or invalid.")
    if status=="cancelled": logs.append("Analysis cancelled; partial outputs must not be treated as complete.")
    return AnalysisBundle(tables={"metrics":metrics_frame,"availability":availability,"events":evout,
                                  "source_events":event_frame.copy(),
                                 "target_audit":pd.DataFrame(target_audit_rows),
                                 "saccade_details":pd.DataFrame(saccade_detail_rows),
                                 "heatmap_grid":pd.DataFrame(heatmap_grid_rows),
                                 "scanpath_audit":pd.DataFrame(scanpath_audit_rows)},
                          metadata=meta,logs=logs)


__all__ = ["analyze_individual", "get_individual_catalogue", "INDIVIDUAL_CATALOGUE"]
