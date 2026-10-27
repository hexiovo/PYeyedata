"""Live-schema metric selection, typed parameters, availability, and presets."""
from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from collections.abc import Mapping, Sequence
import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .forms import TypedParameterForm
from .method_registry import (
    family_selector_registry,
    group_parameter_fields,
    individual_method_registry,
    individual_parameter_fields,
)
from .models import SelectionSpec
from .presets import (
    MODULES,
    PresetDefinition,
    availability_records,
    builtin_presets,
    catalogue_for_module,
    catalogue_index,
    load_custom_preset,
    metric_selectable,
    resolve_preset,
    save_custom_preset,
    schema_version,
    selection_diff,
    validate_preset_definition,
)


_MODULE_LABELS = {
    "individual": "个体分析",
    "group": "群体分析",
    "advanced": "高级方法",
}
_FILTERS = (
    ("all", "全部"),
    ("available", "可用"),
    ("selected", "已选"),
    ("unavailable", "暂不可用"),
)
_PARAMETER_LABELS = {
    "advanced.min_sample_rate_hz": "最低采样率",
    "advanced.support_gap_s": "最大相邻间隔",
    "advanced.main_sequence_min_events": "主序列最少事件数",
    "advanced.main_sequence_min_amplitude_range_deg": "主序列最小幅度范围",
    "advanced.main_sequence_maxfev": "拟合最大函数评估数",
    "advanced.confidence": "置信水平",
    "advanced.uniform_tolerance": "均匀采样容差",
    "advanced.micro_lambda": "EK 椭圆阈值系数",
    "advanced.micro_min_duration_s": "微眼跳最短时长",
    "advanced.micro_max_amplitude_deg": "微眼跳最大幅度",
    "advanced.pupil_psd_eye": "瞳孔 PSD 眼别",
    "advanced.pupil_psd_fs_hz": "PSD 目标采样率",
    "advanced.pupil_psd_nperseg": "Welch 窗长",
    "advanced.pupil_psd_noverlap": "Welch 重叠点数",
    "advanced.pupil_psd_window": "Welch 窗函数",
    "advanced.pupil_psd_detrend": "去趋势方法",
    "advanced.pupil_psd_bands": "功率积分频带",
    "advanced.pupil_psd_baseline_corrected": "使用基线校正信号",
    "advanced.pupil_psd_min_coverage": "最低有效覆盖率",
    "advanced.perclos_column": "眼睑测量列",
    "advanced.eyelid_signal_kind": "眼睑信号类型",
    "advanced.perclos_threshold": "闭合阈值 P80",
    "advanced.perclos_min_window_s": "最短分析窗口",
    "advanced.perclos_min_coverage": "最低有效覆盖率",
    "advanced.perclos_exclude_blinks": "排除已确认眨眼",
    "enable_experimental": "启用研究/实验方法",
    "max_gap_s": "最大有效间隔",
    "alignment_tolerance_s": "时间对齐容差",
    "reference_event_s": "首次注视参考时刻（秒）",
    "fixation_observation_confirmed": "注视分类覆盖观察窗已确认",
    "reading_order_confirmed": "AOI 阅读推进顺序已确认",
    "reading_direction": "阅读方向",
    "reading_scope_id": "阅读范围 ID",
    "event_source_confirmed": "外部事件来源已确认",
    "event_source": "外部事件来源",
    "event_algorithm_version": "外部事件分类版本",
    "event_source_types": "来源覆盖事件类型（JSON）",
    "blink_source_confirmed": "眨眼事件来源已确认",
}


def _display(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_display(item) for item in value)
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_display(item)}" for key, item in value.items())
    try:
        if value != value:  # NaN
            return ""
    except Exception:
        pass
    return str(value)


def _availability_ready(row: Mapping[str, Any]) -> bool | None:
    status = str(row.get("status", "")).casefold()
    if status == "preflight_ready":
        return True
    value = row.get("available")
    if isinstance(value, bool):
        return value
    if value is not None:
        text = str(value).strip().casefold()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    if status in {"available", "complete", "ready"}:
        return True
    if status in {"unavailable", "blocked", "failed", "missing"}:
        return False
    return None


def _availability_text(entry: Mapping[str, Any], rows: list[dict[str, Any]]) -> tuple[str, QColor]:
    if entry.get("implemented") is False:
        return "未实现", QColor("#7a8492")
    if rows:
        ready = [_availability_ready(row) for row in rows]
        yes = sum(value is True for value in ready)
        no = sum(value is False for value in ready)
        if yes and no:
            return f"部分可用 {yes}/{len(rows)}", QColor("#b7791f")
        if yes:
            if entry.get("experimental") or str(entry.get("tier", "")).upper() == "R2":
                return "研究方法 · 前提满足", QColor("#b7791f")
            return "前检条件满足", QColor("#22805a")
        if no:
            return "暂不可用", QColor("#7a8492")
        return "前检待复核", QColor("#b7791f")
    if entry.get("experimental") or str(entry.get("tier", "")).upper() == "R2":
        return "研究方法 · 待前检", QColor("#b7791f")
    return "待前检", QColor("#687386")


