"""Processing controls and views of the public pipeline's actual preflight QC."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from math import isfinite
import numbers
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pyeyedata import ProcessingConfig
from pyeyedata.gui.bridge import PipelineOutcome
from pyeyedata.gui.diagnostic_timeline import (
    diagnostic_rows_match,
    resolve_diagnostic_location,
    validate_outcome_revision,
)
from pyeyedata.gui.models import DatasetRevision
from pyeyedata.gui.signal_timeline import TimelineWidget
from pyeyedata.gui.widgets import DataFrameTableModel, DataFrameTableView


class QualityPage(QWidget):
    """Real ProcessingConfig controls and rendering of an actual pipeline preflight."""

    configuration_changed = Signal(object)

    _CONTROLLED_PROCESSING_FIELDS = frozenset({
        "order_policy",
        "duplicate_policy",
        "support_gap_factor",
        "support_gap_max_s",
        "interpolate_gaze",
        "interpolate_pupil",
        "pupil_interpolation_method",
        "pupil_speed_mad_enabled",
        "pupil_speed_mad_n",
        "pupil_blink_padding_enabled",
        "pupil_blink_padding_before_s",
        "pupil_blink_padding_after_s",
        "gaze_max_gap_s",
        "pupil_max_gap_s",
        "max_missing_samples",
        "lowpass_cutoff_hz",
        "lowpass_order",
        "pupil_baseline_window_s",
        "baseline_min_coverage",
        "baseline_min_length_s",
    })

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        defaults = ProcessingConfig()
        self._uncontrolled_processing_values = self._uncontrolled_values(defaults)
        self._dataset_revision: DatasetRevision | None = None
        self._dataset = None
        self._context_tables: dict[str, pd.DataFrame] = {}
        self._context_manifest: list[dict[str, Any]] = []
        self._last_outcome: Any = None
        self._processing: Any = None
        self._parameter_error_name: str | None = None
        self.status = QLabel("质量统计和图表将在公共 pipeline 前检完成后显示；这里不会本地重算科学结果。", self)
        self.status.setWordWrap(True)
        self.parameter_error_button = QPushButton("定位参数", self)
        self.parameter_error_button.setEnabled(False)
        self.parameter_error_button.clicked.connect(self._focus_parameter_error)
        self._build_controls(defaults)
        self._build_results()
        self._connect_controls()
        layout = QVBoxLayout(self)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.parameter_error_button)
        layout.addLayout(status_row)
        layout.addWidget(self.controls_box)
        layout.addWidget(self.tabs, 1)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.controls_box.setMinimumHeight(self.controls_box.minimumSizeHint().height())
        self.tabs.setMinimumHeight(360)
        self._emit_configuration()

    def _build_controls(self, defaults: ProcessingConfig) -> None:
        self.order_policy = QComboBox(self)
        self.order_policy.addItem("要求时间单调递增", "require_monotonic")
        self.order_policy.addItem("按时间稳定排序", "sort_by_time")
        self.duplicate_policy = QComboBox(self)
        self.duplicate_policy.addItem("拒绝重复时间", "reject")
        self.duplicate_policy.addItem("保留并标记", "retain_with_flag")
        self.gap_factor = self._double(1.01, 100.0, defaults.support_gap_factor, 2)
        self.use_max_gap = QCheckBox("另设最大支持间隔", self)
        self.max_gap = self._double(0.000001, 1000000.0, 0.5, 6)
        self.max_gap.setEnabled(False)
        self.interpolate_gaze = QCheckBox("插值 gaze 短缺口", self)
        self.interpolate_gaze.setChecked(defaults.interpolate_gaze)
        self.gaze_gap = self._double(0.000001, 1000000.0, defaults.gaze_max_gap_s, 6)
        self.gaze_gap.setEnabled(False)
        self.interpolate_pupil = QCheckBox("插值 pupil 短缺口", self)
        self.interpolate_pupil.setChecked(defaults.interpolate_pupil)
        self.pupil_gap = self._double(0.000001, 1000000.0, defaults.pupil_max_gap_s, 6)
        self.pupil_gap.setEnabled(False)
        self.pupil_preset = QComboBox(self)
        self.pupil_preset.addItem("关闭瞳孔插值 / MAD / padding（保留其他设置）", "raw")
        self.pupil_preset.addItem("保守短缺口线性插值", "short_linear")
        self.pupil_preset.addItem("研究方法：PCHIP + MAD + padding", "research")
        self.apply_pupil_preset = QPushButton("应用所选预设", self)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.pupil_preset, 1)
        preset_row.addWidget(self.apply_pupil_preset)
        preset_widget = QWidget(self)
        preset_widget.setLayout(preset_row)
        self.pupil_advanced_toggle = QCheckBox("显示高级瞳孔预处理参数", self)
        self.pupil_advanced_summary = QLabel("", self)
        self.pupil_advanced_summary.setWordWrap(True)
        self.pupil_advanced_summary.setVisible(False)
        self.pupil_advanced_box = QGroupBox("高级瞳孔预处理参数", self)
        advanced_form = QFormLayout(self.pupil_advanced_box)
        self.pupil_interpolation_method = QComboBox(self.pupil_advanced_box)
        self.pupil_interpolation_method.addItem("线性（默认）", "linear")
        self.pupil_interpolation_method.addItem("PCHIP（形状保持）", "pchip")
        self.pupil_speed_mad_enabled = QCheckBox("启用 pupil 速度 MAD 拒绝", self.pupil_advanced_box)
        self.pupil_speed_mad_enabled.setChecked(defaults.pupil_speed_mad_enabled)
        self.pupil_speed_mad_n = QSpinBox(self.pupil_advanced_box)
        self.pupil_speed_mad_n.setRange(1, 1000000)
        self.pupil_speed_mad_n.setValue(defaults.pupil_speed_mad_n)
        self.pupil_blink_padding_enabled = QCheckBox("启用 pupil 眨眼 / 丢失边界遮罩", self.pupil_advanced_box)
        self.pupil_blink_padding_enabled.setChecked(defaults.pupil_blink_padding_enabled)
        self.pupil_blink_padding_before = self._double(0.0, 1000000.0, defaults.pupil_blink_padding_before_s, 6)
        self.pupil_blink_padding_after = self._double(0.0, 1000000.0, defaults.pupil_blink_padding_after_s, 6)
        padding_row = QHBoxLayout()
        padding_row.addWidget(QLabel("之前秒", self.pupil_advanced_box))
        padding_row.addWidget(self.pupil_blink_padding_before)
        padding_row.addWidget(QLabel("之后秒", self.pupil_advanced_box))
        padding_row.addWidget(self.pupil_blink_padding_after)
        padding_row.addStretch(1)
        padding_widget = QWidget(self.pupil_advanced_box)
        padding_widget.setLayout(padding_row)
        self.pupil_preprocessing_note = QLabel(
            "速度 MAD 只检查 pupil，不改 gaze。启用前请核对采集端没有在线 / 保存滤波；硬件滤波可能抹平速度尖峰。"
            "边界遮罩只影响明确缺失 / 无效样本和逐眼眨眼标记附近的分析值。线性 / PCHIP 插值继续遵守现有最大缺口秒数与缺失采样点上限；"
            "缺少足够 PCHIP 锚点时保留缺失值。请检查逐眼质量图和审计记录。",
            self.pupil_advanced_box,
        )
        self.pupil_preprocessing_note.setWordWrap(True)
        self.pupil_preprocessing_note.setToolTip(
            "eyeris 方法说明提醒，EyeLink 在线或保存样本滤波会削弱一步差分速度 MAD 的异常检测前提；"
            "如果记录已经滤波，请考虑保持 MAD 关闭并检查原始与处理后轨迹。"
        )
        self.pupil_speed_mad_enabled.setToolTip(
            "在连续、有限、明确来源有效的样本上计算 pupil 速度；阈值为 median(speed) + n×MAD。默认关闭。"
        )
        self.pupil_speed_mad_n.setToolTip(
            "比例常数 n；2025 iMotion 示范研究使用 20。MAD=0 时按字面阈值 median(speed) 执行，需人工复核。"
        )
        self.pupil_blink_padding_enabled.setToolTip(
            "逐眼缺失 / 无效样本或明确眨眼标记前后按秒遮罩；不扩大现有最大缺口或采样点上限。"
        )
        self.pupil_interpolation_method.setToolTip(
            "PCHIP 要求缺口两侧各有两个同一连续支持段内的有效锚点；不足时保留缺失，不改用线性。"
        )
        advanced_form.addRow("瞳孔插值方法", self.pupil_interpolation_method)
        advanced_form.addRow(self.pupil_speed_mad_enabled, self.pupil_speed_mad_n)
        advanced_form.addRow(self.pupil_blink_padding_enabled, padding_widget)
        advanced_form.addRow(self.pupil_preprocessing_note)
        self.pupil_advanced_box.setVisible(False)
        self.max_missing = QSpinBox(self)
        self.max_missing.setRange(1, 100000)
        self.max_missing.setValue(defaults.max_missing_samples)
        self.use_lowpass = QCheckBox("启用低通滤波", self)
        self.lowpass_cutoff = self._double(0.000001, 1000000.0, 1.0, 6)
        self.lowpass_cutoff.setEnabled(False)
        self.lowpass_order = QSpinBox(self)
        self.lowpass_order.setRange(1, 20)
        self.lowpass_order.setValue(defaults.lowpass_order)
        self.lowpass_order.setEnabled(False)
        self.use_baseline = QCheckBox("启用瞳孔基线校正", self)
        self.baseline_start = self._double(-1000000.0, 1000000.0, 0.0, 6)
        self.baseline_end = self._double(-1000000.0, 1000000.0, defaults.baseline_min_length_s, 6)
        self.baseline_start.setEnabled(False)
        self.baseline_end.setEnabled(False)
        self.baseline_coverage = self._double(0.01, 1.0, defaults.baseline_min_coverage, 2)
        self.baseline_coverage.setEnabled(False)
        self.baseline_length = self._double(0.0, 1000000.0, defaults.baseline_min_length_s, 6)
        self.baseline_length.setEnabled(False)

        box = QGroupBox("共享预处理配置（按后端 ProcessingConfig 执行）", self)
        form = QFormLayout(box)
        form.addRow("常用瞳孔预设（须显式应用）", preset_widget)
        form.addRow(self.pupil_advanced_toggle)
        form.addRow(self.pupil_advanced_summary)
        form.addRow(self.pupil_advanced_box)
        form.addRow("时间顺序", self.order_policy)
        form.addRow("重复时间", self.duplicate_policy)
        form.addRow("支持间隔倍数（> 1）", self.gap_factor)
        form.addRow(self.use_max_gap, self._with_unit(self.max_gap, "秒；> 0"))
        form.addRow(self.interpolate_gaze, self._with_unit(self.gaze_gap, "秒；> 0"))
        form.addRow(self.interpolate_pupil, self._with_unit(self.pupil_gap, "秒；> 0"))
        form.addRow("连续缺失上限（采样点；整数 ≥ 1）", self.max_missing)
        form.addRow(self.use_lowpass, self._with_unit(self.lowpass_cutoff, "Hz；> 0"))
        form.addRow("低通阶数（整数 ≥ 1）", self.lowpass_order)
        baseline_range = QHBoxLayout()
        baseline_range.addWidget(QLabel("起点秒", self))
        baseline_range.addWidget(self.baseline_start)
        baseline_range.addWidget(QLabel("终点秒", self))
        baseline_range.addWidget(self.baseline_end)
        baseline_range.addStretch(1)
        form.addRow(self.use_baseline, baseline_range)
        form.addRow("基线最低覆盖率（0, 1]", self.baseline_coverage)
        form.addRow("基线最短时长（秒；0 不额外限制）", self.baseline_length)
        note = QLabel(
            "默认 pupil 插值、速度 MAD、边界遮罩、低通和基线校正均关闭；插值方法默认为线性。未开放未验证插值边界绕过；"
            "前检由现有 TaskManager 后台调用公共 preflight，"
            "滤波 Nyquist、缺口及条件可用性以实际后端返回为准。基线最短时长设为 0 时不额外限制正时长基线窗；"
            "基线窗口自身仍必须非空且满足最低覆盖率。源信号始终只读。",
            box,
        )
        note.setWordWrap(True)
        form.addRow(note)
        self.controls_box = box

    @staticmethod
    def _double(minimum: float, maximum: float, value: float, decimals: int) -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setRange(minimum, maximum)
        control.setDecimals(decimals)
        control.setValue(value)
        return control

    @staticmethod
    def _with_unit(control: QWidget, unit: str) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(control, 1)
        row.addWidget(QLabel(unit, control.parentWidget()))
        container = QWidget(control.parentWidget())
        container.setLayout(row)
        return container

    def _report_parameter_error(self, message: str) -> None:
        text = str(message)
        candidates = (
            "order_policy", "duplicate_policy", "support_gap_factor", "support_gap_max_s",
            "interpolate_gaze", "interpolate_pupil", "pupil_interpolation_method",
            "pupil_speed_mad_enabled", "pupil_speed_mad_n", "pupil_blink_padding_enabled",
            "pupil_blink_padding_before_s", "pupil_blink_padding_after_s", "gaze_max_gap_s", "pupil_max_gap_s",
            "max_missing_samples", "lowpass_cutoff_hz", "lowpass_order",
            "pupil_baseline_window_s", "baseline_min_coverage", "baseline_min_length_s",
        )
        name = next((item for item in candidates if item in text), None)
        self._parameter_error_name = name
        self.status.setText(f"处理参数无效：{text}。请按字段单位和范围修正。")
        self.parameter_error_button.setEnabled(name is not None)

    def _clear_parameter_error(self) -> None:
        self._parameter_error_name = None
        self.parameter_error_button.setEnabled(False)

    def _focus_parameter_error(self) -> None:
        name = self._parameter_error_name
        controls = {
            "order_policy": self.order_policy,
            "duplicate_policy": self.duplicate_policy,
            "support_gap_factor": self.gap_factor,
            "support_gap_max_s": self.max_gap,
            "interpolate_gaze": self.interpolate_gaze,
            "interpolate_pupil": self.interpolate_pupil,
            "pupil_interpolation_method": self.pupil_interpolation_method,
            "pupil_speed_mad_enabled": self.pupil_speed_mad_enabled,
            "pupil_speed_mad_n": self.pupil_speed_mad_n,
            "pupil_blink_padding_enabled": self.pupil_blink_padding_enabled,
            "pupil_blink_padding_before_s": self.pupil_blink_padding_before,
            "pupil_blink_padding_after_s": self.pupil_blink_padding_after,
            "gaze_max_gap_s": self.gaze_gap,
            "pupil_max_gap_s": self.pupil_gap,
            "max_missing_samples": self.max_missing,
            "lowpass_cutoff_hz": self.lowpass_cutoff,
            "lowpass_order": self.lowpass_order,
            "pupil_baseline_window_s": self.baseline_start,
            "baseline_min_coverage": self.baseline_coverage,
            "baseline_min_length_s": self.baseline_length,
        }
        control = controls.get(name)
        if control is not None:
            control.setFocus(Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _validated_processing_config(value: Mapping[str, Any] | ProcessingConfig | None) -> ProcessingConfig:
        if value is None:
            config = ProcessingConfig()
        elif isinstance(value, ProcessingConfig):
            config = value
        elif isinstance(value, Mapping):
            supplied = dict(value)
            baseline = supplied.get("pupil_baseline_window_s")
            if isinstance(baseline, list):
                supplied["pupil_baseline_window_s"] = tuple(baseline)
            config = ProcessingConfig(**supplied)
        else:
            raise TypeError("processing configuration must be a mapping, ProcessingConfig, or None")

        for name in (
            "interpolate_gaze",
            "interpolate_pupil",
            "pupil_speed_mad_enabled",
            "pupil_blink_padding_enabled",
            "allow_unverified_interpolation_bounds",
        ):
            if type(getattr(config, name)) is not bool:
                raise TypeError(f"processing.{name} must be bool")
        for name in ("max_missing_samples", "lowpass_order", "pupil_speed_mad_n"):
            item = getattr(config, name)
            if isinstance(item, (bool, np.bool_)) or not isinstance(item, numbers.Integral):
                raise TypeError(f"processing.{name} must be an integer")
        for name in (
            "support_gap_factor",
            "gaze_max_gap_s",
            "pupil_max_gap_s",
            "baseline_min_coverage",
            "baseline_min_length_s",
            "pupil_blink_padding_before_s",
            "pupil_blink_padding_after_s",
        ):
            item = getattr(config, name)
            if (isinstance(item, (bool, np.bool_)) or not isinstance(item, numbers.Real)
                    or not isfinite(float(item))):
                raise TypeError(f"processing.{name} must be a finite real number")
        for name in ("support_gap_max_s", "lowpass_cutoff_hz"):
            item = getattr(config, name)
            if item is not None and (
                isinstance(item, (bool, np.bool_)) or not isinstance(item, numbers.Real)
                or not isfinite(float(item))
            ):
                raise TypeError(f"processing.{name} must be None or a finite number")
        baseline = config.pupil_baseline_window_s
        if baseline is not None:
            if not isinstance(baseline, tuple) or len(baseline) != 2:
                raise TypeError("processing.pupil_baseline_window_s must be a two-value tuple or None")
            if any(isinstance(item, (bool, np.bool_)) or not isinstance(item, numbers.Real) or not isfinite(float(item))
                   for item in baseline):
                raise TypeError("processing.pupil_baseline_window_s values must be finite numbers")
        return config

    @classmethod
    def _uncontrolled_values(cls, config: ProcessingConfig) -> dict[str, Any]:
        return {
            name: deepcopy(getattr(config, name))
            for name in config.__dataclass_fields__
            if name not in cls._CONTROLLED_PROCESSING_FIELDS
        }

    def set_processing_config(self, value: Mapping[str, Any] | ProcessingConfig | None) -> None:
        """Restore a typed backend configuration into the visible controls without emitting edits."""
        try:
            config = self._validated_processing_config(value)
        except (TypeError, ValueError, OverflowError) as exc:
            self._report_parameter_error(str(exc))
            raise
        defaults = ProcessingConfig()
        combo_values = (
            (self.order_policy, config.order_policy, "order_policy"),
            (self.duplicate_policy, config.duplicate_policy, "duplicate_policy"),
            (self.pupil_interpolation_method, config.pupil_interpolation_method, "pupil_interpolation_method"),
        )
        for control, item, name in combo_values:
            if control.findData(item) < 0:
                message = f"processing.{name}={item!r} is not supported by this page"
                self._report_parameter_error(message)
                raise ValueError(message)

        doubles = (
            (self.gap_factor, config.support_gap_factor, "support_gap_factor", 2),
            (self.max_gap, config.support_gap_max_s if config.support_gap_max_s is not None else defaults.support_gap_max_s or 0.5, "support_gap_max_s", 6),
            (self.gaze_gap, config.gaze_max_gap_s, "gaze_max_gap_s", 6),
            (self.pupil_gap, config.pupil_max_gap_s, "pupil_max_gap_s", 6),
            (self.pupil_blink_padding_before, config.pupil_blink_padding_before_s, "pupil_blink_padding_before_s", 3),
            (self.pupil_blink_padding_after, config.pupil_blink_padding_after_s, "pupil_blink_padding_after_s", 3),
            (self.lowpass_cutoff, config.lowpass_cutoff_hz if config.lowpass_cutoff_hz is not None else 1.0, "lowpass_cutoff_hz", 6),
            (self.baseline_start, config.pupil_baseline_window_s[0] if config.pupil_baseline_window_s is not None else 0.0, "pupil_baseline_window_s[0]", 6),
            (self.baseline_end, config.pupil_baseline_window_s[1] if config.pupil_baseline_window_s is not None else defaults.baseline_min_length_s, "pupil_baseline_window_s[1]", 6),
            (self.baseline_coverage, config.baseline_min_coverage, "baseline_min_coverage", 2),
            (self.baseline_length, config.baseline_min_length_s, "baseline_min_length_s", 6),
        )
        # Validate all representability/range limits before touching the current controls.
        for control, item, name, base_decimals in doubles:
            try:
                number = float(item)
            except (TypeError, ValueError, OverflowError) as exc:
                message = f"processing.{name}={item!r} cannot be represented by this control"
                self._report_parameter_error(message)
                raise ValueError(message) from exc
            if not isfinite(number) or item != number:
                message = f"processing.{name}={item!r} cannot be represented exactly by this control"
                self._report_parameter_error(message)
                raise ValueError(message)
            decimals = min(323, max(base_decimals, -Decimal(str(number)).as_tuple().exponent))
            if number < control.minimum() or number > control.maximum():
                message = f"processing.{name}={item!r} is outside the value range supported by this control"
                self._report_parameter_error(message)
                raise ValueError(message)
            if round(number, decimals) != number:
                message = f"processing.{name} cannot be represented exactly by this control"
                self._report_parameter_error(message)
                raise ValueError(message)

        integers = (
            (self.max_missing, config.max_missing_samples, "max_missing_samples"),
            (self.lowpass_order, config.lowpass_order, "lowpass_order"),
            (self.pupil_speed_mad_n, config.pupil_speed_mad_n, "pupil_speed_mad_n"),
        )
        for control, item, name in integers:
            if item < control.minimum() or item > control.maximum():
                message = (
                    f"processing.{name}={item!r} is outside the value range supported by this control "
                    f"[{control.minimum()}, {control.maximum()}]"
                )
                self._report_parameter_error(message)
                raise ValueError(message)
        self._clear_parameter_error()

        controls = (
            self.order_policy, self.duplicate_policy, self.gap_factor, self.use_max_gap,
            self.max_gap, self.interpolate_gaze, self.gaze_gap, self.interpolate_pupil,
            self.pupil_gap, self.pupil_interpolation_method, self.pupil_speed_mad_enabled,
            self.pupil_speed_mad_n, self.pupil_blink_padding_enabled,
            self.pupil_blink_padding_before, self.pupil_blink_padding_after,
            self.max_missing, self.use_lowpass, self.lowpass_cutoff,
            self.lowpass_order, self.use_baseline, self.baseline_start, self.baseline_end,
            self.baseline_coverage, self.baseline_length,
        )
        signal_states = [(control, control.blockSignals(True)) for control in controls]
        try:
            self.order_policy.setCurrentIndex(self.order_policy.findData(config.order_policy))
            self.duplicate_policy.setCurrentIndex(self.duplicate_policy.findData(config.duplicate_policy))
            self.pupil_interpolation_method.setCurrentIndex(
                self.pupil_interpolation_method.findData(config.pupil_interpolation_method)
            )
            for control, item, _name, base_decimals in doubles:
                number = float(item)
                control.setDecimals(max(base_decimals, -Decimal(str(number)).as_tuple().exponent))
                control.setValue(number)
            self.use_max_gap.setChecked(config.support_gap_max_s is not None)
            self.interpolate_gaze.setChecked(config.interpolate_gaze)
            self.interpolate_pupil.setChecked(config.interpolate_pupil)
            self.pupil_speed_mad_enabled.setChecked(config.pupil_speed_mad_enabled)
            self.pupil_speed_mad_n.setValue(config.pupil_speed_mad_n)
            self.pupil_blink_padding_enabled.setChecked(config.pupil_blink_padding_enabled)
            self.max_missing.setValue(config.max_missing_samples)
            self.use_lowpass.setChecked(config.lowpass_cutoff_hz is not None)
            self.lowpass_order.setValue(config.lowpass_order)
            self.use_baseline.setChecked(config.pupil_baseline_window_s is not None)
            self._uncontrolled_processing_values = self._uncontrolled_values(config)
            self._sync_control_enabled_states()
        finally:
            for control, previous in signal_states:
                control.blockSignals(previous)

    def _sync_control_enabled_states(self) -> None:
        self.max_gap.setEnabled(self.use_max_gap.isChecked())
        self.gaze_gap.setEnabled(self.interpolate_gaze.isChecked())
        self.pupil_gap.setEnabled(self.interpolate_pupil.isChecked())
        self.pupil_interpolation_method.setEnabled(self.interpolate_pupil.isChecked())
        self.pupil_speed_mad_n.setEnabled(self.pupil_speed_mad_enabled.isChecked())
        padding_enabled = self.pupil_blink_padding_enabled.isChecked()
        self.pupil_blink_padding_before.setEnabled(padding_enabled)
        self.pupil_blink_padding_after.setEnabled(padding_enabled)
        self.lowpass_cutoff.setEnabled(self.use_lowpass.isChecked())
        self.lowpass_order.setEnabled(self.use_lowpass.isChecked())
        enabled = self.use_baseline.isChecked()
        for control in (self.baseline_start, self.baseline_end, self.baseline_coverage, self.baseline_length):
            control.setEnabled(enabled)
        self.pupil_advanced_box.setVisible(self.pupil_advanced_toggle.isChecked())
        self._refresh_pupil_advanced_summary()

    def _build_results(self) -> None:
        self.timeline = TimelineWidget(self)
        self.timeline.setMinimumHeight(430)
        # Keep the QualityPage stream selector API while using TimelineWidget's
        # complete stable-key selector instead of a coarser subject/session filter.
        self.stream_selector = self.timeline.stream_selector
        self.signal_selector = self.timeline.signal_selector
        self.timeline_status = QLabel("导入后可查看当前修订的原始信号；处理曲线和事件需匹配的后端结果。", self)
        self.timeline_status.setWordWrap(True)
        self.range_label = QLabel("尚未选择时间区间。", self)
        self.locator_label = QLabel("诊断和事件定位会核对来源、分区、时间基与秒范围。", self)
        self.locator_label.setWordWrap(True)
        self.quality_model = DataFrameTableModel(parent=self)
        self.quality_view = DataFrameTableView(self)
        self.quality_view.setModel(self.quality_model)
        self.coverage_model = DataFrameTableModel(parent=self)
        self.coverage_view = DataFrameTableView(self)
        self.coverage_view.setModel(self.coverage_model)
        self.event_model = DataFrameTableModel(parent=self)
        self.event_view = DataFrameTableView(self)
        self.event_view.setModel(self.event_model)
        self.diagnostic_model = DataFrameTableModel(parent=self)
        self.diagnostic_view = DataFrameTableView(self)
        self.diagnostic_view.setModel(self.diagnostic_model)
        self._diagnostic_records = pd.DataFrame()
        self.locate_button = QPushButton("定位选中记录", self)
        self.locate_button.setEnabled(False)
        self.context_model = DataFrameTableModel(parent=self)
        self.context_view = DataFrameTableView(self)
        self.context_view.setModel(self.context_model)
        self.overlap_model = DataFrameTableModel(parent=self)
        self.overlap_view = DataFrameTableView(self)
        self.overlap_view.setModel(self.overlap_model)

        overview = QWidget(self)
        overview_layout = QVBoxLayout(overview)
        overview_layout.addWidget(QLabel("每个 source/eye stream 的原始有效、插值、缺失与支持时长（来自后端 quality 表）", overview))
        overview_layout.addWidget(self.quality_view, 1)
        overview_layout.addWidget(self.timeline_status)
        overview_layout.addWidget(self.range_label)
        overview_layout.addWidget(self.timeline, 3)

        coverage = QWidget(self)
        coverage_layout = QVBoxLayout(coverage)
        coverage_layout.addWidget(QLabel("段覆盖以真实 analysis_memberships / support_observation_ids 为分母；不把重叠标签当独立观察。", coverage))
        coverage_layout.addWidget(self.coverage_view, 1)
        coverage_layout.addWidget(QLabel("实际 AOI / Segment 重叠审计", coverage))
        coverage_layout.addWidget(self.overlap_view, 1)

        events = QWidget(self)
        events_layout = QVBoxLayout(events)
        events_layout.addWidget(QLabel("事件覆盖来自真实 event_interval_audit 与 events；前检不会计算指标。", events))
        events_layout.addWidget(self.event_view, 1)
        events_layout.addWidget(QLabel(
            "实际事件与区间审核记录；双击一行或选中后定位。原因、状态、来源、分区和时间字段保留原值。", events
        ))
        events_layout.addWidget(self.diagnostic_view, 2)
        location_row = QHBoxLayout()
        location_row.addWidget(self.locator_label, 1)
        location_row.addWidget(self.locate_button)
        events_layout.addLayout(location_row)

        context = QWidget(self)
        context_layout = QVBoxLayout(context)
        self.context_note = QLabel("参考轨迹、目标区间与多人共同覆盖只在相应来源 / 时间映射存在时展示。", context)
        self.context_note.setWordWrap(True)
        context_layout.addWidget(self.context_note)
        context_layout.addWidget(self.context_view, 1)

        self.tabs = QTabWidget(self)
        overview_scroll = QScrollArea(self)
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setWidget(overview)
        self.tabs.addTab(overview_scroll, "流质量与预览图")
        self.tabs.addTab(coverage, "Segment / AOI 覆盖")
        self.tabs.addTab(events, "事件覆盖")
        self.tabs.addTab(context, "轨迹 / 多人共同覆盖")

    def _connect_controls(self) -> None:
        for control in (
            self.order_policy, self.duplicate_policy, self.gap_factor, self.use_max_gap, self.max_gap,
            self.interpolate_gaze, self.gaze_gap, self.interpolate_pupil, self.pupil_gap,
            self.pupil_interpolation_method, self.pupil_speed_mad_enabled, self.pupil_speed_mad_n,
            self.pupil_blink_padding_enabled, self.pupil_blink_padding_before,
            self.pupil_blink_padding_after, self.max_missing,
            self.use_lowpass, self.lowpass_cutoff, self.lowpass_order, self.use_baseline,
            self.baseline_start, self.baseline_end, self.baseline_coverage, self.baseline_length,
        ):
            if isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._emit_configuration)
            elif isinstance(control, QCheckBox):
                control.toggled.connect(self._control_toggled)
            elif isinstance(control, QDoubleSpinBox):
                control.valueChanged.connect(self._emit_configuration)
            else:
                control.valueChanged.connect(self._emit_configuration)
        self.pupil_advanced_toggle.toggled.connect(self._toggle_pupil_advanced)
        self.apply_pupil_preset.clicked.connect(self._apply_pupil_preset)
        self.timeline.intervalSelected.connect(self._on_interval_selected)
        self.timeline.timeRangeChanged.connect(self._on_time_range_changed)
        self.timeline.diagnosticSelected.connect(self._on_timeline_diagnostic)
        self.diagnostic_view.doubleClicked.connect(self._on_diagnostic_double_clicked)
        self.locate_button.clicked.connect(self._locate_selected_diagnostic)
        self.diagnostic_view.selectionModel().selectionChanged.connect(self._on_diagnostic_selection_changed)

    def _control_toggled(self, *_: Any) -> None:
        self._sync_control_enabled_states()
        self._emit_configuration()

    def _toggle_pupil_advanced(self, checked: bool) -> None:
        self.pupil_advanced_box.setVisible(checked)
        self._refresh_pupil_advanced_summary()
        controls_layout = self.controls_box.layout()
        if controls_layout is not None:
            controls_layout.activate()
        self.controls_box.setMinimumHeight(self.controls_box.minimumSizeHint().height())
        self.controls_box.adjustSize()

    def _refresh_pupil_advanced_summary(self) -> None:
        defaults = ProcessingConfig()
        custom = (
            self.pupil_speed_mad_enabled.isChecked()
            or self.pupil_speed_mad_n.value() != defaults.pupil_speed_mad_n
            or self.pupil_blink_padding_enabled.isChecked()
            or self.pupil_blink_padding_before.value() != defaults.pupil_blink_padding_before_s
            or self.pupil_blink_padding_after.value() != defaults.pupil_blink_padding_after_s
            or self.pupil_interpolation_method.currentData() != defaults.pupil_interpolation_method
        )
        if custom:
            parts = []
            if self.pupil_speed_mad_enabled.isChecked():
                parts.append(f"速度 MAD n={self.pupil_speed_mad_n.value()}")
            if self.pupil_blink_padding_enabled.isChecked():
                parts.append(
                    f"边界遮罩 −{self.pupil_blink_padding_before.value():g}/"
                    f"+{self.pupil_blink_padding_after.value():g} 秒"
                )
            if self.pupil_interpolation_method.currentData() != "linear":
                parts.append(f"插值={self.pupil_interpolation_method.currentText()}")
            if self.pupil_speed_mad_n.value() != defaults.pupil_speed_mad_n and not self.pupil_speed_mad_enabled.isChecked():
                parts.append(f"保存的 MAD n={self.pupil_speed_mad_n.value()}（当前关闭）")
            if (self.pupil_blink_padding_before.value() != defaults.pupil_blink_padding_before_s
                    or self.pupil_blink_padding_after.value() != defaults.pupil_blink_padding_after_s):
                if not self.pupil_blink_padding_enabled.isChecked():
                    parts.append("保存了边界遮罩秒数（当前关闭）")
            if self.pupil_interpolation_method.currentData() != "linear" and not self.interpolate_pupil.isChecked():
                parts.append("插值方法已保存（当前关闭 pupil 插值）")
            self.pupil_advanced_summary.setText("高级瞳孔设置：" + "；".join(parts) + "。勾选上方选项可查看参数。")
            self.pupil_advanced_summary.setVisible(not self.pupil_advanced_toggle.isChecked())
        else:
            self.pupil_advanced_summary.setText("")
            self.pupil_advanced_summary.setVisible(False)

    def _apply_pupil_preset(self) -> None:
        values = self.processing_config()
        selected = self.pupil_preset.currentData()
        if selected == "raw":
            defaults = ProcessingConfig()
            values.update(
                interpolate_pupil=False,
                pupil_interpolation_method="linear",
                pupil_speed_mad_enabled=False,
                pupil_speed_mad_n=defaults.pupil_speed_mad_n,
                pupil_blink_padding_enabled=False,
                pupil_blink_padding_before_s=defaults.pupil_blink_padding_before_s,
                pupil_blink_padding_after_s=defaults.pupil_blink_padding_after_s,
            )
            label = "关闭瞳孔插值 / MAD / padding（其他设置保留）"
        elif selected == "short_linear":
            defaults = ProcessingConfig()
            values.update(
                interpolate_pupil=True,
                pupil_interpolation_method="linear",
                pupil_max_gap_s=min(values["pupil_max_gap_s"], defaults.pupil_max_gap_s),
                max_missing_samples=min(values["max_missing_samples"], defaults.max_missing_samples),
                pupil_speed_mad_enabled=False,
                pupil_speed_mad_n=defaults.pupil_speed_mad_n,
                pupil_blink_padding_enabled=False,
                pupil_blink_padding_before_s=defaults.pupil_blink_padding_before_s,
                pupil_blink_padding_after_s=defaults.pupil_blink_padding_after_s,
            )
            label = "保守短缺口线性插值"
        else:
            defaults = ProcessingConfig()
            values.update(
                interpolate_pupil=True,
                pupil_interpolation_method="pchip",
                pupil_speed_mad_enabled=True,
                pupil_speed_mad_n=20,
                pupil_blink_padding_enabled=True,
                pupil_blink_padding_before_s=defaults.pupil_blink_padding_before_s,
                pupil_blink_padding_after_s=defaults.pupil_blink_padding_after_s,
            )
            label = "研究方法：PCHIP + MAD + padding"
        self.set_processing_config(values)
        if selected == "research":
            self.pupil_advanced_toggle.setChecked(True)
        self._emit_configuration()
        self.status.setText(
            f"已显式应用“{label}”；现有最大缺口秒数与连续缺失采样点上限仍生效。"
            "请复核逐眼质量图、掩码计数及来源有效性。"
        )

    def processing_config(self) -> dict[str, Any]:
        baseline = (self.baseline_start.value(), self.baseline_end.value()) if self.use_baseline.isChecked() else None
        value: dict[str, Any] = {
            "order_policy": self.order_policy.currentData(),
            "duplicate_policy": self.duplicate_policy.currentData(),
            "support_gap_factor": self.gap_factor.value(),
            "support_gap_max_s": self.max_gap.value() if self.use_max_gap.isChecked() else None,
            "interpolate_gaze": self.interpolate_gaze.isChecked(),
            "interpolate_pupil": self.interpolate_pupil.isChecked(),
            "gaze_max_gap_s": self.gaze_gap.value(),
            "pupil_max_gap_s": self.pupil_gap.value(),
            "pupil_interpolation_method": self.pupil_interpolation_method.currentData(),
            "pupil_speed_mad_enabled": self.pupil_speed_mad_enabled.isChecked(),
            "pupil_speed_mad_n": self.pupil_speed_mad_n.value(),
            "pupil_blink_padding_enabled": self.pupil_blink_padding_enabled.isChecked(),
            "pupil_blink_padding_before_s": self.pupil_blink_padding_before.value(),
            "pupil_blink_padding_after_s": self.pupil_blink_padding_after.value(),
            "max_missing_samples": self.max_missing.value(),
            "allow_unverified_interpolation_bounds": False,
            "lowpass_cutoff_hz": self.lowpass_cutoff.value() if self.use_lowpass.isChecked() else None,
            "lowpass_order": self.lowpass_order.value(),
            "pupil_baseline_window_s": baseline,
            "baseline_min_coverage": self.baseline_coverage.value(),
            "baseline_min_length_s": self.baseline_length.value(),
        }
        value.update(deepcopy(self._uncontrolled_processing_values))
        # Use the same public constructor as PipelineBridge so invalid settings never look confirmed.
        ProcessingConfig(**value)
        return value

    def _emit_configuration(self, *_: Any) -> None:
        try:
            self.configuration_changed.emit(self.processing_config())
            self._refresh_pupil_advanced_summary()
            self._clear_parameter_error()
            self.status.setText("处理配置已更新；运行前检查会在现有任务队列中真实执行。")
        except Exception as exc:
            self.configuration_changed.emit({})
            self._report_parameter_error(str(exc))

    def set_dataset(self, revision: DatasetRevision | None) -> None:
        self._dataset_revision = revision
        self._dataset = revision.dataset_copy() if revision is not None else None
        self._last_outcome = None
        self._processing = None
        for model in (self.quality_model, self.coverage_model, self.event_model, self.overlap_model):
            model.set_frame(pd.DataFrame())
        self._set_diagnostic_records(pd.DataFrame(), pd.DataFrame())
        self.locator_label.setText("诊断和事件定位会核对来源、分区、时间基与秒范围。")
        self.range_label.setText("尚未选择时间区间。")
        self.status.setText("数据修订已更改；旧处理结果和时间选择已清除，等待当前修订的后端结果。")
        self._render_context()
        self._refresh_timeline(preserve_stream=False)

    def set_context_tables(
        self,
        tables: Mapping[str, pd.DataFrame] | None,
        source_manifest: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        self._context_tables = {
            str(name): frame.copy(deep=True)
            for name, frame in (tables or {}).items()
            if isinstance(frame, pd.DataFrame)
        }
        self._context_manifest = [dict(item) for item in (source_manifest or []) if isinstance(item, Mapping)]
        self._render_context()

    def set_outcome(self, outcome: PipelineOutcome | Any) -> bool:
        """Render only a result whose source samples match the current revision."""
        payload = getattr(outcome, "payload", outcome)
        processing = getattr(payload, "processing", None)
        if self._dataset_revision is None or self._dataset is None:
            self._clear_outcome("没有当前数据修订；返回结果未显示。")
            return False
        if processing is None:
            self._clear_outcome("返回内容不含后端 processing 结果；处理曲线和事件不可用。")
            return False
        match = validate_outcome_revision(
            self._dataset_revision, self._dataset.signals, processing, outcome,
        )
        if not match.matches:
            self._clear_outcome("返回结果与当前数据修订无法确认匹配；未显示处理曲线和事件。 " + match.reason)
            return False
        self._last_outcome = outcome
        self._processing = processing
        phase = getattr(outcome, "phase", None)
        phase_value = getattr(phase, "value", phase)
        backend_status = getattr(outcome, "backend_status", getattr(processing, "execution_status", "unknown"))
        metadata = getattr(payload, "metadata", {})
        metric_flag = metadata.get("metric_calculations_run", False) if isinstance(metadata, Mapping) else False
        self.status.setText(
            f"真实后端 {phase_value or 'preflight'} · execution_status={backend_status} · "
            f"处理状态={getattr(processing, 'execution_status', 'unknown')} · 指标计算="
            f"{'已运行' if metric_flag else '未运行'}；设备验证仍需原始依据"
        )
        self.quality_model.set_frame(getattr(processing, "quality", None))
        self.coverage_model.set_frame(self._segment_coverage(processing))
        self.event_model.set_frame(self._event_coverage(processing))
        self._set_diagnostic_records(
            getattr(processing, "events", None), getattr(processing, "event_interval_audit", None),
        )
        self.locator_label.setText(
            f"处理结果已与修订 {self._dataset_revision.revision_id} 的样本身份和源值匹配；"
            "可双击事件表记录定位。"
        )
        overlap = getattr(processing, "segment_overlap", None)
        self.overlap_model.set_frame(overlap if isinstance(overlap, pd.DataFrame) else None)
        self._render_context(processing)
        self._refresh_timeline()
        return True

    def _clear_outcome(self, message: str) -> None:
        self._last_outcome = None
        self._processing = None
        for model in (self.quality_model, self.coverage_model, self.event_model, self.overlap_model):
            model.set_frame(pd.DataFrame())
        self._set_diagnostic_records(pd.DataFrame(), pd.DataFrame())
        self._render_context()
        self.status.setText(message)
        self.locator_label.setText(message)
        self.range_label.setText("尚未选择时间区间。")
        self._refresh_timeline()

    def _refresh_timeline(self, *, preserve_stream: bool = True) -> None:
        dataset = self._dataset
        raw = dataset.signals.copy(deep=True) if dataset is not None else pd.DataFrame()
        processing = getattr(self, "_processing", None)
        processed = getattr(processing, "signals", None) if processing is not None else None
        events = getattr(processing, "events", None) if processing is not None else None
        audit = getattr(processing, "event_interval_audit", None) if processing is not None else None
        analysis_streams = getattr(processing, "analysis_streams", None) if processing is not None else None
        context = {"analysis_streams": analysis_streams} if isinstance(analysis_streams, pd.DataFrame) else {}
        previous_key = self.stream_selector.currentData() if preserve_stream else None
        self.timeline.set_data(raw, processed, events, audit, context=context)
        available_keys = [self.stream_selector.itemData(index) for index in range(self.stream_selector.count())]
        if previous_key is not None and previous_key in available_keys:
            self.stream_selector.setCurrentIndex(available_keys.index(previous_key))
        elif len(available_keys) > 1:
            # Multiple stable streams require a user choice; don't silently pick the first row.
            self.stream_selector.setCurrentIndex(-1)
        self.timeline.reset_view()

        revision_id = getattr(self._dataset_revision, "revision_id", "unknown")
        if dataset is None:
            self.timeline_status.setText("没有当前数据修订；时间线不可用。")
        elif processing is None:
            self.timeline_status.setText(
                f"当前修订 {revision_id} · 仅显示原始信号；匹配的后端 processing 结果不可用。"
            )
        else:
            process_status = getattr(processing, "execution_status", "unknown")
            signal_state = "可用" if isinstance(processed, pd.DataFrame) else "不可用（未返回表）"
            event_state = "可用" if isinstance(events, pd.DataFrame) else "不可用（未返回表）"
            audit_state = "可用" if isinstance(audit, pd.DataFrame) else "不可用（未返回表）"
            self.timeline_status.setText(
                f"当前修订 {revision_id} · processing={process_status} · signals={signal_state} · "
                f"events={event_state} · event_interval_audit={audit_state}。未返回或 partial 数据保持原状态。"
            )
    @staticmethod
    def _diagnostic_frame(events: Any, audit: Any) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for kind, value in (("event", events), ("interval_audit", audit)):
            if isinstance(value, pd.DataFrame) and not value.empty:
                frame = value.copy(deep=True)
                if "record_type" in frame:
                    frame["record_type"] = kind
                else:
                    frame.insert(0, "record_type", kind)
                frames.append(frame)
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    def _set_diagnostic_records(self, events: Any, audit: Any) -> None:
        self._diagnostic_records = self._diagnostic_frame(events, audit)
        self.diagnostic_model.set_frame(self._diagnostic_records)
        self.locate_button.setEnabled(False)

    def _on_diagnostic_selection_changed(self, *_: Any) -> None:
        self.locate_button.setEnabled(bool(self.diagnostic_view.selectionModel().selectedIndexes()))

    def _selected_diagnostic_row(self) -> pd.Series | None:
        indexes = self.diagnostic_view.selectionModel().selectedIndexes()
        if not indexes:
            return None
        row = indexes[0].row()
        frame = self.diagnostic_model.frame
        return frame.iloc[row] if 0 <= row < len(frame) else None

    def _locate_selected_diagnostic(self) -> None:
        row = self._selected_diagnostic_row()
        if row is not None:
            self._locate_diagnostic_record(row.to_dict(), from_timeline=False)

    def _on_diagnostic_double_clicked(self, index: Any) -> None:
        frame = self.diagnostic_model.frame
        if index.isValid() and 0 <= index.row() < len(frame):
            self._locate_diagnostic_record(frame.iloc[index.row()].to_dict(), from_timeline=False)

    def _on_timeline_diagnostic(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            self.locator_label.setText("时间线返回了无法识别的定位信息。")
            return
        kind = str(payload.get("kind", "diagnostic"))
        if kind == "signal_sample":
            self.locator_label.setText(
                f"信号样本定位 · time_s={payload.get('time_s')} · "
                f"analysis_stream_id={payload.get('analysis_stream_id')}。样本值来自当前选中的稳定数据流。"
            )
            return
        self._locate_diagnostic_record(payload, from_timeline=True)

    def _locate_diagnostic_record(self, record: Mapping[str, Any], *, from_timeline: bool) -> None:
        location = resolve_diagnostic_location(record, self.timeline.model.stream_options)
        row = location.row
        reason = row.get("reason_code", row.get("reason", row.get("decision", row.get("status", ""))))
        prefix = f"{location.record_kind} · {reason} · " if reason else f"{location.record_kind} · "
        if location.stream_key is None:
            self.locator_label.setText(prefix + location.message)
            return
        if location.start_s is None or location.end_s is None:
            self.locator_label.setText(prefix + location.message)
            return
        self.timeline.set_stream(location.stream_key)
        if location.has_range:
            self.timeline.set_time_range(location.start_s, location.end_s)
            # Use TimelineWidget's normal selection path so shading and seconds stay in sync.
            self.timeline._on_span_selected(location.start_s, location.end_s)
            self.locator_label.setText(
                prefix + f"已定位到 {location.start_s:.6g}–{location.end_s:.6g} 秒。"
            )
        elif location.has_point:
            self.timeline.reset_view()
            prepared = self.timeline.model.prepare(location.stream_key)
            times = pd.to_numeric(prepared.raw.get("time_s", pd.Series(dtype=float)), errors="coerce").to_numpy(
                dtype=float, na_value=np.nan,
            )
            finite = times[np.isfinite(times)]
            pad = max(float(np.ptp(finite)) * 0.025, 0.01) if finite.size else 0.01
            self.timeline.set_time_range(location.start_s - pad, location.start_s + pad)
            self.range_label.setText(
                f"已定位到 time_s={location.start_s:.6g} 秒；这是单点记录，持续时间不可用。"
            )
            self.locator_label.setText(prefix + location.message + "该点没有可选择的区间长度。")
        if not from_timeline:
            self.tabs.setCurrentIndex(0)
        self._select_matching_diagnostic(record)

    def _select_matching_diagnostic(self, payload: Mapping[str, Any]) -> None:
        matches = [index for index, row in self._diagnostic_records.iterrows()
                   if diagnostic_rows_match(row.to_dict(), payload)]
        if len(matches) != 1:
            if len(matches) > 1:
                self.locator_label.setText(self.locator_label.text() + " 事件表存在多个同身份候选行，未自动选择。")
            return
        row_position = int(matches[0])
        if not 0 <= row_position < self.diagnostic_model.rowCount():
            return
        index = self.diagnostic_model.index(row_position, 0)
        self.diagnostic_view.selectionModel().select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.diagnostic_view.setCurrentIndex(index)
        self.diagnostic_view.scrollTo(index)

    def _on_interval_selected(self, start: float, end: float) -> None:
        self.range_label.setText(f"已选择 {start:.6g}–{end:.6g} 秒，持续 {end - start:.6g} 秒。")

    def _on_time_range_changed(self, start: float, end: float) -> None:
        self.range_label.setText(f"当前时间视窗 {start:.6g}–{end:.6g} 秒。")

    @staticmethod
    def _sequence(value: Any) -> list[Any]:
        if isinstance(value, (list, tuple, set, frozenset, np.ndarray, pd.Series)):
            return list(value)
        return []

    def _segment_coverage(self, processing: Any) -> pd.DataFrame:
        memberships = getattr(processing, "analysis_memberships", pd.DataFrame())
        signals = getattr(processing, "signals", pd.DataFrame())
        columns = ("segment_id", "segment_instance_id", "segment_name", "subject_id", "session_id", "signal",
                   "valid_n", "support_n", "coverage", "effective_duration_s")
        if not isinstance(memberships, pd.DataFrame) or memberships.empty or not isinstance(signals, pd.DataFrame):
            return pd.DataFrame(columns=columns)
        if "observation_id" not in signals:
            return pd.DataFrame(columns=columns)
        records: list[dict[str, Any]] = []
        obs = signals["observation_id"].astype("string")
        for member in memberships.to_dict("records"):
            ids = [str(value) for value in self._sequence(member.get("support_observation_ids"))]
            if not ids:
                continue
            support = signals.loc[obs.isin(ids)]
            for signal, valid_col in (("gaze", "analysis_gaze_valid"),
                                      ("pupil_left", "analysis_pupil_left_valid"),
                                      ("pupil_right", "analysis_pupil_right_valid")):
                if valid_col not in support:
                    continue
                valid_n = int(support[valid_col].eq(True).sum())
                denominator = int(len(ids))
                records.append({
                    "segment_id": member.get("segment_id"),
                    "segment_instance_id": member.get("segment_instance_id"),
                    "segment_name": member.get("segment_name"),
                    "subject_id": member.get("subject_id"),
                    "session_id": member.get("session_id"),
                    "signal": signal,
                    "valid_n": valid_n,
                    "support_n": denominator,
                    "coverage": valid_n / denominator if denominator else np.nan,
                    "effective_duration_s": member.get("effective_duration_s"),
                })
        return pd.DataFrame.from_records(records, columns=columns)

    @staticmethod
    def _event_coverage(processing: Any) -> pd.DataFrame:
        events = getattr(processing, "events", pd.DataFrame())
        audit = getattr(processing, "event_interval_audit", pd.DataFrame())
        keys = ["analysis_stream_id", "subject_id", "session_id"]
        frames = [frame for frame in (events, audit) if isinstance(frame, pd.DataFrame) and not frame.empty]
        if not frames:
            return pd.DataFrame(columns=(*keys, "event_n", "interval_n", "decision_counts", "reason_counts"))
        stream_values: set[tuple[str, ...]] = set()
        for frame in frames:
            row_frame = pd.DataFrame(index=frame.index)
            for key in keys:
                row_frame[key] = frame[key].fillna("未知").astype(str) if key in frame else "未知"
            for row in row_frame.drop_duplicates().itertuples(index=False, name=None):
                stream_values.add(tuple(row))
        records: list[dict[str, Any]] = []
        for values in sorted(stream_values):
            key_values = dict(zip(keys, values))
            counts: dict[str, int] = {}
            reason_counts: dict[str, int] = {}
            event_n = interval_n = 0
            for frame in frames:
                present = [key for key in keys if key in frame]
                mask = pd.Series(True, index=frame.index)
                for key in present:
                    mask &= frame[key].fillna("未知").astype(str).eq(key_values[key])
                selected = frame.loc[mask]
                if "decision" in selected:
                    interval_n += len(selected)
                    counts.update({str(k): int(v) + counts.get(str(k), 0)
                                   for k, v in selected["decision"].astype(str).value_counts().items()})
                    if "reason_code" in selected:
                        reason_counts.update({str(k): int(v) + reason_counts.get(str(k), 0)
                                              for k, v in selected["reason_code"].dropna().astype(str).value_counts().items()})
                elif "event_type" in selected:
                    event_n += len(selected)
            records.append({**key_values, "event_n": event_n, "interval_n": interval_n,
                            "decision_counts": counts, "reason_counts": reason_counts})
        return pd.DataFrame.from_records(records)

    def _render_context(self, processing: Any = None) -> None:
        rows: list[dict[str, Any]] = []
        for name, frame in self._context_tables.items():
            manifest = next((item for item in self._context_manifest
                             if item.get("context_source_kind") == name), {})
            rows.append({"context_source_kind": name, "rows": len(frame),
                         "analysis_supported": manifest.get("analysis_supported", name != "reading_annotations"),
                         "source_sha256": manifest.get("source_sha256"),
                         "source_sheet": manifest.get("source_sheet")})
        memberships = getattr(processing, "analysis_memberships", pd.DataFrame()) if processing is not None else pd.DataFrame()
        if isinstance(memberships, pd.DataFrame) and not memberships.empty and "interaction_id" in memberships:
            for interaction_id, group in memberships.groupby("interaction_id", dropna=False):
                rows.append({"context_source_kind": "preflight_shared_memberships",
                             "rows": int(group["subject_id"].nunique(dropna=True)) if "subject_id" in group else len(group),
                             "analysis_supported": "仅报告实际 membership；共同时间网格需在多人分析中按同步证据计算",
                             "source_sha256": None, "source_sheet": str(interaction_id)})
        self.context_model.set_frame(pd.DataFrame.from_records(
            rows, columns=("context_source_kind", "rows", "analysis_supported", "source_sha256", "source_sheet")))
        if self._context_tables:
            self.context_note.setText("显示已载入的具名 context 来源与哈希；多人共同时间覆盖只在真实同步/时间窗分析后报告，不由前检图表推断。")
        else:
            self.context_note.setText("未载入参考轨迹 / 目标运动表。共同覆盖仅依据真实共享 membership 及同步证据显示；当前 preflight 不计算多人指标。")

