"""Read-only results, charts, run provenance, and history review page."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRunnable,
    QSortFilterProxyModel,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pyeyedata import AnalysisBundle

from .charts import ResultChartPanel
from .models import RunRecord, RunSnapshot
from .task_timing import TaskTiming, TimingEstimator, format_duration, format_timing


def _write_figure_output(destination, image, provenance):
    """Commit a Figure and its provenance together, restoring old outputs on error."""
    import os
    import tempfile
    from pyeyedata.exports import _commit_output_group
    destination = Path(destination)
    payloads = ((destination, image),
                (Path(str(destination) + ".provenance.json"),
                 json.dumps(dict(provenance), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")))
    stages = []
    try:
        for target, payload in payloads:
            fd, temporary = tempfile.mkstemp(prefix="." + target.stem, suffix=".tmp", dir=target.parent)
            stage = Path(temporary)
            stages.append((stage, target))
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        # Use the same already checked grouped rollback path as CSV sidecars.
        _commit_output_group([(target, stage) for stage, target in stages], overwrite=True)
    finally:
        for stage, _target in stages:
            stage.unlink(missing_ok=True)


_MISSING = object()
_IDENTITY_COLUMNS = (
    "run_id", "metric_id", "subject_id", "session_id", "interaction_id", "pair_id",
    "segment_id", "segment_instance_id", "trial", "condition", "analysis_stream_id",
    "eye_label", "signal", "event_id", "observation_id",
)
_QUALITY_METRIC_PREFIXES = (
    "individual.coverage.", "individual.data_loss.", "individual.sampling.",
    "individual.calibration.", "individual.signal.", "individual.usable_duration",
)
_STATUS_LABELS = {
    "complete": "已完成",
    "partial": "部分完成",
    "cancelled": "已取消",
    "canceled": "已取消",
    "failed": "失败",
    "unknown": "状态未知",
    "ready": "可开始",
    "running": "运行中",
    "pending": "等待中",
}
_PHASE_LABELS = {
    "preflight": "运行前检查",
    "run": "分析运行",
    "save": "保存结果",
    "load": "打开结果",
}


def _status_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw or "unknown").strip().casefold()
    return _STATUS_LABELS.get(normalized, str(raw or "未知"))


def _phase_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw or "").strip().casefold()
    return _PHASE_LABELS.get(normalized, str(raw or "运行"))


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _display_value(value: Any) -> str:
    if _missing(value):
        return "NA"
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(float(value)):
            return "NA"
        return f"{float(value):.7g}"
    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _safe_value(value: Any) -> Any:
    """Convert nested run state to bounded, JSON-safe provenance text."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return _safe_value(value.item())
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, bytearray):
        content = bytes(value)
        return {"type": "bytes", "length": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    if isinstance(value, pd.DataFrame):
        return {
            "type": "DataFrame", "rows": int(len(value)), "columns": [str(item) for item in value.columns],
            "dtypes": {str(column): str(dtype) for column, dtype in value.dtypes.items()},
        }
    if isinstance(value, pd.Series):
        return {"type": "Series", "rows": int(len(value)), "name": str(value.name), "dtype": str(value.dtype)}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _safe_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _safe_value(value.to_dict())
        except (TypeError, ValueError):
            pass
    return str(value)


def _json_text(value: Any) -> str:
    return json.dumps(_safe_value(value), ensure_ascii=False, indent=2, sort_keys=True)


class _ResultsTableModel(QAbstractTableModel):
    """Lazy, read-only DataFrame model; it creates no per-cell widgets."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._frame = pd.DataFrame()

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        self.beginResetModel()
        self._frame = frame.copy(deep=True) if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else int(len(self._frame.index))

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else int(len(self._frame.columns))

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._frame) or index.column() >= len(self._frame.columns):
            return None
        value = self._frame.iat[index.row(), index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return _display_value(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and pd.api.types.is_numeric_dtype(self._frame.dtypes.iloc[index.column()]):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            column = str(self._frame.columns[index.column()])
            reason = ""
            for reason_column in ("reason_code", "reason"):
                if reason_column in self._frame:
                    reason_value = self._frame.iloc[index.row()][reason_column]
                    if not _missing(reason_value):
                        reason = f"\n{reason_column}: {_display_value(reason_value)}"
                        break
            return f"{column}: {_display_value(value)}{reason}"
        if role == Qt.ItemDataRole.UserRole:
            return self.row_identity(index.row())
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._frame.columns):
            return str(self._frame.columns[section])
        if orientation == Qt.Orientation.Vertical and 0 <= section < len(self._frame.index):
            return str(self._frame.index[section])
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def row_identity(self, row: int) -> str:
        record = self._frame.iloc[row]
        columns = [name for name in _IDENTITY_COLUMNS if name in self._frame.columns and not _missing(record[name])]
        if columns:
            identity = {name: _safe_value(record[name]) for name in columns}
        else:
            # Tables without a backend ID use a content key, never a sorted row number.
            identity = {str(name): _safe_value(value) for name, value in record.items()}
        packed = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(packed.encode("utf-8")).hexdigest()

    def selected_frame(self, rows: Sequence[int]) -> pd.DataFrame:
        unique_rows = sorted(set(int(row) for row in rows if 0 <= int(row) < len(self._frame)))
        return self._frame.iloc[unique_rows].copy(deep=True)


class _MaskProxyModel(QSortFilterProxyModel):
    """Sort proxy whose expensive text search is computed off the GUI thread."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mask: np.ndarray | None = None
        self.setDynamicSortFilter(True)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_mask(self, mask: np.ndarray | None) -> None:
        self._mask = mask
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._mask is None:
            return True
        return source_row < len(self._mask) and bool(self._mask[source_row])

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        model = self.sourceModel()
        a = model._frame.iat[left.row(), left.column()]
        b = model._frame.iat[right.row(), right.column()]
        if _missing(a):
            return not _missing(b)
        if _missing(b):
            return False
        if isinstance(a, (int, float, np.number)) and isinstance(b, (int, float, np.number)):
            try:
                return float(a) < float(b)
            except (TypeError, ValueError, OverflowError):
                pass
        return str(a).casefold() < str(b).casefold()


class _FilterSignals(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)


class _FilterWorker(QRunnable):
    def __init__(
        self,
        generation: int,
        frame: pd.DataFrame,
        search: str,
        filters: Mapping[str, str],
        parent: QObject,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.generation = generation
        self.frame = frame
        self.search = search
        self.filters = dict(filters)
        self.signals = _FilterSignals(parent)

    def run(self) -> None:
        try:
            frame = self.frame
            mask = np.ones(len(frame), dtype=bool)
            if self.search:
                search_mask = np.zeros(len(frame), dtype=bool)
                for column in frame.columns:
                    values = frame[column].astype("string")
                    search_mask |= values.str.contains(self.search, case=False, regex=False, na=False).to_numpy(dtype=bool)
                mask &= search_mask
            for column, term in self.filters.items():
                if column not in frame:
                    mask &= False
                    continue
                values = frame[column].astype("string")
                mask &= values.str.contains(term, case=False, regex=False, na=False).to_numpy(dtype=bool)
        except Exception as exc:
            self.signals.failed.emit(self.generation, f"{type(exc).__name__}: {exc}")
            return
        self.signals.finished.emit(self.generation, mask)


class _ResultTableTab(QWidget):
    """One lazily rendered table/chart pair with async filters and selected export."""

    selected_exported = Signal(str, int, object)
    remediation_requested = Signal(object, int)

    def __init__(self, tab_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tab_name = tab_name
        self._options: dict[str, pd.DataFrame] = {}
        self._all_tables: dict[str, pd.DataFrame] = {}
        self._semantics: dict[str, Any] = {}
        self._filter_generation = 0
        self._active_filters: dict[str, str] = {}
        self._selected_row_ids: tuple[str, ...] = ()
        self._filter_timing_estimator = TimingEstimator(default_seconds={"filter": 5.0})
        self._filter_timing: TaskTiming | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        toolbar = QHBoxLayout()
        self.table_choice = QComboBox(self)
        self.table_choice.setObjectName(f"{tab_name}TableChoice")
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("在当前表中搜索（后台处理）")
        self.search.setClearButtonEnabled(True)
        self.filter_column = QComboBox(self)
        self.filter_value = QLineEdit(self)
        self.filter_value.setPlaceholderText("筛选值，多个列条件按 AND 组合")
        self.add_filter_button = QPushButton("添加筛选", self)
        self.clear_filter_button = QPushButton("清除筛选", self)
        self.copy_button = QPushButton("复制选中单元格", self)
        self.export_button = QPushButton("导出选中行 CSV/XLSX", self)
        self.filtered_export_button = QPushButton("导出当前筛选 CSV/XLSX", self)
        self.save_chart_button = QPushButton("保存图表 PNG/SVG/PDF", self)
        self.remediation_button = QPushButton("查看此行原因 / 处理指引", self)
        self.remediation_button.setObjectName(f"{tab_name}RemediationAction")
        for widget in (
            self.table_choice, self.search, self.filter_column, self.filter_value,
            self.add_filter_button, self.clear_filter_button,
        ):
            toolbar.addWidget(widget)
        root.addLayout(toolbar)
        export_row = QHBoxLayout()
        for widget in (self.copy_button, self.export_button, self.filtered_export_button,
                       self.save_chart_button, self.remediation_button):
            export_row.addWidget(widget)
        export_row.addStretch(1)
        root.addLayout(export_row)

        self.filter_state = QLabel("沒有筛选条件。", self)
        self.filter_state.setWordWrap(True)
        root.addWidget(self.filter_state)
        self.filter_timing_label = QLabel("", self)
        self.filter_timing_label.setObjectName(f"{tab_name}FilterTiming")
        self.filter_timing_label.setWordWrap(True)
        self.filter_timing_label.setVisible(False)
        root.addWidget(self.filter_timing_label)
        self.table_view = QTableView(self)
        self.table_view.setObjectName(f"{tab_name}ResultsTable")
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSortingEnabled(True)
        self.table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectItems)
        self.table_view.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table_view.setWordWrap(False)
        self.table_view.setMinimumHeight(160)
        self.table_view.verticalHeader().setDefaultSectionSize(24)
        self._source_model = _ResultsTableModel(self)
        self._proxy_model = _MaskProxyModel(self)
        self._proxy_model.setSourceModel(self._source_model)
        self.table_view.setModel(self._proxy_model)
        root.addWidget(self.table_view, 3)
        self.chart = ResultChartPanel(self)
        self.chart.setMinimumHeight(340)
        root.addWidget(self.chart, 2)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._start_filter)
        self._filter_timing_timer = QTimer(self)
        self._filter_timing_timer.setInterval(1000)
        self._filter_timing_timer.timeout.connect(self._refresh_filter_timing)
        self.search.textChanged.connect(self._schedule_filter)
        self.add_filter_button.clicked.connect(self._add_filter)
        self.clear_filter_button.clicked.connect(self._clear_filters)
        self.table_choice.currentIndexChanged.connect(self._select_table)
        self.copy_button.clicked.connect(self.copy_selection)
        self.export_button.clicked.connect(self.export_selected)
        self.filtered_export_button.clicked.connect(self.export_filtered)
        self.save_chart_button.clicked.connect(self.save_chart)
        self.remediation_button.clicked.connect(self._request_remediation)
        self.table_view.selectionModel().selectionChanged.connect(self._selection_changed)
        self.export_button.setEnabled(False)
        self.save_chart_button.setEnabled(False)
        self._pool = QThreadPool.globalInstance()
        for widget in (self.table_choice, self.search, self.filter_column, self.filter_value,
                       self.add_filter_button, self.clear_filter_button, self.copy_button,
                       self.export_button, self.filtered_export_button, self.save_chart_button,
                       self.remediation_button):
            widget.ensurePolished()
            widget.setMinimumHeight(widget.sizeHint().height())
        self._show_empty("没有可显示的结果表。")

    def _show_empty(self, message: str) -> None:
        self._advance_filter_generation()
        self._options = {}
        self._all_tables = {}
        self._source_model.set_frame(None)
        self._proxy_model.set_mask(None)
        self.table_choice.blockSignals(True)
        self.table_choice.clear()
        self.table_choice.addItem(message, "")
        self.table_choice.setEnabled(False)
        self.table_choice.blockSignals(False)
        self.filter_column.clear()
        self.filter_column.setEnabled(False)
        self.search.clear()
        self.search.setEnabled(False)
        self.filter_value.clear()
        self.filter_value.setEnabled(False)
        self.add_filter_button.setEnabled(False)
        self.clear_filter_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.filtered_export_button.setEnabled(False)
        self.save_chart_button.setEnabled(False)
        self.remediation_button.setEnabled(False)
        self.chart.set_data("", None)
        self.filter_state.setText(message)

    def set_options(
        self,
        options: Mapping[str, pd.DataFrame],
        *,
        all_tables: Mapping[str, pd.DataFrame] | None = None,
        semantics: Mapping[str, Any] | None = None,
    ) -> None:
        self._advance_filter_generation()
        self._options = {str(name): frame for name, frame in options.items() if isinstance(frame, pd.DataFrame)}
        self._all_tables = {str(name): frame for name, frame in (all_tables or {}).items() if isinstance(frame, pd.DataFrame)}
        self._semantics = dict(semantics or {})
        self._active_filters.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.filter_value.clear()
        if not self._options:
            self._show_empty("该运行没有返回此类数据；没有创建占位结果表。")
            return
        self.table_choice.blockSignals(True)
        self.table_choice.clear()
        for name, frame in self._options.items():
            self.table_choice.addItem(f"{name} · {len(frame):,} 行", name)
        self.table_choice.setEnabled(True)
        self.table_choice.setCurrentIndex(0)
        self.table_choice.blockSignals(False)
        self._load_option(0)

    def _select_table(self, index: int) -> None:
        if index >= 0:
            self._load_option(index)

    def _load_option(self, index: int) -> None:
        name = str(self.table_choice.itemData(index) or "")
        frame = self._options.get(name)
        if frame is None:
            self._show_empty("该表没有返回。")
            return
        self._advance_filter_generation()
        self._active_filters.clear()
        self._source_model.set_frame(frame)
        self._proxy_model.set_mask(None)
        columns = [str(column) for column in self._source_model._frame.columns]
        self.filter_column.blockSignals(True)
        self.filter_column.clear()
        self.filter_column.addItems(columns)
        self.filter_column.setEnabled(bool(columns))
        self.filter_column.blockSignals(False)
        has_rows = len(frame) > 0
        self.filtered_export_button.setEnabled(True)
        for widget in (self.search, self.filter_value, self.add_filter_button, self.clear_filter_button):
            widget.setEnabled(has_rows)
        self.search.setEnabled(has_rows)
        self.filter_value.setEnabled(has_rows)
        self.add_filter_button.setEnabled(has_rows)
        self.clear_filter_button.setEnabled(has_rows)
        self.filter_state.setText(self._count_text())
        self._update_chart()
        self._selection_changed()

    def _schedule_filter(self, _text: str = "") -> None:
        if self.table_choice.isEnabled():
            self._advance_filter_generation()
            self.filter_state.setText("正在等待筛选输入…")
            self._filter_timer.start()

    def _add_filter(self) -> None:
        column = str(self.filter_column.currentText() or "")
        value = self.filter_value.text().strip()
        if not column or not value:
            self.filter_state.setText("组合筛选需要一个列名和非空筛选值。")
            return
        self._active_filters[column] = value
        self.filter_value.clear()
        self._advance_filter_generation()
        self._start_filter()

    def _clear_filters(self) -> None:
        self._active_filters.clear()
        self.search.clear()
        self._advance_filter_generation()
        self._start_filter()

    def _start_filter(self) -> None:
        if not self._options:
            return
        frame = self._source_model._frame
        search = self.search.text().strip()
        generation = self._filter_generation
        if not search and not self._active_filters:
            self._proxy_model.set_mask(None)
            self.filter_state.setText(self._count_text())
            self._update_chart()
            return
        self.filter_state.setText("正在后台计算组合筛选；结果表和图表会同步更新。")
        self._start_filter_timing(generation)
        worker = _FilterWorker(generation, frame, search, self._active_filters, self)
        worker.signals.finished.connect(self._filter_finished)
        worker.signals.failed.connect(self._filter_failed)
        try:
            self._pool.start(worker)
        except Exception as exc:
            self._filter_failed(generation, f"{type(exc).__name__}: {exc}")
            raise

    def _filter_finished(self, generation: int, mask: object) -> None:
        if generation != self._filter_generation:
            return
        if not isinstance(mask, np.ndarray):
            self._filter_failed(generation, "筛选工作线程返回了无效结果")
            return
        timing = self._filter_timing
        if timing is not None and timing.identity == generation and timing.snapshot().status == "running":
            self._finish_filter_timing("complete", train=True)
        self._proxy_model.set_mask(mask)
        self.filter_state.setText(self._count_text())
        self._update_chart()
        self._selection_changed()

    def _filter_failed(self, generation: int, message: str) -> None:
        if generation != self._filter_generation:
            return
        timing = self._filter_timing
        if timing is not None and timing.identity == generation and timing.snapshot().status == "running":
            self._finish_filter_timing("failed", train=False)
        self.filter_state.setText(f"后台筛选失败：{message}；保留当前结果视图。")

    def _advance_filter_generation(self) -> int:
        self._filter_timer.stop()
        self._filter_generation += 1
        timing = self._filter_timing
        if timing is not None:
            if timing.snapshot().status in {"running", "cancelling"}:
                timing.finish("superseded", train=False)
                self._filter_timing_timer.stop()
                self._refresh_filter_timing()
            else:
                self._filter_timing = None
                self._filter_timing_timer.stop()
                self._refresh_filter_timing()
        return self._filter_generation

    def _start_filter_timing(self, generation: int) -> None:
        # Each table tab owns its estimator, so other result tabs never share samples.
        timing = TaskTiming(
            self._filter_timing_estimator,
            "filter",
            "后台筛选结果",
            identity=generation,
        )
        self._filter_timing = timing
        self._refresh_filter_timing()
        self._filter_timing_timer.start()

    def _finish_filter_timing(self, status: str, *, train: bool) -> None:
        timing = self._filter_timing
        if timing is None:
            return
        timing.finish(status, train=train)
        self._filter_timing_timer.stop()
        self._refresh_filter_timing()

    def _refresh_filter_timing(self) -> None:
        timing = self._filter_timing
        if timing is None:
            self.filter_timing_label.clear()
            self.filter_timing_label.setVisible(False)
            return
        snapshot = timing.snapshot()
        if snapshot.status == "superseded":
            text = f"筛选请求已被新操作替代 · 实际耗时 {format_duration(snapshot.elapsed_seconds)}"
        else:
            text = format_timing(snapshot)
        self.filter_timing_label.setText(text)
        self.filter_timing_label.setVisible(True)

    def _count_text(self) -> str:
        total = self._source_model.rowCount()
        visible = self._proxy_model.rowCount()
        filters = [f"{column} 包含「{term}」" for column, term in self._active_filters.items()]
        if self.search.text().strip():
            filters.insert(0, f"全表包含「{self.search.text().strip()}」")
        prefix = "；".join(filters) + " · " if filters else ""
        return f"{prefix}显示 {visible:,}/{total:,} 行。NA 按原值显示为 NA；单元格悬停可查看 reason_code。"

    def _visible_frame(self) -> pd.DataFrame:
        frame = self._source_model._frame
        mask = self._proxy_model._mask
        if mask is None:
            return frame
        return frame.loc[mask]

    def _update_chart(self) -> None:
        if not self._options or not self.table_choice.isEnabled():
            self.chart.set_data("", None)
            return
        name = str(self.table_choice.currentData() or "")
        self.chart.set_data(name, self._visible_frame(), tables=self._all_tables, semantics=self._semantics)
        self.save_chart_button.setEnabled(self.chart.chart_available)

    def _selected_proxy_indexes(self) -> list[QModelIndex]:
        selection = self.table_view.selectionModel()
        if selection is None:
            return []
        return selection.selectedIndexes()

    def _selection_changed(self, *_args: Any) -> None:
        proxy_indexes = self._selected_proxy_indexes()
        source_rows: set[int] = set()
        row_ids: set[str] = set()
        for proxy_index in proxy_indexes:
            source = self._proxy_model.mapToSource(proxy_index)
            if source.isValid():
                source_rows.add(source.row())
                identity = self._source_model.row_identity(source.row())
                row_ids.add(identity)
        self._selected_row_ids = tuple(sorted(row_ids))
        self.copy_button.setEnabled(bool(proxy_indexes))
        self.export_button.setEnabled(bool(source_rows))
        self.refresh_remediation_button(source_rows)
        if source_rows:
            self.filter_state.setText(self._count_text() + f" 已选 {len(source_rows):,} 行（按返回标识保存选择）。")

    def selected_source_rows(self) -> list[int]:
        """Return deduplicated source-model rows for the current proxy selection."""
        rows: set[int] = set()
        for proxy_index in self._selected_proxy_indexes():
            source = self._proxy_model.mapToSource(proxy_index)
            if source.isValid():
                rows.add(source.row())
        return sorted(rows)

    def refresh_remediation_button(self, selected_rows: Sequence[int] | None = None) -> None:
        from .diagnostic_results_actions import has_actionable_diagnostic

        rows = self.selected_source_rows() if selected_rows is None else list(selected_rows)
        enabled = False
        record = getattr(self, "record", None)
        if record is not None and len(rows) == 1 and 0 <= rows[0] < len(self._source_model._frame):
            enabled = has_actionable_diagnostic(self._source_model._frame.iloc[rows[0]])
        self.remediation_button.setEnabled(enabled)

    def _request_remediation(self) -> None:
        rows = self.selected_source_rows()
        if len(rows) != 1:
            self.filter_state.setText("请只选择一条结果行，避免把多个原因合并。")
            self.refresh_remediation_button(rows)
            return
        self.remediation_requested.emit(self, rows[0])

    def copy_selection(self) -> None:
        indexes = self._selected_proxy_indexes()
        if not indexes:
            self.filter_state.setText("先选择要复制的单元格或行。")
            return
        by_row: dict[int, dict[int, str]] = {}
        for proxy_index in indexes:
            value = self._proxy_model.data(proxy_index, Qt.ItemDataRole.DisplayRole)
            by_row.setdefault(proxy_index.row(), {})[proxy_index.column()] = str(value or "")
        columns = [index.column() for index in indexes]
        low, high = min(columns), max(columns)
        text = "\n".join(
            "\t".join(by_row[row].get(column, "") for column in range(low, high + 1))
            for row in sorted(by_row)
        )
        QApplication.clipboard().setText(text)
        self.filter_state.setText(self._count_text() + f" 已复制 {len(by_row):,} 行选中单元格。")

    def export_selected(self) -> None:
        indexes = self._selected_proxy_indexes()
        source_rows = sorted({self._proxy_model.mapToSource(index).row() for index in indexes
                              if self._proxy_model.mapToSource(index).isValid()})
        if not source_rows:
            self.filter_state.setText("先选择要导出的结果行。")
            return
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "导出选中的实际返回行", "selected-results.csv", "CSV 文件 (*.csv);;Excel 文件 (*.xlsx)",
        )
        if not path:
            return
        file_format = "xlsx" if "xlsx" in selected_filter else "csv"
        if not path.casefold().endswith("." + file_format):
            path += "." + file_format
        try:
            from pyeyedata.exports import create_table_export_request, preview_table_export
            record = getattr(self, "record", None)
            if record is None:
                raise ValueError("此视图没有对应的不可变运行记录。")
            table_name, bound_positions = self.source_binding()
            source_rows = [bound_positions[row] for row in source_rows]
            request = create_table_export_request(record, table_name, path, file_format=file_format,
                                                  scope="selected", source_positions=source_rows,
                                                  overwrite=True,
                                                  filter_description=self._count_text())
            preview = preview_table_export(record, request)
            parent = self.window()
            parent.start_table_export(record, request, preview)
        except (OSError, PermissionError, ValueError, TypeError, RuntimeError) as exc:
            self.filter_state.setText(f"导出失败：{type(exc).__name__}: {exc}")
            return
        selected_ids = request.source_row_ids
        self._selected_row_ids = selected_ids
        self.filter_state.setText(f"已提交 {len(source_rows):,} 条真实源行导出任务：{path}")

    def export_filtered(self):
        from pyeyedata.exports import create_table_export_request, preview_table_export
        try:
            table_name, positions = self.source_binding()
            captured = [positions[self._proxy_model.mapToSource(self._proxy_model.index(row, 0)).row()]
                        for row in range(self._proxy_model.rowCount())]
            path, chosen = QFileDialog.getSaveFileName(self, "导出当前筛选", "filtered-results.csv", "CSV (*.csv);;Excel (*.xlsx)")
            if not path:
                return
            fmt = "xlsx" if "xlsx" in chosen else "csv"
            if not path.lower().endswith("." + fmt):
                path += "." + fmt
            request = create_table_export_request(self.record, table_name, path, file_format=fmt,
                         scope="filtered", source_positions=captured, overwrite=True,
                         filter_description=self._count_text())
            self.window().start_table_export(self.record, request, preview_table_export(self.record, request))
        except Exception as exc:
            self.filter_state.setText(f"筛选导出失败：{type(exc).__name__}: {exc}")

    def save_chart(self) -> None:
        if not self.save_chart_button.isEnabled():
            return
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "保存当前结果图", "results-chart.png", "PNG 图片 (*.png);;SVG 矢量图 (*.svg);;PDF 图 (*.pdf)",
        )
        if not path:
            return
        file_format = "svg" if "svg" in selected_filter else "pdf" if "pdf" in selected_filter else "png"
        if not path.casefold().endswith("." + file_format):
            path += "." + file_format
        try:
            from pyeyedata.reporting import render_existing_figure_artifact
            record = getattr(self, "record", None)
            if record is None:
                raise ValueError("此视图没有对应的不可变运行记录。")
            result = render_existing_figure_artifact(record, self.chart.figure,
                        table_name=self.source_binding()[0],
                        source_label="当前可见结果图", source_tables=(self.source_binding()[0],),
                        selection=self.figure_selection(), formats=(file_format,), dpi=300)
            _write_figure_output(Path(path), result.formats[file_format], result.provenance)
        except (OSError, ValueError, RuntimeError) as exc:
            self.filter_state.setText(f"保存图表失败：{type(exc).__name__}: {exc}")
            return
        self.filter_state.setText(f"已保存当前 Matplotlib 图：{path}")

    def figure_selection(self):
        table_name, positions = self.source_binding()
        return {"x_column": self.chart.x_column.currentData(), "y_column": self.chart.y_column.currentData(),
                "group_column": self.chart.group_column.currentData(), "kind": self.chart.mode.currentData(),
                "visible_source_positions": [positions[self._proxy_model.mapToSource(self._proxy_model.index(row, 0)).row()]
                                              for row in range(self._proxy_model.rowCount())],
                "filter_description": self._count_text()}

    def source_binding(self):
        name = str(self.table_choice.currentData())
        frame = self._source_model._frame
        table = frame.attrs.get("pyea_source_table", name)
        positions = frame.attrs.get("pyea_source_positions", tuple(range(len(frame))))
        if getattr(self, "record", None) is None or table not in self.record.table_names():
            raise ValueError("此视图无法对应到实际运行表。")
        actual = self.record.table_copy(table).iloc[list(positions)]
        if not actual.reset_index(drop=True).equals(frame.reset_index(drop=True)):
            raise ValueError("显示行与保存运行的原始 typed 值不一致，导出已停止。")
        return table, tuple(positions)


