"""Typed, provenance-preserving auxiliary context tables for the A pipeline."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pyeyedata.io import CsvOptions, DataImportError, MAX_IMPORT_BYTES, read_table

from .mapping import FieldSpec, TableMappingEditor, context_fields
from .widgets import DataFrameTableModel, DataFrameTableView


_A_CONTEXT_KINDS = (
    ("external_events", "厂商/外部事件"),
    ("target_movement_intervals", "目标移动区间"),
    ("eyelid_intervals", "眼睑区间"),
    ("reference_fixations", "参考注视"),
    ("reference_trajectory", "参考轨迹"),
    ("reading_annotations", "阅读标注（仅保存追溯）"),
)


class _ReadSignals(QObject):
    completed = Signal(int, object)
    failed = Signal(int, str)


class _ReadWorker(QRunnable):
    def __init__(self, generation: int, path: str, sheet: str | None, csv_options: CsvOptions, *, preview: bool) -> None:
        super().__init__()
        self.generation = generation
        self.path = path
        self.sheet = sheet
        self.csv_options = csv_options
        self.preview = preview
        self.signals = _ReadSignals()

    @Slot()
    def run(self) -> None:
        try:
            if Path(self.path).suffix.lower() == ".xlsx" and not self.sheet:
                first = read_table(self.path, role="signals", csv_options=self.csv_options,
                                   preview_rows=100 if self.preview else 1)
                result: dict[str, Any] = {"frame": first.frame, "sha256": first.sha256,
                                          "source_name": first.source_name, "sheet_name": first.sheet_name,
                                          "source_rows": first.source_rows, "byte_size": first.byte_size,
                                          "manifest": first.manifest_entry(), "sheets": (first.sheet_name,)}
            else:
                imported = read_table(self.path, role="signals", sheet_name=self.sheet,
                                      csv_options=self.csv_options, preview_rows=100 if self.preview else None)
                result = {"frame": imported.frame, "sha256": imported.sha256,
                          "source_name": imported.source_name, "sheet_name": imported.sheet_name,
                          "source_rows": imported.source_rows, "byte_size": imported.byte_size,
                          "manifest": imported.manifest_entry(), "sheets": (imported.sheet_name,) if imported.sheet_name else ()}
            self.signals.completed.emit(self.generation, result)
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


class ContextSourcePanel(QWidget):
    """One named A context table with safe import, explicit mapping and source units."""

    changed = Signal()
    loaded = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, kind: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._generation = 0
        self._frame = pd.DataFrame()
        self._preview = pd.DataFrame()
        self._manifest: dict[str, Any] = {}
        self._raw_bytes = b""
        self._dirty = False
        self._loading = False
        self._mapping = TableMappingEditor(kind, field_specs=context_fields(kind), parent=self)
        self.path_edit = QLineEdit(self)
        self.path_edit.setReadOnly(True)
        self.sheet_combo = QComboBox(self)
        self.sheet_combo.setMinimumWidth(150)
        self.encoding_edit = QLineEdit("utf-8-sig", self)
        self.delimiter_combo = QComboBox(self)
        self.delimiter_combo.addItem("逗号 ,", ",")
        self.delimiter_combo.addItem("分号 ;", ";")
        self.delimiter_combo.addItem("制表符", "\t")
        self.delimiter_combo.addItem("竖线 |", "|")
        self.delimiter_combo.addItem("自动检测", None)
        self.decimal_combo = QComboBox(self)
        self.decimal_combo.addItem("小数点 .", ".")
        self.decimal_combo.addItem("小数逗号 ,", ",")
        self.preview_button = QPushButton("预览前 100 行", self)
        self.load_button = QPushButton("载入完整表", self)
        self.remove_button = QPushButton("移除", self)
        self.status = QLabel("未选择文件", self)
        self.status.setWordWrap(True)
        self.table_model = DataFrameTableModel(parent=self)
        self.table_view = DataFrameTableView(self)
        self.table_view.setModel(self.table_model)
        self.table_view.setSortingEnabled(False)

        self.time_unit = QComboBox(self)
        for label, value in (("请选择 / 未确认", ""), ("秒 s", "s"), ("毫秒 ms", "ms"),
                             ("帧 frame", "frame"), ("日期时间 datetime", "datetime"), ("未知", "unknown")):
            self.time_unit.addItem(label, value)
        self.time_origin = QLineEdit(self)
        self.time_origin.setPlaceholderText("datetime：明确基准时刻；frame：起点秒值，留空表示 0")
        self.timezone = QLineEdit(self)
        self.timezone.setPlaceholderText("datetime 时填写 IANA 时区，例如 Asia/Shanghai")
        self.frame_rate = QDoubleSpinBox(self)
        self.frame_rate.setRange(0.0, 1000000.0)
        self.frame_rate.setDecimals(6)
        self.frame_rate.setSpecialValueText("未提供")
        self.frame_rate.setValue(0.0)
        self.reference_id = QLineEdit(self)
        self.reference_id.setPlaceholderText("参考轨迹/注视表 ID（如适用）")
        self.coordinate_unit = QLineEdit(self)
        self.coordinate_unit.setPlaceholderText("例如 px、deg、mm 或 unknown")
        self.coordinate_frame = QLineEdit(self)
        self.coordinate_frame.setPlaceholderText("例如 display-1、screen-normalized 或 unknown")
        self.source_refs = QLineEdit(self)
        self.source_refs.setPlaceholderText("来源引用；多个引用用分号隔开")

        browse = QPushButton("浏览…", self)
        browse.clicked.connect(self._choose_file)
        self.preview_button.clicked.connect(lambda: self._queue_read(preview=True))
        self.load_button.clicked.connect(lambda: self._queue_read(preview=False))
        self.remove_button.clicked.connect(self.clear)
        self.sheet_combo.currentIndexChanged.connect(self._semantic_setting_changed)
        self.time_unit.currentIndexChanged.connect(self._semantic_setting_changed)
        for edit in (self.time_origin, self.timezone, self.reference_id, self.coordinate_unit,
                     self.coordinate_frame, self.source_refs):
            edit.textChanged.connect(self._semantic_setting_changed)
        self.frame_rate.valueChanged.connect(self._semantic_setting_changed)
        self._mapping.mapping_changed.connect(self._semantic_setting_changed)

        source_box = QGroupBox("来源与 CSV/XLSX 选项", self)
        source_form = QFormLayout(source_box)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        path_row.addWidget(self.preview_button)
        path_row.addWidget(self.load_button)
        path_row.addWidget(self.remove_button)
        self.help_button = QPushButton("填写示例", self)
        self.help_button.clicked.connect(self._show_input_help)
        path_row.addWidget(self.help_button)
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

        semantics = QGroupBox("明确时间和参考坐标语义", self)
        semantics_form = QFormLayout(semantics)
        semantics_form.addRow("源时间单位", self.time_unit)
        semantics_form.addRow("时间原点 / 起始秒", self.time_origin)
        semantics_form.addRow("时区", self.timezone)
        semantics_form.addRow("帧率（Hz）", self.frame_rate)
        semantics_form.addRow("参考数据 ID", self.reference_id)
        semantics_form.addRow("坐标单位", self.coordinate_unit)
        semantics_form.addRow("坐标系 / frame", self.coordinate_frame)
        semantics_form.addRow("来源引用", self.source_refs)

        note = QLabel(
            "这些辅助资料只在所选方法需要时添加。先对应源列并确认时间和单位，再点“载入完整表”。"
            "阅读标注只作追溯，不会自动计算阅读指标。Excel 公式请先计算并保存；本软件只读取已保存的值。", self
        )
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(title, self))
        layout.addWidget(source_box)
        layout.addWidget(semantics)
        layout.addWidget(self.status)
        layout.addWidget(self._mapping, 2)
        layout.addWidget(QLabel("来源值预览（只读）", self))
        layout.addWidget(self.table_view, 1)
        layout.addWidget(note)

    @property
    def source_path(self) -> str:
        return self.path_edit.text().strip()

    def _show_input_help(self) -> None:
        from .help_center import show_help
        show_help(self, role=self.kind)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择辅助数据", "", "CSV / XLSX (*.csv *.xlsx)")
        if path:
            self.path_edit.setText(path)
            self.clear_loaded(keep_path=True)
            self.changed.emit()

    def _semantic_setting_changed(self, *_: Any) -> None:
        if self._manifest.get("loaded_full"):
            self._dirty = True
            self.status.setText("映射或语义已修改；请重新载入完整表后再用于分析。")
        if self._loading:
            self._generation += 1
            self._loading = False
            self.preview_button.setEnabled(True)
            self.load_button.setEnabled(True)
            self.status.setText("读取期间映射或语义发生变化；已丢弃旧结果，请重新预览或载入。")
        self.changed.emit()

    def _csv_options(self) -> CsvOptions:
        return CsvOptions(self.encoding_edit.text().strip(), self.delimiter_combo.currentData(), self.decimal_combo.currentData())

    def _queue_read(self, *, preview: bool) -> None:
        path = self.source_path
        if not path:
            self.status.setText("先选择 CSV 或 XLSX 文件。")
            return
        self._generation += 1
        generation = self._generation
        self.status.setText("后台读取中；表格只读。")
        self._loading = True
        self.preview_button.setEnabled(False)
        self.load_button.setEnabled(False)
        if preview:
            worker = _ReadWorker(generation, path, self.sheet_combo.currentData(), self._csv_options(), preview=True)
            worker.signals.completed.connect(self._on_read_completed)
            worker.signals.failed.connect(self._on_read_failed)
        else:
            mapping = self._mapping.mapping()
            missing = [spec.label for spec in context_fields(self.kind) if spec.required and spec.name not in mapping]
            if missing:
                self.preview_button.setEnabled(True)
                self.load_button.setEnabled(True)
                self.status.setText("先完成必需字段映射：" + "、".join(missing))
                return
            time_names = [name for name in mapping if name == "time_s" or name.endswith("_s")]
            source_unit = str(self.time_unit.currentData() or "")
            if time_names and not source_unit:
                self.preview_button.setEnabled(True)
                self.load_button.setEnabled(True)
                self.status.setText("本 context 表含时间列，请明确源单位后再载入。")
                return
            config = {
                "mapping": mapping,
                "source_time_unit": source_unit,
                "time_origin": self.time_origin.text().strip(),
                "timezone": self.timezone.text().strip(),
                "frame_rate_hz": self.frame_rate.value(),
                "reference_id": self.reference_id.text().strip(),
                "coordinate_unit": self.coordinate_unit.text().strip(),
                "coordinate_frame": self.coordinate_frame.text().strip(),
                "source_refs": [value.strip() for value in self.source_refs.text().split(";") if value.strip()],
            }
            worker = _ContextConversionWorker(generation, path, self.sheet_combo.currentData(),
                                              self._csv_options(), self.kind, config)
            worker.signals.completed.connect(self._on_full_completed)
            worker.signals.failed.connect(self._on_read_failed)
        QThreadPool.globalInstance().start(worker)

    @Slot(int, object)
    def _on_read_completed(self, generation: int, payload: object) -> None:
        if generation != self._generation or not isinstance(payload, dict):
            return
        self._loading = False
        self.preview_button.setEnabled(True)
        self.load_button.setEnabled(True)
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
            self.status.setText("工作簿含多个工作表，请明确选择一个后重新预览或导入。")
            return
        self._preview = frame.copy(deep=True)
        self.table_model.set_frame(self._preview)
        self._mapping.set_frame(frame)
        self._manifest = {key: value for key, value in payload.items() if key != "frame"}
        self._manifest["preview_only"] = True
        self.status.setText(
            f"{self._manifest.get('source_name', Path(self.source_path).name)} · {len(frame):,} 行预览"
            f" · SHA-256 {str(self._manifest.get('sha256', ''))[:16]}；点击‘载入完整表’保存整表"
        )
        if self.kind == "reading_annotations":
            self.status.setText(self.status.text() + " · 仅存档，不进入 A 计算")
        self.changed.emit()

    @Slot(int, object)
    def _on_full_completed(self, generation: int, payload: object) -> None:
        if generation != self._generation or not isinstance(payload, dict):
            return
        self._loading = False
        self.preview_button.setEnabled(True)
        self.load_button.setEnabled(True)
        frame, preview, manifest = payload.get("frame"), payload.get("preview"), payload.get("manifest")
        if not isinstance(frame, pd.DataFrame) or not isinstance(preview, pd.DataFrame) or not isinstance(manifest, dict):
            self.status.setText("完整表导入器返回了无效内容。")
            return
        self._frame = frame.copy(deep=True)
        self._preview = preview.copy(deep=True)
        self._manifest = dict(manifest)
        raw_bytes = payload.get("raw_bytes")
        self._raw_bytes = bytes(raw_bytes) if isinstance(raw_bytes, (bytes, bytearray)) else b""
        self.table_model.set_frame(self._preview)
        self._mapping.set_frame(self._preview)
        # set_frame rebuilds the mapping controls and may emit semantic-change signals.
        # The loaded table already reflects the frozen mapping captured by its worker.
        self._dirty = False
        self.status.setText(
            f"已载入 {len(self._frame):,} 行 context 表 · SHA-256 {str(self._manifest.get('source_sha256', ''))[:16]}"
            f" · kind={self.kind} · 时间列输出单位 {self._manifest.get('normalized_time_unit') or '未应用'}"
        )
        if self.kind == "reading_annotations":
            self.status.setText(self.status.text() + " · 仅存档，不进入 A 计算")
        self.loaded.emit(self.kind, self._frame.copy(deep=True))
        self.changed.emit()

    @Slot(int, str)
    def _on_read_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self.preview_button.setEnabled(True)
        self.load_button.setEnabled(True)
        self.status.setText(f"读取失败：{message}")
        self.failed.emit(self.kind, message)

    @staticmethod
    def _unit_conversion(values: pd.Series, unit: str, *, origin: str, timezone: str, frame_rate: float,
                         source_rows: tuple[int, ...] = (), duration: bool = False) -> pd.Series:
        numeric = pd.to_numeric(values, errors="coerce")
        if unit == "s":
            converted = numeric
        elif unit == "ms":
            converted = numeric / 1000.0
        elif unit == "frame":
            if frame_rate <= 0:
                raise ValueError("帧单位必须填写正帧率。")
            origin_s = 0.0 if duration else (float(origin) if origin else 0.0)
            converted = numeric / frame_rate + origin_s
        elif unit == "datetime":
            if duration:
                raise ValueError("duration_s 是时长，不能按日期时间和原点转换；请选择 s、ms 或 frame。")
            if not origin or not timezone:
                raise ValueError("日期时间需明确时间原点和时区。")
            try:
                parsed = pd.to_datetime(values, errors="coerce")
                origin_stamp = pd.Timestamp(origin)
                if parsed.dt.tz is None:
                    parsed = parsed.dt.tz_localize(timezone, ambiguous="NaT", nonexistent="NaT")
                else:
                    parsed = parsed.dt.tz_convert(timezone)
                if origin_stamp.tzinfo is None:
                    origin_stamp = origin_stamp.tz_localize(timezone, ambiguous="NaT", nonexistent="NaT")
                else:
                    origin_stamp = origin_stamp.tz_convert(timezone)
                converted = (parsed - origin_stamp).dt.total_seconds()
            except Exception as exc:
                raise ValueError(f"日期时间无法按明确时区解析：{exc}") from exc
        else:
            raise ValueError("context 时间列需明确为 s、ms、frame 或 datetime；未知单位不能规范化。")
        bad = converted.isna() | ~np.isfinite(pd.to_numeric(converted, errors="coerce").astype(float))
        if bad.any():
            positions = [source_rows[position] if position < len(source_rows) else position + 2
                         for position in np.flatnonzero(bad.to_numpy(dtype=bool))[:8]]
            raise ValueError("时间列含无法转换的值，源行号：" + ", ".join(map(str, positions)))
        return converted.astype(float)

    @staticmethod
    def _materialize_context(kind: str, raw: pd.DataFrame, imported: dict[str, Any], config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
        mapping = dict(config["mapping"])
        source_rows = tuple(imported.get("source_rows") or ())
        labels = {spec.name: spec.label for spec in context_fields(kind)}
        required_missing = [spec.name for spec in context_fields(kind)
                            if spec.required and spec.name not in mapping]
        if required_missing:
            raise ValueError("必需的语义字段未映射：" + ", ".join(labels.get(name, name) for name in required_missing))
        output = raw.copy(deep=True)
        for source in raw.columns:
            raw_name = "raw_source__" + str(source)
            if raw_name not in output.columns:
                output[raw_name] = raw[source].copy(deep=True)
        time_names = [name for name in mapping if name == "time_s" or name.endswith("_s")]
        source_unit = str(config.get("source_time_unit", ""))
        for name in time_names:
            source_column = mapping[name]
            if name in {"start_s", "end_s", "time_s", "duration_s"}:
                output[name] = ContextSourcePanel._unit_conversion(raw[source_column], source_unit,
                                                    origin=str(config.get("time_origin", "")),
                                                    timezone=str(config.get("timezone", "")),
                                                    frame_rate=float(config.get("frame_rate_hz", 0)),
                                                    source_rows=source_rows,
                                                    duration=name == "duration_s")
            else:
                output[name] = raw[source_column]
        for canonical, source in mapping.items():
            if canonical not in time_names:
                output[canonical] = raw[source].copy(deep=True)
        output["time_unit"] = "s" if time_names else source_unit or "unknown"
        if source_rows and len(source_rows) == len(output):
            output["source_row"] = list(source_rows)
        manifest = {
            "context_source_kind": kind,
            "source_name": imported.get("source_name", ""),
            "source_sha256": imported.get("sha256"),
            "source_sheet": imported.get("sheet_name"),
            "byte_size": imported.get("byte_size"),
            "row_count": int(len(raw)),
            "source_time_unit": source_unit or None,
            "normalized_time_unit": "s" if time_names else None,
            "time_origin": config.get("time_origin") or None,
            "timezone": config.get("timezone") or None,
            "frame_rate_hz": config.get("frame_rate_hz") or None,
            "column_mapping": mapping,
            "reference_id": config.get("reference_id") or None,
            "coordinate_unit": config.get("coordinate_unit") or None,
            "coordinate_frame": config.get("coordinate_frame") or None,
            "source_refs": list(config.get("source_refs") or []),
            "analysis_supported": kind != "reading_annotations",
        }
        return output.reset_index(drop=True), manifest

    def _load_full(self) -> None:
        # Full import and unit conversion use the same queued worker as source reads.
        self._queue_read(preview=False)

    def clear_loaded(self, *, keep_path: bool = False) -> None:
        self._generation += 1
        self._loading = False
        self.preview_button.setEnabled(True)
        self.load_button.setEnabled(True)
        self._frame = pd.DataFrame()
        self._preview = pd.DataFrame()
        self._manifest = {}
        self._raw_bytes = b""
        self._dirty = False
        self.table_model.set_frame(None)
        self._mapping.set_frame(None)
        if not keep_path:
            self.path_edit.clear()
            self.sheet_combo.clear()
        self.status.setText("来源已移除或语义设置已变化；请重新载入完整表。")

    def clear(self) -> None:
        self.clear_loaded()
        self.changed.emit()

    def table(self) -> pd.DataFrame | None:
        if self._frame.empty or not self._manifest.get("loaded_full") or self._dirty:
            return None
        return self._frame.copy(deep=True)

    def manifest(self) -> dict[str, Any] | None:
        return dict(self._manifest) if self._manifest.get("loaded_full") else None

    def mapping_values(self) -> dict[str, str]:
        return self._mapping.mapping()


class _ContextConversionWorker(QRunnable):
    def __init__(self, generation: int, path: str, sheet: str | None, csv_options: CsvOptions,
                 kind: str, config: dict[str, Any]) -> None:
        super().__init__()
        self.path = path
        self.sheet = sheet
        self.csv_options = csv_options
        self.kind = kind
        self.generation = generation
        self.config = dict(config)
        self.signals = _ReadSignals()

    @Slot()
    def run(self) -> None:
        try:
            imported = read_table(self.path, role="signals", sheet_name=self.sheet,
                                  csv_options=self.csv_options, preview_rows=None)
            raw_bytes = Path(self.path).read_bytes()
            if len(raw_bytes) != imported.byte_size or hashlib.sha256(raw_bytes).hexdigest() != imported.sha256:
                raise DataImportError("来源文件在读取期间发生变化；请重新选择并导入。")
            frame, manifest = ContextSourcePanel._materialize_context(self.kind, imported.frame, {
                "source_name": imported.source_name, "sha256": imported.sha256,
                "sheet_name": imported.sheet_name, "byte_size": imported.byte_size,
                "source_rows": imported.source_rows,
            }, self.config)
            manifest["loaded_full"] = True
            manifest["raw_source_key"] = f"context/{self.kind}/{imported.source_name}"
            self.signals.completed.emit(self.generation, {"frame": frame, "manifest": manifest,
                                                           "raw_bytes": raw_bytes, "preview": imported.frame.head(100)})
        except Exception as exc:
            self.signals.failed.emit(self.generation, f"{type(exc).__name__}: {exc}")


class TimeWindowEditor(QWidget):
    """Small typed table of named time windows, preserved as an input DataFrame."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["窗口名称", "起点秒", "终点秒", "来源引用"])
        self.table.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("添加窗口", self)
        remove = QPushButton("移除选中", self)
        add.clicked.connect(self.add_row)
        remove.clicked.connect(self.remove_rows)
        self.table.itemChanged.connect(lambda *_: self.changed.emit())
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("具名时间窗（只保存为 context_windows 表；A 未声明自动消费）", self))
        layout.addWidget(self.table)
        layout.addLayout(row)

    def add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate(("", "", "", "")):
            self.table.setItem(row, column, QTableWidgetItem(value))
        self.changed.emit()

    def remove_rows(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self.changed.emit()

    def frame(self) -> pd.DataFrame:
        records: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            values = [(self.table.item(row, column).text().strip() if self.table.item(row, column) else "")
                      for column in range(4)]
            if not any(values):
                continue
            if not values[0] or not values[1] or not values[2]:
                raise ValueError(f"具名时间窗第 {row + 1} 行需填写名称、起点和终点。")
            start, end = float(values[1]), float(values[2])
            if not np.isfinite(start) or not np.isfinite(end) or end <= start:
                raise ValueError(f"具名时间窗第 {row + 1} 行需满足有限的起点 < 终点。")
            records.append({"window_name": values[0], "start_s": start, "end_s": end,
                            "source_ref": values[3] or None, "time_unit": "s"})
        return pd.DataFrame.from_records(records, columns=("window_name", "start_s", "end_s", "source_ref", "time_unit"))


class ContextEditor(QWidget):
    """Editor for named tables accepted by the actual public AnalysisConfig.context."""

    changed = Signal()
    import_failed = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tabs = QTabWidget(self)
        self.panels: dict[str, ContextSourcePanel] = {}
        for kind, title in _A_CONTEXT_KINDS:
            panel = ContextSourcePanel(kind, title, self.tabs)
            panel.changed.connect(self.changed.emit)
            panel.failed.connect(self.import_failed.emit)
            self.panels[kind] = panel
            self.tabs.addTab(panel, title)
        self.windows = TimeWindowEditor(self)
        self.tabs.addTab(self.windows, "具名时间窗")
        self.windows.changed.connect(self.changed.emit)
        note = QLabel(
            "辅助资料用来补充主要信号，例如设备事件、目标移动区间或参考轨迹。"
            "每类资料都可以查看填写示例；只准备所选方法要求的文件。"
            "载入后仍需检查可用条件，参考资料的时间、单位和坐标系应与本次任务对应。", self
        )
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.tabs, 1)

    def tables(self) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        for name, panel in self.panels.items():
            frame = panel.table()
            if frame is not None:
                result[name] = frame
        windows = self.windows.frame()
        if not windows.empty:
            result["context_windows"] = windows
        return result

    def source_manifest(self) -> list[dict[str, Any]]:
        return [manifest for panel in self.panels.values() if (manifest := panel.manifest()) is not None]

    def raw_source_bytes(self) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for panel in self.panels.values():
            raw = getattr(panel, "_raw_bytes", None)
            manifest = panel.manifest()
            if isinstance(raw, bytes) and manifest and isinstance(manifest.get("raw_source_key"), str):
                result[manifest["raw_source_key"]] = raw
        return result

    def individual_parameters(self) -> dict[str, Any]:
        trajectory = self.panels["reference_trajectory"].manifest()
        if not trajectory:
            return {}
        reference_id = trajectory.get("reference_id")
        values: dict[str, Any] = {}
        if reference_id:
            values["reference_trajectory_id"] = reference_id
        if trajectory and trajectory.get("source_sha256"):
            values["reference_trajectory_sha256"] = trajectory["source_sha256"]
        if trajectory.get("coordinate_unit"):
            values["reference_trajectory_unit"] = trajectory["coordinate_unit"]
        if trajectory.get("coordinate_frame"):
            values["reference_trajectory_coordinate_frame"] = trajectory["coordinate_frame"]
        return values

    def analysis_metadata(self) -> dict[str, Any]:
        return {"context_source_manifest": self.source_manifest(),
                "context_windows_count": len(self.windows.frame())}
