"""Versioned segment identity, shared scope, membership, and partition contracts.

Segment labels are interval relationships over original observations. They never
duplicate raw rows, and their boundaries remain separate from physical event
boundaries unless a range is explicitly declared physical with supporting semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import unicodedata
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

SCOPE_SCHEMA_VERSION = "1.2.0"
OVERLAP_POLICIES = frozenset({"require_resolution", "keep_labels", "corrected", "reject"})
SCOPE_MODES = frozenset({"participant_session", "shared_interaction"})
SEGMENT_KINDS = frozenset({"analysis_label", "physical_range"})

SEGMENT_AUDIT_COLUMNS = (
    "segment_id", "segment_instance_id", "segment_name", "scope_kind", "status",
    "reason_code", "source_sha256", "source_file_name", "source_sheet", "source_row",
    "segment_id_recipe",
)
MEMBERSHIP_COLUMNS = (
    "segment_id", "segment_instance_id", "segment_name", "scope_kind", "subject_id",
    "session_id", "interaction_id", "analysis_stream_id", "physical_partition_ids",
    "source_ids",
    "start_s", "end_s", "trial", "condition", "stimulus_id", "scope_definition_ref",
    "shared_observation_group_id", "support_observation_ids", "n_unique_observations",
    "effective_duration_s", "timebase_id",
)
OVERLAP_COLUMNS = (
    "overlap_group_id", "segment_id_a", "segment_instance_id_a", "segment_name_a",
    "segment_id_b", "segment_instance_id_b", "segment_name_b", "subject_id", "session_id",
    "interaction_id", "timebase_id", "intersection_start_s", "intersection_end_s", "intersection_duration_s",
    "shared_observation_count", "shared_supported_duration_s", "shared_observation_ids", "status", "reason_code",
)
PARTITION_COLUMNS = (
    "processing_partition_id", "physical_partition_id", "source_id", "subject_id",
    "session_id", "interaction_id", "trial", "start_s", "end_s", "n_observations",
    "start_barrier_reason", "start_barrier_refs", "segment_kind_refs",
)
DERIVATION_COLUMNS = (
    "analysis_stream_id", "segment_id", "segment_instance_id", "observation_id", "eye",
    "raw_value", "baseline_value", "derived_value", "parameter_ref", "status", "reason_code",
)


class ScopeError(ValueError):
    """A segment scope, stable identity, or membership is ambiguous or unsafe."""


@dataclass(frozen=True)
class MemberSessionMapping:
    """Explicit mapping from one interaction member to the session on its clock."""

    subject_id: str
    session_id: str
    interaction_id: str
    timebase_id: str
    start_s: float | None = None
    end_s: float | None = None

    def __post_init__(self) -> None:
        for name in ("subject_id", "session_id", "interaction_id", "timebase_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ScopeError(f"MemberSessionMapping.{name} must be explicit")
        if (self.start_s is None) != (self.end_s is None):
            raise ScopeError("A multi-session member mapping needs both start_s and end_s")
        if self.start_s is not None and (
            not math.isfinite(float(self.start_s)) or not math.isfinite(float(self.end_s))
            or float(self.end_s) <= float(self.start_s)
        ):
            raise ScopeError("Member session mapping intervals must be finite and nonempty")


@dataclass(frozen=True)
class SegmentScopeConfig:
    """Explicit interval scope and conflict policy used while normalizing segments."""

    mode: Literal["participant_session", "shared_interaction"] = "participant_session"
    overlap_policy: Literal["require_resolution", "keep_labels", "corrected", "reject"] = "require_resolution"
    duplicate_policy: Literal["require_resolution", "deduplicate"] = "require_resolution"
    timebase_id: str | None = None
    synchronized: bool = False

    def __post_init__(self) -> None:
        if self.mode not in SCOPE_MODES:
            raise ScopeError("Unsupported segment scope mode")
        if self.overlap_policy not in OVERLAP_POLICIES:
            raise ScopeError("Unsupported segment overlap policy")
        if self.duplicate_policy not in {"require_resolution", "deduplicate"}:
            raise ScopeError("Unsupported duplicate segment policy")
        if self.timebase_id is not None and (not isinstance(self.timebase_id, str) or not self.timebase_id.strip()):
            raise ScopeError("timebase_id must be nonempty when supplied")
        if self.mode == "shared_interaction" and not self.synchronized:
            raise ScopeError("Shared interaction scope requires an explicit synchronized clock mapping")


@dataclass(frozen=True)
class ScopeRelations:
    processing_partitions: pd.DataFrame = field(default_factory=lambda: empty_table(PARTITION_COLUMNS))
    analysis_memberships: pd.DataFrame = field(default_factory=lambda: empty_table(MEMBERSHIP_COLUMNS))
    segment_overlap: pd.DataFrame = field(default_factory=lambda: empty_table(OVERLAP_COLUMNS))
    segment_scope_audit: pd.DataFrame = field(default_factory=lambda: empty_table(SEGMENT_AUDIT_COLUMNS))
    assignments: pd.DataFrame = field(default_factory=pd.DataFrame)
    blocked: bool = False
    reason: str | None = None


def empty_table(columns: Sequence[str]) -> pd.DataFrame:
    """Return a stable zero-row table with declared nullable dtypes."""
    string_columns = {
        "segment_id", "segment_instance_id", "segment_name", "scope_kind", "status", "reason_code",
        "source_sha256", "source_file_name", "source_sheet", "segment_id_recipe", "analysis_stream_id",
        "subject_id", "session_id", "interaction_id", "timebase_id", "scope_definition_ref",
        "processing_partition_id", "physical_partition_id", "source_id", "start_barrier_reason",
        "segment_kind_refs", "overlap_group_id", "segment_id_a", "segment_instance_id_a",
        "segment_name_a", "segment_id_b", "segment_instance_id_b", "segment_name_b", "eye",
        "parameter_ref",
    }
    float_columns = {
        "start_s", "end_s", "effective_duration_s", "intersection_start_s", "intersection_end_s",
        "intersection_duration_s", "shared_supported_duration_s", "raw_value", "baseline_value", "derived_value",
    }
    integer_columns = {"source_row", "n_unique_observations", "shared_observation_count", "n_observations"}
    result: dict[str, pd.Series] = {}
    for column in columns:
        dtype = "string" if column in string_columns else "Float64" if column in float_columns else "Int64" if column in integer_columns else "object"
        result[column] = pd.Series(dtype=dtype)
    return pd.DataFrame(result)


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _identifier(value: Any, label: str) -> str:
    if _missing(value):
        raise ScopeError(f"{label} must be explicit")
    text = unicodedata.normalize("NFC", str(value).strip())
    if not text:
        raise ScopeError(f"{label} must be nonempty")
    return text


def _json_value(value: Any) -> Any:
    if _missing(value):
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.strip())
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ScopeError("Segment content cannot contain NaN or infinity")
        # A lossless normalized float token avoids locale or CSV spelling effects.
        return {"float_hex": value.hex()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    raise ScopeError(f"Unsupported segment identity value: {type(value).__name__}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def observation_identity_frame(
    frame: pd.DataFrame,
    *,
    source_id: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """Add deterministic source/sample identities without changing source values."""
    return _observation_identity_frame_owned(frame.copy(deep=True), source_id=source_id)


def _observation_identity_frame_owned(
    out: pd.DataFrame, *, source_id: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """Validate/add identities on a private, unpublished frame owned by the caller."""
    required = {"subject_id", "session_id", "original_index"}
    if required - set(out.columns):
        raise ScopeError("Observation identity needs subject_id, session_id, and original_index")
    if "source_id" in out:
        if out["source_id"].isna().any() or out["source_id"].astype("string").str.strip().eq("").any():
            raise ScopeError("source_id must be present for every observation")
    else:
        fallback = source_id
        if fallback is None:
            fallback = "legacy:" + _digest({"identity_kind": "legacy_dataframe", "columns": sorted(map(str, out.columns))})
        out["source_id"] = fallback
    if "sample_index" not in out:
        out["sample_index"] = pd.NA
    ids: list[str] = []
    identity_columns = [key for key in (
        "source_id", "subject_id", "session_id", "original_index", "sample_index",
        "interaction_id", "timebase_id",
    ) if key in out]
    identity_rows = out.loc[:, identity_columns]
    scope_columns = [key for key in ("source_id", "subject_id", "session_id", "interaction_id", "timebase_id") if key in out]
    # A whole column can share one normalized scope only when all its values
    # are missing, or are exactly equal built-in strings. Mixed numeric/object
    # columns always take the original per-row path (including bool/int and ±0).
    constant_scope = bool(len(identity_rows))
    for column in scope_columns:
        values = identity_rows[column]
        if values.isna().all():
            continue
        first_value = values.iloc[0]
        if type(first_value) is not str or not all(type(value) is str and value == first_value for value in values):
            constant_scope = False
            break
    normalized_scope: tuple[dict[str, Any], str] | None = None
    for row in identity_rows.itertuples(index=False, name=None):
        record = dict(zip(identity_columns, row))
        sample = record.get("sample_index")
        identity_index = record.get("original_index") if _missing(sample) else sample
        if not constant_scope or normalized_scope is None:
            required_scope = {
                key: _json_value(record.get(key))
                for key in ("subject_id", "session_id")
                if key in record
            }
            optional_scope = {
                key: _json_value(record.get(key))
                for key in ("interaction_id", "timebase_id")
                if key in record
                and not _missing(record.get(key))
                and not (isinstance(record.get(key), str) and not record.get(key).strip())
            }
            normalized_scope = ({**required_scope, **optional_scope}, _identifier(record.get("source_id"), "source_id"))
        scope, normalized_source = normalized_scope
        payload = {
            "schema": "observation-id-v1",
            "source_id": normalized_source,
            "scope": scope,
            "sample_index": _json_value(identity_index),
        }
        ids.append("obs:" + _digest(payload))
    if len(set(ids)) != len(ids):
        raise ScopeError("Observation identity collision: resolve duplicate source/sample indexes explicitly")
    if "observation_id" in out:
        supplied_mask = out["observation_id"].notna()
        if supplied_mask.any() and not supplied_mask.all():
            raise ScopeError("observation_id cannot be partially supplied")
        supplied_ids = out["observation_id"].astype("string").tolist()
        if supplied_mask.all() and supplied_ids != ids:
            # Only a supplied noncanonical ID needs the old recipe, whose scope
            # included null/blank optional keys whenever the columns existed.
            legacy_ids: list[str] = []
            for row in identity_rows.itertuples(index=False, name=None):
                record = dict(zip(identity_columns, row))
                sample = record.get("sample_index")
                legacy_ids.append("obs:" + _digest({
                    "schema": "observation-id-v1",
                    "source_id": _identifier(record.get("source_id"), "source_id"),
                    "scope": {
                        key: _json_value(record.get(key))
                        for key in ("subject_id", "session_id", "interaction_id", "timebase_id")
                        if key in record
                    },
                    "sample_index": _json_value(record.get("original_index") if _missing(sample) else sample),
                }))
            if supplied_ids != legacy_ids:
                raise ScopeError("Supplied observation_id does not match the declared source/sample identity")
    else:
        out["observation_id"] = ids
    unique_sources = sorted(out["source_id"].astype(str).unique()) if len(out) else []
    resolved_source = unique_sources[0] if len(unique_sources) == 1 else (
        "source-set:" + _digest(unique_sources) if unique_sources else (source_id or "legacy:empty")
    )
    return out, resolved_source


def _config(value: SegmentScopeConfig | Mapping[str, Any] | None) -> SegmentScopeConfig:
    if value is None:
        return SegmentScopeConfig()
    if isinstance(value, SegmentScopeConfig):
        return value
    if not isinstance(value, Mapping):
        raise ScopeError("scope_config must be SegmentScopeConfig or a mapping")
    return SegmentScopeConfig(**dict(value))


def _mapping_entries(
    value: Mapping[str, Any] | Sequence[MemberSessionMapping] | None,
    *,
    interaction_id: str,
    timebase_id: str | None,
) -> list[MemberSessionMapping]:
    if value is None:
        return []
    entries: list[MemberSessionMapping] = []
    if isinstance(value, Mapping):
        for subject, session in value.items():
            if isinstance(session, str):
                if timebase_id is None:
                    raise ScopeError("Shared scope needs an explicit timebase_id")
                entries.append(MemberSessionMapping(str(subject), session, interaction_id, timebase_id))
            else:
                raise ScopeError("Multiple sessions for one member need explicit MemberSessionMapping intervals")
    else:
        for item in value:
            if isinstance(item, MemberSessionMapping):
                entries.append(item)
            elif isinstance(item, Mapping):
                entries.append(MemberSessionMapping(**dict(item)))
            else:
                raise ScopeError("member_session_map entries must be explicit MemberSessionMapping records")
    selected = [entry for entry in entries if entry.interaction_id == interaction_id]
    by_subject: dict[str, list[MemberSessionMapping]] = {}
    for entry in selected:
        by_subject.setdefault(entry.subject_id, []).append(entry)
        if timebase_id is not None and entry.timebase_id != timebase_id:
            raise ScopeError("Member mapping timebase does not match the shared segment timebase")
    for subject, items in by_subject.items():
        if len(items) > 1:
            if any(item.start_s is None for item in items):
                raise ScopeError(f"Member {subject!r} has ambiguous multiple sessions; supply interval mappings")
            ordered = sorted(items, key=lambda item: float(item.start_s))
            if any(float(a.end_s) > float(b.start_s) for a, b in zip(ordered, ordered[1:])):
                raise ScopeError(f"Member {subject!r} session mapping intervals overlap")
    return sorted(selected, key=lambda item: (
        item.subject_id, item.session_id,
        -math.inf if item.start_s is None else float(item.start_s),
    ))


def _dataset_pairs(dataset_scope: Any) -> tuple[set[tuple[str, str]], list[dict[str, Any]]]:
    if dataset_scope is None:
        return set(), []
    if isinstance(dataset_scope, pd.DataFrame):
        if not {"subject_id", "session_id"}.issubset(dataset_scope.columns):
            raise ScopeError("dataset_scope table needs subject_id and session_id")
        rows = dataset_scope.to_dict("records")
    elif isinstance(dataset_scope, Mapping):
        if "members" in dataset_scope:
            rows = list(dataset_scope["members"])
        else:
            rows = []
            for subject, sessions in dataset_scope.items():
                if subject in {"timebase_id", "interaction_id", "synchronized"}:
                    continue
                if isinstance(sessions, str):
                    rows.append({"subject_id": subject, "session_id": sessions})
                elif isinstance(sessions, Sequence):
                    for session in sessions:
                        if isinstance(session, Mapping):
                            rows.append({"subject_id": subject, **dict(session)})
                        else:
                            rows.append({"subject_id": subject, "session_id": session})
                else:
                    raise ScopeError("dataset_scope must enumerate member sessions")
    else:
        raise ScopeError("dataset_scope must be a mapping or DataFrame")
    normalized = []
    pairs = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ScopeError("dataset_scope members must be mappings")
        subject = _identifier(row.get("subject_id"), "dataset_scope.subject_id")
        session = _identifier(row.get("session_id"), "dataset_scope.session_id")
        pairs.add((subject, session))
        normalized.append(dict(row))
    return pairs, normalized


def _segment_payload(record: Mapping[str, Any], members: Sequence[MemberSessionMapping], timebase_id: str) -> dict[str, Any]:
    mode = str(record.get("scope_kind") or record.get("scope_mode") or "participant_session")
    if mode == "shared_interaction":
        scope = {
            "scope_kind": mode,
            "interaction_id": _identifier(record.get("interaction_id"), "interaction_id"),
            "timebase_id": timebase_id,
            "members": [
                {
                    "subject_id": item.subject_id,
                    "session_id": item.session_id,
                    "start_s": _json_value(item.start_s),
                    "end_s": _json_value(item.end_s),
                    "timebase_id": item.timebase_id,
                }
                for item in members
            ],
        }
    else:
        scope = {
            "scope_kind": "participant_session",
            "subject_id": _identifier(record.get("subject_id"), "subject_id"),
            "session_id": _identifier(record.get("session_id"), "session_id"),
            "interaction_id": _json_value(record.get("interaction_id")),
            "timebase_id": timebase_id,
        }
    payload = {
        "recipe": "segment-content-v1",
        "scope": scope,
        "segment_name": _identifier(record.get("segment_name"), "segment_name"),
        "start_s": _json_value(float(record["start_s"])),
        "end_s": _json_value(float(record["end_s"])),
        "condition": _json_value(record.get("condition")),
        "trial": _json_value(record.get("trial")),
        "stimulus_id": _json_value(record.get("stimulus_id")),
        "segment_kind": str(record.get("segment_kind") or "analysis_label"),
    }
    physical_semantics_ref = record.get("physical_semantics_ref")
    if not _missing(physical_semantics_ref):
        payload["physical_semantics_ref"] = _json_value(physical_semantics_ref)
    baseline_start = record.get("baseline_start_s")
    baseline_end = record.get("baseline_end_s")
    if not _missing(baseline_start) or not _missing(baseline_end):
        if _missing(baseline_start) or _missing(baseline_end):
            raise ScopeError("Label baseline windows need both baseline_start_s and baseline_end_s")
        start, end = float(baseline_start), float(baseline_end)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ScopeError("Label baseline window must be finite and nonempty")
        payload["baseline_window_s"] = [_json_value(start), _json_value(end)]
    return payload


def _instance_id(segment_id: str, subject_id: str, session_id: str, interaction_id: str | None,
                 timebase_id: str) -> str:
    payload = {
        "recipe": "segment-instance-v1", "segment_id": segment_id,
        "subject_id": subject_id, "session_id": session_id,
        "interaction_id": interaction_id, "timebase_id": timebase_id,
    }
    return "segment-instance:" + _digest(payload)


def _scope_block_reason(error: ScopeError) -> str:
    """Map shared-scope validation failures to stable, reviewable audit codes."""
    message = str(error).casefold()
    if "unknown shared member session" in message:
        return "shared_member_session_unknown"
    if "ambiguous multiple sessions" in message:
        return "shared_member_sessions_ambiguous"
    if "mapping intervals overlap" in message:
        return "shared_member_mapping_overlap"
    if "timebase" in message or "clock" in message:
        return "shared_timebase_conflict"
    if "conflicting interaction_id" in message:
        return "shared_interaction_conflict"
    if "conflicting trial" in message:
        return "shared_trial_conflict"
    if "conflicting condition" in message:
        return "shared_condition_conflict"
    if "conflicting stimulus_id" in message:
        return "shared_stimulus_conflict"
    if "no member-session mapping overlaps" in message:
        return "shared_member_mapping_out_of_scope"
    if "no explicit member-session mappings" in message:
        return "shared_member_mapping_missing"
    if "dataset_scope" in message:
        return "shared_dataset_scope_invalid"
    if "synchronized" in message:
        return "shared_clock_not_confirmed"
    return "shared_scope_conflict"


def _blocked_scope_audit(row: Mapping[str, Any], row_index: Any, reason_code: str) -> dict[str, Any]:
    """Preserve the source locator without creating a normalized member instance."""
    source_row = row.get("source_row", row_index)
    return {
        "segment_id": None if _missing(row.get("segment_id")) else row.get("segment_id"),
        "segment_instance_id": None,
        "segment_name": None if _missing(row.get("segment_name")) else row.get("segment_name"),
        "scope_kind": None if _missing(row.get("scope_kind")) else row.get("scope_kind"),
        "status": "blocked",
        "reason_code": reason_code,
        "source_sha256": None if _missing(row.get("source_sha256")) else row.get("source_sha256"),
        "source_file_name": None if _missing(row.get("source_file_name")) else row.get("source_file_name"),
        "source_sheet": None if _missing(row.get("source_sheet")) else row.get("source_sheet"),
        "source_row": source_row,
        "segment_id_recipe": None,
    }


def normalize_segment_records(
    records: pd.DataFrame,
    *,
    scope_config: SegmentScopeConfig | Mapping[str, Any] | None = None,
    member_session_map: Mapping[str, Any] | Sequence[MemberSessionMapping] | None = None,
    dataset_scope: Any = None,
    timebase_id: str | None = None,
    source_audit: Mapping[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the segment-content-v1 identity recipe and optionally expand shared labels."""
    cfg = _config(scope_config)
    if records.empty:
        return pd.DataFrame(columns=records.columns), empty_table(SEGMENT_AUDIT_COLUMNS)
    required = {"start_s", "end_s"}
    if cfg.mode == "participant_session":
        required.update({"subject_id", "session_id"})
    else:
        required.add("interaction_id")
    if required - set(records.columns):
        raise ScopeError("Segments missing: " + ", ".join(sorted(required - set(records.columns))))
    data = records.copy(deep=True)
    if "segment_name" not in data:
        if "name" not in data:
            raise ScopeError("New segment records require segment_name")
        data["segment_name"] = data["name"]
    if "name" in data:
        canonical_name = data["segment_name"].map(lambda value: None if _missing(value) else unicodedata.normalize("NFC", str(value).strip()))
        legacy_name = data["name"].map(lambda value: None if _missing(value) else unicodedata.normalize("NFC", str(value).strip()))
        conflicts = canonical_name.notna() & legacy_name.notna() & canonical_name.ne(legacy_name)
        if conflicts.any():
            row = data.index[int(np.flatnonzero(conflicts.to_numpy())[0])]
            raise ScopeError(f"name and segment_name conflict at source row {row}")
    if "scope_kind" not in data:
        data["scope_kind"] = cfg.mode
    if "segment_kind" not in data:
        data["segment_kind"] = "analysis_label"
    if "overlap_policy" not in data:
        data["overlap_policy"] = cfg.overlap_policy
    if "segment_id" not in data:
        data["segment_id"] = pd.NA
    for column in ("subject_id", "session_id", "interaction_id", "condition", "trial", "stimulus_id",
                   "baseline_start_s", "baseline_end_s", "corrected_from_segment_id"):
        if column not in data:
            data[column] = pd.NA
    if "physical_semantics_ref" not in data:
        data["physical_semantics_ref"] = pd.NA
    pairs, dataset_rows = _dataset_pairs(dataset_scope)
    if source_audit:
        for column, key in (("source_sha256", "sha256"), ("source_file_name", "source_name"),
                            ("source_sheet", "sheet_name")):
            if column not in data and source_audit.get(key) is not None:
                data[column] = source_audit[key]
        if "source_row" not in data:
            data["source_row"] = list(records.index)
    expanded: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    semantic_seen: dict[str, int] = {}
    supplied_ids: dict[tuple[str, str], str] = {}
    duplicate_source_rows: dict[str, list[Any]] = {}
    for row_index, series in data.iterrows():
        row = series.to_dict()
        scope_kind = str(row.get("scope_kind") or cfg.mode)
        if scope_kind not in SCOPE_MODES:
            raise ScopeError("Unsupported segment scope_kind")
        segment_kind = str(row.get("segment_kind") or "analysis_label")
        if segment_kind not in SEGMENT_KINDS:
            raise ScopeError("Unsupported segment_kind")
        if segment_kind == "physical_range" and _missing(row.get("physical_semantics_ref")):
            raise ScopeError("physical_range requires an explicit physical_semantics_ref")
        segment_name = _identifier(row.get("segment_name"), "segment_name")
        start, end = float(row["start_s"]), float(row["end_s"])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ScopeError("Segment intervals must be finite, nonempty [start,end) ranges")
        interaction = None if _missing(row.get("interaction_id")) else _identifier(row.get("interaction_id"), "interaction_id")
        row_timebase = row.get("timebase_id")
        selected_timebase = timebase_id or cfg.timebase_id
        if not _missing(row_timebase):
            if selected_timebase is not None and str(row_timebase).strip() != selected_timebase:
                if scope_kind == "shared_interaction":
                    audit.append(_blocked_scope_audit(row, row_index, "shared_timebase_conflict"))
                    continue
                raise ScopeError("Segment row timebase_id conflicts with scope configuration")
            selected_timebase = _identifier(row_timebase, "timebase_id")
        if selected_timebase is None:
            if scope_kind == "participant_session" and not _missing(row.get("subject_id")) and not _missing(row.get("session_id")):
                session_rows = [item for item in dataset_rows if
                                str(item.get("subject_id")) == str(row.get("subject_id")) and
                                str(item.get("session_id")) == str(row.get("session_id")) and
                                not _missing(item.get("timebase_id"))]
                declared_timebases = sorted({_identifier(item.get("timebase_id"), "dataset_scope.timebase_id")
                                             for item in session_rows})
                if len(declared_timebases) == 1:
                    selected_timebase = declared_timebases[0]
                elif len(declared_timebases) > 1:
                    raise ScopeError("Participant segment has multiple candidate timebases; select one explicitly")
            if selected_timebase is None:
                selected_timebase = "unconfirmed"
        members: list[MemberSessionMapping] = []
        if scope_kind == "shared_interaction":
            try:
                if not interaction:
                    raise ScopeError("Shared interaction segment needs an explicit interaction_id")
                if not cfg.synchronized:
                    raise ScopeError("Shared interaction segment needs synchronized clock semantics")
                if selected_timebase == "unconfirmed":
                    raise ScopeError("Shared interaction segment needs an explicit timebase_id")
                members = _mapping_entries(member_session_map, interaction_id=interaction, timebase_id=selected_timebase)
                if not members:
                    raise ScopeError("Shared interaction has no explicit member-session mappings")
                if dataset_scope is None:
                    raise ScopeError("Shared interaction expansion requires explicit dataset_scope")
                for member in members:
                    if (member.subject_id, member.session_id) not in pairs:
                        raise ScopeError(f"Unknown shared member session: {member.subject_id}/{member.session_id}")
                    if member.start_s is not None and (end <= float(member.start_s) or start >= float(member.end_s)):
                        continue
                    for dataset_row in dataset_rows:
                        if (str(dataset_row.get("subject_id")), str(dataset_row.get("session_id"))) != (member.subject_id, member.session_id):
                            continue
                        if dataset_row.get("interaction_id") not in (None, interaction):
                            raise ScopeError("Shared member session has a conflicting interaction_id")
                        if dataset_row.get("timebase_id") not in (None, selected_timebase):
                            raise ScopeError("Shared member session has a conflicting timebase_id")
                        for field_name in ("trial", "condition", "stimulus_id"):
                            if not _missing(row.get(field_name)) and not _missing(dataset_row.get(field_name)):
                                if _identifier(row[field_name], field_name) != _identifier(dataset_row[field_name], field_name):
                                    raise ScopeError(f"Shared member session has conflicting {field_name}")
                if not any(member.start_s is None or not (
                    end <= float(member.start_s) or start >= float(member.end_s)
                ) for member in members):
                    raise ScopeError("No member-session mapping overlaps the shared segment interval")
            except ScopeError as exc:
                audit.append(_blocked_scope_audit(row, row_index, _scope_block_reason(exc)))
                continue
        else:
            subject = _identifier(row.get("subject_id"), "subject_id")
            session = _identifier(row.get("session_id"), "session_id")
            if dataset_scope is not None and pairs and (subject, session) not in pairs:
                raise ScopeError(f"Participant segment is outside dataset_scope: {subject}/{session}")
        canonical_row = {
            **row,
            "segment_name": segment_name,
            "name": segment_name,
            "scope_kind": scope_kind,
            "segment_kind": segment_kind,
            "start_s": start,
            "end_s": end,
            "interaction_id": interaction,
            "timebase_id": selected_timebase,
        }
        if str(row.get("overlap_policy") or cfg.overlap_policy) == "corrected" and _missing(row.get("corrected_from_segment_id")):
            raise ScopeError("Corrected segment tables need corrected_from_segment_id provenance")
        payload = _segment_payload(canonical_row, members, selected_timebase)
        semantic_hash = _digest(payload)
        supplied_id = row.get("segment_id")
        if _missing(supplied_id) or not str(supplied_id).strip():
            segment_id = "segment:" + semantic_hash
            id_recipe = "segment-content-v1"
        else:
            segment_id = _identifier(supplied_id, "segment_id")
            id_recipe = "user_supplied"
        source_row = row.get("source_row", row_index)
        duplicate_source_rows.setdefault(semantic_hash, []).append(source_row)
        if semantic_hash in semantic_seen:
            if cfg.duplicate_policy != "deduplicate":
                raise ScopeError("Identical semantic segment definitions need explicit duplicate_policy='deduplicate'")
            audit.append({
                "segment_id": segment_id, "segment_instance_id": None, "segment_name": segment_name,
                "scope_kind": scope_kind, "status": "deduplicated", "reason_code": "explicit_duplicate_resolution",
                "source_sha256": row.get("source_sha256"), "source_file_name": row.get("source_file_name"),
                "source_sheet": row.get("source_sheet"), "source_row": source_row,
                "segment_id_recipe": id_recipe,
            })
            continue
        semantic_seen[semantic_hash] = row_index
        uniqueness_scope = (
            scope_kind,
            interaction if scope_kind == "shared_interaction" else _identifier(row.get("subject_id"), "subject_id"),
            "" if scope_kind == "shared_interaction" else _identifier(row.get("session_id"), "session_id"),
            segment_id,
        )
        prior = supplied_ids.get(uniqueness_scope)
        if prior is not None and prior != semantic_hash:
            raise ScopeError("Same segment_id has conflicting content in its scope")
        supplied_ids[uniqueness_scope] = semantic_hash
        if scope_kind == "shared_interaction":
            for member in members:
                if member.start_s is not None and (end <= float(member.start_s) or start >= float(member.end_s)):
                    continue
                instance_start = max(start, float(member.start_s)) if member.start_s is not None else start
                instance_end = min(end, float(member.end_s)) if member.end_s is not None else end
                if instance_end <= instance_start:
                    continue
                instance = {
                    **canonical_row,
                    "segment_id": segment_id,
                    "segment_instance_id": _instance_id(segment_id, member.subject_id, member.session_id, interaction, member.timebase_id),
                    "subject_id": member.subject_id,
                    "session_id": member.session_id,
                    "member_session_map": [{"subject_id": item.subject_id, "session_id": item.session_id,
                                             "interaction_id": item.interaction_id, "timebase_id": item.timebase_id,
                                             "start_s": item.start_s, "end_s": item.end_s} for item in members],
                    "scope_definition_ref": "segment-content-v1:" + semantic_hash,
                    "start_s": instance_start,
                    "end_s": instance_end,
                    "segment_id_recipe": id_recipe,
                    "physical_semantics_ref": row.get("physical_semantics_ref"),
                    "baseline_start_s": row.get("baseline_start_s"),
                    "baseline_end_s": row.get("baseline_end_s"),
                    "corrected_from_segment_id": row.get("corrected_from_segment_id"),
                }
                expanded.append(instance)
        else:
            subject = _identifier(row.get("subject_id"), "subject_id")
            session = _identifier(row.get("session_id"), "session_id")
            instance = {
                **canonical_row,
                "segment_id": segment_id,
                "segment_instance_id": _instance_id(segment_id, subject, session, interaction, selected_timebase),
                "subject_id": subject,
                "session_id": session,
                "member_session_map": [],
                "scope_definition_ref": "segment-content-v1:" + semantic_hash,
                "segment_id_recipe": id_recipe,
                "baseline_start_s": row.get("baseline_start_s"),
                "baseline_end_s": row.get("baseline_end_s"),
                "corrected_from_segment_id": row.get("corrected_from_segment_id"),
            }
            expanded.append(instance)
    out = pd.DataFrame.from_records(expanded)
    if not out.empty:
        out = out.sort_values(
            [name for name in ("scope_kind", "subject_id", "session_id", "interaction_id", "start_s", "end_s", "segment_id") if name in out],
            kind="stable", ignore_index=True,
        )
    for key, source_rows in duplicate_source_rows.items():
        if len(source_rows) > 1:
            matching = out["scope_definition_ref"].eq("segment-content-v1:" + key) if "scope_definition_ref" in out else pd.Series(False, index=out.index)
            if matching.any():
                if "duplicate_source_rows" not in out:
                    out["duplicate_source_rows"] = pd.Series([None] * len(out), index=out.index, dtype="object")
                for row_index in out.index[matching]:
                    out.at[row_index, "duplicate_source_rows"] = tuple(source_rows)
    if not out.empty:
        audit.extend({
            "segment_id": row.get("segment_id"), "segment_instance_id": row.get("segment_instance_id"),
            "segment_name": row.get("segment_name"), "scope_kind": row.get("scope_kind"),
            "status": "normalized", "reason_code": "", "source_sha256": row.get("source_sha256"),
            "source_file_name": row.get("source_file_name"), "source_sheet": row.get("source_sheet"),
            "source_row": row.get("source_row"), "segment_id_recipe": row.get("segment_id_recipe"),
        } for row in out.to_dict("records"))
    return out.reset_index(drop=True), pd.DataFrame.from_records(audit, columns=SEGMENT_AUDIT_COLUMNS)