class ResultsPage(QWidget):
    """Results UI for current bundles and immutable historical RunRecord objects."""

    history_selected = Signal(object)
    archive_requested = Signal(object)
    reopen_requested = Signal()
    full_export_requested = Signal(str)
    help_requested = Signal(str)
    selected_rows_exported = Signal(str, int, object)
    remediationNavigationRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bundle: AnalysisBundle | None = None
        self._snapshot: RunSnapshot | None = None
        self._display_record: RunRecord | None = None
        self._history: tuple[RunRecord, ...] = ()
        self._tables: dict[str, pd.DataFrame] = {}
        self._semantics: dict[str, Any] = {}
        self._stale_nodes: tuple[str, ...] = ()
        self._baseline_run_id: str | None = None
        self._changed_nodes: tuple[str, ...] = ()
        self._execution_status = "unknown"
        self._phase = "none"
        self._action_capabilities = {"archive": False, "reopen": False, "full_export": False}
        self._comparison_left_run_id: str | None = None
        self._comparison_right_run_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.title = QLabel("分析结果与运行追溯", self)
        self.title.setObjectName("pageTitle")
        root.addWidget(self.title)
        self.summary = QLabel("等待运行结果。运行前检查状态与指标计算状态会分开显示。", self)
        self.summary.setWordWrap(True)
        self.summary.setObjectName("datasetSummary")
        root.addWidget(self.summary)

        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("只读运行历史", self))
        self.history_choice = QComboBox(self)
        self.history_choice.setObjectName("resultsHistoryChoice")
        self.history_choice.setMinimumContentsLength(48)
        self.history_choice.setEnabled(False)
        history_row.addWidget(self.history_choice, 1)
        self.compare_runs_button = QPushButton("比较两次运行", self)
        self.compare_runs_button.setObjectName("compareRuns")
        self.compare_runs_button.setEnabled(False)
        history_row.addWidget(self.compare_runs_button)
        self.return_current_button = QPushButton("回到当前结果", self)
        self.return_current_button.setEnabled(False)
        history_row.addWidget(self.return_current_button)
        root.addLayout(history_row)

        self.capabilities = QLabel(self)
        self.capabilities.setWordWrap(True)
        root.addWidget(self.capabilities)
        action_row = QHBoxLayout()
        self.archive_button = QPushButton("保存完整档案", self)
        self.reopen_button = QPushButton("打开结果档案", self)
        self.full_export_button = QPushButton("完整 CSV/XLSX/PDF 导出", self)
        self.artifact_button = QPushButton("查看档案静态图/PDF", self)
        self.artifact_button.setEnabled(False)
        self.run_remediation_button = QPushButton("查看运行原因", self)
        self.run_remediation_button.setObjectName("runLevelRemediationAction")
        self.run_remediation_button.setEnabled(False)
        for button in (self.archive_button, self.reopen_button, self.full_export_button):
            button.setEnabled(False)
            action_row.addWidget(button)
        action_row.addStretch(1)
        action_row.addWidget(self.run_remediation_button)
        action_row.addWidget(self.artifact_button)
        root.addLayout(action_row)
        self.help_button = QPushButton("使用说明", self)
        self.help_button.setToolTip("查看使用说明是否可用。")
        root.addWidget(self.help_button)

        self.tabs = QTabWidget(self)
        self.tabs.setMinimumHeight(680)
        self.overview_tab = _ResultTableTab("overview", self.tabs)
        self.qc_tab = _ResultTableTab("qc", self.tabs)
        self.individual_tab = _ResultTableTab("individual", self.tabs)
        self.group_tab = _ResultTableTab("group", self.tabs)
        self.statistics_tab = _ResultTableTab("statistics", self.tabs)
        self.trace_view = QPlainTextEdit(self.tabs)
        self.trace_view.setReadOnly(True)
        self.trace_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tabs.addTab(self.overview_tab, "概览")
        self.tabs.addTab(self.qc_tab, "QC")
        self.tabs.addTab(self.individual_tab, "个体与高级详情")
        self.tabs.addTab(self.group_tab, "多人")
        self.tabs.addTab(self.statistics_tab, "统计")
        self.tabs.addTab(self.trace_view, "运行追溯")
        root.addWidget(self.tabs, 1)

        self.history_choice.activated.connect(self._history_activated)
        self.compare_runs_button.clicked.connect(self._open_run_comparison)
        self.return_current_button.clicked.connect(self._show_current)
        self.archive_button.clicked.connect(self._request_archive)
        self.reopen_button.clicked.connect(self._request_reopen)
        self.full_export_button.clicked.connect(lambda: self.full_export_requested.emit("all-formats"))
        self.help_button.clicked.connect(self._show_help_status)
        self.run_remediation_button.clicked.connect(self._request_run_level_remediation)
        for tab in (self.overview_tab, self.qc_tab, self.individual_tab, self.group_tab, self.statistics_tab):
            tab.selected_exported.connect(self.selected_rows_exported)
            tab.remediation_requested.connect(self._request_selected_remediation)
        self.set_c_capabilities()

    def set_preflight(self, outcome: Any) -> None:
        """Show actual preflight availability without reusing earlier run tables."""
        self._bundle = None
        self._snapshot = None
        self._display_record = None
        self._tables = {}
        self._phase = "preflight"
        self._update_action_capabilities()
        payload = getattr(outcome, "payload", outcome)
        metadata = getattr(payload, "metadata", {})
        backend_status = str(getattr(outcome, "backend_status", "") or
                             (metadata.get("execution_status", "unknown") if isinstance(metadata, Mapping) else "unknown"))
        self._execution_status = backend_status or "unknown"
        availability = getattr(outcome, "availability", None)
        if not isinstance(availability, pd.DataFrame):
            maybe_tables = getattr(payload, "tables", {})
            availability = maybe_tables.get("availability") if isinstance(maybe_tables, Mapping) else None
        options: dict[str, pd.DataFrame] = {}
        if isinstance(availability, pd.DataFrame):
            option = availability.copy(deep=True)
            option.attrs["pyea_source_table"] = "availability"
            option.attrs["pyea_source_positions"] = tuple(range(len(option)))
            options["可用情况 · 前提检查返回"] = option
            self._tables["availability"] = availability.copy(deep=True)
        self.overview_tab.set_options({}, semantics={})
        self.qc_tab.set_options(options, all_tables=self._tables, semantics={})
        self.individual_tab.set_options({})
        self.group_tab.set_options({})
        self.statistics_tab.set_options({})
        run_id = getattr(outcome, "run_id", None) or (metadata.get("run_id") if isinstance(metadata, Mapping) else None)
        status = getattr(getattr(outcome, "status", None), "value", getattr(outcome, "status", backend_status))
        self.summary.setText(
            f"运行前检查 · 运行编号 {run_id or '未提供'} · 状态：{_status_label(status)}。"
            "尚未计算指标；旧结果已清除。"
        )
        self.trace_view.setPlainText(_json_text({
            "phase": self._phase, "ui_status": str(status), "execution_status": backend_status,
            "run_id": run_id, "metadata": metadata, "availability_rows": len(availability) if isinstance(availability, pd.DataFrame) else None,
            "note": "前提检查不是指标计算，不会被呈现成完成的分析结果。",
        }))
        self._sync_return_button()
        self._update_action_capabilities()

    def set_bundle(self, bundle: AnalysisBundle, *, snapshot: RunSnapshot | None = None) -> None:
        """Display the actual returned bundle and exact immutable input snapshot."""
        if not isinstance(bundle, AnalysisBundle):
            self.summary.setText("没有收到可识别的分析结果；请查看运行追溯信息。")
            self._bundle = None
            self._snapshot = None
            self._tables = {}
            self._update_action_capabilities()
            for tab in (self.overview_tab, self.qc_tab, self.individual_tab, self.group_tab, self.statistics_tab):
                tab.set_options({})
            return
        self._bundle = bundle
        self._snapshot = snapshot
        self._update_action_capabilities()
        self._display_record = None
        self._phase = "run"
        self._tables = {
            str(name): frame.copy(deep=True)
            for name, frame in bundle.tables.items()
            if isinstance(frame, pd.DataFrame)
        }
        metadata = bundle.metadata if isinstance(bundle.metadata, Mapping) else {}
        self._execution_status = str(metadata.get("execution_status", "unknown"))
        self._semantics = self._semantics_for(snapshot, metadata)
        options = self._category_options(self._tables)
        self.overview_tab.set_options(options["overview"], all_tables=self._tables, semantics=self._semantics)
        self.qc_tab.set_options(options["qc"], all_tables=self._tables, semantics=self._semantics)
        self.individual_tab.set_options(options["individual"], all_tables=self._tables, semantics=self._semantics)
        self.group_tab.set_options(options["group"], all_tables=self._tables, semantics=self._semantics)
        self.statistics_tab.set_options(options["statistics"], all_tables=self._tables, semantics=self._semantics)
        run_id = metadata.get("run_id") or (snapshot.run_id if snapshot else "未提供")
        status_suffix = "；设备或生理信号标签来自返回数据，不代表设备已认证。"
        self.summary.setText(
            f"运行编号 {run_id} · 状态：{_status_label(self._execution_status)} · "
            f"返回 {len(self._tables)} 张结果表、{sum(len(frame) for frame in self._tables.values()):,} 行。"
            + status_suffix
        )
        self._write_trace()
        if self._display_record is None and self.history_choice.count():
            self.history_choice.blockSignals(True)
            self.history_choice.setCurrentIndex(0)
            self.history_choice.blockSignals(False)
        self._sync_return_button()
        self._update_action_capabilities()

    @staticmethod
    def _category_options(tables: Mapping[str, pd.DataFrame]) -> dict[str, dict[str, pd.DataFrame]]:
        result: dict[str, dict[str, pd.DataFrame]] = {
            "overview": {}, "qc": {}, "individual": {}, "group": {}, "statistics": {},
        }
        for table_name, frame in tables.items():
            frame.attrs["pyea_source_table"] = table_name
            frame.attrs["pyea_source_positions"] = tuple(range(len(frame)))
        metrics = tables.get("metrics")
        if isinstance(metrics, pd.DataFrame):
            result["overview"]["指标 · 实际结果"] = metrics
            if "metric_id" in metrics:
                metric_ids = metrics["metric_id"].astype("string")
                def selected_rows(mask: Sequence[bool]) -> pd.DataFrame:
                    positions = np.flatnonzero(np.asarray(mask, dtype=bool))
                    selected = metrics.iloc[positions].copy(deep=True)
                    selected.attrs["pyea_source_table"] = "metrics"
                    selected.attrs["pyea_source_positions"] = tuple(int(position) for position in positions)
                    return selected

                individual = selected_rows(metric_ids.str.startswith("individual.", na=False).to_numpy(dtype=bool))
                group = selected_rows(metric_ids.str.startswith("group.", na=False).to_numpy(dtype=bool))
                metric_values = metric_ids.tolist()
                quality_mask = [
                    not _missing(value) and any(str(value).startswith(prefix) for prefix in _QUALITY_METRIC_PREFIXES)
                    for value in metric_values
                ]
                quality = selected_rows(quality_mask)
                if len(individual) or metrics.empty:
                    result["individual"]["个体指标 · 实际结果"] = individual
                if len(group) or metrics.empty:
                    result["group"]["多人指标 · 实际结果"] = group
                if len(quality) or metrics.empty:
                    result["qc"]["质量指标 · 实际结果"] = quality
                advanced = selected_rows(metric_ids.str.startswith("advanced.", na=False).to_numpy(dtype=bool))
                if len(advanced):
                    result["individual"]["高级指标 · 实际结果"] = advanced
        for name, frame in tables.items():
            key = name.casefold()
            if key == "metrics":
                continue
            if key.startswith("statistics") or key in {"descriptives", "diagnostics"}:
                result["statistics"][name] = frame
            elif key.startswith("group_") or key == "group":
                result["group"][name] = frame
            elif key.startswith(("individual_", "advanced_")) or key in {"events", "source_events", "saccade_details", "scanpath_audit", "heatmap_grid", "processed_signals", "input_signals"}:
                result["individual"][name] = frame
            elif key in {"quality", "availability", "processed_signals", "analysis_streams", "processing_partitions",
                         "analysis_memberships", "segment_overlap", "segment_scope_audit", "event_interval_audit",
                         "label_derivations"} or key.startswith("input_") or key.startswith("config."):
                result["qc"][name] = frame
            else:
                # Every unclassified returned table remains reachable in the QC/detail tab.
                result["qc"][name] = frame
        if isinstance(metrics, pd.DataFrame):
            for category in result.values():
                for label, frame in category.items():
                    if label.endswith(" · 实际结果") and frame.attrs.get("pyea_source_table") != "metrics":
                        frame.attrs["pyea_source_table"] = "metrics"
                        frame.attrs["pyea_source_positions"] = tuple(range(len(frame)))
        return result

    @staticmethod
    def _semantics_for(snapshot: RunSnapshot | None, metadata: Mapping[str, Any]) -> dict[str, Any]:
        semantics: dict[str, Any] = {"metadata": _safe_value(metadata)}
        if snapshot is None:
            return semantics
        try:
            parameters = snapshot.parameter_values()
            context = snapshot.context_values()
            semantics.update({
                "parameters": parameters,
                "context": context,
                "coordinate_unit": _find_value(parameters, context, metadata, key="coordinate_unit"),
                "coordinate_frame": _find_value(parameters, context, metadata, key="coordinate_frame"),
                "pupil_unit": _find_value(parameters, context, metadata, key="pupil_unit"),
            })
        except (AttributeError, TypeError, ValueError):
            pass
        return semantics

    def set_history(self, records: Sequence[RunRecord]) -> None:
        """Refresh history choices only; never replace the current bundle view."""
        current_id = self._display_record.run_id if self._display_record else None
        self._history = tuple(record for record in records if isinstance(record, RunRecord))
        self.history_choice.blockSignals(True)
        self.history_choice.clear()
        self.history_choice.addItem("选择一条只读历史记录…", None)
        selected_index = 0
        for index, record in enumerate(self._history, start=1):
            label = (
                f"{record.created_at} · {_phase_label(record.phase)} · {_status_label(record.status)} · "
                f"运行编号 {record.run_id} · 数据修订 {record.revision_id}"
            )
            self.history_choice.addItem(label, record)
            if current_id and record.run_id == current_id:
                selected_index = index
        self.history_choice.setCurrentIndex(selected_index)
        self.history_choice.setEnabled(bool(self._history))
        self.history_choice.blockSignals(False)
        self._update_action_capabilities()
        self._sync_return_button()

    def _history_activated(self, index: int) -> None:
        record = self.history_choice.itemData(index)
        if isinstance(record, RunRecord):
            self.history_selected.emit(record)

    def _build_comparison_dialog(self):
        """Create the comparison UI while preserving history selection by run ID."""
        from .comparison_dialog import RunComparisonDialog

        dialog = RunComparisonDialog(self)
        dialog.set_history(
            self._history,
            left_run_id=self._comparison_left_run_id,
            right_run_id=self._comparison_right_run_id,
        )
        return dialog

    def _open_run_comparison(self) -> None:
        if not self.compare_runs_button.isEnabled():
            return
        dialog = self._build_comparison_dialog()
        self._active_comparison_dialog = dialog
        dialog.exec()
        result = dialog.comparison_result
        if result is not None:
            self._comparison_left_run_id = result.left_run_id
            self._comparison_right_run_id = result.right_run_id
        dialog.deleteLater()

    def _build_selected_remediation(self, tab: _ResultTableTab):
        """Resolve one proxy-selected row to the exact immutable source row."""
        from .diagnostic_results_actions import has_actionable_diagnostic, match_availability_row
        from .remediation import build_remediation

        record = self.current_record()
        if not isinstance(record, RunRecord):
            return None
        rows = tab.selected_source_rows()
        if len(rows) != 1:
            return None
        try:
            table_name, positions = tab.source_binding()
            source_position = int(positions[rows[0]])
            if table_name not in record.table_names():
                return None
            actual = record.table_copy(table_name)
            if not 0 <= source_position < len(actual):
                return None
            diagnostic = deepcopy(actual.iloc[source_position].to_dict())
        except (IndexError, KeyError, TypeError, ValueError, RuntimeError):
            return None
        if not has_actionable_diagnostic(diagnostic):
            return None

        availability_row = None
        availability_message = ""
        if table_name != "availability" and "availability" in record.table_names():
            match = match_availability_row(diagnostic, record.table_copy("availability"))
            availability_row = match.row
            if not match.matched:
                availability_message = match.reason
        guide = build_remediation(record, diagnostic, availability_row=availability_row)
        if availability_message:
            guide = replace(guide, limitations=tuple(guide.limitations) + (availability_message,))
        payload = {
            "run_id": str(record.run_id),
            "revision_id": str(record.revision_id) if record.revision_id is not None else None,
            "diagnostic": diagnostic,
        }
        return guide, payload

    @staticmethod
    def _run_level_diagnostic(record: RunRecord) -> dict[str, Any]:
        metadata = dict(record.metadata())
        return {
            "run_id": str(record.run_id),
            "revision_id": record.revision_id,
            "phase": str(record.phase),
            "status": str(record.status or metadata.get("execution_status", "unknown")),
            "failure_type": metadata.get("failure_type"),
            "failure_reason": metadata.get("failure_reason"),
            "reason_code": metadata.get("reason_code"),
            "reason": metadata.get("reason", metadata.get("failure_reason")),
            "required_fields": metadata.get("required_fields"),
            "required_metadata": metadata.get("required_metadata"),
            "parameter_path": metadata.get("parameter_path"),
            "logs": tuple(record.logs()),
        }

    def _build_run_level_remediation(self):
        from .diagnostic_results_actions import has_actionable_diagnostic
        from .remediation import build_remediation

        record = self.current_record()
        if not isinstance(record, RunRecord) or record.table_names():
            return None
        diagnostic = deepcopy(self._run_level_diagnostic(record))
        if not has_actionable_diagnostic(diagnostic):
            return None
        guide = build_remediation(record, diagnostic)
        return guide, {
            "run_id": str(record.run_id),
            "revision_id": str(record.revision_id) if record.revision_id is not None else None,
            "diagnostic": diagnostic,
        }

    def _emit_remediation_navigation(self, payload: Mapping[str, Any] | None, target: Any) -> None:
        from .remediation import NavigationTarget

        if not isinstance(payload, Mapping) or not isinstance(target, NavigationTarget):
            return
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return
        self.remediationNavigationRequested.emit({
            "target": target,
            "run_id": run_id,
            "revision_id": payload.get("revision_id"),
            "diagnostic": deepcopy(dict(payload.get("diagnostic", {}))),
        })

    def _show_remediation_dialog(self, guide: Any, payload: Mapping[str, Any]) -> None:
        from .remediation_dialog import RemediationDialog

        dialog = RemediationDialog(self)
        dialog.set_guide(guide)

        def navigate(target: Any) -> None:
            self._emit_remediation_navigation(payload, target)
            # Close the modal after routing so the host destination is usable.
            dialog.accept()

        dialog.navigateRequested.connect(navigate)
        self._active_remediation_dialog = dialog
        dialog.exec()
        dialog.deleteLater()

    def _request_selected_remediation(self, tab: _ResultTableTab, _source_row: int) -> None:
        built = self._build_selected_remediation(tab)
        if built is None:
            tab.filter_state.setText("无法将所选视图行唯一对应到带原因的运行源行。")
            tab.refresh_remediation_button()
            return
        self._show_remediation_dialog(*built)

    def _request_run_level_remediation(self) -> None:
        built = self._build_run_level_remediation()
        if built is not None:
            self._show_remediation_dialog(*built)

    def show_record(self, record: RunRecord, stale_nodes: Sequence[str] = ()) -> None:
        """Show a detached historical record as read-only, including true table count."""
        if not isinstance(record, RunRecord):
            self.summary.setText("历史记录类型无效；当前结果视图保持不变。")
            return
        self._display_record = record
        self._stale_nodes = tuple(str(node) for node in stale_nodes)
        try:
            metadata = dict(record.metadata())
            tables = dict(record.tables())
            details = record.snapshot_details() if callable(getattr(record, "snapshot_details", None)) else {}
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            metadata, tables, details = {}, {}, {"snapshot_details_error": f"{type(exc).__name__}: {exc}"}
        self._tables = {
            str(name): frame.copy(deep=True)
            for name, frame in tables.items()
            if isinstance(frame, pd.DataFrame)
        }
        self._phase = str(record.phase)
        self._execution_status = str(record.status or metadata.get("execution_status", "unknown"))
        self._semantics = {
            "metadata": _safe_value(metadata),
            "snapshot_details": _safe_value(details),
            "coordinate_unit": _find_value(details, metadata, key="coordinate_unit"),
            "coordinate_frame": _find_value(details, metadata, key="coordinate_frame"),
        }
        options = self._category_options(self._tables)
        self.overview_tab.set_options(options["overview"], all_tables=self._tables, semantics=self._semantics)
        self.qc_tab.set_options(options["qc"], all_tables=self._tables, semantics=self._semantics)
        self.individual_tab.set_options(options["individual"], all_tables=self._tables, semantics=self._semantics)
        self.group_tab.set_options(options["group"], all_tables=self._tables, semantics=self._semantics)
        self.statistics_tab.set_options(options["statistics"], all_tables=self._tables, semantics=self._semantics)
        stale_text = "；与当前配置不同的节点：" + ", ".join(self._stale_nodes) if self._stale_nodes else ""
        stale_return = "；该运行返回时已过期" if metadata.get("gui_stale_at_return") is True else ""
        if self._tables:
            result_text = f"返回 {len(self._tables)} 张结果表、{sum(len(frame) for frame in self._tables.values()):,} 行"
        else:
            result_text = "没有返回结果表，因此没有可显示的分析数据"
        self.summary.setText(
            f"历史记录 · {record.created_at} · {_phase_label(record.phase)} · 状态：{_status_label(record.status)} · "
            f"运行编号 {record.run_id} · 数据修订 {record.revision_id} · {result_text}{stale_return}{stale_text}"
        )
        self._write_trace(metadata=metadata, record=record, snapshot_details=details)
        self._update_action_capabilities()
        self._sync_return_button()

    def current_record(self) -> RunRecord | None:
        if self._display_record is not None:
            return self._display_record
        if self._snapshot is not None:
            return next((record for record in reversed(self._history)
                         if record.run_id == self._snapshot.run_id and record.generation == self._snapshot.generation), None)
        return None

    def set_dependency_state(
        self,
        stale_nodes: Sequence[str],
        *,
        baseline_run_id: str | None = None,
        changed_nodes: Sequence[str] = (),
    ) -> None:
        self._stale_nodes = tuple(str(node) for node in stale_nodes)
        self._baseline_run_id = baseline_run_id
        self._changed_nodes = tuple(str(node) for node in changed_nodes)
        if self._display_record is None and self._bundle is not None:
            self._write_trace()

    def _write_trace(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        record: RunRecord | None = None,
        snapshot_details: Any = None,
    ) -> None:
        if metadata is None:
            metadata = self._bundle.metadata if self._bundle is not None else {}
        table_summary = {
            name: {
                "rows": int(len(frame)),
                "columns": [str(column) for column in frame.columns],
                "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
                "missing_cells": int(frame.isna().sum().sum()),
            }
            for name, frame in self._tables.items()
        }
        if record is not None:
            run_id = record.run_id
            revision_id = record.revision_id
            fingerprint = record.config_fingerprint
            generation = record.generation
            created_at = record.created_at
        elif self._snapshot is not None:
            run_id = self._snapshot.run_id
            revision_id = self._snapshot.dataset_revision.revision_id
            fingerprint = self._snapshot.config_fingerprint
            generation = self._snapshot.generation
            created_at = self._snapshot.created_at
        else:
            run_id = _safe_value(metadata).get("run_id") if isinstance(_safe_value(metadata), Mapping) else None
            revision_id = fingerprint = generation = created_at = None
        snapshot_payload: dict[str, Any] = {}
        if self._snapshot is not None and record is None:
            dataset_revision = self._snapshot.dataset_revision
            snapshot_payload = {
                "selection": self._snapshot.selection.to_dict(),
                "schema_version": self._snapshot.schema_version,
                "parameters": self._snapshot.parameter_values(),
                "context": self._snapshot.context_values(),
                "source_hashes": dict(dataset_revision.source_hashes),
                "raw_source_bytes": dataset_revision.raw_source_bytes(),
                "dataset_fingerprint": dataset_revision.fingerprint,
            }
        trace = {
            "read_only": True,
            "phase": self._phase,
            "execution_status": self._execution_status,
            "run_id": run_id,
            "generation": generation,
            "created_at": created_at,
            "dataset_revision_id": revision_id,
            "config_fingerprint": fingerprint,
            "stale_nodes": list(self._stale_nodes),
            "changed_nodes": list(self._changed_nodes),
            "baseline_run_id": self._baseline_run_id,
            "snapshot": snapshot_payload if record is None else snapshot_details,
            "bundle_metadata": metadata,
            "tables": table_summary,
            "logs": (
                list(self._bundle.logs) if self._bundle is not None and record is None
                else list(record.logs()) if record is not None and callable(getattr(record, "logs", None))
                else []
            ),
            "available_actions": {
                "save_archive": self.archive_button.isEnabled(),
                "open_archive": self.reopen_button.isEnabled(),
                "full_export": self.full_export_button.isEnabled(),
            },
        }
        self.trace_view.setPlainText(_json_text(trace))

    def _show_help_status(self) -> None:
        self.help_requested.emit("offline-user-guide")

    def set_c_capabilities(
        self,
        *,
        archive: bool = False,
        reopen: bool = False,
        full_export: bool = False,
        busy: bool = False,
    ) -> None:
        """Enable only actions that the host has actually connected."""
        self._action_capabilities = {
            "archive": bool(archive),
            "reopen": bool(reopen),
            "full_export": bool(full_export),
        }
        self._capabilities_busy = bool(busy)
        self._update_action_capabilities()

    def _update_action_capabilities(self) -> None:
        archive = self._action_capabilities["archive"]
        reopen = self._action_capabilities["reopen"]
        full_export = self._action_capabilities["full_export"]
        record = self.current_record()
        for tab in (self.overview_tab, self.qc_tab, self.individual_tab, self.group_tab, self.statistics_tab):
            tab.record = record
            tab.refresh_remediation_button()
        self.compare_runs_button.setEnabled(len(self._history) >= 2)
        run_diagnostic_enabled = False
        if isinstance(record, RunRecord) and not record.table_names():
            from .diagnostic_results_actions import has_actionable_diagnostic
            run_diagnostic_enabled = has_actionable_diagnostic(self._run_level_diagnostic(record))
        self.run_remediation_button.setEnabled(run_diagnostic_enabled)
        self.archive_button.setText("保存运行档案")
        self.archive_button.setEnabled(bool(archive and record is not None))
        self.reopen_button.setEnabled(reopen)
        self.full_export_button.setEnabled(bool(full_export and record is not None))
        self.artifact_button.setEnabled(bool(record and record.metadata().get("app_archive", {}).get("artifacts")))
        unavailable = []
        if not archive:
            unavailable.append("完整结果保存")
            self.archive_button.setToolTip("当前尚未提供此功能。")
        elif record is None:
            self.archive_button.setToolTip("先选择一条实际运行记录。")
        else:
            self.archive_button.setToolTip("保存所选运行；没有原始输入的历史记录可保存为仅结果档案。")
        if not reopen:
            unavailable.append("打开历史结果档案")
            self.reopen_button.setToolTip("当前尚未提供此功能。")
        else:
            self.reopen_button.setToolTip("")
        if not full_export:
            unavailable.append("完整格式导出")
            self.full_export_button.setToolTip("当前尚未提供此功能。")
        elif self._bundle is None:
            self.full_export_button.setToolTip("请先运行分析并保留结果。")
        else:
            self.full_export_button.setToolTip("")
        if unavailable:
            self.capabilities.setText("后台归档/导出任务进行中；完成或取消后可继续。" if getattr(self, "_capabilities_busy", False) else "当前尚未提供：" + "、".join(unavailable) + "。对应按钮已禁用。")
            self.capabilities.setVisible(True)
        else:
            self.capabilities.clear()
            self.capabilities.setVisible(False)

    def _request_archive(self) -> None:
        record = self.current_record()
        if self.archive_button.isEnabled() and record is not None:
            self.archive_requested.emit(record)

    def _request_reopen(self) -> None:
        if self.reopen_button.isEnabled():
            self.reopen_requested.emit()

    def _show_current(self) -> None:
        if self._bundle is None:
            self.summary.setText("当前没有可显示的分析结果；运行前检查不会显示为分析结果。")
            return
        self.set_bundle(self._bundle, snapshot=self._snapshot)

    def _sync_return_button(self) -> None:
        self.return_current_button.setEnabled(self._display_record is not None and self._bundle is not None)


def _find_value(*sources: Any, key: str) -> Any:
    for source in sources:
        if isinstance(source, Mapping):
            if key in source and source[key] not in (None, "", "unknown"):
                return source[key]
            found = _find_value(*(source.get(name) for name in ("metadata", "coordinate_metadata", "dataset_metadata", "context", "group", "parameters")
                                  if name in source), key=key)
            if found not in (None, "", "unknown"):
                return found
    return None

