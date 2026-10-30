"""Context-sensitive help for the main desktop workflow."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, WeakSet, ref

from shiboken6 import isValid as _shiboken_is_valid
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHeaderView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTreeWidget,
    QWidget,
)


_LABEL_HELP = {
    "源时间单位": "选择原始文件实际使用的时间单位。未知时保留未知并先核对来源；这里不会根据数值范围自动猜单位。",
    "来源时间已确认": "记录你是否核对过来源时间的单位、原点和精度；这是来源声明，不是软件对时钟的验证。",
    "时间精度（秒）": "填写来源时间本身可分辨的精度，单位为秒。它不是采样率或采样间隔；不清楚时按设备记录核实，不要猜填。",
    "时间分辨率（秒）": "记录时钟时间值可分辨的最小时间刻度，单位为秒。它不等于采样率；未知时应保留未知并补来源依据。",
    "时区": "仅在源时间是日期时间时填写来源实际使用的时区。软件不会按电脑当前时区推断。",
    "时间原点": "记录原始时间相对于哪个起点计量。请按设备或实验记录填写，不要从文件行顺序推断。",
    "frame_rate_hz": "只有原始时间以 frame 编号表示时才填写实际帧率，单位 Hz；没有来源依据时保持未提供。",
    "跨时钟同步": "只有根据记录确认多个时钟的同步关系后才勾选。相同文件顺序或大致起录时间本身不能证明同步。",
    "设备状态（用户声明）": "这是用户提供的设备状态声明。软件会保留声明和依据，但不会自行认证设备准确度。",
    "时间顺序": "选择后端如何处理时间顺序。稳定排序会改变行的呈现顺序但保留原始索引；请依据数据来源与分析要求选择。",
    "重复时间": "选择遇到重复时间值时的处理规则。拒绝会阻止该输入继续；保留并标记会保留重复记录供后续状态追溯。",
    "连续缺失上限（采样点；整数 ≥ 1）": "设置允许按连续采样点计数的缺失长度上限。它与时间间隔参数含义不同；只按当前预处理规则填写。",
    "低通阶数（整数 ≥ 1）": "设置低通滤波器的阶数。该值会影响滤波响应；按所选方法和研究方案填写，不要为消除提示任意更改。",
    "基线最低覆盖率（0, 1]": "设置基线时间窗所需的最低有效覆盖比例。较高阈值会要求更多有效数据；最终可用性以实际前检为准。",
    "基线最短时长（秒；0 不额外限制）": "设置基线窗允许的最短时长，单位秒。该项只约束基线窗；0 表示不额外限制时长，不会自动创建基线数据。",
}


@dataclass(frozen=True)
class _HelpContext:
    label: str
    description: str


class ContextualHelpController(QObject):
    """Update one help label from a cached widget-to-field map.

    The widget tree is indexed at page construction and when Qt reports a new
    child. Focus and mouse events only walk the event target's parent chain;
    they do not rescan the page.
    """

    def __init__(self, target: QLabel | QPlainTextEdit, page_stack: Any) -> None:
        super().__init__(target)
        self._page_stack = page_stack
        self._contexts: WeakKeyDictionary[QWidget, _HelpContext] = WeakKeyDictionary()
        self._attached: WeakSet[QWidget] = WeakSet()
        self._pending_child_parents: WeakSet[QWidget] = WeakSet()
        self._connected_views: dict[int, tuple[ReferenceType[QAbstractItemView], QObject, Any]] = {}
        self._combo_view_owners: WeakKeyDictionary[QAbstractItemView, ReferenceType[QComboBox]] = WeakKeyDictionary()
        self._indexed_layouts: WeakSet[Any] = WeakSet()
        if isinstance(target, QPlainTextEdit):
            self._read_target_text = target.toPlainText
            self._write_target_text = target.setPlainText
        else:
            self._read_target_text = target.text
            self._write_target_text = target.setText
            target.setTextFormat(Qt.TextFormat.PlainText)
            target.setWordWrap(True)
        self._default_text = self._read_target_text()
        for index in range(page_stack.count()):
            self._register_subtree(page_stack.widget(index))
        page_stack.currentChanged.connect(self._on_page_changed)

    def set_default_text(self, text: str) -> None:
        """Set the current page's fallback and clear help from the old page."""
        self._default_text = str(text)
        self._write_target_text(self._default_text)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if self._is_live_widget(watched):
            if self._is_header_interaction_target(watched):
                return False
            event_type = event.type()
            if event_type in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
                if isinstance(watched, QAbstractButton) and watched.property("preserveContextHelp"):
                    return False
                context = self._context_for_event_target(watched, event)
                self._write_target_text(self._format(context) if context is not None else self._default_text)
            elif event_type == QEvent.Type.ChildAdded:
                # ChildAdded fires before QWidget construction is complete, so
                # defer and resolve newly constructed direct children from the
                # parent rather than retaining the provisional event wrapper.
                self._schedule_child_registration(watched)
        return False

    @staticmethod
    def _is_live_qobject(obj: Any) -> bool:
        try:
            return isinstance(obj, QObject) and _shiboken_is_valid(obj)
        except (RuntimeError, ReferenceError):
            return False

    @classmethod
    def _is_live_widget(cls, widget: Any) -> bool:
        if not cls._is_live_qobject(widget) or not isinstance(widget, QWidget):
            return False
        try:
            return callable(getattr(widget, "installEventFilter", None))
        except (RuntimeError, ReferenceError):
            return False

    def _on_page_changed(self, _index: int) -> None:
        if not self._is_live_qobject(self) or not self._is_live_widget(self._page_stack):
            return
        self._write_target_text(self._default_text)
        page = self._page_stack.currentWidget()
        if page is not None:
            self._register_subtree(page)

    def _register_if_alive(self, widget: QWidget) -> None:
        if not self._is_live_widget(widget):
            return
        self._register_subtree(widget)
        self._bind_dynamic_form_row(widget)

    def _schedule_child_registration(self, parent: QWidget) -> None:
        if not self._is_live_widget(parent):
            return
        try:
            if parent in self._pending_child_parents:
                return
            self._pending_child_parents.add(parent)
            parent_ref = ref(parent)
            owner_ref = ref(self)
        except (RuntimeError, ReferenceError, TypeError):
            return

        def register_children() -> None:
            owner = owner_ref()
            widget_parent = parent_ref()
            if owner is None or not owner._is_live_qobject(owner):
                return
            if owner._is_live_widget(widget_parent):
                try:
                    owner._pending_child_parents.discard(widget_parent)
                    children = widget_parent.children()
                except (RuntimeError, ReferenceError):
                    return
                for child in children:
                    if owner._is_live_widget(child) and not owner._is_attached(child):
                        owner._register_if_alive(child)

        QTimer.singleShot(0, register_children)

    def _is_attached(self, widget: QWidget) -> bool:
        try:
            return widget in self._attached
        except (RuntimeError, ReferenceError, TypeError):
            return False

    def _bind_dynamic_form_row(self, widget: QWidget) -> None:
        candidate: QWidget | None = widget
        while candidate is not None:
            parent = candidate.parentWidget()
            if parent is None:
                return
            layout = parent.layout()
            if isinstance(layout, QFormLayout):
                row, _role = layout.getWidgetPosition(candidate)
                if row >= 0:
                    label_item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                    field_item = layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                    label_widget = label_item.widget() if label_item is not None else None
                    field_widget = field_item.widget() if field_item is not None else None
                    label = self._widget_label(label_widget)
                    if label and field_widget is not None:
                        self._bind_label(field_widget, label)
                        if label_widget is not None:
                            self._bind_label(label_widget, label)
                    return
            candidate = parent

    def _register_subtree(self, root: QWidget | None) -> None:
        if not self._is_live_widget(root):
            return
        try:
            widgets = [root, *root.findChildren(QWidget)]
        except RuntimeError:
            return
        for widget in widgets:
            if not self._is_live_widget(widget):
                continue
            self._attach(widget)
            self._index_widget_hint(widget)
            layout = widget.layout()
            if layout is not None:
                self._index_layout(layout)

    def _attach(self, widget: QWidget) -> None:
        if not self._is_live_widget(widget) or self._is_header_interaction_target(widget):
            return
        try:
            if widget not in self._attached:
                self._attached.add(widget)
                widget.installEventFilter(self)
            if isinstance(widget, QAbstractItemView) and widget not in self._combo_view_owners:
                self._connect_view_selection(widget)
            if isinstance(widget, QComboBox):
                # Combo popup views are transient. Map their events to the owning
                # field, but do not retain selection callbacks on the popup view.
                view = widget.view()
                if self._is_live_widget(view):
                    self._combo_view_owners[view] = ref(widget)
                    self._attach(view)
                    viewport = view.viewport()
                    if self._is_live_widget(viewport):
                        self._attach(viewport)
        except (RuntimeError, ReferenceError, TypeError):
            return

    def _connect_view_selection(self, view: QAbstractItemView) -> None:
        if not self._is_live_widget(view) or isinstance(view, QHeaderView):
            return
        try:
            selection_model = view.selectionModel()
            if selection_model is None:
                return
            key = id(view)
            existing = self._connected_views.get(key)
            if existing is not None and existing[0]() is view and existing[1] is selection_model:
                return
            if existing is not None:
                try:
                    existing[1].currentChanged.disconnect(existing[2])
                except (RuntimeError, TypeError):
                    pass

            view_ref = ref(view)
            controller_ref = ref(self)
            model_ref = ref(selection_model)

            def current_changed(current: Any, _previous: Any) -> None:
                owner = controller_ref()
                item_view = view_ref()
                item_model = model_ref()
                if (
                    owner is None
                    or not owner._is_live_qobject(owner)
                    or item_model is None
                    or not owner._is_live_widget(item_view)
                ):
                    return
                try:
                    if _shiboken_is_valid(item_model) and item_view.selectionModel() is item_model:
                        owner._on_view_current_changed(item_view, current)
                except (RuntimeError, ReferenceError):
                    return

            self._connected_views[key] = (view_ref, selection_model, current_changed)

            def forget_view(*_args: Any) -> None:
                owner = controller_ref()
                if owner is not None and owner._is_live_qobject(owner):
                    owner._connected_views.pop(key, None)

            selection_model.currentChanged.connect(current_changed)
            view.destroyed.connect(forget_view)
        except (RuntimeError, ReferenceError, TypeError):
            return

    def _on_view_current_changed(self, view: QAbstractItemView, index: Any) -> None:
        if not self._is_live_qobject(self) or not self._is_live_widget(view) or not self._is_live_widget(self._page_stack):
            return
        try:
            active_page = self._page_stack.currentWidget()
            if not self._is_live_widget(active_page) or not active_page.isAncestorOf(view) or not index.isValid():
                return
            combo_context = self._combo_context_for_view(view)
            if combo_context is not None:
                self._write_target_text(self._format(combo_context))
                return
            context = self._context_for_view_index(view, index)
            if context is not None:
                self._write_target_text(self._format(context))
        except (RuntimeError, ReferenceError):
            return

    def _combo_context_for_view(self, view: QAbstractItemView) -> _HelpContext | None:
        try:
            owner_ref = self._combo_view_owners.get(view)
            owner = owner_ref() if owner_ref is not None else None
            if self._is_live_widget(owner):
                return self._contexts.get(owner)
        except (RuntimeError, ReferenceError):
            return None
        return None

    def _index_widget_hint(self, widget: QWidget) -> None:
        if isinstance(widget, QGroupBox) and widget.title().strip():
            self._contexts.setdefault(
                widget,
                _HelpContext(widget.title().strip(), "此区域包含相关设置；选择具体字段可查看字段说明。"),
            )
        if isinstance(widget, QLabel):
            buddy = widget.buddy()
            if buddy is not None:
                self._bind_label(buddy, widget.text())
                self._bind_label(widget, widget.text())

    def _index_layout(self, layout: Any) -> None:
        if layout in self._indexed_layouts:
            return
        self._indexed_layouts.add(layout)
        if isinstance(layout, QFormLayout):
            for row in range(layout.rowCount()):
                label_item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                field_item = layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                label_widget = label_item.widget() if label_item is not None else None
                field_widget = field_item.widget() if field_item is not None else None
                label = self._widget_label(label_widget)
                if label and field_widget is not None:
                    self._bind_label(field_widget, label)
                    if label_widget is not None:
                        self._bind_label(label_widget, label)
                        if isinstance(label_widget, QLabel) and label_widget.buddy() is not None:
                            self._bind_label(label_widget.buddy(), label)
                if field_item is not None and field_item.layout() is not None:
                    self._bind_inline_labels(field_item.layout())

        for index in range(layout.count()):
            item = layout.itemAt(index)
            child_layout = item.layout()
            if child_layout is not None:
                self._index_layout(child_layout)

    def _bind_inline_labels(self, layout: Any) -> None:
        """Associate simple label-then-editor rows inside compact composites."""
        if not isinstance(layout, QHBoxLayout):
            return
        pending_label = ""
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            if isinstance(widget, QLabel):
                pending_label = widget.text().strip()
                continue
            if widget is not None and pending_label:
                self._bind_label(widget, pending_label)
                pending_label = ""
            child_layout = item.layout()
            if isinstance(child_layout, QHBoxLayout):
                self._bind_inline_labels(child_layout)

    def _bind_label(self, widget: QWidget | None, label: str) -> None:
        if widget is None or not label:
            return
        self._contexts[widget] = self._make_context(widget, label)

    @staticmethod
    def _widget_label(widget: QWidget | None) -> str:
        if isinstance(widget, QLabel):
            return widget.text().replace("&", "").strip()
        if isinstance(widget, QAbstractButton):
            return widget.text().strip()
        if isinstance(widget, QGroupBox):
            return widget.title().strip()
        return ""

    def _make_context(self, widget: QWidget, label: str) -> _HelpContext:
        description = self._explicit_description(widget)
        if not description:
            description = _LABEL_HELP.get(label, "")
        if not description:
            description = (
                "该说明对应当前字段。请依据数据来源、方法要求或已声明的研究设计填写；"
                "软件不会替你推断未提供的单位、时钟关系或参数值。"
            )
        return _HelpContext(label, description)

    @staticmethod
    def _explicit_description(widget: QWidget) -> str:
        for name in ("contextHelp", "parameterDescription", "accessibleDescription"):
            value = widget.property(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        accessible_description = widget.accessibleDescription().strip()
        if accessible_description:
            return accessible_description
        for value in (widget.whatsThis(), widget.statusTip(), widget.toolTip()):
            if value and value.strip():
                return value.strip()
        return ""

    def _context_for_event_target(self, target: QWidget, event: QEvent) -> _HelpContext | None:
        view = self._find_item_view(target)
        if view is not None:
            combo_context = self._combo_context_for_view(view)
            if combo_context is not None:
                return combo_context
            self._connect_view_selection(view)
            context = self._context_for_view(view, event)
            if context is not None:
                return context
        explicit_context: _HelpContext | None = None
        current: QWidget | None = target
        while current is not None:
            context = self._contexts.get(current)
            if context is not None:
                return context
            label = self._widget_label(current)
            if label:
                return self._make_context(current, label)
            description = self._explicit_description(current)
            if description and explicit_context is None:
                accessible = current.accessibleName().strip()
                placeholder = current.placeholderText().strip() if isinstance(current, QLineEdit) else ""
                object_name = accessible or placeholder or self._humanize_object_name(current.objectName())
                explicit_context = _HelpContext(object_name or "当前控件", description)
            current = current.parentWidget()
        return explicit_context

    @staticmethod
    def _find_item_view(widget: QWidget) -> QAbstractItemView | None:
        current: QWidget | None = widget
        while current is not None:
            if isinstance(current, QHeaderView):
                return None
            if isinstance(current, QAbstractItemView):
                return current
            current = current.parentWidget()
        return None

    @staticmethod
    def _is_header_interaction_target(widget: QWidget) -> bool:
        current: QWidget | None = widget
        while current is not None:
            if isinstance(current, QHeaderView):
                return True
            current = current.parentWidget()
        return False

    def _context_for_view(self, view: QAbstractItemView, event: QEvent) -> _HelpContext | None:
        model = view.model()
        if model is None:
            return None
        pos = event.position().toPoint() if hasattr(event, "position") else None
        index = view.indexAt(pos) if pos is not None else view.currentIndex()
        if not index.isValid():
            index = view.currentIndex()
        if not index.isValid():
            return None
        return self._context_for_view_index(view, index, pos)

    def _context_for_view_index(
        self,
        view: QAbstractItemView,
        index: Any,
        pos: Any = None,
    ) -> _HelpContext | None:
        model = view.model()
        if model is None or not index.isValid() or index.model() is not model:
            return None
        column = index.column()
        label = str(model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or "").strip()
        row_label = str(model.headerData(index.row(), Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole) or "").strip()
        if row_label:
            label = f"{label} · {row_label}" if label else row_label
        description = ""
        # QTreeWidget catalogue tooltips describe methods. DataFrame table
        # tooltips include the underlying cell value, which must stay out of help.
        item = view.itemAt(pos) if pos is not None and hasattr(view, "itemAt") else None
        if item is None and isinstance(view, QTreeWidget):
            item = view.itemFromIndex(index)
        if item is not None and hasattr(item, "toolTip"):
            if isinstance(view, QTreeWidget):
                row_context = item.data(0, Qt.ItemDataRole.UserRole + 4)
                if isinstance(row_context, str) and row_context.strip():
                    label = item.text(1).strip() or item.text(0).strip()
                    description = row_context.strip()
                else:
                    description = str(item.toolTip(column) or item.toolTip(0) or "").strip()
                    if item.text(0).strip():
                        label = item.text(0).strip()
            else:
                description = str(item.toolTip() or "").strip()
                if item.text().strip():
                    label = item.text().strip()
        elif type(model).__name__ != "DataFrameTableModel":
            description = str(model.data(index, Qt.ItemDataRole.ToolTipRole) or "").strip()
        if not label:
            label = "当前表格字段"
        if not description:
            description = _LABEL_HELP.get(label, "查看或编辑当前列的值；软件会保留该列的来源和状态，不会据此自动推断科学含义。")
        return _HelpContext(label, description)

    @staticmethod
    def _humanize_object_name(name: str) -> str:
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
        return value.replace("_", " ").strip()

    @staticmethod
    def _format(context: _HelpContext) -> str:
        return f"{context.label}\n\n{context.description}".strip()


__all__ = ["ContextualHelpController"]
