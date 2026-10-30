"""Versioned, per-user import mappings kept separate from analysis plans.

Import profiles preserve explicit source-column and measurement semantics. They
do not contain source paths, analysis configuration, or run results.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from pyeyedata.contracts import CoordinateMetadata, EyeCoordinateMetadata
from pyeyedata.io import (
    CsvOptions,
    READING_DIRECTIONS,
    SIGNAL_OPTIONAL_COLUMNS,
    SIGNAL_REQUIRED_COLUMNS,
)


IMPORT_PROFILE_SCHEMA = "pyeyedata.import-profile/v1"
IMPORT_PROFILE_VERSION = 1
MAX_IMPORT_PROFILE_BYTES = 2 * 1024 * 1024
SOURCE_ROLES = frozenset({"signals", "segments", "aois", "performance"})
VALIDITY_FIELDS = frozenset({
    "gaze_valid", "pupil_left_valid", "pupil_right_valid", "gaze_left_valid",
    "gaze_right_valid", "tracking_valid", "target_valid", "reference_valid",
    "eyelid_closed_left", "eyelid_closed_right",
})
SIGNAL_FIELDS = frozenset(SIGNAL_REQUIRED_COLUMNS) | frozenset(SIGNAL_OPTIONAL_COLUMNS) | {"timestamp"}
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ImportProfileError(ValueError):
    """An import profile is malformed, unsupported, or unsafe to apply."""


def _profile_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROFILE_ID.fullmatch(value) or value in {".", ".."}:
        raise ImportProfileError("导入预设 ID 只能包含字母、数字、点、下划线和连字符。")
    return value


def _plain_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ImportProfileError(f"{name} 必须是字符串键的对象。")
    return dict(value)


def _string_mapping(value: Any, name: str, *, allow_blank_values: bool = True) -> dict[str, str]:
    result = _plain_mapping(value, name)
    for key, item in result.items():
        if not key.strip() or not isinstance(item, str):
            raise ImportProfileError(f"{name} 中的字段名和内容必须是非空字符串。")
        if not allow_blank_values and not item.strip():
            raise ImportProfileError(f"{name} 中的内容不能为空。")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImportProfileError(f"预设 JSON 中重复出现字段：{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ImportProfileError(f"预设 JSON 中不允许无效数字：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ImportProfileError("预设 JSON 中含有超出范围的数字。")
    return number


def _coordinate_from_dict(value: Any) -> CoordinateMetadata:
    data = _plain_mapping(value, "coordinate_metadata")
    for eye in ("left_eye", "right_eye"):
        if eye in data:
            data[eye] = EyeCoordinateMetadata(**_plain_mapping(data[eye], eye))
    if "device_validation_evidence" in data:
        evidence = data["device_validation_evidence"]
        if not isinstance(evidence, (list, tuple)):
            raise ImportProfileError("device_validation_evidence 必须为列表。")
        data["device_validation_evidence"] = tuple(evidence)
    try:
        return CoordinateMetadata(**data)
    except (TypeError, ValueError) as exc:
        raise ImportProfileError(f"坐标或设备元数据不符合当前契约：{exc}") from exc


@dataclass(frozen=True, slots=True)
class ImportSourceProfile:
    """Confirmed mapping for one source role, without a source file path."""

    role: str
    column_map: Mapping[str, str] = field(default_factory=dict)
    units: Mapping[str, str] = field(default_factory=dict)
    csv_options: CsvOptions = field(default_factory=CsvOptions)
    sheet_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in SOURCE_ROLES:
            raise ImportProfileError(f"不支持的导入源角色：{self.role!r}")
        mapping = _string_mapping(self.column_map, "column_map", allow_blank_values=False)
        if self.role == "signals" and set(mapping) - SIGNAL_FIELDS:
            unknown = sorted(set(mapping) - SIGNAL_FIELDS)
            raise ImportProfileError("信号映射包含当前导入契约不支持的字段：" + ", ".join(unknown))
        unit_map = _string_mapping(self.units, "units")
        if not isinstance(self.csv_options, CsvOptions):
            raise ImportProfileError("csv_options 必须使用当前导入契约中的 CsvOptions。")
        if self.sheet_name is not None and (
            not isinstance(self.sheet_name, str) or not self.sheet_name.strip()
        ):
            raise ImportProfileError("sheet_name 必须是非空字符串或 null。")
        object.__setattr__(self, "column_map", MappingProxyType(mapping))
        object.__setattr__(self, "units", MappingProxyType(unit_map))

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "column_map": dict(self.column_map),
            "units": dict(self.units),
            "csv_options": {
                "encoding": self.csv_options.encoding,
                "delimiter": self.csv_options.delimiter,
                "decimal": self.csv_options.decimal,
            },
            "sheet_name": self.sheet_name,
        }


@dataclass(frozen=True, slots=True)
class StructureDifference:
    role: str
    expected_columns: tuple[str, ...] | None
    actual_columns: tuple[str, ...] | None
    added_columns: tuple[str, ...] = ()
    removed_columns: tuple[str, ...] = ()
    reordered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "expected_columns": list(self.expected_columns) if self.expected_columns is not None else None,
            "actual_columns": list(self.actual_columns) if self.actual_columns is not None else None,
            "added_columns": list(self.added_columns),
            "removed_columns": list(self.removed_columns),
            "reordered": self.reordered,
        }


@dataclass(frozen=True, slots=True)
class StructureComparison:
    compatible: bool
    differences: tuple[StructureDifference, ...]


def compare_structure_signatures(
    expected: Mapping[str, Sequence[str]], actual: Mapping[str, Sequence[str]],
) -> StructureComparison:
    """Return explicit role/column differences; callers must not apply on mismatch."""
    expected_map = _normalize_signature(expected, "expected")
    actual_map = _normalize_signature(actual, "actual")
    differences: list[StructureDifference] = []
    for role in sorted(set(expected_map) | set(actual_map)):
        old = expected_map.get(role)
        new = actual_map.get(role)
        if old == new:
            continue
        old_counts = Counter(old or ())
        new_counts = Counter(new or ())
        added = tuple(sorted((new_counts - old_counts).elements()))
        removed = tuple(sorted((old_counts - new_counts).elements()))
        differences.append(StructureDifference(
            role=role,
            expected_columns=old,
            actual_columns=new,
            added_columns=added,
            removed_columns=removed,
            reordered=old is not None and new is not None and not added and not removed and old != new,
        ))
    return StructureComparison(compatible=not differences, differences=tuple(differences))


def _normalize_signature(value: Mapping[str, Sequence[str]], name: str) -> dict[str, tuple[str, ...]]:
    raw = _plain_mapping(value, f"{name} structure signature")
    normalized: dict[str, tuple[str, ...]] = {}
    for role, columns in raw.items():
        if not isinstance(role, str) or role not in SOURCE_ROLES:
            raise ImportProfileError(f"{name} structure signature contains an unsupported role: {role!r}")
        if not isinstance(columns, (list, tuple)) or not columns or any(not isinstance(column, str) or not column.strip()
                                                         for column in columns):
            raise ImportProfileError(f"{name} 列结构必须是非空字符串列表。")
        column_tuple = tuple(columns)
        if len(set(column_tuple)) != len(column_tuple):
            raise ImportProfileError(f"{name} 的 {role} 列名重复，不能安全复用映射。")
        normalized[role] = column_tuple
    return normalized


@dataclass(frozen=True, slots=True)
class ImportProfile:
    profile_id: str
    name: str
    revision: int
    sources: tuple[ImportSourceProfile, ...]
    structure_signature: Mapping[str, Sequence[str]]
    coordinate_metadata: CoordinateMetadata = field(default_factory=CoordinateMetadata)
    validity_values: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)
    reading_direction_values: Mapping[str, str] = field(default_factory=dict)
    pupil_units_by_eye: Mapping[str, str] = field(default_factory=dict)
    schema: str = IMPORT_PROFILE_SCHEMA
    version: int = IMPORT_PROFILE_VERSION

    def __post_init__(self) -> None:
        _profile_id(self.profile_id)
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 160:
            raise ImportProfileError("导入预设名称须为 1 至 160 个字符。")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ImportProfileError("导入预设 revision 必须是正整数。")
        if self.schema != IMPORT_PROFILE_SCHEMA:
            raise ImportProfileError(f"不支持的导入预设 schema：{self.schema!r}")
        if type(self.version) is not int or self.version != IMPORT_PROFILE_VERSION:
            raise ImportProfileError(f"不支持的导入预设版本：{self.version!r}")
        sources = tuple(self.sources)
        if not sources or any(not isinstance(source, ImportSourceProfile) for source in sources):
            raise ImportProfileError("导入预设至少需要一个有效源角色映射。")
        source_roles = [source.role for source in sources]
        if len(set(source_roles)) != len(source_roles):
            raise ImportProfileError("导入预设中的源角色不能重复。")
        signature = _normalize_signature(self.structure_signature, "profile")
        if set(signature) != set(source_roles):
            raise ImportProfileError("每个已保存的源角色都必须有对应的列名结构签名。")
        if not isinstance(self.coordinate_metadata, CoordinateMetadata):
            raise ImportProfileError("coordinate_metadata 必须使用当前 CoordinateMetadata 契约。")
        validity = _plain_mapping(self.validity_values, "validity_values")
        frozen_validity: dict[str, Mapping[str, bool]] = {}
        for field_name, codes in validity.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ImportProfileError("有效性字段名不能为空。")
            if field_name not in VALIDITY_FIELDS:
                raise ImportProfileError(f"不支持的有效性字段：{field_name!r}")
            code_map = _plain_mapping(codes, f"validity_values.{field_name}")
            if any(not isinstance(code, str) or not code.strip() or type(valid) is not bool
                   for code, valid in code_map.items()):
                raise ImportProfileError("有效性码必须是非空字符串并明确映射到 true/false。")
            frozen_validity[field_name] = MappingProxyType(code_map)
        reading = _string_mapping(self.reading_direction_values, "reading_direction_values",
                                  allow_blank_values=False)
        if any(direction.casefold() not in READING_DIRECTIONS for direction in reading.values()):
            raise ImportProfileError("阅读方向必须映射到当前支持的标准方向。")
        pupil_units = _string_mapping(self.pupil_units_by_eye, "pupil_units_by_eye",
                                      allow_blank_values=False)
        if set(pupil_units) - {"left", "right"}:
            raise ImportProfileError("瞳孔单位只能按 left/right 眼分别记录。")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "structure_signature", MappingProxyType(signature))
        object.__setattr__(self, "validity_values", MappingProxyType(frozen_validity))
        object.__setattr__(self, "reading_direction_values", MappingProxyType(reading))
        object.__setattr__(self, "pupil_units_by_eye", MappingProxyType(pupil_units))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "profile_id": self.profile_id,
            "name": self.name,
            "revision": self.revision,
            "sources": [source.to_dict() for source in self.sources],
            "coordinate_metadata": _coordinate_to_dict(self.coordinate_metadata),
            "validity_values": {name: dict(codes) for name, codes in self.validity_values.items()},
            "reading_direction_values": dict(self.reading_direction_values),
            "pupil_units_by_eye": dict(self.pupil_units_by_eye),
            "structure_signature": {role: list(columns)
                                    for role, columns in self.structure_signature.items()},
        }

    def compare_structure(self, actual: Mapping[str, Sequence[str]]) -> StructureComparison:
        return compare_structure_signatures(self.structure_signature, actual)

    @classmethod
    def from_dict(cls, value: Any) -> "ImportProfile":
        raw = _plain_mapping(value, "预设")
        expected = {
            "schema", "version", "profile_id", "name", "revision", "sources",
            "coordinate_metadata", "validity_values", "reading_direction_values",
            "pupil_units_by_eye", "structure_signature",
        }
        if set(raw) != expected:
            raise ImportProfileError("导入预设字段不完整或包含当前版本不支持的内容。")
        if raw["schema"] != IMPORT_PROFILE_SCHEMA:
            raise ImportProfileError(f"不支持的导入预设 schema：{raw['schema']!r}")
        if type(raw["version"]) is not int or raw["version"] != IMPORT_PROFILE_VERSION:
            raise ImportProfileError(f"不支持的导入预设版本：{raw['version']!r}")
        raw_sources = raw["sources"]
        if not isinstance(raw_sources, list):
            raise ImportProfileError("sources 必须是列表。")
        sources: list[ImportSourceProfile] = []
        for index, source_value in enumerate(raw_sources):
            source = _plain_mapping(source_value, f"sources[{index}]")
            old_keys = {"role", "column_map", "units", "csv_options"}
            if set(source) not in (old_keys, old_keys | {"sheet_name"}):
                raise ImportProfileError(f"sources[{index}] 字段不完整或包含未知内容。")
            try:
                csv_options = CsvOptions(**_plain_mapping(source["csv_options"], "csv_options"))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ImportProfileError(f"sources[{index}] 的 CSV 选项无效：{exc}") from exc
            sources.append(ImportSourceProfile(
                role=source["role"], column_map=source["column_map"], units=source["units"],
                csv_options=csv_options, sheet_name=source.get("sheet_name"),
            ))
        try:
            coordinate = _coordinate_from_dict(raw["coordinate_metadata"])
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ImportProfileError):
                raise
            raise ImportProfileError(f"无法读取坐标元数据：{exc}") from exc
        return cls(
            profile_id=raw["profile_id"], name=raw["name"], revision=raw["revision"],
            sources=tuple(sources), structure_signature=raw["structure_signature"],
            coordinate_metadata=coordinate, validity_values=raw["validity_values"],
            reading_direction_values=raw["reading_direction_values"],
            pupil_units_by_eye=raw["pupil_units_by_eye"], schema=raw["schema"],
            version=raw["version"],
        )


def _coordinate_to_dict(value: CoordinateMetadata) -> dict[str, Any]:
    """Serialize the frozen public metadata without depending on dataclass internals."""
    from dataclasses import asdict

    return asdict(value)


def create_structure_signature(columns_by_role: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Capture exact role and column order from user-reviewed import previews."""
    return _normalize_signature(columns_by_role, "observed")


