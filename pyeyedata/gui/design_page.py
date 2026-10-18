"""Typed study-design controls bound to PYeyedata's public statistics contract."""
from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
from typing import Any, Mapping, Sequence

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pyeyedata import DesignSpec, get_analysis_schema

from .forms import TypedFieldSpec, TypedFieldWidget


_DESIGN_TYPES = (
    "unknown", "independent", "team_independent", "paired", "repeated", "lmm",
    "correlation", "within_correlation",
)
_OUTCOME_TYPES = ("unknown", "continuous", "ordinal", "binary", "count", "bounded", "censored")
_ANALYSIS_UNITS = ("unknown", "subject", "individual", "participant", "team", "interaction")
_METRIC_TYPES = _OUTCOME_TYPES
_PERFORMANCE_TYPES = ("unknown", "continuous", "ordinal", "binary", "count", "bounded", "censored")
_LMM_TEMPLATES = ("subject_intercept", "subject_condition_slope", "nested_team", "within_between")
_FIXED_TERMS = ("group", "condition", "interaction")

_OPTION_SPECS: dict[str, tuple[str, Any, str]] = {
    "strata_columns": ("list", [], "显式分层列；每个 metric × stratum 独立建假设。"),
    "metric_types": ("metric_types", {}, "按 metric ID 明确连续、等级或其他结果类型；未声明的类型不推断。"),
    "selected_levels": ("level_selection", {}, "按已声明的组间或组内条件列选择实际水平；关闭时使用全部水平，显式空选择会阻断推断。"),
    "reference_levels": ("reference_levels", {}, "分类因素参考水平；值需存在于实际数据，并按真实字符串、数值或布尔类型录入。"),
    "lmm_template": ("lmm_template", None, "仅限后端已实现的有限 LMM 模板。"),
    "covariates": ("list", [], "预先声明的连续协变量列。"),
    "covariate_units": ("mapping", {}, "协变量单位；用于解释 LMM 系数。"),
    "association_predictor": ("text", None, "within_between 模板或相关目标使用的连续预测变量。"),
    "predictor_unit": ("text", None, "预测变量单位；未知时后端将如实标为 unknown。"),
    "fit_method": ("fit_method", "REML", "LMM 最终估计方式；固定项比较按后端约定另用 ML。"),
    "compare_fixed_terms": ("fixed_terms", [], "可选的预先声明 reduced 固定项；由 A 检查严格嵌套。"),
    "association_method": ("association_method", "pearson", "独立相关目标。重复观测 Spearman 当前不支持。"),
    "performance_types": ("performance_types", {}, "逐性能列声明 continuous 或 ordinal 等真实类型。"),
    "performance_units": ("mapping", {}, "逐性能列记录其单位。"),
    "bootstrap_method": ("bootstrap_method", "percentile", "在 A 支持的推断路径中选择 percentile 或 BCa。"),
    "comparison_family": ("text", "all_selected_metrics_strata_performance", "比较族标识；成员范围由所选 metric、strata 和 performance 目标确定。"),
    "family_correction": ("correction", None, "自动默认是探索性 BY、确认性 Holm；Games–Howell 已独立同时校正。"),
    "confirmatory": ("bool", False, "确认性分析默认使用 Holm。"),
    "bh_dependence_justified": ("bool", False, "只有研究者明确说明 BH 依赖条件有依据时才实际采用 BH；否则后端改用 BY。"),
    "aggregate_pairs": ("bool", False, "团队 pair 行只能在明确聚合和成员映射后按团队作为独立单位。"),
    "team_members": ("members", {}, "团队 ID → 成员 ID；用于核查成员共享及团队聚合。"),
    "pair_members": ("members", {}, "pair ID → 成员 ID；用于核查 pair 的团队归属。"),
}


def _as_plain_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, DesignSpec):
        return asdict(value)
    if not isinstance(value, Mapping):
        raise TypeError("研究设计必须是 DesignSpec 或 mapping")
    return dict(value)


