"""Explicit N-person design, evidence, parameter, pair, and lag views."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any

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
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .forms import TypedParameterForm
from .method_registry import group_parameter_field_groups, group_parameter_fields


_ALL = "__all__"
_GROUP_PARAMETER_FIELDS = group_parameter_field_groups()
_FILTERS = (
    ("interaction_id", "互动单元"),
    ("trial", "试次"),
    ("segment_id", "片段"),
    ("stimulus_id", "刺激"),
)
_SEGMENT_SCOPE_COLUMNS = (
    "subject_id", "session_id", "interaction_id", "timebase_id", "source_id",
    "trial", "condition", "stimulus_id",
)
_UNCONFIRMED_TIMEBASES = {"unconfirmed", "legacy_unconfirmed", "<NA>"}
_FIELD_LABELS = {
    "alignment_tolerance_s": "时间对齐容差",
    "max_gap_s": "最大连续间隔",
    "co_gaze_threshold": "共同注视距离阈值",
    "pupil_baseline_window_s": "瞳孔基线窗口",
    "event_match_tolerance_s": "事件匹配容差",
    "lag_signal": "lag 比较信号",
    "lag_max_s": "lag 最大绝对值",
    "lag_step_s": "lag 步长",
    "lag_window_s": "共同窗口长度",
    "lag_min_window_s": "最短共同支持",
    "lag_min_coverage": "最低共同覆盖率",
    "window_length_s": "分窗长度",
    "window_step_s": "分窗步长",
    "majority_fraction": "多数比例",
    "majority_min_members": "最少多数成员数",
    "network_threshold": "网络边阈值",
    "network_signal": "网络信号轴",
    "comparison_signal": "多变量比较信号",
    "dtw_window_fraction": "DTW Sakoe–Chiba 带宽比例",
    "screen_size_px": "屏幕像素尺寸",
    "multimatch.grouping": "MultiMatch 合并相邻注视",
    "multimatch.TDir": "MultiMatch TDir",
    "multimatch.TDur": "MultiMatch TDur",
    "multimatch.TAmp": "MultiMatch TAmp",
    "scanmatch.grid_cols": "ScanMatch 网格列数",
    "scanmatch.grid_rows": "ScanMatch 网格行数",
    "scanmatch.time_bin_s": "ScanMatch 时间箱",
    "scanmatch.substitution_matrix": "ScanMatch 替代矩阵 JSON",
    "scanmatch.gap_penalty": "ScanMatch 间隙罚分",
    "crqa.embedding_dimension": "CRQA 嵌入维数",
    "crqa.embedding_lag_samples": "CRQA 嵌入时延（样本）",
    "crqa.threshold": "CRQA 复现阈值",
    "crqa.min_diagonal_line": "CRQA 最短对角线（样本）",
    "crqa.min_vertical_line": "CRQA 最短垂直线（样本）",
    "crqa.theiler_window_samples": "CRQA Theiler 窗口（样本）",
}


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _key(value: Any) -> str:
    return "" if _missing(value) else str(value)


def _label(value: Any) -> str:
    return "（空值）" if _missing(value) else str(value)


def _split_refs(text: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in text.replace("\n", ",").split(",") if part.strip()))


class GroupDesignPage(QWidget):
    """Group design page with explicit scope slicing and source-evidence gates.

    The host can pass a ``NormalizedDataset`` (or its signals table), retrieve
    ``scope_state()``, and use ``slice_signals``/``slice_segments`` before it
    creates the immutable run snapshot. ``configuration_values`` contains only
    real individual/group consumer mappings and explicit metadata declarations.
    """

    configuration_changed = Signal(object)
    scope_changed = Signal(object)
    lag_view_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._signals = pd.DataFrame()
        self._segments = pd.DataFrame()
        self._events = pd.DataFrame()
        self._dataset_metadata: dict[str, Any] = {}
        self._member_state: dict[tuple[str, str, str], tuple[bool, str]] = {}
        self._lag_curve = pd.DataFrame()
        self._metric_results = pd.DataFrame()
        self._building_members = False
        self._forms: dict[str, TypedParameterForm] = {}
        self._parameter_cache: dict[str, Any] = {}
        self._parameter_drafts: dict[str, dict[str, Any]] = {}
        self._selected_group_metric_ids: tuple[str, ...] | None = None
        self._parameter_error_location: tuple[str, str] | None = None

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("群体分析", "group")
        self.mode_combo.addItem("个体范围预览", "individual")
        self.mode_combo.setToolTip("群体方法按明确 interaction_id 组成 N 人单元；个体模式不会生成群体对。")
        header.addWidget(QLabel("分析范围", self))
        header.addWidget(self.mode_combo)
        header.addStretch(1)
        self.refresh_button = QPushButton("刷新成员与配对", self)
        header.addWidget(self.refresh_button)
        root.addLayout(header)

        intro = QLabel(
            "只按明确的互动、试次、片段和刺激范围组成成员集合。分析只纳入勾选的成员及所选范围；"
            "角色须由设计者填写，不从相关峰推断。pair 数是分析单元内两两组合，不是独立团队 N。",
            self,
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.tabs = QTabWidget(self)
        self.design_tab = QWidget(self.tabs)
        self.parameter_tab = QWidget(self.tabs)
        self.evidence_tab = QWidget(self.tabs)
        self.results_tab = QWidget(self.tabs)
        self.tabs.addTab(self.design_tab, "成员与范围")
        self.tabs.addTab(self.parameter_tab, "群体参数")
        self.tabs.addTab(self.evidence_tab, "时钟与空间证据")
        self.tabs.addTab(self.results_tab, "成员对与 lag 曲线")
        root.addWidget(self.tabs, 1)

        self._build_design_tab()
        self._build_parameter_tab()
        self._build_evidence_tab()
        self._build_results_tab()

        self.mode_combo.currentIndexChanged.connect(self._on_design_changed)
        self.refresh_button.clicked.connect(self._refresh_design)
        for combo in self.scope_filters.values():
            combo.currentIndexChanged.connect(self._refresh_design)
        self._emit_configuration()

    def _build_design_tab(self) -> None:
        layout = QVBoxLayout(self.design_tab)
        filters = QHBoxLayout()
        self.scope_filters: dict[str, QComboBox] = {}
        for column, title in _FILTERS:
            control = QComboBox(self.design_tab)
            control.setObjectName(f"scope_{column}")
            control.addItem("全部", None)
            filters.addWidget(QLabel(title, self.design_tab))
            filters.addWidget(control, 1)
            self.scope_filters[column] = control
        layout.addLayout(filters)

        self.scope_summary = QLabel("请载入具有 subject_id、session_id、interaction_id 的候选信号表。", self.design_tab)
        self.scope_summary.setWordWrap(True)
        layout.addWidget(self.scope_summary)

        self.member_table = QTableWidget(0, 5, self.design_tab)
        self.member_table.setHorizontalHeaderLabels(["纳入", "互动单元", "成员 ID", "session ID", "角色"])
        self.member_table.setAlternatingRowColors(True)
        self.member_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.member_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.member_table, 2)
        self.role_summary = QLabel("未声明角色；角色方法保持不可用。", self.design_tab)
        self.role_summary.setWordWrap(True)
        layout.addWidget(self.role_summary)

        pair_heading = QLabel("预览成员对", self.design_tab)
        pair_heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(pair_heading)
        self.pair_preview = QTableWidget(0, 5, self.design_tab)
        self.pair_preview.setHorizontalHeaderLabels(["互动 / 范围", "成员 A", "成员 B", "时间匹配数", "时间范围重叠"])
        self.pair_preview.setAlternatingRowColors(True)
        self.pair_preview.horizontalHeader().setStretchLastSection(True)
        self.pair_preview.setMaximumHeight(180)
        layout.addWidget(self.pair_preview)
        self.pair_summary = QLabel("理论成员对：0 · 按时间戳可对齐：0", self.design_tab)
        layout.addWidget(self.pair_summary)

        self.member_table.itemChanged.connect(self._on_member_item_changed)

    def _build_parameter_tab(self) -> None:
        layout = QVBoxLayout(self.parameter_tab)
        help_text = QLabel(
            "参数直接使用 group 后端真实键；MultiMatch、ScanMatch、CRQA 采用后端要求的嵌套对象。"
            "只填写当前所选方法所需项。前检仍是最终条件判断，参数录入不会绕过单位、设备或覆盖率要求。",
            self.parameter_tab,
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.parameter_tabs = QTabWidget(self.parameter_tab)
        self._rebuild_parameter_forms()
        layout.addWidget(self.parameter_tabs, 1)
        self.parameter_status = QLabel("参数尚未校验。", self.parameter_tab)
        self.parameter_status.setWordWrap(True)
        parameter_status_row = QHBoxLayout()
        parameter_status_row.addWidget(self.parameter_status, 1)
        self.parameter_error_button = QPushButton("定位参数", self.parameter_tab)
        self.parameter_error_button.setEnabled(False)
        self.parameter_error_button.clicked.connect(self._focus_parameter_error)
        parameter_status_row.addWidget(self.parameter_error_button)
        layout.addLayout(parameter_status_row)

    def _rebuild_parameter_forms(self) -> None:
        """Render only definitions tied to selected public group leaves."""
        selected_fields = group_parameter_fields(self._selected_group_metric_ids)
        selected_names = {field["name"] for field in selected_fields}
        groups = group_parameter_field_groups()
        if self._selected_group_metric_ids is not None:
            groups = {
                name: [field for field in fields if field["name"] in selected_names]
                for name, fields in groups.items()
            }
        while self.parameter_tabs.count():
            widget = self.parameter_tabs.widget(0)
            self.parameter_tabs.removeTab(0)
            widget.deleteLater()
        self._forms.clear()
        for name, title in (("lag", "对齐与 lag"), ("consensus", "分窗、共识与网络"),
                            ("signal", "DTW 与 CRQA"), ("sequence", "MultiMatch 与 ScanMatch")):
            panel = QWidget(self.parameter_tabs)
            panel_layout = QVBoxLayout(panel)
            fields = [
                {
                    **field,
                    "description": (
                        str(field.get("description", ""))
                        + f" 配置路径：{field['config_path']}；消费位置：{field['consumer']}。"
                    ),
                }
                for field in groups[name]
            ]
            form = TypedParameterForm(fields, parent=panel)
            self._localize_form(form)
            panel_layout.addWidget(form)
            panel_layout.addStretch(1)
            self.parameter_tabs.addTab(panel, title)
            self._forms[name] = form
            form.set_values(self._parameter_cache)
            form.restore_draft_values({
                field_name: state for field_name, state in self._parameter_drafts.items()
                if field_name in form.editors
            })
        for form in self._forms.values():
            for editor in form.editors.values():
                for control in (editor, *editor.findChildren(QWidget)):
                    for signal_name in ("textChanged", "currentIndexChanged", "toggled", "valueChanged", "itemChanged"):
                        signal = getattr(control, signal_name, None)
                        if signal is not None and hasattr(signal, "connect"):
                            signal.connect(self._on_parameters_changed)

    def set_group_metric_ids(self, metric_ids: Sequence[str]) -> None:
        """Bind the parameter editors to the selected public group leaves."""
        values = tuple(sorted({str(metric_id) for metric_id in metric_ids if isinstance(metric_id, str)}))
        if values == self._selected_group_metric_ids:
            return
        # A disabled group editor can contain an unfinished string (for
        # example, the user typed a sign but no number yet). Preserve its raw
        # widget state before changing tabs; asking for parsed values here
        # would incorrectly block an unrelated individual-only selection.
        for form in self._forms.values():
            self._parameter_drafts.update(form.draft_values())
        self._selected_group_metric_ids = values
        self._rebuild_parameter_forms()
        self._on_parameters_changed()

    def _build_evidence_tab(self) -> None:
        layout = QVBoxLayout(self.evidence_tab)

        clock_box = QGroupBox("时钟与同步证据", self.evidence_tab)
        clock = QFormLayout(clock_box)
        self.time_confirmed = QCheckBox("已核对各成员的时间来源、单位和基准；归一化时间为秒", clock_box)
        self.time_source_refs = QLineEdit(clock_box)
        self.time_source_refs.setPlaceholderText("时间来源引用，逗号分隔")
        self.sync_confirmed = QCheckBox("已根据记录确认成员时钟同步", clock_box)
        self.sync_method = QLineEdit(clock_box)
        self.sync_method.setPlaceholderText("同步方法，例如共享硬件触发或已应用的校准")
        self.sync_source_refs = QLineEdit(clock_box)
        self.sync_source_refs.setPlaceholderText("同步证据引用，逗号分隔")
        self.clock_offsets = QLineEdit(clock_box)
        self.clock_offsets.setPlaceholderText('可选 JSON，例如 {"clock-A": 0.0, "clock-B": 0.012}')
        self.offsets_note = QLabel(
            "此处只登记 offset；当前流程不会将其应用到时间值，存在 offset 时同步指标保持阻断。",
            clock_box,
        )
        self.offsets_note.setWordWrap(True)
        self.time_precision = QLineEdit(clock_box)
        self.time_precision.setPlaceholderText("例如 0.001")
        clock.addRow(self.time_confirmed)
        clock.addRow("时间证据引用", self.time_source_refs)
        clock.addRow(self.sync_confirmed)
        clock.addRow("同步方法", self.sync_method)
        clock.addRow("同步证据引用", self.sync_source_refs)
        clock.addRow("时钟 offset（秒）", self.clock_offsets)
        clock.addRow(self.offsets_note)
        clock.addRow("时间分辨率（秒）", self.time_precision)
        layout.addWidget(clock_box)

        space_box = QGroupBox("空间与信号可比性", self.evidence_tab)
        space = QFormLayout(space_box)
        self.coordinate_unit = QLineEdit(space_box)
        self.coordinate_frame = QLineEdit(space_box)
        self.spatial_refs = QLineEdit(space_box)
        self.spatial_refs.setPlaceholderText("同坐标系或显式变换的依据，逗号分隔")
        self.spatial_comparable = QCheckBox("已确认同一坐标 frame，或已应用可复核的坐标变换", space_box)
        self.pupil_unit = QLineEdit(space_box)
        self.pupil_refs = QLineEdit(space_box)
        self.pupil_refs.setPlaceholderText("瞳孔单位/预处理一致性的依据，逗号分隔")
        self.pupil_comparable = QCheckBox("已确认跨成员瞳孔单位与预处理可比", space_box)
        self.aoi_confirmed = QCheckBox("已确认所有成员使用同一 AOI 映射", space_box)
        self.aoi_refs = QLineEdit(space_box)
        self.aoi_refs.setPlaceholderText("AOI 映射证据引用，逗号分隔")
        space.addRow("注视坐标单位", self.coordinate_unit)
        space.addRow("注视坐标 frame", self.coordinate_frame)
        space.addRow(self.spatial_comparable)
        space.addRow("空间证据引用", self.spatial_refs)
        space.addRow("瞳孔单位", self.pupil_unit)
        space.addRow(self.pupil_comparable)
        space.addRow("瞳孔证据引用", self.pupil_refs)
        space.addRow(self.aoi_confirmed)
        space.addRow("AOI 证据引用", self.aoi_refs)
        layout.addWidget(space_box)

        note = QLabel(
            "同时起录、相同刺激名称或字段名相同都不能自动确认为同步/同坐标系。"
            "瞳孔标量比较使用独立单位与预处理证据，不要求屏幕坐标。所有复选框都只记录用户声明，不认证设备准确度。",
            self.evidence_tab,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        self.evidence_status = QLabel("时钟与空间证据未确认。", self.evidence_tab)
        self.evidence_status.setWordWrap(True)
        layout.addWidget(self.evidence_status)
        for widget in (
            self.time_confirmed, self.time_source_refs, self.sync_confirmed, self.sync_method,
            self.sync_source_refs, self.clock_offsets, self.time_precision,
            self.coordinate_unit, self.coordinate_frame, self.spatial_refs, self.spatial_comparable,
            self.pupil_unit, self.pupil_refs, self.pupil_comparable, self.aoi_confirmed, self.aoi_refs,
        ):
            signal = "toggled" if isinstance(widget, QCheckBox) else "textChanged"
            getattr(widget, signal).connect(self._emit_configuration)

    def _build_results_tab(self) -> None:
        layout = QVBoxLayout(self.results_tab)
        self.lag_sign_convention = QLabel(
            "lag 定义：r_AB(τ)=corr(A(t), B(t+τ))。正值表示 B 晚于 A。下面只呈现后端返回的真实 lag_curve 行；未运行时不绘制示意曲线。",
            self.results_tab,
        )
        self.lag_sign_convention.setWordWrap(True)
        layout.addWidget(self.lag_sign_convention)
        self.lag_summary = QLabel("尚无后端 lag_curve 结果。", self.results_tab)
        layout.addWidget(self.lag_summary)
        self.lag_table = QTableWidget(0, 9, self.results_tab)
        self.lag_table.setHorizontalHeaderLabels([
            "互动", "成员对", "角色方向", "lag (s)", "相关", "原始共同 N", "有效时长 (s)", "覆盖率", "边界",
        ])
        self.lag_table.setAlternatingRowColors(True)
        self.lag_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.lag_table, 2)

        result_heading = QLabel("实际成员对指标结果", self.results_tab)
        result_heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(result_heading)
        self.result_table = QTableWidget(0, 8, self.results_tab)
        self.result_table.setHorizontalHeaderLabels([
            "互动", "成员对", "metric_id", "值", "单位", "状态 / 原因", "原始 N", "有效时长 (s)",
        ])
        self.result_table.setAlternatingRowColors(True)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.result_table, 2)

    @staticmethod
    def _localize_form(form: TypedParameterForm) -> None:
        layout = form.layout()
        for name, editor in form.editors.items():
            label = layout.labelForField(editor)
            if label is not None:
                label.setText(_FIELD_LABELS.get(name, name.rsplit(".", 1)[-1].replace("_", " ")))

    def set_dataset(self, dataset: Any, *, segments: pd.DataFrame | None = None,
                    events: pd.DataFrame | None = None) -> None:
        """Load a NormalizedDataset, DatasetRevision, or explicit signals table."""
        if hasattr(dataset, "dataset_copy"):
            dataset = dataset.dataset_copy()
        if hasattr(dataset, "signals") and isinstance(dataset.signals, pd.DataFrame):
            self._signals = dataset.signals.copy(deep=True)
            self._dataset_metadata = dict(getattr(dataset, "metadata", {}) or {})
            self._segments = (segments if segments is not None else getattr(dataset, "segments", None))
            self._events = (events if events is not None else getattr(dataset, "events", None))
        elif isinstance(dataset, pd.DataFrame):
            self._signals = dataset.copy(deep=True)
            self._dataset_metadata = {}
            self._segments = segments
            self._events = events
        else:
            raise TypeError("set_dataset expects NormalizedDataset, DatasetRevision, or a signals DataFrame")
        self._signals = self._signals.copy(deep=True)
        self._segments = self._copy_frame(self._segments)
        self._events = self._copy_frame(self._events)
        self._load_existing_metadata()
        self._populate_filters()
        self._refresh_design()

    @staticmethod
    def _copy_frame(value: Any) -> pd.DataFrame:
        return value.copy(deep=True) if isinstance(value, pd.DataFrame) else pd.DataFrame()

    def _load_existing_metadata(self) -> None:
        raw = self._dataset_metadata
        cm = raw.get("coordinate_metadata", {}) if isinstance(raw.get("coordinate_metadata"), Mapping) else {}
        for widget, value in (
            (self.coordinate_unit, cm.get("coordinate_unit", raw.get("coordinate_unit"))),
            (self.coordinate_frame, cm.get("coordinate_frame", raw.get("coordinate_frame"))),
            (self.pupil_unit, raw.get("pupil_unit")),
            (self.time_precision, cm.get("time_precision_s", raw.get("time_precision_s"))),
        ):
            if value not in (None, "", "unknown"):
                widget.setText(str(value))
        pupil_unit = cm.get("pupil_unit")
        if isinstance(pupil_unit, str) and pupil_unit not in ("", "unknown"):
            self.pupil_unit.setText(pupil_unit)
        if raw.get("time_confirmed") is True or cm.get("time_confirmed") is True:
            self.time_confirmed.setChecked(True)
        if raw.get("cross_subject_spatial_comparable") is True:
            self.spatial_comparable.setChecked(True)
        if raw.get("cross_subject_pupil_comparable") is True:
            self.pupil_comparable.setChecked(True)
        if raw.get("aoi_mapping_confirmed") is True:
            self.aoi_confirmed.setChecked(True)
        sync = raw.get("synchronization", {})
        if isinstance(sync, Mapping):
            if sync.get("confirmed") is True:
                self.sync_confirmed.setChecked(True)
            self.sync_method.setText(str(sync.get("method") or ""))
            refs = sync.get("source_refs", ())
            self.sync_source_refs.setText(", ".join(str(x) for x in refs) if isinstance(refs, (list, tuple)) else "")
        for field, widget in (("time_source_refs", self.time_source_refs),
                              ("spatial_comparability_refs", self.spatial_refs),
                              ("pupil_comparability_refs", self.pupil_refs),
                              ("aoi_mapping_refs", self.aoi_refs)):
            refs = raw.get(field, ())
            if isinstance(refs, (list, tuple)):
                widget.setText(", ".join(str(x) for x in refs))
        offsets = cm.get("clock_offsets_s")
        if isinstance(offsets, Mapping) and offsets:
            self.clock_offsets.setText(json.dumps(dict(offsets), ensure_ascii=False))

    def _populate_filters(self) -> None:
        for column, combo in self.scope_filters.items():
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部", None)
            values: list[Any] = []
            for frame in (self._signals, self._segments):
                if column in frame:
                    values.extend(value for value in frame[column].dropna().drop_duplicates().tolist()
                                  if not any(type(value) is type(old) and value == old for old in values))
            for value in values:
                combo.addItem(_label(value), value)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _filter_value(self, column: str) -> Any:
        combo = self.scope_filters[column]
        value = combo.currentData()
        return None if value == _ALL else value

    def _scope_filters_value(self) -> dict[str, Any]:
        return {name: self._filter_value(name) for name, _ in _FILTERS}

    def _matches_scope(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy(deep=True)
        for column, _ in _FILTERS:
            wanted = self._filter_value(column)
            if wanted is not None and column in result:
                result = result.loc[result[column].map(lambda value: not _missing(value) and type(value) is type(wanted) and value == wanted)]
        return result

    def _member_key(self, row: Mapping[str, Any]) -> tuple[str, str, str]:
        return (_key(row.get("interaction_id")), _key(row.get("subject_id")), _key(row.get("session_id")))

    def _active_member_keys(self) -> set[tuple[str, str, str]]:
        active = self._matches_scope(self._signals)
        if self._filter_value("segment_id") is not None and "segment_id" not in active:
            active = self._apply_selected_segment_interval(active)
        if not {"subject_id", "session_id"}.issubset(active.columns):
            return set()
        group_cols = [column for column in ("interaction_id", "subject_id", "session_id") if column in active]
        keys: set[tuple[str, str, str]] = set()
        for group_key in active.groupby(group_cols, dropna=False, sort=False).groups:
            values = group_key if isinstance(group_key, tuple) else (group_key,)
            row = dict(zip(group_cols, values))
            row.setdefault("interaction_id", None)
            keys.add(self._member_key(row))
        return keys

    def _refresh_design(self, *_: Any) -> None:
        self._refresh_member_table()
        self._refresh_pairs()
        self.scope_summary.setText(self._scope_description())
        self.role_summary.setText(self.role_validation_message())
        self._update_mode_widgets()
        self._emit_scope()
        self._emit_configuration()

    def _refresh_member_table(self) -> None:
        frame = self._matches_scope(self._signals)
        needed = {"subject_id", "session_id"}
        if not needed.issubset(frame.columns):
            self._building_members = True
            self.member_table.setRowCount(0)
            self._building_members = False
            return
        group_cols = [column for column in ("interaction_id", "subject_id", "session_id") if column in frame]
        grouped = frame.groupby(group_cols, dropna=False, sort=True)
        records: list[tuple[tuple[str, str, str], dict[str, Any], int]] = []
        for group_key, rows in grouped:
            values = group_key if isinstance(group_key, tuple) else (group_key,)
            row = dict(zip(group_cols, values))
            row.setdefault("interaction_id", None)
            key = self._member_key(row)
            records.append((key, row, len(rows)))
            self._member_state.setdefault(key, (True, ""))
        self._building_members = True
        self.member_table.setRowCount(len(records))
        for row_index, (key, record, n_rows) in enumerate(records):
            checked, role = self._member_state.get(key, (True, ""))
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, key)
            interaction = QTableWidgetItem(_label(record.get("interaction_id")))
            subject = QTableWidgetItem(_label(record.get("subject_id")))
            subject.setData(Qt.ItemDataRole.UserRole, key)
            session = QTableWidgetItem(_label(record.get("session_id")))
            session.setToolTip(f"信号行数：{n_rows:,}")
            role_combo = QComboBox(self.member_table)
            role_combo.addItem("未指定", "")
            role_combo.addItem("leader", "leader")
            role_combo.addItem("follower", "follower")
            role_combo.setCurrentIndex(max(0, role_combo.findData(role)))
            role_combo.setEnabled(self.mode_combo.currentData() == "group")
            role_combo.currentIndexChanged.connect(lambda _index, member_key=key, control=role_combo:
                                                   self._set_role(member_key, str(control.currentData() or "")))
            for column, item in enumerate((check, interaction, subject, session)):
                self.member_table.setItem(row_index, column, item)
            self.member_table.setCellWidget(row_index, 4, role_combo)
        self._building_members = False
        self.member_table.resizeColumnsToContents()

    def _on_member_item_changed(self, item: QTableWidgetItem) -> None:
        if self._building_members or item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(key, tuple) or len(key) != 3:
            return
        _, role = self._member_state.get(key, (False, ""))
        self._member_state[key] = (item.checkState() == Qt.CheckState.Checked, role)
        self._refresh_pairs()
        self.role_summary.setText(self.role_validation_message())
        self._emit_scope()
        self._emit_configuration()

    def _set_role(self, key: tuple[str, str, str], role: str) -> None:
        checked, _old = self._member_state.get(key, (True, ""))
        self._member_state[key] = (checked, role)
        self.role_summary.setText(self.role_validation_message())
        self._emit_configuration()
        self._emit_scope()

    def _selected_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = self._matches_scope(frame)
        if not {"subject_id", "session_id"}.issubset(result.columns):
            return result
        selected_keys = {key for key, (checked, _role) in self._member_state.items() if checked}
        if not selected_keys:
            return result.iloc[0:0].copy()
        mask = result.apply(lambda row: self._member_key(row) in selected_keys, axis=1)
        result = result.loc[mask].copy()
        return self._apply_selected_segment_interval(result)

    def _apply_selected_segment_interval(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Apply selected segment boundaries when signals have no segment_id yet."""
        segment_id = self._filter_value("segment_id")
        if segment_id is None or "segment_id" in frame or self._segments.empty:
            return frame
        required = {"segment_id", "start_s", "end_s"}
        if not required.issubset(self._segments.columns) or "time_s" not in frame:
            return frame
        segments = self._matches_scope(self._segments)
        segments = segments.loc[segments["segment_id"].map(
            lambda value: not _missing(value) and type(value) is type(segment_id) and value == segment_id
        )]
        if segments.empty:
            return frame.iloc[0:0].copy()
        keep = pd.Series(False, index=frame.index)
        times = pd.to_numeric(frame["time_s"], errors="coerce")
        for segment in segments.to_dict("records"):
            start, end = segment.get("start_s"), segment.get("end_s")
            if _missing(start) or _missing(end):
                continue
            scope = pd.Series(True, index=frame.index)
            for identity in _SEGMENT_SCOPE_COLUMNS:
                value = segment.get(identity)
                if identity == "timebase_id" and (
                    _missing(value) or str(value) in _UNCONFIRMED_TIMEBASES
                ):
                    continue
                if identity not in frame:
                    if not _missing(value):
                        scope &= False
                    continue
                if not _missing(value):
                    scope &= frame[identity].astype("string").eq(str(value)).fillna(False)
                elif identity in {"subject_id", "session_id"}:
                    scope &= False

            timebase = segment.get("timebase_id")
            if _missing(timebase) or str(timebase) in _UNCONFIRMED_TIMEBASES:
                scoped_rows = frame.loc[scope]
                ambiguous_timebase = (
                    "timebase_id" in scoped_rows and scoped_rows["timebase_id"].dropna().astype(str).nunique() > 1
                )
                ambiguous_source = (
                    "source_id" in scoped_rows and scoped_rows["source_id"].dropna().astype(str).nunique() > 1
                )
                if ambiguous_timebase or ambiguous_source:
                    continue

            interval = times.ge(float(start)).fillna(False) & times.lt(float(end)).fillna(False)
            keep |= scope & interval
        return frame.loc[keep].copy()

    def slice_signals(self, frame: pd.DataFrame | None = None) -> pd.DataFrame:
        """Return an isolated signal slice for the current scope and member checks."""
        source = self._signals if frame is None else frame
        if not isinstance(source, pd.DataFrame):
            raise TypeError("slice_signals expects a DataFrame")
        return self._selected_frame(source)

    def slice_segments(self, frame: pd.DataFrame | None = None) -> pd.DataFrame:
        """Return matching segment definitions for host-side snapshot construction."""
        source = self._segments if frame is None else frame
        if not isinstance(source, pd.DataFrame):
            raise TypeError("slice_segments expects a DataFrame")
        result = self._matches_scope(source)
        if not {"subject_id", "session_id"}.issubset(result.columns):
            return result
        selected_keys = {key for key, (checked, _role) in self._member_state.items() if checked}
        mask = result.apply(lambda row: self._member_key(row) in selected_keys, axis=1)
        return result.loc[mask].copy()

    def slice_events(self, frame: pd.DataFrame | None = None) -> pd.DataFrame:
        """Return matching event rows when the host builds a scoped event table."""
        source = self._events if frame is None else frame
        if not isinstance(source, pd.DataFrame):
            raise TypeError("slice_events expects a DataFrame")
        result = self._matches_scope(source)
        if not {"subject_id", "session_id"}.issubset(result.columns):
            return result
        selected_keys = {key for key, (checked, _role) in self._member_state.items() if checked}
        mask = result.apply(lambda row: self._member_key(row) in selected_keys, axis=1)
        return result.loc[mask].copy()

    def scope_state(self) -> dict[str, Any]:
        active_keys = self._active_member_keys()
        selected = [
            {"interaction_id": key[0] or None, "subject_id": key[1], "session_id": key[2], "role": role or None}
            for key, (checked, role) in sorted(self._member_state.items()) if checked and key in active_keys
        ]
        signals = self.slice_signals()
        member_n = len({(str(row["subject_id"]), str(row["session_id"])) for row in selected})
        pair_total, pair_time = self._pair_counts(signals)
        return {
            "mode": self.mode_combo.currentData(),
            "filters": self._scope_filters_value(),
            "member_sessions": selected,
            "member_count": member_n,
            "theoretical_pair_count": pair_total if self.mode_combo.currentData() == "group" else 0,
            "time_matched_pair_count": pair_time if self.mode_combo.currentData() == "group" else 0,
            "pair_count_is_independent_team_n": False,
        }

    def _scope_description(self) -> str:
        filters = self._scope_filters_value()
        active = [f"{title}={_label(filters[column])}" for column, title in _FILTERS if filters[column] is not None]
        active_keys = self._active_member_keys()
        selected = [key for key, (checked, _) in self._member_state.items() if checked and key in active_keys]
        if not self._signals.empty and not {"interaction_id", "subject_id", "session_id"}.issubset(self._signals.columns):
            return "当前信号表缺少明确 interaction_id 或成员 session ID；群体可用性前检会按真实缺字段原因阻断。"
        return "；".join(active + [f"已选成员 session：{len(selected)}"] + ["未选成员不参与本次分析"])

    def _update_mode_widgets(self) -> None:
        group_mode = self.mode_combo.currentData() == "group"
        self.member_table.setEnabled(True)
        self.pair_preview.setEnabled(group_mode)
        self.parameter_tabs.setEnabled(group_mode)
        if not group_mode:
            self.pair_summary.setText("个体范围预览：不会生成成员对或群体统计。")

    def _pair_counts(self, frame: pd.DataFrame) -> tuple[int, int]:
        if self.mode_combo.currentData() != "group" or frame.empty or "interaction_id" not in frame:
            return 0, 0
        columns = [c for c in ("interaction_id", "trial", "stimulus_id", "segment_id", "analysis_stream_id", "eye_label") if c in frame]
        if not columns:
            return 0, 0
        possible = matched = 0
        for _unit, rows in frame.groupby(columns, dropna=False, sort=False):
            members = [member for _, member in rows.groupby(["subject_id", "session_id"], dropna=False, sort=False)] if {"subject_id", "session_id"}.issubset(rows.columns) else []
            for first, second in combinations(members, 2):
                possible += 1
                if self._time_match_count(first, second) > 0:
                    matched += 1
        return possible, matched

    def _time_match_count(self, first: pd.DataFrame, second: pd.DataFrame) -> int:
        if "time_s" not in first or "time_s" not in second:
            return 0
        a = pd.to_numeric(first["time_s"], errors="coerce").dropna().to_numpy(dtype=float)
        b = pd.to_numeric(second["time_s"], errors="coerce").dropna().to_numpy(dtype=float)
        if not len(a) or not len(b):
            return 0
        a.sort()
        b.sort()
        try:
            tolerance = float(self._raw_parameter_values().get("alignment_tolerance_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            tolerance = 0.0
        tolerance = max(0.0, tolerance) if math.isfinite(tolerance) else 0.0
        indexes = b.searchsorted(a)
        count = 0
        for value, index in zip(a, indexes):
            nearby = [candidate for candidate in (index - 1, index) if 0 <= candidate < len(b)]
            if nearby and min(abs(float(b[candidate] - value)) for candidate in nearby) <= tolerance + 1e-12:
                count += 1
        return count

    def _refresh_pairs(self) -> None:
        frame = self.slice_signals()
        if self.mode_combo.currentData() != "group" or frame.empty or not {"interaction_id", "subject_id", "session_id"}.issubset(frame.columns):
            self.pair_preview.setRowCount(0)
            self.pair_summary.setText("理论成员对：0 · 按时间戳可对齐：0")
            return
        columns = [c for c in ("interaction_id", "trial", "stimulus_id", "segment_id", "analysis_stream_id", "eye_label") if c in frame]
        rows_out: list[tuple[str, str, str, int, str]] = []
        for unit, group in frame.groupby(columns, dropna=False, sort=False):
            values = unit if isinstance(unit, tuple) else (unit,)
            unit_label = " / ".join(f"{column}={_label(value)}" for column, value in zip(columns, values))
            members = [member for _, member in group.groupby(["subject_id", "session_id"], dropna=False, sort=False)]
            for first, second in combinations(members, 2):
                a = first.iloc[0]
                b = second.iloc[0]
                count = self._time_match_count(first, second)
                ta = pd.to_numeric(first["time_s"], errors="coerce").dropna() if "time_s" in first else pd.Series(dtype=float)
                tb = pd.to_numeric(second["time_s"], errors="coerce").dropna() if "time_s" in second else pd.Series(dtype=float)
                overlap = "是" if len(ta) and len(tb) and max(float(ta.min()), float(tb.min())) <= min(float(ta.max()), float(tb.max())) else "否"
                rows_out.append((unit_label, f"{a['subject_id']} · {a['session_id']}",
                                 f"{b['subject_id']} · {b['session_id']}", count, overlap))
        self.pair_preview.setRowCount(len(rows_out))
        for row_index, values in enumerate(rows_out):
            for column, value in enumerate(values):
                self.pair_preview.setItem(row_index, column, QTableWidgetItem(str(value)))
        possible = len(rows_out)
        matched = sum(row[3] > 0 for row in rows_out)
        self.pair_summary.setText(
            f"理论成员对：{possible} · 按当前对齐容差有时间匹配的对：{matched} · "
            "配对时间支持不等同于某项指标前检可用；方法级信号质量/共同覆盖仍由后端判断。"
        )
        self.pair_preview.resizeColumnsToContents()

    def _raw_parameter_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for form in self._forms.values():
            values.update(form.values())
        return values

    def _report_parameter_error(self, message: str) -> None:
        text = str(message)
        definitions = group_parameter_fields()
        candidates = sorted(
            (str(item["name"]) for item in definitions if isinstance(item.get("name"), str)),
            key=len,
            reverse=True,
        )
        name = next(
            (candidate for candidate in candidates
             if text.startswith(candidate + ":") or text.startswith(candidate + " ")
             or f"group.{candidate}:" in text or f"group.{candidate}=" in text),
            None,
        )
        self._parameter_error_location = None
        self.parameter_error_button.setEnabled(False)
        if name is not None:
            form_name = next((key for key, form in self._forms.items() if name in form.editors), None)
            if form_name is not None:
                self._parameter_error_location = (form_name, name)
                self.parameter_error_button.setEnabled(True)
        reason = text
        if name is not None:
            for prefix in (name + ":", name + " ", f"group.{name}:", f"group.{name}="):
                if reason.startswith(prefix):
                    reason = reason[len(prefix):].strip()
                    break
            label = _FIELD_LABELS.get(name, name)
            editor = next((form.editors[name] for form in self._forms.values() if name in form.editors), None)
            hint = editor._range_hint() if editor is not None else ""
            range_text = f"（{hint}）" if hint else ""
            self.parameter_status.setText(f"参数错误：{label} / {name}{range_text}：{reason}。请按字段类型、单位和范围修正。")
        else:
            self.parameter_status.setText(f"参数错误：{reason}。请检查群体参数的类型、单位和范围。")

    def _clear_parameter_error(self) -> None:
        self._parameter_error_location = None
        self.parameter_error_button.setEnabled(False)

    def _focus_parameter_error(self) -> None:
        location = self._parameter_error_location
        if location is None:
            return
        form_name, name = location
        self.tabs.setCurrentWidget(self.parameter_tab)
        for index in range(self.parameter_tabs.count()):
            widget = self.parameter_tabs.widget(index)
            form = self._forms.get(form_name)
            if form is not None and widget.isAncestorOf(form):
                self.parameter_tabs.setCurrentIndex(index)
                form.focus_field(name)
                return

    def parameter_values(self) -> dict[str, Any]:
        """Bind selected typed values to their public group config paths."""
        try:
            raw = self._raw_parameter_values()
            result: dict[str, Any] = {}
            definitions = group_parameter_fields(self._selected_group_metric_ids)
            for definition in definitions:
                name = definition["name"]
                value = raw.get(name, self._parameter_cache.get(name, definition.get("default")))
                self._set_config_path(result, definition["config_path"], value, expected_module="group")
            self._clear_parameter_error()
            return result
        except (TypeError, ValueError, OverflowError) as exc:
            self._report_parameter_error(str(exc))
            raise

    @staticmethod
    def _set_config_path(target: dict[str, Any], path: str, value: Any, *, expected_module: str) -> None:
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

    def set_parameter_values(self, values: Mapping[str, Any]) -> None:
        """Restore group consumer values from either a group block or a full config.

        The host may pass ``{"group": {...}}`` as part of the immutable run
        configuration, or the group mapping directly. Nested backend mappings
        are expanded only for the typed editors that own those values.
        """
        if not isinstance(values, Mapping):
            raise TypeError("set_parameter_values expects a mapping")
        group_values = values.get("group", values)
        if not isinstance(group_values, Mapping):
            raise TypeError("group parameter values must be a mapping")

        flattened: dict[str, Any] = {}
        for name, value in group_values.items():
            if name in {"crqa", "multimatch", "scanmatch"} and isinstance(value, Mapping):
                flattened.update({f"{name}.{child}": child_value for child, child_value in value.items()})
            else:
                flattened[str(name)] = value
        self._parameter_cache.update(flattened)
        for name in flattened:
            self._parameter_drafts.pop(name, None)
        for form in self._forms.values():
            form.set_values(flattened)
        self._on_parameters_changed()

    def _on_parameters_changed(self, *_: Any) -> None:
        # Alignment tolerance is reflected in the pair-time preview as well as
        # the backend configuration; other parameter edits update the same
        # configuration signal for snapshot construction.
        self._refresh_pairs()
        self._emit_configuration()

    def _metadata_values(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        time_refs = _split_refs(self.time_source_refs.text())
        sync_refs = _split_refs(self.sync_source_refs.text())
        spatial_refs = _split_refs(self.spatial_refs.text())
        pupil_refs = _split_refs(self.pupil_refs.text())
        aoi_refs = _split_refs(self.aoi_refs.text())

        # The normalized source clock and its confirmation belong to import
        # metadata. A group-page declaration may add confirmation evidence,
        # but an unchecked or incomplete group form must not revoke it.
        source_metadata = self._dataset_metadata.get("analysis_metadata", {})
        source_time_confirmed = self._dataset_metadata.get("time_confirmed") is True
        if isinstance(source_metadata, Mapping):
            source_time_confirmed = source_time_confirmed or source_metadata.get("time_confirmed") is True
        if source_time_confirmed or (self.time_confirmed.isChecked() and time_refs):
            metadata["time_confirmed"] = True
        if time_refs:
            metadata["time_source_refs"] = time_refs
        if self.sync_confirmed.isChecked() and self.sync_method.text().strip() and sync_refs:
            metadata["synchronization"] = {
                "confirmed": True,
                "method": self.sync_method.text().strip(),
                "source_refs": sync_refs,
            }
        else:
            metadata["synchronization"] = {
                "confirmed": False,
                "method": self.sync_method.text().strip(),
                "source_refs": sync_refs,
            }
        offset_text = self.clock_offsets.text().strip()
        if offset_text:
            try:
                offsets = json.loads(offset_text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"时钟 offset JSON 格式错误：{exc.msg}") from exc
            if not isinstance(offsets, dict):
                raise ValueError("时钟 offset 必须是 JSON 对象")
            for clock_id, value in offsets.items():
                if not isinstance(clock_id, str) or not clock_id.strip() or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError("每个 clock offset 都需要非空时钟 ID 与有限秒数")
            metadata["coordinate_metadata"] = {"clock_offsets_s": offsets}
            # No pipeline stage currently changes the signal time values by
            # these offsets. Preserve them as a declaration, but never report
            # that they were applied based only on a user checkbox.
            metadata["clock_offsets_applied"] = False
        if self.time_precision.text().strip():
            precision = float(self.time_precision.text().strip())
            if not math.isfinite(precision) or precision <= 0:
                raise ValueError("时间分辨率必须是有限正数")
            metadata["time_precision_s"] = precision

        unit = self.coordinate_unit.text().strip()
        frame = self.coordinate_frame.text().strip()
        if unit:
            metadata["coordinate_unit"] = unit
        if frame:
            metadata["coordinate_frame"] = frame
        metadata["cross_subject_spatial_comparable"] = bool(
            self.spatial_comparable.isChecked() and unit and unit.casefold() != "unknown"
            and frame and frame.casefold() != "unknown" and spatial_refs
        )
        if spatial_refs:
            metadata["spatial_comparability_refs"] = spatial_refs
        pupil_unit = self.pupil_unit.text().strip()
        if pupil_unit:
            metadata["pupil_unit"] = pupil_unit
        metadata["cross_subject_pupil_comparable"] = bool(
            self.pupil_comparable.isChecked() and pupil_unit and pupil_unit.casefold() != "unknown" and pupil_refs
        )
        if pupil_refs:
            metadata["pupil_comparability_refs"] = pupil_refs
        metadata["aoi_mapping_confirmed"] = bool(self.aoi_confirmed.isChecked() and aoi_refs)
        if aoi_refs:
            metadata["aoi_mapping_refs"] = aoi_refs
        return metadata

    def configuration_values(self, *, include_group: bool = True) -> dict[str, Any]:
        """Return active group values and explicit metadata declarations.

        Disabled group drafts remain in their editors/cache but are not parsed
        into a run snapshot, so an inactive method field cannot block an
        otherwise valid individual-only run.
        """
        if not include_group:
            return {"group": {}, "metadata": {}}
        group = self.parameter_values()
        roles = self._validated_roles()
        if roles:
            group["roles"] = roles
        return {"group": group, "metadata": self._metadata_values()}

    def _validated_roles(self) -> dict[str, str]:
        if self.mode_combo.currentData() != "group":
            return {}
        active_keys = self._active_member_keys()
        selected = [(key, role) for key, (checked, role) in self._member_state.items()
                    if checked and role and key in active_keys]
        roles: dict[str, str] = {}
        for key, role in selected:
            # The group API keys roles by subject_id, so multiple session or
            # interaction assignments must agree before a mapping is emitted.
            subject_id = key[1]
            if subject_id in roles and roles[subject_id] != role:
                return {}
            roles[subject_id] = role
        if len(roles) < 2 or list(roles.values()).count("leader") != 1 or any(v not in {"leader", "follower"} for v in roles.values()):
            return {}
        return roles

    def role_validation_message(self) -> str:
        role_map = self._validated_roles()
        active_keys = self._active_member_keys()
        assigned = sum(bool(role) and checked and key in active_keys
                       for key, (checked, role) in self._member_state.items())
        if assigned == 0:
            return "未声明角色；角色方法保持不可用。"
        if not role_map:
            return "角色配置未提交：需对至少两名已选成员明确分配角色，并恰有一名 leader。"
        return f"已显式记录 {len(role_map)} 个成员角色；leader/follower 仅用于指定方向的 lag 分析。"

    def parameter_validation_messages(self) -> tuple[str, ...]:
        raw = self._raw_parameter_values()
        messages: list[str] = []
        for name, value in raw.items():
            if value is None:
                continue
            definition = next((record for record in group_parameter_fields() if record["name"] == name), {})
            try:
                numeric = float(value) if not isinstance(value, bool) else None
            except (TypeError, ValueError):
                numeric = None
            if numeric is not None:
                if not math.isfinite(numeric):
                    messages.append(f"{_FIELD_LABELS.get(name, name)}必须是有限数值。")
                if definition.get("minimum_exclusive") is not None and numeric <= float(definition["minimum_exclusive"]):
                    messages.append(f"{_FIELD_LABELS.get(name, name)}必须大于 {definition['minimum_exclusive']}。")
                if definition.get("minimum") is not None and numeric < float(definition["minimum"]):
                    messages.append(f"{_FIELD_LABELS.get(name, name)}不能小于 {definition['minimum']}。")
                if definition.get("maximum") is not None and numeric > float(definition["maximum"]):
                    messages.append(f"{_FIELD_LABELS.get(name, name)}不能大于 {definition['maximum']}。")
        window = raw.get("pupil_baseline_window_s")
        if window is not None and (len(window) != 2 or not float(window[0]) < float(window[1])):
            messages.append("瞳孔基线窗口必须由两个递增时点构成。")
        step = raw.get("lag_step_s")
        try:
            precision = float(self.time_precision.text()) if self.time_precision.text().strip() else None
        except ValueError:
            precision = None
        if step is not None and precision is not None and float(step) < precision:
            messages.append("lag 步长低于已声明的时间分辨率；请调整，否则后端会拒绝该 lag 网格。")
        screen = raw.get("screen_size_px")
        if screen is not None and (len(screen) != 2 or any(not math.isfinite(float(x)) or float(x) <= 0 for x in screen)):
            messages.append("屏幕尺寸需要两个有限正数。")
        window_length, window_step = raw.get("window_length_s"), raw.get("window_step_s")
        if (window_length is None) != (window_step is None):
            messages.append("分窗方法需要同时填写 window_length_s 和 window_step_s。")
        self.parameter_status.setText("参数检查：" + ("；".join(messages) if messages else "当前值类型与已声明范围一致；最终可用性以真实前检为准。"))
        return tuple(messages)

    def set_lag_curve(self, frame: pd.DataFrame) -> None:
        """Render the actual lag_curve table returned by the group backend."""
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("set_lag_curve expects the backend lag_curve DataFrame")
        self._lag_curve = frame.copy(deep=True)
        self._render_lag_curve()

    def set_metric_results(self, frame: pd.DataFrame) -> None:
        """Render actual long-format group metric rows, preserving status/reason."""
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("set_metric_results expects the backend metrics DataFrame")
        self._metric_results = frame.copy(deep=True)
        self._render_metric_results()

    def set_results(self, *, lag_curve: pd.DataFrame | None = None,
                    metrics: pd.DataFrame | None = None) -> None:
        if lag_curve is not None:
            self.set_lag_curve(lag_curve)
        if metrics is not None:
            self.set_metric_results(metrics)

    @staticmethod
    def _cell_text(row: Mapping[str, Any], key: str, digits: int = 6) -> str:
        value = row.get(key)
        if _missing(value):
            return "NA"
        if isinstance(value, float):
            return f"{value:.{digits}g}"
        return str(value)

    def _render_lag_curve(self) -> None:
        columns = ("interaction_id", "pair_id", "role", "lag_s", "correlation", "n_observed",
                   "effective_duration_s", "coverage", "boundary_warning")
        if self._lag_curve.empty:
            self.lag_table.setRowCount(0)
            self.lag_summary.setText("尚无后端 lag_curve 结果；不会生成示意曲线或假设 p 值。")
            self.lag_view_changed.emit(self._lag_curve.copy(deep=True))
            return
        self.lag_table.setRowCount(len(self._lag_curve))
        for row_index, record in enumerate(self._lag_curve.to_dict("records")):
            for column, key in enumerate(columns):
                self.lag_table.setItem(row_index, column, QTableWidgetItem(self._cell_text(record, key)))
        summary: list[str] = []
        group_columns = [column for column in ("interaction_id", "pair_id", "role") if column in self._lag_curve]
        if "lag_s" in self._lag_curve and "correlation" in self._lag_curve:
            groups = self._lag_curve.groupby(group_columns, dropna=False, sort=False) if group_columns else [("all", self._lag_curve)]
            for key, rows in groups:
                corr = pd.to_numeric(rows["correlation"], errors="coerce")
                valid = rows.loc[corr.notna()]
                if valid.empty:
                    continue
                max_value = pd.to_numeric(valid["correlation"], errors="coerce").max()
                best_rows = valid.loc[pd.to_numeric(valid["correlation"], errors="coerce").eq(max_value)]
                best_lags = pd.to_numeric(best_rows["lag_s"], errors="coerce").tolist()
                zero_rows = valid.loc[pd.to_numeric(valid["lag_s"], errors="coerce").abs().lt(1e-12)]
                r_zero = self._cell_text(zero_rows.iloc[0].to_dict(), "correlation") if not zero_rows.empty else "NA"
                boundary = any(str(value).casefold() in {"true", "1"} for value in best_rows.get("boundary_warning", pd.Series(dtype=object)).tolist())
                prefix = " / ".join(str(x) for x in (key if isinstance(key, tuple) else (key,)))
                summary.append(f"{prefix}: r(0)={r_zero}；最大 r={self._cell_text({'x': max_value}, 'x')}；并列 lag(s)={best_lags}；边界警示={'有' if boundary else '无'}")
        self.lag_summary.setText("\n".join(summary) if summary else f"显示后端返回的 {len(self._lag_curve)} 条 lag 行；相关列没有可用数值。")
        self.lag_table.resizeColumnsToContents()
        self.lag_view_changed.emit(self._lag_curve.copy(deep=True))

    def _render_metric_results(self) -> None:
        columns = ("interaction_id", "pair_id", "metric_id", "value", "unit", "status", "reason_code", "n_observed", "effective_duration_s")
        if self._metric_results.empty:
            self.result_table.setRowCount(0)
            return
        frame = self._metric_results
        if "module" in frame:
            frame = frame.loc[frame["module"].astype(str).eq("group")]
        if "pair_id" in frame:
            frame = frame.loc[frame["pair_id"].notna()]
        self.result_table.setRowCount(len(frame))
        view_columns = ("interaction_id", "pair_id", "metric_id", "value", "unit", "status", "n_observed", "effective_duration_s")
        for row_index, record in enumerate(frame.to_dict("records")):
            for column, key in enumerate(view_columns):
                text = self._cell_text(record, key)
                if key == "status" and record.get("reason_code") not in (None, ""):
                    text += " / " + str(record.get("reason_code"))
                item = QTableWidgetItem(text)
                if key == "status":
                    item.setToolTip(str(record.get("reason") or record.get("reason_code") or ""))
                self.result_table.setItem(row_index, column, item)
        self.result_table.resizeColumnsToContents()

    def _on_design_changed(self, *_: Any) -> None:
        self._refresh_design()
        self._emit_configuration()

    def _emit_scope(self) -> None:
        state = self.scope_state()
        self.scope_changed.emit(state)

    def _emit_configuration(self, *_: Any) -> None:
        try:
            payload = self.configuration_values()
            messages = self.parameter_validation_messages()
            self.evidence_status.setText(self._evidence_summary(payload["metadata"]))
            self.parameter_status.setText(
                "参数检查：" + ("；".join(messages) if messages else "当前值类型与已声明范围一致；最终可用性以真实前检为准。")
            )
        except (ValueError, TypeError) as exc:
            self.evidence_status.setText("群体参数配置无效；请查看群体参数页的定位提示。")
            self._report_parameter_error(str(exc))
            payload = {"group": {}, "metadata": {}}
        self.configuration_changed.emit(payload)

    @staticmethod
    def _evidence_summary(metadata: Mapping[str, Any]) -> str:
        time_ok = metadata.get("time_confirmed") is True
        sync_ok = metadata.get("synchronization", {}).get("confirmed") is True if isinstance(metadata.get("synchronization"), Mapping) else False
        space_ok = metadata.get("cross_subject_spatial_comparable") is True
        pupil_ok = metadata.get("cross_subject_pupil_comparable") is True
        aoi_ok = metadata.get("aoi_mapping_confirmed") is True
        return (
            f"时间基准：{'已确认' if time_ok else '未确认'}；同步：{'已确认' if sync_ok else '未确认'}；"
            f"空间可比：{'已确认' if space_ok else '未确认'}；瞳孔可比：{'已确认' if pupil_ok else '未确认'}；"
            f"AOI 映射：{'已确认' if aoi_ok else '未确认'}。这些是用户声明，不是设备认证。"
        )


# The short alias makes the page easy to add to an existing QStackedWidget.
GroupPage = GroupDesignPage


__all__ = ["GroupDesignPage", "GroupPage"]
