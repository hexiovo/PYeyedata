"""Identity-aware data preparation for the interactive signal timeline.

This module only reshapes tables supplied by the host.  It never interpolates,
classifies, or otherwise changes scientific values.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable

import numpy as np
import pandas as pd


_STREAM_SCOPE_COLUMNS = (
    "source_id", "subject_id", "session_id", "interaction_id", "trial",
    "physical_partition_id", "processing_partition_id", "timebase_id", "clock_id",
)
_SAMPLE_ID_COLUMNS = (
    "observation_id", "source_id", "physical_partition_id", "processing_partition_id",
    "timebase_id", "clock_id", "original_index", "source_row", "subject_id",
    "session_id", "interaction_id", "trial",
)
_EVENT_SCOPE_COLUMNS = (
    "source_id", "subject_id", "session_id", "interaction_id", "trial",
    "physical_partition_id", "processing_partition_id", "timebase_id", "clock_id",
)
_OBSERVATION_REFERENCE_COLUMNS = (
    "observation_id", "start_observation_id", "end_observation_id",
    "contributing_observation_ids",
)
_SIGNAL_CANDIDATES = ("gaze_x", "gaze_y", "pupil_left", "pupil_right")


def _is_missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if np.ndim(result) == 0 else False
    except (TypeError, ValueError):
        return False


def _token(value: Any) -> tuple[str, str]:
    """Make a stable, type-aware token without using a row position."""
    if _is_missing(value):
        return ("<missing>", "")
    if isinstance(value, np.generic):
        value = value.item()
    return (type(value).__name__, str(value))


def _finite_times(frame: pd.DataFrame) -> np.ndarray:
    if "time_s" not in frame:
        return np.full(len(frame), np.nan, dtype=float)
    return pd.to_numeric(frame["time_s"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy(deep=True)
    if value is None:
        return pd.DataFrame()
    try:
        return pd.DataFrame(value).copy(deep=True)
    except (TypeError, ValueError):
        return pd.DataFrame()


@dataclass(frozen=True)
class StreamOption:
    key: Any
    label: str
    selectors: tuple[tuple[str, Any], ...] = ()
    stream_id: str | None = None


@dataclass
class PreparedTimeline:
    stream: StreamOption | None
    raw: pd.DataFrame
    processed: pd.DataFrame
    events: pd.DataFrame
    interval_audit: pd.DataFrame
    message: str = ""


class TimelineModel:
    """Select streams and pair raw/processed samples using identity and seconds."""

    def __init__(self) -> None:
        self.raw_signals = pd.DataFrame()
        self.processed_signals = pd.DataFrame()
        self.events = pd.DataFrame()
        self.interval_audit = pd.DataFrame()
        self.context: dict[str, Any] = {}
        self._streams: list[StreamOption] = []

    def set_data(
        self,
        raw_signals: Any,
        processed_signals: Any = None,
        events: Any = None,
        interval_audit: Any = None,
        *,
        context: Any = None,
    ) -> None:
        self.raw_signals = _frame(raw_signals)
        self.processed_signals = _frame(processed_signals)
        self.events = _frame(events)
        self.interval_audit = _frame(interval_audit)
        self.context = dict(context) if isinstance(context, dict) else {}
        self._streams = self._build_streams()

    @property
    def stream_options(self) -> tuple[StreamOption, ...]:
        return tuple(self._streams)

    @property
    def signal_names(self) -> tuple[str, ...]:
        available = set(self.raw_signals.columns) | set(self.processed_signals.columns)
        return tuple(name for name in _SIGNAL_CANDIDATES if name in available)

    def _build_streams(self) -> list[StreamOption]:
        processed = self.processed_signals
        context_streams = _frame(self.context.get("analysis_streams"))
        source = processed if "analysis_stream_id" in processed else context_streams
        if "analysis_stream_id" in source and not source.empty:
            options: list[StreamOption] = []
            columns = ["analysis_stream_id", *(name for name in _STREAM_SCOPE_COLUMNS if name in source.columns)]
            unique = source[columns].dropna(subset=["analysis_stream_id"]).drop_duplicates()
            for values in unique.itertuples(index=False, name=None):
                selectors = tuple(zip(columns, values))
                stream_id = str(values[0])
                detail = " · ".join(f"{name}={value}" for name, value in selectors[1:]
                                    if not _is_missing(value))
                label = f"{stream_id}" + (f"  ({detail})" if detail else "")
                key = tuple((name, _token(value)) for name, value in selectors)
                options.append(StreamOption(key=key, label=label, selectors=selectors, stream_id=stream_id))
            if options:
                return options

        frame = processed if not processed.empty else self.raw_signals
        group_columns = [name for name in _STREAM_SCOPE_COLUMNS if name in frame.columns]
        if not frame.empty and group_columns:
            # Use observed identity combinations. A stable identifier is needed
            # to distinguish streams; row order and sample positions are never keys.
            options = []
            for row in frame[group_columns].drop_duplicates().itertuples(index=False, name=None):
                selectors = tuple(zip(group_columns, row))
                label = " · ".join(f"{name}={value}" for name, value in selectors)
                key = tuple((name, _token(value)) for name, value in selectors)
                options.append(StreamOption(key=key, label=label, selectors=selectors))
            return options
        if frame.empty:
            return []
        return [StreamOption(key="__all__", label="全部可用样本")]

    @staticmethod
    def _select_by_option(frame: pd.DataFrame, option: StreamOption | None) -> pd.DataFrame:
        if frame.empty or option is None:
            return frame.copy(deep=True)
        mask = pd.Series(True, index=frame.index)
        used = False
        for column, value in option.selectors:
            if column not in frame:
                continue
            used = True
            if column == "analysis_stream_id":
                mask &= frame[column].astype("string").eq(str(value)).fillna(False)
            else:
                # Type-preserving equality avoids mixing numeric and text IDs.
                expected = _token(value)
                mask &= frame[column].map(lambda cell: _token(cell) == expected)
        return frame.loc[mask].copy(deep=True) if used else frame.copy(deep=True)

    @staticmethod
    def _reference_tokens(value: Any) -> set[tuple[str, str]]:
        if _is_missing(value):
            return set()
        if isinstance(value, (tuple, list, set, np.ndarray, pd.Series)):
            return {_token(item) for item in value if not _is_missing(item)}
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if isinstance(decoded, (list, tuple)):
                return {_token(item) for item in decoded if not _is_missing(item)}
        return {_token(value)}

    @classmethod
    def _select_related(
        cls,
        frame: pd.DataFrame,
        option: StreamOption | None,
        sample_ids: set[tuple[str, str]],
        *,
        label: str,
    ) -> tuple[pd.DataFrame, str]:
        if frame.empty or option is None:
            return frame.copy(deep=True), ""
        selector_map = dict(option.selectors)
        available = [(name, value) for name, value in option.selectors if name in frame]
        scope_selectors = [(name, value) for name, value in option.selectors
                           if name in _EVENT_SCOPE_COLUMNS and not _is_missing(value)]
        missing_scope = [name for name, _ in scope_selectors if name not in frame]
        matched_scope = [(name, value) for name, value in scope_selectors if name in frame]
        has_stream_id = option.stream_id is not None and "analysis_stream_id" in frame
        has_scope_identity = any(name in {"source_id", "physical_partition_id", "processing_partition_id",
                                           "timebase_id", "clock_id"} for name, _ in matched_scope)
        can_filter_scope = bool(available) and not missing_scope and has_scope_identity
        scope_mask = pd.Series(True, index=frame.index)
        if can_filter_scope:
            for column, value in available:
                if column == "analysis_stream_id":
                    scope_mask &= frame[column].astype("string").eq(str(value)).fillna(False)
                else:
                    expected = _token(value)
                    scope_mask &= frame[column].map(lambda cell: _token(cell) == expected)

        reference_columns = [name for name in _OBSERVATION_REFERENCE_COLUMNS if name in frame]
        if reference_columns and sample_ids:
            refs_present: list[bool] = []
            refs_match: list[bool] = []
            for _, row in frame.iterrows():
                row_refs: set[tuple[str, str]] = set()
                for column in reference_columns:
                    row_refs.update(cls._reference_tokens(row[column]))
                refs_present.append(bool(row_refs))
                refs_match.append(bool(row_refs) and row_refs.issubset(sample_ids))
            present = pd.Series(refs_present, index=frame.index)
            matches = pd.Series(refs_match, index=frame.index)
            if can_filter_scope:
                keep = scope_mask & (~present | matches)
            else:
                keep = present & matches
            selected = frame.loc[keep].copy(deep=True)
            if selected.empty:
                return selected, f"{label}没有可确认属于所选样本身份的记录，已隐藏未映射记录。"
            return selected, ""

        if not can_filter_scope:
            return frame.iloc[0:0].copy(), f"{label}缺少完整来源/时间基/分区身份，无法确认归属；未显示未映射记录。"
        selected = frame.loc[scope_mask].copy(deep=True)
        return selected, "" if not selected.empty else f"所选来源下没有{label}记录。"

    def prepare(self, stream_key: Any = None) -> PreparedTimeline:
        exact = [item for item in self._streams if item.key == stream_key]
        if exact:
            option = exact[0]
            selection_message = ""
        elif stream_key is not None:
            matching_id = [item for item in self._streams if item.stream_id == str(stream_key)]
            if len(matching_id) == 1:
                option = matching_id[0]
                selection_message = ""
            elif len(matching_id) > 1:
                return PreparedTimeline(None, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                                        "analysis_stream_id 在多个来源/分区重复；请传入控件选项里的完整 stream_key。")
            else:
                option = None
                selection_message = "所选 analysis stream 不存在。"
        elif len(self._streams) == 1:
            option = self._streams[0]
            selection_message = ""
        else:
            option = None
            selection_message = "请明确选择一个包含来源和分区身份的数据流。" if self._streams else ""

        if option is None and selection_message:
            # An unknown or ambiguous selection must never fall through to the
            # full tables: doing so could mix unrelated sources on one axis.
            return PreparedTimeline(
                None, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                selection_message + " 未选择数据流，已隐藏所有记录。",
            )

        raw = self.raw_signals.copy(deep=True)
        processed = self.processed_signals.copy(deep=True)
        context_sample_ids: set[tuple[str, str]] = set()
        if option is not None:
            raw_scope = self._select_by_option(raw, option)
            processed_has_stream_id = "analysis_stream_id" in processed
            processed = self._select_by_option(processed, option)

            # Some hosts provide stream membership separately in context while
            # processed samples do not repeat analysis_stream_id.  Use those
            # explicit observation references when available; matching only the
            # broader source/partition would risk overlaying a different stream.
            if option.stream_id is not None and not processed_has_stream_id:
                context_streams = _frame(self.context.get("analysis_streams"))
                context_selected = self._select_by_option(context_streams, option)
                if "observation_id" in context_selected and "observation_id" in processed:
                    context_sample_ids = {
                        _token(value) for value in context_selected["observation_id"]
                        if not _is_missing(value)
                    }
                if not context_sample_ids or "observation_id" not in processed:
                    raw = raw.iloc[0:0].copy()
                    processed = processed.iloc[0:0].copy()
                    selection_message += (
                        " 所选流的处理样本缺少 analysis_stream_id，且上下文没有可用的 observation_id 映射；"
                        "为避免显示错误数据，未绘制该流。"
                    )
                else:
                    processed_ids = processed["observation_id"].map(_token)
                    processed = processed.loc[processed_ids.isin(context_sample_ids)].copy(deep=True)
                    if "observation_id" in raw:
                        raw_ids = raw["observation_id"].map(_token)
                        raw = raw.loc[raw_ids.isin(context_sample_ids)].copy(deep=True)
                    else:
                        raw = raw.iloc[0:0].copy()
                    if processed.empty:
                        selection_message += " 上下文流映射与处理样本没有共同 observation_id。"
            else:
                # Analysis streams are usually exact subsets of source rows. Recover
                # raw overlay samples by their observation identity, not by position.
                if option.stream_id is not None and "observation_id" in processed and "observation_id" in raw:
                    ids = {_token(value) for value in processed["observation_id"] if not _is_missing(value)}
                    raw_ids = raw["observation_id"].map(_token)
                    raw = raw.loc[raw_ids.isin(ids)].copy(deep=True)
                    if raw.empty:
                        raw = raw_scope
                else:
                    raw = raw_scope

        sample_ids = context_sample_ids
        for frame in (raw, processed):
            if "observation_id" in frame:
                sample_ids.update(_token(value) for value in frame["observation_id"]
                                  if not _is_missing(value))
        raw, processed, message = self._pair_frames(raw, processed)
        events, event_message = self._select_related(self.events, option, sample_ids, label="实际事件")
        audit, audit_message = self._select_related(self.interval_audit, option, sample_ids, label="区间审核")
        message = " ".join(value for value in (selection_message, message, event_message, audit_message) if value)
        return PreparedTimeline(option, raw, processed, events, audit, message)

    @staticmethod
    def _pair_frames(raw: pd.DataFrame, processed: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, str]:
        if raw.empty or processed.empty:
            message = "仅显示已有信号；原始表或处理表为空。" if raw.empty != processed.empty else "没有可绘制的原始或处理信号。"
            return raw, processed, message
        if "time_s" not in raw or "time_s" not in processed:
            return raw, processed.iloc[0:0].copy(), "缺少 time_s，无法按秒对齐；仅显示原始曲线。"

        identity_columns = [name for name in _SAMPLE_ID_COLUMNS if name in raw and name in processed]
        common_identity = [name for name in identity_columns if name != "observation_id"]
        key_columns: list[str] = []
        if "observation_id" in identity_columns:
            ids_raw = raw["observation_id"].map(_token)
            ids_processed = processed["observation_id"].map(_token)
            if ids_raw.is_unique and ids_processed.is_unique:
                key_columns = ["observation_id"]
        if not key_columns:
            stable = [name for name in identity_columns if name != "observation_id"]
            if not stable:
                return raw, processed.iloc[0:0].copy(), "未提供稳定样本身份；仅显示原始曲线，不按行序或时间单独叠加。"
            key_columns = stable

        raw_times = _finite_times(raw)
        processed_times = _finite_times(processed)

        def keys(frame: pd.DataFrame, times: np.ndarray) -> list[tuple[Any, ...] | None]:
            out: list[tuple[Any, ...] | None] = []
            for row_number in range(len(frame)):
                time_value = times[row_number]
                if not np.isfinite(time_value):
                    out.append(None)
                    continue
                values = [frame.iloc[row_number][column] for column in key_columns]
                if any(_is_missing(value) for value in values):
                    out.append(None)
                    continue
                out.append((tuple(_token(value) for value in values), float(time_value)))
            return out

        raw_keys = keys(raw, raw_times)
        processed_keys = keys(processed, processed_times)
        raw_lookup: dict[tuple[Any, ...], int] = {}
        duplicate_raw: set[tuple[Any, ...]] = set()
        for idx, key in enumerate(raw_keys):
            if key is None:
                continue
            if key in raw_lookup:
                duplicate_raw.add(key)
            else:
                raw_lookup[key] = idx
        processed_lookup: dict[tuple[Any, ...], int] = {}
        duplicate_processed: set[tuple[Any, ...]] = set()
        for idx, key in enumerate(processed_keys):
            if key is None:
                continue
            if key in processed_lookup:
                duplicate_processed.add(key)
            else:
                processed_lookup[key] = idx

        matched = [key for key in processed_lookup if key in raw_lookup and
                   key not in duplicate_raw and key not in duplicate_processed]
        if not matched:
            return raw.iloc[0:0].copy(), processed.iloc[0:0].copy(), (
                "原始与处理表没有可唯一对应的样本身份和相同秒数；曲线不会猜测对齐。"
            )

        # An ID collision with a changed time or source identity is a failed
        # pairing, not permission to silently use another row.
        if "observation_id" in key_columns and common_identity:
            pairs = [(raw_lookup[key], processed_lookup[key]) for key in matched]
            for left, right in pairs:
                for column in common_identity:
                    a, b = raw.iloc[left][column], processed.iloc[right][column]
                    if not _is_missing(a) and not _is_missing(b) and _token(a) != _token(b):
                        return raw.iloc[0:0].copy(), processed.iloc[0:0].copy(), (
                            f"样本身份 {column} 在原始与处理表中冲突；曲线不会叠加。"
                        )

        raw_indices = [raw_lookup[key] for key in matched]
        processed_indices = [processed_lookup[key] for key in matched]
        paired_raw = raw.iloc[raw_indices].copy().reset_index(drop=True)
        paired_processed = processed.iloc[processed_indices].copy().reset_index(drop=True)
        order = np.argsort(_finite_times(paired_raw), kind="mergesort")
        paired_raw = paired_raw.iloc[order].reset_index(drop=True)
        paired_processed = paired_processed.iloc[order].reset_index(drop=True)
        note = f"按 {', '.join(key_columns)} 与 time_s 一对一匹配 {len(matched)} 个样本。"
        if duplicate_raw or duplicate_processed:
            note += "重复身份样本已排除。"
        return paired_raw, paired_processed, note


def status_values(frame: pd.DataFrame, candidates: Iterable[str]) -> tuple[np.ndarray, str | None]:
    """Return a tri-state boolean vector, preserving unknown validity as NaN."""
    column = next((name for name in candidates if name in frame), None)
    if column is None:
        return np.full(len(frame), np.nan, dtype=float), None
    # Source validity columns are intentionally preserved as their original
    # device codes (for example "ok" / "lost").  They must never be coerced
    # with bool(value), because every non-empty string would become True.  The
    # normalized boolean column remains authoritative for custom source codes;
    # this conservative decoder only understands the documented common tokens.
    true_tokens = {"true", "1", "yes", "valid"}
    false_tokens = {"false", "0", "no", "invalid"}
    values = np.full(len(frame), np.nan, dtype=float)
    for index, value in enumerate(frame[column].tolist()):
        if _is_missing(value):
            continue
        # ``source_*`` fields preserve the source device's coding.  Without
        # the import profile's explicit decode result, even 0/1 and the words
        # "true"/"false" are ambiguous device codes (and some devices reverse
        # their polarity).  Only a real boolean is safe to display as a known
        # validity state in this fallback path.
        if column.startswith("source_"):
            if isinstance(value, (bool, np.bool_)):
                values[index] = 1.0 if bool(value) else 0.0
            continue
        if isinstance(value, (bool, np.bool_)):
            values[index] = 1.0 if bool(value) else 0.0
            continue
        if isinstance(value, (int, float, np.integer, np.floating)):
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if np.isfinite(numeric) and numeric in (0.0, 1.0):
                values[index] = numeric
            continue
        token = str(value).strip().casefold()
        if token in true_tokens:
            values[index] = 1.0
        elif token in false_tokens:
            values[index] = 0.0
    return values, column


def signal_validity_columns(signal: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return source-validity and interpolation columns for a plotted signal."""
    if signal in {"gaze_x", "gaze_y"}:
        axis = f"{signal}_interpolated"
        return (("gaze_valid", "source_gaze_valid"), (axis, "gaze_interpolated"))
    if signal == "pupil_left":
        return (("pupil_left_valid", "source_pupil_left_valid"), ("pupil_left_interpolated",))
    if signal == "pupil_right":
        return (("pupil_right_valid", "source_pupil_right_valid"), ("pupil_right_interpolated",))
    return ((), ())

