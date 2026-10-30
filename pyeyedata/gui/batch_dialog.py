"""Queue-facing batch dialog; all analysis and persistence stay with its host."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from PySide6.QtCore import QItemSelectionModel, QSignalBlocker, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


_ANALYSIS_ACTIVE = frozenset({"importing", "checking", "running"})
_EXPORT_ACTIVE = frozenset({"exporting"})


def _row_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            pass
    return str(value)


class BatchQueueDialog(QDialog):
    """A thin queue UI that emits intents and never starts pipeline work itself."""

    addFilesRequested = Signal(list)
    removeItemsRequested = Signal(list)
    startRequested = Signal(str)
    cancelRequested = Signal()
    retryItemsRequested = Signal(list)
    retryExportRequested = Signal(list)
    openResultRequested = Signal(str)
    configureRequested = Signal()
    groupWorkflowRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量分析队列")
        self.resize(1180, 650)
        self._rows: list[dict[str, Any]] = []
        self._preflight: dict[str, tuple[str, ...]] = {}
        self._busy = False
        self._cancelable = False
        self._prepared = False
        self._output_directory: Path | None = None

        root = QVBoxLayout(self)
        self.configuration_label = QLabel("当前批次配置尚未显示。", self)
        self.configuration_label.setWordWrap(True)
        self.configuration_label.setObjectName("batchConfigurationSummary")
        root.addWidget(self.configuration_label)

        timing_row = QHBoxLayout()
        self.timing_label = QLabel("当前阶段：待处理", self)
        self.timing_label.setObjectName("batchCurrentTiming")
        self.timing_label.setWordWrap(True)
        self.queue_timing_label = QLabel("队列剩余：—", self)
        self.queue_timing_label.setObjectName("batchQueueTiming")
        self.queue_timing_label.setWordWrap(True)
        timing_row.addWidget(self.timing_label, 2)
        timing_row.addWidget(self.queue_timing_label, 1)
        root.addLayout(timing_row)

        toolbar = QHBoxLayout()
        self.add_button = QPushButton("添加文件", self)
        self.remove_button = QPushButton("移除所选", self)
        self.configure_button = QPushButton("配置导入映射", self)
        self.group_button = QPushButton("组别工作流", self)
        self.start_button = QPushButton("准备批量", self)
        self.cancel_button = QPushButton("取消队列", self)
        for button in (self.add_button, self.remove_button, self.configure_button,
                       self.group_button, self.start_button, self.cancel_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("筛选", self))
        self.filter_choice = QComboBox(self)
        self.filter_choice.addItem("全部", "all")
        self.filter_choice.addItem("待处理", "pending")
        self.filter_choice.addItem("运行中", "active")
        self.filter_choice.addItem("成功", "success")
        self.filter_choice.addItem("部分结果", "partial")
        self.filter_choice.addItem("失败", "failed")
        self.filter_choice.addItem("已取消", "cancelled")
        self.filter_choice.addItem("导出失败", "export_failed")
        toolbar.addWidget(self.filter_choice)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, 7, self)
        self.table.setObjectName("batchQueueTable")
        self.table.setHorizontalHeaderLabels((
            "输入文件 / 项目", "导入预设 / 结构", "分析阶段 / 尝试", "分析原因 / 预检",
            "导出状态 / 次数", "导出原因", "结果路径",
        ))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(26)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.retry_button = QPushButton("重试分析", self)
        self.retry_export_button = QPushButton("重试结果导出", self)
        self.view_result_button = QPushButton("查看结果", self)
        self.open_directory_button = QPushButton("打开输出目录", self)
        for button in (self.retry_button, self.retry_export_button,
                       self.view_result_button, self.open_directory_button):
            actions.addWidget(button)
        actions.addStretch(1)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("batchPreflightMessages")
        actions.addWidget(self.status_label, 2)
        root.addLayout(actions)

        self.add_button.clicked.connect(self._choose_files)
        self.remove_button.clicked.connect(self._emit_remove)
        self.configure_button.clicked.connect(self.configureRequested)
        self.group_button.clicked.connect(self.groupWorkflowRequested)
        self.start_button.clicked.connect(self._start_or_prepare)
        self.cancel_button.clicked.connect(self.cancelRequested)
        self.retry_button.clicked.connect(self._emit_retry)
        self.retry_export_button.clicked.connect(self._emit_retry_export)
        self.view_result_button.clicked.connect(self._emit_view_result)
        self.open_directory_button.clicked.connect(self._open_selected_directory)
        self.filter_choice.currentIndexChanged.connect(self._apply_filter)
        self.table.itemSelectionChanged.connect(self._update_controls)
        self.table.itemDoubleClicked.connect(lambda _item: self._emit_view_result())
        self._update_controls()

    def selected_item_ids(self) -> list[str]:
        """Return selected stable IDs for host-side configuration/removal actions."""
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        result: list[str] = []
        for row in rows:
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 0)
            item_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if item_id is not None:
                result.append(str(item_id))
        return result

    def select_item(self, item_id: str) -> bool:
        """Select a stable queue row for a host-directed mapping review."""
        wanted = str(item_id)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and str(item.data(Qt.ItemDataRole.UserRole)) == wanted:
                if self.table.isRowHidden(row):
                    self.filter_choice.setCurrentIndex(self.filter_choice.findData("all"))
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                return True
        return False

    def set_queue_view(self, rows: Sequence[Mapping[str, Any]]) -> None:
        selected = set(self.selected_item_ids())
        normalized = [_row_mapping(row) for row in rows]
        self._rows = [row for row in normalized if row]
        selection_model = self.table.selectionModel()
        selection_blocker = QSignalBlocker(selection_model)
        view_blocker = QSignalBlocker(self.table)
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            attempts = row.get("attempts") or ()
            current = attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}
            analysis_status = str(row.get("analysis_status", row.get("status", "pending")))
            export_status = str(row.get("export_status", current.get("export_status", "not_requested")))
            number = current.get("number", len(attempts))
            export_count = current.get("export_attempts", 0)
            structure = "已确认" if row.get("structure_confirmed") else "待确认"
            profile = str(row.get("profile_id", ""))
            profile_revision = row.get("profile_revision")
            if profile_revision is not None:
                profile = f"{profile} · r{profile_revision}"
            source_path = str(row.get("source_path", row.get("source_name", "")))
            item_id = str(row.get("item_id", ""))
            source_cell = QTableWidgetItem(Path(source_path).name if source_path else item_id)
            source_cell.setData(Qt.ItemDataRole.UserRole, item_id)
            source_cell.setToolTip(f"{source_path}\nID: {item_id}\nSHA-256: {row.get('source_sha256', '')}")
            values = (
                source_cell,
                QTableWidgetItem(f"{profile}\n{structure}"),
                QTableWidgetItem(f"{analysis_status} · a{number or '—'}"),
                QTableWidgetItem(_display(row.get("error") or current.get("error"))),
                QTableWidgetItem(f"{export_status} · e{export_count or '—'}"),
                QTableWidgetItem(_display(row.get("export_error") or current.get("export_error"))),
                QTableWidgetItem("\n".join(map(str, row.get("output_paths", current.get("output_paths", ())) or ()))),
            )
            for column, cell in enumerate(values):
                if column == 3:
                    preflight = self._preflight.get(item_id, ())
                    if preflight:
                        detail = "\n".join(preflight)
                        cell.setText((cell.text() + "\n" if cell.text() else "") + detail)
                if column in {3, 5, 6}:
                    cell.setToolTip(cell.text())
                self.table.setItem(row_index, column, cell)
            self.table.setRowHidden(row_index, not self._matches_filter(row))
            if item_id in selected and not self.table.isRowHidden(row_index):
                index = self.table.model().index(row_index, 0)
                selection_model.select(
                    index,
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
        del view_blocker
        del selection_blocker
        self._update_controls()

    def set_busy(self, busy: bool, *, cancelable: bool | None = None) -> None:
        self._busy = bool(busy)
        self._cancelable = self._busy if cancelable is None else self._busy and bool(cancelable)
        self._update_controls()

    def set_prepared(self, prepared: bool) -> None:
        """Show whether the displayed configuration is awaiting explicit start."""
        self._prepared = bool(prepared)
        self.start_button.setText("按此配置开始" if self._prepared else "准备批量")
        self.cancel_button.setText("取消准备" if self._prepared and not self._busy else "取消队列")
        self._update_controls()

    def set_output_directory(self, path: str | Path | None) -> None:
        self._output_directory = Path(path).expanduser().absolute() if path else None
        if self._output_directory is not None:
            self.status_label.setText("本批输出位置：" + str(self._output_directory))
        self._update_controls()

    def set_configuration_summary(self, summary: Any) -> None:
        if isinstance(summary, Mapping):
            text = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        else:
            text = str(summary)
        self.configuration_label.setText(text or "当前批次配置尚未显示。")
        self.configuration_label.setToolTip(text)

    def set_timing_status(self, current: str, queue: str) -> None:
        """Show the active operation timing and the remaining queue estimate."""
        self.timing_label.setText(str(current))
        self.queue_timing_label.setText(str(queue))

    def set_preflight_messages(self, item_id: str, messages: Sequence[Any]) -> None:
        if isinstance(messages, (str, bytes)):
            normalized = (str(messages),)
        else:
            normalized = tuple(_display(message) for message in messages)
        self._preflight[str(item_id)] = tuple(message for message in normalized if message)
        self.set_queue_view(self._rows)
        shown = self._preflight[str(item_id)]
        if shown:
            self.status_label.setText(f"{item_id}：" + "；".join(shown))

    def _matches_filter(self, row: Mapping[str, Any]) -> bool:
        selected = self.filter_choice.currentData()
        attempts = row.get("attempts") or ()
        current = attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}
        analysis = str(row.get("analysis_status", row.get("status", "pending")))
        export = str(row.get("export_status", current.get("export_status", "not_requested")))
        if selected == "all":
            return True
        if selected == "pending":
            return analysis == "pending"
        if selected == "active":
            return analysis in _ANALYSIS_ACTIVE or export in _EXPORT_ACTIVE or export == "pending"
        if selected == "success":
            return analysis == "complete" and export in {"complete", "not_requested"}
        if selected == "partial":
            return analysis == "partial"
        if selected == "failed":
            return analysis == "failed" or export == "failed"
        if selected == "cancelled":
            return analysis == "cancelled" or export == "cancelled"
        if selected == "export_failed":
            return export == "failed"
        return True

    def _selected_rows(self) -> list[dict[str, Any]]:
        selected = set(self.selected_item_ids())
        return [row for row in self._rows if str(row.get("item_id", "")) in selected]

    def _update_controls(self, *_args: Any) -> None:
        selected = self._selected_rows()
        selected_ids = [str(row.get("item_id", "")) for row in selected]
        self.add_button.setEnabled(not self._busy)
        self.remove_button.setEnabled(not self._busy and bool(selected_ids))
        self.configure_button.setEnabled(not self._busy and bool(selected_ids))
        self.group_button.setEnabled(not self._busy)
        self.start_button.setEnabled(not self._busy and any(
            row.get("status", row.get("analysis_status", "pending")) == "pending"
            for row in self._rows
        ))
        self.cancel_button.setText("取消准备" if self._prepared and not self._busy else "取消队列")
        self.cancel_button.setEnabled((self._busy and self._cancelable) or (self._prepared and not self._busy))
        self.retry_button.setEnabled(not self._busy and any(
            (row.get("status", row.get("analysis_status")) in {"failed", "partial", "cancelled"}
             and row.get("structure_confirmed") is True)
            or (row.get("status", row.get("analysis_status")) == "pending"
                and row.get("structure_confirmed") in {True, False})
            for row in selected
        ))
        self.retry_export_button.setEnabled(not self._busy and any(
            row.get("export_status") in {"failed", "cancelled"} for row in selected
        ))
        self.view_result_button.setEnabled(bool(selected) and any(
            row.get("run_id") or (row.get("attempts") and row["attempts"][-1].get("run_id"))
            for row in selected
        ))
        self.open_directory_button.setEnabled(bool(self._output_directory) or (bool(selected) and any(
            row.get("output_paths") or (row.get("attempts") and row["attempts"][-1].get("output_paths"))
            for row in selected
        )))

    def _apply_filter(self, *_args: Any) -> None:
        for index, row in enumerate(self._rows):
            self.table.setRowHidden(index, not self._matches_filter(row))

    def _choose_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "添加待分析文件", "", "眼动数据 (*.csv *.tsv *.txt *.xlsx);;所有文件 (*)",
        )
        if paths:
            self.addFilesRequested.emit([str(path) for path in paths])

    def _start_or_prepare(self) -> None:
        if self._prepared and self._output_directory is not None:
            self.startRequested.emit(str(self._output_directory))
            return
        output_root = QFileDialog.getExistingDirectory(self, "选择批量结果输出文件夹")
        if output_root:
            self.startRequested.emit(str(output_root))

    def _emit_remove(self) -> None:
        selected = self.selected_item_ids()
        if selected:
            self.removeItemsRequested.emit(selected)

    def _emit_retry(self) -> None:
        selected = [str(row.get("item_id")) for row in self._selected_rows()
                    if (row.get("status", row.get("analysis_status")) in {"failed", "partial", "cancelled"}
                        and row.get("structure_confirmed") is True)
                    or (row.get("status", row.get("analysis_status")) == "pending"
                        and row.get("structure_confirmed") in {True, False})]
        if selected:
            self.retryItemsRequested.emit(selected)

    def _emit_retry_export(self) -> None:
        selected = [str(row.get("item_id")) for row in self._selected_rows()
                    if row.get("export_status") in {"failed", "cancelled"}]
        if selected:
            self.retryExportRequested.emit(selected)

    def _emit_view_result(self) -> None:
        for row in self._selected_rows():
            run_id = row.get("run_id")
            attempts = row.get("attempts") or ()
            if not run_id and attempts and isinstance(attempts[-1], Mapping):
                run_id = attempts[-1].get("run_id")
            if run_id:
                self.openResultRequested.emit(str(run_id))
                return

    def _open_selected_directory(self) -> None:
        for row in self._selected_rows():
            paths = row.get("output_paths") or ()
            attempts = row.get("attempts") or ()
            if not paths and attempts and isinstance(attempts[-1], Mapping):
                paths = attempts[-1].get("output_paths") or ()
            if paths:
                target = Path(str(paths[0])).parent
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
                return
        if self._output_directory is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_directory)))


__all__ = ["BatchQueueDialog"]
