"""Four-source CSV/XLSX import and explicit semantic normalization UI."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
from threading import Event
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
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
from .import_profiles import (
    ImportProfile,
    ImportProfileError,
    ImportSourceProfile,
    compare_structure_signatures,
    list_import_profiles,
    read_import_profile,
    save_import_profile,
)
from .mapping import PerformanceMappingWidget, TableMappingEditor, role_fields
from .task_timing import TaskTiming, TimingEstimator, format_timing
from .widgets import DataFrameTableModel, DataFrameTableView


_ROLE_TITLES = {
    "signals": "主要信号",
    "segments": "Segment Mark（段/试次）",
    "aois": "ROI / AOI",
    "performance": "参与者 / 绩效",
}

_PERFORMANCE_VALUE_PREFIX = "performance_value::"
_PROFILE_UNCHANGED_SUMMARY = (
    "本预设不改变：源文件路径与身份、时间确认、subject/session 默认值、同步方法/依据、"
    "SegmentScope/成员映射、AOI 重叠规则、绩效行粒度/连接键和分析方案。"
)


@dataclass
class _StagedProfileSource:
    role: str
    panel: "ImportSourcePanel"
    profile_source: ImportSourceProfile
    frame: pd.DataFrame
    manifest: dict[str, Any]
    source_stat: tuple[int, int]
    sheet_name: str | None
    signature_difference: Any
    editor: QWidget
    preserved_performance: dict[str, Any] | None = None


class _ProfileStageDialog(QDialog):
    """Review candidate parser/mappings before changing the live import page."""

    def __init__(
        self,
        parent: QWidget,
        profile: ImportProfile,
        stages: Mapping[str, _StagedProfileSource],
        notices: list[str],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"应用导入映射预设：{profile.name}")
        self.resize(920, 720)
        self._stages = dict(stages)
        self._confirmations: dict[str, QCheckBox] = {}
        layout = QVBoxLayout(self)
        intro = QLabel(
            "先核对候选解析结果、工作表和映射，再一次性应用。文件路径来自当前导入页；预设不保存文件路径。\n"
            + _PROFILE_UNCHANGED_SUMMARY,
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        if notices:
            notice = QLabel("\n".join(notices), self)
            notice.setWordWrap(True)
            layout.addWidget(notice)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)
        for role, stage in self._stages.items():
            page = QWidget(self.tabs)
            page_layout = QVBoxLayout(page)
            parser = QLabel(
                f"当前文件：{Path(stage.panel.source_path).name} · 候选工作表：{stage.sheet_name or 'CSV'} · "
                f"候选列：{len(stage.frame.columns)}",
                page,
            )
            parser.setWordWrap(True)
            page_layout.addWidget(parser)
            difference = stage.signature_difference
            if not difference.compatible:
                lines = []
                for item in difference.differences:
                    lines.append(
                        f"{_ROLE_TITLES[role]}：预期列 {list(item.expected_columns or ())}；"
                        f"实际列 {list(item.actual_columns or ())}；"
                        f"新增 {list(item.added_columns)}；缺失 {list(item.removed_columns)}；"
                        f"列顺序变化={'是' if item.reordered else '否'}。"
                    )
                diff_label = QLabel("结构与保存时不同：\n" + "\n".join(lines), page)
                diff_label.setWordWrap(True)
                page_layout.addWidget(diff_label)
                confirmation = QCheckBox(
                    "我已在下方候选映射表核对实际字段，并确认使用当前选择",
                    page,
                )
                self._confirmations[role] = confirmation
                page_layout.addWidget(confirmation)
            if role == "performance":
                note = QLabel(
                    "绩效行粒度和连接键沿用当前配置；若候选表不再包含这些连接键，不能应用该预设。",
                    page,
                )
                note.setWordWrap(True)
                page_layout.addWidget(note)
            page_layout.addWidget(stage.editor, 2)
            preview = DataFrameTableView(page)
            preview.setModel(DataFrameTableModel(stage.frame.head(100), parent=preview))
            preview.setSortingEnabled(False)
            page_layout.addWidget(QLabel("按候选解析设置读取的前 100 行", page))
            page_layout.addWidget(preview, 3)
            self.tabs.addTab(page, _ROLE_TITLES[role])
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("应用候选预设")
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        owner = self.parent()
        if isinstance(owner, ImportPage) and owner._profile_operation_blocked():
            QMessageBox.warning(self, "当前操作繁忙", "导入、分析、归档或导出期间不能确认或应用预设。")
            return
        roles = list(self._stages)
        for role, stage in self._stages.items():
            if role in self._confirmations and not self._confirmations[role].isChecked():
                self.tabs.setCurrentIndex(roles.index(role))
                QMessageBox.warning(self, "需要核对结构", "列结构变化后，请先核对候选映射并勾选确认。")
                return
            if role == "performance":
                widget = stage.editor
                if not isinstance(widget, PerformanceMappingWidget):
                    QMessageBox.warning(self, "候选映射无效", "绩效候选映射控件无效。")
                    return
                errors = _performance_profile_validation(widget, widget.value())
            else:
                widget = stage.editor
                if not isinstance(widget, TableMappingEditor):
                    QMessageBox.warning(self, "候选映射无效", "候选映射控件无效。")
                    return
                errors = widget.validation_messages()
                errors.extend(_mapping_source_errors(widget.mapping(), widget.source_headers))
            if errors:
                self.tabs.setCurrentIndex(roles.index(role))
                QMessageBox.warning(self, "候选映射尚未完成", _ROLE_TITLES[role] + "：\n" + "\n".join(errors))
                return
        super().accept()


def _shared_pupil_unit(units: Mapping[str, str]) -> str:
    """Return a shared unit only when all mapped eyes explicitly agree."""
    values = [str(value).strip() for value in units.values() if str(value).strip()]
    if not values or any(value.casefold() == "unknown" for value in values):
        return "unknown"
    if len({value.casefold() for value in values}) != 1:
        return "unknown"
    return values[0]


def _aoi_editor_frame_name(metadata: Mapping[str, Any]) -> str:
    """Name the exact image-pixel frame; never imply a resize or axis transform."""
    try:
        width = int(metadata.get("width"))
        height = int(metadata.get("height"))
    except (TypeError, ValueError):
        return "image_pixel:unverified"
    if width < 1 or height < 1:
        return "image_pixel:unverified"
    return f"image_pixel:{width}x{height}:origin=top_left:y=down"


def _verified_aoi_editor_provenance(
    source: _SourceRequest,
    imported: Any,
    source_bytes: bytes,
    draft: Mapping[str, Any],
    coordinate: CoordinateMetadata,
) -> dict[str, Any]:
    """Bind the AOI image sidecar to the actual imported CSV bytes and image files."""
    actual_csv_path = str(Path(imported.source_path).expanduser().resolve())
    issues: list[str] = []
    expected_csv_path = str(draft.get("csv_path", ""))
    if not expected_csv_path or actual_csv_path.casefold() != str(Path(expected_csv_path).resolve()).casefold():
        issues.append("导入的 AOI CSV 路径与编辑器导出记录不一致。")
    actual_csv_hash = hashlib.sha256(source_bytes).hexdigest()
    expected_csv_hash = str(draft.get("csv_sha256", ""))
    if not expected_csv_hash or actual_csv_hash != expected_csv_hash:
        issues.append("编辑器 CSV 内容哈希缺失或已变化。")

    manifest_path_text = str(draft.get("manifest_path", ""))
    manifest_path = Path(manifest_path_text).expanduser() if manifest_path_text else Path()
    manifest: dict[str, Any] = {}
    manifest_hash: str | None = None
    expected_manifest_hash = str(draft.get("manifest_sha256", ""))
    if not manifest_path_text or not manifest_path.is_file():
        issues.append("AOI 图片 manifest 缺失，图片来源不可验证。")
    else:
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            if manifest_hash != expected_manifest_hash:
                issues.append("AOI 图片 manifest 哈希与导出时记录不一致。")
            if not isinstance(manifest, dict) or manifest.get("schema") != "pyeyedata.aoi-image-manifest/v1":
                issues.append("AOI 图片 manifest 结构或版本不受支持。")
                manifest = {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            issues.append(f"无法读取 AOI 图片 manifest：{type(exc).__name__}: {exc}")

    declaration = manifest.get("coordinate_declaration", {}) if isinstance(manifest, Mapping) else {}
    declaration_ok = (
        isinstance(declaration, Mapping)
        and str(declaration.get("unit", "")).casefold() in {"px", "pixel", "pixels"}
        and str(declaration.get("coordinate_frame", "")).casefold() in {"image_pixel", "image_pixels"}
        and declaration.get("origin") == "top_left"
        and declaration.get("y_axis_direction") == "down"
        and declaration.get("pixel_coordinates_are_image_native") is True
    )
    if manifest and not declaration_ok:
        issues.append("AOI manifest 的像素坐标声明不完整或不受支持。")

    by_stimulus = manifest.get("stimuli", {}) if isinstance(manifest, Mapping) else {}
    if not isinstance(by_stimulus, Mapping):
        by_stimulus = {}
        issues.append("AOI manifest 缺少按 stimulus_id 分组的图片记录。")
    stimulus_column = source.column_map.get("stimulus_id")
    used_stimuli: set[str] = set()
    if stimulus_column and isinstance(imported.frame, pd.DataFrame) and stimulus_column in imported.frame:
        used_stimuli = {
            str(value).strip() for value in imported.frame[stimulus_column].dropna().tolist()
            if str(value).strip()
        }
    else:
        issues.append("AOI CSV 的 stimulus_id 映射缺失，无法核对图片绑定。")

    verified_stimuli: dict[str, dict[str, Any]] = {}
    for stimulus_id in sorted(used_stimuli):
        item = by_stimulus.get(stimulus_id)
        if not isinstance(item, Mapping):
            issues.append(f"stimulus_id={stimulus_id} 没有对应图片记录。")
            continue
        image_path_text = str(item.get("image_path", ""))
        image_path = Path(image_path_text).expanduser() if image_path_text else None
        expected_image_hash = str(item.get("sha256", ""))
        image_status = "verified"
        if image_path is None or not image_path.is_file():
            image_status = "missing"
            issues.append(f"stimulus_id={stimulus_id} 的原图缺失，图片来源不可验证。")
        else:
            try:
                image_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                if not expected_image_hash or image_digest != expected_image_hash:
                    image_status = "hash_mismatch"
                    issues.append(f"stimulus_id={stimulus_id} 的原图哈希与 manifest 不一致。")
                else:
                    try:
                        from PIL import Image

                        with Image.open(image_path) as image:
                            width, height = image.size
                            image.verify()
                        if (int(item.get("width", 0)) != int(width)
                                or int(item.get("height", 0)) != int(height)):
                            image_status = "dimension_mismatch"
                            issues.append(f"stimulus_id={stimulus_id} 的图片尺寸与 manifest 不一致。")
                    except Exception as exc:
                        image_status = "unreadable"
                        issues.append(f"stimulus_id={stimulus_id} 的原图无法验证尺寸：{type(exc).__name__}: {exc}")
            except OSError as exc:
                image_status = "unreadable"
                issues.append(f"stimulus_id={stimulus_id} 的原图无法读取：{type(exc).__name__}: {exc}")
        verified_stimuli[stimulus_id] = {"status": image_status, **dict(item)}

    frame_names = {
        _aoi_editor_frame_name(item)
        for stimulus_id, item in by_stimulus.items()
        if stimulus_id in used_stimuli and isinstance(item, Mapping)
    }
    unit_matches = str(coordinate.coordinate_unit).strip().casefold() in {"px", "pixel", "pixels"}
    frame_matches = bool(frame_names) and frame_names == {str(coordinate.coordinate_frame).strip()}
    fully_verified = not issues and bool(used_stimuli) and set(verified_stimuli) == used_stimuli
    coordinate_mapping = (
        "declared_same_image_pixel_frame" if fully_verified and declaration_ok and unit_matches and frame_matches
        else "requires_explicit_signal_coordinate_declaration"
    )
    image_metadata = draft.get("image_metadata", {})
    return {
        "schema": "pyeyedata.aoi-editor-provenance/v1",
        "source_type": "gui_static_image_editor",
        "verification_status": "verified" if fully_verified else "unverifiable",
        "verification_issues": issues,
        "coordinate_mapping_status": coordinate_mapping,
        "stimulus_id": str(draft.get("stimulus_id", "")) or None,
        "csv_path": actual_csv_path,
        "csv_sha256": actual_csv_hash,
        "manifest_path": str(manifest_path.resolve()) if manifest_path_text else None,
        "manifest_sha256": manifest_hash,
        "manifest": manifest,
        "verified_stimuli": verified_stimuli,
        "image_metadata": dict(image_metadata) if isinstance(image_metadata, Mapping) else {},
        "exported_at": str(draft.get("exported_at", "")) or None,
    }


def _mapping_source_errors(mapping: Mapping[str, str], headers: Any) -> list[str]:
    available = set(headers)
    return [f"{name} 映射到不存在的源列：{source}" for name, source in mapping.items()
            if source not in available]


def _set_combo_value(combo: QComboBox, value: Any, label: str) -> None:
    index = combo.findData(value)
    if index < 0:
        combo.addItem(label, value)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


def _set_table_editor_values(
    editor: TableMappingEditor,
    column_map: Mapping[str, str],
    units: Mapping[str, str],
    *,
    fallback_units: Mapping[str, str] | None = None,
    fallback_mapping: Mapping[str, str] | None = None,
) -> None:
    fallback_units = dict(fallback_units or {})
    fallback_mapping = dict(fallback_mapping or {})
    for row, spec in enumerate(editor.fields):
        source_choice = editor._table.cellWidget(row, 2)
        unit_choice = editor._table.cellWidget(row, 5)
        if not isinstance(source_choice, QComboBox) or not isinstance(unit_choice, QComboBox):
            continue
        source_name = str(column_map.get(spec.name, ""))
        index = source_choice.findData(source_name)
        source_choice.setCurrentIndex(index if index >= 0 else 0)
        unit = units.get(spec.name)
        if unit is None and fallback_mapping.get(spec.name) == source_name:
            unit = fallback_units.get(spec.name)
        unit_choice.setCurrentText(str(unit or ""))


def _performance_profile_parts(source: ImportSourceProfile) -> tuple[dict[str, str], list[str]]:
    semantics: dict[str, str] = {}
    outcomes: list[str] = []
    for name, column in source.column_map.items():
        if name.startswith(_PERFORMANCE_VALUE_PREFIX):
            outcomes.append(name[len(_PERFORMANCE_VALUE_PREFIX):])
            if outcomes[-1] != column:
                raise ImportProfileError("绩效结果列映射损坏，结果字段名和源列不一致。")
        else:
            semantics[name] = column
    allowed_semantics = set(PerformanceMappingWidget._ROLES) - {"未指定", "performance_value"}
    if set(semantics) - allowed_semantics:
        raise ImportProfileError("绩效预设包含当前不支持的语义字段。")
    if len(set(semantics.values())) != len(semantics):
        raise ImportProfileError("绩效预设把一个源列映射到多个语义字段，不能安全应用。")
    if len(set(outcomes)) != len(outcomes):
        raise ImportProfileError("绩效结果列重复，拒绝应用。")
    return semantics, outcomes


def _fill_performance_widget(
    widget: PerformanceMappingWidget,
    frame: pd.DataFrame,
    semantic_fields: Mapping[str, str],
    performance_columns: list[str],
    units: Mapping[str, str],
    *,
    row_level: str,
    join_keys: list[str],
    lock_join_settings: bool,
) -> None:
    widget.set_frame(frame)
    level_index = widget.row_level.findData(row_level)
    if level_index < 0:
        raise ImportProfileError("当前绩效行粒度不受支持，无法安全应用预设。")
    widget.row_level.setCurrentIndex(level_index)
    semantic_by_source = {source: name for name, source in semantic_fields.items()}
    output_set = set(performance_columns)
    for row in range(widget.table.rowCount()):
        name_item = widget.table.item(row, 0)
        role_choice = widget.table.cellWidget(row, 1)
        key_check = widget.table.cellWidget(row, 2)
        unit_edit = widget.table.cellWidget(row, 3)
        if name_item is None:
            continue
        source_column = name_item.text()
        role = "performance_value" if source_column in output_set else semantic_by_source.get(source_column, "未指定")
        if isinstance(role_choice, QComboBox):
            role_choice.setCurrentText(role)
        if isinstance(key_check, QCheckBox):
            key_check.setChecked(source_column in join_keys)
            key_check.setEnabled(not lock_join_settings)
        if isinstance(unit_edit, QLineEdit):
            unit_edit.setText(str(units.get(source_column, "")))
    widget.row_level.setEnabled(not lock_join_settings)


def _performance_profile_validation(
    widget: PerformanceMappingWidget,
    value: Mapping[str, Any],
    *,
    require_connections: bool = True,
) -> list[str]:
    errors: list[str] = []
    frame = widget._frame
    headers = {str(column) for column in frame.columns}
    semantics = value.get("semantic_fields", {})
    outcomes = list(value.get("performance_columns", []))
    keys = list(value.get("performance_join_keys", []))
    errors.extend(_mapping_source_errors(semantics, headers))
    missing_keys = [key for key in keys if key not in headers]
    if missing_keys:
        errors.append("保留的绩效连接键不在候选工作表中：" + ", ".join(missing_keys))
    if require_connections and outcomes and not keys:
        errors.append("选择绩效结果列时，当前保留配置必须至少有一个显式连接键。")
    units = value.get("performance_units", {})
    for column in outcomes:
        if column not in headers:
            errors.append(f"绩效结果列不存在：{column}")
        if not str(units.get(column, "")).strip():
            errors.append(f"绩效结果列 {column} 需要明确单位/类型说明（未知可写 unknown）。")
    if require_connections and outcomes and keys and not missing_keys:
        audit = widget.audit()
        if audit.get("status") == "review" and audit.get("duplicate_performance_keys"):
            errors.append("当前保留连接键在候选绩效表中重复；请先修正现有连接设置。")
    return errors


class _ReadSignals(QObject):
    completed = Signal(int, object)
    failed = Signal(int, str)
    progress = Signal(int, str)


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
        self.signals.progress.emit(self.generation, "正在读取并预览表格")
        try:
            imported = read_table(self.path, role=self.role, sheet_name=self.sheet,
                                  csv_options=self.options, preview_rows=100)
            self.signals.completed.emit(self.generation, {
                "frame": imported.frame, "manifest": imported.manifest_entry(),
                "source_rows": imported.source_rows, "sheet_name": imported.sheet_name,
                "read_path": self.path, "read_csv_options": self.options,
            })
        except DataImportError as exc:
            if Path(self.path).suffix.lower() == ".xlsx" and "Select a worksheet explicitly" in str(exc):
                try:
                    self.signals.progress.emit(self.generation, "正在列出可选工作表")
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
    aoi_editor_provenance: Mapping[str, Any] | None = None


class _ImportSignals(QObject):
    completed = Signal(int, object, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    progress = Signal(int, int, str)
    finished = Signal(int)


class _ImportCancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    def __call__(self) -> bool:
        return self._event.is_set()


class _ImportCancelled(Exception):
    pass


class _NormalizeWorker(QRunnable):
    """Read, normalize and freeze a complete import off the Qt GUI thread."""

    def __init__(self, generation: int, request: _ImportRequest,
                 token: _ImportCancellationToken) -> None:
        super().__init__()
        self.generation = generation
        self.request = request
        self.token = token
        self.signals = _ImportSignals()

    def _check_cancelled(self) -> None:
        if self.token():
            raise _ImportCancelled

    @Slot()
    def run(self) -> None:
        try:
            normalized: NormalizedDataset | None = None
            raw_sources: dict[str, bytes] = {}
            manifests: dict[str, dict[str, Any]] = {}
            performance_meta: dict[str, Any] = {}
            for index, spec in enumerate(self.request.sources, 1):
                self._check_cancelled()
                self.signals.progress.emit(
                    self.generation,
                    round(5 + 75 * (index - 1) / max(1, len(self.request.sources))),
                    f"读取与规范化：{_ROLE_TITLES[spec.role]}",
                )
                imported = read_table(spec.path, role=spec.role, sheet_name=spec.sheet_name,
                                      csv_options=spec.csv_options, preview_rows=None)
                self._check_cancelled()
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
                    self._check_cancelled()
                    normalized = normalize_signals(imported, SignalImportOptions(
                        column_map=spec.column_map,
                        subject_id=self.request.signal_subject_id,
                        session_id=self.request.signal_session_id,
                        time=self.request.coordinate,
                        validity_values=self.request.validity_values,
                        reading_direction_values=self.request.reading_direction_values,
                    ))
                elif spec.role == "segments":
                    self._check_cancelled()
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
                    self._check_cancelled()
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
                        if self.request.aoi_editor_provenance is not None:
                            # An explicit editor handoff must not install a new
                            # revision if its exported AOI table cannot normalize.
                            raise DataImportError(f"AOI 编辑器导出无法通过现有导入验证：{exc}") from exc
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
                    if self.request.aoi_editor_provenance is not None:
                        provenance = _verified_aoi_editor_provenance(
                            spec, imported, source_bytes, self.request.aoi_editor_provenance,
                            self.request.coordinate,
                        )
                        normalized.metadata["aoi_editor_provenance"] = provenance
                        normalized.metadata["aoi_coordinate_mapping_status"] = provenance[
                            "coordinate_mapping_status"
                        ]
                        if not normalized.aois.empty:
                            verified_stimuli = provenance.get("verified_stimuli", {})

                            def _source_image_frame(value: Any) -> str:
                                stimulus_id = str(value).strip()
                                item = verified_stimuli.get(stimulus_id) if isinstance(verified_stimuli, Mapping) else None
                                if isinstance(item, Mapping) and item.get("status") == "verified":
                                    return _aoi_editor_frame_name(item)
                                return "image_pixel:unverified"

                            # Keep the editor's actual coordinate declaration in
                            # the AOI rows. The pipeline's existing unit/frame
                            # check then blocks unmatched or unknown signal frames.
                            normalized.aois["unit"] = "pixel"
                            normalized.aois["coordinate_frame"] = normalized.aois["stimulus_id"].map(
                                _source_image_frame
                            )
                elif spec.role == "performance":
                    self._check_cancelled()
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
            self._check_cancelled()
            self.signals.progress.emit(self.generation, 90, "校验规范化数据与源字节哈希")
            normalized.validate()
            self._check_cancelled()
            revision = DatasetRevision.create(normalized, raw_sources=raw_sources)
            preview = normalized.signals.head(500).copy(deep=True)
            self._check_cancelled()
            self.signals.progress.emit(self.generation, 100, "四类数据导入完成")
            self.signals.completed.emit(self.generation, revision, preview)
        except _ImportCancelled:
            self.signals.cancelled.emit(self.generation)
        except Exception as exc:
            self.signals.failed.emit(self.generation, f"{type(exc).__name__}: {exc}")
        finally:
            self.signals.finished.emit(self.generation)


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

    def set_values(self, values: Mapping[str, Mapping[str, bool]]) -> None:
        for row in range(self.validity_table.rowCount()):
            field_item = self.validity_table.item(row, 0)
            true_edit = self.validity_table.cellWidget(row, 1)
            false_edit = self.validity_table.cellWidget(row, 2)
            if not field_item or not isinstance(true_edit, QLineEdit) or not isinstance(false_edit, QLineEdit):
                continue
            codes = values.get(field_item.text(), {})
            true_edit.setText("; ".join(code for code, valid in codes.items() if valid))
            false_edit.setText("; ".join(code for code, valid in codes.items() if not valid))


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

    def set_values(self, source_column: str | None, values: Mapping[str, str]) -> None:
        self._source_column = source_column
        self.table.setRowCount(0)
        for code, direction in values.items():
            self._append_row(str(code), str(direction))


class ImportSourcePanel(QWidget):
    changed = Signal(str)
    preview_loaded = Signal(str, object)
    preview_failed = Signal(str, str)
    open_aoi_editor_requested = Signal()
    aoi_mapping_confirmation_changed = Signal(bool)

    def __init__(self, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self._generation = 0
        self._preview_timing_estimator = TimingEstimator()
        self._preview_timing: TaskTiming | None = None
        self._preview_timing_identity: object = None
        self._preview_timing_timer = QTimer(self)
        self._preview_timing_timer.setInterval(1000)
        self._preview_timing_timer.timeout.connect(self._refresh_preview_timing)
        self._preview = pd.DataFrame()
        self._manifest: dict[str, Any] = {}
        self._preview_path: str | None = None
        self._preview_options: CsvOptions | None = None
        self._preview_sheet: str | None = None
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
        self.preview_timing_label = QLabel("当前阶段：未预览", self)
        self.preview_timing_label.setObjectName("importPreviewTiming")
        self.preview_timing_label.setWordWrap(True)
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
        self.aoi_mapping_confirmation = QCheckBox(
            "已确认当前数据与所选 AOI 使用此坐标映射", self,
        )
        self.aoi_mapping_confirmation.setObjectName("individualAoiMappingConfirmation")
        self.aoi_mapping_confirmation.setToolTip(
            "只记录对当前数据修订和 AOI 来源的显式核对；不执行坐标变换，也不替代单位、坐标系和几何校验。"
        )
        self.aoi_mapping_confirmation.setEnabled(False)
        self.aoi_mapping_summary = QLabel("导入当前数据修订后，这里会显示信号与 AOI 的坐标和来源依据。", self)
        self.aoi_mapping_summary.setObjectName("individualAoiMappingEvidenceSummary")
        self.aoi_mapping_summary.setWordWrap(True)
        self.aoi_mapping_options = QGroupBox("个体 AOI 映射确认（仅当前数据修订）", self)
        aoi_mapping_layout = QVBoxLayout(self.aoi_mapping_options)
        aoi_mapping_layout.addWidget(self.aoi_mapping_confirmation)
        aoi_mapping_layout.addWidget(self.aoi_mapping_summary)
        self.aoi_mapping_options.setVisible(role == "aois")
        self.aoi_mapping_confirmation.toggled.connect(
            self.aoi_mapping_confirmation_changed.emit
        )
        self.aoi_editor_button = QPushButton("静态图片 AOI 编辑器…", self)
        self.aoi_editor_button.setToolTip(
            "在明确绑定刺激的图片上编辑。导出并加入本源后仍需显式导入；坐标不会自动缩放或翻转。"
        )
        self.aoi_editor_button.setVisible(role == "aois")
        self.aoi_editor_button.clicked.connect(self.open_aoi_editor_requested.emit)
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
        layout.addWidget(self.preview_timing_label)
        layout.addWidget(self.mapping_host, 3)
        if role == "aois":
            layout.addWidget(self.aoi_editor_button)
            layout.addWidget(self.aoi_options)
            layout.addWidget(self.aoi_mapping_options)
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
        if self._preview_timing is not None:
            previous = self._preview_timing.snapshot()
            if previous.status in {"running", "cancelling"}:
                self._preview_timing.finish("superseded", train=False)
        identity = ("preview", self.role, generation)
        self._preview_timing_identity = identity
        self._preview_timing = TaskTiming(
            self._preview_timing_estimator,
            "preview",
            f"正在预览{_ROLE_TITLES[self.role]}",
            identity=identity,
        )
        self._preview_timing_timer.start()
        self._refresh_preview_timing()
        self.status.setText("后台读取前 100 行。")
        self.preview_button.setEnabled(False)
        worker = _PreviewWorker(generation, self.source_path, self.role, self.sheet_combo.currentData(), self.csv_options())
        worker.signals.completed.connect(self._on_preview)
        worker.signals.failed.connect(self._on_preview_error)
        worker.signals.progress.connect(self._on_preview_progress)
        QThreadPool.globalInstance().start(worker)

    def _refresh_preview_timing(self) -> None:
        if self._preview_timing is not None:
            self.preview_timing_label.setText(format_timing(self._preview_timing.snapshot()))

    @Slot(int, str)
    def _on_preview_progress(self, generation: int, message: str) -> None:
        identity = ("preview", self.role, generation)
        if generation != self._generation or identity != self._preview_timing_identity:
            return
        if self._preview_timing is not None:
            self._preview_timing.update_phase(message)
            self._refresh_preview_timing()

    def _finish_preview_timing(self, generation: int, status: str, *, train: bool) -> None:
        identity = ("preview", self.role, generation)
        if generation != self._generation or identity != self._preview_timing_identity:
            return
        if self._preview_timing is not None:
            self._preview_timing.finish(status, train=train)
            self._preview_timing_timer.stop()
            self._refresh_preview_timing()

    @Slot(int, object)
    def _on_preview(self, generation: int, payload: object) -> None:
        if generation != self._generation or not isinstance(payload, dict):
            return
        self.preview_button.setEnabled(True)
        if payload.get("requires_sheet") is True:
            if self._preview_timing is not None:
                self._preview_timing.update_phase("等待选择工作表")
            self._finish_preview_timing(generation, "等待选择工作表", train=False)
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
            if payload.get("requires_sheet") is not True:
                self._finish_preview_timing(generation, "invalid_result", train=False)
            self.status.setText("此工作簿包含多个工作表，请先选择工作表再预览。")
            return
        self._finish_preview_timing(generation, "complete", train=True)
        self._preview = frame.copy(deep=True)
        self._manifest = dict(payload.get("manifest", {}))
        self._preview_path = str(payload.get("read_path") or self.source_path)
        read_options = payload.get("read_csv_options")
        self._preview_options = read_options if isinstance(read_options, CsvOptions) else None
        self._preview_sheet = self._manifest.get("sheet_name")
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
        self._finish_preview_timing(generation, "failed", train=False)
        self.preview_button.setEnabled(True)
        self.status.setText("预览失败：" + message)
        self.preview_failed.emit(self.role, message)

    def capture_profile_settings(self) -> ImportSourceProfile:
        """Capture only confirmed mapping/parser fields, never the current file path."""
        if not self.source_path or self._preview.empty:
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}需先选择文件并完成预览。")
        if self._preview_path != self.source_path or self._preview_options != self.csv_options():
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}的读取选项已变化，请先重新预览。")
        if self.source_path.lower().endswith(".xlsx") and self.sheet_combo.currentData() != self._preview_sheet:
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}工作表已变化，请先重新预览。")
        if self.role == "performance" and self.performance_mapping is not None:
            value = self.performance_mapping.value()
            column_map = dict(value["semantic_fields"])
            for column in value["performance_columns"]:
                column_map[_PERFORMANCE_VALUE_PREFIX + str(column)] = str(column)
            units = dict(value["performance_units"])
        else:
            if self.mapping is None:
                raise ImportProfileError("当前源映射控件不可用。")
            column_map = self.mapping.mapping()
            units = self.mapping.units()
        sheet_name = self._manifest.get("sheet_name") if str(self.source_path).lower().endswith(".xlsx") else None
        if str(self.source_path).lower().endswith(".xlsx") and not sheet_name:
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}工作簿尚未显式选择并预览工作表。")
        return ImportSourceProfile(
            role=self.role,
            column_map=column_map,
            units=units,
            csv_options=self.csv_options(),
            sheet_name=str(sheet_name) if sheet_name else None,
        )

    def stage_profile_settings(self, source: ImportSourceProfile) -> dict[str, Any]:
        """Parse the current file with candidate settings without changing live controls."""
        if source.role != self.role:
            raise ImportProfileError("预设源角色与当前导入面板不一致。")
        if not self.source_path:
            raise ImportProfileError(f"请先为 {_ROLE_TITLES[self.role]} 选择并预览实际源文件。")
        suffix = Path(self.source_path).suffix.lower()
        candidate_sheet = None
        if suffix == ".xlsx":
            current_sheet = self.sheet_combo.currentData()
            candidate_sheet = source.sheet_name if source.sheet_name is not None else current_sheet
            names = _safe_sheet_names(self.source_path)
            if candidate_sheet is None and len(names) == 1:
                # Legacy profiles without a sheet preserve the current choice;
                # a single-sheet workbook remains unambiguous without one.
                pass
            elif candidate_sheet not in names:
                if not names:
                    raise ImportProfileError(f"{_ROLE_TITLES[self.role]}工作簿没有可用工作表。")
                label, ok = QInputDialog.getItem(
                    self,
                    "选择候选工作表",
                    f"预设工作表 {candidate_sheet!r} 不存在或未指定，请为当前文件明确选择工作表：",
                    list(names),
                    0,
                    False,
                )
                if not ok or label not in names:
                    raise ImportProfileError(f"{_ROLE_TITLES[self.role]}候选工作表未确认，预设未应用。")
                candidate_sheet = label
        else:
            candidate_sheet = None
        try:
            source_stat_before = Path(self.source_path).stat()
            imported = read_table(
                self.source_path,
                role=self.role,
                sheet_name=candidate_sheet,
                csv_options=source.csv_options,
                preview_rows=100,
            )
        except DataImportError as exc:
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}按预设设置解析失败：{exc}") from exc
        if imported.frame.columns.has_duplicates:
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}候选列名重复，不能安全应用映射。")
        source_stat_after = Path(self.source_path).stat()
        if (source_stat_before.st_size, source_stat_before.st_mtime_ns) != (
            source_stat_after.st_size, source_stat_after.st_mtime_ns,
        ):
            raise ImportProfileError(f"{_ROLE_TITLES[self.role]}文件在候选预览期间发生变化，请重新预览后再应用。")
        return {
            "frame": imported.frame,
            "manifest": imported.manifest_entry(),
            "source_stat": (source_stat_after.st_size, source_stat_after.st_mtime_ns),
            "sheet_name": imported.sheet_name,
            "csv_options": source.csv_options,
        }

    def clear_preview(self, *, keep_path: bool = False) -> None:
        if self._preview_timing is not None:
            snapshot = self._preview_timing.snapshot()
            if snapshot.status in {"running", "cancelling"}:
                self._preview_timing.finish("superseded", train=False)
            self._preview_timing_timer.stop()
        self._generation += 1
        self._preview = pd.DataFrame()
        self._manifest = {}
        self._preview_path = None
        self._preview_options = None
        self._preview_sheet = None
        self.table_model.set_frame(None)
        if self.mapping is not None:
            self.mapping.set_frame(None)
        if self.performance_mapping is not None:
            self.performance_mapping.set_frame(None)
        if not keep_path:
            self.path_edit.clear()
            self.sheet_combo.clear()
        self.preview_timing_label.setText("当前阶段：未开始预览")
        self.status.setText("未选择文件。选择文件不会自动确认字段映射或单位。")

    def clear(self) -> None:
        self.clear_preview()
        self.changed.emit(self.role)

    def set_external_csv(self, path: str | Path) -> None:
        """Stage a generated AOI CSV through the same preview and mapping path."""
        if self.role != "aois":
            raise ValueError("Only the AOI source panel accepts an editor export.")
        self.path_edit.setText(str(Path(path).expanduser().resolve()))
        self.clear_preview(keep_path=True)
        blocker = QSignalBlocker(self.sheet_combo)
        self.sheet_combo.clear()
        del blocker
        self.encoding_edit.setText("utf-8-sig")
        delimiter = self.delimiter_combo.findData(",")
        if delimiter >= 0:
            self.delimiter_combo.setCurrentIndex(delimiter)
        decimal = self.decimal_combo.findData(".")
        if decimal >= 0:
            self.decimal_combo.setCurrentIndex(decimal)

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
        from .help_center import show_help
        show_help(self, role=self.role)


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
    import_state_changed = Signal(bool, str)
    import_idle = Signal()
    import_worker_completed = Signal(int, object, object)
    import_worker_failed = Signal(int, str)
    import_worker_cancelled = Signal(int)
    import_worker_finished = Signal(int)
    batch_queue_requested = Signal()
    open_aoi_editor_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, context_editor: ContextEditor | None = None) -> None:
        super().__init__(parent)
        self._worker: _NormalizeWorker | None = None
        self._worker_generation: int | None = None
        self._request_generation = 0
        self._import_token: _ImportCancellationToken | None = None
        self._external_busy = False
        self._batch_review_mode = False
        self._applying_import_profile = False
        self._pending_worker_message = ""
        self._last_revision: DatasetRevision | None = None
        self._profile_error_items: dict[str, tuple[Path, str]] = {}
        self._profiles_by_id: dict[str, ImportProfile] = {}
        self._batch_profile_confirmation: tuple[str, int, str] | None = None
        self._aoi_editor_pending_import: dict[str, Any] | None = None
        self._staging_aoi_editor_source = False
        self._aoi_confirmation_eligible_revision_id: str | None = None
        self._aoi_confirmation_revision: DatasetRevision | None = None
        self._aoi_confirmation_binding: dict[str, Any] | None = None
        self._aoi_confirmation_timestamp: str | None = None
        self._aoi_confirmation_notice = "尚未确认。"
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
        self.batch_queue_button = QPushButton("批量分析队列…", self)
        self.batch_queue_button.setToolTip("按当前已确认的导入映射预设，逐个独立分析并导出结果。")
        self.import_profile_combo = QComboBox(self)
        self.import_profile_combo.setObjectName("importProfileCombo")
        self.import_profile_refresh_button = QPushButton("刷新预设", self)
        self.import_profile_save_button = QPushButton("保存当前映射…", self)
        self.import_profile_apply_button = QPushButton("预览并应用", self)
        self.import_profile_status = QLabel("预设与 .pyeplan 分开保存；不会保存源文件路径。", self)
        self.import_profile_status.setWordWrap(True)
        profile_box = QGroupBox("导入映射预设（独立于 .pyeplan）", self)
        profile_layout = QVBoxLayout(profile_box)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.import_profile_combo, 1)
        profile_row.addWidget(self.import_profile_refresh_button)
        profile_row.addWidget(self.import_profile_save_button)
        profile_row.addWidget(self.import_profile_apply_button)
        profile_layout.addLayout(profile_row)
        profile_layout.addWidget(self.import_profile_status)
        self.cancel_import_button = QPushButton("取消导入", self)
        self.cancel_import_button.setObjectName("cancelImportButton")
        self.cancel_import_button.setToolTip("请求协作取消；当前文件读取结束后才会停止。")
        self.cancel_import_button.setEnabled(False)
        self.status = QLabel("先选择主要信号 CSV/XLSX，再按需添加段、AOI 和绩效表。", self)
        self.status.setWordWrap(True)
        self.import_button.clicked.connect(self.start_import)
        self.batch_queue_button.clicked.connect(self.batch_queue_requested.emit)
        self.cancel_import_button.clicked.connect(self.cancel_import)
        self.import_profile_refresh_button.clicked.connect(self.refresh_import_profiles)
        self.import_profile_save_button.clicked.connect(self.save_current_import_profile)
        self.import_profile_apply_button.clicked.connect(self.apply_selected_import_profile)
        for panel in self.sources.values():
            panel.changed.connect(self._source_changed)
            panel.preview_loaded.connect(self._preview_ready)
            panel.preview_failed.connect(self._preview_error)
        self.sources["aois"].open_aoi_editor_requested.connect(self.open_aoi_editor_requested.emit)
        self.sources["aois"].aoi_mapping_confirmation_changed.connect(
            self._on_aoi_mapping_confirmation_changed
        )
        self.coordinate.settings_changed.connect(self._semantics_changed)
        self._validity_codes.changed.connect(
            lambda: self._normalization_configuration_changed("validity_code_mapping_changed", "有效性码表已变化")
        )
        self._reading_directions.changed.connect(
            lambda: self._normalization_configuration_changed("reading_direction_mapping_changed", "阅读方向映射已变化")
        )
        self.scope.settings_changed.connect(self._scope_changed)
        self.context_editor.changed.connect(self._context_configuration_changed)
        self.context_editor.import_failed.connect(self.import_failed.emit)
        layout = QVBoxLayout(self)
        intro = QLabel("四类文件分别导入；来源、单位、ID 与字段映射都需明确。原始信号在标准化前只读，未知/未提供能力保持未知。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(profile_box)
        layout.addWidget(self._coordinate_layout, 2)
        layout.addWidget(self.scope, 1)
        layout.addWidget(self.source_tabs, 5)
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.cancel_import_button)
        footer.addWidget(self.batch_queue_button)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)
        self.refresh_import_profiles()
        self._update_import_profile_controls()

    @property
    def import_active(self) -> bool:
        return self._worker is not None

    @property
    def pending_import_message(self) -> str:
        if not self.import_active:
            return ""
        return self._pending_worker_message or "正在导入并规范化"

    def set_external_busy(self, busy: bool) -> None:
        self._external_busy = bool(busy)
        self.import_button.setEnabled(
            not self._external_busy and not self._batch_review_mode and self._worker is None
        )
        self.batch_queue_button.setEnabled(not self._external_busy and not self._batch_review_mode)
        self._update_aoi_editor_button()
        self._update_aoi_mapping_controls()
        self._update_import_profile_controls()

    def set_batch_review_mode(self, active: bool) -> None:
        """Allow explicit mapping review while preventing a competing import."""
        self._batch_review_mode = bool(active)
        self.import_button.setEnabled(
            not self._external_busy and not self._batch_review_mode and self._worker is None
        )
        self.batch_queue_button.setEnabled(not self._external_busy and not self._batch_review_mode)
        self._update_aoi_editor_button()
        self._update_aoi_mapping_controls()

    def _update_aoi_editor_button(self) -> None:
        button = self.sources["aois"].aoi_editor_button
        button.setEnabled(not self._external_busy and not self._batch_review_mode and self._worker is None)

    def _aoi_mapping_evidence(self, revision: DatasetRevision) -> dict[str, Any]:
        """Return truthful, data-bound evidence for an explicit AOI mapping decision."""
        dataset = revision.dataset_copy()
        analysis = dataset.metadata.get("analysis_metadata", {})
        analysis = analysis if isinstance(analysis, Mapping) else {}

        def values(frame: pd.DataFrame, column: str) -> list[str]:
            if column not in frame:
                return []
            result: list[str] = []
            for value in frame[column].tolist():
                try:
                    if bool(pd.isna(value)):
                        continue
                except (TypeError, ValueError):
                    pass
                text = str(value).strip()
                if text and text not in result:
                    result.append(text)
            return sorted(result)

        manifest_by_role: dict[str, dict[str, Any]] = {}
        for item in dataset.source_manifest:
            if not isinstance(item, Mapping):
                continue
            role = item.get("role")
            if role in {"signals", "aois"}:
                manifest_by_role[str(role)] = dict(item)
        refs = [f"dataset_fingerprint:{revision.fingerprint}"]
        source_hashes: dict[str, str] = {}
        for role in ("signals", "aois"):
            digest = manifest_by_role.get(role, {}).get("sha256")
            if isinstance(digest, str) and len(digest) == 64:
                source_hashes[role] = digest
                refs.append(f"source_sha256:{role}:{digest}")

        provenance = dataset.metadata.get("aoi_editor_provenance", {})
        provenance = provenance if isinstance(provenance, Mapping) else {}
        manifest_hash = provenance.get("manifest_sha256")
        if isinstance(manifest_hash, str) and len(manifest_hash) == 64:
            refs.append(f"aoi_manifest_sha256:{manifest_hash}")
        image_hashes: dict[str, str] = {}
        verified_stimuli = provenance.get("verified_stimuli", {})
        if isinstance(verified_stimuli, Mapping):
            for stimulus, item in verified_stimuli.items():
                if not isinstance(item, Mapping):
                    continue
                # ``verified_stimuli`` preserves the editor manifest entry,
                # whose image digest is named ``sha256``. Keep this lookup
                # aligned with that source schema so the confirmation binds
                # to the actual image bytes.
                digest = item.get("sha256")
                if isinstance(digest, str) and len(digest) == 64:
                    image_hashes[str(stimulus)] = digest
                    refs.append(f"stimulus_image_sha256:{stimulus}:{digest}")

        return {
            "fingerprint": revision.fingerprint,
            "revision_id": revision.revision_id,
            "signal_coordinate_unit": str(analysis.get("coordinate_unit", "unknown")),
            "signal_coordinate_frame": str(analysis.get("coordinate_frame", "unknown")),
            "aoi_units": values(dataset.aois, "unit"),
            "aoi_coordinate_frames": values(dataset.aois, "coordinate_frame"),
            "signal_stimuli": values(dataset.signals, "stimulus_id"),
            "aoi_stimuli": values(dataset.aois, "stimulus_id"),
            "source_hashes": source_hashes,
            "image_hashes": image_hashes,
            "aoi_provenance_status": str(provenance.get("verification_status", "not_editor_export")),
            "coordinate_mapping_status": str(provenance.get("coordinate_mapping_status", "not_declared")),
            "refs": sorted(set(refs)),
        }

    def _update_aoi_mapping_controls(self) -> None:
        panel = self.sources.get("aois")
        if panel is None:
            return
        revision = self._aoi_confirmation_revision
        eligible = (
            revision is not None
            and self._aoi_confirmation_eligible_revision_id == revision.revision_id
            and self._last_revision is not None
            and self._last_revision.revision_id == revision.revision_id
            and self._last_revision.fingerprint == revision.fingerprint
        )
        dataset = revision.dataset_copy() if revision is not None else None
        has_aois = dataset is not None and isinstance(dataset.aois, pd.DataFrame) and not dataset.aois.empty
        enabled = bool(
            eligible and has_aois and not self._external_busy
            and not self._batch_review_mode and self._worker is None
        )
        panel.aoi_mapping_confirmation.setEnabled(enabled)
        if revision is None:
            evidence_text = "导入并规范化当前信号与 AOI 后，才可核对映射。"
        else:
            evidence = self._aoi_mapping_evidence(revision)
            evidence_text = (
                f"修订 {revision.revision_id} · 指纹 {revision.fingerprint[:12]}；"
                f"信号 {evidence['signal_coordinate_unit']} / {evidence['signal_coordinate_frame']}，"
                f"AOI 单位 {', '.join(evidence['aoi_units']) or '未知'} / frame "
                f"{', '.join(evidence['aoi_coordinate_frames']) or '未知'}；"
                f"信号刺激 {', '.join(evidence['signal_stimuli']) or '无'}，"
                f"AOI 刺激 {', '.join(evidence['aoi_stimuli']) or '无'}。"
            )
            source_parts = [
                f"{role} SHA-256 {digest[:12]}"
                for role, digest in evidence["source_hashes"].items()
            ]
            source_parts.extend(
                f"图片 {stimulus} SHA-256 {digest[:12]}"
                for stimulus, digest in evidence["image_hashes"].items()
            )
            if source_parts:
                evidence_text += " 来源：" + "；".join(source_parts) + "。"
            evidence_text += (
                f" 图片来源核验 {evidence['aoi_provenance_status']}，"
                f"坐标声明 {evidence['coordinate_mapping_status']}。"
            )
        checked = bool(
            revision is not None and self._aoi_confirmation_binding is not None
            and self._aoi_confirmation_binding.get("fingerprint") == revision.fingerprint
            and panel.aoi_mapping_confirmation.isChecked()
        )
        state_text = "已对当前修订显式确认。" if checked else self._aoi_confirmation_notice
        if not eligible:
            state_text += " 当前修订未由本页本次显式导入建立可确认绑定。"
        elif not has_aois:
            state_text += " 当前修订没有有效 AOI 表。"
        panel.aoi_mapping_summary.setText(evidence_text + "\n" + state_text)

    def set_aoi_confirmation_revision(
        self, revision: DatasetRevision, *, allow_user_confirmation: bool,
    ) -> None:
        """Bind the checkbox to one newly installed import revision, never archive state."""
        self._aoi_confirmation_revision = revision
        eligible = bool(
            allow_user_confirmation
            and self._aoi_confirmation_eligible_revision_id == revision.revision_id
            and self._last_revision is not None
            and self._last_revision.revision_id == revision.revision_id
            and self._last_revision.fingerprint == revision.fingerprint
        )
        if not eligible:
            self._aoi_confirmation_eligible_revision_id = None
        self._aoi_confirmation_binding = None
        self._aoi_confirmation_timestamp = None
        self._aoi_confirmation_notice = "尚未确认；该确认只适用于当前修订。"
        panel = self.sources["aois"]
        blocker = QSignalBlocker(panel.aoi_mapping_confirmation)
        panel.aoi_mapping_confirmation.setChecked(False)
        del blocker
        self._update_aoi_mapping_controls()

    def clear_aoi_confirmation_revision(self, reason: str) -> None:
        self._aoi_confirmation_eligible_revision_id = None
        self._aoi_confirmation_revision = None
        self._aoi_confirmation_binding = None
        self._aoi_confirmation_timestamp = None
        self._aoi_confirmation_notice = str(reason)
        panel = self.sources["aois"]
        blocker = QSignalBlocker(panel.aoi_mapping_confirmation)
        panel.aoi_mapping_confirmation.setChecked(False)
        del blocker
        self._update_aoi_mapping_controls()

    def _invalidate_aoi_mapping_binding(self, reason: str, *, clear_revision: bool = False) -> None:
        self._aoi_confirmation_eligible_revision_id = None
        if clear_revision:
            self._aoi_confirmation_revision = None
        self._aoi_confirmation_binding = None
        self._aoi_confirmation_timestamp = None
        self._aoi_confirmation_notice = str(reason)
        panel = self.sources["aois"]
        blocker = QSignalBlocker(panel.aoi_mapping_confirmation)
        panel.aoi_mapping_confirmation.setChecked(False)
        del blocker
        self._update_aoi_mapping_controls()

    def individual_aoi_mapping_metadata(self, revision: DatasetRevision | None) -> dict[str, Any]:
        """Return only the current, explicitly confirmed individual AOI relationship."""
        result: dict[str, Any] = {
            "aoi_mapping_confirmed": False,
            "aoi_mapping_refs": [],
        }
        panel = self.sources["aois"]
        bound = self._aoi_confirmation_revision
        if (
            revision is None or bound is None
            or self._aoi_confirmation_eligible_revision_id != revision.revision_id
            or bound.revision_id != revision.revision_id
            or bound.fingerprint != revision.fingerprint
            or self._last_revision is None
            or self._last_revision.revision_id != revision.revision_id
            or not panel.aoi_mapping_confirmation.isChecked()
        ):
            return result
        evidence = self._aoi_mapping_evidence(revision)
        if (self._aoi_confirmation_binding is None
                or self._aoi_confirmation_binding != evidence):
            self.clear_aoi_confirmation_revision("数据或 AOI 来源已变化；请重新核对映射。")
            return result
        result.update({
            "aoi_mapping_confirmed": True,
            "aoi_mapping_refs": list(evidence["refs"]),
            "aoi_mapping_confirmed_at": self._aoi_confirmation_timestamp,
            "aoi_mapping_revision_fingerprint": revision.fingerprint,
        })
        return result

    def _on_aoi_mapping_confirmation_changed(self, checked: bool) -> None:
        revision = self._aoi_confirmation_revision
        if checked:
            if (revision is None or self._aoi_confirmation_eligible_revision_id != revision.revision_id
                    or self._last_revision is None
                    or self._last_revision.fingerprint != revision.fingerprint
                    or revision.dataset_copy().aois.empty):
                panel = self.sources["aois"]
                blocker = QSignalBlocker(panel.aoi_mapping_confirmation)
                panel.aoi_mapping_confirmation.setChecked(False)
                del blocker
                self._aoi_confirmation_notice = "当前修订没有可确认的有效 AOI 来源。"
                self._update_aoi_mapping_controls()
                return
            self._aoi_confirmation_binding = self._aoi_mapping_evidence(revision)
            self._aoi_confirmation_timestamp = datetime.now(timezone.utc).isoformat()
            self._aoi_confirmation_notice = "已确认；该声明不替代坐标或几何前检。"
        else:
            self._aoi_confirmation_binding = None
            self._aoi_confirmation_timestamp = None
            self._aoi_confirmation_notice = "尚未确认；AOI 指标将保留现有缺失原因。"
        self._update_aoi_mapping_controls()
        self.configuration_changed.emit()

    def stage_aoi_editor_export(self, csv_path: str | Path, provenance: Mapping[str, Any]) -> None:
        """Add an exported AOI table as an unimported draft without invalidating the installed revision."""
        if self._external_busy or self._batch_review_mode or self._worker is not None:
            raise RuntimeError("等待当前导入、分析或批量操作结束后，再加入 AOI 编辑器输出。")
        self._invalidate_aoi_mapping_binding(
            "AOI 来源已变更；导入新修订后请重新核对映射。"
        )
        path = Path(csv_path).expanduser().resolve(strict=True)
        if path.suffix.lower() != ".csv":
            raise ValueError("AOI 编辑器输出必须是 CSV 文件。")
        draft = copy.deepcopy(dict(provenance))
        draft["csv_path"] = str(path)
        self._aoi_editor_pending_import = draft
        panel = self.sources["aois"]
        self._staging_aoi_editor_source = True
        try:
            panel.set_external_csv(path)
        finally:
            self._staging_aoi_editor_source = False
        self.configuration_changed.emit()
        panel.request_preview()
        self.status.setText(
            "AOI 编辑器输出已加入 AOI 导入源；当前数据修订保持不变。预览并检查后，点击“导入并规范化”生成新修订。"
        )
        self._update_aoi_editor_button()

    def _update_import_profile_controls(self) -> None:
        busy = self._external_busy or self._worker is not None or self._applying_import_profile
        self.import_profile_combo.setEnabled(not busy and self.import_profile_combo.count() > 0)
        self.import_profile_refresh_button.setEnabled(not busy)
        self.import_profile_save_button.setEnabled(not busy)
        self.import_profile_apply_button.setEnabled(
            not busy and bool(self.import_profile_combo.currentData())
        )

    def refresh_import_profiles(self) -> None:
        selected = self.import_profile_combo.currentData()
        self._profiles_by_id.clear()
        self._profile_error_items.clear()
        blocker = QSignalBlocker(self.import_profile_combo)
        self.import_profile_combo.clear()
        self.import_profile_combo.addItem("选择已保存的预设…", "")
        try:
            entries = list_import_profiles()
        except Exception as exc:
            del blocker
            self.import_profile_status.setText("读取导入预设目录失败：" + str(exc))
            self._update_import_profile_controls()
            return
        for path, profile, error in entries:
            if profile is None:
                key = "invalid:" + path.name
                self._profile_error_items[key] = (path, error or "预设无法读取。")
                self.import_profile_combo.addItem(f"{path.stem} · 无法读取", key)
            else:
                self._profiles_by_id[profile.profile_id] = profile
                self.import_profile_combo.addItem(
                    f"{profile.name} · r{profile.revision}", profile.profile_id
                )
        target = self.import_profile_combo.findData(selected)
        if target < 0 and selected:
            target = self.import_profile_combo.findData(selected)
        self.import_profile_combo.setCurrentIndex(target if target >= 0 else 0)
        del blocker
        self.import_profile_status.setText(
            f"已读取 {len(self._profiles_by_id)} 个预设；与 .pyeplan 分开管理，不保存源文件路径。"
            if self._profiles_by_id else
            "尚无可用预设。保存前请先预览实际源文件并确认映射；预设与 .pyeplan 分开管理。"
        )
        self._update_import_profile_controls()

    def _profile_operation_blocked(self) -> bool:
        return self._external_busy or self._worker is not None or self._applying_import_profile

    def save_current_import_profile(self) -> None:
        if self._profile_operation_blocked():
            self.import_profile_status.setText("当前导入、分析、归档或导出任务运行中，暂不能保存预设。")
            return
        try:
            captured: list[ImportSourceProfile] = []
            signatures: dict[str, tuple[str, ...]] = {}
            for role, panel in self.sources.items():
                if not panel.source_path:
                    continue
                source = panel.capture_profile_settings()
                if role == "performance":
                    value = panel.performance_mapping.value()
                    errors = _performance_profile_validation(
                        panel.performance_mapping, value, require_connections=False,
                    ) if panel.performance_mapping is not None else ["绩效映射控件不可用。"]
                else:
                    subject_id = self.coordinate.subject_id.text().strip() or None
                    session_id = self.coordinate.session_id.text().strip() or None
                    required = self._required_for_role(role, subject_id, session_id)
                    if required is not None and panel.mapping is not None:
                        # Match the live import contract: explicit defaults can
                        # satisfy identity fields, while derived source fields
                        # such as time_raw/original_index are not user mappings.
                        blocker = QSignalBlocker(panel.mapping)
                        panel.mapping.set_required_fields(required)
                        del blocker
                    errors = panel.mapping.validation_messages() if panel.mapping is not None else ["映射控件不可用。"]
                    errors.extend(_mapping_source_errors(source.column_map, panel._preview.columns))
                if errors:
                    raise ImportProfileError(_ROLE_TITLES[role] + "：\n" + "\n".join(errors))
                captured.append(source)
                signatures[role] = tuple(str(column) for column in panel._preview.columns)
            if "signals" not in signatures:
                raise ImportProfileError("必须先选择并预览主要信号文件，且明确保存信号字段映射。")
            signals = next(item for item in captured if item.role == "signals")
            if not signals.column_map:
                raise ImportProfileError("主要信号映射为空，不能保存未确认的导入预设。")
            if self.coordinate.sync_confirmed.isChecked():
                if not self.coordinate.sync_method.text().strip() or not self.coordinate.sync_refs.text().strip():
                    raise ImportProfileError("已确认跨时钟同步时，需要填写同步方法和依据引用。")
            coordinate = self.coordinate.coordinate_metadata()
            validity = self._validity_codes.values()
            reading = self._reading_directions.values()
            signal_map = signals.column_map
            if set(validity) - set(signal_map):
                raise ImportProfileError("有效性码表字段未包含在主要信号映射中。")
            if reading and "reading_direction" not in signal_map:
                raise ImportProfileError("阅读方向代码已配置，但主要信号没有映射 reading_direction。")
            pupil_units: dict[str, str] = {}
            for eye in ("left", "right"):
                canonical = f"pupil_{eye}"
                widget_unit = getattr(self.coordinate, f"pupil_{eye}_unit").text().strip()
                mapping_unit = str(signals.units.get(canonical, "")).strip()
                if canonical not in signal_map:
                    if widget_unit:
                        pupil_units[eye] = widget_unit
                    continue
                if widget_unit and mapping_unit and widget_unit.casefold() != mapping_unit.casefold():
                    raise ImportProfileError(f"{canonical} 在字段映射和单位面板中的声明不一致。")
                unit = widget_unit or mapping_unit
                if not unit:
                    raise ImportProfileError(f"已映射 {canonical}，请明确其单位；未知可写 unknown。")
                pupil_units[eye] = unit
            name, ok = QInputDialog.getText(
                self,
                "保存导入映射预设",
                "预设名称（不会保存源文件路径或分析方案）：",
            )
            if not ok:
                return
            name = name.strip()
            if not name:
                raise ImportProfileError("预设名称不能为空。")
            matching = [item for item in self._profiles_by_id.values()
                        if item.name.casefold() == name.casefold()]
            if len(matching) > 1:
                raise ImportProfileError("存在多个同名预设，请先重命名或删除重复项后再保存。")
            if matching:
                profile_id = matching[0].profile_id
                revision = matching[0].revision + 1
            else:
                profile_id = "profile-" + uuid4().hex[:24]
                revision = 1
            profile = ImportProfile(
                profile_id=profile_id,
                name=name,
                revision=revision,
                sources=tuple(captured),
                structure_signature=signatures,
                coordinate_metadata=coordinate,
                validity_values=validity,
                reading_direction_values=reading,
                pupil_units_by_eye=pupil_units,
            )
            saved_path = save_import_profile(profile)
            self.refresh_import_profiles()
            index = self.import_profile_combo.findData(profile.profile_id)
            if index >= 0:
                self.import_profile_combo.setCurrentIndex(index)
            self.import_profile_status.setText(
                f"已保存“{profile.name}” r{profile.revision}。{_PROFILE_UNCHANGED_SUMMARY}"
            )
            self._batch_profile_confirmation = (
                profile.profile_id, profile.revision, self._profile_configuration_key()
            )
            self.status.setText(f"导入映射预设已保存：{saved_path.name}。")
        except Exception as exc:
            self.import_profile_status.setText("保存失败：" + str(exc))
            QMessageBox.warning(self, "无法保存导入预设", str(exc))

    def apply_selected_import_profile(self) -> None:
        if self._profile_operation_blocked():
            self.import_profile_status.setText("当前导入、分析、归档或导出任务运行中，暂不能应用预设。")
            return
        selected = self.import_profile_combo.currentData()
        if not selected:
            self.import_profile_status.setText("请先选择一个有效的导入映射预设。")
            return
        if isinstance(selected, str) and selected.startswith("invalid:"):
            path, error = self._profile_error_items.get(selected, (Path(selected), "预设无法读取。"))
            self.import_profile_status.setText(f"预设 {path.name} 未加载，当前设置保持不变：{error}")
            QMessageBox.warning(self, "导入预设无法读取", error)
            return
        try:
            entries = {profile.profile_id: path for path, profile, error in list_import_profiles()
                       if profile is not None and error is None}
            path = entries.get(str(selected))
            if path is None:
                raise ImportProfileError("所选预设已不存在或无法读取；刷新列表后重试。")
            profile = read_import_profile(path)
            stages, notices = self._stage_import_profile(profile)
            if "signals" not in stages:
                raise ImportProfileError("请先为当前导入页选择主要信号文件；预设不会恢复或猜测文件路径。")
            dialog = _ProfileStageDialog(self, profile, stages, notices)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.import_profile_status.setText("预设候选已取消；当前导入设置和数据修订保持不变。")
                return
            if self._profile_operation_blocked():
                raise ImportProfileError("候选核对期间操作状态变为繁忙；预设未应用，请空闲后重试。")
            self._validate_staged_profile(profile, stages)
            before = self._profile_configuration_key()
            snapshot = self._capture_profile_apply_snapshot(stages)
            self._applying_import_profile = True
            self._update_import_profile_controls()
            blockers = [QSignalBlocker(widget) for widget in [self, *self.findChildren(QWidget)]]
            try:
                self._apply_staged_profile(profile, stages)
            except Exception:
                self._restore_profile_apply_snapshot(snapshot)
                raise
            finally:
                del blockers
                self._applying_import_profile = False
                self._update_import_profile_controls()
            after = self._profile_configuration_key()
            changed = before != after
            if changed:
                self.configuration_changed.emit()
                if self._last_revision is not None:
                    self.dataset_invalidated.emit("source_or_mapping_changed_requires_renormalization")
                self.status.setText("导入映射预设已应用；未自动导入，请检查后点击“导入并规范化”。")
            else:
                self.status.setText("预设与当前导入设置相同；现有数据修订保持不变。")
            notice_text = " ".join(notices)
            self.import_profile_status.setText(
                f"已应用“{profile.name}” r{profile.revision}。{notice_text} {_PROFILE_UNCHANGED_SUMMARY}"
            )
            self._batch_profile_confirmation = (
                profile.profile_id, profile.revision, self._profile_configuration_key()
            )
            self._refresh_performance_audit()
        except Exception as exc:
            self.import_profile_status.setText("预设未应用，当前设置保持不变：" + str(exc))
            QMessageBox.warning(self, "无法应用导入预设", str(exc))

    def _stage_import_profile(
        self, profile: ImportProfile,
    ) -> tuple[dict[str, _StagedProfileSource], list[str]]:
        stages: dict[str, _StagedProfileSource] = {}
        notices: list[str] = []
        for source in profile.sources:
            panel = self.sources[source.role]
            if not panel.source_path:
                notices.append(
                    f"{_ROLE_TITLES[source.role]}源尚未选择，未恢复该角色的文件路径或应用其映射；请添加并预览文件。"
                )
                continue
            candidate = panel.stage_profile_settings(source)
            frame = candidate["frame"]
            expected = profile.structure_signature[source.role]
            difference = compare_structure_signatures(
                {source.role: expected},
                {source.role: tuple(str(column) for column in frame.columns)},
            )
            preserved_performance = None
            if source.role == "performance":
                live = panel.performance_mapping
                if live is None:
                    raise ImportProfileError("绩效映射控件不可用。")
                current = live.value()
                missing_keys = [key for key in current["performance_join_keys"] if key not in frame.columns]
                if missing_keys:
                    raise ImportProfileError(
                        "候选绩效工作表缺少当前沿用的连接键：" + ", ".join(missing_keys)
                        + "。请先在现有绩效连接设置中调整并预览。"
                    )
                semantics, outcomes = _performance_profile_parts(source)
                editor = PerformanceMappingWidget()
                _fill_performance_widget(
                    editor,
                    frame,
                    semantics,
                    outcomes,
                    source.units,
                    row_level=str(current["row_level"]),
                    join_keys=list(current["performance_join_keys"]),
                    lock_join_settings=True,
                )
                preserved_performance = {
                    "row_level": current["row_level"],
                    "performance_join_keys": list(current["performance_join_keys"]),
                }
            else:
                subject_id = self.coordinate.subject_id.text().strip() or None
                session_id = self.coordinate.session_id.text().strip() or None
                required = self._required_for_role(source.role, subject_id, session_id)
                editor = TableMappingEditor(source.role, required_fields=required)
                editor.set_frame(frame)
                live = panel.mapping
                fallback_map = live.mapping() if live is not None else {}
                fallback_units = live.units() if live is not None else {}
                _set_table_editor_values(
                    editor,
                    source.column_map,
                    source.units,
                    fallback_units=fallback_units,
                    fallback_mapping=fallback_map,
                )
            stages[source.role] = _StagedProfileSource(
                source.role,
                panel,
                source,
                frame.copy(deep=True),
                dict(candidate["manifest"]),
                tuple(candidate["source_stat"]),
                candidate["sheet_name"],
                difference,
                editor,
                preserved_performance,
            )
        for role, panel in self.sources.items():
            if panel.source_path and role not in stages:
                notices.append(f"当前已载入的{_ROLE_TITLES[role]}未包含在预设中，其现有配置保持不变。")
        return stages, notices

    def _validate_staged_profile(
        self, profile: ImportProfile, stages: Mapping[str, _StagedProfileSource],
    ) -> None:
        signal_stage = stages.get("signals")
        if signal_stage is None or not isinstance(signal_stage.editor, TableMappingEditor):
            raise ImportProfileError("主要信号候选映射不可用。")
        signal_mapping = signal_stage.editor.mapping()
        signal_units = signal_stage.editor.units()
        validity = profile.validity_values
        missing_validity = sorted(set(validity) - set(signal_mapping))
        if missing_validity:
            raise ImportProfileError("候选信号映射未包含预设有效性字段：" + ", ".join(missing_validity))
        if profile.reading_direction_values and "reading_direction" not in signal_mapping:
            raise ImportProfileError("候选信号映射未包含预设阅读方向字段。")
        coordinate = profile.coordinate_metadata
        if coordinate.sync_confirmed and (
            not self.coordinate.sync_method.text().strip()
            or not self.coordinate.sync_refs.text().strip()
        ):
            raise ImportProfileError("预设启用了同步确认，但当前同步方法或依据引用为空；这些排除项不会由预设覆盖。")
        for eye in ("left", "right"):
            canonical = f"pupil_{eye}"
            widget_unit = profile.pupil_units_by_eye.get(eye, "").strip()
            mapped_unit = str(signal_units.get(canonical, "")).strip()
            if canonical in signal_mapping:
                if not widget_unit and not mapped_unit:
                    raise ImportProfileError(f"候选信号映射包含 {canonical}，但预设没有明确单位。")
                if widget_unit and mapped_unit and widget_unit.casefold() != mapped_unit.casefold():
                    raise ImportProfileError(f"预设中 {canonical} 的字段单位与瞳孔单位声明不一致。")
        performance_stage = stages.get("performance")
        if performance_stage is not None:
            widget = performance_stage.editor
            if not isinstance(widget, PerformanceMappingWidget):
                raise ImportProfileError("绩效候选映射控件无效。")
            value = widget.value()
            errors = _performance_profile_validation(widget, value)
            if errors:
                raise ImportProfileError("绩效候选配置不兼容：\n" + "\n".join(errors))
            # The existing audit expects canonical signal fields. Build that view
            # from explicitly selected columns rather than guessing by position.
            canonical_signals = pd.DataFrame(index=signal_stage.frame.index)
            for canonical, source_name in signal_mapping.items():
                if source_name in signal_stage.frame.columns:
                    canonical_signals[canonical] = signal_stage.frame[source_name]
            audit = widget.audit(canonical_signals)
            if audit.get("duplicate_performance_keys"):
                raise ImportProfileError("绩效候选配置的保留连接键出现重复值，预设未应用。")
            if audit.get("duplicate_signal_keys"):
                raise ImportProfileError("候选信号中的保留连接键重复，预设未应用。")
            reason = str(audit.get("reason", ""))
            if "信号表缺少对应连接字段" in reason:
                raise ImportProfileError(reason + "；绩效行粒度和连接键由当前配置保留。")

    def _coordinate_raw_state(self) -> dict[str, Any]:
        widget = self.coordinate
        text_names = (
            "time_origin", "timezone", "clock_source", "frame_id", "timecode_format",
            "subject_id", "session_id", "sync_method", "sync_refs", "coordinate_unit",
            "coordinate_frame", "pupil_left_unit", "pupil_right_unit", "device_method", "device_evidence",
        )
        return {
            "combos": {
                "time_unit": widget.time_unit.currentData(),
                "representation": widget.representation.currentData(),
                "device_status": widget.device_status.currentData(),
            },
            "checks": {
                "time_confirmed": widget.time_confirmed.isChecked(),
                "sync_confirmed": widget.sync_confirmed.isChecked(),
            },
            "text": {name: getattr(widget, name).text() for name in text_names},
            "spins": {"precision": widget.precision.value(), "frame_rate": widget.frame_rate.value()},
            "eyes": {name: edit.text() for name, edit in widget.eye_fields.items()},
            "offsets": [
                (widget.offsets.item(row, 0).text() if widget.offsets.item(row, 0) else "",
                 widget.offsets.item(row, 1).text() if widget.offsets.item(row, 1) else "")
                for row in range(widget.offsets.rowCount())
            ],
        }

    def _restore_coordinate_raw_state(self, state: Mapping[str, Any]) -> None:
        widget = self.coordinate
        for name, value in state.get("combos", {}).items():
            combo = getattr(widget, name)
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
        for name, value in state.get("checks", {}).items():
            getattr(widget, name).setChecked(bool(value))
        for name, value in state.get("text", {}).items():
            getattr(widget, name).setText(str(value))
        for name, value in state.get("spins", {}).items():
            getattr(widget, name).setValue(float(value))
        for name, value in state.get("eyes", {}).items():
            if name in widget.eye_fields:
                widget.eye_fields[name].setText(str(value))
        widget.offsets.setRowCount(0)
        for clock, offset in state.get("offsets", []):
            row = widget.offsets.rowCount()
            widget.offsets.insertRow(row)
            widget.offsets.setItem(row, 0, QTableWidgetItem(str(clock)))
            widget.offsets.setItem(row, 1, QTableWidgetItem(str(offset)))

    def _validity_raw_state(self) -> list[tuple[str, str, str]]:
        rows = []
        table = self._validity_codes.validity_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            valid = table.cellWidget(row, 1)
            invalid = table.cellWidget(row, 2)
            rows.append((
                item.text() if item else "",
                valid.text() if isinstance(valid, QLineEdit) else "",
                invalid.text() if isinstance(invalid, QLineEdit) else "",
            ))
        return rows

    def _reading_raw_state(self) -> tuple[str | None, list[tuple[str, str]]]:
        widget = self._reading_directions
        return widget._source_column, widget._snapshot_rows()

    def _profile_configuration_key(self) -> str:
        sources: dict[str, Any] = {}
        for role, panel in self.sources.items():
            state: dict[str, Any] = {
                "encoding": panel.encoding_edit.text(),
                "delimiter": panel.delimiter_combo.currentData(),
                "decimal": panel.decimal_combo.currentData(),
                "sheet": panel.sheet_combo.currentData(),
                "columns": list(panel._preview.columns),
            }
            if panel.mapping is not None:
                state["mapping"] = panel.mapping.mapping()
                state["units"] = panel.mapping.units()
            elif panel.performance_mapping is not None:
                state["performance"] = panel.performance_mapping.value()
            sources[role] = state
        state = {
            "sources": sources,
            "coordinate": self._coordinate_raw_state(),
            "validity": self._validity_raw_state(),
            "reading": self._reading_raw_state(),
        }
        return json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)

    def _capture_profile_apply_snapshot(
        self, stages: Mapping[str, _StagedProfileSource],
    ) -> dict[str, Any]:
        sources: dict[str, dict[str, Any]] = {}
        for role, stage in stages.items():
            panel = stage.panel
            state: dict[str, Any] = {
                "encoding": panel.encoding_edit.text(),
                "delimiter": panel.delimiter_combo.currentData(),
                "decimal": panel.decimal_combo.currentData(),
                "sheet": panel.sheet_combo.currentData(),
                "preview": panel._preview.copy(deep=True),
                "manifest": dict(panel._manifest),
                "preview_path": panel._preview_path,
                "preview_options": panel._preview_options,
                "preview_sheet": panel._preview_sheet,
                "status": panel.status.text(),
            }
            if panel.mapping is not None:
                state["mapping"] = panel.mapping.mapping()
                state["units"] = panel.mapping.units()
            elif panel.performance_mapping is not None:
                state["performance"] = panel.performance_mapping.value()
            sources[role] = state
        return {
            "sources": sources,
            "coordinate": self._coordinate_raw_state(),
            "validity": self._validity_raw_state(),
            "reading": self._reading_raw_state(),
        }

    def _restore_profile_apply_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        for role, state in snapshot["sources"].items():
            panel = self.sources[role]
            panel.encoding_edit.setText(state["encoding"])
            _set_combo_value(panel.delimiter_combo, state["delimiter"], "自定义分隔符")
            _set_combo_value(panel.decimal_combo, state["decimal"], "自定义小数点")
            if state["sheet"] is None:
                empty_sheet = panel.sheet_combo.findData(None)
                panel.sheet_combo.setCurrentIndex(empty_sheet if empty_sheet >= 0 else -1)
            else:
                _set_combo_value(panel.sheet_combo, state["sheet"], str(state["sheet"]))
            panel._preview = state["preview"].copy(deep=True)
            panel._manifest = dict(state["manifest"])
            panel._preview_path = state["preview_path"]
            panel._preview_options = state["preview_options"]
            panel._preview_sheet = state["preview_sheet"]
            panel.table_model.set_frame(panel._preview)
            if panel.mapping is not None:
                panel.mapping.set_frame(panel._preview)
                _set_table_editor_values(panel.mapping, state["mapping"], state["units"])
            elif panel.performance_mapping is not None:
                value = state["performance"]
                _fill_performance_widget(
                    panel.performance_mapping,
                    panel._preview,
                    value["semantic_fields"],
                    list(value["performance_columns"]),
                    value["performance_units"],
                    row_level=str(value["row_level"]),
                    join_keys=list(value["performance_join_keys"]),
                    lock_join_settings=False,
                )
            panel.status.setText(state["status"])
        self._restore_coordinate_raw_state(snapshot["coordinate"])
        signal_panel = self.sources["signals"]
        signal_map = signal_panel.mapping.mapping() if signal_panel.mapping else {}
        self._validity_codes.configure_validity_fields(signal_map, signal_panel._preview)
        for row, (name, valid, invalid) in enumerate(snapshot["validity"]):
            if row >= self._validity_codes.validity_table.rowCount():
                break
            table = self._validity_codes.validity_table
            valid_edit = table.cellWidget(row, 1)
            invalid_edit = table.cellWidget(row, 2)
            if isinstance(valid_edit, QLineEdit):
                valid_edit.setText(valid)
            if isinstance(invalid_edit, QLineEdit):
                invalid_edit.setText(invalid)
        source_column, reading_rows = snapshot["reading"]
        self._reading_directions.set_values(source_column, dict(reading_rows))

    def _set_coordinate_profile(self, metadata: CoordinateMetadata, pupil_units: Mapping[str, str]) -> None:
        widget = self.coordinate
        _set_combo_value(widget.time_unit, metadata.time_unit, metadata.time_unit)
        _set_combo_value(widget.representation, metadata.coordinate_representation,
                         str(metadata.coordinate_representation))
        _set_combo_value(widget.device_status, metadata.device_validation_status,
                         str(metadata.device_validation_status))
        widget.time_origin.setText(metadata.time_origin or "")
        widget.timezone.setText(metadata.timezone or "")
        widget.clock_source.setText(metadata.clock_source or "")
        widget.sync_confirmed.setChecked(bool(metadata.sync_confirmed))
        widget.precision.setValue(float(metadata.time_precision_s or 0.0))
        widget.frame_id.setText(metadata.frame_id or "")
        widget.frame_rate.setValue(float(metadata.frame_rate_hz or 0.0))
        widget.timecode_format.setText(metadata.timecode_format or "")
        widget.coordinate_unit.setText(metadata.coordinate_unit)
        widget.coordinate_frame.setText(metadata.coordinate_frame)
        widget.pupil_left_unit.setText(str(pupil_units.get("left", "")))
        widget.pupil_right_unit.setText(str(pupil_units.get("right", "")))
        widget.device_method.setText(metadata.device_validation_method or "")
        widget.device_evidence.setText("; ".join(metadata.device_validation_evidence))
        for side, eye in (("left", metadata.left_eye), ("right", metadata.right_eye)):
            for rep in ("point", "direction", "origin"):
                for attr in ("unit", "frame"):
                    name = f"{side}_{rep}_{attr}"
                    widget.eye_fields[name].setText(str(getattr(eye, f"{rep}_{attr}")))
        widget.offsets.setRowCount(0)
        for clock, offset in metadata.clock_offsets_s.items():
            row = widget.offsets.rowCount()
            widget.offsets.insertRow(row)
            widget.offsets.setItem(row, 0, QTableWidgetItem(str(clock)))
            widget.offsets.setItem(row, 1, QTableWidgetItem(str(offset)))

    def _apply_staged_profile(
        self, profile: ImportProfile, stages: Mapping[str, _StagedProfileSource],
    ) -> None:
        for role, stage in stages.items():
            panel = stage.panel
            source = stage.profile_source
            current_stat = Path(panel.source_path).stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != stage.source_stat:
                raise ImportProfileError(f"{_ROLE_TITLES[role]}文件在确认后发生变化，请重新预览并应用。")
            panel.encoding_edit.setText(source.csv_options.encoding)
            _set_combo_value(panel.delimiter_combo, source.csv_options.delimiter, "自定义分隔符")
            _set_combo_value(panel.decimal_combo, source.csv_options.decimal, "自定义小数点")
            if panel.source_path.lower().endswith(".xlsx") and stage.sheet_name is not None:
                _set_combo_value(panel.sheet_combo, stage.sheet_name, stage.sheet_name)
            panel._preview = stage.frame.copy(deep=True)
            panel._manifest = dict(stage.manifest)
            panel._preview_path = panel.source_path
            panel._preview_options = source.csv_options
            panel._preview_sheet = stage.sheet_name
            panel.table_model.set_frame(panel._preview)
            if panel.mapping is not None and isinstance(stage.editor, TableMappingEditor):
                subject_id = self.coordinate.subject_id.text().strip() or None
                session_id = self.coordinate.session_id.text().strip() or None
                required = self._required_for_role(role, subject_id, session_id)
                panel.mapping.set_required_fields(required or ())
                panel.mapping.set_frame(panel._preview)
                _set_table_editor_values(
                    panel.mapping,
                    stage.editor.mapping(),
                    stage.editor.units(),
                )
            elif panel.performance_mapping is not None and isinstance(stage.editor, PerformanceMappingWidget):
                value = stage.editor.value()
                _fill_performance_widget(
                    panel.performance_mapping,
                    panel._preview,
                    value["semantic_fields"],
                    list(value["performance_columns"]),
                    value["performance_units"],
                    row_level=str(value["row_level"]),
                    join_keys=list(value["performance_join_keys"]),
                    lock_join_settings=False,
                )
            panel.status.setText(
                f"{panel._manifest.get('source_name', Path(panel.source_path).name)} · "
                f"{len(panel._preview):,} 行候选预览 · 工作表 {stage.sheet_name or 'CSV'} · "
                f"SHA-256 {str(panel._manifest.get('sha256', ''))[:16]}"
            )
        signal_stage = stages["signals"]
        signal_mapping = signal_stage.editor.mapping()
        self._validity_codes.configure_validity_fields(signal_mapping, signal_stage.frame)
        self._validity_codes.set_values(profile.validity_values)
        self._reading_directions.set_values(
            signal_mapping.get("reading_direction"), profile.reading_direction_values,
        )
        self._set_coordinate_profile(profile.coordinate_metadata, profile.pupil_units_by_eye)

    def _context_configuration_changed(self) -> None:
        if self._applying_import_profile:
            return
        self._invalidate_active_import("上下文资料配置已变化")
        self.configuration_changed.emit()

    def _invalidate_active_import(self, reason: str) -> None:
        if self._worker is None or self._import_token is None:
            return
        self._request_generation += 1
        self._import_token.cancel()
        self._pending_worker_message = f"{reason}；已请求取消，等待当前读取结束，迟到结果不会安装。"
        self.status.setText(self._pending_worker_message)
        self.cancel_import_button.setEnabled(False)
        self.import_state_changed.emit(True, self._pending_worker_message)

    def cancel_import(self) -> bool:
        if self._worker is None or self._import_token is None or self._import_token():
            return False
        self._request_generation += 1
        self._import_token.cancel()
        self._pending_worker_message = "已请求取消导入；等待当前文件读取结束，先前有效数据保持不变。"
        self.status.setText(self._pending_worker_message)
        self.cancel_import_button.setEnabled(False)
        self.import_state_changed.emit(True, self._pending_worker_message)
        return True

    def _source_changed(self, role: str) -> None:
        self._invalidate_aoi_mapping_binding(
            f"{_ROLE_TITLES.get(role, role)} 来源或映射已变化；请重新导入并核对 AOI 映射。"
        )
        if self._applying_import_profile or self._staging_aoi_editor_source:
            return
        self._invalidate_active_import(f"{_ROLE_TITLES.get(role, role)} 来源或映射已变化")
        if role == "aois" and self._aoi_editor_pending_import is not None:
            expected = str(self._aoi_editor_pending_import.get("csv_path", "")).casefold()
            current = str(Path(self.sources["aois"].source_path).resolve()).casefold()
            if expected and current == expected:
                # Keep the installed DatasetRevision until the staged AOI source
                # passes the ordinary ImportWorker and normalization path.
                self.configuration_changed.emit()
                return
            self._aoi_editor_pending_import = None
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
            if not self.import_active:
                self.status.setText("AOI 草稿待验证；主要信号仍可用于不依赖 AOI 的分析。")
            self.configuration_changed.emit()
            return
        self.configuration_changed.emit()
        if self._last_revision is not None:
            if not self.import_active:
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
        if (role == "aois" and isinstance(frame, pd.DataFrame)
                and self._aoi_editor_pending_import is not None
                and str(Path(self.sources["aois"].source_path).resolve()).casefold()
                == str(self._aoi_editor_pending_import.get("csv_path", "")).casefold()):
            panel = self.sources["aois"]
            if panel.mapping is not None:
                expected = (
                    "aoi_id", "stimulus_id", "shape", "aoi_name", "xmin", "ymin", "xmax",
                    "ymax", "vertices", "priority", "overlap_policy",
                )
                column_map = {name: name for name in expected if name in panel.mapping.source_headers}
                self._applying_import_profile = True
                try:
                    _set_table_editor_values(panel.mapping, column_map, {})
                finally:
                    self._applying_import_profile = False
            stimuli = dict(self._aoi_editor_pending_import.get("image_metadata", {})).get("stimuli", {})
            frames = sorted({
                _aoi_editor_frame_name(value)
                for value in stimuli.values() if isinstance(value, Mapping)
            }) if isinstance(stimuli, Mapping) else []
            frame_note = "; ".join(frames) if frames else "坐标帧未验证"
            panel.status.setText(
                "编辑器 CSV 已预览并按规范字段预填映射。AOI 使用图像自然像素；信号单位须明确为 px，"
                f"坐标 frame 需逐字匹配图像声明（{frame_note}），否则现有 preflight 会阻断 AOI 指标。"
                "不会自动缩放、翻转或改坐标。"
            )

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
        self._invalidate_aoi_mapping_binding(
            "时间、坐标或设备声明已变化；重新导入后请核对 AOI 映射。"
        )
        if self._applying_import_profile:
            return
        self._normalization_configuration_changed(
            "time_or_coordinate_declaration_changed", "时间、坐标或设备声明已变化"
        )

    def _normalization_configuration_changed(self, reason: str, description: str) -> None:
        if self._applying_import_profile:
            return
        self._invalidate_aoi_mapping_binding(
            f"{description}；重新导入后请核对 AOI 映射。"
        )
        self._invalidate_active_import(description)
        self.configuration_changed.emit()
        if self._last_revision is not None:
            if not self.import_active:
                self.status.setText(f"{description}；请重新导入并规范化，现有数据修订仍绑定旧声明。")
            self.dataset_invalidated.emit(reason)

    def _scope_changed(self) -> None:
        self._invalidate_active_import("作用域、同步或 timebase 配置已变化")
        self.configuration_changed.emit()
        if self._last_revision is not None and not self.sources["segments"].source_path:
            if not self.import_active:
                self.status.setText("作用域/同步/timebase 已变化；如需分析共享段，请重新导入并规范化 segments。")
        elif self._last_revision is not None:
            if not self.import_active:
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
                eye_label = "左瞳孔" if eye == "left" else "右瞳孔"
                raise ValueError(f"{eye_label}的单位不一致：列映射表填了 {mapping_value}，时间与单位面板填了 {widget_value}。请按设备说明把两处改为同一单位，例如 mm。")
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
        request = _ImportRequest(tuple(sources), coordinate, self.coordinate.time_confirmed.isChecked(),
                                 subject_id, session_id, validity_values, reading_direction_values,
                                 synchronization_method, synchronization_source_refs, pupil_units_by_eye,
                                 scope, member_map, dataset_scope, aoi_policy, aoi_rule_ref,
                                 "allow_legacy_missing" if self.sources["aois"].aoi_legacy_name.isChecked() else "require_source")
        pending = self._aoi_editor_pending_import
        aoi_source = next((item for item in request.sources if item.role == "aois"), None)
        if pending is not None and aoi_source is not None:
            expected = str(pending.get("csv_path", "")).casefold()
            actual = str(Path(aoi_source.path).resolve()).casefold()
            if expected and actual == expected:
                request = replace(request, aoi_editor_provenance=copy.deepcopy(pending))
        return request

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
        if self._batch_review_mode:
            self.status.setText("批量队列等待映射核对；请在队列中完成并确认该文件的导入映射。")
            return
        if self._external_busy:
            self.status.setText("请等待当前分析、归档或导出任务结束后再导入。")
            return
        if self._worker is not None:
            self.status.setText("已有导入任务运行中。")
            return
        try:
            request = self._build_request()
        except Exception as exc:
            self.status.setText("导入配置尚未完成：" + str(exc))
            return
        self._start_import_request(request, allow_external_busy=False)

    def batch_profile_and_request(self) -> tuple[ImportProfile, _ImportRequest]:
        """Return the explicitly confirmed profile and an immutable import draft."""
        if self._profile_operation_blocked():
            raise ImportProfileError("导入页正忙；请等待当前操作结束后再准备批量队列。")
        profile_id = self.import_profile_combo.currentData()
        profile = self._profiles_by_id.get(str(profile_id)) if profile_id else None
        if profile is None:
            raise ImportProfileError("批量分析前，请先选择一个有效的导入映射预设。")
        expected_confirmation = (
            profile.profile_id, profile.revision, self._profile_configuration_key()
        )
        if self._batch_profile_confirmation != expected_confirmation:
            raise ImportProfileError("当前导入设置尚未由所选预设确认；请先预览并应用预设，或保存当前映射。")
        request = self._build_request()
        if not any(source.role == "signals" for source in request.sources):
            raise ImportProfileError("当前没有已确认的主要信号映射。")
        if request.scope.mode == "shared_interaction":
            raise ImportProfileError("批量队列按文件分别分析；共享互动/群体分析请使用独立群体工作流。")
        return profile, copy.deepcopy(request)

    def batch_request_for_source(self, request: _ImportRequest, source_path: str) -> _ImportRequest:
        """Copy the frozen import draft and substitute only this item's signals path."""
        normalized_path = str(Path(source_path).expanduser().resolve())
        found = False
        sources = []
        for source in request.sources:
            if source.role == "signals":
                sources.append(replace(source, path=normalized_path))
                found = True
            else:
                sources.append(source)
        if not found:
            raise ImportProfileError("冻结的导入请求缺少主要信号源。")
        return replace(request, sources=tuple(sources))

    def start_batch_import(self, request: _ImportRequest) -> int:
        """Start the ordinary normalization worker for a queued signals source."""
        return self._start_import_request(request, allow_external_busy=True)

    def _start_import_request(self, request: _ImportRequest, *, allow_external_busy: bool) -> int:
        if self._external_busy and not allow_external_busy:
            self.status.setText("请等待当前分析、归档或导出任务结束后再导入。")
            return 0
        if self._worker is not None:
            self.status.setText("已有导入任务运行中。")
            return 0
        self._request_generation += 1
        generation = self._request_generation
        token = _ImportCancellationToken()
        self._worker_generation = generation
        self._import_token = token
        self._pending_worker_message = ""
        # The worker receives an immutable snapshot of the page's current draft.
        self._worker = _NormalizeWorker(generation, copy.deepcopy(request), token)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.completed.connect(self._on_import_completed)
        self._worker.signals.failed.connect(self._on_import_failed)
        self._worker.signals.cancelled.connect(self._on_import_cancelled)
        self._worker.signals.finished.connect(self._on_import_finished)
        self.import_button.setEnabled(False)
        self._update_aoi_editor_button()
        self.cancel_import_button.setEnabled(True)
        self._update_import_profile_controls()
        self._update_aoi_mapping_controls()
        self.status.setText("后台读取并调用公开 normalize_* 接口；源文件不会在 GUI 线程读取。")
        # Reset only when a new import generation actually starts. Cancellation
        # and invalidation notifications keep the current task's progress state.
        self.import_progress.emit(0, "正在导入并规范化")
        self.import_state_changed.emit(True, "正在导入并规范化")
        QThreadPool.globalInstance().start(self._worker)
        return generation

    def _is_current_import(self, generation: int) -> bool:
        return (generation == self._request_generation
                and generation == self._worker_generation
                and self._import_token is not None
                and not self._import_token())

    @Slot(int, int, str)
    def _on_progress(self, generation: int, percent: int, message: str) -> None:
        if not self._is_current_import(generation):
            return
        self.import_progress.emit(percent, message)
        self.status.setText(f"{percent}% · {message}")

    @Slot(int, object, object)
    def _on_import_completed(self, generation: int, revision: object, preview: object) -> None:
        if not self._is_current_import(generation):
            return
        if not isinstance(revision, DatasetRevision) or not isinstance(preview, pd.DataFrame):
            self.status.setText("导入 worker 返回内容无效。")
            return
        self._last_revision = revision
        self._aoi_confirmation_eligible_revision_id = (
            revision.revision_id
            if not self._external_busy and not self._batch_review_mode
            else None
        )
        self._aoi_confirmation_revision = revision
        self._aoi_confirmation_binding = None
        self._aoi_confirmation_timestamp = None
        self._aoi_confirmation_notice = "尚未确认；请核对本次导入的数据与 AOI 来源。"
        blocker = QSignalBlocker(self.sources["aois"].aoi_mapping_confirmation)
        self.sources["aois"].aoi_mapping_confirmation.setChecked(False)
        del blocker
        self._aoi_editor_pending_import = None
        self.import_worker_completed.emit(generation, revision, preview)
        self.status.setText(
            f"数据修订 {revision.revision_id} 已就绪 · {len(preview):,} 行预览 · "
            f"来源/映射/单位及原始字节哈希已绑定 · 设备验证仍按用户声明记录"
        )
        self.import_completed.emit(revision, preview)
        self._update_aoi_mapping_controls()
        metadata = revision.dataset_copy().metadata
        if metadata.get("aoi_import_status") == "invalid_draft":
            issues = metadata.get("aoi_import_issues", [])
            lines = [f"{item.get('source_file')} / {item.get('sheet')} / 源行 {item.get('source_row')} / {item.get('canonical_field')}：{item.get('code')} — {item.get('message')}" for item in issues]
            self.sources["aois"].status.setText("AOI 草稿无效；主要信号已保留，只有依赖 AOI 的方法不可用。\n" + "\n".join(lines))
            self.status.setText("主要信号已导入；AOI 草稿未纳入有效几何，请查看 ROI/AOI 页的具体来源问题。")
        elif isinstance(metadata.get("aoi_editor_provenance"), Mapping):
            provenance = metadata["aoi_editor_provenance"]
            verification = str(provenance.get("verification_status", "unverifiable"))
            coordinate_status = str(provenance.get("coordinate_mapping_status", "unknown"))
            verification_text = "图片与 manifest 哈希已核对" if verification == "verified" else "图片来源不可验证，详情见修订元数据"
            coordinate_text = (
                "信号和图片使用同一已声明像素 frame"
                if coordinate_status == "declared_same_image_pixel_frame"
                else "图像坐标未与信号坐标匹配，依赖 AOI 的指标由现有 preflight 阻断"
            )
            self.sources["aois"].status.setText(
                f"静态图片 AOI 已随修订导入；{verification_text}；{coordinate_text}。未执行坐标变换。"
            )

    @Slot(int, str)
    def _on_import_failed(self, generation: int, message: str) -> None:
        if not self._is_current_import(generation):
            return
        self.status.setText("导入失败：" + message)
        self.import_worker_failed.emit(generation, message)
        self.import_failed.emit(message)

    @Slot(int)
    def _on_import_cancelled(self, generation: int) -> None:
        if generation != self._worker_generation:
            return
        if self._pending_worker_message:
            self.status.setText(self._pending_worker_message)
        else:
            self.status.setText("导入已取消；先前有效数据保持不变。")
        self.import_worker_cancelled.emit(generation)

    @Slot(int)
    def _on_import_finished(self, generation: int) -> None:
        # Even invalidated workers must release their slot before another import
        # starts; only this worker's own generation may clear the active state.
        if generation != self._worker_generation:
            return
        pending_message = self._pending_worker_message
        self._worker = None
        self._worker_generation = None
        self._import_token = None
        self._pending_worker_message = ""
        if pending_message:
            if pending_message.startswith("已请求取消导入"):
                self.status.setText("导入已取消；先前有效数据保持不变。")
            else:
                reason = pending_message.split("；", 1)[0]
                self.status.setText(f"{reason}；后台读取已结束，本次结果未安装。")
        self.cancel_import_button.setEnabled(False)
        self.import_button.setEnabled(not self._external_busy)
        self._update_aoi_editor_button()
        self._update_aoi_mapping_controls()
        self._update_import_profile_controls()
        self.import_state_changed.emit(False, "")
        self.import_idle.emit()
        self.import_worker_finished.emit(generation)

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