def builtin_import_profiles() -> tuple[ImportProfile, ...]:
    """Editable export templates, never declarations of device validation.

    Only tabular sample exports are supported, not EDF or mixed-record ASC.
    EyeLink pupil size stays in device units (area/diameter is export-dependent).
    """
    profiles: list[ImportProfile] = []

    def add(key: str, name: str, mapping: dict[str, str], pupil_unit: str) -> None:
        units = {name: "ms" if name == "time_s" else
                 pupil_unit if name.startswith("pupil_") else "px"
                 for name in mapping if name in {"time_s", "gaze_x", "gaze_y", "pupil_left", "pupil_right"}}
        source = ImportSourceProfile("signals", mapping, units, CsvOptions(delimiter="\t"))
        profiles.append(ImportProfile(
            profile_id=key, name=name, revision=1, sources=(source,),
            structure_signature={"signals": tuple(dict.fromkeys(mapping.values()))},
            coordinate_metadata=CoordinateMetadata(
                time_unit="ms", coordinate_unit="px", coordinate_representation="2d_point",
            ),
            pupil_units_by_eye={eye: pupil_unit for eye in ("left", "right")
                                if f"pupil_{eye}" in mapping},
        ))

    for eye, title in (("LEFT", "左眼"), ("RIGHT", "右眼")):
        add(f"builtin-eyelink-{eye.lower()}", f"EyeLink Data Viewer · {title}采样报告（ms）", {
            "subject_id": "RECORDING_SESSION_LABEL", "trial": "TRIAL_INDEX",
            "time_s": "TIMESTAMP", "gaze_x": f"{eye}_GAZE_X", "gaze_y": f"{eye}_GAZE_Y",
            f"pupil_{eye.lower()}": f"{eye}_PUPIL_SIZE",
        }, "device")
    for bracketed in (False, True):
        suffix = "-units" if bracketed else ""
        add(f"builtin-tobii-prolab{suffix}",
            "Tobii Pro Lab · " + ("带单位表头（ms / DACS px）" if bracketed else "传统表头（ms / DACS px）"), {
                "subject_id": "Participant name", "session_id": "Recording name",
                "time_s": "Recording timestamp [ms]" if bracketed else "Recording timestamp",
                "gaze_x": "Gaze point X [DACS px]" if bracketed else "Gaze point X",
                "gaze_y": "Gaze point Y [DACS px]" if bracketed else "Gaze point Y",
                "pupil_left": "Pupil diameter left [mm]" if bracketed else "Pupil diameter left",
                "pupil_right": "Pupil diameter right [mm]" if bracketed else "Pupil diameter right",
                "stimulus_id": "Presented Stimulus name",
            }, "mm")
    return tuple(profiles)


