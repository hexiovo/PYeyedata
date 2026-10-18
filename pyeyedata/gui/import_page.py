"""Four-source CSV/XLSX import and explicit semantic normalization UI."""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pyeyedata.contracts import (
    CoordinateMetadata,
    EyeCoordinateMetadata,
    NormalizedDataset,
    SIGNAL_REQUIRED_COLUMNS,
)
from pyeyedata.gui.models import DatasetRevision
from pyeyedata.io import (
    CsvOptions,
    DataImportError,
    SignalImportOptions,
    normalize_aois,
    normalize_performance,
    normalize_segments,
    normalize_signals,
    read_table,
)
from pyeyedata.scopes import MemberSessionMapping, SegmentScopeConfig

from .context_editor import ContextEditor
from .mapping import PerformanceMappingWidget, TableMappingEditor, role_fields
from .widgets import DataFrameTableModel, DataFrameTableView


_ROLE_TITLES = {
    "signals": "主要信号",
    "segments": "Segment Mark（段/试次）",
    "aois": "ROI / AOI",
    "performance": "参与者 / 绩效",
}


def _shared_pupil_unit(units: Mapping[str, str]) -> str:
    """Return a shared unit only when all mapped eyes explicitly agree."""
    values = [str(value).strip() for value in units.values() if str(value).strip()]
    if not values or any(value.casefold() == "unknown" for value in values):
        return "unknown"
    if len({value.casefold() for value in values}) != 1:
        return "unknown"
    return values[0]


class _ReadSignals(QObject):
    completed = Signal(int, object)
    failed = Signal(int, str)


class _PreviewWorker(QRunnable):
    def __init__(self, generation: int, path: str, role: str, sheet: str | None, options: CsvOptions) -> None:
        super().__init__()
        self.generation = generation
        self.path = path
        self.role = role
        self.sheet = sheet
        self.options = options
        self.signals = _ReadSignals()

    @Slot()
    def run(self) -> None:
        try:
            imported = read_table(self.path, role=self.role, sheet_name=self.sheet,
                                  csv_options=self.options, preview_rows=100)
            self.signals.completed.emit(self.generation, {
                "frame": imported.frame, "manifest": imported.manifest_entry(),
                "source_rows": imported.source_rows, "sheet_name": imported.sheet_name,
            })
        except DataImportError as exc:
            if Path(self.path).suffix.lower() == ".xlsx" and "Select a worksheet explicitly" in str(exc):
                try:
                    sheets = _safe_sheet_names(self.path)
                    self.signals.completed.emit(self.generation, {"sheets": sheets, "requires_sheet": True})
                    return
                except Exception as sheet_exc:
                    self.signals.failed.emit(self.generation, f"{exc}; 无法列出工作表：{sheet_exc}")
                    return
            self.signals.failed.emit(self.generation, str(exc))
        except Exception as exc:
            self.signals.failed.emit(self.generation, f"{type(exc).__name__}: {exc}")


def _safe_sheet_names(path: str) -> tuple[str, ...]:
    file_path = Path(path).expanduser().resolve(strict=True)
    if file_path.suffix.lower() != ".xlsx" or file_path.stat().st_size > 2 * 1024**3:
        raise DataImportError("只支持受限大小的 XLSX 工作簿")
    # read_table performs the archive guard before returning this multi-sheet notice.
    try:
        read_table(file_path, role="signals", preview_rows=1)
    except DataImportError as exc:
        if "Select a worksheet explicitly" not in str(exc):
            raise
    workbook = __import__("openpyxl").load_workbook(file_path, read_only=True, data_only=False, keep_links=False)
    try:
        return tuple(workbook.sheetnames)
    finally:
        workbook.close()


@dataclass(frozen=True, slots=True)
class _SourceRequest:
    role: str
    path: str
    sheet_name: str | None
    csv_options: CsvOptions
    column_map: dict[str, str]
    units: dict[str, str]
    performance_mapping: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ImportRequest:
    sources: tuple[_SourceRequest, ...]
    coordinate: CoordinateMetadata
    time_confirmed: bool
    signal_subject_id: str | None
    signal_session_id: str | None
    validity_values: dict[str, dict[str, bool]]
    reading_direction_values: dict[str, str]
    synchronization_method: str | None
    synchronization_source_refs: tuple[str, ...]
    pupil_units_by_eye: dict[str, str]
    scope: SegmentScopeConfig
    member_session_map: tuple[MemberSessionMapping, ...]
    dataset_scope: Mapping[str, Any] | None
    aoi_overlap_policy: str | None
    aoi_overlap_rule_ref: str | None
    aoi_missing_name_policy: str = "require_source"


class _ImportSignals(QObject):
    completed = Signal(object, object)
    failed = Signal(str)
    progress = Signal(int, str)