def adapt_legacy_segments(segments: pd.DataFrame) -> pd.DataFrame:
    """Read old ID-only segment tables without pretending they are new named imports."""
    out = segments.copy(deep=True)
    if out.empty:
        return out
    if "segment_name" in out and "name" in out:
        left = out["segment_name"].map(lambda value: None if _missing(value) else unicodedata.normalize("NFC", str(value).strip()))
        right = out["name"].map(lambda value: None if _missing(value) else unicodedata.normalize("NFC", str(value).strip()))
        conflicts = left.notna() & right.notna() & left.ne(right)
        if conflicts.any():
            raise ScopeError("name and segment_name conflict")
    if "segment_name" not in out:
        if "name" in out:
            out["segment_name"] = out["name"]
            out["segment_name_origin"] = "legacy_name_alias"
        elif "segment_id" in out:
            out["segment_name"] = out["segment_id"].map(lambda value: f"[legacy:{value}]" if not _missing(value) else pd.NA)
            out["segment_name_origin"] = "legacy_id_display_only"
        else:
            raise ScopeError("Legacy segments need at least a segment_id for display")
    elif "segment_name_origin" not in out:
        out["segment_name_origin"] = "explicit"
    missing_name = out["segment_name"].isna() | out["segment_name"].astype("string").str.strip().eq("")
    if missing_name.any():
        if "segment_id" not in out:
            raise ScopeError("New segment records require segment_name")
        out.loc[missing_name, "segment_name"] = out.loc[missing_name, "segment_id"].map(lambda value: f"[legacy:{value}]")
        out.loc[missing_name, "segment_name_origin"] = "legacy_id_display_only"
    if "name" not in out:
        out["name"] = out["segment_name"]
    if "scope_kind" not in out:
        out["scope_kind"] = "participant_session"
    if "segment_kind" not in out:
        out["segment_kind"] = "analysis_label"
    if "timebase_id" not in out:
        out["timebase_id"] = "legacy_unconfirmed"
    if "segment_instance_id" not in out:
        instances = []
        for row in out.to_dict("records"):
            sid = _identifier(row.get("segment_id"), "segment_id")
            subject = _identifier(row.get("subject_id"), "subject_id")
            session = _identifier(row.get("session_id"), "session_id")
            interaction = None if _missing(row.get("interaction_id")) else str(row.get("interaction_id"))
            instances.append(_instance_id(sid, subject, session, interaction, str(row["timebase_id"])))
        out["segment_instance_id"] = instances
    return out


