"""Reusable views for read-only backend tables."""
from __future__ import annotations

from typing import Any

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QTableView


class DataFrameTableModel(QAbstractTableModel):
    """Read-only model that keeps data in a DataFrame instead of per-cell widgets."""

    def __init__(self, frame: pd.DataFrame | None = None, parent: Any = None) -> None:
        super().__init__(parent)
        self._frame = frame.copy(deep=True) if frame is not None else pd.DataFrame()

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame.copy(deep=True)

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        self.beginResetModel()
        self._frame = frame.copy(deep=True) if frame is not None else pd.DataFrame()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else int(len(self._frame.index))

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else int(len(self._frame.columns))

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._frame.index) or index.column() >= len(self._frame.columns):
            return None
        value = self._frame.iat[index.row(), index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if value is None or value is pd.NA or value is pd.NaT:
                return ""
            if isinstance(value, float) and pd.isna(value):
                return ""
            return str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and pd.api.types.is_numeric_dtype(self._frame.dtypes.iloc[index.column()]):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            column = str(self._frame.columns[index.column()])
            return f"{column}：{value}"
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


class DataFrameTableView(QTableView):
    """A table view with keyboard copy support and no cell-widget expansion."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QTableView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(24)

    def keyPressEvent(self, event: Any) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_selection(self) -> None:
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return
        model = self.model()
        rows: dict[int, dict[int, str]] = {}
        for index in indexes:
            rows.setdefault(index.row(), {})[index.column()] = str(model.data(index, Qt.ItemDataRole.DisplayRole) or "")
        min_column = min(column for row in rows.values() for column in row)
        max_column = max(column for row in rows.values() for column in row)
        text = "\n".join(
            "\t".join(rows[row].get(column, "") for column in range(min_column, max_column + 1))
            for row in sorted(rows)
        )
        QApplication.clipboard().setText(text)