class _NormalizeWorker(QRunnable):
    """Read, normalize and freeze a complete import off the Qt GUI thread."""

    def __init__(self, request: _ImportRequest) -> None:
        super().__init__()
        self.request = request
        self.signals = _ImportSignals()

    @Slot()
    def run(self) -> None:
        try:
            normalized: NormalizedDataset | None = None
            raw_sources: dict[str, bytes] = {}
            manifests: dict[str, dict[str, Any]] = {}
            performance_meta: dict[str, Any] = {}
            for index, spec in enumerate(self.request.sources, 1):
                self.signals.progress.emit(
                    round(5 + 75 * (index - 1) / max(1, len(self.request.sources))),
                    f"读取与规范化：{_ROLE_TITLES[spec.role]}",
                )
                imported = read_table(spec.path, role=spec.role, sheet_name=spec.sheet_name,
                                      csv_options=spec.csv_options, preview_rows=None)
                source_bytes = Path(spec.path).read_bytes()
                byte_hash = hashlib.sha256(source_bytes).hexdigest()
                if byte_hash != imported.sha256:
                    raise DataImportError(f"{spec.role} 来源文件在读取期间发生变化，哈希不一致。")
                raw_key = f"{spec.role}/{imported.source_name}"
                raw_sources[raw_key] = source_bytes
                manifests[spec.role] = {**imported.manifest_entry(), "source_path": str(imported.source_path),
                                        "raw_source_key": raw_key, "column_mapping": spec.column_map,
                                        "field_units": spec.units}
                if spec.role == "signals":
                    normalized = normalize_signals(imported, SignalImportOptions(
                        column_map=spec.column_map,
                        subject_id=self.request.signal_subject_id,
                        session_id=self.request.signal_session_id,
                        time=self.request.coordinate,
                        validity_values=self.request.validity_values,
                        reading_direction_values=self.request.reading_direction_values,
                    ))
                elif spec.role == "segments":
                    if normalized is None:
                        raise DataImportError("主要信号必须先完成规范化。")
                    dataset_scope = dict(self.request.dataset_scope) if self.request.dataset_scope is not None else None
                    normalized.segments = normalize_segments(
                        imported,
                        column_map=spec.column_map,
                        time=self.request.coordinate,
                        subject_id=self.request.signal_subject_id,
                        session_id=self.request.signal_session_id,
                        scope_config=self.request.scope,
                        member_session_map=self.request.member_session_map or None,
                        dataset_scope=dataset_scope,
                        timebase_id=self.request.scope.timebase_id,
                    )
                elif spec.role == "aois":
                    if normalized is None:
                        raise DataImportError("主要信号必须先完成规范化。")
                    try:
                        normalized.aois = normalize_aois(
                            imported,
                            column_map=spec.column_map,
                            coordinate_unit=self.request.coordinate.coordinate_unit,
                            coordinate_frame=self.request.coordinate.coordinate_frame,
                            overlap_policy=self.request.aoi_overlap_policy,
                            missing_name_policy=self.request.aoi_missing_name_policy,
                        )
                        normalized.metadata["aoi_import_status"] = "validated"
                    except DataImportError as exc:
                        # An invalid optional AOI draft never becomes valid geometry
                        # and does not discard the independently valid primary signal.
                        normalized.aois = pd.DataFrame()
                        normalized.metadata["aoi_import_status"] = "invalid_draft"
                        normalized.metadata["aoi_import_issues"] = getattr(exc, "issues", [{
                            "code": "aoi_import_invalid", "message": str(exc),
                            "source_file": str(imported.source_path), "sheet": imported.sheet_name,
                            "source_row": None, "canonical_field": None}])
                        normalized.metadata["aoi_invalid_draft"] = {
                            "source_name": imported.source_name, "sha256": imported.sha256,
                            "raw_source_key": raw_key, "column_mapping": spec.column_map,
                            "missing_name_policy": self.request.aoi_missing_name_policy}
                elif spec.role == "performance":
                    if normalized is None:
                        raise DataImportError("主要信号必须先完成规范化。")
                    normalized.performance = normalize_performance(imported)
                    performance_meta = dict(spec.performance_mapping)

            if normalized is None:
                raise DataImportError("缺少主要信号 CSV/XLSX 文件。")
            normalized.source_manifest = [manifests[key] for key in ("signals", "segments", "aois", "performance") if key in manifests]
            normalized.metadata["source_manifest"] = normalized.source_manifest
            normalized.metadata["b02_import_mappings"] = {
                key: {"column_mapping": value["column_mapping"], "field_units": value["field_units"],
                      "source_name": value["source_name"], "source_sheet": value["sheet_name"],
                      "sha256": value["sha256"], "source_path": value["source_path"]}
                for key, value in manifests.items()
            }
            normalized.metadata["performance_mapping"] = performance_meta
            normalized.metadata["pupil_units_by_eye"] = dict(self.request.pupil_units_by_eye)
            normalized.metadata["reading_direction_values"] = dict(self.request.reading_direction_values)
            normalized.metadata["aoi_overlap_policy"] = self.request.aoi_overlap_policy or "unresolved"
            normalized.metadata["aoi_overlap_rule_ref"] = self.request.aoi_overlap_rule_ref
            normalized.metadata["time_unit"] = "s"
            normalized.metadata["time_confirmed"] = bool(self.request.time_confirmed)
            normalized.metadata["source_time_unit"] = self.request.coordinate.time_unit
            normalized.metadata["synchronization_method"] = self.request.synchronization_method
            normalized.metadata["synchronization_source_refs"] = list(self.request.synchronization_source_refs)
            normalized.metadata["analysis_metadata"] = {
                "time_unit": "s",
                "normalized_time_unit": "s",
                "source_time_unit": self.request.coordinate.time_unit,
                "time_confirmed": bool(self.request.time_confirmed),
                "synchronization_confirmed": bool(self.request.coordinate.sync_confirmed),
                "synchronization": {
                    "confirmed": bool(self.request.coordinate.sync_confirmed),
                    "method": self.request.synchronization_method,
                    "source_refs": list(self.request.synchronization_source_refs),
                },
                "clock_offsets_s": dict(self.request.coordinate.clock_offsets_s),
                "coordinate_unit": self.request.coordinate.coordinate_unit,
                "coordinate_frame": self.request.coordinate.coordinate_frame,
                "pupil_units_by_eye": dict(self.request.pupil_units_by_eye),
                "pupil_unit": _shared_pupil_unit(self.request.pupil_units_by_eye),
                "synchronization_method": self.request.synchronization_method,
                "synchronization_source_refs": list(self.request.synchronization_source_refs),
                "aoi_overlap_policy": self.request.aoi_overlap_policy or "unresolved",
                "aoi_overlap_rule_ref": self.request.aoi_overlap_rule_ref,
                "device_validation_status": self.request.coordinate.device_validation_status,
                "device_validation_method": self.request.coordinate.device_validation_method,
                "device_validation_evidence": list(self.request.coordinate.device_validation_evidence),
            }
            self.signals.progress.emit(90, "校验规范化数据与源字节哈希")
            normalized.validate()
            revision = DatasetRevision.create(normalized, raw_sources=raw_sources)
            preview = normalized.signals.head(500).copy(deep=True)
            self.signals.progress.emit(100, "四类数据导入完成")
            self.signals.completed.emit(revision, preview)
        except Exception as exc:
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")


