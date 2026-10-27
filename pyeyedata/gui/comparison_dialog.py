"""Read-only two-run comparison dialog."""
from __future__ import annotations

import json
import math
from typing import Any, Sequence

import numpy as np
import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QTableView,
    QVBoxLayout,
)

from .models import RunRecord
from .run_comparison import ComparisonResult, compare_runs


def _cell_text(value: Any) -> str:
    if value is None or value is pd.NA or value is pd.NaT:
        return "NA"
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            return "NA"
        return f"{value:.8g}"
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


class _ComparisonTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frame = pd.DataFrame()

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        self.beginResetModel()
        self._frame = frame.copy(deep=True) if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._frame.index)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._frame.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._frame) or index.column() >= len(self._frame.columns):
            return None
        value = self._frame.iat[index.row(), index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return _cell_text(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and pd.api.types.is_numeric_dtype(self._frame.dtypes.iloc[index.column()]):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{self._frame.columns[index.column()]}: {_cell_text(value)}"
        return None

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._frame.columns):
            return str(self._frame.columns[section])
        if orientation == Qt.Orientation.Vertical and 0 <= section < len(self._frame.index):
            return str(self._frame.index[section] + 1)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.NoItemFlags if not index.isValid() else Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class RunComparisonDialog(QDialog):
    """Compare parameter snapshots, run states and identity-matched metrics."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行对比")
        self.resize(1180, 760)
        self._records: tuple[RunRecord, ...] = ()
        self._result: ComparisonResult | None = None
        self._updating = False

        root = QVBoxLayout(self)
        selectors = QHBoxLayout()
        selectors.addWidget(QLabel("左侧运行", self))
        self.left_choice = QComboBox(self)
        self.left_choice.setObjectName("comparisonLeftRun")
        self.left_choice.setMinimumContentsLength(30)
        selectors.addWidget(self.left_choice, 1)
        selectors.addWidget(QLabel("右侧运行", self))
        self.right_choice = QComboBox(self)
        self.right_choice.setObjectName("comparisonRightRun")
        self.right_choice.setMinimumContentsLength(30)
        selectors.addWidget(self.right_choice, 1)
        root.addLayout(selectors)

        self.warning_label = QLabel("选择两次运行以比较。", self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setObjectName("comparisonWarnings")
        root.addWidget(self.warning_label)

        self.tabs = QTabWidget(self)
        self.parameter_view, self.parameter_model = self._make_view()
        self.state_view, self.state_model = self._make_view()
        self.metric_view, self.metric_model = self._make_view()
        self.tabs.addTab(self.parameter_view, "参数差异")
        self.tabs.addTab(self.state_view, "来源与状态")
        self.tabs.addTab(self.metric_view, "指标对比")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)
        self.left_choice.currentIndexChanged.connect(self._selection_changed)
        self.right_choice.currentIndexChanged.connect(self._selection_changed)

    @property
    def comparison_result(self) -> ComparisonResult | None:
        return self._result

    @staticmethod
    def _make_view() -> tuple[QTableView, _ComparisonTableModel]:
        view = QTableView()
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        view.setWordWrap(False)
        view.setSortingEnabled(False)
        model = _ComparisonTableModel(view)
        view.setModel(model)
        view.horizontalHeader().setStretchLastSection(True)
        view.horizontalHeader().setMinimumSectionSize(90)
        view.verticalHeader().setDefaultSectionSize(24)
        return view, model

    def set_history(
        self,
        records: Sequence[RunRecord],
        left_run_id: str | None = None,
        right_run_id: str | None = None,
    ) -> None:
        self._records = tuple(record for record in records if isinstance(record, RunRecord))
        self._updating = True
        try:
            self.left_choice.clear()
            self.right_choice.clear()
            for index, record in enumerate(self._records):
                label = f"{record.run_id} · {record.phase} · {record.status} · {record.created_at}"
                self.left_choice.addItem(label, index)
                self.right_choice.addItem(label, index)
            if self._records:
                left_index = self._find_run(left_run_id)
                right_index = self._find_run(right_run_id)
                if left_index < 0:
                    left_index = max(0, len(self._records) - 2)
                if right_index < 0:
                    right_index = len(self._records) - 1
                self.left_choice.setCurrentIndex(left_index)
                self.right_choice.setCurrentIndex(right_index)
        finally:
            self._updating = False
        self._refresh()

    def _find_run(self, run_id: str | None) -> int:
        if run_id is None:
            return -1
        return next((index for index, record in enumerate(self._records) if record.run_id == str(run_id)), -1)

    def _selected(self, choice: QComboBox) -> RunRecord | None:
        index = choice.currentData()
        return self._records[index] if isinstance(index, int) and 0 <= index < len(self._records) else None

    def _selection_changed(self, *_: Any) -> None:
        if not self._updating:
            self._refresh()

    def _refresh(self) -> None:
        left, right = self._selected(self.left_choice), self._selected(self.right_choice)
        if left is None or right is None:
            self._result = None
            for model in (self.parameter_model, self.state_model, self.metric_model):
                model.set_frame(pd.DataFrame())
            self.warning_label.setText("至少需要一条运行记录。")
            return
        try:
            self._result = compare_runs(left, right)
        except Exception as exc:
            self._result = None
            for model in (self.parameter_model, self.state_model, self.metric_model):
                model.set_frame(pd.DataFrame())
            self.warning_label.setText(f"无法生成运行对比：{type(exc).__name__}: {exc}")
            return
        self.parameter_model.set_frame(self._result.parameter_differences)
        self.state_model.set_frame(self._result.source_status_summary)
        self.metric_model.set_frame(self._result.metrics)
        if self._result.warnings:
            self.warning_label.setText("\n".join(self._result.warnings))
        elif self._result.metrics.empty:
            self.warning_label.setText("参数与状态已读取；两次运行没有可比较的指标行。")
        else:
            self.warning_label.setText("指标只在来源、完整语义身份、单位、算法、范围及双方状态允许时计算差值。")


__all__ = ["RunComparisonDialog"]

