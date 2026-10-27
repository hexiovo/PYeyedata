"""Window queries over full, provenance-preserving event records.

Counts and duration summaries use different interval relations. These helpers
never rewrite a source event's original boundaries or identity.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd


EVENT_QUERY_REQUIRED = frozenset({"event_id", "event_type", "start_s", "end_s"})
_SCOPE_KEYS = (
    "source_id", "subject_id", "session_id", "interaction_id", "trial",
    "stimulus_id", "analysis_stream_id", "physical_partition_id",
    "timebase_id", "clock_id",
)


def _numeric_interval(events: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    starts = pd.to_numeric(events["start_s"], errors="coerce")
    ends = pd.to_numeric(events["end_s"], errors="coerce")
    valid = np.isfinite(starts.to_numpy(dtype=float, na_value=np.nan))
    valid &= np.isfinite(ends.to_numpy(dtype=float, na_value=np.nan))
    valid &= ends.to_numpy(dtype=float, na_value=np.nan) > starts.to_numpy(dtype=float, na_value=np.nan)
    return starts, ends, pd.Series(valid, index=events.index)


def _scope_filter(events: pd.DataFrame, identities: Mapping[str, Any] | None,
                  *, allow_unspecified_stream: bool = False) -> pd.DataFrame:
    out = events.copy()
    if not identities:
        return out
    for key in _SCOPE_KEYS:
        value = identities.get(key)
        if value is None or pd.isna(value):
            continue
        if key not in out:
            if key == "analysis_stream_id" and allow_unspecified_stream:
                continue
            return out.iloc[0:0].copy()
        out = out.loc[out[key].astype("string").eq(str(value)).fillna(False)]
    return out


def onset_events(
    events: pd.DataFrame,
    start_s: float,
    end_s: float,
    *,
    identities: Mapping[str, Any] | None = None,
    event_types: Sequence[str] | None = None,
    status_values: Sequence[str] | None = None,
    allow_unspecified_stream: bool = False,
) -> pd.DataFrame:
    """Select full events whose onset is in the half-open window [start,end)."""
    if not isinstance(events, pd.DataFrame) or events.empty or not EVENT_QUERY_REQUIRED <= set(events):
        return pd.DataFrame(columns=events.columns if isinstance(events, pd.DataFrame) else None)
    start, end = float(start_s), float(end_s)
    if not np.isfinite(start) or not np.isfinite(end) or end <= start:
        raise ValueError("event query window must be finite and increasing")
    out = _scope_filter(events, identities, allow_unspecified_stream=allow_unspecified_stream)
    starts, ends, valid = _numeric_interval(out)
    keep = valid & starts.ge(start) & starts.lt(end)
    if event_types is not None:
        selected = {str(value).casefold() for value in event_types}
        keep &= out["event_type"].astype("string").str.casefold().isin(selected)
    if status_values is not None and "status" in out:
        selected_status = {str(value).casefold() for value in status_values}
        keep &= out["status"].astype("string").str.casefold().isin(selected_status)
    return out.loc[keep].copy().reset_index(drop=True)


def overlapping_events(
    events: pd.DataFrame,
    start_s: float,
    end_s: float,
    *,
    identities: Mapping[str, Any] | None = None,
    event_types: Sequence[str] | None = None,
    status_values: Sequence[str] | None = None,
    allow_unspecified_stream: bool = False,
) -> pd.DataFrame:
    """Select full events with positive overlap and add separate clipped bounds.

    ``start_s``/``end_s`` and ``event_id`` remain the source values. The query
    contribution is carried by ``intersection_*`` columns.
    """
    if not isinstance(events, pd.DataFrame) or events.empty or not EVENT_QUERY_REQUIRED <= set(events):
        empty = pd.DataFrame(columns=events.columns if isinstance(events, pd.DataFrame) else None)
        for column in ("intersection_start_s", "intersection_end_s", "intersection_duration_s"):
            empty[column] = pd.Series(dtype=float)
        return empty
    start, end = float(start_s), float(end_s)
    if not np.isfinite(start) or not np.isfinite(end) or end <= start:
        raise ValueError("event query window must be finite and increasing")
    out = _scope_filter(events, identities, allow_unspecified_stream=allow_unspecified_stream)
    starts, ends, valid = _numeric_interval(out)
    keep = valid & ends.gt(start) & starts.lt(end)
    if event_types is not None:
        selected = {str(value).casefold() for value in event_types}
        keep &= out["event_type"].astype("string").str.casefold().isin(selected)
    if status_values is not None and "status" in out:
        selected_status = {str(value).casefold() for value in status_values}
        keep &= out["status"].astype("string").str.casefold().isin(selected_status)
    out = out.loc[keep].copy()
    source_start = starts.loc[out.index].to_numpy(dtype=float)
    source_end = ends.loc[out.index].to_numpy(dtype=float)
    out["intersection_start_s"] = np.maximum(source_start, start)
    out["intersection_end_s"] = np.minimum(source_end, end)
    out["intersection_duration_s"] = out["intersection_end_s"] - out["intersection_start_s"]
    return out.reset_index(drop=True)

