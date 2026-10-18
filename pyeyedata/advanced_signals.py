"""Seven advanced, explicitly gated eye-signal methods.

Pure numerical helpers do not make device-validity claims. The public entry
requires separate measurement, clock, geometry and source evidence; a source
file hash is never device validation. See ADVANCED_METHODS for typed controls.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import warnings
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import OptimizeWarning, curve_fit
from scipy.signal import welch
from scipy.stats import t as student_t

from .contracts import AnalysisBundle, AVAILABILITY_COLUMNS, CONTRACT_VERSION, ContractError, METRIC_COLUMNS
from .event_queries import onset_events as query_event_onsets, overlapping_events as query_event_overlaps

ALGORITHM_VERSION = "advanced-signals-1.0.1"
_IDS = ("subject_id", "session_id", "interaction_id", "segment_id", "trial", "condition",
        "analysis_stream_id", "eye_label", "segment_instance_id", "physical_partition_id")
_P = "individual."


def _parameter(name, kind, default, unit, description, **limits):
    return {"name": "advanced." + name, "type": kind, "default": default,
            "unit": unit, "description": description, **limits}


PARAMETER_DEFINITIONS = (
    _parameter("min_sample_rate_hz", "float", 250.0, "Hz", "Product engineering lower bound for peaks and microsaccades; cannot establish device accuracy.", minimum=250.0),
    _parameter("support_gap_s", "optional_float", None, "s", "Maximum supported adjacent interval; omitted uses 3*median positive dt.", minimum_exclusive=0.0),
    _parameter("uniform_tolerance", "float", 0.001, "relative", "Maximum relative spacing error for EK and declared uniform PSD grid.", minimum=0.0, maximum=0.01),
    _parameter("micro_lambda", "float", 6.0, "ratio", "Robust velocity ellipse threshold multiplier.", minimum_exclusive=0.0),
    _parameter("micro_min_duration_s", "float", 0.006, "s", "Minimum EK candidate duration; sample count times dt.", minimum_exclusive=0.0),
    _parameter("micro_max_amplitude_deg", "float", 1.0, "deg", "Maximum displacement separately for both monocular events.", minimum_exclusive=0.0),
    _parameter("main_sequence_min_events", "integer", 5, "count", "Minimum independently qualified saccades for each fit.", minimum=3),
    _parameter("main_sequence_min_amplitude_range_deg", "float", 0.5, "deg", "Required observed amplitude range for identifiable fitting.", minimum_exclusive=0.0),
    _parameter("confidence", "float", 0.95, "proportion", "Two-sided Student-t approximate parameter intervals, conditional on fit assumptions.", minimum_exclusive=0.0, maximum_exclusive=1.0),
    _parameter("main_sequence_maxfev", "integer", 10000, "count", "Maximum nonlinear solver evaluations.", minimum=100),
    _parameter("pupil_psd_eye", "enum", "left", "eye", "A single measured eye, never an inferred binocular diameter.", choices=["left", "right"]),
    _parameter("pupil_psd_fs_hz", "optional_float", None, "Hz", "Explicit uniform derived grid rate, no higher than reliable original rate.", minimum_exclusive=0.0),
    _parameter("pupil_psd_nperseg", "integer", 256, "samples", "Welch window length; short blocks are disabled rather than silently shrinking it.", minimum=8),
    _parameter("pupil_psd_noverlap", "integer", 128, "samples", "Overlap must be smaller than nperseg.", minimum=0),
    _parameter("pupil_psd_window", "enum", "hann", "window", "DFT-even SciPy window.", choices=["hann", "boxcar", "hamming"]),
    _parameter("pupil_psd_detrend", "enum", "constant", "operation", "Welch detrending; false disables it.", choices=["constant", "linear", False]),
    _parameter("pupil_psd_bands", "band_list", [], "Hz", "Explicit [{name,low_hz,high_hz}] bins: low<=f<high, including final Nyquist when high=Nyquist. Integral=sum(PSD*df); no bands invented."),
    _parameter("pupil_psd_baseline_corrected", "boolean", False, "flag", "Use declared processing baseline-corrected column and record processing evidence."),
    _parameter("pupil_psd_min_coverage", "float", 0.8, "proportion", "Eligible original-block duration / analysis-window duration; does not count generated grid samples.", minimum_exclusive=0.0, maximum=1.0),
    _parameter("perclos_column", "string", "eyelid", "column", "Declared continuous closure measurement column; never missing pupil."),
    _parameter("eyelid_signal_kind", "enum", "continuous_fraction", "representation", "Continuous closure crosses threshold linearly; categorical labels require explicit intervals.", choices=["continuous_fraction", "categorical_intervals"]),
    _parameter("perclos_threshold", "float", 0.8, "closure_fraction", "P80 uses inclusive >= boundary for continuous closure.", minimum=0.0, maximum=1.0),
    _parameter("perclos_min_window_s", "float", 60.0, "s", "Product default analysis-window minimum, adjustable; not a universal device standard.", minimum_exclusive=0.0),
    _parameter("perclos_min_coverage", "float", 0.8, "proportion", "Valid supported eyelid duration divided by explicit window duration.", minimum_exclusive=0.0, maximum=1.0),
    _parameter("perclos_exclude_blinks", "boolean", False, "flag", "Optionally remove confirmed blink intervals from numerator and denominator."),
)
_PARAMS = {p["name"].split(".", 1)[1]: p for p in PARAMETER_DEFINITIONS}


def _spec(metric_id, label, unit, algorithm, fields, metadata, description, parameters, parent=None):
    return {"metric_id": metric_id, "label": label, "category": metric_id.split(".")[1],
            "unit": unit, "algorithm_id": algorithm, "algorithm_version": ALGORITHM_VERSION,
            "requires_fields": list(fields), "requires_metadata": list(metadata),
            "analysis_modes": ["individual"], "reference_ids": {
                "ek2003": ["doi:10.1016/S0042-6989(03)00084-1"],
                "welch1967": ["SciPy-1.14.1:signal.welch"],
                "main-sequence": ["SciPy-1.14.1:optimize.curve_fit", "product-main-sequence-v1"],
                "perclos-p80": ["FHWA-MCRT-98-006", "product-P80-time-support-v1"],
            }.get(algorithm, ["product-advanced-signal-definition-v1"]),
            "implemented": True, "validated": True, "numerically_validated": True,
            "device_validated": False, "experimental": True, "tier": "R2",
            "note": description, "description": description, "parent_metric_id": parent,
            "parameter_definitions": [dict(_PARAMS[n]) for n in parameters]}


_PEAK_META = ("time_confirmed", "clock_source", "angle_geometry", "device_verified", "device_validation_status", "device_validation_method", "device_validation_evidence", "high_rate_validation", "reliable_saccade_source")
ADVANCED_METHODS = (
    _spec(_P+"saccade.peak_velocity", "峰值扫视角速度", "deg/s", "angular-adjacent-speed", ("time_s", "gaze_x", "gaze_y"), _PEAK_META,
          "Mean of per-event maximum adjacent angular speeds. Requires >=5 original event points; details in advanced_saccade_points.", ("min_sample_rate_hz", "support_gap_s")),
    _spec(_P+"saccade.peak_acceleration", "峰值扫视角速率导数", "deg/s^2", "angular-speed-derivative", ("time_s", "gaze_x", "gaze_y"), _PEAK_META,
          "Mean of per-event maximum absolute derivative of scalar angular speed at adjacent-speed midpoint times, >=7 original points; not vector acceleration.", ("min_sample_rate_hz", "support_gap_s")),
    _spec(_P+"saccade.main_sequence", "扫视主序点表与两模型", "deg/s", "main-sequence", ("time_s", "gaze_x", "gaze_y"), _PEAK_META,
          "Primary scalar is fitted Vmax in v=Vmax*(1-exp(-A/C)); all points, Vmax/C and duration=a+b*A parameters, approximate CIs and convergence in advanced_mainsequence_fits.", ("min_sample_rate_hz", "support_gap_s", "main_sequence_min_events", "main_sequence_min_amplitude_range_deg", "confidence", "main_sequence_maxfev")),
    _spec(_P+"microsaccade.features", "双眼 Engbert–Kliegl 微扫视", "count", "ek2003", ("time_s", "gaze_left_x", "gaze_left_y", "gaze_right_x", "gaze_right_y", "gaze_left_valid", "gaze_right_valid"), _PEAK_META[:-1]+("high_precision_binocular_confirmed", "binocular_clock_confirmed"),
          "Primary scalar is binocular overlap-matched event count. Monocular candidates, bilateral displacement and duration in advanced_microsaccades/advanced_micro_monocular; degenerate robust scale is disabled.", ("min_sample_rate_hz", "support_gap_s", "uniform_tolerance", "micro_lambda", "micro_min_duration_s", "micro_max_amplitude_deg")),
    _spec(_P+"pursuit.features", "明确目标运动窗的追踪增益", "ratio", "target-projection-gain", ("time_s", "gaze_x", "gaze_y", "target_x", "target_y", "target_valid"), ("time_confirmed", "clock_source", "target_trajectory_confirmed", "target_clock_confirmed", "target_frame_unit_confirmed", "pursuit_source"),
          "Time-weighted mean dot(vg,vt)/dot(vt,vt) in explicit target movement intervals, excluding reliable saccades/blinks/gaps; RMS positional error is a separate metric. No automatic clinical classification.", ("support_gap_s",)),
    _spec(_P+"pupil.spectral_power", "单眼 Welch 全频谱与定义频带", "pupil_unit^2", "welch1967", ("time_s",), ("time_confirmed", "clock_source", "pupil_unit", "sampling_rate_hz", "uniform_grid", "stationarity_confirmed"),
          "Primary scalar is duration-weighted full-spectrum integral (squared measurement units); PSD density has unit^2/Hz. Each supported block requires at least nperseg original points, preserving original N and grid N; no long-gap concatenation.", ("support_gap_s", "uniform_tolerance", "pupil_psd_eye", "pupil_psd_fs_hz", "pupil_psd_nperseg", "pupil_psd_noverlap", "pupil_psd_window", "pupil_psd_detrend", "pupil_psd_bands", "pupil_psd_baseline_corrected", "pupil_psd_min_coverage")),
    _spec(_P+"blink.perclos", "P80 闭眼时间比例", "proportion", "perclos-p80", ("time_s",), ("time_confirmed", "clock_source", "eyelid_measurement_confirmed", "eyelid_unit", "eyelid_closure_direction"),
          "Closed supported duration / valid supported eyelid duration. Continuous declared fraction uses linear threshold crossings >=P80; categorical closed labels use explicit half-open intervals. Default product window 60s; blink exclusion optional, off by default.", ("support_gap_s", "perclos_column", "eyelid_signal_kind", "perclos_threshold", "perclos_min_window_s", "perclos_min_coverage", "perclos_exclude_blinks")),
)
_CHILDREN = (
    ("saccade.main_sequence.vmax", "saccade.main_sequence", "主序 Vmax", "deg/s"),
    ("saccade.main_sequence.c", "saccade.main_sequence", "主序幅度常数 C", "deg"),
    ("saccade.main_sequence.duration_intercept", "saccade.main_sequence", "时长模型截距", "s"),
    ("saccade.main_sequence.duration_slope", "saccade.main_sequence", "时长模型斜率", "s/deg"),
    ("microsaccade.rate", "microsaccade.features", "双眼微扫视每分钟率", "count/min"),
    ("microsaccade.duration.mean", "microsaccade.features", "双眼微扫视平均时长", "s"),
    ("microsaccade.amplitude.mean", "microsaccade.features", "双眼微扫视平均双眼幅度", "deg"),
    ("pursuit.gain", "pursuit.features", "目标投影增益", "ratio"),
    ("pursuit.rms_error", "pursuit.features", "追踪位置 RMS 误差", "coordinate_unit"),
    ("pupil.spectral_power.frequency_resolution", "pupil.spectral_power", "Welch 频率分辨率", "Hz"),
    ("blink.perclos.percentage", "blink.perclos", "P80 闭眼时间百分比", "percent"),
    ("blink.perclos.closed_duration", "blink.perclos", "P80 闭眼有效时长", "s"),
    ("blink.perclos.valid_duration", "blink.perclos", "P80 眼睑有效支持时长", "s"),
)
_BY_ID = {s["metric_id"]: s for s in ADVANCED_METHODS}
ADVANCED_CHILD_SPECS = tuple({**_BY_ID[_P+parent], "metric_id": _P+child,
                              "label": label, "unit": unit, "parent_metric_id": _P+parent}
                             for child, parent, label, unit in _CHILDREN)
_ALL_SPECS = ADVANCED_METHODS + ADVANCED_CHILD_SPECS
_BY_ID.update({s["metric_id"]: s for s in ADVANCED_CHILD_SPECS})
for _record in _ALL_SPECS:
    if (_record.get("parent_metric_id") or _record["metric_id"])==_P+"pupil.spectral_power":
        _record["conditional_requires_fields"]={"advanced.pupil_psd_eye":{"left":["pupil_left","pupil_left_valid"],"right":["pupil_right","pupil_right_valid"]}}

_DETAIL_SCHEMAS = {
    "advanced_saccade_points": {"event_id":"string","start_s":"Float64","end_s":"Float64","duration_s":"Float64","n_observed":"Int64","status":"string","reason_code":"string","amplitude_deg":"Float64","peak_velocity_deg_s":"Float64","peak_acceleration_deg_s2":"Float64","acceleration_reason_code":"string"},
    "advanced_mainsequence_fits": {"model":"string","parameter":"string","value":"Float64","unit":"string","ci_lower":"Float64","ci_upper":"Float64","confidence":"Float64","ci_method":"string","n_events":"Int64","amplitude_min_deg":"Float64","amplitude_max_deg":"Float64","amplitude_range_deg":"Float64","rmse":"Float64","converged":"boolean","status":"string","reason_code":"string"},
    "advanced_micro_monocular": {"eye":"string","monocular_id":"string","start_s":"Float64","end_s":"Float64","duration_s":"Float64","amplitude_deg":"Float64","delta_x_deg":"Float64","delta_y_deg":"Float64","peak_velocity_deg_s":"Float64","n_observed":"Int64","sigma_x_deg_s":"Float64","sigma_y_deg_s":"Float64","matched":"boolean"},
    "advanced_microsaccades": {"event_id":"string","left_id":"string","right_id":"string","start_s":"Float64","end_s":"Float64","duration_s":"Float64","amplitude_deg":"Float64","left_amplitude_deg":"Float64","right_amplitude_deg":"Float64","left_peak_velocity_deg_s":"Float64","right_peak_velocity_deg_s":"Float64","n_observed_left":"Int64","n_observed_right":"Int64"},
    "advanced_pursuit_series": {"start_s":"Float64","end_s":"Float64","duration_s":"Float64","gain":"Float64","mean_squared_position_error":"Float64","target_speed":"Float64"},
    "advanced_pupil_psd": {"block_id":"Int64","eye":"string","frequency_hz":"Float64","power_density":"Float64","unit":"string","fs_hz":"Float64","frequency_resolution_hz":"Float64","n_original":"Int64","n_grid":"Int64","effective_duration_s":"Float64","baseline_corrected":"boolean"},
    "advanced_pupil_bands": {"block_id":"Int64","eye":"string","band_name":"string","low_hz":"Float64","high_hz":"Float64","power":"Float64","unit":"string","status":"string","reason_code":"string","effective_duration_s":"Float64"},
    "advanced_perclos_intervals": {"start_s":"Float64","end_s":"Float64","closure_start":"Float64","closure_end":"Float64","closed":"boolean","closed_duration_s":"Float64","valid_duration_s":"Float64","signal_kind":"string","threshold":"Float64","closure_direction":"string","blink_exclusion":"boolean","coverage":"Float64","window_start_s":"Float64","window_end_s":"Float64"},
}


def _detail_frame(name, records):
    common={"run_id":"string",**{key:"object" for key in _IDS},"stream_parent_refs":"object","pair_id":"object",
            "support_ref":"object","dependence_ref":"object",
            "algorithm_version":"string","parameter_ref":"string"}
    schema={**common,**_DETAIL_SCHEMAS[name]}
    return pd.DataFrame(records,columns=list(schema)).astype(schema)


def _finite(value):
    return isinstance(value, (int, float, np.number)) and not isinstance(value, (bool, np.bool_)) and math.isfinite(float(value))


def _clean(value):
    if is_dataclass(value):
        return _clean(asdict(value))
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if not isinstance(v, pd.DataFrame)}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        return _clean(value.item())
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _meta(config, name, default=None):
    metadata = config.get("metadata", {})
    cm = metadata.get("coordinate_metadata", config.get("coordinate_metadata", {}))
    if is_dataclass(cm):
        cm = asdict(cm)
    for source in (config, metadata, cm):
        if isinstance(source, dict) and name in source:
            return source[name]
    return default


def _params(config):
    supplied = config.get("advanced", {})
    if not isinstance(supplied, dict):
        raise ContractError("config.advanced must be an object")
    unknown = set(supplied) - set(_PARAMS)
    if unknown:
        raise ContractError("Unknown advanced parameters: " + ",".join(sorted(unknown)))
    result = {name: supplied.get(name, p["default"]) for name, p in _PARAMS.items()}
    for name, p in _PARAMS.items():
        v = result[name]
        kind = p["type"]
        if kind == "optional_float" and v is None:
            continue
        if kind in {"float", "optional_float", "integer"}:
            if not _finite(v) or (kind == "integer" and (not isinstance(v, (int, np.integer)))):
                raise ContractError(f"advanced.{name} requires {kind}")
            for bound, compare in (("minimum", lambda a,b:a<b), ("maximum",lambda a,b:a>b), ("minimum_exclusive",lambda a,b:a<=b), ("maximum_exclusive",lambda a,b:a>=b)):
                if bound in p and compare(v, p[bound]):
                    raise ContractError(f"advanced.{name} violates {bound}")
        elif kind == "boolean" and not isinstance(v, bool):
            raise ContractError(f"advanced.{name} requires boolean")
        elif kind == "enum" and not any(type(v) is type(choice) and v==choice for choice in p["choices"]):
            raise ContractError(f"advanced.{name} has invalid choice")
        elif kind == "string" and (not isinstance(v, str) or not v.strip()):
            raise ContractError(f"advanced.{name} requires nonempty string")
        elif kind == "band_list":
            if not isinstance(v, list):
                raise ContractError("pupil_psd_bands requires a list")
            names = []
            for band in v:
                if not isinstance(band, dict) or not isinstance(band.get("name"), str) or not band["name"].strip() or not _finite(band.get("low_hz")) or not _finite(band.get("high_hz")) or not 0 <= band["low_hz"] < band["high_hz"]:
                    raise ContractError("Each PSD band requires name and 0<=low_hz<high_hz")
                names.append(band["name"])
            if len(set(names)) != len(names):
                raise ContractError("PSD band names must be unique")
    if result["pupil_psd_noverlap"] >= result["pupil_psd_nperseg"]:
        raise ContractError("Welch noverlap must be smaller than nperseg")
    return result


def _array(frame, name):
    if name not in frame:
        return np.full(len(frame), np.nan)
    return pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _flag(frame, name, fallback=None):
    column = name if name in frame else fallback
    if not column or column not in frame:
        return np.zeros(len(frame), dtype=bool)
    try:
        return frame[column].astype("boolean").fillna(False).to_numpy(dtype=bool)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"{column} requires nullable boolean flags") from exc


def _original_valid(frame, signal, columns):
    valid = _flag(frame, "analysis_" + signal + "_valid", signal + "_valid")
    # Explicit source invalid/unknown cannot become trusted high-risk observations.
    source_name = "source_" + signal + "_valid"
    if source_name in frame:
        valid &= _flag(frame, source_name)
    for name in (signal + "_interpolated", "is_interpolated"):
        if name in frame:
            valid &= ~_flag(frame, name)
    valid &= np.isfinite(_array(frame, "time_s"))
    if "time_duplicate" in frame:
        valid &= ~_flag(frame, "time_duplicate")
    for name in columns:
        valid &= np.isfinite(_array(frame, name))
    return valid


def _gap(times, p):
    if p["support_gap_s"] is not None:
        return float(p["support_gap_s"])
    dt = np.diff(times)
    positive = dt[np.isfinite(dt) & (dt > 0)]
    return float(3 * np.median(positive)) if len(positive) else 0.0


def _blocks(times, valid, gap):
    runs = []
    begin = None
    for i in range(len(times)):
        connected = i > 0 and valid[i-1] and 0 < times[i]-times[i-1] <= gap
        if valid[i]:
            if begin is None:
                begin = i
            elif not connected:
                runs.append(np.arange(begin, i))
                begin = i
        elif begin is not None:
            runs.append(np.arange(begin, i))
            begin = None
    if begin is not None:
        runs.append(np.arange(begin, len(times)))
    return runs


def _evidence(value):
    return isinstance(value, dict) and value.get("confirmed") is True and isinstance(value.get("source_refs"), (list,tuple)) and bool(value["source_refs"]) and all(isinstance(x,str) and x.strip() for x in value["source_refs"])


def _metadata_boundary(config):
    for name in ("device_verified","sync_confirmed"):
        value=_meta(config,name,False)
        if not isinstance(value,bool):raise ContractError(name+" must be an actual boolean")
    status=_meta(config,"device_validation_status","unknown")
    if not isinstance(status,str) or status not in {"unknown","unverified","verified"}:
        raise ContractError("Unsupported device_validation_status")
    method=_meta(config,"device_validation_method")
    refs=_meta(config,"device_validation_evidence",())
    if method is not None and (not isinstance(method,str) or not method.strip()):
        raise ContractError("device_validation_method must be a nonempty string")
    if not isinstance(refs,(tuple,list)) or any(not isinstance(x,str) or not x.strip() for x in refs):
        raise ContractError("device_validation_evidence must be a list/tuple of nonempty references")
    if status=="verified" and (not method or not refs):raise ContractError("verified status requires method and evidence")
    if _meta(config,"device_verified",False) is True and (status!="verified" or not method or not refs):
        raise ContractError("device_verified requires verified status, method and evidence")
    for name in ("coordinate_unit","coordinate_frame","target_coordinate_unit","target_coordinate_frame","pupil_unit","eyelid_unit","clock_source"):
        value=_meta(config,name)
        if value is not None and (not isinstance(value,str) or not value.strip()):
            raise ContractError(name+" must be a nonempty string when supplied")
    offsets=_meta(config,"clock_offsets_s",{})
    if not isinstance(offsets,dict) or any(not isinstance(k,str) or not k.strip() or not _finite(v) for k,v in offsets.items()):
        raise ContractError("clock_offsets_s requires explicit clock names and finite numeric seconds")


def _clock_reason(config):
    if _meta(config, "time_confirmed") is not True:
        return "time_not_confirmed"
    source = _meta(config, "clock_source")
    if not isinstance(source, str) or not source.strip() or source.lower() == "unknown":
        return "clock_source_missing"
    return None


def _angle_mode(config):
    geometry = _meta(config, "geometry", {})
    if not isinstance(geometry, dict):
        return None
    rep = _meta(config, "coordinate_representation")
    if rep == "direction" and geometry.get("direction_confirmed") is True:
        return "direction"
    if _meta(config, "coordinate_unit") in {"deg", "degree", "degrees"} and geometry.get("angular_coordinates_confirmed") is True:
        if geometry.get("angular_representation") in {"cartesian_angular", "azimuth_elevation"}:
            return geometry["angular_representation"]
    return None


def _high_rate_reason(frame, config, p, *, binocular=False):
    reason = _clock_reason(config)
    if reason:
        return reason
    if _angle_mode(config) is None:
        return "angular_geometry_not_confirmed"
    if _meta(config, "device_verified") is not True or _meta(config,"device_validation_status")!="verified" or not _meta(config,"device_validation_method") or not _meta(config,"device_validation_evidence"):
        return "device_validation_missing"
    if not _evidence(_meta(config, "high_rate_validation")):
        return "high_rate_validation_missing"
    reason=_sample_rate_reason(_array(frame,"time_s"),p["min_sample_rate_hz"])
    if reason:return reason
    if binocular:
        if _angle_mode(config) != "cartesian_angular":
            return "ek_requires_planar_angular_coordinates"
        eyes=[_meta(config,name,{}) for name in ("left_eye","right_eye")]
        eyes=[asdict(e) if is_dataclass(e) else e for e in eyes]
        if any(not isinstance(e,dict) or e.get("point_unit") not in {"deg","degree","degrees"} or not e.get("point_frame") or e.get("point_frame")=="unknown" for e in eyes):
            return "binocular_angular_units_or_frames_missing"
        if eyes[0]["point_frame"]!=eyes[1]["point_frame"]:
            return "binocular_coordinate_frames_differ"
        if _meta(config, "high_precision_binocular_confirmed") is not True or not _evidence(_meta(config, "binocular_precision_validation")):
            return "binocular_precision_validation_missing"
        if _meta(config, "binocular_clock_confirmed") is not True:
            return "binocular_clock_not_confirmed"
        offsets = _meta(config, "clock_offsets_s", {})
        if isinstance(offsets, dict) and abs(float(offsets.get("left",0))-float(offsets.get("right",0))) > 1e-12:
            return "binocular_clock_offset_unresolved"
    return None


def _sample_rate_reason(times, minimum):
    dt=np.diff(times);positive=dt[np.isfinite(dt)&(dt>0)]
    if not len(positive) or 1.0/float(np.median(positive))<minimum*(1-1e-10):
        return "sample_rate_below_engineering_minimum"
    return None


def _check_cancel(cancel):
    if cancel and cancel():raise InterruptedError("advanced analysis cancelled")


def _vectors(points, mode):
    if mode == "direction":
        norms = np.linalg.norm(points, axis=1)
        if np.any(norms <= 0):
            raise ValueError("zero_direction_vector")
        return points / norms[:, None]
    az, el = np.radians(points[:,0]), np.radians(points[:,1])
    return np.column_stack((np.cos(el)*np.sin(az), np.sin(el), np.cos(el)*np.cos(az)))


def _distance(a, b, mode):
    if mode == "cartesian_angular":
        return np.linalg.norm(b-a, axis=-1)
    return np.degrees(np.arctan2(np.linalg.norm(np.cross(a,b),axis=-1), np.sum(a*b,axis=-1)))


def _saccade_kinematics(times, points, mode):
    """Adjacent speeds and midpoint derivative, excluding no endpoints implicitly."""
    times, points = np.asarray(times,float), np.asarray(points,float)
    if len(times) < 2 or not np.isfinite(times).all() or not np.isfinite(points).all() or np.any(np.diff(times)<=0):
        raise ValueError("invalid_saccade_samples")
    coords = points if mode == "cartesian_angular" else _vectors(points,mode)
    dt = np.diff(times)
    speed = _distance(coords[:-1],coords[1:],mode)/dt
    mid = (times[:-1]+times[1:])/2
    acceleration = np.diff(speed)/np.diff(mid)
    return {"amplitude_deg": float(_distance(coords[:1],coords[-1:],mode)[0]),
            "peak_velocity_deg_s": float(speed.max()),
            "peak_acceleration_deg_s2": float(np.abs(acceleration).max()) if len(acceleration) else None,
            "speed": speed, "speed_time_s": mid, "acceleration": acceleration,
            "acceleration_time_s": (mid[:-1]+mid[1:])/2}


def _main_sequence_model(amplitude, vmax, c):
    return vmax * (-np.expm1(-np.asarray(amplitude,float)/c))


def _fit_main_sequence(points, p, cancel=None):
    """Parameter rows; approximate covariance CIs, with convergence/range audit."""
    a = np.asarray(points["amplitude_deg"],float)
    v = np.asarray(points["peak_velocity_deg_s"],float)
    d = np.asarray(points["duration_s"],float)
    n, spread = len(a), float(np.ptp(a)) if len(a) else 0.0
    reason = "insufficient_main_sequence_events" if n < p["main_sequence_min_events"] else "insufficient_amplitude_range" if spread < p["main_sequence_min_amplitude_range_deg"] else None
    rows = []
    for model, names, units, y in (("v=Vmax*(1-exp(-A/C))", ("vmax","c"),("deg/s","deg"),v), ("duration=a+b*A",("duration_intercept","duration_slope"),("s","s/deg"),d)):
        _check_cancel(cancel)
        params, covariance, local_reason, rmse = None, None, reason, None
        if reason is None:
            try:
                if model.startswith("v="):
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", OptimizeWarning)
                        def checked_model(amplitude,vmax,c):
                            _check_cancel(cancel)
                            return _main_sequence_model(amplitude,vmax,c)
                        params, covariance = curve_fit(checked_model, a, y,
                            p0=(max(float(v.max()),1.0),max(float(np.median(a)),0.01)),
                            bounds=([np.finfo(float).eps,np.finfo(float).eps],[np.inf,np.inf]),
                            maxfev=p["main_sequence_maxfev"], method="trf", x_scale="jac")
                    prediction = _main_sequence_model(a,*params)
                    exp = np.exp(-a/params[1])
                    jac = np.column_stack((1-exp, -params[0]*exp*a/(params[1]**2)))
                else:
                    jac = np.column_stack((np.ones(n),a))
                    params, _, rank, _ = np.linalg.lstsq(jac,y,rcond=None)
                    if rank != 2:
                        raise ValueError("singular_main_sequence_model")
                    prediction = jac @ params
                    covariance = np.linalg.inv(jac.T@jac)*float(np.sum((y-prediction)**2)/(n-2))
                if np.linalg.matrix_rank(jac) != 2 or not np.isfinite(covariance).all() or not np.isfinite(params).all():
                    raise ValueError("main_sequence_covariance_unidentifiable")
                rmse = float(np.sqrt(np.mean((y-prediction)**2)))
            except (RuntimeError, ValueError, OptimizeWarning, np.linalg.LinAlgError) as exc:
                local_reason = "main_sequence_fit_failed:" + str(exc)
                params, covariance = None, None
        q = float(student_t.ppf((1+p["confidence"])/2,n-2)) if n>2 else None
        for i,(name,unit) in enumerate(zip(names,units)):
            value = float(params[i]) if params is not None else None
            se = float(np.sqrt(max(0.,covariance[i,i]))) if covariance is not None else None
            rows.append({"model":model, "parameter":name, "value":value, "unit":unit,
                         "ci_lower":value-q*se if value is not None else None,
                         "ci_upper":value+q*se if value is not None else None,
                         "confidence":p["confidence"], "ci_method":"local covariance, Student-t(n-2); conditional approximate",
                         "n_events":n, "amplitude_min_deg":float(a.min()) if n else None,
                         "amplitude_max_deg":float(a.max()) if n else None,
                         "amplitude_range_deg":spread, "rmse":rmse,
                         "converged":params is not None, "status":"complete" if value is not None else "unavailable", "reason_code":local_reason})
    return rows


def _ek_monocular(times, xy, p, eye):
    times, xy = np.asarray(times,float), np.asarray(xy,float)
    if len(times)<5:
        return [], "insufficient_ek_samples", None
    dt = np.diff(times)
    spacing = float(np.median(dt))
    if spacing<=0 or np.max(np.abs(dt/spacing-1)) > p["uniform_tolerance"]:
        return [], "ek_nonuniform_sampling", None
    v = (xy[3:-1]+xy[4:]-xy[1:-3]-xy[:-4])/(6*spacing)
    sigma2 = np.median(v*v,axis=0)-np.median(v,axis=0)**2
    if not np.isfinite(sigma2).all() or np.any(sigma2<=0):
        return [], "ek_degenerate_robust_scale", None
    sigma = np.sqrt(sigma2)
    supra = np.sum((v/(p["micro_lambda"]*sigma))**2,axis=1)>1
    records = []
    for run in _blocks(times[2:-2],supra,1.5*spacing):
        if len(run)*spacing+1e-12 < p["micro_min_duration_s"]:
            continue
        idx = run+2
        delta = xy[idx[-1]]-xy[idx[0]]
        amplitude = float(np.linalg.norm(delta))
        if amplitude>p["micro_max_amplitude_deg"]:
            continue
        records.append({"eye":eye, "monocular_id":f"{eye}-{len(records)}",
                        "start_s":float(times[idx[0]]), "end_s":float(times[idx[-1]]+spacing),
                        "duration_s":float(len(run)*spacing), "amplitude_deg":amplitude,
                        "delta_x_deg":float(delta[0]), "delta_y_deg":float(delta[1]),
                        "peak_velocity_deg_s":float(np.linalg.norm(v[run],axis=1).max()),
                        "n_observed":int(len(idx)), "sigma_x_deg_s":float(sigma[0]),"sigma_y_deg_s":float(sigma[1]), "matched":False})
    return records, None, sigma.tolist()


def _match_micro(left, right):
    """Maximum temporal overlap, deterministic one-to-one binocular matching."""
    candidates = []
    for i,l in enumerate(left):
        for j,r in enumerate(right):
            overlap = min(l["end_s"],r["end_s"])-max(l["start_s"],r["start_s"])
            if overlap>0:
                candidates.append((-overlap,i,j))
    used_l, used_r, rows = set(),set(),[]
    for neg,i,j in sorted(candidates):
        if i in used_l or j in used_r:
            continue
        used_l.add(i); used_r.add(j)
        l,r=left[i],right[j]
        l["matched"]=r["matched"]=True
        start,end=max(l["start_s"],r["start_s"]),min(l["end_s"],r["end_s"])
        rows.append({"event_id":f"micro-{len(rows)}", "left_id":l["monocular_id"],"right_id":r["monocular_id"],
                     "start_s":start,"end_s":end,"duration_s":end-start,
                     "amplitude_deg":(l["amplitude_deg"]+r["amplitude_deg"])/2,
                     "left_amplitude_deg":l["amplitude_deg"],"right_amplitude_deg":r["amplitude_deg"],
                     "left_peak_velocity_deg_s":l["peak_velocity_deg_s"],"right_peak_velocity_deg_s":r["peak_velocity_deg_s"],
                     "n_observed_left":l["n_observed"],"n_observed_right":r["n_observed"]})
    return rows


def _pursuit_numeric(times, gaze, target, valid, windows, exclusions, gap, cancel=None):
    times,gaze,target=np.asarray(times,float),np.asarray(gaze,float),np.asarray(target,float)
    intervals=[]
    for i in range(len(times)-1):
        if i%4096==0:_check_cancel(cancel)
        t0,t1=times[i:i+2]; dt=t1-t0
        if not (valid[i] and valid[i+1] and 0<dt<=gap):
            continue
        if not any(t0>=a and t1<=b for a,b in windows) or any(t0<b and t1>a for a,b in exclusions):
            continue
        vt=(target[i+1]-target[i])/dt; vg=(gaze[i+1]-gaze[i])/dt
        denominator=float(vt@vt)
        if denominator<=np.finfo(float).tiny:
            continue
        e0=gaze[i]-target[i];e1=gaze[i+1]-target[i+1]
        # Exact integral of squared linearly interpolated positional error.
        error2=float((e0@e0+e0@e1+e1@e1)/3)
        intervals.append({"start_s":float(t0),"end_s":float(t1),"duration_s":float(dt),
                          "gain":float(vg@vt/denominator),"mean_squared_position_error":error2,
                          "target_speed":float(np.sqrt(denominator))})
    duration=sum(r["duration_s"] for r in intervals)
    return {"gain":sum(r["gain"]*r["duration_s"] for r in intervals)/duration if duration else None,
            "rms_error":math.sqrt(sum(r["mean_squared_position_error"]*r["duration_s"] for r in intervals)/duration) if duration else None,
            "duration_s":duration,"intervals":intervals}


def _merge_ranges(ranges):
    ordered=sorted((float(a),float(b)) for a,b in ranges if _finite(a) and _finite(b) and float(b)>float(a))
    merged=[]
    for start,end in ordered:
        if merged and start<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:
            merged.append((start,end))
    return merged


def _pursuit_windowed_numeric(times,gaze,target,valid,windows,exclusions,gap,analysis_window,cancel=None):
    """Clip original linear supports to the label/target windows, then reuse the frozen interval formula."""
    times=np.asarray(times,float);gaze=np.asarray(gaze,float);target=np.asarray(target,float);valid=np.asarray(valid,bool)
    allowed=[]
    for start,end in _merge_ranges(windows):
        left,right=max(start,float(analysis_window[0])),min(end,float(analysis_window[1]))
        if right>left:allowed.append((left,right))
    exclusions=sorted((float(a),float(b)) for a,b in exclusions if _finite(a) and _finite(b) and float(b)>float(a))
    intervals=[]
    for i,dt in enumerate(np.diff(times)):
        if i%4096==0:_check_cancel(cancel)
        if not (valid[i] and valid[i+1] and np.isfinite(dt) and 0<dt<=gap):continue
        t0,t1=float(times[i]),float(times[i+1])
        for a,b in allowed:
            left,right=max(t0,a),min(t1,b)
            if right<=left:continue
            for start,end in _subtract_intervals(left,right,exclusions):
                if end<=start:continue
                f0=(start-t0)/dt;f1=(end-t0)/dt
                g0=gaze[i]+f0*(gaze[i+1]-gaze[i]);g1=gaze[i]+f1*(gaze[i+1]-gaze[i])
                q0=target[i]+f0*(target[i+1]-target[i]);q1=target[i]+f1*(target[i+1]-target[i])
                clipped=_pursuit_numeric(np.asarray([start,end]),np.vstack([g0,g1]),np.vstack([q0,q1]),
                                         np.asarray([True,True]),[(start,end)],(),end-start,cancel)
                intervals.extend(clipped["intervals"])
    duration=sum(row["duration_s"] for row in intervals)
    return {"gain":sum(row["gain"]*row["duration_s"] for row in intervals)/duration if duration else None,
            "rms_error":math.sqrt(sum(row["mean_squared_position_error"]*row["duration_s"] for row in intervals)/duration) if duration else None,
            "duration_s":duration,"intervals":intervals}


def _support_reference(frame):
    id_column="observation_id" if "observation_id" in frame else "original_index"
    times=_array(frame,"time_s")
    identities=frame[id_column].astype("string").fillna("").tolist() if id_column in frame else [""]*len(frame)
    rows=sorted(((float(time),identities[index]) for index,time in enumerate(times) if np.isfinite(time)),
                key=lambda row:(row[0],row[1]))
    raw=json.dumps(rows,ensure_ascii=False,separators=(",",":"))
    return "support:"+hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _welch_block(times, values, fs, p):
    """Uniform derived grid within a single supported original block."""
    times,values=np.asarray(times,float),np.asarray(values,float)
    if len(times)<p["pupil_psd_nperseg"]:
        return None,"psd_block_shorter_than_nperseg"
    n=int(math.floor((times[-1]-times[0])*fs+1e-9))+1
    grid=times[0]+np.arange(n)/fs
    data=np.interp(grid,times,values)
    if len(data)<p["pupil_psd_nperseg"]:
        return None,"psd_block_shorter_than_nperseg"
    freq,power=welch(data,fs=fs,window=p["pupil_psd_window"],nperseg=p["pupil_psd_nperseg"],
                     noverlap=p["pupil_psd_noverlap"],detrend=p["pupil_psd_detrend"],scaling="density",return_onesided=True,average="mean")
    return {"frequency_hz":freq,"density":power,"n_original":len(times),"n_grid":len(data),
            "duration_s":float(grid[-1]-grid[0]),"frequency_resolution_hz":float(fs/p["pupil_psd_nperseg"]),
            "total_power":float(np.sum(power)*(fs/p["pupil_psd_nperseg"]))},None


def _band_integral(frequency, density, low, high):
    if low<frequency[0] or high>frequency[-1] or high-low<frequency[1]-frequency[0]-1e-12:
        return None,"psd_band_unresolved_or_outside_nyquist"
    selected=(frequency>=low)&(frequency<high)
    if math.isclose(high,float(frequency[-1]),rel_tol=1e-12,abs_tol=1e-12):
        selected[-1]=True
    if not selected.any():return None,"psd_band_contains_no_frequency_bins"
    return float(np.sum(density[selected])*(frequency[1]-frequency[0])),None


def _subtract_intervals(start,end,exclude):
    pieces=[(start,end)]
    for a,b in exclude:
        next_pieces=[]
        for x,y in pieces:
            if b<=x or a>=y:
                next_pieces.append((x,y))
            else:
                if a>x:next_pieces.append((x,min(y,a)))
                if b<y:next_pieces.append((max(x,b),y))
        pieces=next_pieces
    return pieces


def _perclos_continuous(times, closure, valid, threshold, gap, window, exclusions=(), cancel=None):
    records=[]
    for i in range(len(times)-1):
        if i%4096==0:_check_cancel(cancel)
        t0,t1=times[i:i+2];y0,y1=closure[i:i+2];dt=t1-t0
        if not (valid[i] and valid[i+1] and 0<dt<=gap and 0<=y0<=1 and 0<=y1<=1):continue
        start,end=max(t0,window[0]),min(t1,window[1])
        if end<=start:continue
        for a,b in _subtract_intervals(start,end,exclusions):
            u=y0+(y1-y0)*(a-t0)/dt;v=y0+(y1-y0)*(b-t0)/dt
            if u>=threshold and v>=threshold:closed=b-a
            elif u<threshold and v<threshold:closed=0.0
            else:
                crossing=a+(threshold-u)*(b-a)/(v-u)
                closed=b-crossing if v>=threshold else crossing-a
            records.append({"start_s":float(a),"end_s":float(b),"closure_start":float(u),"closure_end":float(v),
                            "closed_duration_s":float(closed),"valid_duration_s":float(b-a),"signal_kind":"continuous_fraction"})
    return records


def _perclos_categorical(intervals, window, exclusions=(), cancel=None):
    """Union consistent categorical intervals; conflicting closure is ambiguous."""
    required={"start_s","end_s","closed","valid"}
    if not required<=set(intervals):return [],"categorical_eyelid_interval_fields_missing"
    records=[];source=[]
    ordered=intervals.sort_values("start_s",kind="stable")
    for _,r in ordered.iterrows():
        _check_cancel(cancel)
        a,b=r["start_s"],r["end_s"]
        if not _finite(a) or not _finite(b) or b<=a:return [],"invalid_categorical_eyelid_interval"
        if not isinstance(r["valid"],(bool,np.bool_)) or not isinstance(r["closed"],(bool,np.bool_)):return [],"categorical_eyelid_labels_not_boolean"
        if not r["valid"]:continue
        a,b=max(float(a),window[0]),min(float(b),window[1])
        if b>a:source.append((a,b,bool(r["closed"])))
    boundaries=sorted({v for a,b,_ in source for v in (a,b)})
    for a,b in zip(boundaries[:-1],boundaries[1:]):
        _check_cancel(cancel)
        active={closed for x,y,closed in source if x<b and y>a}
        if len(active)>1:return [],"overlapping_categorical_eyelid_intervals"
        if not active:continue
        closed=active.pop()
        for x,y in _subtract_intervals(a,b,exclusions):
            if y>x:records.append({"start_s":x,"end_s":y,"closed":closed,"closed_duration_s":y-x if closed else 0.0,"valid_duration_s":y-x,"signal_kind":"categorical_intervals"})
    return records,None


def _associated(table, ids, *, segment_optional=False):
    if not isinstance(table,pd.DataFrame) or table.empty:
        return pd.DataFrame(), "associated_table_missing"
    out=table.copy()
    for key in ("subject_id","session_id","interaction_id","trial","condition","segment_id"):
        value=ids.get(key)
        if value is None:continue
        if key=="segment_id" and segment_optional and key not in out:continue
        if key not in out:return pd.DataFrame(),"association_key_missing:"+key
        out=out.loc[out[key].eq(value).fillna(False)]
    for key in ("source_id","stimulus_id","analysis_stream_id","physical_partition_id",
                "timebase_id","clock_id","segment_instance_id"):
        value=ids.get(key)
        if value is not None and key in out:
            out=out.loc[out[key].eq(value).fillna(False)]
    return out,None


def _scopes(signals, context):
    segments=context.get("segments")
    has_segments=isinstance(segments,pd.DataFrame) and not segments.empty
    identity_keys=tuple(dict.fromkeys((*_IDS,"source_id","timebase_id","clock_id")))
    keys=[x for x in identity_keys if x in signals and (x!="segment_id" or not has_segments)]
    for group_key, group in signals.groupby(keys,dropna=False,sort=False):
        if not isinstance(group_key,tuple):group_key=(group_key,)
        ids={k:_clean(v) for k,v in zip(keys,group_key)}
        if has_segments:
            matched,reason=_associated(segments,ids,segment_optional=True)
            if reason:raise ContractError("segments "+reason)
            required={"segment_id","start_s","end_s"}
            if not required<=set(matched):raise ContractError("segments missing interval fields")
            for _,segment in matched.iterrows():
                if not _finite(segment["start_s"]) or not _finite(segment["end_s"]) or float(segment["end_s"])<=float(segment["start_s"]):
                    raise ContractError("invalid segment boundary")
            overlap_refs={index:set() for index in matched.index}
            segment_rows=list(matched.iterrows())
            for position,(index_a,segment_a) in enumerate(segment_rows):
                for index_b,segment_b in segment_rows[position+1:]:
                    a0,a1=float(segment_a["start_s"]),float(segment_a["end_s"])
                    b0,b1=float(segment_b["start_s"]),float(segment_b["end_s"])
                    overlap_start,overlap_end=max(a0,b0),min(a1,b1)
                    if overlap_end<=overlap_start:continue
                    policies={str(segment_a.get("overlap_policy","")).casefold(),str(segment_b.get("overlap_policy","")).casefold()}
                    if policies!={"keep_labels"}:
                        raise ContractError("overlapping advanced analysis labels require overlap_policy='keep_labels'")
                    identities=sorted(str(row.get("segment_instance_id") if pd.notna(row.get("segment_instance_id")) else row.get("segment_id"))
                                      for row in (segment_a,segment_b))
                    payload=json.dumps({"instances":identities,"start_s":overlap_start,"end_s":overlap_end},
                                       sort_keys=True,separators=(",",":"))
                    ref="segment-overlap:"+hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    overlap_refs[index_a].add(ref);overlap_refs[index_b].add(ref)
            for _,segment in matched.iterrows():
                a,b=segment["start_s"],segment["end_s"]
                if not _finite(a) or not _finite(b) or b<=a:raise ContractError("invalid segment boundary")
                ordered=group.sort_values(["time_s","original_index"],kind="mergesort")
                t=_array(ordered,"time_s")
                inside=(t>=a)&(t<b)
                support_mask=inside.copy()
                before=np.flatnonzero(t<a);after=np.flatnonzero(t>=b)
                if len(before):support_mask[before[-1]]=True
                if len(after):support_mask[after[0]]=True
                support=ordered.loc[support_mask].copy().reset_index(drop=True)
                scope=ordered.loc[inside].copy().reset_index(drop=True)
                if not support.empty:
                    scoped_ids={**ids,"segment_id":_clean(segment["segment_id"])}
                    for key in ("trial","condition","segment_instance_id","analysis_stream_id","physical_partition_id"):
                        if key in segment and pd.notna(segment[key]):scoped_ids[key]=_clean(segment[key])
                    scope.attrs["_window_support_frame"]=support
                    refs=set(overlap_refs.get(segment.name,set()))
                    declared=segment.get("dependence_ref")
                    if isinstance(declared,(list,tuple,set)):refs.update(str(value) for value in declared if value is not None)
                    elif declared is not None and not pd.isna(declared):refs.add(str(declared))
                    scope.attrs["_scope_dependence_ref"]=tuple(sorted(refs)) or None
                    yield scoped_ids,scope,(float(a),float(b))
        else:
            ordered=group.sort_values(["time_s","original_index"],kind="mergesort").copy().reset_index(drop=True)
            t=_array(ordered,"time_s");finite=t[np.isfinite(t)]
            if len(finite):
                ordered.attrs["_window_support_frame"]=ordered.copy()
                yield ids,ordered,(float(finite.min()),float(finite.max()))


def _event_source_reason(config, *, pursuit=False):
    if pursuit:
        source=_meta(config,"pursuit_source")
        if not isinstance(source,dict) or source.get("confirmed") is not True or not source.get("source") or not source.get("version"):
            return "pursuit_source_definition_not_confirmed"
    else:
        source=_meta(config,"saccade_detector",{})
        if config.get("event_source_confirmed") is not True:
            return "event_source_not_confirmed"
        direct=bool(config.get("event_source") and config.get("event_algorithm_version"))
        documented=isinstance(source,dict) and source.get("validated") is True and source.get("source") and source.get("version")
        if not (direct or documented):return "saccade_source_or_version_missing"
    return None


def _reliable_events(events, ids, window, *, overlap=False):
    required={"event_id","event_type","start_s","end_s","status","subject_id","session_id"}
    if isinstance(events,pd.DataFrame) and events.empty and required<=set(events):
        return events.copy(),None
    if not isinstance(events,pd.DataFrame) or not required<=set(events):
        return pd.DataFrame(),"event_fields_missing"
    identities={k:v for k,v in ids.items() if k!="segment_id" and v is not None}
    query=query_event_overlaps if overlap else query_event_onsets
    selected=query(events,float(window[0]),float(window[1]),identities=identities,
                   status_values=("reliable","complete","source_confirmed"),
                   allow_unspecified_stream="analysis_stream_id" not in identities)
    return selected,None


def _provenance(config):
    manifest=config.get("provenance",{}).get("source_manifest",config.get("source_manifest",_meta(config,"source_manifest",[])))
    okay=isinstance(manifest,list) and bool(manifest)
    if okay:
        for r in manifest:
            digest=r.get("sha256",r.get("content_sha256")) if isinstance(r,dict) else None
            name=r.get("source_id",r.get("source_name")) if isinstance(r,dict) else None
            if not name or not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdefABCDEF" for c in digest):okay=False;break
    return okay,{"state":"verified" if okay else "missing_or_invalid","source_manifest":_clean(manifest),"device_verification_independent":True}


def _compute_advanced_metrics(signals, config, events=None, context=None, cancel=None, progress=None):
    """Return scalar long-table metrics plus named reproducible detail tables.

    signals is a canonical processed DataFrame, config a typed dict, raw events
    retains association/evidence columns, context holds named DataFrames including
    segments, target_movement_intervals and eyelid_intervals. No source is guessed.
    Cancellation returns a cancelled bundle and all prior rows marked partial.
    """
    if not isinstance(signals,pd.DataFrame) or not {"subject_id","session_id","time_s","original_index"}<=set(signals):
        raise ContractError("advanced signals require canonical subject/session/time_s/original_index")
    if signals[["subject_id","session_id","original_index"]].isna().any().any():
        raise ContractError("advanced signal identifiers must be explicit")
    if not isinstance(config,dict) or not isinstance(config.get("run_id"),str) or not config["run_id"].strip():
        raise ContractError("advanced config requires nonempty run_id")
    context={} if context is None else context
    if not isinstance(context,dict):raise ContractError("advanced context must be an object")
    events=config.get("events") if events is None else events
    _metadata_boundary(config)
    p=_params(config);prov_ok,prov=_provenance(config)
    snapshot={"algorithm_version":ALGORITHM_VERSION,"advanced":p,
              "metadata":_clean(config.get("metadata",{})),
              "coordinate_metadata":_clean(config.get("coordinate_metadata",{})),
              "event_source":_clean(config.get("event_source")),"event_algorithm_version":_clean(config.get("event_algorithm_version")),
              "event_source_confirmed":config.get("event_source_confirmed",False),
              "source_manifest":prov["source_manifest"]}
    semantic_names={x for s in ADVANCED_METHODS for x in s["requires_metadata"]}|{
        "geometry","coordinate_unit","coordinate_frame","coordinate_representation","device_validation",
        "binocular_precision_validation","clock_offsets_s","saccade_detector","processing_refs",
        "sampling_confirmed","stationarity_evidence","eyelid_measurement_evidence","eyelid_percent_conversion_confirmed","blink_source_confirmed","blink_source","blink_algorithm_version","left_eye","right_eye"}
    snapshot["semantic_metadata"]={name:_clean(_meta(config,name)) for name in sorted(semantic_names)}
    def table_hash(table):
        if not isinstance(table,pd.DataFrame):return None
        representation=table.to_json(orient="split",double_precision=15,date_unit="ns")
        return hashlib.sha256(representation.encode("utf-8")).hexdigest()
    snapshot["context_table_sha256"]={str(name):table_hash(table) for name,table in context.items() if isinstance(table,pd.DataFrame)}
    snapshot["event_table_sha256"]=table_hash(events)
    raw=json.dumps(_clean(snapshot),sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False)
    parameter_ref="advanced:"+hashlib.sha256(raw.encode("utf-8")).hexdigest()
    requested=config.get("metric_ids")
    requested=set(requested) if isinstance(requested,(list,tuple,set)) else {s["metric_id"] for s in ADVANCED_METHODS}
    families={_BY_ID[x].get("parent_metric_id") or x for x in requested if x in _BY_ID}
    selected={x for x in _BY_ID if x in requested or (_BY_ID[x].get("parent_metric_id") in requested)}
    rows=[];details={name:[] for name in ("advanced_saccade_points","advanced_mainsequence_fits","advanced_micro_monocular","advanced_microsaccades","advanced_pursuit_series","advanced_pupil_psd","advanced_pupil_bands","advanced_perclos_intervals")}
    status="complete";logs=[]
    scopes=list(_scopes(signals,context))
    pupil_scopes_seen=set()
    for number,(ids,frame,window) in enumerate(scopes):
        if cancel and cancel():status="cancelled";break
        if progress:progress(number/max(1,len(scopes)),"计算高级眼动方法：明确数据流与分析窗")
        support_frame=frame.attrs.get("_window_support_frame",frame)
        support_frame=support_frame.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True)
        t=_array(frame,"time_s");gap=_gap(t,p)
        support_t=_array(support_frame,"time_s");support_gap=_gap(support_t,p)
        common={"run_id":config["run_id"],**{k:ids.get(k) for k in _IDS},"pair_id":None,
                "stream_parent_refs":_clean(frame.iloc[0].get("stream_parent_refs")) if len(frame) and "stream_parent_refs" in frame else None,
                "support_ref":_support_reference(support_frame),
                "dependence_ref":_clean(frame.attrs.get("_scope_dependence_ref"))}
        pupil_columns=[c for c in ("time_s","original_index","observation_id","pupil_left","pupil_left_valid",
                                    "pupil_right","pupil_right_valid","eyelid","eyelid_valid","eyelid_interpolated")
                       if c in support_frame]
        pupil_payload={"scope":{k:ids.get(k) for k in ("source_id","subject_id","session_id","interaction_id","segment_id","segment_instance_id","trial","physical_partition_id","timebase_id","clock_id")},
                       "window":window,
                       "support":support_frame[pupil_columns].to_json(orient="records",double_precision=15) if pupil_columns else []}
        pupil_scope_key=hashlib.sha256(json.dumps(_clean(pupil_payload),sort_keys=True,ensure_ascii=False,separators=(",",":"),default=str).encode("utf-8")).hexdigest()
        compute_pupil_scope=pupil_scope_key not in pupil_scopes_seen
        pupil_scopes_seen.add(pupil_scope_key)
        psd_eyes=("left","right") if p["pupil_psd_eye"]=="both" else (p["pupil_psd_eye"],)
        psd_stream="pupil:"+("both" if len(psd_eyes)==2 else psd_eyes[0])
        pupil_common={**common,"analysis_stream_id":psd_stream,
                      "eye_label":"both" if len(psd_eyes)==2 else psd_eyes[0],
                      "stream_parent_refs":tuple("pupil:"+eye for eye in psd_eyes) if len(psd_eyes)>1 else None}
        eyelid_stream=_meta(config,"eyelid_stream_id")
        eyelid_common={**common,"analysis_stream_id":eyelid_stream if isinstance(eyelid_stream,str) and eyelid_stream.strip() else "eyelid",
                       "eye_label":_meta(config,"eyelid_eye"),"stream_parent_refs":None}
        def emit(metric,value=None,unit=None,reason=None,n=0,duration=0.,row_common=None,
                 n_window=None,n_support=None):
            if metric not in selected:return
            finite=_finite(value)
            rows.append({**(common if row_common is None else row_common),"metric_id":metric,"value":float(value) if finite else np.nan,
                         "unit":unit or _BY_ID[metric]["unit"],"status":"complete" if finite and prov_ok and not reason else "partial" if finite else "unavailable",
                         "reason_code":reason or (None if finite and prov_ok else "provenance_missing" if finite else "no_supported_observations"),
                         "n_observed":int(n),"n_interpolated":0,"effective_duration_s":float(duration),
                         "n_window_observed":int(n if n_window is None else n_window),
                         "n_support_observed":int(n if n_support is None else n_support),
                         "algorithm_version":ALGORITHM_VERSION,"parameter_ref":parameter_ref})
        def blocked(family,reason,row_common=None):
            for s in _ALL_SPECS:
                if (s.get("parent_metric_id") or s["metric_id"])==family:emit(s["metric_id"],reason=reason,row_common=row_common)
        def add(name,record,row_common=None):
            details[name].append({**(common if row_common is None else row_common),**_clean(record),"algorithm_version":ALGORITHM_VERSION,"parameter_ref":parameter_ref})
        if not np.isfinite(t).all() or np.any(np.diff(t)<=0):
            for family in families:blocked(family,"time_axis_not_finite_strictly_increasing")
            continue
        if window[1]<=window[0]:
            for family in families:blocked(family,"no_positive_analysis_window")
            continue
        ev,event_reason=_reliable_events(events,ids,window)
        exclusion_events,exclusion_reason=_reliable_events(events,ids,window,overlap=True)
        peak_families=families&{_P+"saccade.peak_velocity",_P+"saccade.peak_acceleration",_P+"saccade.main_sequence"}
        if peak_families:
            reason=_high_rate_reason(frame,config,p) or _event_source_reason(config) or event_reason
            if reason:
                for family in peak_families:blocked(family,reason)
            else:
                points=[];mode=_angle_mode(config)
                columns=["gaze_x","gaze_y"]+(["gaze_z"] if mode=="direction" else [])
                valid=_original_valid(frame,"gaze",columns)
                for _,event in ev.loc[ev["event_type"].astype(str).str.lower().eq("saccade")].iterrows():
                    _check_cancel(cancel)
                    a,b=float(event["start_s"]),float(event["end_s"])
                    ix=np.flatnonzero((t>=a)&(t<=b))
                    local_reason=None
                    if a<window[0] or b>window[1]:local_reason="saccade_crosses_analysis_boundary"
                    elif len(ix)<5:local_reason="saccade_less_than_5_original_points"
                    elif not valid[ix].all():local_reason="saccade_contains_invalid_or_interpolated_samples"
                    elif np.any(np.diff(t[ix])>min(gap,1.0/p["min_sample_rate_hz"]*(1+1e-8))):local_reason="saccade_gap_or_low_local_sample_rate"
                    rec={"event_id":_clean(event["event_id"]),"start_s":a,"end_s":b,"duration_s":b-a,"n_observed":int(valid[ix].sum()),"status":"unavailable" if local_reason else "complete","reason_code":local_reason}
                    if not local_reason:
                        try:
                            k=_saccade_kinematics(t[ix],np.column_stack([_array(frame,c)[ix] for c in columns]),mode)
                            rec.update({x:k[x] for x in ("amplitude_deg","peak_velocity_deg_s","peak_acceleration_deg_s2")})
                            if len(ix)<7:rec["peak_acceleration_deg_s2"]=None;rec["acceleration_reason_code"]="saccade_less_than_7_original_points"
                            points.append(rec)
                        except ValueError as exc:rec["status"]="unavailable";rec["reason_code"]=str(exc)
                    add("advanced_saccade_points",rec)
                duration=sum(x["duration_s"] for x in points);n=sum(x["n_observed"] for x in points)
                for family,key in ((_P+"saccade.peak_velocity","peak_velocity_deg_s"),(_P+"saccade.peak_acceleration","peak_acceleration_deg_s2")):
                    eligible=[x for x in points if x.get(key) is not None]
                    values=[x[key] for x in eligible]
                    if family in families:emit(family,float(np.mean(values)) if values else None,reason=None if values else "no_qualified_saccades",n=sum(x["n_observed"] for x in eligible),duration=sum(x["duration_s"] for x in eligible))
                family=_P+"saccade.main_sequence"
                if family in families:
                    pointframe=pd.DataFrame(points,columns=["amplitude_deg","peak_velocity_deg_s","duration_s"])
                    fits=_fit_main_sequence(pointframe,p,cancel)
                    for fit in fits:add("advanced_mainsequence_fits",fit)
                    for fit in fits:
                        suffix={"vmax":"vmax","c":"c","duration_intercept":"duration_intercept","duration_slope":"duration_slope"}[fit["parameter"]]
                        emit(family+"."+suffix,fit["value"],fit["unit"],fit["reason_code"],n,duration)
                        if suffix=="vmax":emit(family,fit["value"],fit["unit"],fit["reason_code"],n,duration)
        family=_P+"microsaccade.features"
        if family in families:
            reason=_high_rate_reason(frame,config,p,binocular=True)
            if reason:blocked(family,reason)
            else:
                leftcols=["gaze_left_x","gaze_left_y"];rightcols=["gaze_right_x","gaze_right_y"]
                valid=_original_valid(frame,"gaze_left",leftcols)&_original_valid(frame,"gaze_right",rightcols)
                candidates=[];matched=[];support=0.;n=0;failures=[]
                for bi,ix in enumerate(_blocks(t,valid,gap)):
                    _check_cancel(cancel)
                    if len(ix)<5:failures.append("insufficient_ek_samples");continue
                    left,lreason,_=_ek_monocular(t[ix],np.column_stack([_array(frame,c)[ix] for c in leftcols]),p,"left")
                    right,rreason,_=_ek_monocular(t[ix],np.column_stack([_array(frame,c)[ix] for c in rightcols]),p,"right")
                    if lreason or rreason:failures.append(lreason or rreason);continue
                    for r in left+right:r["monocular_id"]=f"block{bi}:"+r["monocular_id"]
                    events_micro=_match_micro(left,right)
                    for r in events_micro:r["event_id"]=f"block{bi}:"+r["event_id"]
                    candidates.extend(left+right);matched.extend(events_micro)
                    support+=float(t[ix[-3]]-t[ix[2]]+np.median(np.diff(t[ix])))
                    n+=len(ix)
                for r in candidates:add("advanced_micro_monocular",r)
                for r in matched:add("advanced_microsaccades",r)
                if not support:blocked(family,failures[0] if failures else "no_supported_binocular_samples")
                else:
                    emit(family,len(matched),n=n,duration=support)
                    emit(_P+"microsaccade.rate",len(matched)*60/support,n=n,duration=support)
                    for metric,key in (("duration.mean","duration_s"),("amplitude.mean","amplitude_deg")):
                        emit(_P+"microsaccade."+metric,np.mean([r[key] for r in matched]) if matched else None,reason=None if matched else "no_binocular_microsaccades",n=n,duration=support)
                    if failures:logs.append("microsaccade_partial_blocks:"+",".join(sorted(set(failures))))
        family=_P+"pursuit.features"
        if family in families:
            reason=_clock_reason(config) or _event_source_reason(config,pursuit=True)
            for field in ("target_trajectory_confirmed","target_clock_confirmed","target_frame_unit_confirmed"):
                if not reason and _meta(config,field) is not True:reason=field+"_missing"
            unit=_meta(config,"coordinate_unit");coordframe=_meta(config,"coordinate_frame")
            if not reason and (not unit or unit=="unknown" or not coordframe or coordframe=="unknown"):reason="pursuit_coordinate_unit_or_frame_missing"
            if not reason and _meta(config,"target_coordinate_unit")!=unit:reason="pursuit_target_unit_missing_or_different"
            if not reason and _meta(config,"target_coordinate_frame")!=coordframe:reason="pursuit_target_frame_missing_or_different"
            movements,mreason=_associated(context.get("target_movement_intervals"),ids,segment_optional=True)
            if not reason and (mreason or not {"start_s","end_s"}<=set(movements)):reason="explicit_target_movement_intervals_missing"
            if not reason and exclusion_reason:reason="reliable_exclusion_events_missing"
            if not reason and _event_source_reason(config):reason="reliable_exclusion_events_source_missing"
            columns=["gaze_x","gaze_y"];targetcols=["target_x","target_y"]
            if _meta(config,"coordinate_representation")=="3d_point":columns.append("gaze_z");targetcols.append("target_z")
            if not reason and not set(targetcols)<=set(frame):reason="target_sample_coordinates_missing"
            if reason:blocked(family,reason)
            else:
                windows=[]
                for _,r in movements.iterrows():
                    if not _finite(r["start_s"]) or not _finite(r["end_s"]) or r["end_s"]<=r["start_s"]:raise ContractError("invalid target movement interval")
                    windows.append((float(r["start_s"]),float(r["end_s"])))
                valid=_original_valid(frame,"gaze",columns)&_original_valid(frame,"target",targetcols)
                exclude=[(float(r["start_s"]),float(r["end_s"])) for _,r in exclusion_events.loc[exclusion_events["event_type"].astype(str).str.lower().isin(["saccade","blink"])].iterrows()]
                support_valid=_original_valid(support_frame,"gaze",columns)&_original_valid(support_frame,"target",targetcols)
                result=_pursuit_windowed_numeric(support_t,np.column_stack([_array(support_frame,c) for c in columns]),
                    np.column_stack([_array(support_frame,c) for c in targetcols]),support_valid,windows,exclude,
                    support_gap,window,cancel)
                for r in result["intervals"]:add("advanced_pursuit_series",r)
                reason=None if result["duration_s"] else "no_moving_target_supported_intervals"
                window_valid=_original_valid(frame,"gaze",columns)&_original_valid(frame,"target",targetcols)
                emit(family,result["gain"],reason=reason,n=support_valid.sum(),duration=result["duration_s"],n_window=window_valid.sum(),n_support=support_valid.sum())
                emit(_P+"pursuit.gain",result["gain"],reason=reason,n=support_valid.sum(),duration=result["duration_s"],n_window=window_valid.sum(),n_support=support_valid.sum())
                emit(_P+"pursuit.rms_error",result["rms_error"],unit,reason,support_valid.sum(),result["duration_s"],n_window=window_valid.sum(),n_support=support_valid.sum())
        family=_P+"pupil.spectral_power"
        if family in families and compute_pupil_scope:
            reason=_clock_reason(config);unit=_meta(config,"pupil_unit")
            rate=_meta(config,"sampling_rate_hz");fs=p["pupil_psd_fs_hz"]
            grid=_meta(config,"uniform_grid")
            if not reason and (not isinstance(unit,str) or not unit.strip() or unit=="unknown"):reason="pupil_measurement_unit_missing"
            if not reason and not (_finite(rate) and rate>0 and _meta(config,"sampling_confirmed") is True):reason="reliable_sampling_rate_missing"
            if not reason and not _evidence(grid):reason="uniform_grid_evidence_missing"
            if not reason and not (_meta(config,"stationarity_confirmed") is True and _evidence(_meta(config,"stationarity_evidence"))):reason="stationarity_assessment_missing"
            dt=np.diff(t);actual=1/float(np.median(dt)) if len(dt) and np.all(dt>0) else 0.
            if not reason and (fs is None or fs>min(rate,actual)*(1+1e-9)):reason="psd_grid_rate_missing_or_above_original"
            if not reason and fs<actual*(1-p["uniform_tolerance"]):
                cutoff=grid.get("anti_alias_cutoff_hz")
                if grid.get("anti_alias_applied") is not True or not _finite(cutoff) or not 0<cutoff<fs/2:
                    reason="psd_downsample_antialias_evidence_missing"
            eye=p["pupil_psd_eye"];column="pupil_"+eye
            sourcecol=column
            if p["pupil_psd_baseline_corrected"]:
                column+="_baseline_corrected"
                if not reason and not _meta(config,"processing_refs"):reason="baseline_processing_evidence_missing"
            if not reason and column not in frame:reason="pupil_psd_column_missing"
            if reason:blocked(family,reason,pupil_common)
            else:
                valid=_original_valid(frame,sourcecol,[column]);values=_array(frame,column)
                blocks=[];failures=[]
                psd_gap=min(gap,3./float(rate))
                for bi,ix in enumerate(_blocks(t,valid,psd_gap)):
                    _check_cancel(cancel)
                    if len(ix)<2:failures.append("psd_block_shorter_than_nperseg");continue
                    block,breason=_welch_block(t[ix],values[ix],fs,p)
                    if breason:failures.append(breason);continue
                    blocks.append(block)
                    for f,power in zip(block["frequency_hz"],block["density"]):add("advanced_pupil_psd",{"block_id":bi,"eye":eye,"frequency_hz":float(f),"power_density":float(power),"unit":unit+"^2/Hz","fs_hz":fs,"frequency_resolution_hz":block["frequency_resolution_hz"],"n_original":block["n_original"],"n_grid":block["n_grid"],"effective_duration_s":block["duration_s"],"baseline_corrected":p["pupil_psd_baseline_corrected"]},pupil_common)
                    for band in p["pupil_psd_bands"]:
                        power,breason=_band_integral(block["frequency_hz"],block["density"],band["low_hz"],band["high_hz"])
                        add("advanced_pupil_bands",{"block_id":bi,"eye":eye,"band_name":band["name"],"low_hz":band["low_hz"],"high_hz":band["high_hz"],"power":power,"unit":unit+"^2","status":"complete" if power is not None else "unavailable","reason_code":breason,"effective_duration_s":block["duration_s"]},pupil_common)
                duration=sum(b["duration_s"] for b in blocks);n=sum(b["n_original"] for b in blocks)
                if not duration:blocked(family,failures[0] if failures else "psd_no_supported_blocks",pupil_common)
                elif duration/(window[1]-window[0])+1e-12<p["pupil_psd_min_coverage"]:
                    blocked(family,"psd_insufficient_original_time_coverage",pupil_common)
                else:
                    emit(family,sum(b["total_power"]*b["duration_s"] for b in blocks)/duration,unit+"^2",n=n,duration=duration,row_common=pupil_common)
                    emit(family+".frequency_resolution",fs/p["pupil_psd_nperseg"],n=n,duration=duration,row_common=pupil_common)
                    if failures:logs.append("psd_partial_blocks:"+",".join(sorted(set(failures))))
        family=_P+"blink.perclos"
        if family in families and compute_pupil_scope:
            reason=_clock_reason(config)
            if not reason and (_meta(config,"eyelid_measurement_confirmed") is not True or not _evidence(_meta(config,"eyelid_measurement_evidence"))):reason="eyelid_measurement_evidence_missing"
            kind=p["eyelid_signal_kind"];unit=_meta(config,"eyelid_unit")
            if not reason and _meta(config,"eyelid_closure_direction")!="closed_increases":reason="eyelid_closure_direction_not_confirmed"
            if not reason and window[1]-window[0]+1e-9<p["perclos_min_window_s"]:reason="perclos_window_shorter_than_product_minimum"
            exclusions=[]
            if not reason and p["perclos_exclude_blinks"]:
                has_source=bool(config.get("blink_source",config.get("event_source")) and config.get("blink_algorithm_version",config.get("event_algorithm_version")))
                if exclusion_reason or config.get("blink_source_confirmed") is not True or not has_source:reason="perclos_blink_exclusion_source_missing"
                else:exclusions=[(float(r["start_s"]),float(r["end_s"])) for _,r in exclusion_events.loc[exclusion_events["event_type"].astype(str).str.lower().eq("blink")].iterrows()]
            if not reason and kind=="continuous_fraction":
                column=p["perclos_column"]
                if unit not in {"closure_fraction","percent_closed"}:reason="continuous_eyelid_closure_unit_not_confirmed"
                elif column not in support_frame:reason="eyelid_closure_column_missing"
                elif unit=="percent_closed" and _meta(config,"eyelid_percent_conversion_confirmed") is not True:reason="eyelid_percent_conversion_not_declared"
                else:
                    closure=_array(support_frame,column)/(100. if unit=="percent_closed" else 1.)
                    valid=_original_valid(support_frame,"eyelid",[column])&(closure>=0)&(closure<=1)
                    intervals=_perclos_continuous(support_t,closure,valid,p["perclos_threshold"],support_gap,window,exclusions,cancel)
                    n=int(valid.sum())
            elif not reason:
                if unit!="closed_label":reason="categorical_eyelid_unit_not_confirmed"
                else:
                    categorical,creason=_associated(context.get("eyelid_intervals"),ids,segment_optional=True)
                    if creason:reason=creason
                    else:intervals,reason=_perclos_categorical(categorical,window,exclusions,cancel);n=len(categorical)
            if reason:blocked(family,reason,eyelid_common)
            else:
                closed=sum(r["closed_duration_s"] for r in intervals);duration=sum(r["valid_duration_s"] for r in intervals)
                coverage=duration/(window[1]-window[0])
                for r in intervals:add("advanced_perclos_intervals",{**r,"threshold":p["perclos_threshold"],"closure_direction":"closed_increases","blink_exclusion":p["perclos_exclude_blinks"],"coverage":coverage,"window_start_s":window[0],"window_end_s":window[1]},eyelid_common)
                if duration<=0 or coverage+1e-12<p["perclos_min_coverage"]:blocked(family,"perclos_insufficient_valid_time_coverage",eyelid_common)
                else:
                    window_times=_array(frame,"time_s")
                    n_window=int(np.sum((window_times>=window[0])&(window_times<window[1])))
                    emit(family,closed/duration,n=n,duration=duration,row_common=eyelid_common,n_window=n_window,n_support=n)
                    emit(family+".percentage",100*closed/duration,n=n,duration=duration,row_common=eyelid_common,n_window=n_window,n_support=n)
                    emit(family+".closed_duration",closed,n=n,duration=duration,row_common=eyelid_common,n_window=n_window,n_support=n)
                    emit(family+".valid_duration",duration,n=n,duration=duration,row_common=eyelid_common,n_window=n_window,n_support=n)
    if status=="cancelled":
        for r in rows:
            if r["value"]==r["value"]:r["status"]="partial";r["reason_code"]="analysis_cancelled_partial_result"
    if not scopes and status!="cancelled":
        for metric in sorted(selected):
            rows.append({"run_id":config["run_id"],**{k:None for k in _IDS},"pair_id":None,"metric_id":metric,"value":np.nan,"unit":_BY_ID[metric]["unit"],"status":"unavailable","reason_code":"no_analysis_scope","n_observed":0,"n_interpolated":0,"effective_duration_s":0.,"algorithm_version":ALGORITHM_VERSION,"parameter_ref":parameter_ref})
    metrics=pd.DataFrame(rows,columns=METRIC_COLUMNS)
    available=[]
    for s in _ALL_SPECS:
        scope_rows=[r for r in rows if r["metric_id"]==s["metric_id"]]
        computed=any(_finite(r["value"]) for r in scope_rows)
        reason=next((r["reason_code"] for r in scope_rows if r["reason_code"]),"method_not_selected" if not scope_rows else "ready")
        required_fields=list(s["requires_fields"])
        if (s.get("parent_metric_id") or s["metric_id"])==_P+"pupil.spectral_power":required_fields.extend(["pupil_"+p["pupil_psd_eye"],"pupil_"+p["pupil_psd_eye"]+"_valid"])
        available.append({"metric_id":s["metric_id"],"available":computed,"status":"available" if computed else "blocked","reason_code":"ready" if computed else reason,"reason":"Computed on supported observations; inspect per-row status and evidence." if computed else reason,"required_fields":required_fields,"required_metadata":s["requires_metadata"]})
    tables={"metrics":metrics,"availability":pd.DataFrame(available,columns=AVAILABILITY_COLUMNS)}
    tables.update({name:_detail_frame(name,records) for name,records in details.items()})
    if isinstance(events,pd.DataFrame):tables["source_events_raw"]=events.copy(deep=True)
    for name,table in context.items():
        if isinstance(table,pd.DataFrame):tables["source_context_"+str(name)]=table.copy(deep=True)
    if progress and status!="cancelled":progress(1.,"高级眼动方法计算完成")
    return AnalysisBundle(tables=tables,metadata={"execution_status":status,"contract_version":CONTRACT_VERSION,"algorithm_version":ALGORITHM_VERSION,
                         "metric_count":len(metrics),"parameter_ref":parameter_ref,"parameter_snapshot":_clean(snapshot),"provenance":prov,
                         "device_verified":_meta(config,"device_verified",False) is True,
                         "implementation_status":"implemented","numerical_validation_status":"analytic_and_reference_cases_passed",
                         "data_device_evidence_status":"declared_verified" if _meta(config,"device_verified",False) is True else "unverified",
                         "validation_evidence_refs":["A05:advanced-signals-golden-v1","A05:advanced-signals-golden-v2-additional","A05:advanced-signals-psd-endpoint-repair1","A05:advanced-signals-golden-v3-contract"],
                         "operational_definitions":{
                             "peak_velocity":"Adjacent angular distance divided by actual dt; mean of eligible per-event maxima. No internal filtering; input preprocessing must be archived.",
                             "peak_acceleration":"Maximum absolute scalar angular-speed derivative at speed-midpoint times, >=7 original event points. Not vector acceleration; no endpoint extrapolation.",
                             "main_sequence":"Amplitude between first/last eligible observed gaze samples; source event duration. Positive Vmax/C nonlinear least squares and duration intercept/slope; local covariance Student-t CI.",
                             "microsaccade":"EK five-point velocity; discard two block endpoints per side; sigma^2=median(v^2)-median(v)^2; candidate duration=N*dt; deterministic largest-overlap one-to-one binocular match.",
                             "pursuit":"Adjacent target-projection gain weighted by interval dt; RMS integrates squared linearly interpolated positional error exactly. Exclusions include events overlapping window even if onset is earlier.",
                             "pupil_psd":"SciPy Welch mean of modified periodograms; one-sided density in measurement_unit^2/Hz. Total/band bin integral=sum(PSD*df); low<=f<high plus final Nyquist. Lower grid rate requires declared prior anti-alias filtering below grid Nyquist. Estimate depends on window, detrend and overlap; not asserted identical to arbitrary finite-sample RMS.",
                             "perclos":">=threshold continuous closure crossings use linear support; categorical intervals union consistent explicit labels and reject contradictory overlaps. Valid eyelid time denominator, optional reliable blink exclusion. Minimum 60s is adjustable product default."},
                         "detail_table_dtypes":{name:{str(c):str(dtype) for c,dtype in table.dtypes.items()} for name,table in tables.items()},
                         "warnings":logs},logs=logs)


def compute_advanced_metrics(signals, config, events=None, context=None, cancel=None, progress=None):
    """Canonical advanced entry; cancellation is checked within long numerical loops.

    A cancellation inside a group discards that incomplete group and returns an
    explicitly cancelled, typed empty bundle. All prerequisites remain enforced.
    """
    try:
        return _compute_advanced_metrics(signals,config,events,context,cancel,progress)
    except InterruptedError:
        tables={"metrics":pd.DataFrame(columns=METRIC_COLUMNS),"availability":pd.DataFrame(columns=AVAILABILITY_COLUMNS)}
        tables.update({name:_detail_frame(name,[]) for name in _DETAIL_SCHEMAS})
        return AnalysisBundle(tables,{"execution_status":"cancelled","contract_version":CONTRACT_VERSION,"algorithm_version":ALGORITHM_VERSION,"metric_count":0,"implementation_status":"implemented","numerical_validation_status":"analytic_and_reference_cases_passed"},["advanced analysis cancelled during numerical work; incomplete group discarded"])
