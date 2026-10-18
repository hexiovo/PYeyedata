"""N-person eye-tracking comparison, similarity, and lag methods.

Pairs are formed only within an explicit interaction_id. Comparisons use source
observations (never interpolated points), confirmed clock/coordinate semantics,
and deterministic pair IDs composed from explicit participant IDs. R2 methods are
kept experimental until explicitly enabled and remain labelled partial.
"""
from __future__ import annotations

from itertools import combinations
import hashlib
import importlib.util
import json
import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from .aoi_geometry import AOI_DOMAIN_SCHEMA_VERSION, AOI_GEOMETRY_VERSION, AOIGeometryBudget, AOIGeometryError, PreparedStaticAOIs
from .contracts import AnalysisBundle, ContractError, METRIC_COLUMNS, AVAILABILITY_COLUMNS
from .event_queries import onset_events
from .methods.scanpath import (
    fixation_vectors,
    multimatch_dimensions,
    needleman_wunsch_score,
    scanmatch_cell_tokens,
)
from .individual import (
    ALGORITHM_VERSION as INDIVIDUAL_VERSION,
    _aoi_audit_scalar,
    _aoi_labels,
    _prepare_aoi_geometry_for_frame,
    _field_condition,
    _finite_number,
    _extension_field_context,
    _flag_mask,
    _gap_limit_s,
    _interp_mask,
    _meta_value,
    _metadata,
    _nested_meta,
    _param_ref,
    _provenance,
    _time_weighted_mean_values,
    _event_consumer_rows,
)

ALGORITHM_VERSION = "group-0.1.0"
R2_RESOURCE_LIMITS = {
    "max_pairwise_cells": 1_000_000,
    "max_sequence_samples": 10_000,
}
_SEGMENT_START_COLUMN = "__pyeyedata_segment_start_s"
_SEGMENT_END_COLUMN = "__pyeyedata_segment_end_s"


def _spec(metric_id: str, label: str, unit: str, fields: tuple[str, ...] = (),
          metadata: tuple[str, ...] = (), tier: str = "R1", implemented: bool = True,
          algorithm: str = "group-descriptive-v1", note: str = "",
          method_version: str | None = None) -> dict[str, Any]:
    return {"metric_id":metric_id,"label":label,"category":"group","unit":unit,
            "algorithm_id":algorithm,"algorithm_version":method_version or ALGORITHM_VERSION,
            "requires_fields":fields,"requires_metadata":metadata,
            "analysis_modes":("group",),"reference_ids":("PRD-v0.9:8-9",),
            "tier":tier,"implemented":implemented,"experimental":tier=="R2","note":note}


