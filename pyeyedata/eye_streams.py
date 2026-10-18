"""Typed, provenance-preserving gaze stream selection and point fusion."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Callable, Literal, Mapping

import numpy as np
import pandas as pd


class EyeStreamError(ValueError):
    """An eye-selection request is invalid or cannot be interpreted safely."""


class _EyeStreamCancelled(Exception):
    """Internal unwind when cooperative cancellation interrupts stream construction."""


@dataclass(frozen=True)
class EyeSelectionConfig:
    mode: Literal["separate", "left", "right", "mean_points", "legacy_source"] = "separate"
    single_eye_fallback: bool = False
    representation: Literal["auto", "point", "direction"] = "auto"
    source_validity_policy_ref: str = "explicit_source_flags_only-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"separate", "left", "right", "mean_points", "legacy_source"}:
            raise EyeStreamError("Unsupported eye-selection mode")
        if not isinstance(self.single_eye_fallback, bool):
            raise EyeStreamError("single_eye_fallback must be boolean")
        if self.representation not in {"auto", "point", "direction"}:
            raise EyeStreamError("representation must be auto, point, or direction")
        if not isinstance(self.source_validity_policy_ref, str) or not self.source_validity_policy_ref.strip():
            raise EyeStreamError("source_validity_policy_ref must be nonempty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ANALYSIS_STREAM_COLUMNS = (
    "analysis_stream_id", "source_id", "subject_id", "session_id", "interaction_id",
    "trial", "stimulus_id", "time_s", "time_valid", "original_index", "observation_id", "timebase_id", "clock_id", "frame_id", "frame_id_source",
    "physical_partition_id", "event_barrier_before", "event_barrier_reasons",
    "selected_eye", "selection_mode", "representation", "coordinate_unit", "coordinate_frame",
    "coordinate_dimension", "source_validity_status", "analysis_gaze_valid", "selection_status",
    "selection_reason", "contributing_eyes", "contributing_observation_ids", "point_method",
    "gaze_x", "gaze_y", "gaze_z", "gaze_direction_x", "gaze_direction_y", "gaze_direction_z",
    "gaze_origin_x", "gaze_origin_y", "gaze_origin_z", "tracking_state", "tracking_valid",
    "eye_selection_parameter_ref", "source_validity_policy_ref",
)


_STREAM_SOURCE_COLUMNS = (
    "source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id", "time_s", "time_valid",
    "original_index", "timebase_id", "clock_id", "frame_id", "physical_partition_id", "event_barrier_before",
    "event_barrier_reasons", "tracking_state", "tracking_valid",
)
_EYE_STREAM_INPUT_COLUMNS = frozenset((
    *_STREAM_SOURCE_COLUMNS, "observation_id", "gaze_x", "gaze_y", "gaze_z", "gaze_valid",
    *(f"gaze_{eye}{kind}_{axis}" for eye in ("left", "right") for kind in ("", "_direction", "_origin") for axis in "xyz"),
    "gaze_left_valid", "gaze_right_valid",
))


def _snapshot_eye_stream_inputs(signals: pd.DataFrame) -> pd.DataFrame:
    """Own every array read by eye selection before preprocessing changes values."""
    if not signals.columns.is_unique:
        return signals.copy(deep=True)
    return pd.DataFrame({
        column: signals[column].copy(deep=True)
        for column in signals if column in _EYE_STREAM_INPUT_COLUMNS
    }, index=signals.index, copy=False)


def derive_eye_streams(
    signals: pd.DataFrame,
    config: EyeSelectionConfig | None = None,
    *,
    coordinate_metadata: Mapping[str, Any] | None = None,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[pd.DataFrame, list[str], bool]:
    """Build long gaze-only streams; pupil arrays remain on the original signal table."""
    settings = config or EyeSelectionConfig()
    raw_metadata: Any = coordinate_metadata or {}
    if hasattr(raw_metadata, "to_dict"):
        raw_metadata = raw_metadata.to_dict()
    metadata = dict(raw_metadata)
    rows: list[dict[str, Any]] | pd.DataFrame = []
    logs: list[str] = []
    try:
        _check_cancel(cancel)
        if settings.mode == "legacy_source":
            # Mixed object/string frames retain scalar types in iterrows. Build
            # their columns directly instead of materializing a dict per row.
            # Keep the original route for numeric-only frames, where iterrows
            # may coerce integer identifiers to floats.
            if signals.columns.is_unique and any(
                dtype == object or isinstance(dtype, pd.StringDtype)
                for dtype in signals.dtypes
            ):
                rows = _legacy_frame(signals, metadata, cancel)
            else:
                rows.extend(_legacy_rows(signals, settings, metadata, cancel))
        elif settings.mode == "mean_points":
            rows.extend(_mean_rows(signals, settings, metadata, cancel))
        else:
            eyes = ("left", "right") if settings.mode == "separate" else (settings.mode,)
            for eye_index, eye in enumerate(eyes):
                _check_cancel(cancel)
                if not _eye_has_representation(signals, eye, settings.representation, metadata):
                    if settings.mode == "separate":
                        other = "right" if eye == "left" else "left"
                        if _eye_has_representation(signals, other, settings.representation, metadata):
                            rows.extend(_unavailable_rows(signals, eye, settings.mode,
                                                          reason=f"missing_{eye}_eye_representation", cancel=cancel))
                        continue
                    if settings.single_eye_fallback:
                        other = "right" if eye == "left" else "left"
                        if _eye_has_representation(signals, other, settings.representation, metadata):
                            rows.extend(_single_eye_rows(signals, other, settings, metadata, selected_mode=eye, fallback=True, cancel=cancel))
                            logs.append(f"explicit_{eye}_fallback_to_{other}")
                            continue
                    rows.extend(_unavailable_rows(signals, eye, settings.mode,
                                                  reason=f"missing_{eye}_eye_representation", cancel=cancel))
                    continue
                rows.extend(_single_eye_rows(signals, eye, settings, metadata, selected_mode=settings.mode, fallback=False, cancel=cancel))
                if progress:
                    progress((eye_index + 1) / max(1, len(eyes)), f"建立 {eye} eye gaze stream")
    except _EyeStreamCancelled:
        logs.append("cancelled during eye stream expansion")
        return _finalize_rows(rows, settings), logs, True
    return _finalize_rows(rows, settings), logs, False


def _finalize_rows(rows: list[dict[str, Any]] | pd.DataFrame, settings: EyeSelectionConfig) -> pd.DataFrame:
    if len(rows):
        result = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame.from_records(rows)
        for column in ANALYSIS_STREAM_COLUMNS:
            if column not in result:
                result[column] = pd.NA
        result["eye_selection_parameter_ref"] = _parameter_ref(settings)
        result["source_validity_policy_ref"] = settings.source_validity_policy_ref
        if tuple(result.columns) != ANALYSIS_STREAM_COLUMNS:
            result = result.loc[:, ANALYSIS_STREAM_COLUMNS]
    else:
        result = pd.DataFrame(columns=ANALYSIS_STREAM_COLUMNS)
    return result


def _check_cancel(cancel: Callable[[], bool] | None, row_number: int | None = None) -> None:
    if cancel and (row_number is None or row_number % 256 == 0) and cancel():
        raise _EyeStreamCancelled


def _legacy_frame(signals: pd.DataFrame, metadata: Mapping[str, Any], cancel: Callable[[], bool] | None) -> pd.DataFrame:
    """Columnar equivalent of legacy records, including their inference rules."""
    count = len(signals)
    if not count:
        return pd.DataFrame()

    def values(column: str, default: Any = pd.NA) -> list[Any]:
        return signals[column].tolist() if column in signals else [default] * count

    result: dict[str, Any] = {key: values(key) for key in (
        "source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id", "time_s", "time_valid",
        "original_index", "timebase_id", "clock_id", "frame_id", "physical_partition_id", "event_barrier_before",
        "event_barrier_reasons", "tracking_state", "tracking_valid",
    )}
    observation_ids: list[str] = []
    coordinates: list[list[float | None]] = [[], [], []]
    dimensions: list[int | None] = []
    validity: list[bool | None] = []
    indexes = values("original_index") if "original_index" in signals else signals.index.tolist()
    raw_coordinates = [values(f"gaze_{axis}", None) for axis in "xyz"]
    for row_number, (value, index, x, y, z, valid) in enumerate(zip(
        values("observation_id", None), indexes, *raw_coordinates, values("gaze_valid", None),
    )):
        # As with _legacy_rows, cancellation discards the entire pending legacy
        # stream, and checks occur before work on rows 0, 256, 512, ... .
        _check_cancel(cancel, row_number)
        observation_ids.append(f"row:{index}" if value is None or value is pd.NA or pd.isna(value) else str(value))
        point = (_number(x), _number(y), _number(z))
        for axis, coordinate in enumerate(point):
            coordinates[axis].append(coordinate)
        dimensions.append(None if point[0] is None or point[1] is None else 2 if point[2] is None else 3)
        validity.append(_tri_bool(valid))
    representation = "direction" if str(metadata.get("coordinate_representation", "unknown")) == "direction" else "point"
    result.update({
        "analysis_stream_id": "gaze:legacy_source", "observation_id": observation_ids,
        "selected_eye": "legacy_source", "selection_mode": "legacy_source", "frame_id_source": None,
        "representation": representation, "coordinate_unit": str(metadata.get("coordinate_unit", "unknown")),
        "coordinate_frame": str(metadata.get("coordinate_frame", "unknown")), "coordinate_dimension": dimensions,
        "source_validity_status": ["invalid" if valid is False else "unknown" if valid is None else
                                   "valid" if dimension in (2, 3) else "missing_values"
                                   for valid, dimension in zip(validity, dimensions)],
        "analysis_gaze_valid": [valid is True and dimension in (2, 3) for valid, dimension in zip(validity, dimensions)],
        "selection_status": "legacy_source_retain", "selection_reason": "legacy_source_representation_preserved",
        "contributing_eyes": [()] * count,
        "contributing_observation_ids": [(value,) for value in observation_ids], "point_method": None,
        "eye_selection_parameter_ref": None, "source_validity_policy_ref": None,
    })
    for axis, coordinate in zip("xyz", coordinates):
        result[f"gaze_{axis}"] = coordinate if representation == "point" else None
        result[f"gaze_direction_{axis}"] = coordinate if representation == "direction" else None
        result[f"gaze_origin_{axis}"] = None
    for column in ANALYSIS_STREAM_COLUMNS:
        if column not in result:
            result[column] = [pd.NA] * count
    return pd.DataFrame(result, columns=ANALYSIS_STREAM_COLUMNS, copy=False)


def _legacy_rows(signals: pd.DataFrame, settings: EyeSelectionConfig, metadata: Mapping[str, Any], cancel: Callable[[], bool] | None) -> list[dict[str, Any]]:
    declared_representation = str(metadata.get("coordinate_representation", "unknown"))
    representation = "direction" if declared_representation == "direction" else "point"
    unit = str(metadata.get("coordinate_unit", "unknown"))
    frame = str(metadata.get("coordinate_frame", "unknown"))
    rows = []
    for row_number, (_, row) in enumerate(signals.iterrows()):
        _check_cancel(cancel, row_number)
        point = tuple(_number(row.get(f"gaze_{axis}")) for axis in "xyz")
        direction = point if representation == "direction" else (None, None, None)
        point_values = point if representation != "direction" else (None, None, None)
        validity = _tri_bool(row.get("gaze_valid"))
        rows.append(_stream_record(row, "gaze:legacy_source", "legacy_source", "legacy_source", representation, unit, frame,
                                   point_values, direction, (None, None, None), validity, "legacy_source_retain",
                                   "legacy_source_representation_preserved", _row_observation_id(row)))
    return rows


def _single_eye_rows(
    signals: pd.DataFrame, eye: str, settings: EyeSelectionConfig, metadata: Mapping[str, Any], *,
    selected_mode: str, fallback: bool, cancel: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    requested_eye = selected_mode if selected_mode in {"left", "right"} else eye
    rows = []
    for row_number, (_, row) in enumerate(signals.iterrows()):
        _check_cancel(cancel, row_number)
        source_eye = eye if fallback or selected_mode not in {"left", "right"} else requested_eye
        representation = _eye_representation(signals, source_eye, settings.representation, metadata)
        xyz, validity = _eye_values(row, source_eye, representation)
        coord_dim = _dimension(xyz)
        usable = validity is True and coord_dim in (2, 3)
        row_fallback = fallback and source_eye != requested_eye
        if (settings.single_eye_fallback and selected_mode in {"left", "right"} and not usable):
            other = "right" if requested_eye == "left" else "left"
            other_representation = _eye_representation(signals, other, settings.representation, metadata)
            other_xyz, other_validity = _eye_values(row, other, other_representation)
            if other_validity is True and _dimension(other_xyz) in (2, 3):
                source_eye, representation, xyz, validity = other, other_representation, other_xyz, other_validity
                coord_dim, usable, row_fallback = _dimension(xyz), True, True
        eye_meta = metadata.get(f"{source_eye}_eye", {})
        if not isinstance(eye_meta, Mapping):
            eye_meta = {}
        unit_key = "direction_unit" if representation == "direction" else "point_unit"
        frame_key = "direction_frame" if representation == "direction" else "point_frame"
        unit, frame = str(eye_meta.get(unit_key, "unknown")), str(eye_meta.get(frame_key, "unknown"))
        point = xyz if representation == "point" else (None, None, None)
        direction = xyz if representation == "direction" else tuple(_number(row.get(f"gaze_{source_eye}_direction_{axis}")) for axis in "xyz")
        origin = tuple(_number(row.get(f"gaze_{source_eye}_origin_{axis}")) for axis in "xyz")
        status = _source_status(validity, xyz)
        reason = "explicit_single_eye_fallback" if row_fallback and usable else ("source_valid" if usable else status)
        selection_status = "unilateral_fallback" if row_fallback and usable else ("unavailable" if not usable else "selected_eye")
        stream_id = f"gaze:{source_eye}" if selected_mode == "separate" else f"gaze:{selected_mode}"
        rows.append(_stream_record(row, stream_id, selected_mode, source_eye, representation, unit, frame,
                                   point, direction, origin, validity, selection_status,
                                   reason, _row_observation_id(row), contributing_eyes=(source_eye,),
                                   coordinate_dimension=coord_dim, analysis_valid=usable))
    return rows


def _eye_values(row: pd.Series, eye: str, representation: str) -> tuple[tuple[float | None, float | None, float | None], bool | None]:
    prefix = f"gaze_{eye}_direction_" if representation == "direction" else f"gaze_{eye}_"
    values = tuple(_number(row.get(prefix + axis)) for axis in "xyz")
    return values, _tri_bool(row.get(f"gaze_{eye}_valid"))


def _mean_rows(signals: pd.DataFrame, settings: EyeSelectionConfig, metadata: Mapping[str, Any], cancel: Callable[[], bool] | None) -> list[dict[str, Any]]:
    left_meta = metadata.get("left_eye", {})
    right_meta = metadata.get("right_eye", {})
    if not isinstance(left_meta, Mapping): left_meta = {}
    if not isinstance(right_meta, Mapping): right_meta = {}
    left_unit, right_unit = str(left_meta.get("point_unit", "unknown")), str(right_meta.get("point_unit", "unknown"))
    left_frame, right_frame = str(left_meta.get("point_frame", "unknown")), str(right_meta.get("point_frame", "unknown"))
    compatible_decl = left_unit != "unknown" and left_unit == right_unit and left_frame != "unknown" and left_frame == right_frame
    has_left = _eye_has_representation(signals, "left", "point", metadata)
    has_right = _eye_has_representation(signals, "right", "point", metadata)
    rows = []
    for row_number, (_, row) in enumerate(signals.iterrows()):
        _check_cancel(cancel, row_number)
        lxyz = tuple(_number(row.get(f"gaze_left_{axis}")) for axis in "xyz")
        rxyz = tuple(_number(row.get(f"gaze_right_{axis}")) for axis in "xyz")
        ldim, rdim = _dimension(lxyz), _dimension(rxyz)
        lvalid = _tri_bool(row.get("gaze_left_valid"))
        rvalid = _tri_bool(row.get("gaze_right_valid"))
        # A row-level frame annotation is the most specific evidence. When it is
        # absent, use only an explicitly declared dataset frame; unknown IDs must
        # remain unavailable rather than comparing equal by omission.
        row_frame_id = _known_frame_id(row.get("frame_id")) if "frame_id" in row.index else None
        metadata_frame_id = _known_frame_id(metadata.get("frame_id"))
        fusion_frame_id = row_frame_id if row_frame_id is not None else metadata_frame_id
        frame_id_source = "row" if row_frame_id is not None else ("metadata" if metadata_frame_id is not None else "unavailable")
        frame_match = _known_equal(fusion_frame_id, fusion_frame_id)
        compatible = compatible_decl and has_left and has_right and ldim in (2, 3) and ldim == rdim and frame_match
        contributors: tuple[str, ...] = ()
        ids: tuple[str, ...] = ()
        coords: tuple[float | None, float | None, float | None] = (None, None, None)
        status = "unknown"
        reason = "compatible_points_required"
        selection_status = "unavailable"
        usable = False
        left_usable = lvalid is True and ldim in (2, 3) and left_unit != "unknown" and left_frame != "unknown"
        right_usable = rvalid is True and rdim in (2, 3) and right_unit != "unknown" and right_frame != "unknown"
        left_missing = lvalid is False or not has_left or (lvalid is True and ldim not in (2, 3))
        right_missing = rvalid is False or not has_right or (rvalid is True and rdim not in (2, 3))
        if compatible and left_usable and right_usable:
            coords = tuple((float(lxyz[i]) + float(rxyz[i])) / 2.0 for i in range(ldim)) + ((None,) if ldim == 2 else ())
            contributors = ("left", "right")
            ids = (_row_observation_id(row),)
            status = "valid"
            reason = "mean_same_frame_points-v1"
            selection_status = "binocular_mean"
            usable = True
        elif settings.single_eye_fallback and left_usable and right_missing:
            eye = "left"
            vals = lxyz if eye == "left" else rxyz
            coords = vals if ldim == 3 else (vals[0], vals[1], None)
            contributors = (eye,)
            ids = (_row_observation_id(row),)
            status = "valid"
            reason = f"explicit_fallback_to_{eye}"
            selection_status = "unilateral_fallback"
            usable = _dimension(coords) in (2, 3)
        elif settings.single_eye_fallback and right_usable and left_missing:
            eye = "right"
            vals = rxyz
            coords = vals if rdim == 3 else (vals[0], vals[1], None)
            contributors = (eye,)
            ids = (_row_observation_id(row),)
            status = "valid"
            reason = f"explicit_fallback_to_{eye}"
            selection_status = "unilateral_fallback"
            usable = _dimension(coords) in (2, 3)
        elif lvalid is None or rvalid is None:
            status = "unknown"
            reason = "source_validity_unknown"
        elif lvalid is False and rvalid is False:
            status = "invalid"
            reason = "both_eye_sources_invalid"
        elif lvalid is False:
            status = "invalid"
            reason = "left_eye_source_invalid"
        elif rvalid is False:
            status = "invalid"
            reason = "right_eye_source_invalid"
        elif not has_left or not has_right:
            reason = "point_representation_missing"
        elif not frame_match:
            reason = "frame_id_mismatch"
        elif left_frame == "unknown" or right_frame == "unknown":
            reason = "coordinate_frame_unverified"
        elif left_unit == "unknown" or right_unit == "unknown":
            reason = "coordinate_unit_unverified"
        elif ldim != rdim or ldim not in (2, 3) or rdim not in (2, 3):
            reason = "point_dimension_mismatch"
        else:
            reason = "point_frame_or_unit_mismatch"
        output_eye = contributors[0] if len(contributors) == 1 else None
        output_unit = (left_unit if output_eye == "left" else right_unit) if output_eye else (left_unit if left_unit == right_unit else "unknown")
        output_frame = (left_frame if output_eye == "left" else right_frame) if output_eye else (left_frame if left_frame == right_frame else "unknown")
        rows.append(_stream_record(row, "gaze:mean_points", "mean_points", "mean_points", "point",
                                   output_unit if output_unit is not None else "unknown", output_frame if output_frame is not None else "unknown",
                                   coords, (None, None, None), (None, None, None),
                                   True if usable else (False if status == "invalid" else None), selection_status, reason,
                                   _row_observation_id(row), contributing_eyes=contributors, contributing_ids=ids,
                                   coordinate_dimension=_dimension(coords), analysis_valid=usable,
                                   point_method="mean_same_frame_points-v1" if len(contributors) == 2 else None,
                                   frame_id_source=frame_id_source))
    return rows


def _unavailable_rows(signals: pd.DataFrame, stream_id: str, mode: str, *, reason: str,
                      cancel: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    rows = []
    for row_number, (_, row) in enumerate(signals.iterrows()):
        _check_cancel(cancel, row_number)
        rows.append(_stream_record(row, f"gaze:{stream_id}", mode, stream_id, "unknown", "unknown", "unknown",
                                   (None, None, None), (None, None, None), (None, None, None), None,
                                   "unavailable", reason, _row_observation_id(row), analysis_valid=False))
    return rows


def _stream_record(
    row: pd.Series, stream_id: str, mode: str, eye: str, representation: str, unit: str, frame: str,
    point: tuple[Any, Any, Any], direction: tuple[Any, Any, Any], origin: tuple[Any, Any, Any], validity: bool | None,
    selection_status: str, reason: str, observation_id: str, *, contributing_eyes: tuple[str, ...] | None = None,
    contributing_ids: tuple[str, ...] | None = None, coordinate_dimension: int | None = None,
    analysis_valid: bool | None = None, point_method: str | None = None,
    frame_id_source: str | None = None,
) -> dict[str, Any]:
    result = {key: row.get(key, pd.NA) for key in (
        "source_id", "subject_id", "session_id", "interaction_id", "trial", "stimulus_id", "time_s", "time_valid",
        "original_index", "timebase_id", "clock_id", "frame_id", "physical_partition_id", "event_barrier_before", "event_barrier_reasons", "tracking_state", "tracking_valid",
    )}
    result.update({
        "analysis_stream_id": stream_id, "observation_id": observation_id, "selected_eye": eye, "selection_mode": mode,
        "frame_id_source": frame_id_source,
        "representation": representation, "coordinate_unit": unit, "coordinate_frame": frame,
        "coordinate_dimension": coordinate_dimension or _dimension(point if representation == "point" else direction),
        "source_validity_status": _source_status(validity, point if representation == "point" else direction),
        "analysis_gaze_valid": bool(analysis_valid) if analysis_valid is not None else bool(validity is True and _dimension(point if representation == "point" else direction) in (2, 3)),
        "selection_status": selection_status, "selection_reason": reason,
        "contributing_eyes": contributing_eyes or ((eye,) if eye in {"left", "right"} else ()),
        "contributing_observation_ids": contributing_ids or (observation_id,), "point_method": point_method,
        "gaze_x": point[0], "gaze_y": point[1], "gaze_z": point[2],
        "gaze_direction_x": direction[0], "gaze_direction_y": direction[1], "gaze_direction_z": direction[2],
        "gaze_origin_x": origin[0], "gaze_origin_y": origin[1], "gaze_origin_z": origin[2],
        "eye_selection_parameter_ref": None, "source_validity_policy_ref": None,
    })
    return result


def _eye_has_representation(signals: pd.DataFrame, eye: str, requested: str, metadata: Mapping[str, Any]) -> bool:
    prefix = "gaze_" + eye + ("_direction_" if _eye_representation(signals, eye, requested, metadata) == "direction" else "_")
    return any(prefix + axis in signals and signals[prefix + axis].notna().any() for axis in "xy")


def _eye_representation(signals: pd.DataFrame, eye: str, requested: str, metadata: Mapping[str, Any]) -> str:
    if requested in {"point", "direction"}:
        return requested
    eye_meta = metadata.get(f"{eye}_eye", {})
    if isinstance(eye_meta, Mapping) and str(eye_meta.get("direction_frame", "unknown")) != "unknown":
        dcols = [f"gaze_{eye}_direction_{axis}" for axis in "xyz"]
        if any(column in signals and signals[column].notna().any() for column in dcols[:2]):
            return "direction"
    global_repr = metadata.get("coordinate_representation")
    if global_repr == "direction" and any(f"gaze_{eye}_direction_{axis}" in signals and signals[f"gaze_{eye}_direction_{axis}"].notna().any() for axis in "xy"):
        return "direction"
    return "point"


def _tri_bool(value: Any) -> bool | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    return None


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math_isfinite(out) else None


def _dimension(values: tuple[Any, Any, Any]) -> int | None:
    if _number(values[0]) is None or _number(values[1]) is None:
        return None
    z = _number(values[2])
    return 3 if z is not None else 2


def _source_status(validity: bool | None, coords: tuple[Any, Any, Any]) -> str:
    if validity is False: return "invalid"
    if validity is None: return "unknown"
    return "valid" if _dimension(coords) in (2, 3) else "missing_values"


def _row_observation_id(row: pd.Series) -> str:
    value = row.get("observation_id")
    if value is None or value is pd.NA or pd.isna(value):
        return f"row:{row.get('original_index', row.name)}"
    return str(value)


def _known_equal(a: Any, b: Any) -> bool:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return False
    return str(a) == str(b)


def _known_frame_id(value: Any) -> Any | None:
    """Return an explicit scalar frame ID, treating null/blank/unknown as absent."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(missing, (bool, np.bool_)) or bool(missing):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.casefold() in {"unknown", "<na>"}:
            return None
    return value


def math_isfinite(value: float) -> bool:
    return bool(np.isfinite(value))


def _parameter_ref(config: EyeSelectionConfig) -> str:
    raw = json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "eye-selection-parameter:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