class MetricReasonDialog(QDialog):
    """Accessible, row-preserving explanation for an unavailable metric."""

    edit_requested = Signal(object)

    def __init__(
        self,
        entry: Mapping[str, Any],
        module: str,
        availability_rows: Sequence[Mapping[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        metric_id = str(entry.get("metric_id", "unknown metric"))
        self.setWindowTitle("指标条件说明")
        self.setMinimumSize(560, 360)
        layout = QVBoxLayout(self)
        title = QLabel(f"{_display(entry.get('label'))}（{metric_id}）", self)
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 600; font-size: 16px;")
        layout.addWidget(title)
        body = QTextBrowser(self)
        body.setOpenExternalLinks(False)
        body.setPlainText(self._format_details(entry, module, availability_rows))
        layout.addWidget(body, 1)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        self.edit_button = controls.addButton("定位修改入口", QDialogButtonBox.ButtonRole.ActionRole)
        self.edit_button.setAccessibleName(f"定位 {metric_id} 的条件修改入口")
        edit_payload = {
            "module": module,
            "metric_id": metric_id,
            "entry": dict(entry),
            "availability_rows": [dict(row) for row in availability_rows],
        }

        def request_edit(_checked: bool = False) -> None:
            self.edit_requested.emit(edit_payload)
            self.accept()

        self.edit_button.clicked.connect(request_edit)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    @staticmethod
    def _format_details(
        entry: Mapping[str, Any], module: str, rows: Sequence[Mapping[str, Any]]
    ) -> str:
        lines = [f"模块：{_MODULE_LABELS.get(module, module)}"]
        description = entry.get("description") or entry.get("note")
        if description:
            lines.extend(["", "用途与计算说明：", _display(description)])
        for label, key in (
            ("分类", "category"),
            ("单位", "unit"),
            ("算法", "algorithm_id"),
            ("算法版本", "algorithm_version"),
            ("所需字段", "requires_fields"),
            ("所需元数据", "requires_metadata"),
            ("验证状态", "validation_status"),
            ("方法等级", "tier"),
            ("设备要求", "device_requirement"),
        ):
            value = entry.get(key)
            if value not in (None, "", [], ()):
                lines.append(f"{label}：{_display(value)}")
        if entry.get("device_validated") is False:
            lines.append("设备验证：未验证；目录或源码哈希不能替代设备证据。")
        if entry.get("implemented") is False:
            lines.append("当前公共 schema 标记：未实现；保留该目录项但不能将其提交为可运行指标。")
        if rows:
            lines.extend(["", f"前检条件行（保留全部 scope/stream，不合并）：{len(rows)}"])
            for index, row in enumerate(rows, 1):
                keys = (
                    "module", "scope", "analysis_stream_id", "available", "status",
                    "reason_code", "reason", "required", "metadata",
                )
                pairs = [f"{key}={_display(row.get(key))}" for key in keys if row.get(key) is not None]
                lines.append(f"{index}. " + ("；".join(pairs) or _display(row)))
        else:
            lines.extend(["", "尚无对应前检行。运行前检后会按 scope 和 analysis stream 更新条件。"])
        lines.extend([
            "",
            "前检满足只表示输入条件可尝试；运行仍可能返回 NA、partial 或运行时数值支持不足。",
            "筛选和搜索只改变列表视图，不更改已选 ID。",
        ])
        return "\n".join(lines)


class MetricCatalogueWidget(QWidget):
    """A searchable live catalogue that retains selections across all filters."""

    selection_changed = Signal(str, object)
    reason_requested = Signal(object)
    edit_requirements_requested = Signal(object)
    selection_reconciled = Signal(object)
    experimental_changed = Signal(str, bool)

    def __init__(self, schema: Mapping[str, Any] | None = None, *, module: str = "individual",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._schema: dict[str, Any] = {}
        self._module = module if module in MODULES else "individual"
        self._catalogue: list[dict[str, Any]] = []
        self._selected: dict[str, set[str]] = {name: set() for name in MODULES}
        self._availability: Any = None
        self._allow_experimental: dict[str, bool] = {name: False for name in MODULES}
        self._orphan_ids: dict[str, tuple[str, ...]] = {name: () for name in MODULES}
        self._rebuilding = False

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索中文标签、英文说明或 metric_id")
        self.search.setClearButtonEnabled(True)
        self.category_filter = QComboBox(self)
        self.state_filter = QComboBox(self)
        for key, label in _FILTERS:
            self.state_filter.addItem(label, key)
        self.experimental_toggle = QCheckBox("启用研究/实验方法", self)
        self.experimental_toggle.setToolTip("仅启用目录中标记为 experimental 的方法；不会代表设备验证。")
        controls.addWidget(self.search, 2)
        controls.addWidget(self.category_filter, 1)
        controls.addWidget(self.state_filter, 1)
        controls.addWidget(self.experimental_toggle)
        layout.addLayout(controls)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["指标（metric_id）", "用途 / 计算摘要", "状态", "选择 / 原因"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setSortingEnabled(False)
        self.tree.setColumnWidth(0, 360)
        self.tree.setColumnWidth(1, 420)
        self.tree.setColumnWidth(2, 180)
        self.tree.setColumnWidth(3, 150)
        layout.addWidget(self.tree, 1)
        self.detail = QTextBrowser(self)
        self.detail.setMaximumHeight(115)
        self.detail.setPlaceholderText("选中目录项后显示描述、条件字段和证据引用。")
        layout.addWidget(self.detail)

        self.search.textChanged.connect(self._refresh_visibility)
        self.category_filter.currentIndexChanged.connect(self._refresh_visibility)
        self.state_filter.currentIndexChanged.connect(self._refresh_visibility)
        self.experimental_toggle.toggled.connect(self._on_experimental_toggled)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._update_detail)
        self.tree.itemActivated.connect(self._on_item_activated)
        if schema is not None:
            self.set_schema(schema)

    @property
    def module(self) -> str:
        return self._module

    def set_module(self, module: str) -> None:
        if module not in MODULES or module == self._module:
            return
        self._module = module
        self.experimental_toggle.blockSignals(True)
        self.experimental_toggle.setChecked(self._allow_experimental[module])
        self.experimental_toggle.blockSignals(False)
        self._catalogue = catalogue_for_module(self._schema, module)
        self._refresh_categories()
        self._rebuild_tree()

    def focus_metric(self, metric_id: str) -> bool:
        """Select an exact catalogue row without changing its checked state."""
        if not isinstance(metric_id, str) or not metric_id:
            return False
        self.search.clear()
        for combo in (self.category_filter, self.state_filter):
            index = combo.findData("all")
            if index >= 0:
                combo.setCurrentIndex(index)

        def find(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.ItemDataRole.UserRole) == metric_id:
                return item
            for child_index in range(item.childCount()):
                result = find(item.child(child_index))
                if result is not None:
                    return result
            return None

        match = None
        for index in range(self.tree.topLevelItemCount()):
            match = find(self.tree.topLevelItem(index))
            if match is not None:
                break
        if match is None:
            return False
        ancestor = match.parent()
        while ancestor is not None:
            ancestor.setExpanded(True)
            ancestor = ancestor.parent()
        self.tree.setCurrentItem(match)
        self.tree.scrollToItem(match)
        return True

    def set_schema(self, schema: Mapping[str, Any]) -> None:
        self._schema = dict(schema)
        self._catalogue = catalogue_for_module(self._schema, self._module)
        for module in MODULES:
            live_ids = {entry["metric_id"] for entry in catalogue_for_module(self._schema, module)}
            self._orphan_ids[module] = tuple(sorted(self._selected[module] - live_ids))
        self._refresh_categories()
        self._rebuild_tree()

    def set_availability(self, availability: Any) -> tuple[str, ...]:
        self._availability = availability.copy(deep=True) if hasattr(availability, "copy") else availability
        removed: list[str] = []
        if self._availability is not None:
            indexes = catalogue_index(self._schema)
            for module in MODULES:
                for metric_id in tuple(self._selected[module]):
                    entry = indexes[module].get(metric_id)
                    if entry is None:
                        continue
                    can_select, _ = metric_selectable(
                        entry, module, availability=self._availability,
                        allow_experimental=self._allow_experimental[module],
                    )
                    rows = availability_records(self._availability, metric_id, module)
                    has_blocked_rows = bool(rows) and all(_availability_ready(row) is False for row in rows)
                    if (not can_select or has_blocked_rows) and rows:
                        self._selected[module].discard(metric_id)
                        removed.append(f"{module}:{metric_id}")
            if removed:
                self.selection_reconciled.emit(tuple(removed))
                self.selection_changed.emit(self._module, self.selected_ids(self._module))
        self._rebuild_tree()
        return tuple(removed)

    def set_allow_experimental(self, module: str, enabled: bool) -> None:
        if module not in MODULES:
            return
        self._allow_experimental[module] = bool(enabled)
        if module == self._module:
            self.experimental_toggle.blockSignals(True)
            self.experimental_toggle.setChecked(bool(enabled))
            self.experimental_toggle.blockSignals(False)
        if not enabled:
            indexes = catalogue_index(self._schema)
            removed = []
            for metric_id in tuple(self._selected[module]):
                entry = indexes[module].get(metric_id)
                if entry and entry.get("experimental"):
                    self._selected[module].discard(metric_id)
                    removed.append(f"{module}:{metric_id}")
            if removed:
                self.selection_reconciled.emit(tuple(removed))
                self.selection_changed.emit(module, self.selected_ids(module))
        self._rebuild_tree()

    def set_selection_ids(self, module: str, metric_ids: Sequence[str]) -> tuple[str, ...]:
        if module not in MODULES:
            return tuple(metric_ids)
        known = {entry["metric_id"] for entry in catalogue_for_module(self._schema, module)}
        values = {str(metric_id) for metric_id in metric_ids if isinstance(metric_id, str)}
        self._selected[module] = values & known
        self._orphan_ids[module] = tuple(sorted(values - known))
        self._rebuild_tree()
        return self._orphan_ids[module]

    def selected_ids(self, module: str | None = None) -> tuple[str, ...]:
        name = module or self._module
        if name not in MODULES:
            return ()
        return tuple(sorted(self._selected[name]))

    def selectable_ids(self, module: str | None = None) -> tuple[str, ...]:
        name = module or self._module
        return tuple(sorted(
            entry["metric_id"] for entry in catalogue_for_module(self._schema, name)
            if metric_selectable(entry, name, availability=self._availability,
                                 allow_experimental=self._allow_experimental[name])[0]
        ))

    def catalogue(self, module: str | None = None) -> tuple[dict[str, Any], ...]:
        return tuple(dict(entry) for entry in catalogue_for_module(self._schema, module or self._module))

    def _on_experimental_toggled(self, enabled: bool) -> None:
        self._allow_experimental[self._module] = bool(enabled)
        self.set_allow_experimental(self._module, enabled)
        self.experimental_changed.emit(self._module, bool(enabled))

    def _refresh_categories(self) -> None:
        current = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("全部分类", "all")
        categories = sorted({str(entry.get("category")) for entry in self._catalogue
                             if entry.get("category") not in (None, "")})
        for category in categories:
            self.category_filter.addItem(category, category)
        index = self.category_filter.findData(current)
        self.category_filter.setCurrentIndex(index if index >= 0 else 0)
        self.category_filter.blockSignals(False)

    def _entry_state(self, entry: Mapping[str, Any]) -> tuple[bool, str, QColor, list[dict[str, Any]], str]:
        metric_id = str(entry.get("metric_id", ""))
        rows = availability_records(self._availability, metric_id, self._module)
        selectable, reason = metric_selectable(
            entry, self._module, availability=self._availability,
            allow_experimental=self._allow_experimental[self._module],
        )
        status, color = _availability_text(entry, rows)
        if entry.get("experimental") and not self._allow_experimental[self._module]:
            status, color = "需显式启用研究方法", QColor("#7a8492")
            reason = "这是目录标记的研究/实验方法；启用复选框后仍会保持琥珀色条件状态，不表示设备验证。"
        elif entry.get("experimental") or str(entry.get("tier", "")).upper() == "R2":
            status, color = "研究方法 · 条件状态", QColor("#b7791f")
        return selectable, status, color, rows, reason

    def _rebuild_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        self._rebuilding = True
        self.tree.blockSignals(True)
        self.tree.clear()
        entries = {entry["metric_id"]: entry for entry in self._catalogue}
        item_for: dict[str, QTreeWidgetItem] = {}
        parent_for: dict[str, str | None] = {}
        def depth(metric_id: str) -> int:
            result = 0
            parent_id = parent_for.get(metric_id)
            while parent_id is not None and result < len(parent_for):
                result += 1
                parent_id = parent_for.get(parent_id)
            return result

        for metric_id, entry in sorted(entries.items(), key=lambda item: (depth(item[0]), item[0])):
            parent_id = entry.get("parent_metric_id")
            if not isinstance(parent_id, str) or parent_id not in entries:
                prefixes = [candidate for candidate in entries if metric_id.startswith(candidate + ".")
                            and candidate != metric_id]
                parent_id = max(prefixes, key=len) if prefixes else None
            parent_for[metric_id] = parent_id

        for metric_id, entry in entries.items():
            parent_id = parent_for[metric_id]
            parent_item = item_for.get(parent_id) if parent_id else None
            label = _display(entry.get("label")) or metric_id
            title = f"{label} ({metric_id})"
            summary = _display(entry.get("description") or entry.get("note") or entry.get("unit") or "")
            item = QTreeWidgetItem(parent_item or self.tree, [title, summary, "", ""])
            selectable, status, color, rows, reason = self._entry_state(entry)
            item.setData(0, Qt.ItemDataRole.UserRole, metric_id)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, dict(entry))
            item.setData(0, Qt.ItemDataRole.UserRole + 2, selectable)
            item.setData(0, Qt.ItemDataRole.UserRole + 3, rows)
            item.setText(2, status)
            item.setForeground(2, QBrush(color))
            tooltip = "\n".join(filter(None, [
                f"ID: {metric_id}",
                f"Category: {_display(entry.get('category'))}",
                f"Unit: {_display(entry.get('unit'))}",
                f"Description: {_display(entry.get('description') or entry.get('note'))}",
                f"Reason: {reason}" if not selectable else "",
            ]))
            for column in range(4):
                item.setToolTip(column, tooltip)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if selectable:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)
                item.setCheckState(3, Qt.CheckState.Checked if metric_id in self._selected[self._module]
                                   else Qt.CheckState.Unchecked)
            else:
                item.setFlags(flags)
                button = QPushButton("查看原因", self.tree)
                button.setAccessibleName(f"查看 {metric_id} 的条件原因")
                button.setToolTip(reason or "当前前检条件不满足")
                button.clicked.connect(lambda _checked=False, e=dict(entry), r=rows:
                                        self._show_reason(e, r))
                self.tree.setItemWidget(item, 3, button)
            item_for[metric_id] = item
            if parent_id:
                parent_item = item_for.get(parent_id)
                if parent_item is not None:
                    parent_item.setExpanded(True)
        self.tree.blockSignals(False)
        self._rebuilding = False
        self._refresh_visibility()
        self._update_detail()

    def _refresh_visibility(self) -> None:
        if not hasattr(self, "tree"):
            return
        query = self.search.text().strip().casefold()
        category = self.category_filter.currentData()
        state = self.state_filter.currentData()

        def row_matches(item: QTreeWidgetItem) -> bool:
            metric_id = item.data(0, Qt.ItemDataRole.UserRole)
            if not metric_id:
                return False
            entry = item.data(0, Qt.ItemDataRole.UserRole + 1) or {}
            text = " ".join(_display(entry.get(key)) for key in (
                "metric_id", "label", "category", "description", "note", "algorithm_id", "unit"
            )).casefold()
            if query and query not in text:
                return False
            if category not in (None, "all") and str(entry.get("category", "")) != str(category):
                return False
            selected = metric_id in self._selected[self._module]
            selectable = bool(item.data(0, Qt.ItemDataRole.UserRole + 2))
            if state == "available" and not selectable:
                return False
            if state == "selected" and not selected:
                return False
            if state == "unavailable" and selectable:
                return False
            return True

        def update_branch(parent: QTreeWidgetItem) -> bool:
            visible_descendant = False
            for index in range(parent.childCount()):
                child = parent.child(index)
                child_visible = row_matches(child)
                child_descendant = update_branch(child)
                child.setHidden(not child_visible and not child_descendant)
                visible_descendant = visible_descendant or child_visible or child_descendant
            return visible_descendant

        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            visible = row_matches(item)
            descendant = update_branch(item)
            item.setHidden(not visible and not descendant)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._rebuilding or column != 3:
            return
        metric_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not metric_id:
            return
        if item.checkState(3) == Qt.CheckState.Checked:
            if not bool(item.data(0, Qt.ItemDataRole.UserRole + 2)):
                self.tree.blockSignals(True)
                item.setCheckState(3, Qt.CheckState.Unchecked)
                self.tree.blockSignals(False)
                self._show_reason(item.data(0, Qt.ItemDataRole.UserRole + 1),
                                  item.data(0, Qt.ItemDataRole.UserRole + 3) or [])
                return
            self._selected[self._module].add(str(metric_id))
        else:
            self._selected[self._module].discard(str(metric_id))
        self._refresh_visibility()
        self.selection_changed.emit(self._module, self.selected_ids(self._module))

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        metric_id = item.data(0, Qt.ItemDataRole.UserRole)
        if metric_id and not bool(item.data(0, Qt.ItemDataRole.UserRole + 2)):
            self._show_reason(item.data(0, Qt.ItemDataRole.UserRole + 1),
                              item.data(0, Qt.ItemDataRole.UserRole + 3) or [])

    def _show_reason(self, entry: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
        payload = {"module": self._module, "metric_id": entry.get("metric_id"),
                   "entry": dict(entry), "availability_rows": [dict(row) for row in rows]}
        self.reason_requested.emit(payload)
        dialog = MetricReasonDialog(entry, self._module, rows, self)
        dialog.edit_requested.connect(self.edit_requirements_requested.emit)
        dialog.exec()

    def _update_detail(self) -> None:
        items = self.tree.selectedItems() if hasattr(self, "tree") else []
        if not items:
            self.detail.clear()
            return
        item = items[0]
        entry = item.data(0, Qt.ItemDataRole.UserRole + 1) or {}
        metric_id = item.data(0, Qt.ItemDataRole.UserRole)
        rows = item.data(0, Qt.ItemDataRole.UserRole + 3) or []
        lines = [f"{_display(entry.get('label'))} ({metric_id})"]
        for key, title in (("description", "说明"), ("note", "备注"), ("requires_fields", "所需字段"),
                           ("requires_metadata", "所需元数据"), ("reference_ids", "参考来源")):
            value = entry.get(key)
            if value not in (None, "", [], ()):
                lines.append(f"{title}：{_display(value)}")
        if entry.get("device_validated") is False:
            lines.append("设备验证状态：未验证")
        if rows:
            lines.append(f"前检条件行：{len(rows)}（按 scope/stream 保留，灰色项可打开完整原因）")
        self.detail.setPlainText("\n".join(lines))


class _PresetDiffDialog(QDialog):
    def __init__(self, preset: PresetDefinition, resolution: Any,
                 diff: Mapping[str, Mapping[str, Sequence[str]]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("预设差异确认")
        self.setMinimumSize(580, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"应用“{preset.label}”前核对差异。", self))
        if resolution.incompatible_schema:
            layout.addWidget(QLabel("预设保存时的 schema 版本不同；只恢复当前 schema 仍识别且可用的 ID。", self))
        body = QTextBrowser(self)
        lines: list[str] = []
        for module in MODULES:
            if module not in preset.target_modules:
                continue
            values = diff.get(module, {})
            lines.append(f"{_MODULE_LABELS[module]}：")
            for key, title in (("added", "新增"), ("removed", "移除"), ("retained", "保留")):
                ids = tuple(values.get(key, ()))
                lines.append(f"  {title} ({len(ids)}): " + (", ".join(ids) if ids else "无"))
            blocked = tuple(resolution.blocked.get(module, ()))
            unknown = tuple(resolution.unknown.get(module, ()))
            lines.append(f"  当前不可用，未勾选 ({len(blocked)}): " + (", ".join(blocked) if blocked else "无"))
            lines.append(f"  当前 schema 未识别 ({len(unknown)}): " + (", ".join(unknown) if unknown else "无"))
        changed_parameters = len(resolution.parameters)
        if changed_parameters:
            lines.extend(["", f"预设带有 {changed_parameters} 个 typed 参数值；空值、False、enum 值和矩阵会按其类型恢复。"])
        lines.extend(["", "应用后会请求新前检；前检状态不会被预设或筛选覆盖。"])
        body.setPlainText("\n".join(lines))
        layout.addWidget(body, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel,
                                        parent=self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class MetricsPage(QWidget):
    """Analysis selection page. MainWindow owns bridge, workers, and run dispatch."""

    selection_changed = Signal(object)
    parameters_changed = Signal(object)
    preflight_requested = Signal()
    edit_requirements_requested = Signal(object)
    selection_reconciled = Signal(object)

    def __init__(self, schema: Mapping[str, Any] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._schema: dict[str, Any] = {}
        self._selected: dict[str, set[str]] = {module: set() for module in MODULES}
        self._run_enabled: dict[str, bool] = {module: False for module in MODULES}
        self._allow_experimental: dict[str, bool] = {module: False for module in MODULES}
        self._availability: Any = None
        self._orphan_ids: dict[str, tuple[str, ...]] = {module: () for module in MODULES}
        self._forms: dict[str, TypedParameterForm] = {}
        self._defaults_by_module: dict[str, dict[str, Any]] = {}
        self.catalogue = MetricCatalogueWidget(module="individual", parent=self)
        self.catalogue.selection_changed.connect(self._on_module_selection_changed)
        self.catalogue.experimental_changed.connect(self._on_experimental_changed)
        self.catalogue.edit_requirements_requested.connect(self.edit_requirements_requested.emit)
        self.catalogue.selection_reconciled.connect(self._on_selection_reconciled)

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.module_combo = QComboBox(self)
        for module in MODULES:
            self.module_combo.addItem(_MODULE_LABELS[module], module)
        self.run_module = QCheckBox("运行此模块", self)
        self.run_module.setToolTip("模块开关与所选 ID 分开；显式运行空列表会保留后端空请求契约。")
        self.restore_button = QPushButton("恢复本模块默认", self)
        header.addWidget(QLabel("分析模块", self))
        header.addWidget(self.module_combo)
        header.addWidget(self.run_module)
        header.addStretch(1)
        header.addWidget(self.restore_button)
        root.addLayout(header)

        self.parameter_error = QLabel(self)
        self.parameter_error.setObjectName("parameterErrorSummary")
        self.parameter_error.setWordWrap(True)
        self.parameter_error.setStyleSheet("QLabel { color: #8b1e1e; background: #fff0f0; padding: 6px; }")
        self.parameter_error.hide()
        self.parameter_error_button = QPushButton("定位并修改", self)
        self.parameter_error_button.setEnabled(False)
        self.parameter_error_button.clicked.connect(self._focus_parameter_error)
        error_row = QHBoxLayout()
        error_row.addWidget(self.parameter_error, 1)
        error_row.addWidget(self.parameter_error_button)
        root.addLayout(error_row)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.catalogue, "指标目录")
        self.parameter_tabs = QTabWidget(self.tabs)
        self.tabs.addTab(self.parameter_tabs, "Typed 参数")
        self.preset_tab = QWidget(self.tabs)
        self._build_preset_tab()
        self.tabs.addTab(self.preset_tab, "预设")
        root.addWidget(self.tabs, 1)
        self.module_combo.currentIndexChanged.connect(self._on_module_changed)
        self.run_module.toggled.connect(self._on_run_toggled)
        self.restore_button.clicked.connect(self.restore_current_module_defaults)
        if schema is not None:
            self.set_schema(schema)

    def set_schema(self, schema: Mapping[str, Any]) -> None:
        self._schema = deepcopy(dict(schema))
        self._attach_individual_bindings(self._schema)
        self.catalogue.set_schema(self._schema)
        self._build_parameter_tabs()
        self._populate_presets()
        for module in MODULES:
            live_ids = {entry["metric_id"] for entry in catalogue_for_module(self._schema, module)}
            self._orphan_ids[module] = tuple(sorted(self._selected[module] - live_ids))

    @staticmethod
    def _attach_individual_bindings(schema: dict[str, Any]) -> None:
        individual = schema.get("individual")
        if not isinstance(individual, dict):
            return
        leaves = individual_method_registry()
        selectors = family_selector_registry()
        for entry in individual.get("catalogue", ()):
            if not isinstance(entry, dict):
                continue
            metric_id = entry.get("metric_id")
            binding = leaves.get(metric_id, selectors.get(metric_id))
            if binding is None:
                continue
            if metric_id in leaves:
                entry["registered_parameters"] = [
                    {
                        "name": field["name"],
                        "config_path": field["config_path"],
                        "consumer": field["consumer"],
                        "unit": field.get("unit"),
                    }
                    for field in binding["parameter_definitions"]
                ]
                entry["named_context"] = deepcopy(binding["named_context"])
                entry["consumer"] = binding["consumer"]
            else:
                entry["selection_only"] = True
                entry["child_metric_ids"] = list(binding["children"])

    def set_availability(self, availability: Any) -> tuple[str, ...]:
        self._availability = availability.copy(deep=True) if hasattr(availability, "copy") else availability
        removed = self.catalogue.set_availability(self._availability)
        if removed:
            self.preflight_requested.emit()
        return removed

    def invalidate_preflight(self) -> None:
        """Call on input/config revision change; selections remain pending until a fresh preflight."""
        self._availability = None
        self.catalogue.set_availability(None)

    def set_selection(self, selection: SelectionSpec | Mapping[str, Any]) -> None:
        if isinstance(selection, SelectionSpec):
            values = {
                "individual": selection.individual_metric_ids,
                "group": selection.group_metric_ids,
                "advanced": selection.advanced_metric_ids,
            }
            self._run_enabled = {
                "individual": selection.run_individual,
                "group": selection.run_group,
                "advanced": selection.run_advanced,
            }
        else:
            values = {
                "individual": selection.get("individual_metric_ids", ()),
                "group": selection.get("group_metric_ids", ()),
                "advanced": selection.get("advanced_metric_ids", ()),
            }
            self._run_enabled = {
                "individual": bool(selection.get("run_individual", False)),
                "group": bool(selection.get("run_group", False)),
                "advanced": bool(selection.get("run_advanced", False)),
            }
        for module in MODULES:
            self._selected[module] = set(str(value) for value in values[module] if isinstance(value, str))
            known = {entry["metric_id"] for entry in catalogue_for_module(self._schema, module)}
            self._orphan_ids[module] = tuple(sorted(self._selected[module] - known))
            self._selected[module] -= set(self._orphan_ids[module])
        self._sync_current_module()
        self._emit_selection()

    def selection_spec(self) -> SelectionSpec:
        return SelectionSpec(
            individual_metric_ids=tuple(sorted(self._selected["individual"])),
            group_metric_ids=tuple(sorted(self._selected["group"])),
            advanced_metric_ids=tuple(sorted(self._selected["advanced"])),
            run_individual=bool(self._run_enabled["individual"]),
            run_group=bool(self._run_enabled["group"]),
            run_advanced=bool(self._run_enabled["advanced"]),
        )

    def _report_parameter_error(self, message: str, *, module: str | None = None,
                                field: str | None = None) -> None:
        text = str(message)
        if module not in MODULES:
            module = None
        if module is None or field is None:
            for candidate_module, form in self._forms.items():
                match = next((name for name in sorted(form.editors, key=len, reverse=True)
                              if text.startswith(name + ":") or text.startswith(name + " ")
                              or f"{candidate_module}.{name}:" in text), None)
                if match is not None:
                    module, field = candidate_module, match
                    break
        reason = text
        if module and field:
            for prefix in (field + ":", field + " ", module + "." + field + ":"):
                if reason.startswith(prefix):
                    reason = reason[len(prefix):].strip()
                    break
            control = self._forms.get(module).editors.get(field) if module in self._forms else None
            hint = control._range_hint() if control is not None else ""
            range_text = f"（{hint}）" if hint else ""
            location = f"{_MODULE_LABELS[module]} / {field}{range_text}"
            self.parameter_error.setText(f"参数错误：{location}：{reason}。请按显示的类型、单位和范围修正。")
            self._parameter_error_location = (module, field)
            self.parameter_error_button.setEnabled(True)
        else:
            self.parameter_error.setText(f"参数错误：{reason}。请检查当前方法的类型、单位和范围。")
            self._parameter_error_location = None
            self.parameter_error_button.setEnabled(False)
        self.parameter_error.show()

    def _clear_parameter_error(self) -> None:
        self._parameter_error_location = None
        self.parameter_error.clear()
        self.parameter_error.hide()
        self.parameter_error_button.setEnabled(False)

    def _focus_parameter_error(self) -> None:
        if self._parameter_error_location is None:
            return
        module, field = self._parameter_error_location
        self.set_module(module)
        self.tabs.setCurrentWidget(self.parameter_tabs)
        form_index = MODULES.index(module)
        if form_index < self.parameter_tabs.count():
            self.parameter_tabs.setCurrentIndex(form_index)
        form = self._forms.get(module)
        if form is not None:
            form.focus_field(field)

    def parameter_values(self) -> dict[str, Any]:
        """Read only fields used by enabled modules and their selected methods."""
        output: dict[str, Any] = {}
        active_module: str | None = None
        active_field: str | None = None
        try:
            if self._run_enabled["individual"]:
                active_module = "individual"
                selected = self._selected["individual"]
                definitions = individual_parameter_fields(selected) if selected else []
                names = {definition["name"] for definition in definitions}
                if selected:
                    names.add("max_gap_s")
                values = self._forms["individual"].values(names)
                individual: dict[str, Any] = {
                    "enable_experimental": bool(self._allow_experimental["individual"]),
                }
                if "max_gap_s" in values:
                    individual["max_gap_s"] = values["max_gap_s"]
                for definition in definitions:
                    name = definition["name"]
                    active_field = name
                    self._set_config_path(
                        individual, definition["config_path"], values[name], expected_module="individual"
                    )
                output["individual"] = individual

            if self._run_enabled["group"]:
                output["group"] = {"enable_experimental": bool(self._allow_experimental["group"])}

            if self._run_enabled["advanced"] and self._selected["advanced"]:
                active_module = "advanced"
                advanced_entries = {
                    entry["metric_id"]: entry for entry in catalogue_for_module(self._schema, "advanced")
                }
                wanted: set[str] = set()
                for metric_id in self._selected["advanced"]:
                    entry = advanced_entries.get(metric_id, {})
                    for definition in entry.get("parameter_definitions", ()):
                        if isinstance(definition, Mapping):
                            name = definition.get("name")
                            if isinstance(name, str):
                                wanted.add(name if name.startswith("advanced.") else "advanced." + name)
                values = self._forms["advanced"].values(sorted(wanted))
                output["advanced"] = values
            self._clear_parameter_error()
            return output
        except (TypeError, ValueError, OverflowError) as exc:
            message = str(exc)
            if active_module == "individual" and active_field is None:
                match = next((name for name in self._forms["individual"].editors
                              if message.startswith(name + ":")), None)
                active_field = match
            if active_module == "advanced":
                match = next((name for name in self._forms["advanced"].editors
                              if message.startswith(name + ":")), None)
                active_field = match
            self._report_parameter_error(message, module=active_module, field=active_field)
            raise

    def all_parameter_values(self) -> dict[str, Any]:
        """Serialize and validate every draft field for a complete custom preset."""
        raw: dict[str, dict[str, Any]] = {}
        active_module: str | None = None
        try:
            for module, form in self._forms.items():
                active_module = module
                raw[module] = form.values()
            output: dict[str, Any] = {}
            individual_values = raw.get("individual", {})
            individual: dict[str, Any] = {
                "enable_experimental": bool(self._allow_experimental["individual"]),
                "max_gap_s": individual_values.get("max_gap_s"),
            }
            for definition in individual_parameter_fields():
                name = definition["name"]
                self._set_config_path(
                    individual, definition["config_path"], individual_values[name],
                    expected_module="individual",
                )
            output["individual"] = individual
            output["group"] = dict(raw.get("group", {}))
            output["advanced"] = dict(raw.get("advanced", {}))
            self._clear_parameter_error()
            return output
        except (TypeError, ValueError, OverflowError) as exc:
            self._report_parameter_error(str(exc), module=active_module)
            raise

    @staticmethod
    def _set_config_path(
        target: dict[str, Any], path: str, value: Any, *, expected_module: str
    ) -> None:
        parts = path.split(".")
        if len(parts) < 2 or parts[0] != expected_module:
            raise ValueError(f"Invalid {expected_module} parameter path: {path}")
        current = target
        for part in parts[1:-1]:
            child = current.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"Conflicting nested parameter path: {path}")
            current = child
        current[parts[-1]] = value

    def _normalize_parameter_values(self, values: Mapping[str, Any], *, strict: bool) -> dict[str, dict[str, Any]]:
        if not isinstance(values, Mapping):
            raise TypeError("预设 parameters 必须是对象")
        if strict:
            unknown_modules = set(values) - set(MODULES)
            if unknown_modules:
                raise ValueError("parameters 包含未知模块：" + ", ".join(sorted(map(str, unknown_modules))))
        prepared: dict[str, dict[str, Any]] = {}
        for module in MODULES:
            if module not in values:
                continue
            section = values[module]
            if not isinstance(section, Mapping):
                raise ValueError(f"parameters.{module} 必须是对象")
            form = self._forms[module]
            editors = form.editors
            known = set(editors)
            flattened: dict[str, Any] = {}

            def collect(part: Mapping[str, Any], prefix: str = "") -> None:
                for raw_name, value in part.items():
                    if not isinstance(raw_name, str) or not raw_name:
                        raise ValueError(f"parameters.{module} 包含无效字段名")
                    name = f"{prefix}.{raw_name}" if prefix else raw_name
                    if module == "advanced" and name not in known and f"advanced.{name}" in known:
                        name = f"advanced.{name}"
                    if name in known:
                        flattened[name] = editors[name].normalize_serialized_value(value)
                    elif isinstance(value, Mapping):
                        if not any(field.startswith(name + ".") for field in known):
                            if strict:
                                raise ValueError(f"parameters.{module}.{name} 是未知字段")
                            continue
                        collect(value, name)
                    elif strict:
                        raise ValueError(f"parameters.{module}.{name} 是未知字段")

            collect(section)
            prepared[module] = form.validate_values(flattened, strict=True)
        return prepared

    def set_parameter_values(self, values: Mapping[str, Any], *, emit: bool = True,
                             strict: bool = False) -> None:
        try:
            prepared = self._normalize_parameter_values(values, strict=strict)
        except (TypeError, ValueError) as exc:
            self._report_parameter_error(str(exc))
            raise
        # Every target module is checked above, so widget changes below cannot
        # leave half of a multi-module preset restored.
        for module, section in prepared.items():
            self._forms[module].set_values(section, strict=True)
        if emit:
            self._emit_parameters_changed()

    def _emit_parameters_changed(self) -> None:
        try:
            values = self.parameter_values()
        except (TypeError, ValueError):
            # A partially typed value can be invalid until the user finishes
            # editing. Notify the host anyway so it can stale the prior run.
            values = None
        self.parameters_changed.emit(values)

    def _on_form_edited(self, _name: str) -> None:
        self._emit_parameters_changed()

    def unresolved_ids(self) -> dict[str, tuple[str, ...]]:
        return dict(self._orphan_ids)

    def restore_current_module_defaults(self) -> None:
        module = self.module_combo.currentData()
        entries = catalogue_for_module(self._schema, module)
        default_ids = [entry["metric_id"] for entry in entries
                       if str(entry.get("tier", "")).upper() == "R1"
                       and metric_selectable(entry, module, availability=self._availability,
                                             allow_experimental=self._allow_experimental[module])[0]]
        self._selected[module] = set(default_ids)
        form = self._forms.get(module)
        if form is not None:
            form.set_values(self._defaults_by_module.get(module, {}))
        self._run_enabled[module] = bool(default_ids)
        self._sync_current_module()
        self._emit_selection()
        self._emit_parameters_changed()
        self.preflight_requested.emit()

    def _build_parameter_tabs(self) -> None:
        if not hasattr(self, "parameter_tabs"):
            return
        while self.parameter_tabs.count():
            widget = self.parameter_tabs.widget(0)
            self.parameter_tabs.removeTab(0)
            widget.deleteLater()
        self._forms: dict[str, TypedParameterForm] = {}
        self._defaults_by_module: dict[str, dict[str, Any]] = {}
        self._parameter_error_location: tuple[str, str] | None = None
        schema_typed = self._schema.get("typed_parameters", {})
        for module in MODULES:
            if module == "advanced":
                definitions = self._schema.get("advanced", {}).get("typed_parameters", {})
                fields = list(definitions.values()) if isinstance(definitions, Mapping) else []
            elif module == "individual":
                fields = individual_parameter_fields()
                fields = [
                    {
                        **field,
                        "description": (
                            str(field.get("description", ""))
                            + f" 配置路径：{field['config_path']}；消费位置：{field['consumer']}。"
                        ),
                    }
                    for field in fields
                ] + self._registered_legacy_fields(module, schema_typed)
            else:
                fields = self._registered_legacy_fields(module, schema_typed)
            panel = QWidget(self.parameter_tabs)
            panel_layout = QVBoxLayout(panel)
            hint = QLabel(self._parameter_hint(module), panel)
            hint.setWordWrap(True)
            panel_layout.addWidget(hint)
            scroll = QScrollArea(panel)
            scroll.setWidgetResizable(True)
            form = TypedParameterForm(fields, parent=scroll)
            form.edited.connect(self._on_form_edited)
            self._localize_form_labels(form)
            scroll.setWidget(form)
            panel_layout.addWidget(scroll, 1)
            self.parameter_tabs.addTab(panel, _MODULE_LABELS[module])
            self._forms[module] = form
            self._defaults_by_module[module] = form.values()

    @staticmethod
    def _registered_legacy_fields(module: str, schema_typed: Mapping[str, Any]) -> list[dict[str, Any]]:
        # These are real mapping keys from the frozen public schema/binding.
        if module == "individual":
            keys = ("enable_experimental", "max_gap_s")
        else:
            # Group-specific options live in GroupDesignPage so they are edited
            # once alongside the explicit member scope and pair preview.
            keys = ("enable_experimental",)
        fields: list[dict[str, Any]] = []
        for key in keys:
            if key == "enable_experimental":
                fields.append({"name": key, "type": "boolean", "default": False,
                               "description": "仅允许显式选择目录标记为研究/实验的方法；不是设备认证。"})
            else:
                fields.append({"name": key, "type": "optional_float", "default": None,
                               "unit": "s", "minimum_exclusive": 0.0,
                               "description": "留空时由后端使用其真实默认值；设置后按此间隔限制。"})
        return fields

    @staticmethod
    def _parameter_hint(module: str) -> str:
        if module == "advanced":
            return "参数定义从实时公共 schema 读取。可选 None、False、enum 和频带列表按原类型提交；研究方法仍需运行时数值支持，设备状态保持未验证。"
        if module == "individual":
            return "方法参数来自 A02 leaf 注册表；只把已选 leaf 所需值送入对应消费者。None、False、单位和嵌套路径均按定义保留；实验开关单独绑定到 enable_experimental。"
        return "群体分析开关在此显式设置；群体参数、成员、角色、时钟和空间证据在群体设计页统一配置。"

    @staticmethod
    def _localize_form_labels(form: TypedParameterForm) -> None:
        layout = form.layout()
        for name, editor in form.editors.items():
            label = layout.labelForField(editor)
            if label is not None:
                label.setText(_PARAMETER_LABELS.get(name, name.rsplit(".", 1)[-1].replace("_", " ")))

    def _build_preset_tab(self) -> None:
        layout = QVBoxLayout(self.preset_tab)
        controls = QHBoxLayout()
        self.preset_combo = QComboBox(self.preset_tab)
        self.apply_preset_button = QPushButton("查看差异并应用", self.preset_tab)
        self.save_preset_button = QPushButton("保存自定义…", self.preset_tab)
        self.load_preset_button = QPushButton("载入自定义…", self.preset_tab)
        controls.addWidget(self.preset_combo, 1)
        controls.addWidget(self.apply_preset_button)
        controls.addWidget(self.save_preset_button)
        controls.addWidget(self.load_preset_button)
        layout.addLayout(controls)
        self.preset_description = QLabel("预设应用前会显示新增、移除、不可用和不兼容 ID。", self.preset_tab)
        self.preset_description.setWordWrap(True)
        layout.addWidget(self.preset_description)
        layout.addStretch(1)
        self._presets: list[PresetDefinition] = []
        self.apply_preset_button.clicked.connect(self._apply_selected_preset)
        self.save_preset_button.clicked.connect(self._save_custom_preset)
        self.load_preset_button.clicked.connect(self._load_custom_preset)

    def _populate_presets(self) -> None:
        if not hasattr(self, "preset_combo"):
            return
        self._presets = builtin_presets(self._schema)
        self.preset_combo.clear()
        for preset in self._presets:
            self.preset_combo.addItem(preset.label, preset.preset_id)
        self.preset_combo.setCurrentIndex(0 if self._presets else -1)

    def _current_selections(self) -> dict[str, tuple[str, ...]]:
        return {module: tuple(sorted(self._selected[module])) for module in MODULES}

    def _experimental_map(self) -> dict[str, bool]:
        return dict(self._allow_experimental)

    def _apply_preset(self, preset: PresetDefinition) -> bool:
        try:
            validate_preset_definition(preset)
            prepared_parameters = self._normalize_parameter_values(preset.parameters, strict=True)
            current = self._current_selections()
            resolution = resolve_preset(
                preset, self._schema, current_selections=current, availability=self._availability,
                allow_experimental=self._experimental_map(),
            )
        except (TypeError, ValueError, KeyError) as exc:
            self._report_parameter_error(str(exc))
            return False
        diff = selection_diff(current, resolution.selections, preset.target_modules)
        dialog = _PresetDiffDialog(preset, resolution, diff, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        for module, parameters in prepared_parameters.items():
            self._forms[module].set_values(parameters, strict=True)
        self._selected = {module: set(resolution.selections.get(module, ())) for module in MODULES}
        for module in preset.target_modules:
            self._run_enabled[module] = bool(self._selected[module])
        self._sync_current_module()
        self._clear_parameter_error()
        self._emit_selection()
        self._emit_parameters_changed()
        self.preflight_requested.emit()
        return True

    def _apply_selected_preset(self) -> None:
        index = self.preset_combo.currentIndex()
        if 0 <= index < len(self._presets):
            self._apply_preset(self._presets[index])

    def _save_custom_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, accepted = QInputDialog.getText(self, "保存自定义预设", "预设名称")
        if not accepted or not name.strip():
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存自定义预设", f"{name.strip()}.json",
                                              "PYeyedata preset (*.json)")
        if not path:
            return
        try:
            save_custom_preset(path, name=name.strip(), selections=self._current_selections(),
                               parameters=self.all_parameter_values(), schema=self._schema)
        except (OSError, TypeError, ValueError) as exc:
            self._report_parameter_error(str(exc))
            QMessageBox.critical(self, "保存失败", str(exc))

    def _load_custom_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "载入自定义预设", "", "PYeyedata preset (*.json)")
        if not path:
            return
        try:
            preset = load_custom_preset(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:  # type: ignore[name-defined]
            self._report_parameter_error(str(exc))
            QMessageBox.critical(self, "预设无法读取", str(exc))
            return
        self._apply_preset(preset)

    def _on_module_changed(self, _index: int) -> None:
        self._sync_current_module()

    def set_module(self, module: str) -> None:
        """Select an analysis module without changing any metric IDs."""
        if module not in MODULES:
            raise ValueError(f"Unknown analysis module: {module}")
        index = self.module_combo.findData(module)
        if index >= 0:
            self.module_combo.setCurrentIndex(index)

    def focus_requirement(self, module: str, field: str) -> bool:
        """Focus one existing typed parameter without changing its value."""
        if module not in MODULES or not isinstance(field, str) or not field:
            return False
        form = self._forms.get(module)
        if form is None or field not in form.editors:
            return False
        self.set_module(module)
        self.tabs.setCurrentWidget(self.parameter_tabs)
        index = MODULES.index(module)
        if index < self.parameter_tabs.count():
            self.parameter_tabs.setCurrentIndex(index)
        form.focus_field(field)
        return True

    def focus_metric(self, metric_id: str, module: str | None = None) -> bool:
        """Show an exact catalogue metric when its owning module is unambiguous."""
        if not isinstance(metric_id, str) or not metric_id:
            return False
        owner = module if module in MODULES else self.module_for_metric(metric_id)
        candidates = [owner] if owner in MODULES else []
        if len(candidates) != 1 or candidates[0] not in MODULES:
            return False
        self.set_module(candidates[0])
        self.tabs.setCurrentWidget(self.catalogue)
        return self.catalogue.focus_metric(metric_id)

    def module_for_metric(self, metric_id: str) -> str | None:
        """Return a module only when the live schema gives the metric one owner."""
        if not isinstance(metric_id, str) or not metric_id:
            return None
        candidates = [
            name for name in MODULES
            if any(str(entry.get("metric_id")) == metric_id
                   for entry in catalogue_for_module(self._schema, name))
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _on_experimental_changed(self, module: str, enabled: bool) -> None:
        self._allow_experimental[module] = bool(enabled)
        form = self._forms.get(module)
        if form is not None and "enable_experimental" in form.editors:
            form.set_values({"enable_experimental": bool(enabled)}, strict=True)
        self._emit_parameters_changed()
        self.preflight_requested.emit()

    def _sync_current_module(self) -> None:
        if not hasattr(self, "module_combo"):
            return
        module = self.module_combo.currentData()
        if module not in MODULES:
            return
        self.catalogue.set_module(module)
        self.catalogue.set_selection_ids(module, tuple(self._selected[module]))
        self.catalogue.set_allow_experimental(module, self._allow_experimental[module])
        self.run_module.blockSignals(True)
        self.run_module.setChecked(self._run_enabled[module])
        self.run_module.blockSignals(False)

    def _on_module_selection_changed(self, module: str, ids: Sequence[str]) -> None:
        self._selected[module] = set(ids)
        if ids and not self._run_enabled[module]:
            self._run_enabled[module] = True
            if self.module_combo.currentData() == module:
                self.run_module.blockSignals(True)
                self.run_module.setChecked(True)
                self.run_module.blockSignals(False)
        self._emit_selection()

    def _on_run_toggled(self, enabled: bool) -> None:
        module = self.module_combo.currentData()
        if module in MODULES:
            self._run_enabled[module] = bool(enabled)
            self._emit_selection()

    def _on_selection_reconciled(self, removed: object) -> None:
        for item in removed if isinstance(removed, (list, tuple)) else ():
            if isinstance(item, str) and ":" in item:
                module, metric_id = item.split(":", 1)
                if module in MODULES:
                    self._selected[module].discard(metric_id)
        self.selection_reconciled.emit(removed)
        self._emit_selection()

    def _emit_selection(self) -> None:
        self.selection_changed.emit(self.selection_spec())