def _profile_directory(directory: str | os.PathLike[str] | None) -> Path:
    if directory is None:
        from pyeyedata_bootstrap import user_directory

        return user_directory("settings", "import-profiles")
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_profile_directory(directory: str | os.PathLike[str] | None = None) -> Path:
    """Return the per-user profile directory, creating it when necessary."""
    return _profile_directory(directory)


def list_import_profiles(
    *, directory: str | os.PathLike[str] | None = None,
) -> tuple[tuple[Path, ImportProfile | None, str | None], ...]:
    """List saved profiles while preserving unreadable entries for UI diagnosis."""
    root = _profile_directory(directory)
    entries: list[tuple[Path, ImportProfile | None, str | None]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name.casefold()):
        try:
            profile = read_import_profile(path)
            if profile.profile_id != path.stem:
                raise ImportProfileError("预设 ID 与文件名不一致，拒绝使用。")
        except ImportProfileError as exc:
            entries.append((path, None, str(exc)))
        else:
            entries.append((path, profile, None))
    return tuple(entries)


def profile_path(profile_id: str, *, directory: str | os.PathLike[str] | None = None) -> Path:
    return _profile_directory(directory) / f"{_profile_id(profile_id)}.json"


def save_import_profile(
    profile: ImportProfile, *, directory: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically save a profile, requiring a higher revision on replacement."""
    if not isinstance(profile, ImportProfile):
        raise ImportProfileError("只能保存 ImportProfile。")
    root = _profile_directory(directory)
    destination = root / f"{_profile_id(profile.profile_id)}.json"
    if destination.exists():
        existing = read_import_profile(destination)
        if existing.profile_id != profile.profile_id:
            raise ImportProfileError("目标预设 ID 与文件内容不一致，拒绝覆盖。")
        if profile.revision <= existing.revision:
            raise ImportProfileError("保存新预设时 revision 必须高于当前版本。")
    content = (json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(content) > MAX_IMPORT_PROFILE_BYTES:
        raise ImportProfileError("导入预设超过 2 MiB，未保存。")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=root, prefix=f".{profile.profile_id}-", suffix=".tmp", delete=False,
        ) as stream:
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


def read_import_profile(path: str | os.PathLike[str]) -> ImportProfile:
    """Read one profile and reject duplicate fields, unknown schemas, and oversized files."""
    try:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file() or source.stat().st_size > MAX_IMPORT_PROFILE_BYTES:
            raise ImportProfileError("导入预设必须是小于等于 2 MiB 的普通文件。")
        raw = json.loads(
            source.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except ImportProfileError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise ImportProfileError(f"无法读取导入预设：{exc}") from exc
    try:
        return ImportProfile.from_dict(raw)
    except ImportProfileError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise ImportProfileError(f"无法读取导入预设：{exc}") from exc


def load_import_profile(
    profile_id: str, *, directory: str | os.PathLike[str] | None = None,
) -> ImportProfile:
    """Load a profile by its safe ID from the per-user settings folder."""
    return read_import_profile(profile_path(profile_id, directory=directory))