class _KeyValueEditor(QWidget):
    """Table editor for typed option mappings, with no JSON-only main path."""

    value_changed = Signal()

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if kind == "reference_levels":
            self.table = QTableWidget(0, 3, self)
            self.table.setHorizontalHeaderLabels(["因子列", "参考水平", "水平类型"])
        else:
            self.table = QTableWidget(0, 2, self)
            self.table.setHorizontalHeaderLabels(["列 / ID", "声明值"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(90)
        buttons = QHBoxLayout()
        add = QPushButton("添加一行", self)
        remove = QPushButton("删除选中行", self)
        add.clicked.connect(self._append_row)
        remove.clicked.connect(self._remove_row)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.table.itemChanged.connect(lambda *_: self.value_changed.emit())

    def _value_editor(self, value: Any = None, row: int | None = None) -> QWidget:
        if self.kind == "reference_levels":
            type_editor = QComboBox(self)
            type_options = (("文本", "str"), ("整数", "int"), ("小数", "float"), ("布尔值", "bool"))
            for label, value_type in type_options:
                type_editor.addItem(label, value_type)
            if isinstance(value, bool):
                value_type = "bool"
            elif isinstance(value, int):
                value_type = "int"
            elif isinstance(value, float):
                value_type = "float"
            else:
                value_type = "str"
            type_editor.setCurrentIndex(next(i for i in range(type_editor.count())
                                             if type_editor.itemData(i) == value_type))
            type_editor.currentIndexChanged.connect(lambda *_: self.value_changed.emit())
            if row is not None:
                self.table.setCellWidget(row, 2, type_editor)
            editor = QLineEdit(self)
            editor.setText("" if value is None else str(value))
            editor.setPlaceholderText("true / false" if value_type == "bool" else "输入与选择类型一致的值")
            editor.textChanged.connect(lambda *_: self.value_changed.emit())
            return editor
        choices: tuple[Any, ...] = ()
        if self.kind == "metric_types":
            choices = _METRIC_TYPES
        elif self.kind == "performance_types":
            choices = _PERFORMANCE_TYPES
        editor = QComboBox(self) if choices else QLineEdit(self)
        if isinstance(editor, QComboBox):
            editor.addItem("选择结果类型", None)
            for choice in choices:
                editor.addItem(str(choice), choice)
            if value in choices:
                editor.setCurrentIndex(choices.index(value) + 1)
            editor.currentIndexChanged.connect(lambda *_: self.value_changed.emit())
        elif value is not None:
            if self.kind == "members" and isinstance(value, (list, tuple, set)):
                editor.setText(", ".join(map(str, value)))
            else:
                editor.setText(str(value))
        if isinstance(editor, QLineEdit):
            editor.textChanged.connect(lambda *_: self.value_changed.emit())
        return editor

    def _append_row(self, key: str = "", value: Any = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        key_item = QTableWidgetItem(key)
        self.table.setItem(row, 0, key_item)
        self.table.setCellWidget(row, 1, self._value_editor(value, row))
        self.value_changed.emit()

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.value_changed.emit()

    def set_values(self, values: Mapping[str, Any] | None) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for key, value in (values or {}).items():
            self._append_row(str(key), value)
        self.table.blockSignals(False)

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            key = key_item.text().strip() if key_item else ""
            if not key:
                raise ValueError(f"第 {row + 1} 行的列名或 ID 不能为空")
            if key in result:
                raise ValueError(f"重复的列名或 ID：{key}")
            editor = self.table.cellWidget(row, 1)
            if isinstance(editor, QComboBox):
                value = editor.currentData()
                if value is None:
                    raise ValueError(f"{key} 尚未选择类型")
            elif isinstance(editor, QLineEdit):
                raw = editor.text().strip()
                if self.kind == "reference_levels":
                    type_editor = self.table.cellWidget(row, 2)
                    value_type = type_editor.currentData() if isinstance(type_editor, QComboBox) else "str"
                    if not raw:
                        raise ValueError(f"{key} 的参考水平不能为空")
                    try:
                        if value_type == "int":
                            value = int(raw)
                        elif value_type == "float":
                            value = float(raw)
                        elif value_type == "bool":
                            if raw.casefold() not in {"true", "false"}:
                                raise ValueError("布尔值只能填写 true 或 false")
                            value = raw.casefold() == "true"
                        else:
                            value = raw
                    except ValueError as exc:
                        raise ValueError(f"{key} 的参考水平与所选类型不匹配") from exc
                elif self.kind == "members":
                    value = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
                    if not value:
                        raise ValueError(f"{key} 至少要有一个成员 ID")
                elif not raw:
                    raise ValueError(f"{key} 的声明值不能为空")
                else:
                    value = raw
            else:
                raise TypeError("不支持的映射值控件")
            result[key] = value
        return result


class _ChoiceListEditor(QWidget):
    """Multi-select list for the backend's fixed, enumerated comparison terms."""

    value_changed = Signal()

    def __init__(self, choices: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.checkboxes: dict[str, QCheckBox] = {}
        for choice in choices:
            box = QCheckBox(choice, self)
            box.stateChanged.connect(lambda *_: self.value_changed.emit())
            self.checkboxes[choice] = box
            layout.addWidget(box)
        layout.addStretch(1)

    def set_values(self, values: Sequence[str]) -> None:
        selected = set(values)
        for name, box in self.checkboxes.items():
            box.setChecked(name in selected)

    def values(self) -> list[str]:
        return [name for name, box in self.checkboxes.items() if box.isChecked()]


def _typed_level(value: Any) -> Any:
    if hasattr(value, "item") and callable(value.item):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if type(value) in {str, int, float, bool}:
        return value
    return value


def _level_equal(left: Any, right: Any) -> bool:
    left, right = _typed_level(left), _typed_level(right)
    return type(left) is type(right) and left == right


def _level_label(value: Any) -> str:
    value = _typed_level(value)
    kind = {str: "文本", int: "整数", float: "小数", bool: "布尔值"}.get(type(value), type(value).__name__)
    return f"{value!r}（{kind}）"


class _LevelSelectionEditor(QWidget):
    """Typed per-factor level checkboxes with round-trip retention for stale values."""

    value_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._updating = False
        self._declarations: list[tuple[str, str]] = []
        self._available: dict[str, list[Any]] = {}
        self._invalid_raw: Any = None
        self._invalid_active = False
        self._sections: dict[str, tuple[QCheckBox, list[tuple[Any, QCheckBox]]]] = {}
        self._orphans: dict[str, tuple[QCheckBox, list[tuple[Any, QCheckBox]]]] = {}
        self._notice = QLabel(self)
        self._notice.setWordWrap(True)
        self._layout.addWidget(self._notice)
        self._section_root = QWidget(self)
        self._section_layout = QVBoxLayout(self._section_root)
        self._section_layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self._section_root)

    def set_values(self, values: Any) -> None:
        self._invalid_active = not isinstance(values, Mapping) or any(not isinstance(item, list) for item in values.values())
        self._invalid_raw = deepcopy(values) if self._invalid_active else None
        self._rebuild(self._declarations, self._available, values if isinstance(values, Mapping) else {})
        if self._invalid_active:
            self._notice.setText("保存的 selected_levels 格式无效；原值已保留，修正后才能运行。")

    def set_available(self, declarations: Sequence[tuple[str, str]], available: Mapping[str, Sequence[Any]]) -> None:
        try:
            selected = self.values()
        except (TypeError, ValueError):
            selected = {}
        self._declarations = list(declarations)
        self._available = {key: list(values) for key, values in available.items()}
        if not self._invalid_active:
            self._rebuild(self._declarations, self._available, selected if isinstance(selected, Mapping) else {})

    def _clear_sections(self) -> None:
        self._sections = {}
        self._orphans = {}
        while self._section_layout.count():
            item = self._section_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild(self, declarations: Sequence[tuple[str, str]], available: Mapping[str, Sequence[Any]], selected: Mapping[str, Any]) -> None:
        self._updating = True
        try:
            self._clear_sections()
            self._notice.setText(
                "选择范围会用于本次描述与推断；所选水平缺失或完整观测不足时，该目标会保留为阻断状态。参考水平与对比方向仍由独立设置控制。"
            )
            active: dict[str, str] = {}
            for field_name, column in declarations:
                if column:
                    title = "组间（between-group）" if field_name == "group_column" else "组内 condition"
                    active[column] = title if column not in active else active[column] + " / " + title
            for column, title in active.items():
                self._add_section(column, title, available.get(column, ()), selected.get(column), self._sections)
            orphan_keys = [key for key in selected if key not in active]
            if orphan_keys:
                label = QLabel("待修正的已保存选择（当前设计未声明这些列；原值已保留）", self._section_root)
                label.setObjectName("designCaution")
                label.setWordWrap(True)
                self._section_layout.addWidget(label)
                for column in orphan_keys:
                    self._add_section(str(column), "待修正列", (), selected[column], self._orphans, raw_key=column)
            if not active and not orphan_keys:
                empty = QLabel("先声明组间列或组内 condition 列，再选择实际数据中的水平。", self._section_root)
                empty.setWordWrap(True)
                self._section_layout.addWidget(empty)
        finally:
            self._updating = False

    def _add_section(self, column: str, title: str, available: Sequence[Any], saved: Any,
                     store: dict[str, tuple[QCheckBox, list[tuple[Any, QCheckBox]]]], raw_key: Any = None) -> None:
        group = QGroupBox(f"{title} · {column}", self._section_root)
        layout = QVBoxLayout(group)
        toggle = QCheckBox("只比较勾选水平", group)
        layout.addWidget(toggle)
        is_saved_list = isinstance(saved, list)
        toggle.setChecked(is_saved_list)
        saved_values = list(saved) if is_saved_list else []
        levels: list[Any] = []
        for value in available:
            value = _typed_level(value)
            if not any(_level_equal(value, old) for old in levels):
                levels.append(value)
        stale_values = [value for value in saved_values if not any(_level_equal(value, actual) for actual in levels)]
        boxes: list[tuple[Any, QCheckBox]] = []
        for value, stale in [(item, False) for item in levels] + [(item, True) for item in stale_values]:
            supported = type(_typed_level(value)) in {str, int, float, bool}
            suffix = " · 保存值不在当前数据中，待修正" if stale else ""
            if not supported:
                suffix += " · 此类型不能写入 JSON 选择"
            box = QCheckBox(_level_label(value) + suffix, group)
            box.setProperty("_typed_level_value", value)
            box.setChecked(any(_level_equal(value, chosen) for chosen in saved_values))
            box.setEnabled(supported)
            box.setToolTip("保留水平原始类型；例如 False、0 和 '0' 是不同水平。" + (" 请核对过期或不匹配的已保存值。" if stale else ""))
            box.stateChanged.connect(self._changed)
            boxes.append((value, box))
            layout.addWidget(box)
        if not levels:
            empty = QLabel("当前 metric / 可匹配绩效数据中没有有效水平。", group)
            empty.setWordWrap(True)
            layout.addWidget(empty)
        for _, box in boxes:
            box.setEnabled(box.isEnabled() and toggle.isChecked())
        toggle.toggled.connect(lambda enabled, entries=boxes: self._toggle_levels(enabled, entries))
        toggle.stateChanged.connect(self._changed)
        store[raw_key if raw_key is not None else column] = (toggle, boxes)
        self._section_layout.addWidget(group)

    def _toggle_levels(self, enabled: bool, entries: Sequence[tuple[Any, QCheckBox]]) -> None:
        for _, box in entries:
            supported = type(_typed_level(box.property("_typed_level_value"))) in {str, int, float, bool}
            box.setEnabled(enabled and supported)

    def _changed(self, *_: Any) -> None:
        if self._updating:
            return
        self._invalid_active = False
        self._invalid_raw = None
        self.value_changed.emit()

    def values(self) -> Any:
        if self._invalid_active:
            return deepcopy(self._invalid_raw)
        result: dict[str, list[Any]] = {}
        for column, (toggle, boxes) in self._sections.items():
            if toggle.isChecked():
                result[column] = [value for value, box in boxes if box.isChecked()]
        for column, (toggle, boxes) in self._orphans.items():
            if toggle.isChecked():
                result[column] = [value for value, box in boxes if box.isChecked()]
        return result

    def selected_stale_levels(self) -> dict[str, list[Any]]:
        stale: dict[str, list[Any]] = {}
        for column, (toggle, boxes) in list(self._sections.items()) + list(self._orphans.items()):
            if not toggle.isChecked():
                continue
            missing = [value for value, box in boxes if box.isChecked() and "待修正" in box.text()]
            if missing:
                stale[str(column)] = missing
        return stale


class DesignPage(QWidget):
    """DesignSpec editor whose values map directly to the public statistics API.

    The page declares inputs only. Method selection, inferential status, independent N,
    families, exclusions, and diagnostics are populated from the real A result tables.
    """

    configuration_changed = Signal(object)
    validation_changed = Signal(object)

    def __init__(
        self,
        schema: Mapping[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        live_schema = dict(schema or get_analysis_schema())
        records = live_schema.get("typed_parameters", {}).get("statistics_design", ())
        if not records:
            raise ValueError("Public schema is missing typed_parameters.statistics_design")
        self._schema_records = list(records)
        self._design_editors: dict[str, TypedFieldWidget] = {}
        self._option_editors: dict[str, QWidget] = {}
        self._option_defaults: dict[str, Any] = {}
        self._unknown_options: dict[str, Any] = {}
        self._unknown_design_fields: dict[str, Any] = {}
        self._metric_frame = pd.DataFrame()
        self._performance_frame = pd.DataFrame()
        self._loading_design = False

        root = QVBoxLayout(self)
        heading = QLabel("研究设计与统计", self)
        heading.setObjectName("pageTitle")
        root.addWidget(heading)
        intro = QLabel(
            "请明确独立单位、结果类型和设计层级。实际统计方法、推断阻断原因、N、效应量、区间与校正族由 A 的真实运行结果返回；"
            "未知或不支持的设计仍可保留描述统计。这里不会按数据数值猜设计，也不会在界面补做统计。",
            self,
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.tabs = QTabWidget(self)
        self.design_tab = QWidget(self.tabs)
        self.options_tab = QWidget(self.tabs)
        self.results_tab = QWidget(self.tabs)
        self.tabs.addTab(self.design_tab, "设计与分析单位")
        self.tabs.addTab(self.options_tab, "分层、模型与比较族")
        self.tabs.addTab(self.results_tab, "实际统计结果")
        root.addWidget(self.tabs, 1)

        self._build_design_tab()
        self._build_options_tab()
        self._build_results_tab()
        self._connect_change_signals()
        self.set_design_spec(DesignSpec())

    def _build_design_tab(self) -> None:
        outer = QVBoxLayout(self.design_tab)
        scroll = QScrollArea(self.design_tab)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        design_group = QGroupBox("研究设计与结果类型", content)
        self.design_form = QFormLayout(design_group)
        self.design_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        choices = {
            "design_type": _DESIGN_TYPES,
            "outcome_type": _OUTCOME_TYPES,
            "analysis_unit": _ANALYSIS_UNITS,
        }
        for record in self._schema_records:
            name = str(record.get("name", ""))
            if not name or name == "options":
                continue
            spec = TypedFieldSpec.from_schema(record)
            if name in choices:
                spec = replace(spec, choices=choices[name])
            widget = TypedFieldWidget(spec, design_group)
            widget.setObjectName("design_" + name)
            self._design_editors[name] = widget
            self.design_form.addRow(QLabel(self._field_label(name), design_group), widget)
        layout.addWidget(design_group)

        self.independence_note = QLabel(
            "独立性确认仅记录研究者声明，不证明随机化或设计有效。共享 pair、同人跨 team、重复条件和多对多绩效键仍由后端检查；不满足时保留描述结果并阻断相应推断。",
            content,
        )
        self.independence_note.setWordWrap(True)
        self.independence_note.setObjectName("designCaution")
        layout.addWidget(self.independence_note)

        self.levels_label = QLabel("声明组间或组内 condition 列后，可按实际观测水平设置本次比较范围。", content)
        self.levels_label.setWordWrap(True)
        layout.addWidget(self.levels_label)
        self.performance_join_label = QLabel("绩效数据表未提供；若选择绩效关联，后端会在运行时检查一对一连接。", content)
        self.performance_join_label.setWordWrap(True)
        layout.addWidget(self.performance_join_label)
        self.validation_label = QLabel("描述统计可以在推断设计未知时运行。", content)
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    @staticmethod
    def _field_label(name: str) -> str:
        labels = {
            "design_type": "研究设计（Design type）",
            "outcome_type": "结果类型（Outcome type）",
            "analysis_unit": "独立分析单位（Analysis unit）",
            "subject_column": "被试 / 成员 ID 列",
            "session_column": "Session ID 列",
            "interaction_column": "Interaction ID 列",
            "team_column": "团队 ID 列",
            "analysis_unit_column": "实际独立单位列（可选）",
            "group_column": "组间条件列",
            "condition_column": "组内条件列",
            "trial_column": "Trial 列",
            "performance_columns": "绩效结果列（可多项）",
            "performance_join_keys": "绩效一对一连接键（必填）",
            "independence_confirmed": "研究者确认独立单位与依赖结构",
            "independence_note": "独立性依据说明",
        }
        return labels.get(name, name)

    def _build_options_tab(self) -> None:
        outer = QVBoxLayout(self.options_tab)
        scroll = QScrollArea(self.options_tab)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        self.options_form = QFormLayout()
        self.options_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self._options_content_layout = layout

        self._option_types: dict[str, QWidget] = {}
        for name, (kind, default, description) in _OPTION_SPECS.items():
            if kind == "level_selection":
                editor = _LevelSelectionEditor(content)
                editor.value_changed.connect(self._emit_configuration)
            elif kind in {"list", "fixed_terms"}:
                if kind == "fixed_terms":
                    editor = _ChoiceListEditor(_FIXED_TERMS, content)
                    editor.value_changed.connect(self._emit_configuration)
                else:
                    editor = QLineEdit(content)
                    editor.setPlaceholderText("用逗号分隔列名" if name.endswith("columns") else "用逗号分隔")
                    editor.textChanged.connect(lambda *_: self._emit_configuration())
            elif kind in {"metric_types", "performance_types", "mapping", "reference_levels", "members"}:
                editor = _KeyValueEditor(kind, content)
                editor.value_changed.connect(self._emit_configuration)
            elif kind == "bool":
                editor = QCheckBox(content)
                editor.stateChanged.connect(lambda *_: self._emit_configuration())
            elif kind in {"fit_method", "association_method", "bootstrap_method", "lmm_template", "correction"}:
                editor = QComboBox(content)
                if kind == "fit_method":
                    values = (("REML", "REML"), ("ML", "ML"))
                elif kind == "association_method":
                    values = (("Pearson（线性）", "pearson"), ("Spearman（等级/单调）", "spearman"))
                elif kind == "bootstrap_method":
                    values = (("Percentile", "percentile"), ("BCa", "bca"))
                elif kind == "lmm_template":
                    values = tuple((item, item) for item in _LMM_TEMPLATES)
                else:
                    values = (("自动：探索性 BY / 确认性 Holm", None), ("BY", "BY"), ("BH（须依据）", "BH"), ("Holm", "Holm"))
                if kind == "lmm_template":
                    editor.addItem("未选择（使用后端模板默认）", None)
                for label, value in values:
                    editor.addItem(label, value)
                editor.currentIndexChanged.connect(lambda *_: self._emit_configuration())
            elif kind == "text":
                editor = QLineEdit(content)
                if default is not None:
                    editor.setText(str(default))
                editor.textChanged.connect(lambda *_: self._emit_configuration())
            else:
                raise ValueError(f"Unknown statistics option type: {kind}")
            editor.setObjectName("statistics_option_" + name)
            editor.setToolTip(description)
            self._option_types[name] = editor
            self._option_defaults[name] = default
            self._options_content_layout.addWidget(QLabel(self._option_label(name), content))
            self._options_content_layout.addWidget(editor)
            note = QLabel(description, content)
            note.setWordWrap(True)
            note.setObjectName("fieldHelp")
            self._options_content_layout.addWidget(note)
        self._option_editors = self._option_types

        locked = QGroupBox("本方法版本锁定值", content)
        locked_layout = QFormLayout(locked)
        locked_layout.addRow("Bootstrap seed", QLabel("20261011 · 固定值"))
        locked_layout.addRow("Bootstrap 重采样", QLabel("5000 · 固定值"))
        locked_layout.addRow("显著性水平 α", QLabel("0.05 · 固定值"))
        locked_layout.addRow("置信区间", QLabel("95% · 固定值；LMM 使用渐近 Wald 区间"))
        locked_layout.addRow("Games–Howell", QLabel("各 metric / stratum 内独立同时校正，不再叠加 BY/Holm"))
        self._options_content_layout.addWidget(locked)
        self.options_default_note = QLabel(
            "比较族成员由所选 metrics × strata × performance 目标在运行前确定；后端拒绝的设计不会由界面换成另一个推断方法。",
            content,
        )
        self.options_default_note.setWordWrap(True)
        self._options_content_layout.addWidget(self.options_default_note)
        self._options_content_layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    @staticmethod
    def _option_label(name: str) -> str:
        labels = {
            "strata_columns": "分层列",
            "metric_types": "各指标的结果类型",
            "selected_levels": "组别与条件水平选择",
            "reference_levels": "分类因素的参考水平",
            "lmm_template": "LMM 模板",
            "covariates": "协变量列",
            "covariate_units": "协变量单位",
            "association_predictor": "相关 / within-between 预测列",
            "predictor_unit": "预测变量单位",
            "fit_method": "LMM 最终拟合方法",
            "compare_fixed_terms": "预先声明 reduced 固定项",
            "association_method": "相关估计目标",
            "performance_types": "绩效列类型",
            "performance_units": "绩效列单位",
            "bootstrap_method": "Bootstrap 区间方法",
            "comparison_family": "比较族 ID",
            "family_correction": "多重比较校正",
            "confirmatory": "确认性分析",
            "bh_dependence_justified": "BH 依赖条件有明确依据",
            "aggregate_pairs": "按团队聚合 pair 结果",
            "team_members": "团队成员表",
            "pair_members": "Pair 成员表",
        }
        return labels.get(name, name)

    def _build_results_tab(self) -> None:
        layout = QVBoxLayout(self.results_tab)
        self.result_status = QLabel("尚无统计运行结果。保存的设计声明本身不代表已执行推断。", self.results_tab)
        self.result_status.setWordWrap(True)
        layout.addWidget(self.result_status)
        self.result_table = QTableWidget(0, 0, self.results_tab)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.result_table, 1)
        self.diagnostics_label = QLabel("方法、独立 N、观测数、CI、原始/校正 p、族和诊断将显示后端实际返回值。", self.results_tab)
        self.diagnostics_label.setWordWrap(True)
        layout.addWidget(self.diagnostics_label)

    def _connect_change_signals(self) -> None:
        for widget in self._design_editors.values():
            editor = widget._editor
            if isinstance(editor, QComboBox):
                editor.currentIndexChanged.connect(lambda *_: self._emit_configuration())
            elif isinstance(editor, QCheckBox):
                editor.stateChanged.connect(lambda *_: self._emit_configuration())
            elif isinstance(editor, QLineEdit):
                editor.textChanged.connect(lambda *_: self._emit_configuration())
            enabled = widget._enabled
            if enabled is not None:
                enabled.toggled.connect(lambda *_: self._emit_configuration())

    def _raw_option_values(self) -> dict[str, Any]:
        values: dict[str, Any] = dict(self._unknown_options)
        for name, (kind, _default, _description) in _OPTION_SPECS.items():
            editor = self._option_editors[name]
            if kind == "level_selection":
                value = editor.values() if isinstance(editor, _LevelSelectionEditor) else {}
            elif kind == "list":
                raw = editor.text().strip() if isinstance(editor, QLineEdit) else ""
                value: Any = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
            elif kind == "fixed_terms":
                value = editor.values() if isinstance(editor, _ChoiceListEditor) else []
            elif kind in {"metric_types", "performance_types", "mapping", "reference_levels", "members"}:
                value = editor.values() if isinstance(editor, _KeyValueEditor) else {}
            elif kind == "bool":
                value = bool(editor.isChecked()) if isinstance(editor, QCheckBox) else False
            elif kind == "text":
                raw = editor.text().strip() if isinstance(editor, QLineEdit) else ""
                value = raw or None
            elif isinstance(editor, QComboBox):
                value = editor.currentData()
            else:
                value = None
            if value not in (None, [], {}, ""):
                values[name] = value
            else:
                values.pop(name, None)
        return values

    def _draft_values(self) -> dict[str, Any]:
        values = {name: widget.value() for name, widget in self._design_editors.items()}
        values["options"] = self._raw_option_values()
        return values

    def design_spec(self) -> DesignSpec:
        """Return a validated instance of the actual public backend DesignSpec."""
        values = self._draft_values()
        problems = self.validate()
        if problems:
            raise ValueError("；".join(problems))
        return DesignSpec(**values)

    def configuration_values(self) -> dict[str, Any]:
        """Return the mapping expected under the host snapshot's statistics_design key."""
        return self._draft_values()

    def set_design_spec(self, value: DesignSpec | Mapping[str, Any]) -> None:
        raw = _as_plain_mapping(value)
        options = raw.pop("options", {})
        if not isinstance(options, Mapping):
            raise TypeError("DesignSpec.options 必须是 mapping")
        defaults = asdict(DesignSpec())
        prepared = {name: widget.normalize_serialized_value(raw.get(name, defaults.get(name)))
                    for name, widget in self._design_editors.items()}
        for name, widget in self._design_editors.items():
            widget.validate_value(prepared[name])
        self._loading_design = True
        try:
            self._unknown_design_fields = {key: item for key, item in raw.items() if key not in self._design_editors}
            for name, widget in self._design_editors.items():
                widget.set_value(prepared[name])
            self._unknown_options = {key: item for key, item in options.items() if key not in _OPTION_SPECS}
            for name, (kind, default, _description) in _OPTION_SPECS.items():
                editor = self._option_editors[name]
                item = options.get(name, default)
                if kind == "list" and isinstance(editor, QLineEdit):
                    editor.setText(", ".join(map(str, item or ())))
                elif kind == "level_selection" and isinstance(editor, _LevelSelectionEditor):
                    editor.set_values(item)
                elif kind == "fixed_terms" and isinstance(editor, _ChoiceListEditor):
                    editor.set_values(item or ())
                elif kind in {"metric_types", "performance_types", "mapping", "reference_levels", "members"} and isinstance(editor, _KeyValueEditor):
                    editor.set_values(item if isinstance(item, Mapping) else {})
                elif kind == "bool" and isinstance(editor, QCheckBox):
                    editor.setChecked(bool(item))
                elif kind == "text" and isinstance(editor, QLineEdit):
                    editor.setText("" if item is None else str(item))
                elif isinstance(editor, QComboBox):
                    match = next((i for i in range(editor.count()) if editor.itemData(i) == item), None)
                    if match is not None:
                        editor.setCurrentIndex(match)
        finally:
            self._loading_design = False
        self._refresh_levels()
        self._refresh_performance_info()
        self._emit_configuration()

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        try:
            values = self._draft_values()
        except (TypeError, ValueError) as exc:
            return (str(exc),)
        if self._unknown_design_fields:
            errors.append("保存设计含当前公共 DesignSpec 不支持的字段：" + ", ".join(sorted(self._unknown_design_fields)))
        if values.get("design_type") == "unknown" and values.get("independence_confirmed"):
            errors.append("设计未知时不能确认独立性。")
        if values.get("independence_confirmed") and not str(values.get("independence_note") or "").strip():
            errors.append("确认独立性时必须填写依据说明。")
        if values.get("performance_columns") and not values.get("performance_join_keys"):
            errors.append("选择绩效列时必须声明一对一连接键。")
        if len(set(values.get("performance_join_keys") or ())) != len(values.get("performance_join_keys") or ()):
            errors.append("绩效连接键不能重复。")
        options = values.get("options", {})
        if options.get("family_correction") == "BH" and not options.get("bh_dependence_justified", False):
            errors.append("已选 BH，但尚未声明其依赖条件有依据；后端会保守使用 BY。")
        if values.get("design_type") == "lmm" and options.get("lmm_template") is None:
            errors.append("LMM 模板使用后端 subject_intercept 默认；请核对重复结构与分析单位。")
        if options.get("aggregate_pairs") and not (options.get("team_members") or options.get("pair_members")):
            errors.append("团队 pair 聚合需要显式团队或 pair 成员映射；后端会阻断缺少成员信息的推断。")
        return tuple(errors)

    def set_metric_table(self, metrics: pd.DataFrame | None) -> None:
        self._metric_frame = metrics.copy(deep=True) if isinstance(metrics, pd.DataFrame) else pd.DataFrame()
        self._refresh_levels()
        self._refresh_performance_info()

    def set_performance_table(self, performance: pd.DataFrame | None) -> None:
        self._performance_frame = performance.copy(deep=True) if isinstance(performance, pd.DataFrame) else pd.DataFrame()
        self._refresh_levels()
        self._refresh_performance_info()

    def _refresh_performance_info(self) -> None:
        if self._performance_frame.empty:
            self.performance_join_label.setText(
                "绩效数据表未提供；若选择绩效关联，后端会在运行时检查完整键、一对一关系和覆盖。"
            )
            return
        try:
            values = self._draft_values()
        except (TypeError, ValueError) as exc:
            self.performance_join_label.setText("请先修正设计字段：" + str(exc))
            return
        columns = tuple(values.get("performance_columns") or ())
        keys = tuple(values.get("performance_join_keys") or ())
        missing_performance = sorted(set(columns + keys) - set(self._performance_frame.columns))
        missing_metrics = sorted(set(keys) - set(self._metric_frame.columns)) if not self._metric_frame.empty else []
        if not columns and not keys:
            self.performance_join_label.setText(
                f"已载入绩效表：{len(self._performance_frame):,} 行、{len(self._performance_frame.columns)} 列。"
                "未选择绩效列时不会执行相关分析。"
            )
        else:
            details = [f"绩效表 {len(self._performance_frame):,} 行"]
            if missing_performance:
                details.append("绩效表缺少列：" + ", ".join(missing_performance))
            if missing_metrics:
                details.append("metric 表缺少连接键：" + ", ".join(missing_metrics))
            if not missing_performance and not missing_metrics:
                details.append("所声明的列名已出现；键空值、重复、多对多和匹配覆盖由 A 逐 metric/stratum 实际校验")
            self.performance_join_label.setText("；".join(details) + "。")

    def _refresh_levels(self) -> None:
        try:
            values = self._draft_values()
        except (TypeError, ValueError):
            values = {}
        declarations: list[tuple[str, str]] = []
        for field_name in ("group_column", "condition_column"):
            editor = self._design_editors.get(field_name)
            column = editor.value() if editor else None
            if isinstance(column, str) and column:
                declarations.append((field_name, column))
        source = self._metric_frame
        mapping_note = ""
        performance_columns = tuple(values.get("performance_columns") or ())
        join_keys = tuple(values.get("performance_join_keys") or ())
        if performance_columns and join_keys and not self._performance_frame.empty:
            missing = (set(join_keys) - set(source.columns)) | (set(join_keys) - set(self._performance_frame.columns))
            if missing:
                mapping_note = "；绩效连接键尚不能映射，可用水平按 metric 表显示"
            elif (source[list(join_keys)].isna().any(axis=None) or self._performance_frame[list(join_keys)].isna().any(axis=None)
                  or source.duplicated(list(join_keys)).any() or self._performance_frame.duplicated(list(join_keys)).any()):
                mapping_note = "；当前绩效键非一对一或含空值，可用水平按 metric 表显示，后端会保留连接阻断"
            else:
                try:
                    matched = source.assign(_level_source_row=range(len(source))).merge(
                        self._performance_frame[list(join_keys)], on=list(join_keys), how="inner",
                        validate="one_to_one", sort=False,
                    )
                    source = source.iloc[matched["_level_source_row"].to_numpy(dtype=int)]
                    mapping_note = "；水平按当前一对一绩效键匹配后的 metric 行显示"
                except (KeyError, ValueError, TypeError):
                    mapping_note = "；绩效键暂不能唯一映射，可用水平按 metric 表显示"
        available: dict[str, list[Any]] = {}
        for _, column in declarations:
            if column not in source:
                available[column] = []
                continue
            levels: list[Any] = []
            for item in source[column].tolist():
                if item is None or item is pd.NA or item is pd.NaT:
                    continue
                try:
                    missing = pd.isna(item)
                    if bool(missing):
                        continue
                except (TypeError, ValueError):
                    pass
                if isinstance(item, str) and not item.strip():
                    continue
                value = _typed_level(item)
                if not any(_level_equal(value, old) for old in levels):
                    levels.append(value)
            available[column] = levels
        level_editor = self._option_editors.get("selected_levels")
        if isinstance(level_editor, _LevelSelectionEditor):
            level_editor.set_available(declarations, available)
        if self._metric_frame.empty:
            self.levels_label.setText("尚无实际 metric 表；声明列后的水平选择会在数据载入后更新。所选水平缺失或完整观测不足时会阻断相应目标。")
            return
        if not declarations:
            self.levels_label.setText("请先声明组间列或组内 condition 列，页面才会显示可选择的实际水平。")
            return
        summaries = []
        for field_name, column in declarations:
            title = "组间" if field_name == "group_column" else "组内 condition"
            levels = available.get(column, [])
            shown = "、".join(_level_label(value) for value in levels[:10]) or "无有效水平"
            summaries.append(f"{title}列 {column}: {shown}" + (" …" if len(levels) > 10 else ""))
        stale = level_editor.selected_stale_levels() if isinstance(level_editor, _LevelSelectionEditor) else {}
        stale_note = "；有已保存水平不在当前数据中，需修正" if stale else ""
        self.levels_label.setText("实际可用水平：" + "；".join(summaries) + mapping_note + stale_note +
                                  "。水平选择与参考水平/对比方向分开保存；描述与推断都按所选范围执行。")

    def set_statistical_results(
        self,
        tables: Mapping[str, pd.DataFrame],
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Render only rows and diagnostics returned by the real pipeline run."""
        statistics = tables.get("statistics")
        diagnostics = tables.get("diagnostics")
        metadata = metadata or {}
        if not isinstance(statistics, pd.DataFrame):
            self.result_table.clear()
            self.result_table.setRowCount(0)
            self.result_table.setColumnCount(0)
            self.result_status.setText("此运行未返回 statistics 表；不会合成统计方法或结果。")
            return
        columns = [
            ("metric_id", "指标"), ("outcome", "绩效目标"), ("analysis_unit", "独立单位"),
            ("n_independent", "独立 N"), ("n_observations", "观测行 N"), ("n_pair_observations", "pair 行数"),
            ("method", "实际方法"), ("status", "推断状态"), ("estimate", "估计值"),
            ("effect_name", "效应量"), ("effect_definition", "效应定义"),
            ("ci_low", "CI 下限"), ("ci_high", "CI 上限"), ("df", "df"),
            ("p", "原始 p"), ("p_adj", "校正 p"), ("correction", "校正方法"),
            ("family_id", "比较族"), ("reason_code", "原因代码"), ("reason", "后端原因"),
        ]
        present = [(key, title) for key, title in columns if key in statistics.columns]
        self.result_table.setColumnCount(len(present))
        self.result_table.setHorizontalHeaderLabels([title for _, title in present])
        self.result_table.setRowCount(len(statistics))
        for row_index, (_, row) in enumerate(statistics.iterrows()):
            for column_index, (key, _title) in enumerate(present):
                value = row[key]
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    text = "NA"
                elif key in {"p", "p_adj", "estimate", "ci_low", "ci_high"} and isinstance(value, (int, float)):
                    text = f"{value:.6g}"
                elif isinstance(value, (list, tuple, dict)):
                    text = str(value)
                else:
                    text = str(value)
                item = QTableWidgetItem(text)
                if key in {"n_independent", "n_observations", "n_pair_observations", "estimate", "ci_low", "ci_high", "df", "p", "p_adj"}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if key == "status":
                    state = str(value)
                    item.setToolTip("推断状态来自 A 的统计表，不根据是否有数值或进度推定。")
                if key == "reason" and value:
                    item.setToolTip(str(value))
                self.result_table.setItem(row_index, column_index, item)
        self.result_table.resizeColumnsToContents()
        status = str(metadata.get("execution_status", "unknown"))
        self.result_status.setText(
            f"统计结果来自真实后端表；统计阶段状态：{status}。阻断、失败、取消和 NA 按原状态保留，不把部分结果标为完成。"
        )
        if isinstance(diagnostics, pd.DataFrame) and not diagnostics.empty:
            warning_count = sum(bool(value) for value in diagnostics.get("warnings", pd.Series(dtype=object)))
            self.diagnostics_label.setText(
                f"后端 diagnostics 返回 {len(diagnostics)} 行；含 warning 的诊断行：{warning_count}。"
                "具体模型、排除和拟合信息请查看本次运行的 diagnostics/audit 原始表。"
            )
        else:
            self.diagnostics_label.setText("后端未返回 diagnostics 行；不额外构造模型诊断。")

    def _emit_configuration(self) -> None:
        if self._loading_design:
            return
        if hasattr(self, "levels_label"):
            self._refresh_levels()
            self._refresh_performance_info()
        try:
            values = self._draft_values()
            self.configuration_changed.emit({"statistics_design": values})
            errors = self.validate()
        except (TypeError, ValueError) as exc:
            errors = (str(exc),)
        self.validation_changed.emit(errors)
        if hasattr(self, "validation_label"):
            if errors:
                self.validation_label.setText("配置提示：" + "；".join(errors))
            else:
                self.validation_label.setText("本地字段检查通过；正式可用性、独立 N 和设计阻断以 A 的实际 preflight/统计结果为准。")

