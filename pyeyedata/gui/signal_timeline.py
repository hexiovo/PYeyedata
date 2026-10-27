"""Interactive, identity-aware timeline for raw and processed signals."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.widgets import SpanSelector
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .timeline_model import PreparedTimeline, StreamOption, TimelineModel, signal_validity_columns, status_values


_MAX_DRAW_POINTS = 12_000
_EVENT_COLORS = ("#3579a8", "#3d906f", "#bd7b29", "#9761a8", "#b4514d", "#69778a")
_AUDIT_COLORS = {
    "accepted": "#3d906f",
    "included": "#3d906f",
    "rejected": "#bd625d",
    "blocked": "#a83d3d",
    "partial": "#bd7b29",
    "unavailable": "#7b718b",
    "not_applicable": "#77808a",
}


def _finite_column(frame: pd.DataFrame, column: str) -> np.ndarray:
    if column not in frame:
        return np.full(len(frame), np.nan, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _text(value: Any, fallback: str = "") -> str:
    value = _json_value(value)
    if value is None or value == "":
        return fallback
    return str(value)


class TimelineWidget(QWidget):
    """A synchronized multi-track timeline embedded in the Qt workflow.

    ``raw_signals`` and ``processed_signals`` are paired only by stable sample
    identity plus the same ``time_s`` value.  When no safe pairing exists the
    widget explains why and leaves the curves separate/hidden.
    """

    timeRangeChanged = Signal(float, float)
    intervalSelected = Signal(float, float)
    diagnosticSelected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = TimelineModel()
        self._prepared = PreparedTimeline(None, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        self._selected_interval: tuple[float, float] | None = None
        self._setting_range = False
        self._hit_artists: list[tuple[Any, dict[str, Any]]] = []
        self._range_patches: list[Any] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("分析流", self))
        self.stream_selector = QComboBox(self)
        self.stream_selector.setMinimumContentsLength(18)
        controls.addWidget(self.stream_selector, 3)
        controls.addWidget(QLabel("信号", self))
        self.signal_selector = QComboBox(self)
        controls.addWidget(self.signal_selector, 1)
        self.reset_button = QPushButton("重置视图", self)
        controls.addWidget(self.reset_button)
        root.addLayout(controls)

        self.figure = Figure(figsize=(9.2, 5.3), dpi=100, constrained_layout=False)
        grid = self.figure.add_gridspec(3, 1, height_ratios=(4.2, 1.05, 1.55), hspace=0.09)
        self.signal_axes = self.figure.add_subplot(grid[0, 0])
        self.status_axes = self.figure.add_subplot(grid[1, 0], sharex=self.signal_axes)
        self.interval_axes = self.figure.add_subplot(grid[2, 0], sharex=self.signal_axes)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        root.addWidget(self.toolbar)
        root.addWidget(self.canvas, 1)
        self.note_label = QLabel("载入信号、事件及区间审核表后显示；横轴使用实际 time_s（秒）。", self)
        self.note_label.setWordWrap(True)
        root.addWidget(self.note_label)

        self.stream_selector.currentIndexChanged.connect(self._stream_changed)
        self.signal_selector.currentIndexChanged.connect(self._signal_changed)
        self.reset_button.clicked.connect(self.reset_view)
        self.canvas.mpl_connect("button_press_event", self._on_mouse_press)
        self._span_selector = SpanSelector(
            self.signal_axes, self._on_span_selected, "horizontal", useblit=True,
            props={"facecolor": "#4c9bd6", "alpha": 0.20},
            interactive=True, drag_from_anywhere=True,
            button=1, minspan=0.0, ignore_event_outside=True,
        )
        self.signal_axes.callbacks.connect("xlim_changed", self._on_xlim_changed)
        self._draw_empty("选择一个信号流以查看样本、处理曲线和事件区间。")

    @property
    def selected_interval(self) -> tuple[float, float] | None:
        return self._selected_interval

    def set_data(
        self,
        raw_signals: Any,
        processed_signals: Any = None,
        events: Any = None,
        interval_audit: Any = None,
        *,
        context: Any = None,
    ) -> None:
        self.model.set_data(raw_signals, processed_signals, events, interval_audit, context=context)
        self.stream_selector.blockSignals(True)
        self.stream_selector.clear()
        for option in self.model.stream_options:
            self.stream_selector.addItem(option.label, option.key)
        self.stream_selector.blockSignals(False)
        self.signal_selector.blockSignals(True)
        self.signal_selector.clear()
        for signal_name in self.model.signal_names:
            self.signal_selector.addItem(signal_name, signal_name)
        self.signal_selector.blockSignals(False)
        if self.model.stream_options:
            self.stream_selector.setCurrentIndex(0)
        self._render()

    def set_stream(self, stream_key: Any) -> None:
        index = next((i for i in range(self.stream_selector.count())
                      if self.stream_selector.itemData(i) == stream_key), -1)
        if index < 0:
            matches = [i for i, option in enumerate(self.model.stream_options)
                       if option.stream_id == str(stream_key)]
            if len(matches) == 1:
                index = matches[0]
        if index >= 0:
            self.stream_selector.setCurrentIndex(index)
        else:
            # An unknown or reused ID must not silently select a different source.
            self._draw_empty("所选 analysis_stream_id 不存在或跨来源重复；请从完整数据流列表选择。")

    def set_time_range(self, start: float, end: float) -> None:
        start_value, end_value = float(start), float(end)
        if not math.isfinite(start_value) or not math.isfinite(end_value) or end_value <= start_value:
            raise ValueError("time range must contain finite seconds with start < end")
        self._setting_range = True
        try:
            self.signal_axes.set_xlim(start_value, end_value)
            self.canvas.draw_idle()
        finally:
            self._setting_range = False

    def reset_view(self) -> None:
        self._selected_interval = None
        self._clear_selection_patches()
        times = np.concatenate((
            _finite_column(self._prepared.raw, "time_s"),
            _finite_column(self._prepared.processed, "time_s"),
            _finite_column(self._prepared.events, "start_s"),
            _finite_column(self._prepared.events, "end_s"),
            _finite_column(self._prepared.interval_audit, "start_s"),
            _finite_column(self._prepared.interval_audit, "end_s"),
        )) if any(not frame.empty for frame in (self._prepared.raw, self._prepared.processed,
                                                self._prepared.events, self._prepared.interval_audit)) else np.array([])
        times = times[np.isfinite(times)]
        if len(times):
            lo, hi = float(np.min(times)), float(np.max(times))
            pad = max((hi - lo) * 0.025, 0.01)
            self.set_time_range(lo - pad, hi + pad if hi > lo else lo + pad)
        else:
            self.signal_axes.set_xlim(0, 1)
        self.canvas.draw_idle()

    def _current_stream_key(self) -> Any:
        return self.stream_selector.currentData()

    def _stream_changed(self, *_: Any) -> None:
        self._render()

    def _signal_changed(self, *_: Any) -> None:
        self._render()

    def _render(self) -> None:
        self._prepared = self.model.prepare(self._current_stream_key())
        signal = self.signal_selector.currentData()
        self._clear_selection_patches()
        self._hit_artists.clear()
        for axis in (self.signal_axes, self.status_axes, self.interval_axes):
            axis.clear()
        self._draw_signal(signal)
        self._draw_status(signal)
        self._draw_intervals()
        self.signal_axes.set_ylabel("信号值")
        self.status_axes.set_ylabel("样本状态")
        self.interval_axes.set_xlabel("time_s（秒）")
        self.interval_axes.set_ylabel("事件 / 审核")
        self.signal_axes.grid(True, axis="both", alpha=0.20)
        self.interval_axes.grid(True, axis="x", alpha=0.16)
        self.figure.subplots_adjust(left=0.12, right=0.985, top=0.97, bottom=0.11, hspace=0.09)
        if self._prepared.message:
            note = self._prepared.message
        else:
            note = "原始与处理曲线按稳定样本身份和相同秒数对齐。"
        if self._prepared.stream is None:
            note = "没有可用数据流。 " + note
        self.note_label.setText(note)
        self.canvas.draw_idle()
        self.reset_view()

    def _draw_empty(self, message: str) -> None:
        for axis in (self.signal_axes, self.status_axes, self.interval_axes):
            axis.clear()
        self.signal_axes.text(0.5, 0.5, message, ha="center", va="center", transform=self.signal_axes.transAxes,
                              color="#58636f", wrap=True)
        self.signal_axes.set_ylabel("信号值")
        self.status_axes.set_ylabel("样本状态")
        self.interval_axes.set_ylabel("事件 / 审核")
        self.interval_axes.set_xlabel("time_s（秒）")
        self.figure.subplots_adjust(left=0.12, right=0.985, top=0.97, bottom=0.11)
        self.note_label.setText(message)
        self.canvas.draw_idle()

    @staticmethod
    def _partitions(frame: pd.DataFrame) -> list[pd.DataFrame]:
        if frame.empty:
            return []
        columns = [name for name in ("physical_partition_id", "processing_partition_id", "timebase_id", "clock_id", "source_id")
                   if name in frame]
        if not columns:
            return [frame]
        try:
            groups = frame.groupby(columns, dropna=False, sort=False, observed=True)
            return [group for _, group in groups]
        except (TypeError, ValueError):
            return [frame]

    @staticmethod
    def _draw_points(axis: Any, frame: pd.DataFrame, signal: str, *, label: str, color: str,
                     linestyle: str, linewidth: float, alpha: float) -> bool:
        if signal not in frame or "time_s" not in frame or frame.empty:
            return False
        plotted = False
        for part in TimelineWidget._partitions(frame):
            x = _finite_column(part, "time_s")
            y = _finite_column(part, signal)
            finite_time = np.isfinite(x)
            if not finite_time.any() or not np.isfinite(y[finite_time]).any():
                continue
            # Keep missing signal values so Matplotlib breaks the line there;
            # filtering them out would draw an invented bridge across a gap.
            x, y = x[finite_time], y[finite_time]
            order = np.argsort(x, kind="mergesort")
            x, y = x[order], y[order]
            if len(x) > _MAX_DRAW_POINTS:
                regular = np.linspace(0, len(x) - 1, _MAX_DRAW_POINTS, dtype=int)
                missing_breaks = np.flatnonzero(~np.isfinite(y))
                selected = np.unique(np.concatenate((regular, missing_breaks)))
                x, y = x[selected], y[selected]
            axis.plot(x, y, label=label if not plotted else "_nolegend_", color=color,
                      linestyle=linestyle, linewidth=linewidth, alpha=alpha)
            plotted = True
        return plotted

    def _draw_signal(self, signal: str | None) -> None:
        axis = self.signal_axes
        raw, processed = self._prepared.raw, self._prepared.processed
        if not signal:
            axis.text(0.5, 0.5, "所选数据没有可绘制的连续信号列。", ha="center", va="center",
                      transform=axis.transAxes, color="#58636f")
            return
        raw_drawn = self._draw_points(axis, raw, signal, label=f"原始 {signal}", color="#365f89",
                                      linestyle="--", linewidth=1.15, alpha=0.76)
        processed_drawn = self._draw_points(axis, processed, signal, label=f"处理 {signal}", color="#bd5f49",
                                            linestyle="-", linewidth=1.35, alpha=0.92)
        if not raw_drawn and not processed_drawn:
            message = "该信号列没有有限数值。"
            if self._prepared.message:
                message += " " + self._prepared.message
            axis.text(0.5, 0.5, message, ha="center", va="center", transform=axis.transAxes,
                      color="#58636f", wrap=True)
        else:
            axis.legend(loc="upper right", frameon=False, ncol=2)
        axis.set_title(f"{signal} · 原始 / 处理", loc="left", fontsize=10)

    def _draw_status(self, signal: str | None) -> None:
        axis = self.status_axes
        axis.set_ylim(-0.55, 2.55)
        axis.set_yticks((2, 1, 0), labels=("源无效", "来源未知", "插值"))
        axis.tick_params(axis="y", labelsize=8, length=0)
        axis.grid(True, axis="x", alpha=0.12)
        if not signal:
            return
        source_cols, interp_cols = signal_validity_columns(signal)
        raw, processed = self._prepared.raw, self._prepared.processed
        times = _finite_column(raw, "time_s")
        if source_cols and len(raw) == len(times):
            values, used = status_values(raw, source_cols)
            finite = np.isfinite(times)
            invalid = finite & (values == 0)
            unknown = finite & ~np.isfinite(values)
            if invalid.any():
                axis.scatter(times[invalid], np.full(int(invalid.sum()), 2.0), s=16, marker="|",
                             color="#bd514d", linewidths=1.2, label="源无效样本")
            if unknown.any() and used:
                axis.scatter(times[unknown], np.full(int(unknown.sum()), 1.0), s=12, marker="|",
                             color="#8a9299", linewidths=1.0, label="来源状态未知")
        interp_times = _finite_column(processed, "time_s")
        if interp_cols and len(processed) == len(interp_times):
            values, _ = status_values(processed, interp_cols)
            mask = np.isfinite(interp_times) & (values == 1)
            if mask.any():
                axis.scatter(interp_times[mask], np.zeros(int(mask.sum())), s=18, marker="|",
                             color="#8061a5", linewidths=1.3, label="插值样本")
        handles, labels = axis.get_legend_handles_labels()
        if handles:
            axis.legend(handles, labels, loc="upper right", frameon=False, ncol=3, fontsize=8)
        elif not any(column in raw or column in processed for column in (*source_cols, *interp_cols)):
            axis.text(0.5, 0.5, "该信号没有来源有效性或插值标记。", ha="center", va="center",
                      transform=axis.transAxes, fontsize=8, color="#6f7881")

    @staticmethod
    def _interval(row: dict[str, Any], start_names: tuple[str, ...], end_names: tuple[str, ...]) -> tuple[float, float] | None:
        start = next((row.get(name) for name in start_names if _json_value(row.get(name)) is not None), None)
        end = next((row.get(name) for name in end_names if _json_value(row.get(name)) is not None), None)
        try:
            start_value, end_value = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(start_value) or not math.isfinite(end_value):
            return None
        return start_value, end_value

    def _draw_intervals(self) -> None:
        axis = self.interval_axes
        events = self._prepared.events
        audit = self._prepared.interval_audit
        event_rows = [row for row in events.to_dict(orient="records")
                      if self._interval(row, ("start_s",), ("end_s",)) is not None]
        audit_rows = [row for row in audit.to_dict(orient="records")
                      if self._interval(row, ("start_s",), ("end_s",)) is not None]

        event_types = sorted({_text(row.get("event_type"), "事件") for row in event_rows})
        audit_labels = sorted({
            " / ".join(value for value in (_text(row.get("decision"), "审核"),
                                           _text(row.get("reason_code"))) if value)
            for row in audit_rows
        })
        lane_items: list[tuple[str, str, list[dict[str, Any]], str]] = []
        for idx, event_type in enumerate(event_types):
            rows = [row for row in event_rows if _text(row.get("event_type"), "事件") == event_type]
            lane_items.append((f"事件 · {event_type}", "event", rows, _EVENT_COLORS[idx % len(_EVENT_COLORS)]))
        for label in audit_labels:
            decision = label.split(" / ", 1)[0]
            rows = [row for row in audit_rows if " / ".join(value for value in
                    (_text(row.get("decision"), "审核"), _text(row.get("reason_code"))) if value) == label]
            color = _AUDIT_COLORS.get(decision.lower(), "#bd7b29")
            lane_items.append((f"审核 · {label}", "interval_audit", rows, color))

        if not lane_items:
            axis.set_ylim(-0.6, 0.6)
            axis.set_yticks([])
            axis.text(0.5, 0.5, "没有实际事件或区间审核记录。", ha="center", va="center",
                      transform=axis.transAxes, fontsize=8, color="#6f7881")
            return

        for lane, (label, kind, rows, color) in enumerate(lane_items):
            for row in rows:
                interval = self._interval(row, ("start_s",), ("end_s",))
                if interval is None:
                    continue
                start, end = interval
                width = max(0.0, end - start)
                if width == 0:
                    artist = axis.axvline(start, ymin=max(0.0, (lane + 0.18) / len(lane_items)),
                                          ymax=min(1.0, (lane + 0.82) / len(lane_items)),
                                          color=color, linewidth=2.0, alpha=0.85)
                else:
                    from matplotlib.patches import Rectangle
                    artist = Rectangle((start, lane - 0.32), width, 0.64,
                                       facecolor=color, edgecolor=color, linewidth=0.5, alpha=0.78)
                    axis.add_patch(artist)
                artist.set_picker(True)
                payload = {
                    "kind": kind,
                    "row": {str(key): _json_value(value) for key, value in row.items()},
                    "analysis_stream_id": _json_value(row.get("analysis_stream_id")) or
                    (self._prepared.stream.stream_id if self._prepared.stream else None),
                    "start_s": start,
                    "end_s": end,
                }
                self._hit_artists.append((artist, payload))
        axis.set_ylim(-0.55, len(lane_items) - 0.45)
        axis.set_yticks(range(len(lane_items)), labels=[item[0] for item in lane_items], fontsize=8)
        axis.invert_yaxis()
        axis.tick_params(axis="y", length=0)

    def _clear_selection_patches(self) -> None:
        for patch in self._range_patches:
            try:
                patch.remove()
            except (ValueError, AttributeError):
                pass
        self._range_patches.clear()

    def _on_span_selected(self, start: float, end: float) -> None:
        if not math.isfinite(start) or not math.isfinite(end):
            return
        lo, hi = sorted((float(start), float(end)))
        if hi <= lo:
            return
        self._selected_interval = (lo, hi)
        self._clear_selection_patches()
        for axis in (self.signal_axes, self.status_axes, self.interval_axes):
            self._range_patches.append(axis.axvspan(lo, hi, color="#4c9bd6", alpha=0.10, zorder=0))
        self.canvas.draw_idle()
        self.intervalSelected.emit(lo, hi)

    def _on_xlim_changed(self, axis: Any) -> None:
        if self._setting_range:
            return
        lo, hi = axis.get_xlim()
        if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
            self.timeRangeChanged.emit(float(lo), float(hi))

    def _on_mouse_press(self, event: Any) -> None:
        if event.inaxes is self.signal_axes and event.xdata is not None:
            self._emit_nearest_sample(float(event.xdata))
        elif event.inaxes is self.interval_axes:
            for artist, payload in reversed(self._hit_artists):
                try:
                    contains, _ = artist.contains(event)
                except (AttributeError, ValueError):
                    contains = False
                if contains:
                    self.diagnosticSelected.emit(payload)
                    return

    def _emit_nearest_sample(self, time_s: float) -> None:
        frame = self._prepared.processed if not self._prepared.processed.empty else self._prepared.raw
        if frame.empty or "time_s" not in frame:
            return
        times = _finite_column(frame, "time_s")
        finite = np.isfinite(times)
        if not finite.any():
            return
        positions = np.flatnonzero(finite)
        index = int(positions[np.argmin(np.abs(times[finite] - time_s))])
        row = frame.iloc[index].to_dict()
        self.diagnosticSelected.emit({
            "kind": "signal_sample",
            "row": {str(key): _json_value(value) for key, value in row.items()},
            "time_s": float(times[index]),
            "analysis_stream_id": _json_value(row.get("analysis_stream_id")) or
            (self._prepared.stream.stream_id if self._prepared.stream else None),
        })

