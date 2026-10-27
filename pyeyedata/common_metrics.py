"""Explicit event distributions, fixation AOI/reading measures and observed hit rates.

Sample-based legacy AOI visits are deliberately not reused as fixations. Helpers
import the individual engine lazily to share its source, scope and geometry gates.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

VERSION = "common-metrics-1.0.0"
EVIDENCE = "gui/diagnostic_common_metrics.py"
READING_PREFIX = "individual.aoi.reading."
AOI_MEASURES = {
    "fixation_ttff": ("AOI 首次注视潜伏期", "s"),
    "fixation_first_duration": ("AOI 首个注视时长", "s"),
    "fixation_total_duration": ("AOI 总注视时长", "s"),
    "fixation_mean_duration": ("AOI 平均注视时长", "s"),
    "fixations_before_first": ("首次注视 AOI 前注视次数", "count"),
    "hit": ("AOI 注视命中（单次观察）", "proportion"),
    "trial_hit_rate": ("AOI 已观察试次命中率", "proportion"),
    "trial_miss_rate": ("AOI 已观察试次未命中率", "proportion"),
    "participant_hit_rate": ("AOI 已观察被试命中比例", "proportion"),
}
READING_MEASURES = {
    "first_fixation_duration": ("阅读首遍首个注视时长", "s"),
    "first_pass_duration": ("首遍阅读时间", "s"),
    "single_fixation_duration": ("阅读单次注视时长", "s"),
    "skipping": ("首遍跳读（单次观察）", "proportion"),
    "rereading_duration": ("再阅读时间（总时长减首遍）", "s"),
    "go_past_duration": ("回视路径时间 Go-past", "s"),
    "first_pass_regression": ("首遍回视（单次观察）", "proportion"),
    "skipping_rate": ("已观察试次首遍跳读率", "proportion"),
    "first_pass_regression_rate": ("已观察试次首遍回视率", "proportion"),
}
_SUMMARY_LABELS = {"median": "中位数", "sd": "标准差", "iqr": "IQR",
                   "p10": "P10", "p25": "P25", "p75": "P75", "p90": "P90"}


def metric_definitions() -> list[dict[str, Any]]:
    definitions = []
    def add(metric_id, label, category, unit, metadata, note):
        definitions.append(dict(metric_id=metric_id, label=label, category=category,
            unit=unit, fields=("time_s", "gaze_x", "gaze_y", "gaze_valid"),
            metadata=tuple(metadata), implemented=True, validated=True,
            algorithm=VERSION, method_version=VERSION,
            validation_status="targeted_software_validation", evidence_ref=EVIDENCE,
            refs=("common-eye-metrics:v1",), note=note))
    for event_type, name in (("fixation", "注视"), ("saccade", "扫视"), ("blink", "眨眼")):
        summaries = [key for key in _SUMMARY_LABELS if key != "median" or event_type != "fixation"]
        for key in summaries:
            add(f"individual.{event_type}.duration.{key}", name+"时长"+_SUMMARY_LABELS[key],
                event_type, "s", ("time_confirmed", f"qualified_{event_type}_events"),
                "等权事件分布；时长为事件与当前半开窗的交集，跨窗事件保留原边界并标为 partial；SD ddof=1，分位数 linear。")
    for measure, name, unit, typ in (("saccade.amplitude", "扫视振幅", "coordinate_unit", "saccade"),
                                     ("blink.interval", "眨眼起点间隔", "s", "blink")):
        for key, label in _SUMMARY_LABELS.items():
            add(f"individual.{measure}.{key}", name+label, typ, unit,
                ("time_confirmed", f"qualified_{typ}_events", *(("coordinate_unit", "coordinate_frame") if typ == "saccade" else ())),
                "窗内起点事件；振幅使用同坐标系完整事件起终点的欧氏距离；眨眼间隔为相邻可靠事件起点之差。SD ddof=1，分位数 linear。")
    for key, (label, unit) in AOI_MEASURES.items():
        add("individual.aoi."+key, label, "aoi_fixation", unit,
            ("time_confirmed", "coordinate_unit", "coordinate_frame", "aoi_mapping", "qualified_fixation_events",
             *(("reference_event_s",) if key in {"fixation_ttff", "fixations_before_first"} else ())),
            "按可靠注视事件的原始有效连续采样时间加权中心分配静态 AOI；未知观察不作未命中。命中率仅描述已观察单位，分母/未知数及来源见 aoi_exposures 与 parameter_ref。")
    for key, (label, unit) in READING_MEASURES.items():
        add(READING_PREFIX+key, label, "reading", unit,
            ("time_confirmed", "coordinate_unit", "coordinate_frame", "aoi_mapping", "qualified_fixation_events", "reading_aoi_order", "fixation_observation_confirmed"),
            "需明确确认的 reading_annotations：reading_scope_id/stimulus_id/aoi_id/reading_order；order 按阅读推进递增（适用于 ltr/rtl）。首遍须未先看后续区域；缺少观察到的退出保留右删失。")
    return definitions


NEW_IDS = frozenset(row["metric_id"] for row in metric_definitions())
AOI_IDS = frozenset(x for x in NEW_IDS if x.startswith("individual.aoi."))
READING_RATE_IDS = frozenset(READING_PREFIX+x for x in ("skipping_rate", "first_pass_regression_rate"))
RATE_IDS = frozenset("individual.aoi."+x for x in ("trial_hit_rate", "trial_miss_rate", "participant_hit_rate")) | READING_RATE_IDS


def _reading_table(config: dict[str, Any]) -> tuple[pd.DataFrame | None, str | None]:
    table = config.get("reading_annotations")
    required = {"reading_scope_id", "stimulus_id", "aoi_id", "reading_order"}
    if config.get("reading_order_confirmed") is not True:
        return None, "reading_order_unconfirmed"
    if config.get("reading_direction") not in {"ltr", "rtl"}:
        return None, "reading_direction_missing"
    if not isinstance(config.get("reading_scope_id"), str) or not config["reading_scope_id"].strip():
        return None, "reading_scope_missing"
    if not isinstance(table, pd.DataFrame) or table.empty or not required <= set(table):
        return None, "reading_annotations_fields_missing"
    table = table.loc[table["reading_scope_id"].astype("string").eq(config["reading_scope_id"]).fillna(False)].copy()
    if table.empty or table[list(required)].isna().any().any():
        return None, "reading_annotations_scope_or_identity_missing"
    if table[["reading_scope_id", "stimulus_id", "aoi_id"]].astype("string").apply(lambda col: col.str.strip().eq("")).any().any():
        return None, "reading_annotations_blank_identity"
    if table["reading_order"].map(lambda value: isinstance(value, (bool, np.bool_))).any():
        return None, "reading_order_must_be_positive_integers"
    order = pd.to_numeric(table["reading_order"], errors="coerce")
    if not np.isfinite(order.to_numpy(dtype=float)).all() or (order <= 0).any() or (order != np.floor(order)).any():
        return None, "reading_order_must_be_positive_integers"
    identities = table[["stimulus_id", "aoi_id"]].astype("string")
    if identities.duplicated().any() or table.assign(stimulus_id=table["stimulus_id"].astype("string"), reading_order=order).duplicated(["stimulus_id", "reading_order"]).any():
        return None, "reading_order_or_aoi_duplicated"
    if "reading_direction" in table and not table["reading_direction"].astype("string").eq(config["reading_direction"]).fillna(False).all():
        return None, "reading_direction_mismatch"
    table["reading_order"] = order.astype(int)
    return table, None


def precondition(key: str, config: dict[str, Any]) -> bool | None:
    from .individual import _event_consumer_rows, _events
    cache = config.get("_aoi_common_preconditions")
    if isinstance(cache, dict) and key in cache:
        return cache[key]
    if key == "reading_aoi_order":
        value = _reading_table(config)[1] is None
        if isinstance(cache, dict):
            cache[key] = value
        return value
    if key.startswith("qualified_") and key.endswith("_events"):
        typ = key[len("qualified_"):-len("_events")]
        if typ not in {"fixation", "saccade", "blink"}:
            return False
        events = _events(config)
        eligible, _ = _event_consumer_rows(events, typ, config)
        if not eligible.empty:
            if isinstance(cache, dict):
                cache[key] = True
            return True
        types = config.get("event_source_types", ())
        declared = (isinstance(types, (list, tuple, set)) and typ in types
                    and config.get("event_source_confirmed") is True
                    and isinstance(config.get("event_source"), str) and bool(config["event_source"].strip())
                    and isinstance(config.get("event_algorithm_version"), str) and bool(config["event_algorithm_version"].strip()))
        value = bool(declared and (typ != "blink" or config.get("blink_source_confirmed") is True))
        if isinstance(cache, dict):
            cache[key] = value
        return value
    return None


def distribution(values: list[float] | np.ndarray, statistic: str) -> tuple[float | None, str | None]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values) & (values >= 0)]
    if not len(values):
        return None, "no_qualified_event_observations"
    if statistic == "sd":
        return (float(np.std(values, ddof=1)), None) if len(values) > 1 else (None, "insufficient_events_for_sample_sd")
    if statistic == "median":
        return float(np.median(values)), None
    if statistic == "iqr":
        q = np.quantile(values, [.25, .75], method="linear")
        return float(q[1]-q[0]), None
    return float(np.quantile(values, int(statistic[1:])/100.0, method="linear")), None


def _event_scope_frame(frame, base):
    additions = {column: base[key] for key, column in (("trial", "trial"), ("interaction", "interaction_id"))
                 if column not in frame and base.get(key) is not None and pd.notna(base[key])}
    if not additions:
        return frame
    scoped = frame.copy(deep=False)
    for column, value in additions.items():
        scoped[column] = value
    return scoped


def append_distributions(frame, config, prov_ok, base, start, end, requested, metrics, full_group=None):
    from .individual import _source_events_for_segment, _row, _meta_value, _event_consumer_rows, _events, INDIVIDUAL_CATALOGUE
    frame = _event_scope_frame(frame, base)
    specs = {s["metric_id"]: s for s in INDIVIDUAL_CATALOGUE}
    for typ in ("fixation", "saccade", "blink"):
        wanted = {mid for mid in requested & NEW_IDS if mid.startswith("individual."+typ+".")}
        if not wanted:
            continue
        rows = _source_events_for_segment(config, frame, typ, start, end)
        excluded_count = _unqualified_count(frame, config, typ, start, end, rows)
        _, legacy = _event_consumer_rows(_events(config), typ, config)
        durations = rows.get("duration_in_window_s", pd.Series(dtype=float)).to_numpy(dtype=float)
        onsets = rows.loc[pd.to_numeric(rows["start_s"], errors="coerce").ge(start)].copy() if not rows.empty else rows
        partial = bool(not rows.empty and ((rows["start_s"] < start) | (rows["end_s"] > end)).any())
        amplitude_values = None
        for mid in sorted(wanted):
            statistic = mid.rsplit(".", 1)[1]
            source = rows
            unit = "s"
            invalid_count = 0
            if ".amplitude." in mid:
                source = onsets
                unit = str(_meta_value(config, "coordinate_unit"))
                if amplitude_values is None:
                    amplitude_values = _amplitudes(source, frame if full_group is None else full_group, config, unit)
                values = amplitude_values
                invalid_count = int((~np.isfinite(values)).sum())
            elif ".interval." in mid:
                source = onsets
                values = np.diff(pd.to_numeric(source.get("start_s", pd.Series(dtype=float)), errors="coerce").to_numpy(dtype=float))
            else:
                values = durations
            value, reason = distribution(values, statistic)
            if reason is None:
                reason = ("legacy_event_provenance_limited" if legacy else "some_events_unqualified" if excluded_count
                          else "some_event_endpoints_unavailable" if invalid_count
                          else "event_durations_clipped_to_window" if partial and ".duration." in mid else None)
            metrics.append(_row(specs[mid], config, prov_ok, **base, value=value, reason=reason, unit=unit,
                n_obs=int(np.isfinite(values).sum()), duration=end-start,
                extra_params={"definition_version": VERSION, "weighting": "equal_events", "ddof": 1,
                    "quantile_method": "linear", "duration_rule": "positive_window_intersection",
                    "interval_rule": "onset_to_onset", "amplitude_rule": "declared_endpoints_or_continuous_original_gaze_at_source_bounds",
                    "source_event_ids": source.get("event_id", pd.Series(dtype=str)).astype(str).tolist(),
                    "invalid_endpoint_count": invalid_count, "unqualified_event_count": excluded_count}))


def _unqualified_count(frame, config, typ, start, end, eligible):
    # Include rejected records with missing scope/time evidence conservatively.
    # Strict eligible queries must not make an unbound event disappear into a
    # confirmed zero. Explicitly different identities still remain isolated.
    events = config.get("events")
    if not isinstance(events, pd.DataFrame) or events.empty:
        return 0
    if "event_type" not in events:
        return len(events)
    scoped = events.loc[events["event_type"].astype("string").str.casefold().eq(typ).fillna(False)].copy()
    identities = {key: frame.iloc[0].get(key) for key in (
        "source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id",
        "analysis_stream_id", "physical_partition_id", "timebase_id", "clock_id")
        if key in frame and pd.notna(frame.iloc[0].get(key))}
    for key, value in identities.items():
        if key in scoped:
            values = scoped[key].astype("string")
            scoped = scoped.loc[values.isna() | values.str.strip().eq("").fillna(False) | values.eq(str(value)).fillna(False)]
    starts = pd.to_numeric(scoped.get("start_s", pd.Series(np.nan, index=scoped.index)), errors="coerce")
    ends = pd.to_numeric(scoped.get("end_s", pd.Series(np.nan, index=scoped.index)), errors="coerce")
    valid = np.isfinite(starts.to_numpy(dtype=float)) & np.isfinite(ends.to_numpy(dtype=float)) & ends.gt(starts).to_numpy()
    scoped = scoped.loc[~valid | (ends.gt(start) & starts.lt(end)).to_numpy()]
    eligible_ids = set(eligible.get("event_id", pd.Series(dtype=str)).astype(str))
    return int((~scoped.get("event_id", pd.Series(None, index=scoped.index, dtype=object)).astype(str).isin(eligible_ids)).sum())


def _amplitudes(events, frame, config, unit):
    from .individual import _gap_limit_s
    times = pd.to_numeric(frame["time_s"], errors="coerce").to_numpy(dtype=float)
    support_config = {**config, "max_gap_s": _gap_limit_s(times, config)}
    result = []
    for event in events.to_dict("records"):
        endpoint_values = [event.get(key) for key in ("start_x", "start_y", "end_x", "end_y")]
        values = pd.to_numeric(pd.Series(endpoint_values), errors="coerce").to_numpy(dtype=float)
        declared_unit = event.get("coordinate_unit", event.get("unit"))
        if pd.notna(declared_unit) and str(declared_unit).strip() and str(declared_unit) != unit:
            result.append(np.nan)
        elif np.isfinite(values).all():
            result.append(float(np.hypot(values[2]-values[0], values[3]-values[1])))
        elif all(pd.isna(value) for value in endpoint_values):
            start, end = float(event["start_s"]), float(event["end_s"])
            begin = max(0, int(np.searchsorted(times, start, side="right"))-1)
            finish = min(len(times), int(np.searchsorted(times, end, side="left"))+1)
            part = frame.iloc[begin:finish]
            support, center = _observed_support(part, support_config, start, end)
            if center is not None and np.isclose(support, end-start, rtol=1e-9, atol=1e-12):
                t = times[begin:finish]
                x = np.interp([start, end], t, pd.to_numeric(part["gaze_x"]).to_numpy(dtype=float))
                y = np.interp([start, end], t, pd.to_numeric(part["gaze_y"]).to_numpy(dtype=float))
                result.append(float(np.hypot(x[1]-x[0], y[1]-y[0])))
            else:
                result.append(np.nan)
        else:
            result.append(np.nan)
    return np.asarray(result, dtype=float)


def _observed_support(frame, config, start, end):
    """Return trapezoidal support and center; do not cross invalid/gap barriers."""
    from .individual import _gaze_arrays, _gap_limit_s
    t, x, y, valid = _gaze_arrays(frame)
    for col in ("time_valid", "source_gaze_valid"):
        if col in frame:
            valid &= frame[col].fillna(False).astype(bool).to_numpy()
    for col in ("gaze_x_interpolated", "gaze_y_interpolated"):
        if col in frame:
            valid &= ~frame[col].fillna(False).astype(bool).to_numpy()
    if "source_validity_status" in frame:
        valid &= frame["source_validity_status"].astype("string").eq("valid").fillna(False).to_numpy(dtype=bool)
    if len(t) < 2 or end <= start:
        return 0.0, None
    gap = _gap_limit_s(t, config)
    dt = np.diff(t)
    left, right = np.maximum(t[:-1], start), np.minimum(t[1:], end)
    good = valid[:-1] & valid[1:] & (dt > 0) & (dt <= (gap or 0)) & (right > left)
    if "event_barrier_before" in frame:
        good &= ~frame["event_barrier_before"].iloc[1:].fillna(True).astype(bool).to_numpy()
    for col in ("physical_partition_id", "stimulus_id"):
        if col in frame:
            identities = frame[col].astype("string")
            good &= identities.iloc[:-1].reset_index(drop=True).eq(identities.iloc[1:].reset_index(drop=True)).fillna(False).to_numpy(dtype=bool)
    used = np.flatnonzero(good)
    if not len(used):
        return 0.0, None
    weights = right[used]-left[used]
    a = (left[used]-t[used])/dt[used]
    b = (right[used]-t[used])/dt[used]
    mx = x[used]+(x[used+1]-x[used])*(a+b)/2
    my = y[used]+(y[used+1]-y[used])*(a+b)/2
    total = float(weights.sum())
    return total, (float(np.dot(weights, mx)/total), float(np.dot(weights, my)/total))


def reading_measures(sequence: list[dict], target_order: int, observation_complete: bool) -> dict[str, tuple[float | None, str | None]]:
    """One explicitly ordered text sequence; durations include only fixations."""
    output = {key: (None, "no_observed_reading_sequence") for key in READING_MEASURES}
    if not sequence:
        return output
    if any(item.get("order") is None for item in sequence):
        return {key: (None, "unassigned_or_ambiguous_reading_fixation") for key in output}
    target = [i for i, item in enumerate(sequence) if item["order"] == target_order]
    later = [i for i, item in enumerate(sequence) if item["order"] > target_order]
    skipped = bool(later and (not target or later[0] < target[0]))
    if skipped:
        output["skipping"] = (1.0, None if observation_complete else "incomplete_reading_observation")
        for key in ("first_pass_duration", "first_fixation_duration", "single_fixation_duration", "first_pass_regression", "go_past_duration", "rereading_duration"):
            output[key] = (None, "skipped_on_first_pass")
        return output
    if not target:
        return {key: (None, "target_not_reached_or_observation_censored") for key in output}
    first = target[0]
    output["skipping"] = (0.0, None if observation_complete else "incomplete_reading_observation")
    exit_index = next((i for i in range(first+1, len(sequence)) if sequence[i]["order"] != target_order), None)
    progressive_exit = next((i for i in range(first+1, len(sequence)) if sequence[i]["order"] > target_order), None)
    output["first_fixation_duration"] = (float(sequence[first]["duration"]),
        "first_fixation_right_censored" if sequence[first].get("right_censored") else None)
    if exit_index is None:
        for key in ("first_pass_duration", "single_fixation_duration", "first_pass_regression", "rereading_duration"):
            output[key] = (None, "first_pass_right_censored")
    else:
        first_pass = float(sum(item["duration"] for item in sequence[first:exit_index]))
        output["first_pass_duration"] = (first_pass, None)
        output["single_fixation_duration"] = ((first_pass, None) if len(target) == 1 else (None, "multiple_target_fixations"))
        output["first_pass_regression"] = (float(sequence[exit_index]["order"] < target_order), None)
        output["rereading_duration"] = (float(sum(sequence[i]["duration"] for i in target))-first_pass,
                                         None if observation_complete else "incomplete_reading_observation")
    output["go_past_duration"] = ((float(sum(item["duration"] for item in sequence[first:progressive_exit])), None)
                                    if progressive_exit is not None else (None, "go_past_right_censored"))
    if not observation_complete:
        output = {key: (value, reason or "incomplete_reading_observation") for key, (value, reason) in output.items()}
    return output


def append_aoi_fixations(frame, full_group, aois, config, prov_ok, base, start, end, requested, metrics, audit, exposures):
    from .individual import (_source_events_for_segment, _aoi_labels, _aoi_metric_audit, _row,
                             _event_consumer_rows, _events, _gap_limit_s, INDIVIDUAL_CATALOGUE)
    wanted = requested & AOI_IDS
    if not wanted:
        return
    frame = _event_scope_frame(frame, base)
    specs = {s["metric_id"]: s for s in INDIVIDUAL_CATALOGUE}
    stimuli = frame.get("stimulus_id", pd.Series(dtype=str)).dropna().unique()
    if len(stimuli) != 1:
        for mid in sorted(wanted - RATE_IDS):
            metrics.append(_row(specs[mid], config, prov_ok, **base, reason="one_explicit_stimulus_per_exposure_required"))
        return
    stimulus = stimuli[0]
    rows = _source_events_for_segment(config, frame, "fixation", start, end)
    excluded_count = _unqualified_count(frame, config, "fixation", start, end, rows)
    _, legacy = _event_consumer_rows(_events(config), "fixation", config)
    center_records, event_records = [], []
    full_times = pd.to_numeric(full_group["time_s"], errors="coerce").to_numpy(dtype=float)
    support_config = {**config, "max_gap_s": _gap_limit_s(full_times, config)}
    for event_index, event in enumerate(rows.to_dict("records")):
        budget = config.get("_aoi_geometry_budget")
        if budget is not None and event_index % 128 == 0:
            budget.checkpoint()
        left, right = float(event["start_s"]), float(event["end_s"])
        begin = max(0, int(np.searchsorted(full_times, left, side="right"))-1)
        finish = min(len(full_times), int(np.searchsorted(full_times, right, side="left"))+1)
        support, center = _observed_support(full_group.iloc[begin:finish], support_config, left, right)
        complete = center is not None and np.isclose(support, right-left, rtol=1e-9, atol=1e-12)
        record = {**event, "center_valid": bool(complete), "labels": set(),
                  "contribution_s": float(event["duration_in_window_s"])}
        event_records.append(record)
        if complete:
            center_records.append({"gaze_x": center[0], "gaze_y": center[1], "gaze_valid": True,
                                   "time_s": left, "stimulus_id": stimulus})
    centers = pd.DataFrame(center_records, columns=["gaze_x", "gaze_y", "gaze_valid", "time_s", "stimulus_id"])
    labels, shapes, err = _aoi_labels(centers if len(centers) else frame.iloc[:1], aois, config)
    if err:
        return
    label_index = 0
    for record in event_records:
        if record["center_valid"]:
            record["labels"] = labels[label_index]
            label_index += 1
    observed, _ = _observed_support(frame.attrs.get("_window_support_frame", frame), config, start, end)
    observation_complete = bool(np.isclose(observed, end-start, rtol=1e-9, atol=1e-12))
    unknown = bool(excluded_count) or any(not record["center_valid"] for record in event_records)
    overlaps = any(float(a["end_s"]) > float(b["start_s"])+1e-12 for a, b in zip(event_records, event_records[1:]))
    unknown |= overlaps
    source_ids = [str(record["event_id"]) for record in event_records]
    read_table, read_error = _reading_table(config)
    read_stim = read_table.loc[read_table["stimulus_id"].astype("string").eq(str(stimulus))] if read_table is not None else pd.DataFrame()
    orders = {str(row["aoi_id"]): int(row["reading_order"]) for row in read_stim.to_dict("records")}
    reading_hash = hashlib.sha256(read_stim.to_json(orient="split", index=False, double_precision=15).encode()).hexdigest()
    if orders and not set(orders) <= {str(shape["aoi_id"]) for shape in shapes}:
        read_error = "reading_annotation_aoi_not_defined"
    sequence = []
    for record in event_records:
        order = orders.get(str(next(iter(record["labels"])))) if len(record["labels"]) == 1 and record["center_valid"] else None
        sequence.append({"order": order, "duration": record["contribution_s"],
                         "right_censored": float(record["end_s"]) > end})
        audit.append({**{key: value for key, value in base.items() if key != "dependence_ref"},
            "stimulus_id": stimulus, "event_id": str(record["event_id"]),
            "original_start_s": record["start_s"], "original_end_s": record["end_s"],
            "duration_in_window_s": record["contribution_s"], "aoi_ids": tuple(sorted(map(str, record["labels"]))),
            "center_valid": record["center_valid"], "reading_order": order,
            "algorithm_version": VERSION, "source_refs": record.get("source_refs"),
            "source_parameter_ref": record.get("parameter_ref"), "event_evidence": record.get("event_evidence"),
            "event_algorithm_version": record.get("algorithm_version"),
            "reason_code": "provenance_missing" if not prov_ok else "legacy_event_provenance_limited" if legacy
                else "no_continuous_original_event_support" if not record["center_valid"] else None,
            "status": "partial" if not prov_ok or legacy or not record["center_valid"] else "complete"})
    # Ignore no AOI simply because it was not fixated: every declared matching AOI is emitted.
    for shape in shapes:
        aid = shape["aoi_id"]
        aoi_audit = _aoi_metric_audit(shape)
        assigned = [record for record in event_records if aid in record["labels"]]
        onset_assigned = [record for record in assigned if float(record["start_s"]) >= start]
        count = len(assigned)
        total_duration = float(sum(record["contribution_s"] for record in assigned))
        known_miss = (observation_complete and not unknown and not legacy
                      and config.get("fixation_observation_confirmed") is True)
        hit = 1.0 if count else 0.0 if known_miss else None
        general_reason = ("ambiguous_or_unsupported_fixation_events" if unknown else "legacy_event_provenance_limited" if legacy
                          else "incomplete_aoi_observation" if not observation_complete else None)
        unit_key = {key: base.get(key) for key in ("subject", "session", "segment", "trial", "condition", "analysis_stream_id", "physical_partition_id", "segment_instance_id", "eye_label", "stream_parent_refs")}
        reading = reading_measures(sequence, orders[str(aid)], observation_complete and not unknown) if str(aid) in orders else {}
        left_censored = any(float(record["start_s"]) < start for record in event_records)
        reading_rates_known = not (read_error or overlaps or left_censored or legacy or unknown) and observation_complete
        exposures.append({**unit_key, "stimulus_id": stimulus, "aoi_id": str(aid), "hit": hit,
            "reading_skipping": reading.get("skipping", (None, None))[0] if reading_rates_known else None,
            "reading_first_pass_regression": reading.get("first_pass_regression", (None, None))[0] if reading_rates_known else None,
            "observation_complete": observation_complete,
            "reason_code": general_reason or ("fixation_observation_coverage_unconfirmed" if hit is None else None),
            "qualification_limited": legacy,
            "observed_duration_s": observed, "window_start_s": start, "window_end_s": end,
            "support_ref": base.get("support_ref"), "aoi_geometry_fingerprint": shape.get("aoi_geometry_fingerprint")})
        exposures[-1]["reading_annotations_sha256"] = reading_hash
        values = {"hit": (hit, ("legacy_event_provenance_limited" if legacy else None) if hit is not None else "aoi_hit_unknown"),
                  "fixation_total_duration": (total_duration if count or known_miss else None, general_reason),
                  "fixation_mean_duration": (total_duration/count if count else None, general_reason or "no_aoi_fixation"),
                  "fixation_first_duration": (onset_assigned[0]["contribution_s"] if onset_assigned else None, general_reason or (None if onset_assigned else "no_aoi_fixation_onset"))}
        ref = config.get("reference_event_s")
        ref_ok = isinstance(ref, (int, float)) and not isinstance(ref, bool) and np.isfinite(ref) and start <= ref < end
        after = [record for record in onset_assigned if ref_ok and float(record["start_s"]) >= ref]
        first = after[0] if after else None
        values["fixation_ttff"] = (float(first["start_s"])-float(ref) if first else None,
                                    general_reason or (None if first else "reference_outside_window_or_no_aoi_fixation"))
        values["fixations_before_first"] = (float(sum(ref <= float(record["start_s"]) < float(first["start_s"]) for record in event_records)) if first else None,
                                             general_reason or (None if first else "reference_outside_window_or_no_aoi_fixation"))
        for mid in sorted(wanted - RATE_IDS):
            if mid.startswith(READING_PREFIX):
                value, reason = reading.get(mid[len(READING_PREFIX):], (None, read_error or "aoi_not_in_reading_annotations"))
                if read_error or overlaps or left_censored or unknown or not observation_complete:
                    value, reason = None, read_error or ("overlapping_reading_fixations" if overlaps else
                        "reading_window_left_censored" if left_censored else "unqualified_or_unsupported_reading_events" if unknown else
                        "incomplete_reading_observation")
                reason = reason or ("legacy_event_provenance_limited" if legacy else None)
            else:
                value, reason = values[mid[len("individual.aoi."):]]
                if mid.endswith("fixation_first_duration") and onset_assigned and float(onset_assigned[0]["end_s"]) > end:
                    reason = reason or "first_fixation_right_censored"
            metrics.append(_row(specs[mid], config, prov_ok, **base, value=value, reason=reason,
                n_obs=count, duration=observed, extra_params={**aoi_audit, "definition_version": VERSION,
                    "assignment": "time_weighted_original_valid_event_center", "source_event_ids": source_ids,
                    "reference_event_s": ref if ref_ok else None, "window_start_s": start, "window_end_s": end,
                    "observed_duration_s": observed, "unknown_event_count": excluded_count+sum(not r["center_valid"] for r in event_records),
                    "reading_direction": config.get("reading_direction"), "reading_scope_id": config.get("reading_scope_id"),
                    "reading_order": orders, "reading_annotations_sha256": reading_hash,
                    "rereading_definition": "total_target_fixation_duration_minus_first_pass"}))


def append_hit_rates(exposures, config, prov_ok, requested, metrics):
    from .individual import _row, INDIVIDUAL_CATALOGUE
    wanted = requested & RATE_IDS
    if not wanted or not exposures:
        return
    specs = {s["metric_id"]: s for s in INDIVIDUAL_CATALOGUE}
    frame = pd.DataFrame(exposures)
    strata = ["session", "segment", "condition", "stimulus_id", "aoi_id", "analysis_stream_id", "aoi_geometry_fingerprint"]
    for identity, rows in frame.groupby(strata, dropna=False, sort=False):
        stratum = dict(zip(strata, identity))
        for mid in sorted(wanted):
            is_participant = mid.endswith("participant_hit_rate")
            is_reading = mid in READING_RATE_IDS
            groups = [(None, rows)] if is_participant else list(rows.groupby("subject", dropna=False, sort=False))
            for subject, subject_rows in groups:
                unit_cols = ["subject"] if is_participant else ["subject", "trial"]
                missing_trials = not is_participant and (subject_rows["trial"].isna() | subject_rows["trial"].astype("string").str.strip().eq("")).any()
                unit_hits, refs = [], []
                for unit, unit_rows in subject_rows.groupby(unit_cols, dropna=False, sort=False):
                    column = ("reading_skipping" if mid.endswith("skipping_rate") else "reading_first_pass_regression") if is_reading else "hit"
                    values = unit_rows.get(column, pd.Series([None]*len(unit_rows))).tolist()
                    # Duplicate physical partitions/overlapping labels count once per named unit.
                    hit = 1 if any(pd.notna(v) and v == 1 for v in values) else (0 if all(pd.notna(v) and v == 0 for v in values) else None)
                    if is_reading:
                        known_values = {v for v in values if pd.notna(v)}
                        hit = next(iter(known_values)) if len(known_values) == 1 and all(pd.notna(v) for v in values) else None
                    unit_hits.append(hit)
                    identity_values = list(unit) if isinstance(unit, tuple) else [unit]
                    refs.append({"unit": [None if pd.isna(value) else value for value in identity_values],
                                 "support_refs": unit_rows["support_ref"].dropna().astype(str).unique().tolist(), "hit": hit})
                known = [value for value in unit_hits if value is not None]
                unknown = len(unit_hits)-len(known)
                numerator = sum(value == (0 if mid.endswith("trial_miss_rate") else 1) for value in known)
                value = numerator/len(known) if known and not missing_trials else None
                reason = ("explicit_trial_ids_required" if missing_trials else "no_known_aoi_exposures" if not known
                          else "unknown_aoi_exposures_excluded" if unknown else None)
                if reason is None and subject_rows.get("qualification_limited", pd.Series(dtype=bool)).fillna(False).astype(bool).any():
                    reason = "legacy_event_provenance_limited"
                payload = {**stratum, "subject": subject, "units": refs}
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
                fields = {key: stratum[key] if pd.notna(stratum[key]) else None for key in ("session", "segment", "condition", "analysis_stream_id")}
                eyes = subject_rows.get("eye_label", pd.Series(dtype=str)).dropna().astype(str).unique()
                parent_refs = {str(ref) for value in subject_rows.get("stream_parent_refs", pd.Series(dtype=object))
                               if isinstance(value, (list, tuple)) for ref in value}
                metrics.append(_row(specs[mid], config, prov_ok, subject=subject, **fields, value=value,
                    reason=reason, n_obs=len(known), support_ref="observed-aoi-units:"+digest,
                    eye_label=str(eyes[0]) if len(eyes) == 1 else None, stream_parent_refs=tuple(sorted(parent_refs)) or None,
                    dependence_ref=tuple(sorted({str(ref) for ref in subject_rows["support_ref"].dropna()})),
                    extra_params={"definition_version": VERSION, "aoi_id": stratum["aoi_id"],
                        "stimulus_id": stratum["stimulus_id"], "aoi_geometry_fingerprint": stratum["aoi_geometry_fingerprint"],
                        "denominator_scope": "known_observed_units_only", "unit": "participant" if is_participant else "trial",
                        "numerator": numerator, "denominator": len(known), "unknown_units": unknown,
                        "observed_units": len(unit_hits), "units": refs, "unobserved_units_inferred": False,
                        "reading_scope_id": config.get("reading_scope_id") if is_reading else None,
                        "reading_direction": config.get("reading_direction") if is_reading else None,
                        "reading_annotation_hashes": sorted(set(subject_rows.get("reading_annotations_sha256", pd.Series(dtype=str)).dropna())) if is_reading else []}))