GROUP_CATALOGUE: tuple[dict[str, Any], ...] = (
    _spec("group.zero_lag_correlation", "零延迟相关", "r", ("time_s",), ("time_confirmed","synchronization_confirmed")),
    _spec("group.mean_gaze_distance", "平均注视距离", "coordinate_unit", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","coordinate_unit","coordinate_frame","cross_subject_spatial_comparable","synchronization_confirmed")),
    _spec("group.co_gaze_proportion", "阈内共同注视比例", "proportion", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","coordinate_unit","coordinate_frame","cross_subject_spatial_comparable","synchronization_confirmed","co_gaze_threshold")),
    _spec("group.shared_aoi_proportion", "共同 AOI 比例", "proportion", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","aoi_mapping","coordinate_unit","coordinate_frame","synchronization_confirmed"), method_version=AOI_GEOMETRY_VERSION),
    _spec("group.aoi_agreement", "AOI 序列一致率", "proportion", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","aoi_mapping","coordinate_unit","coordinate_frame","synchronization_confirmed"), method_version=AOI_GEOMETRY_VERSION),
    _spec("group.transition_similarity", "AOI 转移相似性", "similarity", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","aoi_mapping","coordinate_unit","coordinate_frame"), "R2", True, "jensen_shannon_transition-v1", "No pseudocounts; rows without observed outgoing transitions are omitted and coverage is reported.", method_version=AOI_GEOMETRY_VERSION),
    _spec("group.pupil_synchrony", "瞳孔同步", "r", ("time_s",), ("time_confirmed","synchronization_confirmed","pupil_unit","pupil_baseline_window")),
    _spec("group.fixation_cooccurrence", "注视事件共现", "proportion", (), ("time_confirmed","reliable_events","synchronization_confirmed","event_match_tolerance_s"), "R2", True, "one-to-one-onset-match-v1", "Symmetric F1-style rate: 2*matches/(events_A+events_B)."),
    _spec("group.blink_synchrony", "眨眼同步", "proportion", (), ("time_confirmed","validated_blink_events","synchronization_confirmed","event_match_tolerance_s"), "R2", True, "one-to-one-onset-match-v1", "Requires reliable blink event evidence; missing gaze is never treated as a blink."),
    _spec("group.lagged_correlation", "延迟相关", "r", ("time_s",), ("time_confirmed","synchronization_confirmed","lag_search")),
    _spec("group.best_lag", "最佳延迟", "s", ("time_s",), ("time_confirmed","synchronization_confirmed","lag_search")),
    _spec("group.dtw_distance", "DTW 距离", "coordinate_unit", ("time_s",), ("dtw_window","comparison_signal"), "R2", True, "sakoe_chiba-dtw-euclidean-v1", "Global path, explicit Sakoe-Chiba band, Euclidean local cost; reported cost divided by path length."),
    _spec("group.frechet_distance", "离散 Fréchet 距离", "coordinate_unit", ("gaze_x","gaze_y","gaze_valid"), ("coordinate_unit","coordinate_frame","cross_subject_spatial_comparable"), "R2", True, "discrete-frechet-v1"),
    _spec("group.hausdorff_distance", "Hausdorff 距离", "coordinate_unit", ("gaze_x","gaze_y","gaze_valid"), ("coordinate_unit","coordinate_frame","cross_subject_spatial_comparable"), "R2", True, "symmetric-discrete-hausdorff-v1"),
    _spec("group.multimatch.shape", "MultiMatch 形状相似性", "similarity", (), ("multimatch_gaze","screen_size_px","coordinate_unit","coordinate_frame"), "R2", True, "adswa-multimatch-gaze", "Uses the published five-dimension implementation; requires >=3 reliable fixation vectors."),
    _spec("group.multimatch.direction", "MultiMatch 方向相似性", "similarity", (), ("multimatch_gaze","screen_size_px","coordinate_unit","coordinate_frame"), "R2", True, "adswa-multimatch-gaze"),
    _spec("group.multimatch.length", "MultiMatch 长度相似性", "similarity", (), ("multimatch_gaze","screen_size_px","coordinate_unit","coordinate_frame"), "R2", True, "adswa-multimatch-gaze"),
    _spec("group.multimatch.position", "MultiMatch 位置相似性", "similarity", (), ("multimatch_gaze","screen_size_px","coordinate_unit","coordinate_frame"), "R2", True, "adswa-multimatch-gaze"),
    _spec("group.multimatch.duration", "MultiMatch 时长相似性", "similarity", (), ("multimatch_gaze","screen_size_px","coordinate_unit","coordinate_frame"), "R2", True, "adswa-multimatch-gaze"),
    _spec("group.scanmatch.score", "ScanMatch 序列相似性", "score", ("gaze_x","gaze_y","gaze_valid","time_s"), ("scanmatch_parameters","screen_size_px","coordinate_unit"), "R2", True, "global-needleman-wunsch-v1", "Uses explicit spatial grid, time bins, symmetric substitution matrix, and gap penalty."),
    _spec("group.crqa.recurrence_rate", "CRQA 复现率 RR", "proportion", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.crqa.determinism", "CRQA 确定性 DET", "proportion", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.crqa.mean_diagonal_length", "CRQA 平均对角线长度 L", "samples", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.crqa.max_diagonal_length", "CRQA 最大对角线长度 Lmax", "samples", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.crqa.diagonal_entropy", "CRQA 对角线熵 ENTR", "bit", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.crqa.laminarity", "CRQA 层流性 LAM", "proportion", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.crqa.trapping_time", "CRQA 陷获时间 TT", "samples", ("time_s",), ("crqa_parameters","comparison_signal"), "R2", True, "cross-recurrence-v1"),
    _spec("group.windowed_synchrony", "分窗同步", "r", ("time_s",), ("time_confirmed","synchronization_confirmed","window_parameters")),
    _spec("group.mean_pairwise_similarity", "组内两两相似性均值", "r", ("time_s",), ("time_confirmed","synchronization_confirmed")),
    _spec("group.dispersion", "组内空间离散度", "coordinate_unit", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","coordinate_unit","coordinate_frame","cross_subject_spatial_comparable","synchronization_confirmed")),
    _spec("group.all_member_aoi_agreement", "全体共同 AOI 一致率", "proportion", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","aoi_mapping","coordinate_unit","coordinate_frame","synchronization_confirmed"), method_version=AOI_GEOMETRY_VERSION),
    _spec("group.majority_aoi_agreement", "多数 AOI 共识", "proportion", ("gaze_x","gaze_y","gaze_valid","time_s"), ("time_confirmed","aoi_mapping","coordinate_unit","coordinate_frame","synchronization_confirmed","majority_parameters"), "R2", True, "majority-aoi-v1", "Reports n(t) and uses only timestamps with at least k valid members.", method_version=AOI_GEOMETRY_VERSION),
    _spec("group.network.edge", "相似网络边", "r", ("time_s",), ("time_confirmed","synchronization_confirmed","cross_subject_spatial_comparable","network_signal","network_threshold"), "R2", True, "thresholded-undirected-network-v1"),
    _spec("group.network.strength", "相似网络节点强度", "count", ("time_s",), ("time_confirmed","synchronization_confirmed","cross_subject_spatial_comparable","network_signal","network_threshold"), "R2", True, "thresholded-undirected-network-v1"),
    _spec("group.role_following.max_correlation", "指定角色跟随相关峰", "r", ("time_s",), ("time_confirmed","synchronization_confirmed","explicit_roles","lag_search")),
    _spec("group.role_following.best_lag", "指定角色跟随延迟", "s", ("time_s",), ("time_confirmed","synchronization_confirmed","explicit_roles","lag_search")),
)


def get_group_catalogue() -> list[dict[str, Any]]:
    return [{**s,"requires_fields":list(s["requires_fields"]),"requires_metadata":list(s["requires_metadata"]),"analysis_modes":list(s["analysis_modes"]),"reference_ids":list(s["reference_ids"])} for s in GROUP_CATALOGUE]


def _meta_ok(key: str, config: dict[str, Any]) -> bool:
    if key == "time_confirmed":
        return _meta_value(config,"time_unit")=="s" and _meta_value(config,"time_confirmed","time_source_confirmed") is True
    if key == "synchronization_confirmed":
        s=config.get("synchronization",{})
        if not isinstance(s,dict) or s.get("confirmed") is not True or not str(s.get("method","")).strip(): return False
        if not isinstance(s.get("source_refs"),(list,tuple)) or not s["source_refs"]: return False
        offsets=_nested_meta(config).get("clock_offsets_s",{})
        return not offsets or config.get("clock_offsets_applied") is True
    if key in {"coordinate_unit","pupil_unit"}:
        v=_meta_value(config,key)
        return isinstance(v,str) and bool(v.strip()) and v.lower()!="unknown"
    if key == "coordinate_frame":
        v=_meta_value(config,key)
        return isinstance(v,str) and bool(v.strip()) and v.lower()!="unknown"
    if key == "cross_subject_spatial_comparable": return config.get(key) is True
    if key == "aoi_mapping": return config.get("aoi_mapping_confirmed") is True
    if key == "co_gaze_threshold": return _finite_number(config.get("co_gaze_threshold")) and float(config["co_gaze_threshold"])>0
    if key == "pupil_baseline_window":
        w=config.get("pupil_baseline_window_s")
        return isinstance(w,(list,tuple)) and len(w)==2 and all(_finite_number(x) for x in w) and float(w[0])<float(w[1])
    if key in {"reliable_events","validated_blink_events"}:
        ev=config.get("events")
        if not isinstance(ev,pd.DataFrame) or ev.empty: return False
        event_type="fixation" if key=="reliable_events" else "blink"
        eligible,_=_event_consumer_rows(ev,event_type,config)
        return not eligible.empty
    if key == "event_match_tolerance_s": return _finite_number(config.get("event_match_tolerance_s")) and float(config["event_match_tolerance_s"])>=0
    if key == "lag_search":
        return all(_finite_number(config.get(k)) for k in ("lag_max_s",)) and float(config["lag_max_s"])>0
    if key == "dtw_window": return _finite_number(config.get("dtw_window_fraction")) and 0<=float(config["dtw_window_fraction"])<=1
    if key == "comparison_signal":
        signal=config.get("comparison_signal")
        if signal in {"gaze_xy","gaze_x","gaze_y"}:
            return _meta_ok("coordinate_unit",config) and _meta_ok("coordinate_frame",config) and _meta_ok("cross_subject_spatial_comparable",config)
        if signal in {"pupil_left","pupil_right"}:
            return _meta_ok("pupil_unit",config) and config.get("cross_subject_pupil_comparable") is True
        return False
    if key == "multimatch_gaze":
        return importlib.util.find_spec("multimatch_gaze") is not None
    if key == "screen_size_px":
        v=config.get("screen_size_px")
        return isinstance(v,(list,tuple)) and len(v)==2 and all(_finite_number(x) and float(x)>0 for x in v)
    if key == "scanmatch_parameters":
        p=config.get("scanmatch")
        if not isinstance(p,dict):return False
        cols,rows=p.get("grid_cols"),p.get("grid_rows");matrix=p.get("substitution_matrix")
        return (isinstance(cols,(int,np.integer)) and not isinstance(cols,bool) and cols>0
                and isinstance(rows,(int,np.integer)) and not isinstance(rows,bool) and rows>0
                and _finite_number(p.get("time_bin_s")) and float(p["time_bin_s"])>0
                and isinstance(matrix,dict) and bool(matrix) and all(isinstance(k,str) and _finite_number(v) for k,v in matrix.items())
                and _finite_number(p.get("gap_penalty")) and float(p["gap_penalty"])<=0)
    if key == "crqa_parameters":
        p=config.get("crqa")
        if not isinstance(p,dict):return False
        integer_keys=("embedding_dimension","embedding_lag_samples","min_diagonal_line","min_vertical_line")
        return (all(isinstance(p.get(k),(int,np.integer)) and not isinstance(p.get(k),bool) and p[k]>=1 for k in integer_keys)
                and _finite_number(p.get("threshold")) and float(p["threshold"])>0
                and ("theiler_window_samples" not in p or (isinstance(p["theiler_window_samples"],(int,np.integer)) and not isinstance(p["theiler_window_samples"],bool) and p["theiler_window_samples"]>=0)))
    if key == "window_parameters": return all(_finite_number(config.get(k)) and float(config[k])>0 for k in ("window_length_s","window_step_s"))
    if key == "majority_parameters":
        minimum=config.get("majority_min_members")
        return (_finite_number(config.get("majority_fraction")) and 0<float(config["majority_fraction"])<=1
                and isinstance(minimum,(int,np.integer)) and not isinstance(minimum,bool) and minimum>=2)
    if key == "network_threshold": return _finite_number(config.get("network_threshold"))
    if key == "network_signal": return config.get("network_signal") in {"gaze_x","gaze_y"}
    if key == "explicit_roles":
        roles=config.get("roles")
        return isinstance(roles,dict) and len(roles)>=2 and all(v in {"leader","follower"} for v in roles.values()) and list(roles.values()).count("leader")==1
    return config.get(key) is True or _metadata(config).get(key) is True


def _available(spec: dict[str, Any], frame: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    fields_ok, field_reason=_field_condition(frame,spec["requires_fields"])
    if "comparison_signal" in spec["requires_metadata"]:
        signal_fields={"gaze_xy":("gaze_x","gaze_y","gaze_valid"),"gaze_x":("gaze_x","gaze_valid"),"gaze_y":("gaze_y","gaze_valid"),"pupil_left":("pupil_left","pupil_left_valid"),"pupil_right":("pupil_right","pupil_right_valid")}
        required_signal_fields=signal_fields.get(config.get("comparison_signal"),())
        absent=[name for name in required_signal_fields if name not in frame]
        if absent:
            fields_ok=False
            field_reason="; ".join(x for x in (field_reason,"missing_comparison_signal_fields:"+",".join(absent)) if x)
    meta_missing=[k for k in spec["requires_metadata"] if not _meta_ok(k,config)]
    if not fields_ok or meta_missing:
        return {"metric_id":spec["metric_id"],"available":False,"status":"blocked","reason_code":"missing_precondition",
                "reason":"; ".join(x for x in (field_reason,"missing_metadata:"+",".join(meta_missing) if meta_missing else None) if x),
                "required_fields":list(spec["requires_fields"]),"required_metadata":list(spec["requires_metadata"])}
    if spec["experimental"] and config.get("enable_experimental") is not True:
        return {"metric_id":spec["metric_id"],"available":False,"status":"experimental","reason_code":"experimental_opt_in_required",
                "reason":spec.get("note") or "R2 研究方法须显式启用并按方法审查。",
                "required_fields":list(spec["requires_fields"]),"required_metadata":list(spec["requires_metadata"])}
    if not spec["implemented"]:
        return {"metric_id":spec["metric_id"],"available":False,"status":"unavailable","reason_code":"method_not_implemented",
                "reason":spec.get("note") or "当前版本没有已验证实现。",
                "required_fields":list(spec["requires_fields"]),"required_metadata":list(spec["requires_metadata"])}
    return {"metric_id":spec["metric_id"],"available":True,"status":"experimental" if spec["experimental"] else "available",
            "reason_code":"experimental_method" if spec["experimental"] else "ready",
            "reason":spec.get("note") or "方法实现与数据条件满足。",
            "required_fields":list(spec["requires_fields"]),"required_metadata":list(spec["requires_metadata"])}


def _metric(spec: dict[str, Any], config: dict[str, Any], provenance_ok: bool,
            *, value: Any=None, unit: str|None=None, reason: str|None=None,
            subject_id: Any=None, session_id: Any=None, interaction_id: Any=None,
            pair_id: Any=None, segment_id: Any=None, trial: Any=None, condition: Any=None,
            n_obs: int=0, n_interp: int=0, duration: float|None=None,
            analysis_stream_id: str|None=None, eye_label: str|None=None,
            stream_parent_refs: tuple[str,...]|None=None, segment_instance_id: str|None=None,
            physical_partition_id: str|None=None, support_ref: str|None=None,
            dependence_ref: tuple[str,...]|str|None=None,
            n_window_observed: int|None=None,n_support_observed: int|None=None,
            extra: dict[str,Any]|None=None) -> dict[str,Any]:
    numeric=value is not None and _finite_number(value)
    experimental=bool(spec["experimental"])
    complete=numeric and provenance_ok and not experimental and reason is None
    status="complete" if complete else ("partial" if numeric else "unavailable")
    code=None if complete else (reason or ("experimental_method" if experimental and numeric else "provenance_missing" if numeric and not provenance_ok else "precondition_not_met"))
    return {"run_id":config.get("run_id"),"subject_id":subject_id,"session_id":session_id,
            "interaction_id":interaction_id,"pair_id":pair_id,"segment_id":segment_id,
            "trial":trial,"condition":condition,"metric_id":spec["metric_id"],
            "value":float(value) if numeric else np.nan,"unit":unit or spec["unit"],"status":status,
            "reason_code":code,"n_observed":int(n_obs),"n_interpolated":int(n_interp),
            "effective_duration_s":float(duration) if _finite_number(duration) else np.nan,
            "algorithm_version":spec["algorithm_version"],"parameter_ref":_param_ref(config,extra),
            "analysis_stream_id":analysis_stream_id,"eye_label":eye_label,
            "stream_parent_refs":stream_parent_refs,"segment_instance_id":segment_instance_id,
            "physical_partition_id":physical_partition_id,"support_ref":support_ref,
            "dependence_ref":dependence_ref,
            "n_window_observed":int(n_obs if n_window_observed is None else n_window_observed),
            "n_support_observed":n_support_observed}


def _resource_limit_row(metric_id: str, requested: set[str], metrics: list[dict[str,Any]],
                       config: dict[str,Any], provenance_ok: bool,
                       pair_common: dict[str,Any], seq_a: np.ndarray,
                       seq_b: np.ndarray) -> None:
    if metric_id not in requested:
        return
    spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]==metric_id)
    metrics.append(_metric(spec,config,provenance_ok,value=None,reason="resource_limit",
                           **pair_common,n_obs=min(len(seq_a),len(seq_b)),
                           extra={"sequence_lengths":[len(seq_a),len(seq_b)],
                                  "estimated_pairwise_cells":int(len(seq_a)*len(seq_b)),
                                  "resource_limits":dict(R2_RESOURCE_LIMITS),
                                  "truncation":"none"}))


def _pair_id(a: Any, b: Any) -> str:
    vals=sorted((str(a),str(b)))
    return "pair:"+json.dumps(vals,ensure_ascii=False,separators=(",",":"))


def _prepare_segments(signals: pd.DataFrame, segments: pd.DataFrame|None,
                      overlap_policy: str|None=None) -> pd.DataFrame:
    frame=signals.copy()
    if segments is None or segments.empty:
        if "segment_id" not in frame: frame["segment_id"]=None
        return frame
    if _SEGMENT_START_COLUMN in frame or _SEGMENT_END_COLUMN in frame:
        raise ContractError("signals use reserved internal segment-boundary columns")
    need={"subject_id","session_id","segment_id","start_s","end_s"}
    if not need.issubset(segments.columns):
        raise ContractError("segments missing required columns: "+", ".join(sorted(need-set(segments.columns))))
    if "interaction_id" in segments and segments["interaction_id"].isna().any():
        raise ContractError("segment interaction_id must be explicit when the column is present")
    bounds: list[tuple[Any,dict[str,Any],float,float]] = []
    for row_index, segment_row in segments.iterrows():
        try:
            start, end = float(segment_row["start_s"]), float(segment_row["end_s"])
        except (TypeError, ValueError):
            raise ContractError("segment boundaries must be finite numeric seconds") from None
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ContractError("segments must be nonempty finite left-closed, right-open intervals")
        bounds.append((row_index,segment_row.to_dict(),start,end))
    dependency_by_row:dict[Any,set[str]]={row_index:set() for row_index,_,_,_ in bounds}
    for pos,(row_a,seg_a,start_a,end_a) in enumerate(bounds):
        for row_b,seg_b,start_b,end_b in bounds[pos+1:]:
            keys=("subject_id","session_id","interaction_id","trial","stimulus_id","source_id",
                  "analysis_stream_id","timebase_id","clock_id","physical_partition_id")
            same_scope=True
            for key in keys:
                left,right=seg_a.get(key),seg_b.get(key)
                if left is not None and right is not None and pd.notna(left) and pd.notna(right) and str(left)!=str(right):
                    same_scope=False;break
            overlap_start,overlap_end=max(start_a,start_b),min(end_a,end_b)
            if not same_scope or overlap_end<=overlap_start:continue
            rows=segments.loc[[row_a,row_b]]
            policies=rows.get("overlap_policy",pd.Series(index=rows.index,dtype="object"))
            keep_labels=(str(overlap_policy).casefold()=="keep_labels" if overlap_policy is not None
                         else len(policies)==2 and policies.astype("string").str.casefold().eq("keep_labels").all())
            if not keep_labels:
                raise ContractError("overlapping segment membership requires explicit overlap_policy='keep_labels'")
            instances=sorted(str(seg.get("segment_instance_id") if pd.notna(seg.get("segment_instance_id")) else seg.get("segment_id")) for seg in (seg_a,seg_b))
            overlap_ref="segment-overlap:"+hashlib.sha256(json.dumps(
                {"instances":instances,"start_s":overlap_start,"end_s":overlap_end},
                sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
            dependency_by_row[row_a].add(overlap_ref);dependency_by_row[row_b].add(overlap_ref)
    pieces=[]
    for row_index,s in segments.iterrows():
        m=(frame["subject_id"]==s["subject_id"])&(frame["session_id"]==s["session_id"])&(frame["time_s"]>=float(s["start_s"]))&(frame["time_s"]<float(s["end_s"]))
        for key in ("interaction_id","trial","stimulus_id","source_id","analysis_stream_id",
                    "timebase_id","clock_id","physical_partition_id"):
            if key in segments.columns and key in frame and pd.notna(s.get(key)):
                m &= frame[key].astype("string").eq(str(s.get(key))).fillna(False)
            elif key in segments.columns and key in frame and pd.isna(s.get(key)) and key=="interaction_id":
                raise ContractError("segment interaction_id must be explicit when the column is present")
            elif key=="interaction_id" and key in frame:
                candidate_interactions=frame.loc[m,key].dropna().unique()
                if len(candidate_interactions)>1: m &= False
        part=frame.loc[m].copy()
        if part.empty: continue
        part["segment_id"]=s["segment_id"]
        if "segment_instance_id" in s and pd.notna(s.get("segment_instance_id")):
            part["segment_instance_id"]=s.get("segment_instance_id")
        part["dependence_ref"]=[tuple(sorted(dependency_by_row.get(row_index,set()))) or None]*len(part)
        part[_SEGMENT_START_COLUMN]=float(s["start_s"])
        part[_SEGMENT_END_COLUMN]=float(s["end_s"])
        for c in ("condition","trial"):
            if c in s and pd.notna(s[c]): part[c]=s[c]
        pieces.append(part)
    return pd.concat(pieces,ignore_index=True) if pieces else frame.iloc[0:0].copy()


def _valid_signal(frame: pd.DataFrame, signal: str) -> tuple[np.ndarray,np.ndarray,str]:
    if signal=="gaze_x": col,vc="gaze_x","gaze_valid"
    elif signal=="gaze_y": col,vc="gaze_y","gaze_valid"
    elif signal=="pupil_left": col,vc="pupil_left","pupil_left_valid"
    elif signal=="pupil_right": col,vc="pupil_right","pupil_right_valid"
    else: raise ValueError(signal)
    if col not in frame: return np.full(len(frame),np.nan),np.zeros(len(frame),bool),col
    val=pd.to_numeric(frame[col],errors="coerce").to_numpy(dtype=float)
    valid=_flag_mask(frame,col,vc)&~_interp_mask(frame,signal if signal.startswith("pupil") else "gaze")
    return val,valid,col


def _align(a: pd.DataFrame,b: pd.DataFrame, signal: str, tolerance: float=0.0) -> dict[str,np.ndarray]:
    aa=a.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True)
    bb=b.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True)
    ta=pd.to_numeric(aa["time_s"],errors="coerce").to_numpy(dtype=float)
    tb=pd.to_numeric(bb["time_s"],errors="coerce").to_numpy(dtype=float)
    xa,va,_=_valid_signal(aa,signal); xb,vb,_=_valid_signal(bb,signal)
    # Nearest, monotone, one-to-one matching; default tolerance zero is exact support.
    ia=[]; ib=[]; ts=[]; j0=0
    for i,t in enumerate(ta):
        while j0<len(tb) and tb[j0]<t-tolerance: j0+=1
        candidates=[j for j in (j0,j0+1) if j<len(tb)]
        if not candidates: continue
        j=min(candidates,key=lambda q:abs(tb[q]-t))
        if abs(tb[j]-t)<=tolerance+1e-15:
            ia.append(i);ib.append(j);ts.append((float(t)+float(tb[j]))*.5);j0=j+1
    if not ia:
        return {"ta":np.array([]),"a":np.array([]),"b":np.array([]),"va":np.array([],bool),"vb":np.array([],bool),"ia":np.array([],int),"ib":np.array([],int),"frame_a":aa,"frame_b":bb}
    idxa=np.asarray(ia,dtype=int);idxb=np.asarray(ib,dtype=int)
    return {"ta":np.asarray(ts,dtype=float),"a":xa[idxa],"b":xb[idxb],"va":va[idxa],"vb":vb[idxb],"ia":idxa,"ib":idxb,"frame_a":aa,"frame_b":bb}


def _valid_support(times: np.ndarray, mask: np.ndarray, config: dict[str,Any]) -> tuple[np.ndarray,float,float]:
    keep=np.asarray(mask,dtype=bool).copy()
    if len(times)>1:
        dt=np.diff(times); pos=dt[(dt>0)&np.isfinite(dt)]
        maxgap=_gap_limit_s(times,config,float(np.median(pos))) if len(pos) else 0
        if maxgap is None:maxgap=0
        # Intervals after a long gap are excluded; a point cannot bridge across it.
        keep[:-1]&=(dt>0)&(dt<=maxgap)
        weights=np.where(keep[:-1],dt,0.)
        duration=float(weights.sum())
        return keep,duration,maxgap
    return keep,0.,float(config.get("max_gap_s",0) or 0)


def _corr(a: np.ndarray,b: np.ndarray,method: str="pearson") -> float|None:
    if len(a)<3 or len(b)!=len(a) or not np.isfinite(a).all() or not np.isfinite(b).all() or np.ptp(a)==0 or np.ptp(b)==0: return None
    if method=="spearman":
        a=pd.Series(a).rank(method="average").to_numpy(dtype=float);b=pd.Series(b).rank(method="average").to_numpy(dtype=float)
    return float(np.corrcoef(a,b)[0,1])


def _pair_lag(a: pd.DataFrame,b: pd.DataFrame, signal: str, config: dict[str,Any]) -> tuple[dict[str,Any]|None,list[dict[str,Any]],str|None]:
    aa=a.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True)
    bb=b.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True)
    ta=pd.to_numeric(aa["time_s"],errors="coerce").to_numpy(dtype=float);tb=pd.to_numeric(bb["time_s"],errors="coerce").to_numpy(dtype=float)
    xa,va,_=_valid_signal(aa,signal);xb,vb,_=_valid_signal(bb,signal)
    if len(ta)<4 or len(tb)<4:return None,[],"insufficient_samples"
    dta=np.diff(ta);dtb=np.diff(tb);pa=dta[dta>0];pb=dtb[dtb>0]
    if not len(pa) or not len(pb):return None,[],"no_positive_sample_interval"
    step=float(config.get("lag_step_s",max(float(np.median(pa)),float(np.median(pb)))))
    resolution=max(float(np.median(pa)),float(np.median(pb)))
    if step+1e-12<resolution:return None,[],"lag_step_below_time_resolution"
    maxlag=float(config.get("lag_max_s",2.0));nstep=int(math.floor(maxlag/step+1e-12))
    lags=np.arange(-nstep,nstep+1,dtype=float)*step
    if not len(lags) or not np.any(np.isclose(lags,0)): return None,[],"invalid_lag_grid"
    tol=float(config.get("alignment_tolerance_s",0.0))
    # Use fixed A-time support valid for every candidate tau, preventing each lag
    # from selecting a different sample range. Positive tau pairs A(t) with B(t+tau).
    candidates: dict[float,dict[int,int]]={}
    for lag in lags:
        mapping={}
        for i,t in enumerate(ta):
            target=t+float(lag)
            j=int(np.searchsorted(tb,target,side="left"))
            opts=[x for x in (j-1,j) if 0<=x<len(tb)]
            if not opts: continue
            k=min(opts,key=lambda x:abs(tb[x]-target))
            if abs(tb[k]-target)<=tol+1e-12:
                mapping[i]=k
        candidates[float(lag)]=mapping
    support=[i for i in range(len(ta)) if va[i] and all(i in m and vb[m[i]] for m in candidates.values())]
    if len(support)<3:return None,[],"no_fixed_common_support"
    support_times=ta[support]
    dts=np.diff(support_times);good_dt=dts[dts>0]
    if not len(good_dt):return None,[],"zero_duration_support"
    meddt=float(np.median(good_dt));maxgap=_gap_limit_s(support_times,config,meddt) or 0.0;duration=float(np.sum(np.where((dts>0)&(dts<=maxgap),dts,0.0)))
    # PRD initial rule: at least 10 seconds and >=80% of the requested common window.
    expected=float(config.get("lag_window_s",max(ta[-1],tb[-1])-min(ta[0],tb[0])))
    if duration<float(config.get("lag_min_window_s",10.0)):
        return None,[],"window_shorter_than_minimum"
    coverage=duration/expected if expected>0 else 0.0
    if coverage<float(config.get("lag_min_coverage",0.8)):
        return None,[],"common_support_below_coverage_threshold"
    curve=[]
    for lag in lags:
        mapping=candidates[float(lag)]; ia=np.asarray(support,dtype=int); ib=np.asarray([mapping[i] for i in support],dtype=int)
        r=_corr(xa[ia],xb[ib],"pearson")
        curve.append({"lag_s":float(lag),"correlation":r,"n_observed":len(ia),"effective_duration_s":duration,"coverage":coverage})
    valid=[r for r in curve if r["correlation"] is not None]
    if not valid:return None,curve,"constant_or_invalid_series"
    best=max(r["correlation"] for r in valid)
    ties=[r for r in valid if abs(r["correlation"]-best)<=1e-12]
    ties.sort(key=lambda r:(abs(r["lag_s"]),r["lag_s"]))
    chosen=ties[0]
    chosen.update({"best_lag_s":chosen["lag_s"],"r_zero":next((r["correlation"] for r in curve if abs(r["lag_s"])<1e-12),None),"boundary_warning":abs(abs(chosen["lag_s"])-maxlag)<=step/2+1e-12,"tie_lags_s":[r["lag_s"] for r in ties],"support_start_s":float(support_times[0]),"support_end_s":float(support_times[-1]),"support_observed":len(support)})
    return chosen,curve,None


def _transition_counts(labels: list[Any]) -> dict[Any,dict[Any,int]]:
    out:dict[Any,dict[Any,int]]={}
    prev=None
    for x in labels:
        if x is None: prev=None;continue
        if prev is not None and x!=prev: out.setdefault(prev,{})[x]=out.setdefault(prev,{}).get(x,0)+1
        prev=x
    return out


def _js_transition_similarity(a: dict[Any,dict[Any,int]],b: dict[Any,dict[Any,int]]) -> tuple[float|None,dict[str,Any]]:
    rows=[]; coverage=0
    for origin in sorted(set(a)&set(b),key=str):
        va=a[origin];vb=b[origin]
        na=sum(va.values());nb=sum(vb.values())
        if na==0 or nb==0:continue
        outcomes=sorted(set(va)|set(vb),key=str)
        p=np.asarray([va.get(k,0)/na for k in outcomes],dtype=float);q=np.asarray([vb.get(k,0)/nb for k in outcomes],dtype=float);m=(p+q)/2
        def kl(x,y): return float(np.sum(x[x>0]*np.log2(x[x>0]/y[x>0])))
        js=(kl(p,m)+kl(q,m))/2
        rows.append(1-js);coverage+=na+nb
    return (float(np.mean(rows)) if rows else None,{"compared_origin_rows":len(rows),"transition_support":coverage,"similarity_definition":"1 - base2 Jensen-Shannon divergence"})


def _event_frame(events: Any, interaction: Any, member: Any, session: Any, event_type: str,
                 segment_id: Any=None, segment_start_s: float|None=None,
                 segment_end_s: float|None=None, analysis_stream_id: Any=None,
                 config: dict[str,Any]|None=None) -> pd.DataFrame:
    if not isinstance(events,pd.DataFrame) or events.empty:return pd.DataFrame()
    e=events.copy()
    eligible,_=_event_consumer_rows(e,event_type,config or {})
    if eligible.empty:return eligible
    identities={"subject_id":member,"session_id":session}
    if interaction is not None:identities["interaction_id"]=interaction
    if analysis_stream_id is not None:identities["analysis_stream_id"]=analysis_stream_id
    for key,value in identities.items():
        if key in eligible:
            eligible=eligible.loc[eligible[key].astype("string").eq(str(value)).fillna(False)]
    if segment_start_s is not None and segment_end_s is not None:
        selected=onset_events(eligible,segment_start_s,segment_end_s,identities=identities,
                              allow_unspecified_stream="analysis_stream_id" not in identities,
                              event_types=(event_type,),status_values=("reliable","complete","source_confirmed"))
        return selected.sort_values("start_s",kind="mergesort").reset_index(drop=True)
    if segment_id is not None and "segment_id" in eligible:
        eligible=eligible.loc[eligible["segment_id"].astype("string").eq(str(segment_id))]
    return eligible.sort_values("start_s",kind="mergesort").reset_index(drop=True)


def _event_match_rate(a: pd.DataFrame,b: pd.DataFrame,tolerance: float) -> tuple[float|None,int]:
    if a.empty and b.empty:return None,0
    x=a["start_s"].to_numpy(dtype=float) if not a.empty else np.array([])
    y=b["start_s"].to_numpy(dtype=float) if not b.empty else np.array([])
    i=j=n=0
    while i<len(x) and j<len(y):
        delta=x[i]-y[j]
        if abs(delta)<=tolerance:
            n+=1;i+=1;j+=1
        elif delta<0:i+=1
        else:j+=1
    return (2*n/(len(x)+len(y)) if len(x)+len(y)>0 else None),n


def _dtw(a: np.ndarray,b: np.ndarray,window_fraction: float,
         cancel: Callable[[],bool]|None=None) -> tuple[float|None,int,bool]:
    n,m=len(a),len(b)
    if n==0 or m==0:return None,0,False
    band=max(abs(n-m),int(math.ceil(max(n,m)*window_fraction)))
    inf=float("inf");cost=np.full((n+1,m+1),inf);cost[0,0]=0
    pred=np.full((n+1,m+1),-1,dtype=np.int8)
    for i in range(1,n+1):
        if cancel and cancel():return None,0,True
        for j in range(max(1,i-band),min(m,i+band)+1):
            local=float(np.linalg.norm(a[i-1]-b[j-1]))
            opts=(cost[i-1,j-1],cost[i-1,j],cost[i,j-1]);k=int(np.argmin(opts))
            if math.isfinite(opts[k]):
                cost[i,j]=opts[k]+local
                pred[i,j]=k
    if not math.isfinite(cost[n,m]):return None,0,False
    i,j=n,m;plen=0
    while i>0 or j>0:
        plen+=1;k=int(pred[i,j])
        if k==0:i-=1;j-=1
        elif k==1:i-=1
        elif k==2:j-=1
        else:return None,0,False
    return float(cost[n,m]/plen),plen,False


def _discrete_frechet(a: np.ndarray,b: np.ndarray,
                      cancel: Callable[[],bool]|None=None) -> tuple[float|None,bool]:
    if len(a)==0 or len(b)==0:return None,False
    ca=np.full((len(a),len(b)),np.nan)
    for i in range(len(a)):
        if cancel and cancel():return None,True
        for j in range(len(b)):
            d=float(np.linalg.norm(a[i]-b[j]))
            if i==0 and j==0:ca[i,j]=d
            elif i==0:ca[i,j]=max(ca[i,j-1],d)
            elif j==0:ca[i,j]=max(ca[i-1,j],d)
            else:ca[i,j]=max(min(ca[i-1,j],ca[i-1,j-1],ca[i,j-1]),d)
    return float(ca[-1,-1]),False


def _hausdorff(a: np.ndarray,b: np.ndarray,
               cancel: Callable[[],bool]|None=None) -> tuple[float|None,bool]:
    if not len(a) or not len(b):return None,False
    if cancel and cancel():return None,True
    d=np.linalg.norm(a[:,None,:]-b[None,:,:],axis=2)
    if cancel and cancel():return None,True
    return float(max(np.max(np.min(d,axis=1)),np.max(np.min(d,axis=0)))),False


def _nw_score(a: list[tuple[int,int]],b: list[tuple[int,int]],params: dict[str,Any],
              cancel: Callable[[],bool]|None=None) -> tuple[float|None,bool]:
    return needleman_wunsch_score(a,b,params,cancel)


def _line_lengths(matrix: np.ndarray, diagonal: bool) -> list[int]:
    lines=[];n,m=matrix.shape
    if diagonal:
        offsets=range(-n+1,m)
        for off in offsets:
            coords=[(i,i+off) for i in range(n) if 0<=i+off<m]
            count=0
            for i,j in coords:
                if matrix[i,j]:count+=1
                elif count:lines.append(count);count=0
            if count:lines.append(count)
    else:
        for j in range(m):
            count=0
            for i in range(n):
                if matrix[i,j]:count+=1
                elif count:lines.append(count);count=0
            if count:lines.append(count)
    return lines


def _crqa(a: np.ndarray,b: np.ndarray,params: dict[str,Any],
          cancel: Callable[[],bool]|None=None) -> tuple[dict[str,float|None]|None,str|None]:
    dim=int(params.get("embedding_dimension",0));lag=int(params.get("embedding_lag_samples",0))
    if dim<1 or lag<1 or not _finite_number(params.get("threshold")) or float(params["threshold"])<=0:return None,"invalid_crqa_parameters"
    if a.ndim==1:a=a[:,None]
    if b.ndim==1:b=b[:,None]
    if a.shape[1]!=b.shape[1]:return None,"comparison_dimension_mismatch"
    def embed(x):
        n=len(x)-(dim-1)*lag
        if n<=0:return np.empty((0,x.shape[1]*dim))
        rows=[]
        for i in range(n):
            if cancel and cancel():return None
            rows.append(np.concatenate([x[i+k*lag] for k in range(dim)]))
        return np.asarray(rows)
    ea,eb=embed(a),embed(b)
    if ea is None or eb is None:return None,"cancelled"
    if not len(ea) or not len(eb):return None,"insufficient_embedded_points"
    rec=np.linalg.norm(ea[:,None,:]-eb[None,:,:],axis=2)<=float(params["threshold"])
    if cancel and cancel():return None,"cancelled"
    tw=max(0,int(params.get("theiler_window_samples",0)))
    if tw:
        for i in range(rec.shape[0]):
            lo=max(0,i-tw);hi=min(rec.shape[1],i+tw+1);rec[i,lo:hi]=False
    rr=float(rec.mean()) if rec.size else None
    diag=_line_lengths(rec,True);vert=_line_lengths(rec,False)
    lmin=int(params["min_diagonal_line"]);vmin=int(params["min_vertical_line"])
    eligible=[x for x in diag if x>=lmin];ve=[x for x in vert if x>=vmin]
    recurrent=int(rec.sum())
    det=sum(eligible)/recurrent if recurrent and eligible else (0.0 if recurrent else None)
    mean_l=float(np.mean(eligible)) if eligible else None;max_l=float(max(eligible)) if eligible else None
    if eligible:
        counts=np.asarray([eligible.count(k) for k in set(eligible)],dtype=float);p=counts/counts.sum();entr=float(-np.sum(p*np.log2(p)))
    else:entr=None
    lam=sum(ve)/recurrent if recurrent and ve else (0.0 if recurrent else None)
    tt=float(np.mean(ve)) if ve else None
    return {"rr":rr,"det":det,"l":mean_l,"lmax":max_l,"entr":entr,"lam":lam,"tt":tt},None


def _lag_result(a: pd.DataFrame,b: pd.DataFrame,signal: str,config: dict[str,Any],spec_corr:dict[str,Any],spec_lag:dict[str,Any],prov_ok:bool,interaction:Any,pair_id:str,segment:Any,trial:Any,condition:Any,curve_rows:list[dict[str,Any]],metrics:list[dict[str,Any]],role:str|None=None) -> dict[str,Any]|None:
    best,curve,reason=_pair_lag(a,b,signal,config)
    for row in curve:
        curve_rows.append({"run_id":config.get("run_id"),"interaction_id":interaction,"pair_id":pair_id,"segment_id":segment,"signal":signal,"role":role,"lag_s":row["lag_s"],"correlation":row["correlation"],"n_observed":row["n_observed"],"effective_duration_s":row["effective_duration_s"],"coverage":row["coverage"],"boundary_warning":bool(best and best.get("boundary_warning")),"parameter_ref":_param_ref(config,{"lag_sign":"positive means B follows A","lag_order":"r_AB(tau)=corr(A(t),B(t+tau))"})})
    if best is None:return None
    metrics.append(_metric(spec_corr,config,prov_ok,value=best["correlation"],reason=None,unit="r",interaction_id=interaction,pair_id=pair_id,segment_id=segment,trial=trial,condition=condition,n_obs=best["n_observed"],duration=best["effective_duration_s"],extra={"best_lag_s":best["best_lag_s"],"r_zero":best["r_zero"],"boundary_warning":best["boundary_warning"],"tie_lags_s":best["tie_lags_s"],"support_start_s":best["support_start_s"],"support_end_s":best["support_end_s"],"signal":signal,"role":role}))
    metrics.append(_metric(spec_lag,config,prov_ok,value=best["best_lag_s"],reason=None,unit="s",interaction_id=interaction,pair_id=pair_id,segment_id=segment,trial=trial,condition=condition,n_obs=best["n_observed"],duration=best["effective_duration_s"],extra={"max_correlation":best["correlation"],"r_zero":best["r_zero"],"boundary_warning":best["boundary_warning"],"tie_lags_s":best["tie_lags_s"],"signal":signal,"role":role}))
    return best


def _all_times(frames: list[pd.DataFrame], signal: str, tol: float) -> tuple[np.ndarray,list[np.ndarray],list[np.ndarray],list[np.ndarray]]:
    # Full-member analysis uses the first member's timestamps, with monotone
    # one-to-one nearest matching for every other member.
    if not frames:return np.array([]),[],[],[]
    ordered=[f.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True) for f in frames]
    base=ordered[0];tbase=pd.to_numeric(base["time_s"],errors="coerce").to_numpy(dtype=float)
    cols=[];masks=[];time_vectors=[]
    for f in ordered:
        vals,valid,_=_valid_signal(f,signal);cols.append(vals);masks.append(valid);time_vectors.append(pd.to_numeric(f["time_s"],errors="coerce").to_numpy(dtype=float))
    common_t=[];per=[[] for _ in ordered];last=[-1]*len(ordered)
    for i,t in enumerate(tbase):
        chosen=[i]
        ok=True
        for mi in range(1,len(ordered)):
            tv=time_vectors[mi];j=int(np.searchsorted(tv,t-tol));opts=[x for x in (j-1,j) if last[mi]<x<len(tv)]
            if not opts:ok=False;break
            q=min(opts,key=lambda x:abs(tv[x]-t))
            if abs(tv[q]-t)>tol+1e-15:ok=False;break
            chosen.append(q)
        if ok:
            common_t.append(float(t))
            for mi,q in enumerate(chosen):per[mi].append(q);last[mi]=q
    ix=[np.asarray(x,dtype=int) for x in per]
    aligned_vals=[cols[k][ix[k]] for k in range(len(ordered))]
    aligned_masks=[masks[k][ix[k]] for k in range(len(ordered))]
    return np.asarray(common_t,dtype=float),aligned_vals,aligned_masks,ix


def _make_groups(frame: pd.DataFrame) -> list[tuple[tuple[Any,...],pd.DataFrame]]:
    if "interaction_id" not in frame or frame["interaction_id"].isna().any():return []
    cols=["interaction_id"]+[c for c in ("trial","stimulus_id","segment_id","analysis_stream_id","eye_label") if c in frame]
    return list(frame.groupby(cols,dropna=False,sort=False))


def _group_support_ref(frames: list[pd.DataFrame], interaction: Any, segment: Any,
                       trial: Any, stream_id: Any) -> str:
    payload=[]
    for frame in frames:
        id_column="observation_id" if "observation_id" in frame else "original_index"
        times=pd.to_numeric(frame.get("time_s",pd.Series(index=frame.index,dtype=float)),errors="coerce").to_numpy(dtype=float)
        ids=frame[id_column].astype("string").tolist() if id_column in frame else [None]*len(frame)
        records=[(float(t),ids[i]) for i,t in enumerate(times) if np.isfinite(t)]
        records.sort(key=lambda row:(row[0],str(row[1])))
        payload.append({"subject_id":str(frame.iloc[0].get("subject_id")) if len(frame) else None,
                        "session_id":str(frame.iloc[0].get("session_id")) if len(frame) else None,
                        "analysis_stream_id":str(frame.iloc[0].get("analysis_stream_id")) if len(frame) else None,
                        "observations":records})
    raw=json.dumps({"interaction_id":interaction,"segment_id":segment,"trial":trial,
                    "analysis_stream_id":stream_id,"members":payload},sort_keys=True,
                   ensure_ascii=False,separators=(",",":"),default=str)
    return "support:"+hashlib.sha256(raw.encode("utf-8")).hexdigest()


def analyze_group(signals: pd.DataFrame, config: dict[str,Any], segments: pd.DataFrame|None=None,
                  aois: pd.DataFrame|None=None, cancel: Callable[[],bool]|None=None,
                  progress: Callable[[float,str],None]|None=None, *,
                  aoi_geometry_budget: AOIGeometryBudget|None=None) -> AnalysisBundle:
    """Analyze explicit N-person interaction units and produce auditable pair tables.

    An optional AOI budget is transient to this invocation and can be shared
    with individual analysis to enforce the run-wide exact-predicate limit.
    """
    if not isinstance(signals,pd.DataFrame):raise ContractError("signals must be a pandas DataFrame")
    required={"subject_id","session_id","time_s","time_raw","original_index","interaction_id"}
    if not required.issubset(signals.columns):raise ContractError("group signals missing required columns: "+", ".join(sorted(required-set(signals.columns))))
    if signals[list(("subject_id","session_id","original_index","interaction_id"))].isna().any().any():raise ContractError("group analysis requires explicit subject/session/original_index/interaction_id; no IDs are inferred")
    if not isinstance(config,dict):raise ContractError("config must be a dictionary")
    config=dict(config)
    config["_aoi_cancel_check"]=cancel
    run_id=config.get("run_id")
    if not isinstance(run_id,str) or not run_id.strip():raise ContractError("config.run_id must be a nonempty string")
    prov_ok,provenance=_provenance(config)
    frame=signals.copy();frame["time_s"]=pd.to_numeric(frame["time_s"],errors="coerce")
    frame=frame.loc[np.isfinite(frame["time_s"].to_numpy(dtype=float))].copy()
    requested_stream=config.get("analysis_stream_id")
    if requested_stream is not None and "analysis_stream_id" in frame:
        frame=frame.loc[frame["analysis_stream_id"].astype("string").eq(str(requested_stream)).fillna(False)].copy()
    frame=_prepare_segments(frame,segments,config.get("overlap_policy"))
    tolerance=float(config.get("alignment_tolerance_s",0.0))
    if tolerance<0 or not math.isfinite(tolerance):raise ContractError("alignment_tolerance_s must be finite and nonnegative")
    requested_raw=config.get("metric_ids")
    if isinstance(requested_raw,(list,tuple,set)):requested=set(requested_raw)
    else:requested={s["metric_id"] for s in GROUP_CATALOGUE if s["tier"]=="R1"}
    requested_all={x for x in requested if x in {s["metric_id"] for s in GROUP_CATALOGUE}}
    aoi_metric_ids={"group.shared_aoi_proportion", "group.aoi_agreement", "group.transition_similarity",
                    "group.all_member_aoi_agreement", "group.majority_aoi_agreement"}
    if requested_all & aoi_metric_ids:
        if aoi_geometry_budget is not None and not isinstance(aoi_geometry_budget, AOIGeometryBudget):
            raise ContractError("aoi_geometry_budget must be an AOIGeometryBudget or None")
        if aoi_geometry_budget is None:
            aoi_geometry_budget=AOIGeometryBudget(cancel_check=cancel)
        elif cancel is not None and aoi_geometry_budget.cancel_check is None:
            aoi_geometry_budget.cancel_check=cancel
        config["_aoi_geometry_budget"]=aoi_geometry_budget
        config["_aoi_membership_scope_id"]=f"group:{run_id}"
        try:
            config["_aoi_geometry_prepared"]=_prepare_aoi_geometry_for_frame(frame,aois,config)
        except AOIGeometryError as exc:
            config["_aoi_geometry_issue"]=exc.issue.as_dict()
    av_rows=[_available(s,frame,config) for s in GROUP_CATALOGUE]
    geometry_issue=config.get("_aoi_geometry_issue")
    if isinstance(geometry_issue,dict):
        for row in av_rows:
            if row["metric_id"] in aoi_metric_ids:
                row.update({"available":False,"status":"blocked",
                            "reason_code":geometry_issue.get("code","aoi_geometry_unavailable"),
                            "reason":geometry_issue.get("message","AOI geometry is unavailable")})
    av_map={r["metric_id"]:r for r in av_rows}
    # R2 is explicit opt-in; the availability table still lists all entries.
    requested_all={x for x in requested if x in av_map}
    # Only fully available methods may enter computational branches. The full
    # request is retained separately so blocked/experimental methods still get
    # an explicit result row below.
    requested={x for x in requested_all if av_map[x]["available"]}
    metrics:list[dict[str,Any]]=[];lag_rows:list[dict[str,Any]]=[];window_rows:list[dict[str,Any]]=[];network_rows:list[dict[str,Any]]=[];majority_rows:list[dict[str,Any]]=[]
    pair_corrs:dict[tuple[Any,...],list[tuple[float,int,float]]]={}
    groups=_make_groups(frame);total_groups=max(1,len(groups));execution="complete"
    event_types=tuple(config.get("event_source_types",[])) if isinstance(config.get("event_source_types"), (list,tuple,set)) else ()
    for gi,(group_key,gdf) in enumerate(groups):
        if cancel and cancel():execution="cancelled";break
        keycols=["interaction_id"]+[c for c in ("trial","stimulus_id","segment_id","analysis_stream_id","eye_label") if c in frame]
        keyed=dict(zip(keycols,group_key if isinstance(group_key,tuple) else (group_key,)))
        interaction=keyed.get("interaction_id");trial=keyed.get("trial");stimulus=keyed.get("stimulus_id");segment=keyed.get("segment_id")
        stream_id=keyed.get("analysis_stream_id");eye_label=keyed.get("eye_label")
        segment_start=None;segment_end=None
        if _SEGMENT_START_COLUMN in gdf and _SEGMENT_END_COLUMN in gdf:
            starts=pd.to_numeric(gdf[_SEGMENT_START_COLUMN],errors="coerce").dropna().unique()
            ends=pd.to_numeric(gdf[_SEGMENT_END_COLUMN],errors="coerce").dropna().unique()
            if len(starts)==1 and len(ends)==1:
                segment_start=float(starts[0]);segment_end=float(ends[0])
        members=[]
        for sid,member_frame in gdf.groupby("subject_id",sort=False):
            sessions=member_frame["session_id"].dropna().unique()
            if len(sessions)!=1:continue
            members.append((sid,sessions[0],member_frame.copy()))
        if len(members)<2:continue
        # Reused IDs within a team are not silently resolved to a pair.
        if len({str(x[0]) for x in members})!=len(members):continue
        raw_pair_results=[]
        network_local=[]
        dependence_refs=set()
        if "dependence_ref" in gdf:
            for value in gdf["dependence_ref"].dropna():
                values=value if isinstance(value,(list,tuple,set)) else (value,)
                dependence_refs.update(str(x) for x in values if x is not None and not pd.isna(x))
        stream_refs=tuple(sorted({str(mf.iloc[0].get("analysis_stream_id")) for _,_,mf in members
                                  if len(mf) and pd.notna(mf.iloc[0].get("analysis_stream_id"))})) or None
        parent_refs=set()
        for _,_,member_frame in members:
            if "contributing_eyes" not in member_frame:continue
            for value in member_frame["contributing_eyes"].dropna():
                eyes=value if isinstance(value,(list,tuple,set)) else (value,)
                parent_refs.update("gaze:"+str(eye) for eye in eyes if str(eye) in {"left","right"})
        if parent_refs:stream_refs=tuple(sorted(parent_refs))
        support_ref=_group_support_ref([mf for _,_,mf in members],interaction,segment,trial,stream_id)
        metric_start=len(metrics)
        for (ida,sa,fa),(idb,sb,fb) in combinations(members,2):
            pid=_pair_id(ida,idb)
            pair_common={"interaction_id":interaction,"pair_id":pid,"segment_id":segment,"trial":trial,
                         "condition":fa.iloc[0].get("condition",fb.iloc[0].get("condition")),
                         "analysis_stream_id":stream_id,"eye_label":eye_label,
                         "stream_parent_refs":stream_refs,"support_ref":support_ref,
                         "dependence_ref":tuple(sorted(dependence_refs)) or None}
            if cancel and cancel():execution="cancelled";break
            # Scalar metrics: exact/nearest paired raw observations, no interpolation.
            for sig in ("gaze_x","gaze_y","pupil_left","pupil_right"):
                metric_id="group.zero_lag_correlation" if sig.startswith("gaze") else "group.pupil_synchrony"
                needs_pair_summary=sig.startswith("gaze") and "group.mean_pairwise_similarity" in requested
                needs_network=sig==config.get("network_signal") and bool(requested & {"group.network.edge","group.network.strength"})
                if metric_id not in requested and not needs_pair_summary and not needs_network:continue
                if sig.startswith("gaze") and (not _meta_ok("cross_subject_spatial_comparable",config) or not _meta_ok("synchronization_confirmed",config)):continue
                if sig.startswith("pupil") and (not _meta_ok("pupil_unit",config) or not _meta_ok("synchronization_confirmed",config)):continue
                al=_align(fa,fb,sig,tolerance)
                valid=al["va"]&al["vb"]
                if sig.startswith("pupil"):
                    bw=config.get("pupil_baseline_window_s")
                    if isinstance(bw,(list,tuple)) and len(bw)==2:
                        am=al["ta"]>=float(bw[0]);am&=al["ta"]<=float(bw[1]);
                        basea,_=_time_weighted_mean_values(al["ta"],al["a"],valid,config,float(bw[0]),float(bw[1]))
                        baseb,_=_time_weighted_mean_values(al["ta"],al["b"],valid,config,float(bw[0]),float(bw[1]))
                        if basea is None or baseb is None:continue
                        xa=al["a"]-basea;xb=al["b"]-baseb
                    else:continue
                else:xa,xb=al["a"],al["b"]
                val=_corr(xa[valid],xb[valid],"pearson")
                support,duration,maxgap=_valid_support(al["ta"],valid,config)
                if val is not None:
                    spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]==metric_id)
                    if metric_id in requested:
                        metrics.append(_metric(spec,config,prov_ok,value=val,unit="r",subject_id=None,session_id=None,**pair_common,n_obs=int(valid.sum()),n_interp=0,duration=duration,extra={"signal":sig,"correlation":"Pearson","alignment_tolerance_s":tolerance,"max_gap_s":maxgap,"common_support_only":True}))
                    if sig.startswith("gaze"):
                        pair_corrs.setdefault((interaction,segment,trial,stream_id,eye_label),[]).append((val,int(valid.sum()),duration))
                    if needs_network:
                        network_local.append((pid,str(ida),str(idb),float(val),int(valid.sum()),duration))
            # Spatial co-sampling metrics: only confirmed same space and time.
            if _meta_ok("cross_subject_spatial_comparable",config) and _meta_ok("synchronization_confirmed",config):
                ax=_align(fa,fb,"gaze_x",tolerance);ay=_align(fa,fb,"gaze_y",tolerance)
                if len(ax["ta"]) and len(ay["ta"]) and len(ax["ta"])==len(ay["ta"]) and np.allclose(ax["ta"],ay["ta"],rtol=0,atol=1e-12):
                    valid=ax["va"]&ax["vb"]&ay["va"]&ay["vb"]
                    dist=np.hypot(ax["a"]-ax["b"],ay["a"]-ay["b"])
                    support,duration,maxgap=_valid_support(ax["ta"],valid,config)
                    dt=np.diff(ax["ta"]);w=np.where(support[:-1],dt,0.) if len(dt) else np.array([])
                    total=float(w.sum())
                    if total>0:
                        mean,mean_duration=_time_weighted_mean_values(ax["ta"],dist,valid,config)
                        if "group.mean_gaze_distance" in requested:
                            spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.mean_gaze_distance")
                            metrics.append(_metric(spec,config,prov_ok,value=mean,unit=str(_meta_value(config,"coordinate_unit")),**pair_common,n_obs=int(valid.sum()),duration=mean_duration,extra={"weighting":"trapezoid over valid adjacent support","max_gap_s":maxgap,"space_confirmed":True}))
                        delta=config.get("co_gaze_threshold")
                        if "group.co_gaze_proportion" in requested and _finite_number(delta) and float(delta)>0:
                            value=float(np.sum(w*(dist[:-1]<=float(delta)))/total)
                            spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.co_gaze_proportion")
                            metrics.append(_metric(spec,config,prov_ok,value=value,unit="proportion",**pair_common,n_obs=int(valid.sum()),duration=total,extra={"threshold":float(delta),"threshold_unit":_meta_value(config,"coordinate_unit"),"weighting":"left-held valid interval"}))
                    # AOI comparisons match exact aligned rows and preserve missing labels.
                    if aois is not None and _meta_ok("aoi_mapping",config):
                        la,shapes_a,ea=_aoi_labels(ax["frame_a"],aois,config);lb,shapes_b,eb=_aoi_labels(ax["frame_b"],aois,config)
                        if not ea and not eb:
                            aoi_audit={"aoi_geometry_version":AOI_GEOMETRY_VERSION,
                                       "aoi_domain_schema_version":AOI_DOMAIN_SCHEMA_VERSION,
                                       "aoi_boundary_policy":"closed",
                                       "aoi_geometry_fingerprints":sorted({str(s.get("aoi_geometry_fingerprint")) for s in (*shapes_a,*shapes_b)})}
                            lbl_a=[la[i] for i in ax["ia"]];lbl_b=[lb[i] for i in ax["ib"]]
                            valid_aoi=np.asarray([bool(x) and bool(y) for x,y in zip(lbl_a,lbl_b)])&valid
                            if valid_aoi.any():
                                agree=np.asarray([x==y for x,y in zip(lbl_a,lbl_b)],dtype=bool)
                                asupport,aduration,_=_valid_support(ax["ta"],valid_aoi,config);aw=np.where(asupport[:-1],np.diff(ax["ta"]),0.) if len(ax["ta"])>1 else np.array([]);atotal=float(aw.sum())
                                if atotal>0:
                                    a_agree=float(np.sum(aw*(agree[:-1]))/atotal)
                                    if "group.aoi_agreement" in requested:
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.aoi_agreement")
                                        metrics.append(_metric(spec,config,prov_ok,value=a_agree,unit="proportion",**pair_common,n_obs=int(valid_aoi.sum()),duration=atotal,extra={"missing_strategy":"complete paired AOI labels","aoi_error_a":ea,"aoi_error_b":eb,**aoi_audit}))
                                    same=np.asarray([len(x)==1 and x==y for x,y in zip(lbl_a,lbl_b)],dtype=bool)
                                    if "group.shared_aoi_proportion" in requested:
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.shared_aoi_proportion")
                                        metrics.append(_metric(spec,config,prov_ok,value=float(np.sum(aw*same[:-1])/atotal),unit="proportion",**pair_common,n_obs=int(valid_aoi.sum()),duration=atotal,extra={"denominator":"jointly valid AOI time",**aoi_audit}))
                                    if "group.transition_similarity" in requested and config.get("enable_experimental") is True:
                                        seq_a=[next(iter(x)) if len(x)==1 else None for x in lbl_a];seq_b=[next(iter(x)) if len(x)==1 else None for x in lbl_b]
                                        sim,info=_js_transition_similarity(_transition_counts(seq_a),_transition_counts(seq_b))
                                        if sim is not None:
                                            spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.transition_similarity")
                                            metrics.append(_metric(spec,config,prov_ok,value=sim,unit="similarity",**pair_common,n_obs=info["transition_support"],duration=atotal,extra={**info,**aoi_audit}))
            # Delay search uses an explicit scalar channel and the documented sign convention.
            if "group.lagged_correlation" in requested or "group.best_lag" in requested:
                sig=config.get("lag_signal","pupil_left")
                corr_spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.lagged_correlation")
                lag_spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.best_lag")
                best=_lag_result(fa,fb,sig,config,corr_spec,lag_spec,prov_ok,interaction,pid,segment,trial,pair_common["condition"],lag_rows,metrics)
                if best:raw_pair_results.append((best["correlation"],best["effective_duration_s"],best["best_lag_s"],pid))
            # Experimental sequence-distance methods require explicit opt-in.
            if config.get("enable_experimental") is True:
                signal=config.get("comparison_signal")
                if signal in {"gaze_xy","gaze_x","gaze_y","pupil_left","pupil_right"}:
                    if signal=="gaze_xy":
                        x1,v1,_=_valid_signal(fa,"gaze_x");y1,vy1,_=_valid_signal(fa,"gaze_y");x2,v2,_=_valid_signal(fb,"gaze_x");y2,vy2,_=_valid_signal(fb,"gaze_y")
                        ma=v1&vy1;mb=v2&vy2;seq_a=np.c_[x1[ma],y1[ma]];seq_b=np.c_[x2[mb],y2[mb]]
                        ua=str(_meta_value(config,"coordinate_unit") or "unknown")
                    else:
                        x1,v1,_=_valid_signal(fa,signal);x2,v2,_=_valid_signal(fb,signal);seq_a=x1[v1,None];seq_b=x2[v2,None]
                        ua=str(_meta_value(config,"pupil_unit") if signal.startswith("pupil") else _meta_value(config,"coordinate_unit") or "unknown")
                    # Keep no-gap paths; deleting internal invalid stretches would join data across missing time.
                    def contiguous(f,mask):
                        tt=pd.to_numeric(f["time_s"],errors="coerce").to_numpy(dtype=float);use=tt[mask]
                        pos=np.diff(tt);good=pos[pos>0];lim=_gap_limit_s(tt,config,float(np.median(good))) if len(good) else 0
                        if lim is None:lim=0
                        return bool(len(use)>=2 and (len(use)==len(tt) or np.all(np.diff(use)<=lim+1e-12)))
                    if len(seq_a)>=2 and len(seq_b)>=2:
                        if signal=="gaze_xy": c_a=contiguous(fa,ma);c_b=contiguous(fb,mb)
                        else: c_a=contiguous(fa,v1);c_b=contiguous(fb,v2)
                        if c_a and c_b:
                            r2_too_large=(max(len(seq_a),len(seq_b))>R2_RESOURCE_LIMITS["max_sequence_samples"] or len(seq_a)*len(seq_b)>R2_RESOURCE_LIMITS["max_pairwise_cells"])
                            if "group.dtw_distance" in requested:
                                if r2_too_large:
                                    _resource_limit_row("group.dtw_distance",requested,metrics,config,prov_ok,pair_common,seq_a,seq_b)
                                else:
                                    val,pathlen,was_cancelled=_dtw(seq_a,seq_b,float(config["dtw_window_fraction"]),cancel)
                                    if was_cancelled:
                                        execution="cancelled";break
                                    if val is not None:
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.dtw_distance")
                                        metrics.append(_metric(spec,config,prov_ok,value=val,unit=ua,**pair_common,n_obs=min(len(seq_a),len(seq_b)),duration=None,extra={"local_cost":"Euclidean","normalization":"accumulated_cost/path_length","path_length":pathlen,"sakoe_chiba_window_fraction":float(config["dtw_window_fraction"]),"not_a_sync_measure":True}))
                            if execution=="cancelled":break
                            if signal=="gaze_xy":
                                if "group.frechet_distance" in requested:
                                    if r2_too_large:
                                        _resource_limit_row("group.frechet_distance",requested,metrics,config,prov_ok,pair_common,seq_a,seq_b)
                                    else:
                                        val,was_cancelled=_discrete_frechet(seq_a,seq_b,cancel)
                                        if was_cancelled:
                                            execution="cancelled";break
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.frechet_distance")
                                        metrics.append(_metric(spec,config,prov_ok,value=val,unit=ua,**pair_common,n_obs=min(len(seq_a),len(seq_b)),extra={"algorithm":"discrete Frechet recurrence","order_preserved":True}))
                                if "group.hausdorff_distance" in requested:
                                    if r2_too_large:
                                        _resource_limit_row("group.hausdorff_distance",requested,metrics,config,prov_ok,pair_common,seq_a,seq_b)
                                    else:
                                        val,was_cancelled=_hausdorff(seq_a,seq_b,cancel)
                                        if was_cancelled:
                                            execution="cancelled";break
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.hausdorff_distance")
                                        metrics.append(_metric(spec,config,prov_ok,value=val,unit=ua,**pair_common,n_obs=min(len(seq_a),len(seq_b)),extra={"algorithm":"symmetric directed point-set Hausdorff","order_preserved":False}))
                            if execution=="cancelled":break
                            crqa_metric_ids={"group.crqa.recurrence_rate","group.crqa.determinism","group.crqa.mean_diagonal_length","group.crqa.max_diagonal_length","group.crqa.diagonal_entropy","group.crqa.laminarity","group.crqa.trapping_time"}
                            if requested & crqa_metric_ids:
                                if r2_too_large:
                                    for mid in sorted(requested & crqa_metric_ids):
                                        _resource_limit_row(mid,requested,metrics,config,prov_ok,pair_common,seq_a,seq_b)
                                else:
                                    cp=config.get("crqa",{});res,why=_crqa(seq_a,seq_b,cp,cancel)
                                    if why=="cancelled":
                                        execution="cancelled";break
                                    if res:
                                        mapping={"rr":"group.crqa.recurrence_rate","det":"group.crqa.determinism","l":"group.crqa.mean_diagonal_length","lmax":"group.crqa.max_diagonal_length","entr":"group.crqa.diagonal_entropy","lam":"group.crqa.laminarity","tt":"group.crqa.trapping_time"}
                                        for key,mid in mapping.items():
                                            if mid in requested:
                                                spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]==mid)
                                                metrics.append(_metric(spec,config,prov_ok,value=res[key],unit=spec["unit"],reason=None if res[key] is not None else "no_qualifying_recurrence_lines",**pair_common,n_obs=len(seq_a),extra={"threshold":cp["threshold"],"embedding_dimension":cp["embedding_dimension"],"embedding_lag_samples":cp["embedding_lag_samples"],"min_diagonal_line":cp["min_diagonal_line"],"min_vertical_line":cp["min_vertical_line"],"theiler_window_samples":cp.get("theiler_window_samples",0),"line_boundary_rule":"false recurrences split runs","zero_recurrence_handling":"NA"}))
            # Event coincidence and blink synchrony are onset matches, never inferred from missingness.
            evdata=config.get("events")
            if isinstance(evdata,pd.DataFrame):
                for event_type,mid,confirmed in (("fixation","group.fixation_cooccurrence",config.get("event_source_confirmed") is True),("blink","group.blink_synchrony",config.get("blink_source_confirmed") is True)):
                    if mid not in requested or not confirmed:continue
                    ea=_event_frame(evdata,interaction,ida,sa,event_type,segment,segment_start,segment_end,stream_id,config)
                    eb=_event_frame(evdata,interaction,idb,sb,event_type,segment,segment_start,segment_end,stream_id,config)
                    if ea.empty and eb.empty:continue
                    val,nmatch=_event_match_rate(ea,eb,float(config.get("event_match_tolerance_s",0)))
                    spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]==mid)
                    metrics.append(_metric(spec,config,prov_ok,value=val,unit="proportion",**pair_common,n_obs=len(ea)+len(eb),extra={"matches":nmatch,"tolerance_s":float(config.get("event_match_tolerance_s",0)),"matching":"sorted one-to-one nearest feasible onsets","symmetric_rate":"2*matches/(nA+nB)"}))
            # Published MultiMatch implementation (adswa/multimatch_gaze), if installed.
            if config.get("enable_experimental") is True and any(mid in requested for mid in ("group.multimatch.shape","group.multimatch.direction","group.multimatch.length","group.multimatch.position","group.multimatch.duration")) and _meta_ok("multimatch_gaze",config) and _meta_ok("screen_size_px",config) and str(_meta_value(config,"coordinate_unit")).lower()=="px":
                evdata=config.get("events")
                faev=_event_frame(evdata,interaction,ida,sa,"fixation",segment,segment_start,segment_end,stream_id,config)
                fbev=_event_frame(evdata,interaction,idb,sb,"fixation",segment,segment_start,segment_end,stream_id,config)
                needed={"start_x","start_y","duration_s"}
                if needed.issubset(faev.columns) and needed.issubset(fbev.columns) and len(faev)>=3 and len(fbev)>=3:
                    mm_ids={f"group.multimatch.{dim}" for dim in ("shape","direction","length","position","duration")}
                    if max(len(faev),len(fbev))>R2_RESOURCE_LIMITS["max_sequence_samples"] or len(faev)*len(fbev)>R2_RESOURCE_LIMITS["max_pairwise_cells"]:
                        dummy_a=np.empty((len(faev),1));dummy_b=np.empty((len(fbev),1))
                        for mid in sorted(requested & mm_ids):
                            _resource_limit_row(mid,requested,metrics,config,prov_ok,pair_common,dummy_a,dummy_b)
                    elif cancel and cancel():
                        execution="cancelled";break
                    else:
                        try:
                            x_a=pd.to_numeric(faev.start_x,errors="coerce").to_numpy(dtype=float);y_a=pd.to_numeric(faev.start_y,errors="coerce").to_numpy(dtype=float);d_a=pd.to_numeric(faev.duration_s,errors="coerce").to_numpy(dtype=float)
                            x_b=pd.to_numeric(fbev.start_x,errors="coerce").to_numpy(dtype=float);y_b=pd.to_numeric(fbev.start_y,errors="coerce").to_numpy(dtype=float);d_b=pd.to_numeric(fbev.duration_s,errors="coerce").to_numpy(dtype=float)
                            va=fixation_vectors(x_a,y_a,d_a);vb=fixation_vectors(x_b,y_b,d_b)
                            mm=config.get("multimatch",{}) if isinstance(config.get("multimatch"),dict) else {}
                            vals=multimatch_dimensions(va,vb,config["screen_size_px"],mm)
                            if cancel and cancel():execution="cancelled";break
                            if vals is not None and len(vals)==5:
                                for value,dim in zip(vals,("shape","direction","length","position","duration")):
                                    mid=f"group.multimatch.{dim}"
                                    if mid in requested:
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]==mid)
                                        metrics.append(_metric(spec,config,prov_ok,value=float(value),unit="similarity",**pair_common,n_obs=min(len(faev),len(fbev)),extra={"package":"multimatch-gaze","version":"0.1.3","implementation":"adswa/multimatch_gaze docomparison","screensize_px":config["screen_size_px"],"grouping":bool(mm.get("grouping",False)),"TDir_deg":float(mm.get("TDir",0)),"TDur_s":float(mm.get("TDur",0)),"TAmp_px":float(mm.get("TAmp",0)),"dimensions":["shape","direction","length","position","duration"]}))
                        except Exception:
                            # Method errors must never degrade to a proxy result.
                            execution="partial"
            # ScanMatch: time-bin positions to grid symbols, then global NW scoring.
            if "group.scanmatch.score" in requested and config.get("enable_experimental") is True and _meta_ok("scanmatch_parameters",config) and str(_meta_value(config,"coordinate_unit")).lower()=="px":
                p=config["scanmatch"];size=config.get("screen_size_px");al=_align(fa,fb,"gaze_x",tolerance)
                if size and len(al["ta"]):
                    # Use explicit equal-duration bins over the shared observed interval;
                    # gaps or invalid bins block this sequence method.
                    ay=_align(fa,fb,"gaze_y",tolerance)
                    if len(ay["ta"])==len(al["ta"]) and np.allclose(ay["ta"],al["ta"],atol=1e-12,rtol=0):
                        x1,y1=al["a"],ay["a"];x2,y2=al["b"],ay["b"]
                        valid=al["va"]&al["vb"]&ay["va"]&ay["vb"]
                        start=max(float(al["ta"][0]),float(ay["ta"][0]));end=min(float(al["ta"][-1]),float(ay["ta"][-1]));bin_s=float(p["time_bin_s"])
                        tokens_a=scanmatch_cell_tokens(al["ta"],x1,y1,valid,size,int(p["grid_cols"]),int(p["grid_rows"]),bin_s,start,end,cancel)
                        tokens_b=scanmatch_cell_tokens(al["ta"],x2,y2,valid,size,int(p["grid_cols"]),int(p["grid_rows"]),bin_s,start,end,cancel)
                        if cancel and cancel():
                            execution="cancelled";break
                        if tokens_a is not None and tokens_b is not None and len(tokens_a)==len(tokens_b) and len(tokens_a)>=2:
                            if max(len(tokens_a),len(tokens_b))>R2_RESOURCE_LIMITS["max_sequence_samples"] or len(tokens_a)*len(tokens_b)>R2_RESOURCE_LIMITS["max_pairwise_cells"]:
                                dummy=np.empty((len(tokens_a),1));dummy_b=np.empty((len(tokens_b),1))
                                _resource_limit_row("group.scanmatch.score",requested,metrics,config,prov_ok,pair_common,dummy,dummy_b)
                            else:
                                score,was_cancelled=_nw_score(tokens_a,tokens_b,p,cancel)
                                if was_cancelled:
                                    execution="cancelled";break
                                if score is not None:
                                    spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.scanmatch.score")
                                    metrics.append(_metric(spec,config,prov_ok,value=score,unit="substitution_score",**pair_common,n_obs=len(tokens_a),duration=len(tokens_a)*bin_s,extra={"grid_cols":int(p["grid_cols"]),"grid_rows":int(p["grid_rows"]),"time_bin_s":bin_s,"substitution_matrix":p["substitution_matrix"],"gap_penalty":float(p["gap_penalty"]),"alignment":"global Needleman-Wunsch","symbols":"row,col spatial cells sampled at first valid observation in each bin"}))
            # Windowed correlations yield a row per window and a mean descriptive summary.
            if "group.windowed_synchrony" in requested and _meta_ok("synchronization_confirmed",config) and _meta_ok("window_parameters",config):
                spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.windowed_synchrony")
                for sig in ("pupil_left","pupil_right"):
                    if not _meta_ok("pupil_unit",config):continue
                    al=_align(fa,fb,sig,tolerance);wlen=float(config["window_length_s"]);step=float(config["window_step_s"])
                    if len(al["ta"])<3:continue
                    start=float(al["ta"][0]);end=float(al["ta"][-1]);vals=[]
                    wstart=start
                    while wstart+wlen<=end+1e-12:
                        sel=(al["ta"]>=wstart)&(al["ta"]<wstart+wlen)&al["va"]&al["vb"]
                        r=_corr(al["a"][sel],al["b"][sel])
                        if r is not None:
                            vals.append(r);window_rows.append({"run_id":config.get("run_id"),"interaction_id":interaction,"pair_id":pid,"segment_id":segment,"signal":sig,"start_s":wstart,"end_s":wstart+wlen,"correlation":r,"n_observed":int(sel.sum()),"parameter_ref":_param_ref(config,{"window_length_s":wlen,"window_step_s":step})})
                            metrics.append(_metric(spec,config,prov_ok,value=r,unit="r",**pair_common,n_obs=int(sel.sum()),duration=wlen,extra={"signal":sig,"window_start_s":wstart,"window_end_s":wstart+wlen,"window_length_s":wlen,"window_step_s":step,"overlap_windows_nonindependent":step<wlen}))
                        wstart+=step
            # Pairwise AOI transition similarity is covered above. Record correlations for team aggregation.
        if execution=="cancelled":break
        # N-person complete-member metrics. Time support is exact/shared and all members are required.
        if _meta_ok("synchronization_confirmed",config) and _meta_ok("cross_subject_spatial_comparable",config):
            ordered_members=[x[2] for x in members]
            t,vals,masks,indices=_all_times(ordered_members,"gaze_x",tolerance)
            if len(t):
                # Reuse identical timestamp matches for Y, preserving duplicate-time row identity.
                ty,ys,yms,indices_y=_all_times(ordered_members,"gaze_y",tolerance)
                allvalid=np.logical_and.reduce(masks)
                if all(len(x)==len(t) for x in vals) and len(ty)==len(t) and np.allclose(ty,t,rtol=0,atol=1e-12) and all(np.array_equal(indices[k],indices_y[k]) for k in range(len(members))):
                    point=np.stack([np.c_[vals[k],ys[k]] for k in range(len(members))],axis=0) if all(len(y)==len(t) for y in ys) else None
                    if point is not None:
                        allvalid &= np.logical_and.reduce(yms)
                        support,dur,maxgap=_valid_support(t,allvalid,config);dt=np.diff(t);w=np.where(support[:-1],dt,0.) if len(dt) else np.array([]);total=float(w.sum())
                        if total>0 and "group.dispersion" in requested:
                            center=np.mean(point,axis=0);rad=np.linalg.norm(point-center[None,:,:],axis=2)
                            value,duration=_time_weighted_mean_values(t,np.mean(rad,axis=0),allvalid,config)
                            spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.dispersion")
                            metrics.append(_metric(spec,config,prov_ok,value=value,unit=str(_meta_value(config,"coordinate_unit")),interaction_id=interaction,segment_id=segment,trial=trial,n_obs=int(allvalid.sum()),duration=duration,extra={"members":len(members),"n_t_distribution":{"min":len(members),"max":len(members)},"definition":"mean member-to-centroid Euclidean radius; trapezoid over valid adjacent support; all members required"}))
                        if aois is not None and _meta_ok("aoi_mapping",config):
                            labs=[];aoi_ok=True
                            for mi,mf in enumerate(ordered_members):
                                sorted_mf=mf.sort_values(["time_s","original_index"],kind="mergesort").reset_index(drop=True)
                                ls,member_shapes,err=_aoi_labels(sorted_mf,aois,config)
                                if err:aoi_ok=False;break
                                if mi==0:all_member_shapes=member_shapes
                                labs.append([ls[i] for i in indices[mi]])
                            if aoi_ok and labs:
                                common=np.logical_and.reduce([np.asarray([bool(v) and len(v)==1 for v in seq],dtype=bool) for seq in labs])&allvalid
                                agree=np.asarray([all(labs[0][i]==seq[i] for seq in labs[1:]) for i in range(len(t))],dtype=bool)
                                sup,ad,_=_valid_support(t,common,config);aw=np.where(sup[:-1],np.diff(t),0.) if len(t)>1 else np.array([]);at=float(aw.sum())
                                if at>0 and "group.all_member_aoi_agreement" in requested:
                                    spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.all_member_aoi_agreement")
                                    metrics.append(_metric(spec,config,prov_ok,value=float(np.sum(aw*agree[:-1])/at),unit="proportion",interaction_id=interaction,segment_id=segment,trial=trial,n_obs=int(common.sum()),duration=at,extra={"member_count":len(members),"missing_policy":"complete specified member set","aoi_geometry_version":AOI_GEOMETRY_VERSION,"aoi_domain_schema_version":AOI_DOMAIN_SCHEMA_VERSION,"aoi_boundary_policy":"closed","aoi_geometry_fingerprints":sorted({str(s.get("aoi_geometry_fingerprint")) for s in all_member_shapes})}))
                                if "group.majority_aoi_agreement" in requested and config.get("enable_experimental") is True:
                                    k=max(int(config["majority_min_members"]),int(math.ceil(float(config["majority_fraction"])*len(members))))
                                    rows=[];n_valid=[];tot=0;hit=0
                                    for i in range(len(t)):
                                        labels=[next(iter(seq[i])) for seq in labs if seq[i] and len(seq[i])==1]
                                        n_valid.append(len(labels))
                                        if len(labels)>=k:
                                            tot+=1;counts=pd.Series(labels).value_counts();hit+=int(counts.max()>=k);rows.append({"time_s":float(t[i]),"n_members":len(labels),"agreement":bool(counts.max()>=k)})
                                    majority_rows.extend({"interaction_id":interaction,"segment_id":segment,**r} for r in rows)
                                    if tot:
                                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.majority_aoi_agreement")
                                        metrics.append(_metric(spec,config,prov_ok,value=hit/tot,unit="proportion",interaction_id=interaction,segment_id=segment,trial=trial,n_obs=tot,duration=at,extra={"majority_min_members":k,"specified_members":len(members),"n_t_distribution":{"min":min(n_valid) if n_valid else 0,"max":max(n_valid) if n_valid else 0},"denominator":"timestamps with at least k valid members","aoi_geometry_version":AOI_GEOMETRY_VERSION,"aoi_domain_schema_version":AOI_DOMAIN_SCHEMA_VERSION,"aoi_boundary_policy":"closed","aoi_geometry_fingerprints":sorted({str(s.get("aoi_geometry_fingerprint")) for s in all_member_shapes})}))
        # Simple within-interaction pairwise correlation mean, Fisher-z averaged.
        cvals=pair_corrs.get((interaction,segment,trial,stream_id,eye_label),[])
        if cvals and "group.mean_pairwise_similarity" in requested:
            corr_values=np.asarray([x[0] for x in cvals],dtype=float);clipped=np.clip(corr_values,-1+1e-15,1-1e-15);mean=float(np.tanh(np.mean(np.arctanh(clipped))))
            spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.mean_pairwise_similarity")
            metrics.append(_metric(spec,config,prov_ok,value=mean,unit="r",interaction_id=interaction,segment_id=segment,trial=trial,n_obs=sum(x[1] for x in cvals),duration=float(np.mean([x[2] for x in cvals])),extra={"available_pair_signal_count":len(cvals),"weighting":"equal pair-signal weight; Fisher z then tanh","pairs_not_independent":True}))
        # Role-based search uses explicitly named members and the same sign convention.
        if "group.role_following.max_correlation" in requested or "group.role_following.best_lag" in requested:
            roles=config.get("roles",{})
            leader=next((sid for sid,role in roles.items() if role=="leader"),None);followers=[sid for sid,role in roles.items() if role=="follower"]
            if leader is not None:
                lead=next((x for x in members if str(x[0])==str(leader)),None)
                if lead:
                    for follower in followers:
                        foll=next((x for x in members if str(x[0])==str(follower)),None)
                        if foll:
                            pid=_pair_id(lead[0],foll[0]);cs=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.role_following.max_correlation");ls=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.role_following.best_lag")
                            cfg=dict(config,lag_max_s=abs(float(config.get("lag_max_s",2.0))))
                            _lag_result(lead[2],foll[2],config.get("lag_signal","pupil_left"),cfg,cs,ls,prov_ok,interaction,pid,segment,trial,None,lag_rows,metrics,role="leader_to_follower")
        if "group.network.edge" in requested or "group.network.strength" in requested:
            # One explicitly selected gaze axis defines one undirected edge per member pair.
            threshold=float(config.get("network_threshold",0))
            strengths={str(sid):0 for sid,_,_ in members}
            for pid,u,v,weight,nobs,duration in network_local:
                if weight>=threshold:
                    strengths[u]=strengths.get(u,0)+1;strengths[v]=strengths.get(v,0)+1
                    network_rows.append({"interaction_id":interaction,"segment_id":segment,"subject_a":u,"subject_b":v,"signal":config.get("network_signal"),"weight":weight,"threshold":threshold,"included":True})
                    if "group.network.edge" in requested:
                        spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.network.edge")
                        metrics.append(_metric(spec,config,prov_ok,value=weight,unit="r",interaction_id=interaction,pair_id=pid,segment_id=segment,n_obs=nobs,duration=duration,extra={"threshold":threshold,"signal":config.get("network_signal"),"edge_rule":"undirected weight >= threshold"}))
            if network_local and "group.network.strength" in requested:
                spec=next(s for s in GROUP_CATALOGUE if s["metric_id"]=="group.network.strength")
                possible=max(1,len(members)*(len(members)-1)//2)
                for sid,val in strengths.items():metrics.append(_metric(spec,config,prov_ok,value=val,unit="count",subject_id=sid,interaction_id=interaction,segment_id=segment,n_obs=len(network_local),duration=float(np.mean([x[5] for x in network_local])),extra={"threshold":threshold,"signal":config.get("network_signal"),"node_strength":"degree count","available_pair_count":len(network_local),"possible_pair_count":possible,"pair_coverage":len(network_local)/possible,"not_leadership":True}))
        if progress:progress((gi+1)/total_groups,f"group {interaction}")
        shared_fields={"analysis_stream_id":stream_id,"eye_label":eye_label,
                       "stream_parent_refs":stream_refs,"support_ref":support_ref,
                       "dependence_ref":tuple(sorted(dependence_refs)) or None}
        for metric_row in metrics[metric_start:]:
            for field,value in shared_fields.items():
                if metric_row.get(field) is None:metric_row[field]=value
    # Every requested ID gets an explicit unavailable row when no qualifying result exists.
    geometry_issue=config.get("_aoi_geometry_issue")
    if isinstance(geometry_issue,dict):
        for row in av_rows:
            if row["metric_id"] in aoi_metric_ids:
                row.update({"available":False,"status":"blocked",
                            "reason_code":geometry_issue.get("code","aoi_geometry_unavailable"),
                            "reason":geometry_issue.get("message","AOI geometry is unavailable")})
        av_map={r["metric_id"]:r for r in av_rows}
        metrics=[m for m in metrics if m["metric_id"] not in aoi_metric_ids]
        majority_rows=[]
    seen={m["metric_id"] for m in metrics}
    for mid in sorted(requested_all-seen):
        spec=next((s for s in GROUP_CATALOGUE if s["metric_id"]==mid),None)
        if not spec:continue
        av=av_map[mid]
        reason=av["reason_code"] if not av["available"] else "no_qualifying_analysis_unit"
        metrics.append(_metric(spec,config,prov_ok,reason=reason))
        if execution!="cancelled":execution="partial"
    if not groups and execution!="cancelled":execution="partial"
    if not prov_ok and metrics and execution!="cancelled":execution="partial"
    if any(x["status"] in {"partial","unavailable"} for x in metrics) and execution=="complete":execution="partial"
    if execution=="cancelled":
        logs=["Analysis cancelled; partial outputs are retained with cancelled bundle status."]
    else:logs=[]
    if not prov_ok:logs.append("Results are partial: source_id and SHA-256 source hashes are required to mark outputs complete.")
    if not groups:logs.append("No explicit interaction_id group with at least two explicit members was available.")
    aoi_prepared=config.get("_aoi_geometry_prepared")
    aoi_budget=config.get("_aoi_geometry_budget")
    aoi_method_audit=None
    if isinstance(aoi_prepared,PreparedStaticAOIs):
        aoi_method_audit={**dict(aoi_prepared.audit),"rows":[{
            "aoi_id":_aoi_audit_scalar(s.get("aoi_id")),"stimulus_id":_aoi_audit_scalar(s.get("stimulus_id")),
            "aoi_name":_aoi_audit_scalar(s.get("aoi_name")),"aoi_name_status":s.get("aoi_name_status"),
            "fingerprint":s.get("aoi_geometry_fingerprint"),"source":s.get("aoi_source"),
        } for s in aoi_prepared.rows]}
        if isinstance(aoi_budget,AOIGeometryBudget):
            aoi_method_audit["counts"].update({"membership_edge_tests":aoi_budget.membership_edge_tests,
                                                "membership_edge_tests_by_scope":dict(aoi_budget.membership_edge_tests_by_scope),
                                                "exact_predicate_evaluations":aoi_budget.exact_predicates})
    if isinstance(geometry_issue,dict):
        aoi_method_audit={"geometry_version":AOI_GEOMETRY_VERSION,
                          "domain_schema_version":AOI_DOMAIN_SCHEMA_VERSION,
                          "boundary_policy":"closed","status":"blocked",
                          "issue":geometry_issue}
    meta={"contract_version":"1.0.0","execution_status":execution,"engine":"group","algorithm_version":ALGORITHM_VERSION,
          "provenance":provenance,"provenance_verified":prov_ok,"requested_metric_ids":sorted(requested_all),
          "source_conditions":{"time_confirmed":_meta_ok("time_confirmed",config),"synchronization_confirmed":_meta_ok("synchronization_confirmed",config),"coordinate_unit":_meta_value(config,"coordinate_unit"),"coordinate_frame":_meta_value(config,"coordinate_frame"),"cross_subject_spatial_comparable":config.get("cross_subject_spatial_comparable") is True,"raw_validity_flags_preserved":True,"interpolated_points_excluded":True,"extended_fields":_extension_field_context(frame,config)},
          "interaction_count":len(groups),"metric_count":len(metrics),"experimental_enabled":config.get("enable_experimental") is True,
          "unavailable_metric_ids":sorted({x["metric_id"] for x in metrics if x["status"]=="unavailable"}),"partial_metric_ids":sorted({x["metric_id"] for x in metrics if x["status"]=="partial"})}
    if aoi_method_audit is not None:
        meta["aoi_method_audit"]=aoi_method_audit
    return AnalysisBundle(tables={"metrics":pd.DataFrame(metrics,columns=METRIC_COLUMNS),"availability":pd.DataFrame(av_rows,columns=AVAILABILITY_COLUMNS),
                                 "lag_curve":pd.DataFrame(lag_rows),"windowed_synchrony":pd.DataFrame(window_rows),"network":pd.DataFrame(network_rows),"majority_support":pd.DataFrame(majority_rows)},metadata=meta,logs=logs)


__all__=["analyze_group","get_group_catalogue","GROUP_CATALOGUE"]