class CoordinateSettingsWidget(QWidget):
    """Separate source time, synchronization, eye and device declarations."""

    settings_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.time_unit = QComboBox(self)
        for label, value in (("请选择时间单位", "unknown"), ("秒 s", "s"), ("毫秒 ms", "ms"),
                             ("日期时间 datetime", "datetime"), ("帧 frame", "frame")):
            self.time_unit.addItem(label, value)
        self.time_confirmed = QCheckBox("我已确认来源时间单位、原点和精度", self)
        self.time_origin = QLineEdit(self)
        self.timezone = QLineEdit(self)
        self.timezone.setPlaceholderText("datetime 时填写，例如 Asia/Shanghai")
        self.clock_source = QLineEdit(self)
        self.clock_source.setPlaceholderText("设备 / 时钟来源 ID")
        self.precision = QDoubleSpinBox(self)
        self.precision.setDecimals(9)
        self.precision.setRange(0, 1000000)
        self.precision.setSpecialValueText("未提供")
        self.frame_id = QLineEdit(self)
        self.frame_rate = QDoubleSpinBox(self)
        self.frame_rate.setDecimals(6)
        self.frame_rate.setRange(0, 1000000)
        self.frame_rate.setSpecialValueText("未提供")
        self.timecode_format = QLineEdit(self)
        self.subject_id = QLineEdit(self)
        self.subject_id.setPlaceholderText("若源表没有 subject_id，在此显式指定")
        self.session_id = QLineEdit(self)
        self.session_id.setPlaceholderText("若源表没有 session_id，在此显式指定")
        self.sync_confirmed = QCheckBox("已确认时钟同步关系", self)
        self.sync_method = QLineEdit(self)
        self.sync_method.setPlaceholderText("同步方法（不按文件顺序推断）")
        self.sync_refs = QLineEdit(self)
        self.sync_refs.setPlaceholderText("证据引用；多个用分号隔开")
        self.offsets = QTableWidget(0, 2, self)
        self.offsets.setHorizontalHeaderLabels(["clock_id", "相对主时钟偏移（秒）"])
        self.offsets.horizontalHeader().setStretchLastSection(True)
        add_offset = QPushButton("添加时钟偏移", self)
        remove_offset = QPushButton("移除选中", self)
        add_offset.clicked.connect(self._add_offset)
        remove_offset.clicked.connect(self._remove_offset)

        self.coordinate_unit = QLineEdit("unknown", self)
        self.coordinate_frame = QLineEdit("unknown", self)
        self.representation = QComboBox(self)
        for value in ("unknown", "2d_point", "3d_point", "direction", "origin"):
            self.representation.addItem(value, value)
        self.pupil_left_unit = QLineEdit(self)
        self.pupil_left_unit.setPlaceholderText("必需时填写 mm、area、device 或 unknown")
        self.pupil_right_unit = QLineEdit(self)
        self.pupil_right_unit.setPlaceholderText("必需时填写 mm、area、device 或 unknown")
        self.device_status = QComboBox(self)
        for label, value in (("未知", "unknown"), ("未验证", "unverified"), ("用户声明已验证", "verified")):
            self.device_status.addItem(label, value)
        self.device_method = QLineEdit(self)
        self.device_evidence = QLineEdit(self)
        self.device_evidence.setPlaceholderText("用户提供的验证依据引用，多个用分号隔开")
        self.eye_fields: dict[str, QLineEdit] = {}
        for side, prefix in (("左眼", "left"), ("右眼", "right")):
            for rep in ("point", "direction", "origin"):
                for attr in ("unit", "frame"):
                    edit = QLineEdit("unknown", self)
                    self.eye_fields[f"{prefix}_{rep}_{attr}"] = edit

        time_box = QGroupBox("源时间与同步（单位和确认分开记录）", self)
        time_form = QFormLayout(time_box)
        time_form.addRow("源时间单位", self.time_unit)
        time_form.addRow("来源时间已确认", self.time_confirmed)
        time_form.addRow("默认参与者 ID", self.subject_id)
        time_form.addRow("默认会话 ID", self.session_id)
        time_form.addRow("时间原点", self.time_origin)
        time_form.addRow("时区", self.timezone)
        time_form.addRow("时钟来源", self.clock_source)
        time_form.addRow("时间精度（秒）", self.precision)
        time_form.addRow("frame_id", self.frame_id)
        time_form.addRow("frame_rate_hz", self.frame_rate)
        time_form.addRow("timecode_format", self.timecode_format)
        time_form.addRow("跨时钟同步", self.sync_confirmed)
        time_form.addRow("同步方法", self.sync_method)
        time_form.addRow("同步依据", self.sync_refs)
        offset_row = QHBoxLayout()
        offset_row.addWidget(add_offset)
        offset_row.addWidget(remove_offset)
        offset_row.addStretch(1)
        time_form.addRow("按 clock_id 的偏移", self.offsets)
        time_form.addRow("偏移编辑", offset_row)

        coordinate_box = QGroupBox("坐标、眼别瞳孔单位和设备声明", self)
        coordinate_form = QFormLayout(coordinate_box)
        coordinate_form.addRow("总体坐标单位", self.coordinate_unit)
        coordinate_form.addRow("总体坐标系 / frame", self.coordinate_frame)
        coordinate_form.addRow("表示类型", self.representation)
        coordinate_form.addRow("左瞳孔单位", self.pupil_left_unit)
        coordinate_form.addRow("右瞳孔单位", self.pupil_right_unit)
        for side, prefix in (("左眼", "left"), ("右眼", "right")):
            eye_group = QGroupBox(side + " gaze point / direction / origin", coordinate_box)
            eye_form = QFormLayout(eye_group)
            for rep in ("point", "direction", "origin"):
                unit = self.eye_fields[f"{prefix}_{rep}_unit"]
                frame = self.eye_fields[f"{prefix}_{rep}_frame"]
                eye_form.addRow(rep + " 单位", unit)
                eye_form.addRow(rep + " 坐标系", frame)
            coordinate_form.addRow(eye_group)
        coordinate_form.addRow("设备状态（用户声明）", self.device_status)
        coordinate_form.addRow("声明方法", self.device_method)
        coordinate_form.addRow("声明依据", self.device_evidence)
        warning = QLabel("软件不会认证设备；设备状态默认未知。左/右眼坐标与瞳孔单位独立保存。", self)
        warning.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(time_box)
        layout.addWidget(coordinate_box)
        layout.addWidget(warning)
        for widget in (self.time_unit, self.time_confirmed, self.sync_confirmed, self.device_status, self.representation):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(lambda *_: self.settings_changed.emit())
            else:
                widget.currentIndexChanged.connect(lambda *_: self.settings_changed.emit())
        for widget in (self.time_origin, self.timezone, self.clock_source, self.frame_id, self.timecode_format,
                       self.sync_method, self.sync_refs, self.coordinate_unit, self.coordinate_frame,
                       self.subject_id, self.session_id, self.pupil_left_unit, self.pupil_right_unit,
                       self.device_method, self.device_evidence,
                       *self.eye_fields.values()):
            widget.textChanged.connect(lambda *_: self.settings_changed.emit())
        self.precision.valueChanged.connect(lambda *_: self.settings_changed.emit())
        self.frame_rate.valueChanged.connect(lambda *_: self.settings_changed.emit())

    def _add_offset(self) -> None:
        row = self.offsets.rowCount()
        self.offsets.insertRow(row)
        self.offsets.setItem(row, 0, QTableWidgetItem(""))
        self.offsets.setItem(row, 1, QTableWidgetItem("0"))
        self.settings_changed.emit()

    def _remove_offset(self) -> None:
        rows = sorted({idx.row() for idx in self.offsets.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            self.offsets.removeRow(row)
        self.settings_changed.emit()

    def coordinate_metadata(self) -> CoordinateMetadata:
        unit = str(self.time_unit.currentData() or "unknown")
        if unit == "frame" and self.frame_rate.value() <= 0:
            raise ValueError("源时间单位为 frame 时必须填写正 frame_rate_hz。")
        if unit == "datetime" and not self.timezone.text().strip():
            raise ValueError("datetime 源时间必须填写时区。")
        if unit == "datetime" and not self.time_origin.text().strip():
            raise ValueError("datetime 源时间必须填写明确的 time_origin。")
        offsets: dict[str, float] = {}
        for row in range(self.offsets.rowCount()):
            clock_item, offset_item = self.offsets.item(row, 0), self.offsets.item(row, 1)
            if not clock_item or not offset_item or not clock_item.text().strip():
                raise ValueError(f"时钟偏移第 {row + 1} 行需要 clock_id 和偏移秒值。")
            clock_id = clock_item.text().strip()
            if clock_id in offsets:
                raise ValueError(f"时钟偏移 clock_id 重复：{clock_id}")
            value = float(offset_item.text().strip())
            if not np.isfinite(value):
                raise ValueError(f"时钟偏移第 {row + 1} 行必须是有限秒值。")
            offsets[clock_id] = value
        device_status = str(self.device_status.currentData() or "unknown")
        evidence = tuple(value.strip() for value in self.device_evidence.text().split(";") if value.strip())
        method = self.device_method.text().strip() or None
        if device_status == "verified" and (not method or not evidence):
            raise ValueError("用户声明设备已验证时，请填写方法和依据引用。")
        eye = {}
        for prefix in ("left", "right"):
            eye[prefix] = EyeCoordinateMetadata(**{
                f"{rep}_{attr}": self.eye_fields[f"{prefix}_{rep}_{attr}"].text().strip() or "unknown"
                for rep in ("point", "direction", "origin") for attr in ("unit", "frame")
            })
        return CoordinateMetadata(
            time_unit=unit,
            time_origin=self.time_origin.text().strip() or None,
            timezone=self.timezone.text().strip() or None,
            clock_source=self.clock_source.text().strip() or None,
            sync_confirmed=self.sync_confirmed.isChecked(),
            clock_offsets_s=offsets,
            time_precision_s=self.precision.value() or None,
            frame_id=self.frame_id.text().strip() or None,
            frame_rate_hz=self.frame_rate.value() or None,
            timecode_format=self.timecode_format.text().strip() or None,
            coordinate_unit=self.coordinate_unit.text().strip() or "unknown",
            coordinate_frame=self.coordinate_frame.text().strip() or "unknown",
            coordinate_representation=self.representation.currentData(),
            left_eye=eye["left"],
            right_eye=eye["right"],
            device_verified=(device_status == "verified"),
            device_validation_status=device_status,
            device_validation_method=method,
            device_validation_evidence=evidence,
        )

class _ValidCodeTable(QWidget):
    """Small explicit source-code to True/False mapping table."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.validity_table = QTableWidget(0, 3, self)
        self.validity_table.setHorizontalHeaderLabels(["标准有效性字段", "有效源 code（分号分隔）", "无效源 code（分号分隔）"])
        self.validity_table.horizontalHeader().setStretchLastSection(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("空白 code 保持未知；非空 code 必须明确映射为有效或无效，否则后端会拒绝导入。", self))
        layout.addWidget(self.validity_table)

    def configure_validity_fields(self, mapped: Mapping[str, str], preview: pd.DataFrame) -> None:
        bool_fields = {"gaze_valid", "pupil_left_valid", "pupil_right_valid", "gaze_left_valid", "gaze_right_valid",
                       "tracking_valid", "target_valid", "reference_valid", "eyelid_closed_left", "eyelid_closed_right"}
        selected = [name for name in mapped if name in bool_fields]
        existing: dict[str, tuple[str, str]] = {}
        for row in range(self.validity_table.rowCount()):
            field_item = self.validity_table.item(row, 0)
            valid_edit = self.validity_table.cellWidget(row, 1)
            invalid_edit = self.validity_table.cellWidget(row, 2)
            if field_item and isinstance(valid_edit, QLineEdit) and isinstance(invalid_edit, QLineEdit):
                existing[field_item.text()] = (valid_edit.text(), invalid_edit.text())
        self.validity_table.setRowCount(len(selected))
        for row, name in enumerate(selected):
            self.validity_table.setItem(row, 0, QTableWidgetItem(name))
            true_edit = QLineEdit(self.validity_table)
            true_edit.setPlaceholderText("有效码；分号分隔")
            false_edit = QLineEdit(self.validity_table)
            false_edit.setPlaceholderText("无效码；分号分隔")
            if name in existing:
                true_edit.setText(existing[name][0])
                false_edit.setText(existing[name][1])
            true_edit.textChanged.connect(lambda *_: self.changed.emit())
            false_edit.textChanged.connect(lambda *_: self.changed.emit())
            self.validity_table.setCellWidget(row, 1, true_edit)
            self.validity_table.setCellWidget(row, 2, false_edit)
            source = mapped.get(name)
            if source in preview:
                codes = ", ".join(map(str, preview[source].dropna().drop_duplicates().head(8).tolist()))
                true_edit.setToolTip("预览到的原始代码（仅供查看，不自动分类）：" + codes)
                false_edit.setToolTip("预览到的原始代码（仅供查看，不自动分类）：" + codes)

    def values(self) -> dict[str, dict[str, bool]]:
        result: dict[str, dict[str, bool]] = {}
        for row in range(self.validity_table.rowCount()):
            field_item = self.validity_table.item(row, 0)
            valid_edit = self.validity_table.cellWidget(row, 1)
            invalid_edit = self.validity_table.cellWidget(row, 2)
            if not field_item or not isinstance(valid_edit, QLineEdit) or not isinstance(invalid_edit, QLineEdit):
                continue
            name = field_item.text().strip()
            valid = [value.strip() for value in valid_edit.text().split(";") if value.strip()]
            invalid = [value.strip() for value in invalid_edit.text().split(";") if value.strip()]
            overlap = {value.casefold() for value in valid} & {value.casefold() for value in invalid}
            if overlap:
                raise ValueError(f"{name} 的有效码与无效码重叠：{', '.join(sorted(overlap))}")
            codes = {value: True for value in valid}
            codes.update({value: False for value in invalid})
            if codes:
                result[name] = codes
        return result


class _ReadingDirectionTable(QWidget):
    """Explicitly translate unusual source codes to the public direction enum."""

    changed = Signal()
    _DIRECTIONS = ("left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_column: str | None = None
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["原始源代码", "标准阅读方向"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.add_button = QPushButton("添加代码映射", self)
        self.remove_button = QPushButton("移除选中", self)
        self.add_button.clicked.connect(self.add_row)
        self.remove_button.clicked.connect(self.remove_rows)
        row = QHBoxLayout()
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("只为非标准来源代码添加映射；空白码保持后端原有解析规则。", self))
        layout.addWidget(self.table)
        layout.addLayout(row)

    def configure(self, source_column: str | None, preview: pd.DataFrame) -> None:
        previous = self._snapshot_rows() if source_column == self._source_column else []
        self._source_column = source_column
        if not source_column or source_column not in preview:
            self.table.setRowCount(0)
            return
        codes = list(dict.fromkeys(str(value) for value in preview[source_column].dropna().tolist()))[:20]
        values = previous or [(code, "") for code in codes]
        self.table.setRowCount(0)
        for code, direction in values:
            self._append_row(str(code), direction)

    def _snapshot_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for row in range(self.table.rowCount()):
            edit = self.table.cellWidget(row, 0)
            choice = self.table.cellWidget(row, 1)
            code = edit.text().strip() if isinstance(edit, QLineEdit) else ""
            direction = choice.currentData() if isinstance(choice, QComboBox) else ""
            rows.append((code, str(direction or "")))
        return rows

    def _append_row(self, code: str = "", direction: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        edit = QLineEdit(code, self.table)
        edit.textChanged.connect(lambda *_: self.changed.emit())
        self.table.setCellWidget(row, 0, edit)
        choice = QComboBox(self.table)
        choice.addItem("未映射", "")
        for value in self._DIRECTIONS:
            choice.addItem(value, value)
        index = choice.findData(direction)
        choice.setCurrentIndex(index if index >= 0 else 0)
        choice.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.table.setCellWidget(row, 1, choice)

    def add_row(self) -> None:
        self._append_row()
        self.changed.emit()

    def remove_rows(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        if rows:
            self.changed.emit()

    def values(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            edit = self.table.cellWidget(row, 0)
            choice = self.table.cellWidget(row, 1)
            code = edit.text().strip() if isinstance(edit, QLineEdit) else ""
            direction = choice.currentData() if isinstance(choice, QComboBox) else ""
            if not code and not direction:
                continue
            if not code or not direction:
                raise ValueError(f"阅读方向代码表第 {row + 1} 行需同时填写源代码和标准方向。")
            key = code.casefold()
            if key in {item.casefold() for item in result}:
                raise ValueError(f"阅读方向源代码重复：{code}")
            result[code] = str(direction)
        return result


class ImportSourcePanel(QWidget):
    changed = Signal(str)
    preview_loaded = Signal(str, object)
    preview_failed = Signal(str, str)

    def __init__(self, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self._generation = 0
        self._preview = pd.DataFrame()
        self._manifest: dict[str, Any] = {}
        self.path_edit = QLineEdit(self)
        self.path_edit.setReadOnly(True)
        self.sheet_combo = QComboBox(self)
        self.encoding_edit = QLineEdit("utf-8-sig", self)
        self.delimiter_combo = QComboBox(self)
        for label, value in (("逗号 ,", ","), ("分号 ;", ";"), ("制表符", "\t"), ("竖线 |", "|"), ("自动检测", None)):
            self.delimiter_combo.addItem(label, value)
        self.decimal_combo = QComboBox(self)
        self.decimal_combo.addItem("小数点 .", ".")
        self.decimal_combo.addItem("小数逗号 ,", ",")
        self.status = QLabel("未选择文件。选择文件不会自动确认字段映射或单位。", self)
        self.status.setWordWrap(True)
        self.table_model = DataFrameTableModel(parent=self)
        self.table_view = DataFrameTableView(self)
        self.table_view.setModel(self.table_model)
        self.table_view.setSortingEnabled(False)
        self.mapping = None if role == "performance" else TableMappingEditor(role, parent=self)
        self.performance_mapping = PerformanceMappingWidget(self) if role == "performance" else None
        self.mapping_host = self.performance_mapping or self.mapping
        self.aoi_overlap_combo = QComboBox(self)
        self.aoi_overlap_combo.addItem("未声明 / 暂不处理重叠", "unresolved")
        self.aoi_overlap_combo.addItem("排他 AOI（需显式优先级）", "exclusive")
        self.aoi_overlap_combo.addItem("保留多标签（需记录规则依据）", "multi_label")
        self.aoi_overlap_rule_ref = QLineEdit(self)
        self.aoi_legacy_name = QCheckBox("旧 AOI 没有名称：保留缺失状态（不以 ID 代填）", self)
        self.aoi_legacy_name.toggled.connect(lambda: self.changed.emit(self.role))
        self.aoi_overlap_rule_ref.setPlaceholderText("多标签概率/归属规则或研究方案依据引用")
        self.aoi_options = QGroupBox("AOI 重叠解释", self)
        aoi_form = QFormLayout(self.aoi_options)
        aoi_form.addRow("重叠策略", self.aoi_overlap_combo)
        aoi_form.addRow("规则依据", self.aoi_overlap_rule_ref)
        aoi_form.addRow(self.aoi_legacy_name)
        self.aoi_options.setVisible(role == "aois")
        self.aoi_overlap_combo.currentIndexChanged.connect(lambda: self.changed.emit(self.role))
        self.aoi_overlap_rule_ref.textChanged.connect(lambda: self.changed.emit(self.role))
        self.preview_button = QPushButton("预览前 100 行", self)
        self.template_button = QPushButton("参考模板", self)
        self.help_button = QPushButton("填写说明", self)
        self.remove_button = QPushButton("移除", self)
        browse = QPushButton("浏览…", self)
        browse.clicked.connect(self._choose_file)
        self.preview_button.clicked.connect(self.request_preview)
        self.remove_button.clicked.connect(self.clear)
        self.template_button.clicked.connect(self.export_template)
        self.help_button.clicked.connect(self.show_help)
        if self.mapping is not None:
            self.mapping.mapping_changed.connect(lambda: self.changed.emit(self.role))
        if self.performance_mapping is not None:
            self.performance_mapping.mapping_changed.connect(lambda: self.changed.emit(self.role))
        self.sheet_combo.currentIndexChanged.connect(lambda: self.changed.emit(self.role))

        source_box = QGroupBox("数据来源", self)
        source_form = QFormLayout(source_box)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        path_row.addWidget(self.preview_button)
        path_row.addWidget(self.template_button)
        path_row.addWidget(self.help_button)
        path_row.addWidget(self.remove_button)
        source_form.addRow("文件", path_row)
        source_form.addRow("工作表（XLSX）", self.sheet_combo)
        csv_row = QHBoxLayout()
        csv_row.addWidget(QLabel("编码", self))
        csv_row.addWidget(self.encoding_edit)
        csv_row.addWidget(QLabel("分隔符", self))
        csv_row.addWidget(self.delimiter_combo)
        csv_row.addWidget(QLabel("小数点", self))
        csv_row.addWidget(self.decimal_combo)
        source_form.addRow("CSV 读取", csv_row)
        note = QLabel("XLSX 只读取选定工作表的缓存值；不执行宏，也不重算公式。包含公式的工作簿由安全读取器拒绝。", self)
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(source_box)
        layout.addWidget(self.status)
        layout.addWidget(self.mapping_host, 3)
        if role == "aois":
            layout.addWidget(self.aoi_options)
        layout.addWidget(QLabel("只读预览", self))
        layout.addWidget(self.table_view, 2)
        layout.addWidget(note)

    @property
    def source_path(self) -> str:
        return self.path_edit.text().strip()

    def csv_options(self) -> CsvOptions:
        return CsvOptions(self.encoding_edit.text().strip(), self.delimiter_combo.currentData(), self.decimal_combo.currentData())

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择" + _ROLE_TITLES[self.role] + "文件", "", "CSV / XLSX (*.csv *.xlsx)")
        if path:
            self.path_edit.setText(path)
            self.clear_preview(keep_path=True)
            self.changed.emit(self.role)

    def request_preview(self) -> None:
        if not self.source_path:
            self.status.setText("先选择 CSV 或 XLSX 文件。")
            return
        self._generation += 1
        generation = self._generation
        self.status.setText("后台读取前 100 行。")
        self.preview_button.setEnabled(False)
        worker = _PreviewWorker(generation, self.source_path, self.role, self.sheet_combo.currentData(), self.csv_options())
        worker.signals.completed.connect(self._on_preview)
        worker.signals.failed.connect(self._on_preview_error)
        QThreadPool.globalInstance().start(worker)

    @Slot(int, object)
    def _on_preview(self, generation: int, payload: object) -> None:
        if generation != self._generation or not isinstance(payload, dict):
            return
        self.preview_button.setEnabled(True)
        sheets = payload.get("sheets", ())
        if sheets:
            current = self.sheet_combo.currentData()
            self.sheet_combo.blockSignals(True)
            self.sheet_combo.clear()
            self.sheet_combo.addItem("请选择工作表", None)
            for sheet in sheets:
                self.sheet_combo.addItem(str(sheet), str(sheet))
            selected = self.sheet_combo.findData(current)
            if selected >= 0:
                self.sheet_combo.setCurrentIndex(selected)
            elif len(sheets) == 1:
                self.sheet_combo.setCurrentIndex(1)
            self.sheet_combo.blockSignals(False)
        frame = payload.get("frame")
        if not isinstance(frame, pd.DataFrame):
            self.status.setText("此工作簿包含多个工作表，请先选择工作表再预览。")
            return
        self._preview = frame.copy(deep=True)
        self._manifest = dict(payload.get("manifest", {}))
        self.table_model.set_frame(self._preview)
        if self.mapping is not None:
            self.mapping.set_frame(self._preview)
        if self.performance_mapping is not None:
            self.performance_mapping.set_frame(self._preview)
        self.status.setText(
            f"{self._manifest.get('source_name', Path(self.source_path).name)} · {len(frame):,} 行预览"
            f" · 工作表 {self._manifest.get('sheet_name') or 'CSV'} · SHA-256 {str(self._manifest.get('sha256', ''))[:16]}"
        )
        self.preview_loaded.emit(self.role, self._preview)
        self.changed.emit(self.role)

    @Slot(int, str)
    def _on_preview_error(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self.preview_button.setEnabled(True)
        self.status.setText("预览失败：" + message)
        self.preview_failed.emit(self.role, message)

    def clear_preview(self, *, keep_path: bool = False) -> None:
        self._generation += 1
        self._preview = pd.DataFrame()
        self._manifest = {}
        self.table_model.set_frame(None)
        if self.mapping is not None:
            self.mapping.set_frame(None)
        if self.performance_mapping is not None:
            self.performance_mapping.set_frame(None)
        if not keep_path:
            self.path_edit.clear()
            self.sheet_combo.clear()
        self.status.setText("未选择文件。选择文件不会自动确认字段映射或单位。")

    def clear(self) -> None:
        self.clear_preview()
        self.changed.emit(self.role)

    def source_request(self, required_fields: set[str] | None = None, *, validate_mapping: bool = True) -> _SourceRequest:
        if not self.source_path:
            raise ValueError("尚未选择文件。")
        if self.mapping is not None:
            if required_fields is not None:
                self.mapping.set_required_fields(required_fields)
            errors = self.mapping.validation_messages()
            if errors and validate_mapping:
                raise ValueError("\n".join(errors))
            mapping, units = self.mapping.mapping(), self.mapping.units()
            performance = {}
        else:
            mapping, units = {}, {}
            performance = self.performance_mapping.value() if self.performance_mapping else {}
            if performance.get("performance_columns") and not performance.get("performance_join_keys"):
                raise ValueError("选择绩效结果列时，必须明确选择唯一连接键。")
            if self.performance_mapping:
                audit = self.performance_mapping.audit()
                if audit.get("status") == "review" and audit.get("duplicate_performance_keys"):
                    raise ValueError("绩效表连接键重复；请修正或选择正确的行层级/唯一键。")
        return _SourceRequest(self.role, self.source_path, self.sheet_combo.currentData(),
                              self.csv_options(), mapping, units, performance)

    def export_template(self) -> None:
        from pyeyedata.resource_paths import resource_path
        try:
            with resource_path("resource-index.json") as index_path:
                entry = json.loads(index_path.read_text(encoding="utf-8"))["roles"][self.role]
            choice, ok = QInputDialog.getItem(self, "参考模板", "选择资料", ["空白 CSV 模板", "合成示例 CSV", "XLSX（空白/示例/字段说明）"], 0, False)
            if not ok:
                return
            resource = entry["workbook"] if choice.startswith("XLSX") else entry["example" if choice.startswith("合成") else "empty_template"]["csv"]
            suffix = Path(resource).suffix
            path, _ = QFileDialog.getSaveFileName(self, "保存参考模板", Path(resource).name, f"{suffix.upper()} (*{suffix})")
            if not path:
                return
            if not path.lower().endswith(suffix):
                path += suffix
            with resource_path(resource) as source:
                Path(path).write_bytes(source.read_bytes())
            self.status.setText(f"已保存版本化模板：{path}。XLSX 显式选择 empty_template 或 example；fields 是说明页。")
        except Exception as exc:
            QMessageBox.warning(self, "参考模板不可用", str(exc))

    def show_help(self) -> None:
        from pyeyedata.resource_paths import resource_path
        try:
            with resource_path("templates/field-guide-zh-en.md") as path:
                guide = path.read_text(encoding="utf-8")
            with resource_path("templates/example-pages-zh-en.md") as path:
                examples = path.read_text(encoding="utf-8")
            box = QMessageBox(self)
            box.setWindowTitle(_ROLE_TITLES[self.role] + "字段与示例说明")
            box.setText("查看字段含义、必填条件和合成示例。XLSX 的 fields 为说明页，请勿作为数据导入。")
            box.setDetailedText(guide + "\n\n" + examples)
            save = box.addButton("另存说明", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() == save:
                destination, _ = QFileDialog.getSaveFileName(self, "保存模板说明", "模板说明.txt", "文本 (*.txt)")
                if destination:
                    Path(destination).write_text(guide + "\n\n" + examples, encoding="utf-8")
            return
        except Exception as exc:
            self.status.setText("版本化说明暂不可用：" + str(exc))
        texts = {
            "signals": "主要信号至少需要时间和明确参与者/会话 ID。左/右瞳孔、坐标、有效性列逐项映射。源时间单位、时间确认、坐标单位/坐标系是独立声明。",
            "segments": "段边界按 [start, end) 处理，segment_name 必填。范围、共享时钟、成员会话和重复/重叠策略必须明确；改动范围设置后请重新导入规范化。",
            "aois": "AOI 需明确 aoi_id、stimulus_id、shape 与几何字段。exclusive 重叠策略需由用户提供 priority；本页不按源行顺序猜优先级。",
            "performance": "选择分析行层级、每个语义角色、绩效结果列及单位，再显式选择唯一连接键。重复/空键会显示审计状态，不会自动合并或宣称独立样本。",
        }
        QMessageBox.information(self, _ROLE_TITLES[self.role] + "填写说明", texts[self.role])


class ScopeSettingsWidget(QWidget):
    settings_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = QComboBox(self)
        self.mode.addItem("参与者 / 会话", "participant_session")
        self.mode.addItem("共享互动（需明确同步）", "shared_interaction")
        self.overlap = QComboBox(self)
        for value in ("require_resolution", "keep_labels", "corrected", "reject"):
            self.overlap.addItem(value, value)
        self.duplicate = QComboBox(self)
        for value in ("require_resolution", "deduplicate"):
            self.duplicate.addItem(value, value)
        self.timebase_id = QLineEdit(self)
        self.timebase_id.setPlaceholderText("共享 segments 必填；与同步声明同一 timebase")
        self.interaction_id = QLineEdit(self)
        self.interaction_id.setPlaceholderText("数据范围互动 ID（共享互动时必填）")
        self.dataset_scope_ref = QLineEdit(self)
        self.dataset_scope_ref.setPlaceholderText("成员会话范围依据引用")
        self.members = QTableWidget(0, 6, self)
        self.members.setHorizontalHeaderLabels(["参与者 ID", "会话 ID", "互动 ID", "timebase_id", "起点秒（可选）", "终点秒（可选）"])
        self.members.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("添加成员会话", self)
        remove = QPushButton("移除选中", self)
        add.clicked.connect(self.add_member)
        remove.clicked.connect(self.remove_member)
        self.warning = QLabel("范围、同步和 timebase 会在 normalize_segments 阶段生效；修改后必须重新规范化 segments。", self)
        self.warning.setWordWrap(True)
        form = QFormLayout()
        form.addRow("段作用域", self.mode)
        form.addRow("重叠策略", self.overlap)
        form.addRow("重复 ID 策略", self.duplicate)
        form.addRow("timebase_id", self.timebase_id)
        form.addRow("互动 ID", self.interaction_id)
        form.addRow("成员范围依据", self.dataset_scope_ref)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("共享成员会话映射（每行必须明确）", self))
        layout.addWidget(self.members)
        layout.addLayout(row)
        layout.addWidget(self.warning)
        self.mode.currentIndexChanged.connect(self._scope_changed)
        self.overlap.currentIndexChanged.connect(self._scope_changed)
        self.duplicate.currentIndexChanged.connect(self._scope_changed)
        self.timebase_id.textChanged.connect(self._scope_changed)
        self.interaction_id.textChanged.connect(self._scope_changed)
        self.dataset_scope_ref.textChanged.connect(self._scope_changed)
        self.members.itemChanged.connect(self._scope_changed)

    def _scope_changed(self, *_: Any) -> None:
        self.settings_changed.emit()

    def add_member(self) -> None:
        row = self.members.rowCount()
        self.members.insertRow(row)
        for col in range(self.members.columnCount()):
            self.members.setItem(row, col, QTableWidgetItem(""))
        self.settings_changed.emit()

    def remove_member(self) -> None:
        rows = sorted({index.row() for index in self.members.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            self.members.removeRow(row)
        self.settings_changed.emit()

    def build(self, *, synchronized: bool) -> tuple[SegmentScopeConfig, tuple[MemberSessionMapping, ...], Mapping[str, Any] | None]:
        mode = str(self.mode.currentData())
        timebase = self.timebase_id.text().strip() or None
        config = SegmentScopeConfig(
            mode=mode,
            overlap_policy=str(self.overlap.currentData()),
            duplicate_policy=str(self.duplicate.currentData()),
            timebase_id=timebase,
            synchronized=bool(synchronized),
        )
        members: list[MemberSessionMapping] = []
        dataset_members: list[dict[str, str]] = []
        if mode == "shared_interaction":
            interaction = self.interaction_id.text().strip()
            if not interaction:
                raise ValueError("共享互动作用域需明确 interaction_id。")
            if not timebase:
                raise ValueError("共享互动作用域需明确 timebase_id。")
            for row in range(self.members.rowCount()):
                cells = [self.members.item(row, col).text().strip() if self.members.item(row, col) else ""
                         for col in range(self.members.columnCount())]
                if not any(cells):
                    continue
                if not all(cells[:4]):
                    raise ValueError(f"成员映射第 {row + 1} 行需要 subject_id、session_id、interaction_id、timebase_id。")
                start = float(cells[4]) if cells[4] else None
                end = float(cells[5]) if cells[5] else None
                members.append(MemberSessionMapping(cells[0], cells[1], cells[2], cells[3], start, end))
                dataset_members.append({"subject_id": cells[0], "session_id": cells[1],
                                        "interaction_id": cells[2], "timebase_id": cells[3]})
            if not members:
                raise ValueError("共享互动作用域需至少一行成员会话映射。")
        dataset_scope = None
        if dataset_members:
            dataset_scope = {"members": dataset_members, "timebase_id": timebase,
                             "interaction_id": self.interaction_id.text().strip(),
                             "source_ref": self.dataset_scope_ref.text().strip() or None,
                             "synchronized": bool(synchronized)}
        return config, tuple(members), dataset_scope


class ImportPage(QWidget):
    """B02 entry page. Emits a frozen DatasetRevision only after public normalization."""

    import_completed = Signal(object, object)
    import_failed = Signal(str)
    import_progress = Signal(int, str)
    dataset_invalidated = Signal(str)
    configuration_changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, context_editor: ContextEditor | None = None) -> None:
        super().__init__(parent)
        self._worker: _NormalizeWorker | None = None
        self._last_revision: DatasetRevision | None = None
        self.sources = {role: ImportSourcePanel(role, self) for role in _ROLE_TITLES}
        self.coordinate = CoordinateSettingsWidget(self)
        self._validity_codes = _ValidCodeTable(self)
        self._reading_directions = _ReadingDirectionTable(self)
        self._coordinate_layout = QTabWidget(self)
        self._coordinate_layout.addTab(self.coordinate, "时间 / 坐标 / 设备元数据")
        self._coordinate_layout.addTab(self._validity_codes, "有效性码表")
        self._coordinate_layout.addTab(self._reading_directions, "阅读方向代码")
        self.scope = ScopeSettingsWidget(self)
        self.context_editor = context_editor or ContextEditor(self)
        self.source_tabs = QTabWidget(self)
        for role, panel in self.sources.items():
            self.source_tabs.addTab(panel, _ROLE_TITLES[role])
        self.source_tabs.addTab(self.context_editor, "目标、参考与事件资料")
        self.import_button = QPushButton("导入并规范化", self)
        self.status = QLabel("先选择主要信号 CSV/XLSX，再按需添加段、AOI 和绩效表。", self)
        self.status.setWordWrap(True)
        self.import_button.clicked.connect(self.start_import)
        for panel in self.sources.values():
            panel.changed.connect(self._source_changed)
            panel.preview_loaded.connect(self._preview_ready)
            panel.preview_failed.connect(self._preview_error)
        self.coordinate.settings_changed.connect(self._semantics_changed)
        self._validity_codes.changed.connect(
            lambda: self._normalization_configuration_changed("validity_code_mapping_changed", "有效性码表已变化")
        )
        self._reading_directions.changed.connect(
            lambda: self._normalization_configuration_changed("reading_direction_mapping_changed", "阅读方向映射已变化")
        )
        self.scope.settings_changed.connect(self._scope_changed)
        self.context_editor.changed.connect(self.configuration_changed.emit)
        self.context_editor.import_failed.connect(self.import_failed.emit)
        layout = QVBoxLayout(self)
        intro = QLabel("四类文件分别导入；来源、单位、ID 与字段映射都需明确。原始信号在标准化前只读，未知/未提供能力保持未知。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(self._coordinate_layout, 2)
        layout.addWidget(self.scope, 1)
        layout.addWidget(self.source_tabs, 5)
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)

    def _source_changed(self, role: str) -> None:
        if role == "aois" and self._last_revision is not None:
            # Keep the valid primary input available while the optional AOI
            # source/mapping is a draft. Previous geometry cannot masquerade as
            # the newly edited AOI definition.
            dataset = self._last_revision.dataset_copy()
            dataset.aois = pd.DataFrame()
            dataset.metadata["aoi_import_status"] = "pending_draft"
            dataset.metadata["aoi_import_issues"] = [{"code": "aoi_draft_requires_validation",
                "message": "AOI 来源或映射已变化，需要重新导入验证。",
                "source_file": self.sources["aois"].source_path}]
            revision = DatasetRevision.create(dataset, raw_sources=self._last_revision.raw_source_bytes())
            self._last_revision = revision
            self.import_completed.emit(revision, dataset.signals.head(500))
            self.sources["aois"].status.setText("AOI 草稿待验证；主要信号仍可用于不依赖 AOI 的分析。")
            self.configuration_changed.emit()
            return
        self.configuration_changed.emit()
        if self._last_revision is not None:
            self.status.setText(f"{_ROLE_TITLES[role]} 来源或映射已变化；请重新导入，旧数据修订已失效。")
            self.dataset_invalidated.emit("source_or_mapping_changed_requires_renormalization")
        if role == "signals":
            panel = self.sources["signals"]
            if panel.mapping is not None and not panel._preview.empty:
                code_widget = self._validity_codes
                mapped = panel.mapping.mapping()
                code_widget.configure_validity_fields(mapped, panel._preview)
                direction_source = mapped.get("reading_direction")
                self._reading_directions.configure(direction_source, panel._preview)
        if role == "performance":
            self._refresh_performance_audit()

    def _preview_ready(self, role: str, frame: object) -> None:
        if role == "signals" and isinstance(frame, pd.DataFrame) and self.sources[role].mapping is not None:
            mapped = self.sources[role].mapping.mapping()
            self._validity_codes.configure_validity_fields(mapped, frame)
            self._reading_directions.configure(mapped.get("reading_direction"), frame)
        if role == "performance":
            self._refresh_performance_audit()

    def _preview_error(self, role: str, message: str) -> None:
        self.status.setText(f"{_ROLE_TITLES.get(role, role)}：{message}")

    def _refresh_performance_audit(self) -> None:
        perf = self.sources["performance"].performance_mapping
        signal_frame = self.sources["signals"]._preview
        if perf is not None:
            summary = perf.audit(signal_frame)
            if summary.get("duplicate_performance_keys") is not None:
                self.sources["performance"].status.setText(
                    f"绩效审计：空键 {summary.get('empty_performance_keys')}；重复连接键涉及行 "
                    f"{summary.get('duplicate_performance_keys')}；未匹配行 {summary.get('unmatched_performance_rows')}；"
                    "未自动 join。"
                )

    def _semantics_changed(self) -> None:
        self._normalization_configuration_changed(
            "time_or_coordinate_declaration_changed", "时间、坐标或设备声明已变化"
        )

    def _normalization_configuration_changed(self, reason: str, description: str) -> None:
        self.configuration_changed.emit()
        if self._last_revision is not None:
            self.status.setText(f"{description}；请重新导入并规范化，现有数据修订仍绑定旧声明。")
            self.dataset_invalidated.emit(reason)

    def _scope_changed(self) -> None:
        self.configuration_changed.emit()
        if self._last_revision is not None and not self.sources["segments"].source_path:
            self.status.setText("作用域/同步/timebase 已变化；如需分析共享段，请重新导入并规范化 segments。")
        elif self._last_revision is not None:
            self.status.setText("作用域、同步或 timebase 已变化；必须重新导入以重新规范化 segments。")
            self.dataset_invalidated.emit("segment_scope_changed_requires_renormalization")

    def _required_for_role(self, role: str, subject_id: str | None, session_id: str | None) -> set[str] | None:
        if role == "signals":
            required = set(SIGNAL_REQUIRED_COLUMNS)
            # The public normalizer derives these columns from the selected source
            # time field and physical source rows; they are outputs, not mappings.
            required.difference_update({"time_raw", "original_index"})
            if subject_id:
                required.discard("subject_id")
            if session_id:
                required.discard("session_id")
            return required
        if role == "segments":
            required = {"segment_name", "start_s", "end_s"}
            if self.scope.mode.currentData() == "participant_session":
                if not subject_id:
                    required.add("subject_id")
                if not session_id:
                    required.add("session_id")
            else:
                required.add("interaction_id")
            return required
        if role == "aois":
            return {"aoi_id", "stimulus_id", "shape"} | (set() if self.sources["aois"].aoi_legacy_name.isChecked() else {"aoi_name"})
        return None

    def _build_request(self) -> _ImportRequest:
        subject_id = self.coordinate.subject_id.text().strip() or None
        session_id = self.coordinate.session_id.text().strip() or None
        coordinate = self.coordinate.coordinate_metadata()
        synchronization_method = self.coordinate.sync_method.text().strip() or None
        synchronization_source_refs = tuple(
            value.strip() for value in self.coordinate.sync_refs.text().split(";") if value.strip()
        )
        if coordinate.sync_confirmed and (not synchronization_method or not synchronization_source_refs):
            raise ValueError("确认跨时钟同步前，需分别填写同步方法和依据引用。")
        if coordinate.time_unit == "unknown":
            raise ValueError("请选择明确的源时间单位；time_confirmed 是否勾选会另外记录。")
        if self.sources["segments"].source_path:
            scope, member_map, dataset_scope = self.scope.build(synchronized=coordinate.sync_confirmed)
        else:
            # Scope/member controls have no normalization effect without a segment source.
            scope, member_map, dataset_scope = SegmentScopeConfig(), (), None
        sources: list[_SourceRequest] = []
        for role in ("signals", "segments", "aois", "performance"):
            source_panel = self.sources[role]
            if not source_panel.source_path:
                if role == "signals":
                    raise ValueError("必须选择主要信号文件。")
                continue
            required = self._required_for_role(role, subject_id, session_id)
            sources.append(source_panel.source_request(required, validate_mapping=role != "aois"))
        signal_source = next(item for item in sources if item.role == "signals")
        pupil_units_by_eye: dict[str, str] = {}
        for eye in ("left", "right"):
            canonical = f"pupil_{eye}"
            if canonical not in signal_source.column_map:
                continue
            widget_value = getattr(self.coordinate, f"pupil_{eye}_unit").text().strip()
            mapping_value = signal_source.units.get(canonical, "").strip()
            if widget_value and mapping_value and widget_value.casefold() != mapping_value.casefold():
                raise ValueError(f"{eye} pupil 单位声明不一致，请统一坐标面板和映射表。")
            value = widget_value or mapping_value
            if not value:
                raise ValueError(f"已映射 {canonical}，请明确其单位（mm、area、device 或 unknown）。")
            pupil_units_by_eye[eye] = value
        validity_values = self._validity_codes.values()
        reading_direction_values = self._reading_directions.values()
        aoi_policy = self._aoi_overlap_policy()
        aoi_rule_ref = self._aoi_overlap_rule_ref()
        if aoi_policy == "multi_label" and not aoi_rule_ref:
            raise ValueError("多标签 AOI 需记录概率/归属规则依据；该引用只作审计，不会改变 A 算法。")
        return _ImportRequest(tuple(sources), coordinate, self.coordinate.time_confirmed.isChecked(),
                              subject_id, session_id, validity_values, reading_direction_values,
                              synchronization_method, synchronization_source_refs, pupil_units_by_eye,
                              scope, member_map, dataset_scope, aoi_policy, aoi_rule_ref,
                              "allow_legacy_missing" if self.sources["aois"].aoi_legacy_name.isChecked() else "require_source")

    def _aoi_overlap_policy(self) -> str | None:
        panel = self.sources["aois"]
        if (panel.mapping is None or not panel.source_path
                or panel.aoi_overlap_combo.currentData() == "unresolved"):
            return None
        return str(panel.aoi_overlap_combo.currentData())

    def _aoi_overlap_rule_ref(self) -> str | None:
        panel = self.sources["aois"]
        if not panel.source_path:
            return None
        return panel.aoi_overlap_rule_ref.text().strip() or None

    def start_import(self) -> None:
        if self._worker is not None:
            self.status.setText("已有导入任务运行中。")
            return
        try:
            request = self._build_request()
        except Exception as exc:
            self.status.setText("导入配置尚未完成：" + str(exc))
            return
        self._worker = _NormalizeWorker(request)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.completed.connect(self._on_import_completed)
        self._worker.signals.failed.connect(self._on_import_failed)
        self.import_button.setEnabled(False)
        self.status.setText("后台读取并调用公开 normalize_* 接口；源文件不会在 GUI 线程读取。")
        QThreadPool.globalInstance().start(self._worker)

    @Slot(int, str)
    def _on_progress(self, percent: int, message: str) -> None:
        self.import_progress.emit(percent, message)
        self.status.setText(f"{percent}% · {message}")

    @Slot(object, object)
    def _on_import_completed(self, revision: object, preview: object) -> None:
        self._worker = None
        self.import_button.setEnabled(True)
        if not isinstance(revision, DatasetRevision) or not isinstance(preview, pd.DataFrame):
            self.status.setText("导入 worker 返回内容无效。")
            return
        self._last_revision = revision
        self.status.setText(
            f"数据修订 {revision.revision_id} 已就绪 · {len(preview):,} 行预览 · "
            f"来源/映射/单位及原始字节哈希已绑定 · 设备验证仍按用户声明记录"
        )
        self.import_completed.emit(revision, preview)
        metadata = revision.dataset_copy().metadata
        if metadata.get("aoi_import_status") == "invalid_draft":
            issues = metadata.get("aoi_import_issues", [])
            lines = [f"{item.get('source_file')} / {item.get('sheet')} / 源行 {item.get('source_row')} / {item.get('canonical_field')}：{item.get('code')} — {item.get('message')}" for item in issues]
            self.sources["aois"].status.setText("AOI 草稿无效；主要信号已保留，只有依赖 AOI 的方法不可用。\n" + "\n".join(lines))
            self.status.setText("主要信号已导入；AOI 草稿未纳入有效几何，请查看 ROI/AOI 页的具体来源问题。")

    @Slot(str)
    def _on_import_failed(self, message: str) -> None:
        self._worker = None
        self.import_button.setEnabled(True)
        self.status.setText("导入失败：" + message)
        self.import_failed.emit(message)

    def analysis_metadata(self) -> dict[str, Any]:
        if self._last_revision is None:
            return {}
        data = self._last_revision.dataset_copy()
        return dict(data.metadata.get("analysis_metadata", {}))

    def context_tables(self) -> dict[str, pd.DataFrame]:
        return self.context_editor.tables()

    def context_source_manifest(self) -> list[dict[str, Any]]:
        return self.context_editor.source_manifest()

    def context_raw_source_bytes(self) -> dict[str, bytes]:
        return self.context_editor.raw_source_bytes()

    def context_metadata(self) -> dict[str, Any]:
        return self.context_editor.analysis_metadata()

    def context_individual_parameters(self) -> dict[str, Any]:
        return self.context_editor.individual_parameters()

    def performance_mapping(self) -> dict[str, Any]:
        return self.sources["performance"].performance_mapping.value() if self.sources["performance"].performance_mapping else {}