def _same_scope_mask(frame: pd.DataFrame, segment: Mapping[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for column in ("subject_id", "session_id", "interaction_id", "timebase_id", "source_id",
                   "trial", "condition", "stimulus_id"):
        value = segment.get(column)
        if column == "timebase_id" and str(value or "") in {"unconfirmed", "legacy_unconfirmed"}:
            continue
        if column not in frame:
            if not _missing(value):
                raise ScopeError(f"Segment {column} cannot be matched to signal rows")
            continue
        if not _missing(value):
            mask &= frame[column].astype("string").eq(str(value)).fillna(False)
        elif column in {"subject_id", "session_id"}:
            raise ScopeError(f"Segment {column} must be explicit")
    return mask.fillna(False).astype(bool)


def _supported_duration(times: Sequence[float], start: float, end: float, max_gap_s: float | None) -> float:
    if max_gap_s is None or not math.isfinite(float(max_gap_s)) or float(max_gap_s) <= 0:
        return 0.0
    values = np.asarray(times, dtype=float)
    total = 0.0
    for a, b in zip(values, values[1:]):
        delta = b - a
        if np.isfinite(a) and np.isfinite(b) and 0 < delta <= max_gap_s:
            total += max(0.0, min(float(b), float(end)) - max(float(a), float(start)))
    return total


def _measurement_usable(frame: pd.DataFrame) -> np.ndarray:
    """Mark rows with at least one finite, non-invalid canonical eye measurement."""
    usable = np.zeros(len(frame), dtype=bool)
    found_measurement = False
    if {"gaze_x", "gaze_y"}.issubset(frame.columns):
        found_measurement = True
        finite = np.isfinite(pd.to_numeric(frame["gaze_x"], errors="coerce").to_numpy(dtype=float, na_value=np.nan))
        finite &= np.isfinite(pd.to_numeric(frame["gaze_y"], errors="coerce").to_numpy(dtype=float, na_value=np.nan))
        source = frame.get("source_gaze_valid", frame.get("gaze_valid", pd.Series(pd.NA, index=frame.index)))
        usable |= finite & source.ne(False).fillna(True).to_numpy(dtype=bool)
    for eye in ("left", "right"):
        column = f"pupil_{eye}"
        if column not in frame:
            continue
        found_measurement = True
        finite = np.isfinite(pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float, na_value=np.nan))
        source = frame.get(
            f"source_pupil_{eye}_valid",
            frame.get(f"pupil_{eye}_valid", pd.Series(pd.NA, index=frame.index)),
        )
        usable |= finite & source.ne(False).fillna(True).to_numpy(dtype=bool)
    return usable if found_measurement else np.ones(len(frame), dtype=bool)


def derive_scope_relations(
    frame: pd.DataFrame,
    segments: pd.DataFrame,
    *,
    overlap_policy: str,
    gap_threshold_for: Any,
    stream_keys: Sequence[str],
) -> ScopeRelations:
    """Build one-row-per-observation assignments plus interval membership relations."""
    if overlap_policy not in OVERLAP_POLICIES:
        raise ScopeError("Unsupported overlap policy")
    out = frame.copy(deep=True)
    out, _ = _observation_identity_frame_owned(out)
    return _derive_scope_relations_identified(
        out, segments, overlap_policy=overlap_policy,
        gap_threshold_for=gap_threshold_for, stream_keys=stream_keys,
    )


def _identity_values_are_immutable(frame: pd.DataFrame) -> bool:
    """Limit internal validation reuse to scalar identity values without state."""
    for column in ("source_id", "subject_id", "session_id", "original_index", "sample_index", "interaction_id", "timebase_id", "observation_id"):
        if column not in frame:
            continue
        values = frame[column]
        if isinstance(values.dtype, pd.StringDtype) or pd.api.types.is_numeric_dtype(values.dtype):
            continue
        if not all(value is pd.NA or value is pd.NaT or type(value) in (str, int, float, bool, type(None))
                   or isinstance(value, (np.integer, np.floating, np.bool_, np.str_)) for value in values):
            return False
    return True


def _derive_scope_relations_identified(
    out: pd.DataFrame, segments: pd.DataFrame, *, overlap_policy: str,
    gap_threshold_for: Any, stream_keys: Sequence[str],
) -> ScopeRelations:
    """Internal core on an owned frame validated immediately by its caller."""
    if overlap_policy not in OVERLAP_POLICIES:
        raise ScopeError("Unsupported overlap policy")
    if segments.empty:
        parts, part_table = _partitions(out, pd.DataFrame(), gap_threshold_for, stream_keys)
        assignments = pd.DataFrame({"segment_id": [pd.NA] * len(out), "processing_partition_id": parts["processing_partition_id"],
                                    "segment_instance_id": [pd.NA] * len(out),
                                    "analysis_membership_ids": [tuple() for _ in range(len(out))],
                                    "physical_partition_id": parts["physical_partition_id"],
                                    "processing_barrier_before": parts["processing_barrier_before"],
                                    "processing_barrier_reasons": parts["processing_barrier_reasons"],
                                    "event_barrier_before": parts["event_barrier_before"],
                                    "event_barrier_reasons": parts["event_barrier_reasons"]}, index=out.index)
        return ScopeRelations(part_table, empty_table(MEMBERSHIP_COLUMNS), empty_table(OVERLAP_COLUMNS),
                              empty_table(SEGMENT_AUDIT_COLUMNS), assignments)
    normalized = adapt_legacy_segments(segments)
    req = {"subject_id", "session_id", "segment_id", "segment_name", "start_s", "end_s"}
    if req - set(normalized.columns):
        raise ScopeError("Segments missing: " + ", ".join(sorted(req - set(normalized.columns))))
    for column in ("interaction_id", "condition", "trial", "stimulus_id", "timebase_id", "scope_definition_ref"):
        if column not in normalized:
            normalized[column] = pd.NA
    parts, part_table = _partitions(out, normalized, gap_threshold_for, stream_keys)
    out["processing_partition_id"] = parts["processing_partition_id"].astype("string")
    out["physical_partition_id"] = parts["physical_partition_id"].astype("string")
    memberships: list[dict[str, Any]] = []
    row_to_memberships: dict[Any, list[tuple[str, str]]] = {index: [] for index in out.index}
    segment_supports: dict[str, tuple[dict[str, Any], list[Any], set[str]]] = {}
    overlap_groups_by_instance: dict[str, set[str]] = {}
    audit_records: list[dict[str, Any]] = []
    scope_blocked = False
    for segment in normalized.to_dict("records"):
        start, end = float(segment["start_s"]), float(segment["end_s"])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ScopeError("Segment boundaries must be finite nonempty [start,end) intervals")
        try:
            mask = _same_scope_mask(out, segment)
            scope_rows = out.loc[mask]
            if str(segment.get("timebase_id") or "") in {"unconfirmed", "legacy_unconfirmed", "<NA>"}:
                if "timebase_id" in scope_rows:
                    known_timebases = scope_rows["timebase_id"].dropna().astype(str).unique()
                    if len(known_timebases) > 1:
                        raise ScopeError("Segment scope matches multiple timebases; select one explicitly")
                if "source_id" in scope_rows and scope_rows["source_id"].dropna().astype(str).nunique() > 1:
                    raise ScopeError("Segment scope matches multiple source clocks; select a timebase explicitly")
        except ScopeError:
            scope_blocked = True
            audit_records.append({
                "segment_id": segment.get("segment_id"), "segment_instance_id": segment.get("segment_instance_id"),
                "segment_name": segment.get("segment_name"), "scope_kind": segment.get("scope_kind"),
                "status": "blocked", "reason_code": "ambiguous_segment_scope",
                "source_sha256": segment.get("source_sha256"), "source_file_name": segment.get("source_file_name"),
                "source_sheet": segment.get("source_sheet"), "source_row": segment.get("source_row"),
                "segment_id_recipe": segment.get("segment_id_recipe"),
            })
            continue
        times = pd.to_numeric(out["time_s"], errors="coerce")
        mask &= times.ge(start).fillna(False) & times.lt(end).fillna(False)
        selected = out.loc[mask]
        obs_ids = selected["observation_id"].astype(str).tolist()
        instance_id = _identifier(segment.get("segment_instance_id"), "segment_instance_id")
        segment_id = _identifier(segment.get("segment_id"), "segment_id")
        record = {
            "segment_id": segment_id,
            "segment_instance_id": instance_id,
            "segment_name": _identifier(segment.get("segment_name"), "segment_name"),
            "scope_kind": str(segment.get("scope_kind") or "participant_session"),
            "subject_id": _identifier(segment.get("subject_id"), "subject_id"),
            "session_id": _identifier(segment.get("session_id"), "session_id"),
            "interaction_id": None if _missing(segment.get("interaction_id")) else str(segment.get("interaction_id")),
            "analysis_stream_id": None,
            "physical_partition_ids": (),
            "source_ids": tuple(sorted(selected["source_id"].dropna().astype(str).unique())) if "source_id" in selected else (),
            "start_s": start,
            "end_s": end,
            "trial": None if _missing(segment.get("trial")) else segment.get("trial"),
            "condition": None if _missing(segment.get("condition")) else segment.get("condition"),
            "stimulus_id": None if _missing(segment.get("stimulus_id")) else segment.get("stimulus_id"),
            "scope_definition_ref": segment.get("scope_definition_ref"),
            "shared_observation_group_id": None,
            "support_observation_ids": tuple(obs_ids),
            "n_unique_observations": len(set(obs_ids)),
            "effective_duration_s": 0.0,
            "timebase_id": None if _missing(segment.get("timebase_id")) else str(segment.get("timebase_id")),
        }
        if str(record["timebase_id"] or "") in {"unconfirmed", "legacy_unconfirmed"} and "timebase_id" in scope_rows:
            observed_timebases = scope_rows["timebase_id"].dropna().astype(str).unique()
            if len(observed_timebases) == 1:
                record["timebase_id"] = str(observed_timebases[0])
        support_scope = out.loc[_same_scope_mask(out, segment)]
        duration = 0.0
        for _, partition_rows in support_scope.groupby("physical_partition_id", dropna=False, sort=False):
            partition_times = pd.to_numeric(partition_rows["time_s"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
            max_gap = gap_threshold_for(partition_times)
            duration += _supported_duration(partition_times, start, end, max_gap)
        record["effective_duration_s"] = float(duration)
        memberships.append(record)
        physical_ids: set[str] = set()
        for partition_id, partition_rows in scope_rows.groupby("physical_partition_id", dropna=False, sort=False):
            partition_times = pd.to_numeric(partition_rows["time_s"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
            if _supported_duration(partition_times, start, end, gap_threshold_for(partition_times)) > 0:
                physical_ids.add(str(partition_id))
        segment_supports[instance_id] = (segment, list(selected.index), physical_ids)
        for index in selected.index:
            row_to_memberships[index].append((segment_id, instance_id))
        overlap_groups_by_instance.setdefault(instance_id, set())
        audit_records.append({
            "segment_id": segment_id, "segment_instance_id": instance_id, "segment_name": record["segment_name"],
            "scope_kind": record["scope_kind"], "status": "matched" if obs_ids else "unmatched",
            "reason_code": "" if obs_ids else "scope_has_no_matching_observations",
            "source_sha256": segment.get("source_sha256"), "source_file_name": segment.get("source_file_name"),
            "source_sheet": segment.get("source_sheet"), "source_row": segment.get("source_row"),
            "segment_id_recipe": segment.get("segment_id_recipe"),
        })
    overlap_records: list[dict[str, Any]] = []
    for left_pos, left in enumerate(memberships):
        for right in memberships[left_pos + 1:]:
            if (left["subject_id"], left["session_id"], left["interaction_id"], left["timebase_id"]) != (
                right["subject_id"], right["session_id"], right["interaction_id"], right["timebase_id"]
            ):
                continue
            lo, hi = max(left["start_s"], right["start_s"]), min(left["end_s"], right["end_s"])
            if hi <= lo:
                continue
            ids = sorted(set(left["support_observation_ids"]) & set(right["support_observation_ids"]))
            _, _, left_partitions = segment_supports[left["segment_instance_id"]]
            _, _, right_partitions = segment_supports[right["segment_instance_id"]]
            shared_partition_ids = left_partitions & right_partitions
            shared_duration = 0.0
            for partition_id in shared_partition_ids:
                rows = out.loc[out["physical_partition_id"].astype("string").eq(partition_id)]
                shared_times = pd.to_numeric(rows["time_s"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
                shared_duration += _supported_duration(
                    shared_times, lo, hi, gap_threshold_for(shared_times)
                )
            group_id = "overlap:" + _digest({"a": left["segment_instance_id"], "b": right["segment_instance_id"], "start_s": lo, "end_s": hi})
            overlap_groups_by_instance[left["segment_instance_id"]].add(group_id)
            overlap_groups_by_instance[right["segment_instance_id"]].add(group_id)
            overlap_records.append({
                "overlap_group_id": group_id,
                "segment_id_a": left["segment_id"], "segment_instance_id_a": left["segment_instance_id"],
                "segment_name_a": left["segment_name"], "segment_id_b": right["segment_id"],
                "segment_instance_id_b": right["segment_instance_id"], "segment_name_b": right["segment_name"],
                "subject_id": left["subject_id"], "session_id": left["session_id"],
                "interaction_id": left["interaction_id"], "timebase_id": left["timebase_id"],
                "intersection_start_s": lo,
                "intersection_end_s": hi, "intersection_duration_s": hi - lo,
                "shared_observation_count": len(ids), "shared_supported_duration_s": float(shared_duration),
                "shared_observation_ids": tuple(ids),
                "status": "retained" if overlap_policy == "keep_labels" else "blocked",
                "reason_code": "shared_label_support" if overlap_policy == "keep_labels" else (
                    "overlap_rejected" if overlap_policy == "reject" else
                    "corrected_table_still_overlaps" if overlap_policy == "corrected" else
                    "segment_overlap_requires_resolution"
                ),
            })
    for item in memberships:
        refs = sorted(overlap_groups_by_instance[item["segment_instance_id"]])
        item["shared_observation_group_id"] = tuple(refs) if refs else None
    overlap_table = pd.DataFrame.from_records(overlap_records, columns=OVERLAP_COLUMNS)
    overlap_blocked = bool(len(overlap_table) and overlap_policy in {"require_resolution", "reject", "corrected"})
    blocked = scope_blocked or overlap_blocked
    reason = "ambiguous_segment_scope" if scope_blocked else (
        "segment_overlap_requires_explicit_keep_labels" if overlap_blocked else None
    )
    if blocked:
        audit_records.extend({
            "segment_id": row["segment_id_a"], "segment_instance_id": row["segment_instance_id_a"],
            "segment_name": row["segment_name_a"], "scope_kind": "overlap", "status": "blocked",
            "reason_code": reason, "source_sha256": None, "source_file_name": None,
            "source_sheet": None, "source_row": None, "segment_id_recipe": None,
        } for row in overlap_records)
    for item in memberships:
        item["physical_partition_ids"] = tuple(sorted(segment_supports[item["segment_instance_id"]][2]))
    assigned = []
    for index, ids in row_to_memberships.items():
        assigned.append(ids[0][0] if len(ids) == 1 else pd.NA)
    assignments = pd.DataFrame({
        "segment_id": assigned,
        "segment_instance_id": [ids[0][1] if len(ids) == 1 else pd.NA for ids in row_to_memberships.values()],
        "analysis_membership_ids": [tuple(ids) for ids in row_to_memberships.values()],
        "processing_partition_id": parts["processing_partition_id"].to_numpy(),
        "physical_partition_id": parts["physical_partition_id"].to_numpy(),
        "processing_barrier_before": parts["processing_barrier_before"].to_numpy(),
        "processing_barrier_reasons": parts["processing_barrier_reasons"].to_numpy(),
        "event_barrier_before": parts["event_barrier_before"].to_numpy(),
        "event_barrier_reasons": parts["event_barrier_reasons"].to_numpy(),
    }, index=out.index)
    return ScopeRelations(
        processing_partitions=part_table,
        analysis_memberships=pd.DataFrame.from_records(memberships, columns=MEMBERSHIP_COLUMNS),
        segment_overlap=overlap_table,
        segment_scope_audit=pd.DataFrame.from_records(audit_records, columns=SEGMENT_AUDIT_COLUMNS),
        assignments=assignments,
        blocked=blocked,
        reason=reason,
    )


def _partitions(
    frame: pd.DataFrame,
    segments: pd.DataFrame,
    gap_threshold_for: Any,
    stream_keys: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = pd.DataFrame(index=frame.index)
    out["processing_partition_id"] = pd.Series([pd.NA] * len(frame), index=frame.index, dtype="string")
    out["physical_partition_id"] = pd.Series([pd.NA] * len(frame), index=frame.index, dtype="string")
    out["processing_barrier_before"] = False
    out["processing_barrier_reasons"] = [tuple() for _ in range(len(frame))]
    out["event_barrier_before"] = False
    out["event_barrier_reasons"] = [tuple() for _ in range(len(frame))]
    # Positional buffers avoid a pandas assignment for every short partition.
    # Preserve label-based behavior for callers with a nonunique frame index.
    buffers = {column: out[column].to_numpy(copy=True) for column in out} if frame.index.is_unique else None
    physical_keys = list(dict.fromkeys([*stream_keys, "source_id", "timebase_id", "trial"]))
    physical_keys = [key for key in physical_keys if key in frame]
    groups = frame.groupby(physical_keys, dropna=False, sort=False).indices if physical_keys else {(): np.arange(len(frame))}
    partition_records: list[dict[str, Any]] = []
    times_all = pd.to_numeric(frame["time_s"], errors="coerce")
    emission_frame = frame.loc[:, ["observation_id", "time_s"]]
    usable_all = _measurement_usable(frame)
    for group_key, raw_positions in groups.items():
        positions = np.asarray(raw_positions, dtype=np.int64)
        group = frame.iloc[positions]
        times = times_all.iloc[positions].to_numpy(dtype=float, na_value=np.nan)
        finite_times = times[np.isfinite(times)]
        max_gap = gap_threshold_for(finite_times)
        group_values = group.iloc[0].to_dict() if len(group) else {}
        matched_segments = pd.DataFrame()
        if not segments.empty:
            candidate = segments
            mask = pd.Series(True, index=candidate.index)
            for key in ("subject_id", "session_id", "interaction_id", "timebase_id", "source_id", "trial"):
                if key in frame and key in candidate and key in group_values and not _missing(group_values[key]):
                    if key == "timebase_id":
                        group_timebase = str(group_values[key])
                        if group_timebase in {"unconfirmed", "legacy_unconfirmed"}:
                            continue
                        candidate_timebase = candidate[key].astype("string")
                        unresolved = candidate_timebase.isna() | candidate_timebase.isin({"unconfirmed", "legacy_unconfirmed"})
                        mask &= candidate_timebase.eq(group_timebase).fillna(False) | unresolved
                    else:
                        mask &= candidate[key].astype("string").eq(str(group_values[key])).fillna(False)
            matched_segments = candidate.loc[mask]
        boundaries: list[tuple[float, str, str, bool]] = []
        for segment in matched_segments.to_dict("records"):
            kind = str(segment.get("segment_kind") or "analysis_label")
            label = str(segment.get("segment_instance_id") or segment.get("segment_id") or "segment")
            for boundary in (float(segment["start_s"]), float(segment["end_s"])):
                boundaries.append((boundary, kind, label, kind == "physical_range"))
        physical_start = 0
        processing_start = 0
        physical_reasons: tuple[str, ...] = ("partition_start",)
        processing_reasons: tuple[str, ...] = ("partition_start",)
        physical_refs: tuple[str, ...] = ()
        processing_refs: tuple[str, ...] = ()
        for local in range(len(positions)):
            reasons: list[str] = []
            refs: list[str] = []
            physical_break = False
            processing_break = False
            if local > 0:
                prev, curr = times[local - 1], times[local]
                if not np.isfinite(prev) or not np.isfinite(curr):
                    reasons.append("invalid_time_support")
                    physical_break = True
                else:
                    delta = curr - prev
                    if delta <= 0:
                        reasons.append("nonpositive_or_duplicate_dt")
                        physical_break = True
                    elif max_gap is not None and delta > max_gap:
                        reasons.append("long_gap")
                        physical_break = True
                    previous_usable = bool(usable_all[int(positions[local - 1])])
                    current_usable = bool(usable_all[int(positions[local])])
                    if previous_usable != current_usable:
                        reasons.append("invalid_measurement_support")
                        physical_break = True
                    for boundary, kind, ref, physical in boundaries:
                        if prev < boundary <= curr:
                            reasons.append("physical_range_boundary" if physical else "analysis_label_boundary")
                            refs.append(ref)
                            physical_break = physical_break or physical
                processing_break = physical_break or bool(reasons)
                if physical_break:
                    if local > processing_start:
                        _emit_partition_chunk(emission_frame, out, positions, times, processing_start, local,
                                              group_values, physical_start, physical_reasons,
                                              processing_reasons, physical_refs, processing_refs,
                                              partition_records, buffers)
                    physical_start = local
                    processing_start = local
                    physical_reasons = tuple(dict.fromkeys(reasons))
                    processing_reasons = physical_reasons
                    physical_refs = tuple(sorted(set(refs)))
                    processing_refs = physical_refs
                elif processing_break:
                    if local > processing_start:
                        _emit_partition_chunk(emission_frame, out, positions, times, processing_start, local,
                                              group_values, physical_start, physical_reasons,
                                              processing_reasons, physical_refs, processing_refs,
                                              partition_records, buffers)
                    processing_start = local
                    processing_reasons = tuple(dict.fromkeys(reasons))
                    processing_refs = tuple(sorted(set(refs)))
            if local == len(positions) - 1:
                _emit_partition_chunk(emission_frame, out, positions, times, processing_start, local + 1,
                                      group_values, physical_start, physical_reasons,
                                      processing_reasons, physical_refs, processing_refs,
                                      partition_records, buffers)
    if buffers is not None:
        for column, values in buffers.items():
            out[column] = pd.array(values, dtype="string") if column.endswith("partition_id") else values
    return out, pd.DataFrame.from_records(partition_records, columns=PARTITION_COLUMNS)


def _emit_partition_chunk(
    frame: pd.DataFrame,
    out: pd.DataFrame,
    positions: np.ndarray,
    times: np.ndarray,
    begin: int,
    end: int,
    group_values: Mapping[str, Any],
    physical_start: int,
    physical_reasons: Sequence[str],
    processing_reasons: Sequence[str],
    physical_refs: Sequence[str],
    processing_refs: Sequence[str],
    records: list[dict[str, Any]],
    buffers: dict[str, np.ndarray] | None = None,
) -> None:
    chunk_positions = positions[begin:end]
    if not len(chunk_positions):
        return
    first = frame.iloc[int(chunk_positions[0])]
    last = frame.iloc[int(chunk_positions[-1])]
    first_obs = str(first["observation_id"])
    physical_first = str(frame.iloc[int(positions[physical_start])]["observation_id"])
    last_obs = str(last["observation_id"])
    scope = {key: _json_value(group_values.get(key)) for key in ("source_id", "subject_id", "session_id", "interaction_id", "trial") if key in group_values}
    physical_reasons = tuple(physical_reasons) or (("partition_start",) if physical_start == 0 else ("unspecified_barrier",))
    processing_reasons = tuple(processing_reasons) or (("partition_start",) if begin == 0 else ("unspecified_barrier",))
    physical_id = "physical:" + _digest({"scope": scope, "first_observation_id": physical_first, "barrier": list(physical_reasons)})
    processing_id = "processing:" + _digest({"physical_partition_id": physical_id, "first_observation_id": first_obs,
                                               "barrier_refs": sorted(set(processing_refs)),
                                               "barrier_reasons": list(processing_reasons)})
    idx = frame.index[chunk_positions]
    event_reasons = tuple(reason for reason in physical_reasons if reason != "partition_start") if physical_start == begin else ()
    if buffers is None:
        out.loc[idx, "physical_partition_id"] = physical_id
        out.loc[idx, "processing_partition_id"] = processing_id
        out.loc[idx[0], "processing_barrier_before"] = bool(processing_reasons)
        out.at[idx[0], "processing_barrier_reasons"] = tuple(processing_reasons)
        out.loc[idx[0], "event_barrier_before"] = bool(event_reasons)
        out.at[idx[0], "event_barrier_reasons"] = event_reasons
    else:
        start = int(chunk_positions[0])
        buffers["physical_partition_id"][chunk_positions] = physical_id
        buffers["processing_partition_id"][chunk_positions] = processing_id
        buffers["processing_barrier_before"][start] = bool(processing_reasons)
        buffers["processing_barrier_reasons"][start] = tuple(processing_reasons)
        buffers["event_barrier_before"][start] = bool(event_reasons)
        buffers["event_barrier_reasons"][start] = event_reasons
    records.append({
        "processing_partition_id": processing_id, "physical_partition_id": physical_id,
        "source_id": group_values.get("source_id"), "subject_id": group_values.get("subject_id"),
        "session_id": group_values.get("session_id"), "interaction_id": group_values.get("interaction_id"),
        "trial": group_values.get("trial"), "start_s": None if _missing(first.get("time_s")) else float(first["time_s"]),
        "end_s": None if _missing(last.get("time_s")) else float(last["time_s"]),
        "n_observations": int(len(set(frame.loc[idx, "observation_id"].astype(str)))),
        "start_barrier_reason": tuple(processing_reasons), "start_barrier_refs": tuple(sorted(set(processing_refs))),
        "segment_kind_refs": tuple(sorted(set(physical_refs) | set(processing_refs))),
    })

