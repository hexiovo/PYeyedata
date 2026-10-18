"""Processing controls and views of the public pipeline's actual preflight QC."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from math import ceil, isfinite
import numbers
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pyeyedata import ProcessingConfig
from pyeyedata.gui.bridge import PipelineOutcome
from pyeyedata.gui.models import DatasetRevision
from pyeyedata.gui.widgets import DataFrameTableModel, DataFrameTableView


_COLORS = (QColor("#2368a0"), QColor("#d97919"), QColor("#29956b"), QColor("#8b5bb5"))
_SIGNAL_VALID = {
    "gaze": ("gaze_valid", ("gaze_x", "gaze_y"), "analysis_gaze_valid"),
    "pupil_left": ("pupil_left_valid", ("pupil_left",), "analysis_pupil_left_valid"),
    "pupil_right": ("pupil_right_valid", ("pupil_right",), "analysis_pupil_right_valid"),
}


def _finite_array(values: Iterable[Any]) -> np.ndarray:
    return pd.to_numeric(pd.Series(list(values)), errors="coerce").to_numpy(dtype=float, na_value=np.nan)


class _QcPlot(QWidget):
    """Small self-contained plot that needs no optional plotting dependency."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.x_label = ""
        self.y_label = ""
        self.note = ""
        self.kind = "line"
        self.series: list[tuple[str, np.ndarray, np.ndarray, QColor]] = []
        self.setMinimumSize(360, 220)
        self.setAutoFillBackground(True)

    def set_series(
        self,
        series: Iterable[tuple[str, Iterable[Any], Iterable[Any]]],
        *,
        x_label: str,
        y_label: str,
        note: str = "",
        kind: str = "line",
    ) -> None:
        self.kind = kind
        self.x_label = x_label
        self.y_label = y_label
        self.note = note
        self.series = []
        for index, (name, x, y) in enumerate(series):
            xa, ya = _finite_array(x), _finite_array(y)
            size = min(len(xa), len(ya))
            self.series.append((str(name), xa[:size], ya[:size], _COLORS[index % len(_COLORS)]))
        self.update()

    def set_histogram(self, values: Iterable[Any], *, note: str = "") -> None:
        vals = _finite_array(values)
        vals = vals[np.isfinite(vals) & (vals > 0)]
        if not len(vals):
            self.set_series([], x_label="dt (s)", y_label="计数", note=note)
            return
        counts, edges = np.histogram(vals, bins=min(30, max(5, int(np.sqrt(len(vals))))))
        centers = (edges[:-1] + edges[1:]) / 2
        self.set_series([("正向 dt", centers, counts)], x_label="dt (s)", y_label="计数", note=note, kind="histogram")

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QColor("#263746"))
        painter.drawText(12, 20, self.title)
        if self.note:
            painter.setPen(QColor("#657482"))
            painter.drawText(12, 38, self.note)
        if not any(len(x) for _, x, _, _ in self.series):
            painter.setPen(QColor("#6c7882"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无可绘制的后端数据")
            painter.end()
            return

        plot = self.rect().adjusted(54, 48 if self.note else 34, -16, -40)
        if plot.width() < 80 or plot.height() < 60:
            painter.end()
            return
        finite_pairs = []
        for _, xs, ys, _ in self.series:
            size = min(len(xs), len(ys))
            mask = np.isfinite(xs[:size]) & np.isfinite(ys[:size])
            if mask.any():
                finite_pairs.append((xs[:size][mask], ys[:size][mask]))
        if not finite_pairs:
            painter.setPen(QColor("#6c7882"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "所选数据没有有限值")
            painter.end()
            return
        finite_x = np.concatenate([x for x, _ in finite_pairs])
        finite_y = np.concatenate([y for _, y in finite_pairs])
        if not len(finite_x) or not len(finite_y):
            painter.end()
            return
        x_min, x_max = float(np.min(finite_x)), float(np.max(finite_x))
        y_min, y_max = float(np.min(finite_y)), float(np.max(finite_y))
        if x_min == x_max:
            x_min, x_max = x_min - 0.5, x_max + 0.5
        if y_min == y_max:
            padding = max(abs(y_min) * 0.05, 0.5)
            y_min, y_max = y_min - padding, y_max + padding
        elif self.kind == "histogram":
            y_min = 0.0
            y_max += max(1.0, (y_max - y_min) * 0.08)

        painter.setPen(QPen(QColor("#dce3e8"), 1))
        for tick in range(5):
            x = plot.left() + plot.width() * tick / 4
            y = plot.bottom() - plot.height() * tick / 4
            painter.drawLine(int(x), plot.top(), int(x), plot.bottom())
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
            painter.setPen(QColor("#657482"))
            painter.drawText(int(x - 22), plot.bottom() + 16, 46, 16, Qt.AlignmentFlag.AlignHCenter,
                             f"{x_min + (x_max - x_min) * tick / 4:.3g}")
            painter.drawText(2, int(y - 8), 48, 16, Qt.AlignmentFlag.AlignRight,
                             f"{y_min + (y_max - y_min) * tick / 4:.3g}")
            painter.setPen(QPen(QColor("#dce3e8"), 1))
        painter.setPen(QPen(QColor("#71808b"), 1.2))
        painter.drawRect(plot)

        legend_x = plot.left() + 8
        for name, xs, ys, color in self.series:
            size = min(len(xs), len(ys))
            if size > 1800:
                stride = ceil(size / 1800)
                xs, ys = xs[::stride], ys[::stride]
            painter.setPen(QPen(color, 1.8))
            if self.kind == "histogram":
                bar_w = max(2, plot.width() / max(1, len(xs)) * 0.72)
                for x_value, y_value in zip(xs, ys):
                    if not np.isfinite(x_value) or not np.isfinite(y_value):
                        continue
                    sx = plot.left() + (x_value - x_min) / (x_max - x_min) * plot.width()
                    sy = plot.bottom() - (y_value - y_min) / (y_max - y_min) * plot.height()
                    painter.fillRect(int(sx - bar_w / 2), int(sy), int(bar_w), int(plot.bottom() - sy), color)
            else:
                previous: tuple[float, float] | None = None
                for x_value, y_value in zip(xs, ys):
                    if not np.isfinite(x_value) or not np.isfinite(y_value):
                        previous = None
                        continue
                    current = (
                        plot.left() + (x_value - x_min) / (x_max - x_min) * plot.width(),
                        plot.bottom() - (y_value - y_min) / (y_max - y_min) * plot.height(),
                    )
                    if previous is not None:
                        painter.drawLine(int(previous[0]), int(previous[1]), int(current[0]), int(current[1]))
                    previous = current
            painter.setPen(QPen(color, 2.5))
            painter.drawLine(legend_x, plot.top() + 8, legend_x + 20, plot.top() + 8)
            painter.setPen(QColor("#263746"))
            painter.drawText(legend_x + 25, plot.top() + 12, name)
            legend_x += 88 + min(80, len(name) * 8)

        painter.setPen(QColor("#263746"))
        painter.drawText(plot.left(), self.height() - 8, self.width() - 62, 18,
                         Qt.AlignmentFlag.AlignHCenter, self.x_label)
        painter.save()
        painter.translate(13, plot.center().y())
        painter.rotate(-90)
        painter.drawText(-plot.height() / 2, 0, plot.height(), 18, Qt.AlignmentFlag.AlignHCenter, self.y_label)
        painter.restore()
        painter.end()


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
            "默认插值、低通和基线校正均关闭。未开放未验证插值边界绕过；前检由现有 TaskManager 后台调用公共 preflight，"
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
            "interpolate_gaze", "interpolate_pupil", "gaze_max_gap_s", "pupil_max_gap_s",
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
            "allow_unverified_interpolation_bounds",
        ):
            if type(getattr(config, name)) is not bool:
                raise TypeError(f"processing.{name} must be bool")
        for name in ("max_missing_samples", "lowpass_order"):
            item = getattr(config, name)
            if isinstance(item, (bool, np.bool_)) or not isinstance(item, numbers.Integral):
                raise TypeError(f"processing.{name} must be an integer")
        for name in (
            "support_gap_factor",
            "gaze_max_gap_s",
            "pupil_max_gap_s",
            "baseline_min_coverage",
            "baseline_min_length_s",
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
            self.pupil_gap, self.max_missing, self.use_lowpass, self.lowpass_cutoff,
            self.lowpass_order, self.use_baseline, self.baseline_start, self.baseline_end,
            self.baseline_coverage, self.baseline_length,
        )
        signal_states = [(control, control.blockSignals(True)) for control in controls]
        try:
            self.order_policy.setCurrentIndex(self.order_policy.findData(config.order_policy))
            self.duplicate_policy.setCurrentIndex(self.duplicate_policy.findData(config.duplicate_policy))
            for control, item, _name, base_decimals in doubles:
                number = float(item)
                control.setDecimals(max(base_decimals, -Decimal(str(number)).as_tuple().exponent))
                control.setValue(number)
            self.use_max_gap.setChecked(config.support_gap_max_s is not None)
            self.interpolate_gaze.setChecked(config.interpolate_gaze)
            self.interpolate_pupil.setChecked(config.interpolate_pupil)
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
        self.lowpass_cutoff.setEnabled(self.use_lowpass.isChecked())
        self.lowpass_order.setEnabled(self.use_lowpass.isChecked())
        enabled = self.use_baseline.isChecked()
        for control in (self.baseline_start, self.baseline_end, self.baseline_coverage, self.baseline_length):
            control.setEnabled(enabled)

    def _build_results(self) -> None:
        self.signal_selector = QComboBox(self)
        self.signal_selector.addItem("注视点 gaze", "gaze")
        self.signal_selector.addItem("左瞳孔 pupil_left", "pupil_left")
        self.signal_selector.addItem("右瞳孔 pupil_right", "pupil_right")
        self.value_selector = QComboBox(self)
        for label, value in (("gaze_x", "gaze_x"), ("gaze_y", "gaze_y"),
                             ("pupil_left", "pupil_left"), ("pupil_right", "pupil_right")):
            self.value_selector.addItem(label, value)
        self.stream_selector = QComboBox(self)
        self.stream_selector.addItem("尚未选择数据流", None)
        self.raw_plot = _QcPlot("dt 分布（正向采样间隔）", self)
        self.missing_plot = _QcPlot("有效性 / 缺失时间线", self)
        self.signal_plot = _QcPlot("原始与预处理信号对照", self)
        self.quality_model = DataFrameTableModel(parent=self)
        self.quality_view = DataFrameTableView(self)
        self.quality_view.setModel(self.quality_model)
        self.coverage_model = DataFrameTableModel(parent=self)
        self.coverage_view = DataFrameTableView(self)
        self.coverage_view.setModel(self.coverage_model)
        self.event_model = DataFrameTableModel(parent=self)
        self.event_view = DataFrameTableView(self)
        self.event_view.setModel(self.event_model)
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
        selectors = QHBoxLayout()
        selectors.addWidget(QLabel("数据流", overview))
        selectors.addWidget(self.stream_selector)
        selectors.addWidget(QLabel("时间线信号", overview))
        selectors.addWidget(self.signal_selector)
        selectors.addWidget(QLabel("前后对照字段", overview))
        selectors.addWidget(self.value_selector)
        selectors.addStretch(1)
        overview_layout.addLayout(selectors)
        overview_layout.addWidget(self.raw_plot, 1)
        overview_layout.addWidget(self.missing_plot, 1)
        overview_layout.addWidget(self.signal_plot, 1)

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
            self.interpolate_gaze, self.gaze_gap, self.interpolate_pupil, self.pupil_gap, self.max_missing,
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
        self.signal_selector.currentIndexChanged.connect(self._refresh_plots)
        self.value_selector.currentIndexChanged.connect(self._refresh_plots)
        self.stream_selector.currentIndexChanged.connect(self._refresh_plots)

    def _control_toggled(self, *_: Any) -> None:
        self._sync_control_enabled_states()
        self._emit_configuration()

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
            self._clear_parameter_error()
            self.status.setText("处理配置已更新；运行前检查会在现有任务队列中真实执行。")
        except Exception as exc:
            self.configuration_changed.emit({})
            self._report_parameter_error(str(exc))

    def set_dataset(self, revision: DatasetRevision | None) -> None:
        self._dataset_revision = revision
        self._dataset = revision.dataset_copy() if revision is not None else None
        self.stream_selector.blockSignals(True)
        self.stream_selector.clear()
        self.stream_selector.addItem("尚未选择数据流", None)
        if self._dataset is not None:
            frame = self._dataset.signals
            keys = [key for key in ("subject_id", "session_id", "interaction_id") if key in frame]
            if keys:
                for values in frame[keys].astype("string").fillna("未知").drop_duplicates().itertuples(index=False, name=None):
                    value_tuple = tuple(str(value) for value in values)
                    label = " / ".join(f"{key}={value}" for key, value in zip(keys, value_tuple))
                    self.stream_selector.addItem(label, (tuple(keys), value_tuple))
                if self.stream_selector.count() > 1:
                    self.stream_selector.setCurrentIndex(1)
        self.stream_selector.blockSignals(False)
        self._refresh_plots()

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

    def set_outcome(self, outcome: PipelineOutcome | Any) -> None:
        """Render the actual returned preflight/run payload; no local preflight is run here."""
        self._last_outcome = outcome
        payload = getattr(outcome, "payload", outcome)
        processing = getattr(payload, "processing", None)
        if processing is None:
            self.status.setText("返回内容不含后端 processing 结果；没有生成 QC 数值。")
            return
        phase = getattr(outcome, "phase", None)
        phase_value = getattr(phase, "value", phase)
        backend_status = getattr(outcome, "backend_status", getattr(processing, "execution_status", "unknown"))
        metric_flag = getattr(payload, "metadata", {}).get("metric_calculations_run", False)
        self.status.setText(
            f"真实后端 {phase_value or 'preflight'} · execution_status={backend_status} · "
            f"处理状态={getattr(processing, 'execution_status', 'unknown')} · 指标计算="
            f"{'已运行' if metric_flag else '未运行'}；设备验证仍按原声明。"
        )
        self.quality_model.set_frame(getattr(processing, "quality", None))
        self.coverage_model.set_frame(self._segment_coverage(processing))
        self.event_model.set_frame(self._event_coverage(processing))
        overlap = getattr(processing, "segment_overlap", None)
        self.overlap_model.set_frame(overlap if isinstance(overlap, pd.DataFrame) else None)
        self._render_context(processing)
        self._refresh_plots(processing=processing)

    def _selected_rows(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        selected = self.stream_selector.currentData()
        if not selected:
            return frame
        stream_keys, values = selected
        if not set(stream_keys).issubset(frame.columns):
            return frame
        mask = pd.Series(True, index=frame.index)
        for key, value in zip(stream_keys, values):
            current = frame[key].astype("string").fillna("未知")
            mask &= current.eq(value)
        return frame.loc[mask]

    def _refresh_plots(self, *_: Any, processing: Any = None) -> None:
        dataset = self._dataset
        if dataset is None:
            self.raw_plot.set_histogram([], note="导入数据后显示")
            self.missing_plot.set_series([], x_label="time_s", y_label="有效状态", note="导入并运行前检后显示")
            self.signal_plot.set_series([], x_label="time_s", y_label="值", note="导入并运行前检后显示")
            return
        raw = self._selected_rows(dataset.signals)
        times = pd.to_numeric(raw.get("time_s", pd.Series(dtype=float)), errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        diffs = np.diff(times) if len(times) > 1 else np.array([], dtype=float)
        self.raw_plot.set_histogram(diffs, note="从所选 source stream 的原始时间相邻差计算；倒序/重复行由后端策略报告。")
        payload = getattr(self._last_outcome, "payload", self._last_outcome)
        if processing is None:
            processing = getattr(payload, "processing", None)
        after = getattr(processing, "signals", pd.DataFrame()) if processing is not None else pd.DataFrame()
        raw_sig = self.signal_selector.currentData() or "gaze"
        if raw_sig in _SIGNAL_VALID:
            raw_valid, value_columns, after_valid = _SIGNAL_VALID[raw_sig]
            if raw_sig == "gaze":
                if set(value_columns).issubset(raw.columns):
                    coordinates = raw.loc[:, list(value_columns)].apply(pd.to_numeric, errors="coerce")
                    present = pd.Series(np.isfinite(coordinates.to_numpy(dtype=float)).all(axis=1), index=raw.index)
                else:
                    present = pd.Series(False, index=raw.index)
            else:
                if value_columns[0] in raw:
                    numeric = pd.to_numeric(raw[value_columns[0]], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
                    present = pd.Series(np.isfinite(numeric), index=raw.index)
                else:
                    present = pd.Series(False, index=raw.index)
            if raw_valid in raw:
                source_valid = raw[raw_valid]
                is_invalid = source_valid.eq(False).fillna(False).to_numpy(dtype=bool)
                is_valid = (source_valid.eq(True).fillna(False) & present).to_numpy(dtype=bool)
                is_missing = ~present.to_numpy(dtype=bool)
                state = np.full(len(raw), np.nan, dtype=float)
                state[is_invalid | is_missing] = 0.0
                state[is_valid] = 1.0
            else:
                # Data presence does not establish source validity. Missing values are
                # visibly absent, while present values without a validity declaration
                # remain unknown instead of being promoted to valid.
                state = np.where(present, np.nan, 0.0)
            timeline: list[tuple[str, Iterable[Any], Iterable[Any]]] = [("source", times, state)]
            if isinstance(after, pd.DataFrame) and not after.empty and after_valid in after and "time_s" in after:
                processed = self._selected_rows(after)
                after_state = processed[after_valid].astype("boolean").to_numpy(dtype=float, na_value=np.nan)
                timeline.append(("preflight", processed["time_s"], after_state))
            self.missing_plot.set_series(timeline, x_label="time_s", y_label="有效(1) / 无效(0) / 未知(空)",
                                         note="仅画图抽样；缺失值为 0，缺少有效性声明的数据保持未知空白。")
        else:
            self.missing_plot.set_series([], x_label="time_s", y_label="状态")
        value = self.value_selector.currentData() or "gaze_x"
        before_values = raw[value] if value in raw else pd.Series(dtype=float)
        line_series: list[tuple[str, Iterable[Any], Iterable[Any]]] = [("source", times, before_values)]
        if isinstance(after, pd.DataFrame) and not after.empty and value in after and "time_s" in after:
            processed = self._selected_rows(after)
            output_column = value
            if value in {"pupil_left", "pupil_right"}:
                baseline = f"{value}_baseline_corrected"
                if baseline in processed and self._current_baseline_enabled():
                    output_column = baseline
            line_series.append(("after", processed["time_s"], processed[output_column]))
        self.signal_plot.set_series(line_series, x_label="time_s", y_label=value,
                                    note="原始与处理值来自同一数据修订 / 后端 outcome；图表最多抽样显示 1,800 点。")

    def _current_baseline_enabled(self) -> bool:
        return self.use_baseline.isChecked()

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

