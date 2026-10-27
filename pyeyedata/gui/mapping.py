"""Explicit semantic mapping widgets for CSV/XLSX import.

Source columns are never selected automatically. Alias matches are shown only as
suggestions so the user can confirm each semantic assignment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pyeyedata.contracts import AOI_REQUIRED_COLUMNS, SEGMENT_REQUIRED_COLUMNS, SIGNAL_OPTIONAL_COLUMNS, SIGNAL_REQUIRED_COLUMNS
from pyeyedata.io import COLUMN_ALIASES


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    label: str
    description: str = ""
    required: bool = False
    standard: str = "按用户确认的单位与映射传给标准化接口"
    unit: str = ""


_LABELS: dict[str, tuple[str, str, str]] = {
    "subject_id": ("参与者 ID", "必须明确标识参与者；不按文件顺序生成。", ""),
    "session_id": ("会话 ID", "必须明确标识本次采集会话。", ""),
    "interaction_id": ("互动 ID", "共享互动范围的稳定标识。", ""),
    "time_s": ("采样时刻", "源时间由下方时间元数据明确解释，规范输出为秒。", "s"),
    "timestamp": ("时间戳（别名）", "与 time_s 二选一；不得同时映射两个候选时间列。", ""),
    "original_index": ("原始行序号", "保留输入的原始行身份；缺省时由读取器使用源行号。", ""),
    "gaze_x": ("注视点 X", "标准注视点坐标 X。", ""),
    "gaze_y": ("注视点 Y", "标准注视点坐标 Y。", ""),
    "gaze_z": ("注视点 Z", "三维注视点的 Z 分量。", ""),
    "pupil_left": ("左瞳孔", "左眼直径或面积；单位需单独确认。", ""),
    "pupil_right": ("右瞳孔", "右眼直径或面积；单位需单独确认。", ""),
    "gaze_valid": ("总体注视有效性", "保留源 code；真/假/未知映射需显式配置。", ""),
    "pupil_left_valid": ("左瞳孔有效性", "左眼独立有效性标志。", ""),
    "pupil_right_valid": ("右瞳孔有效性", "右眼独立有效性标志。", ""),
    "gaze_left_valid": ("左眼注视有效性", "左眼独立有效性标志。", ""),
    "gaze_right_valid": ("右眼注视有效性", "右眼独立有效性标志。", ""),
    "trial": ("试次 / trial", "契约字段名为 trial；trial_id 别名由用户确认。", ""),
    "condition": ("实验条件", "实验条件标识。", ""),
    "stimulus_id": ("刺激 ID", "刺激/材料的稳定标识。", ""),
    "gaze_left_x": ("左眼注视点 X", "左眼 point 表示；不推断与右眼处于同一坐标系。", ""),
    "gaze_left_y": ("左眼注视点 Y", "左眼 point 表示。", ""),
    "gaze_left_z": ("左眼注视点 Z", "左眼 point 表示。", ""),
    "gaze_right_x": ("右眼注视点 X", "右眼 point 表示。", ""),
    "gaze_right_y": ("右眼注视点 Y", "右眼 point 表示。", ""),
    "gaze_right_z": ("右眼注视点 Z", "右眼 point 表示。", ""),
    "gaze_left_direction_x": ("左眼视线方向 X", "方向向量分量，单位/坐标系独立确认。", ""),
    "gaze_left_direction_y": ("左眼视线方向 Y", "方向向量分量。", ""),
    "gaze_left_direction_z": ("左眼视线方向 Z", "方向向量分量。", ""),
    "gaze_right_direction_x": ("右眼视线方向 X", "方向向量分量，单位/坐标系独立确认。", ""),
    "gaze_right_direction_y": ("右眼视线方向 Y", "方向向量分量。", ""),
    "gaze_right_direction_z": ("右眼视线方向 Z", "方向向量分量。", ""),
    "gaze_left_origin_x": ("左眼原点 X", "三维 origin 分量，单位/坐标系独立确认。", ""),
    "gaze_left_origin_y": ("左眼原点 Y", "三维 origin 分量。", ""),
    "gaze_left_origin_z": ("左眼原点 Z", "三维 origin 分量。", ""),
    "gaze_right_origin_x": ("右眼原点 X", "三维 origin 分量，单位/坐标系独立确认。", ""),
    "gaze_right_origin_y": ("右眼原点 Y", "三维 origin 分量。", ""),
    "gaze_right_origin_z": ("右眼原点 Z", "三维 origin 分量。", ""),
    "start_s": ("起始时间", "左闭区间起点；源单位按已确认时间元数据换为秒。", "s"),
    "end_s": ("结束时间", "右开区间终点；源单位按已确认时间元数据换为秒。", "s"),
    "segment_id": ("段 ID（源）", "只作为来源审计；标准稳定 ID 按 A06 内容规则生成。", ""),
    "segment_name": ("段名称", "必填标签；保留不同分析标签，不把重叠标签当独立观察。", ""),
    "aoi_id": ("AOI ID", "源定义的稳定兴趣区 ID。", ""),
    "aoi_name": ("AOI 名称", "独立于 AOI ID 的源名称；缺失时需显式选择旧数据兼容，不能代造名称。", ""),
    "shape": ("AOI 形状", "读取器当前支持 rectangle 或 polygon。", ""),
    "vertices": ("多边形顶点 JSON", "填写 [[x,y],[x,y],...]；边界包含在 AOI 内，不支持洞或多环。", ""),
    "xmin": ("最小 X", "矩形左界。", ""),
    "ymin": ("最小 Y", "矩形上界。", ""),
    "xmax": ("最大 X", "矩形右界。", ""),
    "ymax": ("最大 Y", "矩形下界。", ""),
    "priority": ("重叠优先级", "exclusive 重叠策略要求用户明确优先级。", ""),
    "member_role": ("成员角色", "角色来自用户的实验设计声明。", ""),
}

_UNIT_CHOICES = ("", "unknown", "s", "ms", "frame", "px", "deg", "rad", "mm", "cm", "m", "area", "device")


def role_fields(role: str, required_fields: Iterable[str] | None = None) -> tuple[FieldSpec, ...]:
    """Return actual canonical fields supported by the current public import API."""
    if role == "signals":
        names = list(dict.fromkeys((*SIGNAL_REQUIRED_COLUMNS, *SIGNAL_OPTIONAL_COLUMNS, "timestamp")))
        required = set(SIGNAL_REQUIRED_COLUMNS)
    elif role == "segments":
        names = list(dict.fromkeys((*SEGMENT_REQUIRED_COLUMNS, "segment_name", "start_s", "end_s", "segment_id",
                                    "subject_id", "session_id", "interaction_id", "condition", "trial",
                                    "stimulus_id", "baseline_start_s", "baseline_end_s", "timebase_id",
                                    "segment_kind", "physical_semantics_ref", "overlap_policy")))
        required = {"segment_name", "start_s", "end_s"}
    elif role == "aois":
        names = list(dict.fromkeys((*AOI_REQUIRED_COLUMNS, "aoi_name", "xmin", "ymin", "xmax", "ymax", "vertices", "priority", "overlap_policy")))
        required = set(AOI_REQUIRED_COLUMNS) | {"aoi_name"}
    else:
        raise ValueError(f"No canonical mapping grid for role {role!r}")
    if required_fields is not None:
        required = set(required_fields)
    return tuple(_field_spec(name, name in required) for name in names)


def context_fields(kind: str) -> tuple[FieldSpec, ...]:
    definitions: dict[str, tuple[tuple[str, bool, str], ...]] = {
        "external_events": (
            ("event_id", True, "外部事件唯一 ID"), ("event_type", True, "事件类型"),
            ("start_s", True, "起始时间（秒）"), ("end_s", True, "结束时间（秒）"),
            ("subject_id", True, "参与者 ID"), ("session_id", True, "会话 ID"),
            ("interaction_id", False, "互动 ID"), ("trial", False, "试次"),
            ("eye", False, "眼别"), ("event_evidence", False, "来源证据类型"),
            ("source_refs", False, "来源引用"), ("source_version", False, "厂商/工具版本"),
            ("classification_method", False, "分类方法"), ("time_unit", False, "外部事件时间单位"),
            ("status", False, "状态"), ("reason_code", False, "原因码"),
        ),
        "target_movement_intervals": (
            ("start_s", True, "目标移动起始时间（秒）"), ("end_s", True, "目标移动结束时间（秒）"),
            ("subject_id", True, "参与者 ID"), ("session_id", True, "会话 ID"),
            ("interaction_id", False, "互动 ID"), ("segment_id", False, "段 ID"),
            ("target_id", False, "目标 ID"), ("source_id", False, "来源 ID"),
        ),
        "eyelid_intervals": (
            ("start_s", True, "眼睑区间起点（秒）"), ("end_s", True, "眼睑区间终点（秒）"),
            ("closed", True, "是否闭合（布尔）"), ("valid", True, "标记是否有效（布尔）"),
            ("subject_id", True, "参与者 ID"), ("session_id", True, "会话 ID"),
            ("interaction_id", False, "互动 ID"), ("segment_id", False, "段 ID"),
            ("eye", False, "眼别"),
        ),
        "reference_fixations": (
            ("reference_id", True, "参考数据 ID"), ("fixation_index", True, "注视序号"),
            ("start_x", True, "起始 X"), ("start_y", True, "起始 Y"),
            ("duration_s", True, "持续时间（秒）"), ("aoi_id", False, "AOI ID"),
        ),
        "reference_trajectory": (
            ("time_s", True, "参考轨迹时刻（秒）"), ("gaze_x", True, "参考位置 X"),
            ("gaze_y", True, "参考位置 Y"), ("reference_valid", False, "参考数据有效性"),
            ("reference_id", False, "参考数据 ID"), ("subject_id", False, "参与者 ID"),
            ("session_id", False, "会话 ID"), ("segment_id", False, "段 ID"),
        ),
        "reading_annotations": (
            ("reading_scope_id", True, "阅读范围 ID"), ("stimulus_id", False, "刺激 ID"),
            ("aoi_id", False, "阅读区域 AOI ID（计算必需）"),
            ("reading_order", False, "阅读推进顺序（正整数，计算必需）"),
            ("line_id", False, "行 ID"), ("start_s", False, "起始时间"), ("end_s", False, "结束时间"),
            ("reading_direction", False, "阅读方向"),
        ),
    }
    if kind not in definitions:
        raise ValueError(f"Unsupported context source kind: {kind}")
    return tuple(FieldSpec(name, label, label, required, label, "s" if name.endswith("_s") else "")
                 for name, required, label in definitions[kind])


def _field_spec(name: str, required: bool) -> FieldSpec:
    label, description, unit = _LABELS.get(name, (name, "按公开契约保留的源字段。", ""))
    if name in {"start_s", "end_s"}:
        standard = "按时间确认换为秒"
    elif name == "time_s":
        standard = "归一化后的秒（time_s）"
    elif name == "timestamp":
        standard = "作为 time_s 的源列候选；二选一"
    elif name in {"trial", "segment_id"}:
        standard = f"契约标准名 {name}；trial_id 是显式别名"
    else:
        standard = f"契约标准名 {name}，单位由用户声明"
    return FieldSpec(name, label, description, required, standard, unit)


def _fold(text: Any) -> str:
    return "".join(ch.casefold() for ch in str(text).strip() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _suggestion(field: str, headers: tuple[str, ...]) -> str:
    candidates = (field, *COLUMN_ALIASES.get(field, ()))
    folded = {_fold(name): name for name in headers}
    for candidate in candidates:
        if _fold(candidate) in folded:
            return folded[_fold(candidate)]
    return ""


class TableMappingEditor(QWidget):
    """Searchable mapping table; it reports candidates without auto-selecting them."""

    field_requested = Signal(str)
    mapping_changed = Signal()

    def __init__(
        self,
        role: str,
        *,
        required_fields: Iterable[str] | None = None,
        field_specs: Iterable[FieldSpec] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.fields = tuple(field_specs) if field_specs is not None else role_fields(role, required_fields)
        self._frame = pd.DataFrame()
        self._headers: tuple[str, ...] = ()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索内部字段或中文说明")
        self._table = QTableWidget(self)
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels(["内部字段", "中文含义", "源列（明确选择）", "原值示例", "标准值说明", "单位", "要求", "提示"])
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSortingEnabled(False)
        note = QLabel("候选别名仅供参考；下拉框默认未映射，只有明确选择后才会传给标准化器。", self)
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self._search)
        layout.addWidget(note)
        layout.addWidget(self._table, 1)
        self._search.textChanged.connect(self._apply_filter)
        self._build_rows()

    @property
    def source_headers(self) -> tuple[str, ...]:
        return self._headers

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        self._frame = frame.copy(deep=False) if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        self._headers = tuple(str(column) for column in self._frame.columns)
        previous = self.mapping()
        previous_units = self.units()
        self._build_rows(previous, previous_units)

    def set_required_fields(self, names: Iterable[str]) -> None:
        required = set(names)
        self.fields = tuple(FieldSpec(item.name, item.label, item.description, item.name in required,
                                      item.standard, item.unit) for item in self.fields)
        self._build_rows(self.mapping(), self.units())

    def _build_rows(self, previous: Mapping[str, str] | None = None, previous_units: Mapping[str, str] | None = None) -> None:
        previous = dict(previous or {})
        previous_units = dict(previous_units or {})
        self._table.setRowCount(len(self.fields))
        for row, spec in enumerate(self.fields):
            name_item = QTableWidgetItem(spec.name)
            name_item.setData(Qt.ItemDataRole.UserRole, spec.name)
            name_item.setToolTip(spec.name)
            self._table.setItem(row, 0, name_item)
            label_item = QTableWidgetItem(spec.label)
            label_item.setToolTip(spec.description)
            self._table.setItem(row, 1, label_item)
            source = QComboBox(self._table)
            source.addItem("未映射", "")
            for header in self._headers:
                source.addItem(header, header)
            selected = previous.get(spec.name, "")
            index = source.findData(selected)
            source.setCurrentIndex(index if index >= 0 else 0)
            source.currentIndexChanged.connect(lambda *_: self.mapping_changed.emit())
            self._table.setCellWidget(row, 2, source)
            source_name = source.currentData()
            sample = self._sample(source_name) if source_name else ""
            example = QTableWidgetItem(sample)
            example.setToolTip("原始数据预览值，不进行猜测或转换")
            self._table.setItem(row, 3, example)
            standard = QTableWidgetItem(spec.standard)
            standard.setToolTip(spec.description)
            self._table.setItem(row, 4, standard)
            unit = QComboBox(self._table)
            unit.setEditable(True)
            unit.addItem("未确认", "")
            for item in _UNIT_CHOICES[1:]:
                unit.addItem(item, item)
            wanted_unit = previous_units.get(spec.name, spec.unit)
            if wanted_unit:
                unit.setCurrentText(wanted_unit)
            unit.currentTextChanged.connect(lambda *_: self.mapping_changed.emit())
            self._table.setCellWidget(row, 5, unit)
            self._table.setItem(row, 6, QTableWidgetItem("必需" if spec.required else "可选"))
            suggestion = _suggestion(spec.name, self._headers)
            hint = ("候选：" + suggestion) if suggestion else ("待选择" if spec.required else "")
            hint_item = QTableWidgetItem(hint)
            if spec.required and not source.currentData():
                hint_item.setForeground(Qt.GlobalColor.darkRed)
            self._table.setItem(row, 7, hint_item)
        self._table.resizeColumnsToContents()
        self._table.setColumnWidth(2, 185)
        self._table.setColumnWidth(4, 230)
        self._apply_filter(self._search.text())

    def _sample(self, name: Any) -> str:
        if not isinstance(name, str) or name not in self._frame.columns or self._frame.empty:
            return ""
        values = self._frame[name].dropna().head(2).tolist()
        return " / ".join(str(value)[:80] for value in values)

    def _apply_filter(self, text: str) -> None:
        query = _fold(text)
        for row, spec in enumerate(self.fields):
            match = not query or query in _fold(spec.name + " " + spec.label + " " + spec.description)
            self._table.setRowHidden(row, not match)

    def mapping(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row, spec in enumerate(self.fields):
            source = self._table.cellWidget(row, 2)
            value = source.currentData() if isinstance(source, QComboBox) else ""
            if isinstance(value, str) and value.strip():
                result[spec.name] = value
        return result

    def units(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row, spec in enumerate(self.fields):
            source = self._table.cellWidget(row, 2)
            unit = self._table.cellWidget(row, 5)
            if isinstance(source, QComboBox) and source.currentData() and isinstance(unit, QComboBox):
                value = unit.currentText().strip()
                if value:
                    result[spec.name] = value
        return result

    def validation_messages(self) -> list[str]:
        mapped = self.mapping()
        problems = [
            f"{spec.label}（{spec.name}）尚未映射"
            for spec in self.fields
            if spec.required and spec.name not in mapped
            and not (self.role == "signals" and spec.name == "time_s" and "timestamp" in mapped)
        ]
        if self.role == "signals" and "time_s" in mapped and "timestamp" in mapped:
            problems.append("time_s 与 timestamp 只能选择其中一列")
        return problems

    def focus_field(self, name: str) -> bool:
        for row, spec in enumerate(self.fields):
            if spec.name == name:
                self._table.setRowHidden(row, False)
                self._table.setCurrentCell(row, 2)
                self._table.scrollToItem(self._table.item(row, 0))
                self.field_requested.emit(name)
                return True
        return False

    def frame_copy(self) -> pd.DataFrame:
        return self._frame.copy(deep=True)


class PerformanceMappingWidget(QWidget):
    """Explicitly describe performance columns and one-to-one join keys."""

    mapping_changed = Signal()
    _ROLES = ("未指定", "subject_id", "session_id", "interaction_id", "between_group", "condition", "trial", "member_role", "performance_value")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame = pd.DataFrame()
        self.row_level = QComboBox(self)
        self.row_level.addItem("参与者", "participant")
        self.row_level.addItem("会话", "session")
        self.row_level.addItem("试次", "trial")
        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["源列", "语义字段", "连接键", "单位/类型说明"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        self.summary = QLabel("尚未载入绩效表。连接键必须显式选择；不会自动合并到信号行。", self)
        self.summary.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(QLabel("绩效行层级", self))
        row.addWidget(self.row_level)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        self.row_level.currentIndexChanged.connect(lambda *_: self.mapping_changed.emit())
        self.mapping_changed.connect(self._update_summary)

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        self._frame = frame.copy(deep=False) if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        self.table.setRowCount(len(self._frame.columns))
        for row, column in enumerate(self._frame.columns):
            self.table.setItem(row, 0, QTableWidgetItem(str(column)))
            role = QComboBox(self.table)
            role.addItems(self._ROLES)
            role.currentIndexChanged.connect(lambda *_: self.mapping_changed.emit())
            self.table.setCellWidget(row, 1, role)
            key = QCheckBox(self.table)
            key.toggled.connect(lambda *_: self.mapping_changed.emit())
            self.table.setCellWidget(row, 2, key)
            unit = QLineEdit("", self.table)
            unit.setPlaceholderText("明确单位；未知可写 unknown")
            unit.textChanged.connect(lambda *_: self.mapping_changed.emit())
            self.table.setCellWidget(row, 3, unit)
        self.table.resizeColumnsToContents()
        self._update_summary()

    def value(self) -> dict[str, Any]:
        roles: dict[str, str] = {}
        outcomes: list[str] = []
        keys: list[str] = []
        units: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            if name_item is None:
                continue
            source = name_item.text()
            role = self.table.cellWidget(row, 1)
            key = self.table.cellWidget(row, 2)
            unit = self.table.cellWidget(row, 3)
            role_value = role.currentText() if isinstance(role, QComboBox) else "未指定"
            if role_value != "未指定":
                if role_value == "performance_value":
                    outcomes.append(source)
                    if isinstance(unit, QLineEdit) and unit.text().strip():
                        units[source] = unit.text().strip()
                else:
                    roles[role_value] = source
            if isinstance(key, QCheckBox) and key.isChecked():
                keys.append(source)
        return {"row_level": self.row_level.currentData(), "semantic_fields": roles,
                "performance_columns": outcomes, "performance_join_keys": keys, "performance_units": units}

    def audit(self, signal_frame: pd.DataFrame | None = None) -> dict[str, Any]:
        spec = self.value()
        keys = spec["performance_join_keys"]
        result: dict[str, Any] = {"row_level": spec["row_level"], "keys": keys,
                                  "outcome_columns": spec["performance_columns"], "duplicate_performance_keys": None,
                                  "empty_performance_keys": None, "duplicate_signal_keys": None,
                                  "unmatched_performance_rows": None, "matched_performance_rows": None,
                                  "status": "unresolved"}
        if not isinstance(self._frame, pd.DataFrame) or self._frame.empty or not keys:
            result["reason"] = "请载入绩效数据并明确选择至少一个连接键。"
            return result
        missing = [key for key in keys if key not in self._frame]
        if missing:
            result["reason"] = "连接键不存在：" + ", ".join(missing)
            return result
        empty = self._frame[keys].isna().any(axis=1)
        for key in keys:
            if pd.api.types.is_string_dtype(self._frame[key]) or self._frame[key].dtype == object:
                empty |= self._frame[key].astype("string").str.strip().eq("").fillna(True)
        duplicate_performance = int(self._frame.loc[~empty].duplicated(keys, keep=False).sum())
        result.update({"empty_performance_keys": int(empty.sum()), "duplicate_performance_keys": duplicate_performance})
        if isinstance(signal_frame, pd.DataFrame) and not signal_frame.empty:
            semantic = spec["semantic_fields"]
            canonical_by_source = {source: canonical for canonical, source in semantic.items()}
            signal_keys = [canonical_by_source.get(key, key) for key in keys]
            absent = [key for key in signal_keys if key not in signal_frame]
            if not absent:
                left = self._frame.loc[~empty, keys].astype("string").drop_duplicates()
                right_empty = signal_frame[signal_keys].isna().any(axis=1)
                right = signal_frame.loc[~right_empty, signal_keys].copy()
                right.columns = keys
                result["duplicate_signal_keys"] = int(right.duplicated(keys, keep=False).sum())
                available = set(map(tuple, right.astype("string").drop_duplicates().to_numpy().tolist()))
                row_keys = list(map(tuple, self._frame.loc[~empty, keys].astype("string").to_numpy().tolist()))
                result["matched_performance_rows"] = sum(value in available for value in row_keys)
                result["unmatched_performance_rows"] = sum(value not in available for value in row_keys)
                result["status"] = "unique" if duplicate_performance == 0 and int(result["unmatched_performance_rows"]) == 0 else "review"
            else:
                result["reason"] = "信号表缺少对应连接字段：" + ", ".join(absent)
        else:
            result["status"] = "keys_checked"
        if not spec["performance_columns"]:
            result["reason"] = "尚未选择绩效结果列。"
            result["status"] = "review"
        elif any(column not in spec["performance_units"] for column in spec["performance_columns"]):
            result["reason"] = "至少一个结果列尚未明确单位；未知单位需显式填写 unknown。"
            result["status"] = "review"
        return result

    def _update_summary(self) -> None:
        audit = self.audit()
        self.summary.setText(
            f"行层级：{audit['row_level']}；连接键：{', '.join(audit['keys']) or '未选择'}；"
            f"空键：{audit['empty_performance_keys'] if audit['empty_performance_keys'] is not None else '待检查'}；"
            f"重复绩效键：{audit['duplicate_performance_keys'] if audit['duplicate_performance_keys'] is not None else '待检查'}。"
            " 当前仅生成审计，不会自动 join 或把绩效行当作独立被试。"
        )
