"""Read-only, table-backed result charts for the desktop workflow.

Charts in this module never recompute scientific metrics.  They only reshape or
draw values already returned by the public pipeline, and refuse plots whose
units, coordinate frame, or scope cannot be identified from that data.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
from matplotlib import font_manager, rcParams
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


_MAX_PLOT_ROWS = 6000
_MAX_SERIES = 10
_ID_COLUMNS = (
    "subject_id", "session_id", "interaction_id", "pair_id", "session_id",
    "segment_id", "segment_instance_id", "trial", "condition", "eye", "signal",
    "metric_id", "analysis_stream_id",
)


def _install_chart_font() -> None:
    """Use the app's Chinese UI font in Matplotlib when installed on Windows."""
    candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            font_manager.fontManager.addfont(str(path))
            family = font_manager.FontProperties(fname=str(path)).get_name()
        except (OSError, RuntimeError, ValueError):
            continue
        if family:
            rcParams["font.family"] = [family]
            rcParams["axes.unicode_minus"] = False
            return


_install_chart_font()


def _clean_text(value: Any) -> str:
    if value is None or value is pd.NA or value is pd.NaT:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _unique_values(frame: pd.DataFrame, column: str) -> list[str]:
    if column not in frame:
        return []
    values = [_clean_text(value) for value in frame[column].dropna().unique()]
    return [value for value in values if value]


def _first(mapping: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = mapping
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                value = None
                break
            value = value[part]
        if value not in (None, "", "unknown"):
            return value
    return None


class ResultChartPanel(QWidget):
    """A real Qt-embedded Matplotlib canvas with explicit axis selection."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._table_name = ""
        self.chart_available = False
        self._frame = pd.DataFrame()
        self._tables: dict[str, pd.DataFrame] = {}
        self._semantics: dict[str, Any] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.x_column = QComboBox(self)
        self.x_column.setToolTip("X 轴字段；仅列出本表的数值字段。")
        self.y_column = QComboBox(self)
        self.y_column.setToolTip("Y 轴字段；图表只使用实际返回值。")
        self.group_column = QComboBox(self)
        self.group_column.setToolTip("可按一个实际类别字段分组；过多组会停止绘图。")
        self.mode = QComboBox(self)
        self.mode.addItem("散点", "scatter")
        self.mode.addItem("按 X 连线", "line")
        self.draw_button = QPushButton("绘图", self)
        controls.addWidget(QLabel("X", self))
        controls.addWidget(self.x_column, 2)
        controls.addWidget(QLabel("Y", self))
        controls.addWidget(self.y_column, 2)
        controls.addWidget(QLabel("分组", self))
        controls.addWidget(self.group_column, 2)
        controls.addWidget(self.mode)
        controls.addWidget(self.draw_button)
        root.addLayout(controls)

        self.figure = Figure(figsize=(7.4, 3.9), dpi=100, constrained_layout=True)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        root.addWidget(self.toolbar)
        root.addWidget(self.canvas, 1)
        self.notice = QLabel("选择实际返回的表后，图表会从该表绘制。", self)
        self.notice.setWordWrap(True)
        self.notice.setObjectName("pageDescription")
        root.addWidget(self.notice)
        self.draw_button.clicked.connect(self.draw)
        self.x_column.currentIndexChanged.connect(self._selection_changed)
        self.y_column.currentIndexChanged.connect(self._selection_changed)
        self.group_column.currentIndexChanged.connect(self._selection_changed)
        self.mode.currentIndexChanged.connect(self._selection_changed)
        self._show_message("结果图表", "等待结果表格数据。")

    def set_data(
        self,
        table_name: str,
        frame: pd.DataFrame | None,
        *,
        tables: Mapping[str, pd.DataFrame] | None = None,
        semantics: Mapping[str, Any] | None = None,
    ) -> None:
        self._table_name = str(table_name or "")
        self._frame = frame.copy(deep=True) if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        self._tables = {
            str(name): table.copy(deep=True)
            for name, table in (tables or {}).items()
            if isinstance(table, pd.DataFrame)
        }
        self._semantics = dict(semantics or {})
        self._reset_axis_choices()
        self.draw()

    def _reset_axis_choices(self) -> None:
        numeric = [
            str(column) for column in self._frame.columns
            if pd.api.types.is_numeric_dtype(self._frame[column].dtype)
            and not pd.api.types.is_bool_dtype(self._frame[column].dtype)
        ]
        categorical = [str(column) for column in self._frame.columns
                       if str(column) in _ID_COLUMNS or not pd.api.types.is_numeric_dtype(self._frame[column].dtype)]
        self.x_column.blockSignals(True)
        self.y_column.blockSignals(True)
        self.group_column.blockSignals(True)
        self.x_column.clear()
        self.y_column.clear()
        self.group_column.clear()
        for column in numeric:
            self.x_column.addItem(column, column)
            self.y_column.addItem(column, column)
        self.group_column.addItem("不分组", "")
        for column in categorical:
            self.group_column.addItem(column, column)
        preferred_x = next((name for name in (
            "time_s", "frequency_hz", "lag_s", "start_s", "amplitude_deg", "row", "column",
        ) if name in numeric), numeric[0] if numeric else "")
        preferred_y = next((name for name in (
            "value", "dwell_s", "correlation", "power_density", "pupil_left",
            "gaze_x", "peak_velocity_deg_s", "estimate", "n_members",
        ) if name in numeric and name != preferred_x), numeric[1] if len(numeric) > 1 else (numeric[0] if numeric else ""))
        if preferred_x:
            self.x_column.setCurrentIndex(max(0, self.x_column.findData(preferred_x)))
        if preferred_y:
            self.y_column.setCurrentIndex(max(0, self.y_column.findData(preferred_y)))
        preferred_group = next((name for name in (
            "pair_id", "subject_id", "signal", "eye", "condition", "metric_id", "event_type",
        ) if name in categorical), "")
        if preferred_group:
            self.group_column.setCurrentIndex(max(0, self.group_column.findData(preferred_group)))
        self.x_column.blockSignals(False)
        self.y_column.blockSignals(False)
        self.group_column.blockSignals(False)

    def _selection_changed(self, _index: int = 0) -> None:
        # Combo-box changes should not redraw while another combo is being rebuilt.
        if self._table_name:
            self.draw()

    def _show_message(self, title: str, message: str) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_axis_off()
        ax.text(0.5, 0.56, title, ha="center", va="center", fontsize=13,
                fontweight="semibold", color="#24364b", transform=ax.transAxes)
        ax.text(0.5, 0.40, message, ha="center", va="center", wrap=True,
                fontsize=10, color="#596b80", transform=ax.transAxes)
        self.canvas.draw_idle()
        self.notice.setText(message)
        self.chart_available = False

    def _semantic_value(self, frame: pd.DataFrame, column: str, kind: str) -> str | None:
        column_key = "coordinate_unit" if kind == "coordinate_unit" else "coordinate_frame"
        values = _unique_values(frame, column_key)
        if len(values) > 1:
            raise ValueError(f"表中存在多个 {column_key}，请用表格筛选保留单一语义后再绘图。")
        value = values[0] if values else _first(
            self._semantics,
            f"{column_key}", f"metadata.{column_key}", f"dataset_metadata.{column_key}",
            f"coordinate_metadata.{column_key}", f"import_metadata.{column_key}",
        )
        if not value or str(value).casefold() == "unknown":
            raise ValueError(f"{column} 的 {column_key} 未由输入或返回元数据确认，空间图已停止。")
        return str(value)

    def _unit(self, frame: pd.DataFrame, column: str) -> str | None:
        known = {
            "time_s": "s", "start_s": "s", "end_s": "s", "duration_s": "s",
            "lag_s": "s", "frequency_hz": "Hz", "fs_hz": "Hz",
            "power_density": "unit²/Hz", "amplitude_deg": "deg",
            "peak_velocity_deg_s": "deg/s", "peak_acceleration_deg_s2": "deg/s²",
            "estimate": None, "value": None, "correlation": "r", "coverage": "proportion",
            "proportion": "proportion", "dwell_s": "s", "n_members": "participants",
        }
        if column in known:
            if known[column] is not None:
                return known[column]
        if column.startswith("n_") or column == "_row_index":
            return "count"
        for suffix, unit in (
            ("_frequency_hz", "Hz"), ("_hz", "Hz"), ("_deg_s2", "deg/s²"),
            ("_deg_s", "deg/s"), ("_deg", "deg"), ("_s", "s"),
        ):
            if column.endswith(suffix):
                return unit
        if "unit" in frame:
            units = _unique_values(frame, "unit")
            if len(units) > 1:
                raise ValueError("所选数据包含多个单位；请先筛选为单一单位。")
            if units:
                return units[0]
        return None

    def _axis_label(self, frame: pd.DataFrame, column: str) -> str:
        unit = self._unit(frame, column)
        if any(token in column.casefold() for token in ("gaze_x", "gaze_y", "target_x", "target_y", "xmin", "xmax", "ymin", "ymax")):
            unit = self._semantic_value(frame, column, "coordinate_unit")
        return f"{column} [{unit}]" if unit else column

    def _validate_coordinate_axes(self, frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
        coordinate_names = ("gaze_x", "gaze_y", "target_x", "target_y", "xmin", "xmax", "ymin", "ymax")
        if any(any(token in column.casefold() for token in coordinate_names) for column in columns):
            self._semantic_value(frame, columns[0], "coordinate_unit")
            self._semantic_value(frame, columns[0], "coordinate_frame")

    def _check_single_scope(self, frame: pd.DataFrame) -> None:
        multiple = [(column, len(values)) for column in _ID_COLUMNS
                    if len(values := _unique_values(frame, column)) > 1]
        if multiple:
            labels = ", ".join(f"{column}={count}" for column, count in multiple[:3])
            raise ValueError("当前表包含多个分析 scope（" + labels + "）；请使用表格的组合筛选选择单一对象。")

    @staticmethod
    def _plot_slice(frame: pd.DataFrame) -> pd.DataFrame:
        if len(frame) <= _MAX_PLOT_ROWS:
            return frame
        positions = np.linspace(0, len(frame) - 1, _MAX_PLOT_ROWS, dtype=int)
        return frame.iloc[positions]

    def draw(self) -> None:
        frame = self._frame
        if not self._table_name or not isinstance(frame, pd.DataFrame) or frame.empty:
            self._show_message("没有可绘制的数据", "该运行没有返回此表的行；空表不会生成示意线或零值。")
            return
        key = self._table_name.casefold()
        try:
            if "heatmap_grid" in key and {"row", "column", "dwell_s"}.issubset(frame.columns):
                self._draw_heatmap(frame)
            elif "mainsequence" in key and {"amplitude_deg", "peak_velocity_deg_s", "duration_s"}.issubset(frame.columns):
                self._draw_main_sequence(frame)
            elif "pupil_psd" in key and {"frequency_hz", "power_density"}.issubset(frame.columns):
                self._draw_psd(frame)
            elif "perclos" in key and {"start_s", "end_s", "closed"}.issubset(frame.columns):
                self._draw_perclos(frame)
            elif "pursuit_series" in key and {"start_s", "end_s"}.issubset(frame.columns):
                self._draw_pursuit(frame)
            elif "lag_curve" in key and {"lag_s", "correlation"}.issubset(frame.columns):
                self._draw_lag(frame)
            elif "statistics" in key and {"estimate", "ci_lower", "ci_upper"}.issubset(frame.columns):
                self._draw_estimates(frame)
            elif "aoi" in key and self._has_aoi_geometry(frame):
                self._draw_aois(frame)
            elif "event" in key and {"start_s", "end_s"}.issubset(frame.columns):
                self._draw_events(frame)
            elif "majority_support" in key and {"time_s", "n_members"}.issubset(frame.columns):
                self._draw_nt(frame)
            elif "metrics_group" in key and {"pair_id", "metric_id", "value"}.issubset(frame.columns):
                self._draw_pair_matrix(frame)
            elif "metrics" in key and {"subject_id", "condition", "value"}.issubset(frame.columns):
                paired = self._try_draw_paired(frame)
                if not paired:
                    self._draw_generic(frame)
            else:
                self._draw_generic(frame)
            self.chart_available = True
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            self._show_message("暂不绘图", str(exc))

    def _annotate_data(self, ax: Any, frame: pd.DataFrame, value_column: str | None = None) -> str:
        count = len(frame)
        na_count = int(frame[value_column].isna().sum()) if value_column and value_column in frame else 0
        parts = [f"显示 {min(count, _MAX_PLOT_ROWS):,}/{count:,} 行", f"NA {na_count:,}"]
        coverage_column = next((name for name in ("coverage", "grid_coverage_proportion", "coverage_proportion") if name in frame), None)
        if coverage_column:
            numeric = pd.to_numeric(frame[coverage_column], errors="coerce").dropna()
            if len(numeric):
                parts.append(f"返回覆盖度均值 {numeric.mean():.3g}")
        reason_column = next((name for name in ("reason_code", "reason") if name in frame), None)
        if reason_column:
            reasons = frame[reason_column].dropna().astype(str)
            if len(reasons):
                top = reasons.value_counts().head(3)
                parts.append("不可用原因：" + "、".join(f"{name} ({int(n)})" for name, n in top.items()))
        note = "；".join(parts) + "。缺失值保留为 NA；时间点按实际返回顺序绘制，不跨缺口补线。"
        ax.text(0.01, -0.20, note, transform=ax.transAxes, ha="left", va="top", fontsize=8,
                color="#53647a", wrap=True)
        return note

    def _draw_generic(self, frame: pd.DataFrame) -> None:
        x_name = str(self.x_column.currentData() or "")
        y_name = str(self.y_column.currentData() or "")
        if x_name not in frame or y_name not in frame:
            numeric = [str(c) for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c].dtype)]
            if len(numeric) < 1:
                raise ValueError("此表没有可用数值字段；表中状态和原因仍可在数据表查看。")
            x_name = "_row_index"
            frame = frame.assign(_row_index=np.arange(len(frame), dtype=float))
            y_name = numeric[0]
        self._validate_coordinate_axes(frame, (x_name, y_name))
        unit = self._unit(frame, y_name)
        group_name = str(self.group_column.currentData() or "")
        data = self._plot_slice(frame)
        x = pd.to_numeric(data[x_name], errors="coerce") if x_name in data else pd.Series(np.arange(len(data)), index=data.index)
        y = pd.to_numeric(data[y_name], errors="coerce")
        valid = np.isfinite(x.to_numpy(dtype=float, na_value=np.nan)) & np.isfinite(y.to_numpy(dtype=float, na_value=np.nan))
        if not valid.any():
            raise ValueError(f"字段 {x_name} / {y_name} 没有有限数值；NA 与失败状态不会替换成 0。")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        mode = str(self.mode.currentData() or "scatter")
        groups: list[tuple[str, pd.DataFrame]] = [("", data)]
        if group_name and group_name in data:
            labels = data[group_name].fillna("NA").astype(str)
            values = labels.drop_duplicates().tolist()
            if len(values) > _MAX_SERIES:
                raise ValueError(f"分组含 {len(values)} 个类别，超过安全图例上限 {_MAX_SERIES}；请先筛选范围。")
            groups = [(value, data.loc[labels.eq(value)]) for value in values]
        plotted = 0
        for label, group in groups:
            gx = pd.to_numeric(group[x_name], errors="coerce") if x_name in group else pd.Series(np.arange(len(group)), index=group.index)
            gy = pd.to_numeric(group[y_name], errors="coerce")
            good = np.isfinite(gx.to_numpy(dtype=float, na_value=np.nan)) & np.isfinite(gy.to_numpy(dtype=float, na_value=np.nan))
            if not good.any():
                continue
            gxv, gyv = gx.to_numpy(dtype=float)[good], gy.to_numpy(dtype=float)[good]
            order = np.argsort(gxv, kind="stable") if mode == "line" else np.arange(len(gxv))
            if mode == "line":
                # Draw observations only: a time gap is explicitly broken using its
                # observed spacing and never filled with interpolated values.
                sx, sy = gxv[order], gyv[order]
                if len(sx) > 2:
                    delta = np.diff(sx)
                    positive = delta[delta > 0]
                    median = float(np.median(positive)) if len(positive) else None
                    if median and median > 0:
                        gap = np.flatnonzero(delta > 3.0 * median)
                        for position in gap[::-1]:
                            sx = np.insert(sx, position + 1, np.nan)
                            sy = np.insert(sy, position + 1, np.nan)
                ax.plot(sx, sy, marker=".", markersize=3, linewidth=1.0, label=label or y_name)
            else:
                ax.scatter(gxv, gyv, s=18, alpha=0.72, label=label or y_name)
            plotted += int(good.sum())
        if not plotted:
            raise ValueError("没有可绘制的有限数据点。")
        ax.set_title(f"{self._table_name} · {y_name}")
        ax.set_xlabel(self._axis_label(frame, x_name))
        ax.set_ylabel(self._axis_label(frame, y_name) if unit is None or any(
            token in y_name.casefold() for token in ("gaze_x", "gaze_y", "target_x", "target_y", "xmin", "xmax", "ymin", "ymax")
        ) else f"{y_name} [{unit}]" if unit else y_name)
        ax.grid(True, alpha=0.22)
        if len(groups) > 1:
            ax.legend(loc="best", fontsize=8)
        note = self._annotate_data(ax, frame, y_name)
        self.canvas.draw_idle()
        self.notice.setText(note)

    def _draw_heatmap(self, frame: pd.DataFrame) -> None:
        self._check_single_scope(frame)
        unit = self._semantic_value(frame, "heatmap coordinates", "coordinate_unit")
        coordinate_frame = self._semantic_value(frame, "heatmap coordinates", "coordinate_frame")
        grid = frame.copy()
        for column in ("row", "column", "dwell_s"):
            grid[column] = pd.to_numeric(grid[column], errors="coerce")
        grid = grid.dropna(subset=["row", "column", "dwell_s"])
        if grid.empty:
            raise ValueError("heatmap_grid 没有有效 row/column/dwell_s 行。")
        matrix = grid.pivot_table(index="row", columns="column", values="dwell_s", aggfunc="first")
        if matrix.size > 3600:
            raise ValueError("heatmap 网格超过 60×60 安全显示范围；原始表仍可查看。")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        x_bounds = pd.to_numeric(grid.get("xmin", pd.Series(dtype=float)), errors="coerce")
        x_end = pd.to_numeric(grid.get("xmax", pd.Series(dtype=float)), errors="coerce")
        y_bounds = pd.to_numeric(grid.get("ymin", pd.Series(dtype=float)), errors="coerce")
        y_end = pd.to_numeric(grid.get("ymax", pd.Series(dtype=float)), errors="coerce")
        extent = None
        if x_bounds.notna().any() and x_end.notna().any() and y_bounds.notna().any() and y_end.notna().any():
            extent = (float(x_bounds.min()), float(x_end.max()), float(y_bounds.min()), float(y_end.max()))
        image = ax.imshow(matrix.to_numpy(dtype=float), origin="lower", aspect="auto", interpolation="nearest",
                          extent=extent, cmap="viridis")
        colorbar = fig.colorbar(image, ax=ax)
        colorbar.set_label("dwell_s [s]")
        ax.set_title(f"{self._table_name} · 时间加权停留时长")
        ax.set_xlabel(f"X [{unit}] · {coordinate_frame}")
        ax.set_ylabel(f"Y [{unit}] · {coordinate_frame}")
        cov_text = ""
        if "grid_coverage_proportion" in grid:
            values = pd.to_numeric(grid["grid_coverage_proportion"], errors="coerce").dropna()
            if len(values):
                cov_text = f"网格内覆盖度 {values.iloc[0]:.3g}"
        ax.text(0.01, -0.20, f"来源={self._table_name}；{len(grid):,} 个返回网格；{cov_text}。颜色只表示表内 dwell_s。",
                transform=ax.transAxes, fontsize=8, color="#53647a")
        self.canvas.draw_idle()
        self.notice.setText(f"空间语义来自实际元数据：{unit} / {coordinate_frame}。无有效网格时不会绘制。")

    def _draw_main_sequence(self, points: pd.DataFrame) -> None:
        self._check_single_scope(points)
        fits_name = next((name for name in self._tables if "mainsequence_fits" in name.casefold()), None)
        if not fits_name:
            raise ValueError("运行没有返回 advanced_mainsequence_fits；只显示表格，不拟合或补造曲线。")
        fits = self._tables[fits_name].copy()
        for column in _ID_COLUMNS:
            if column in points and column in fits:
                values = _unique_values(points, column)
                if len(values) == 1:
                    fits = fits.loc[fits[column].astype("string").eq(values[0]).fillna(False)]
                elif len(values) > 1:
                    raise ValueError(f"点表含多个 {column}，请先筛选为单一对象。")
        fig = self.figure
        fig.clear()
        velocity_ax, duration_ax = fig.subplots(1, 2)
        numeric = points.copy()
        for column in ("amplitude_deg", "peak_velocity_deg_s", "duration_s"):
            numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
        valid_v = np.isfinite(numeric["amplitude_deg"]) & np.isfinite(numeric["peak_velocity_deg_s"])
        valid_d = np.isfinite(numeric["amplitude_deg"]) & np.isfinite(numeric["duration_s"])
        velocity_ax.scatter(numeric.loc[valid_v, "amplitude_deg"], numeric.loc[valid_v, "peak_velocity_deg_s"],
                            s=18, alpha=0.72, label="返回的扫视点")
        duration_ax.scatter(numeric.loc[valid_d, "amplitude_deg"], numeric.loc[valid_d, "duration_s"],
                            s=18, alpha=0.72, label="返回的扫视点")
        curve_x = np.linspace(float(numeric["amplitude_deg"].min()), float(numeric["amplitude_deg"].max()), 200)
        if len(curve_x) and np.isfinite(curve_x).all():
            for model, rows in fits.groupby("model", sort=False, dropna=False) if "model" in fits else []:
                parameter = {
                    str(row.get("parameter")): pd.to_numeric(pd.Series([row.get("value")]), errors="coerce").iloc[0]
                    for row in rows.to_dict("records")
                    if str(row.get("status", "complete")) == "complete"
                }
                if str(model).startswith("v=") and np.isfinite(parameter.get("vmax", np.nan)) and np.isfinite(parameter.get("c", np.nan)) and parameter["c"] > 0:
                    y = parameter["vmax"] * (-np.expm1(-curve_x / parameter["c"]))
                    velocity_ax.plot(curve_x, y, label=str(model))
                elif str(model).startswith("duration=") and np.isfinite(parameter.get("duration_intercept", np.nan)) and np.isfinite(parameter.get("duration_slope", np.nan)):
                    y = parameter["duration_intercept"] + parameter["duration_slope"] * curve_x
                    duration_ax.plot(curve_x, y, label=str(model))
        velocity_ax.set_title("速度主序")
        velocity_ax.set_xlabel("amplitude_deg [deg]")
        velocity_ax.set_ylabel("peak_velocity_deg_s [deg/s]")
        duration_ax.set_title("时长模型")
        duration_ax.set_xlabel("amplitude_deg [deg]")
        duration_ax.set_ylabel("duration_s [s]")
        for ax in (velocity_ax, duration_ax):
            ax.grid(True, alpha=0.22)
            ax.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()
        self.notice.setText("散点来自扫视数据；曲线只使用状态为“已完成”的参数，不重新拟合。参数置信区间不代表预测范围。")

    def _draw_psd(self, frame: pd.DataFrame) -> None:
        self._check_single_scope(frame)
        data = frame.copy()
        for column in ("frequency_hz", "power_density"):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        data = data.dropna(subset=["frequency_hz", "power_density"])
        if data.empty:
            raise ValueError("advanced_pupil_psd 没有有限频率和功率密度。")
        units = _unique_values(data, "unit")
        if len(units) > 1:
            raise ValueError("PSD 明细含多个功率密度单位；请筛选为单一 eye/unit。")
        groups = [("", data)]
        group_col = next((column for column in ("block_id", "eye") if column in data and data[column].nunique(dropna=False) > 1), None)
        if group_col:
            groups = [(str(label), group) for label, group in data.groupby(group_col, sort=False, dropna=False)]
        if len(groups) > _MAX_SERIES:
            raise ValueError("PSD block 数超过图例安全上限；请先筛选到少量 block。")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        for label, group in groups:
            x = group["frequency_hz"].to_numpy(dtype=float)
            y = group["power_density"].to_numpy(dtype=float)
            order = np.argsort(x, kind="stable")
            ax.plot(x[order], y[order], marker=".", markersize=3, linewidth=1.0, label=label or None)
        ax.set_title(f"{self._table_name} · Welch PSD")
        ax.set_xlabel("frequency_hz [Hz]")
        ax.set_ylabel(f"power_density [{units[0] if units else 'declared density unit'}]")
        ax.grid(True, alpha=0.22)
        if len(groups) > 1:
            ax.legend(loc="best", fontsize=8)
        self._annotate_data(ax, data, "power_density")
        self.canvas.draw_idle()
        self.notice.setText("横轴为频率，纵轴为谱功率密度；不同数据段不连线。")

    def _draw_lag(self, frame: pd.DataFrame) -> None:
        data = frame.copy()
        data["lag_s"] = pd.to_numeric(data["lag_s"], errors="coerce")
        data["correlation"] = pd.to_numeric(data["correlation"], errors="coerce")
        data = data.dropna(subset=["lag_s", "correlation"])
        if data.empty:
            raise ValueError("lag_curve 没有有限的 lag_s/correlation 返回值。")
        group_col = next((name for name in ("pair_id", "signal", "role") if name in data and data[name].nunique(dropna=False) > 1), None)
        groups = [("", data)] if not group_col else [(str(label), group) for label, group in data.groupby(group_col, sort=False, dropna=False)]
        if len(groups) > _MAX_SERIES:
            raise ValueError("lag 曲线分组过多，请用表格筛选少量 pair/signal。")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        for label, group in groups:
            ordered = group.sort_values("lag_s", kind="mergesort")
            ax.plot(ordered["lag_s"], ordered["correlation"], marker=".", markersize=3,
                    linewidth=1.0, label=label or None)
        ax.axhline(0.0, color="#667085", linewidth=0.8)
        ax.set_title(f"{self._table_name} · lag correlation")
        ax.set_xlabel("lag_s [s]")
        ax.set_ylabel("correlation [r]")
        ax.set_ylim(-1.05, 1.05)
        ax.grid(True, alpha=0.22)
        if len(groups) > 1:
            ax.legend(loc="best", fontsize=8)
        self._annotate_data(ax, data, "correlation")
        self.canvas.draw_idle()
        self.notice.setText("曲线使用实际延迟数据；正负延迟沿用数据定义，缺失点不连线。")

    def _draw_estimates(self, frame: pd.DataFrame) -> None:
        data = frame.copy()
        if "status" in data:
            data = data.loc[data["status"].astype("string").eq("complete").fillna(False)]
        for column in ("estimate", "ci_lower", "ci_upper"):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        data = data.dropna(subset=["estimate", "ci_lower", "ci_upper"])
        if data.empty:
            raise ValueError("统计表没有已完成且含估计值与置信区间的数据；无法计算的行与 NA 保留在表格。")
        if "unit" in data and len(_unique_values(data, "unit")) > 1:
            raise ValueError("统计估计含多个单位；请用 metric_id/unit 筛选单一量纲。")
        self._check_single_scope(data)
        labels = []
        for row in data.to_dict("records"):
            pieces = [str(row.get(name)) for name in ("metric_id", "contrast", "outcome", "condition") if row.get(name) not in (None, "")]
            labels.append(" · ".join(pieces) or str(row.get("hypothesis_id", len(labels) + 1)))
        positions = np.arange(len(data))
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        center = data["estimate"].to_numpy(dtype=float)
        lower = data["ci_lower"].to_numpy(dtype=float)
        upper = data["ci_upper"].to_numpy(dtype=float)
        ax.hlines(positions, lower, upper, color="#6f91b5", linewidth=2)
        ax.scatter(center, positions, color="#326ca8", s=28, zorder=3)
        ax.axvline(0.0, color="#667085", linewidth=0.8)
        ax.set_yticks(positions, labels)
        ax.invert_yaxis()
        unit = _unique_values(data, "unit")
        ax.set_xlabel(f"estimate [{unit[0]}]" if unit else "estimate")
        ax.set_title(f"{self._table_name} · 返回估计与区间")
        ax.grid(True, axis="x", alpha=0.22)
        self.canvas.draw_idle()
        self.notice.setText("只显示完整估计值及置信区间；没有值或无法计算的行不画点，可在表格查看原因。")

    def _has_aoi_geometry(self, frame: pd.DataFrame) -> bool:
        return {"xmin", "xmax", "ymin", "ymax"}.issubset(frame.columns) or "vertices" in frame.columns

    def _draw_aois(self, frame: pd.DataFrame) -> None:
        self._check_single_scope(frame)
        unit = self._semantic_value(frame, "AOI coordinates", "coordinate_unit")
        coordinate_frame = self._semantic_value(frame, "AOI coordinates", "coordinate_frame")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        drawn = 0
        if {"xmin", "xmax", "ymin", "ymax"}.issubset(frame.columns):
            from matplotlib.patches import Rectangle
            for _, row in frame.iterrows():
                values = [pd.to_numeric(pd.Series([row.get(name)]), errors="coerce").iloc[0]
                          for name in ("xmin", "xmax", "ymin", "ymax")]
                if not all(np.isfinite(values)):
                    continue
                xmin, xmax, ymin, ymax = map(float, values)
                patch = Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                                  fill=False, linewidth=1.2, label=str(row.get("aoi_id", "AOI")))
                ax.add_patch(patch)
                ax.text((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, str(row.get("aoi_id", "")),
                        ha="center", va="center", fontsize=8)
                drawn += 1
        if drawn == 0 and "vertices" in frame:
            from matplotlib.patches import Polygon
            for _, row in frame.iterrows():
                vertices = row.get("vertices")
                try:
                    polygon = np.asarray(vertices, dtype=float)
                except (TypeError, ValueError):
                    continue
                if polygon.ndim != 2 or polygon.shape[1] < 2 or len(polygon) < 3 or not np.isfinite(polygon[:, :2]).all():
                    continue
                ax.add_patch(Polygon(polygon[:, :2], closed=True, fill=False, linewidth=1.2,
                                     label=str(row.get("aoi_id", "AOI"))))
                drawn += 1
        if not drawn:
            raise ValueError("AOI 表没有可绘制的有限矩形或顶点。")
        ax.autoscale_view()
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xlabel(f"X [{unit}] · {coordinate_frame}")
        ax.set_ylabel(f"Y [{unit}] · {coordinate_frame}")
        ax.set_title(f"{self._table_name} · AOI 边界")
        ax.grid(True, alpha=0.22)
        self.canvas.draw_idle()
        self.notice.setText(f"仅绘制输入表中的 {drawn} 个 AOI 边界；不推断缺失 geometry。")

    def _draw_events(self, frame: pd.DataFrame) -> None:
        data = frame.copy()
        data["start_s"] = pd.to_numeric(data["start_s"], errors="coerce")
        data["end_s"] = pd.to_numeric(data["end_s"], errors="coerce")
        data = data.dropna(subset=["start_s", "end_s"])
        if data.empty:
            raise ValueError("事件表没有可绘制的实际 start_s/end_s 区间。")
        if len(data) > _MAX_PLOT_ROWS:
            data = self._plot_slice(data)
        group_col = "event_type" if "event_type" in data else None
        labels = data[group_col].fillna("NA").astype(str) if group_col else pd.Series(["event"] * len(data), index=data.index)
        types = labels.drop_duplicates().tolist()
        if len(types) > _MAX_SERIES:
            raise ValueError("事件类型超过安全图例上限；请先筛选事件类型。")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        y_map = {label: index for index, label in enumerate(types)}
        for index, (_, row) in enumerate(data.iterrows()):
            label = labels.iloc[index]
            ax.hlines(y_map[label], row["start_s"], row["end_s"], linewidth=3, alpha=0.8,
                      label=label if label not in getattr(self, "_event_legend_seen", set()) else None)
            if not hasattr(self, "_event_legend_seen"):
                self._event_legend_seen = set()
            self._event_legend_seen.add(label)
        ax.set_yticks(list(y_map.values()), list(y_map.keys()))
        ax.set_xlabel("time [s]")
        ax.set_title(f"{self._table_name} · 实际事件区间")
        ax.grid(True, axis="x", alpha=0.22)
        self.canvas.draw_idle()
        self.notice.setText(f"显示 {len(data):,} 条实际事件区间；失败/无效事件仍可在表格按 status/reason_code 检查。")
        self._event_legend_seen = set()

    def _draw_perclos(self, frame: pd.DataFrame) -> None:
        data = frame.copy()
        for column in ("start_s", "end_s"):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        data = data.dropna(subset=["start_s", "end_s"])
        if data.empty:
            raise ValueError("PERCLOS 表没有实际区间。")
        self._check_single_scope(data)
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        plotted = 0
        for row_index, (_, row) in enumerate(data.iterrows()):
            valid = row.get("valid_duration_s", None)
            if pd.isna(valid) if valid is not None else True:
                continue
            closed = row.get("closed") is True or row.get("closed") == np.bool_(True)
            ax.broken_barh([(float(row["start_s"]), float(row["end_s"] - row["start_s"]))],
                           (0.72 if closed else 0.25, 0.24),
                           facecolors="#346ea7" if closed else "#b8c6d8", edgecolors="#53647a")
            plotted += 1
        if not plotted:
            raise ValueError("没有同时含有效支持时长和区间边界的 PERCLOS 行。")
        ax.set_yticks([0.37, 0.84], ["开放/未闭合", "闭合"])
        ax.set_xlabel("time [s]")
        ax.set_title(f"{self._table_name} · 实际闭合区间")
        ax.grid(True, axis="x", alpha=0.22)
        self.canvas.draw_idle()
        self.notice.setText("区间和 closed 状态来自 advanced_perclos_intervals；coverage 与 reason 保留在表格。")

    def _draw_pursuit(self, frame: pd.DataFrame) -> None:
        self._check_single_scope(frame)
        data = frame.copy()
        for column in ("start_s", "end_s", "gain"):
            if column in data:
                data[column] = pd.to_numeric(data[column], errors="coerce")
        y_name = "gain" if "gain" in data else next((name for name in ("target_speed", "mean_squared_position_error") if name in data), None)
        if not y_name:
            raise ValueError("追踪数据缺少可绘制的增益、目标速度或误差值。")
        data = data.dropna(subset=["start_s", "end_s", y_name])
        if data.empty:
            raise ValueError("追踪序列表没有有效的实际区间和值。")
        x = (data["start_s"] + data["end_s"]) / 2.0
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        ax.scatter(x, data[y_name], s=28, alpha=0.8)
        ax.set_xlabel("interval midpoint [s]")
        unit = {"gain": "ratio", "target_speed": "declared target speed unit", "mean_squared_position_error": "coordinate_unit²"}.get(y_name)
        ax.set_ylabel(f"{y_name} [{unit}]" if unit else y_name)
        ax.set_title(f"{self._table_name} · 返回的追踪区间")
        ax.grid(True, alpha=0.22)
        self.canvas.draw_idle()
        self.notice.setText("每点对应一个实际 pursuit interval；区间之间不连线，也不外推目标轨迹。")

    def _draw_nt(self, frame: pd.DataFrame) -> None:
        self._check_single_scope(frame)
        data = frame.copy()
        data["time_s"] = pd.to_numeric(data["time_s"], errors="coerce")
        data["n_members"] = pd.to_numeric(data["n_members"], errors="coerce")
        data = data.dropna(subset=["time_s", "n_members"])
        if data.empty:
            raise ValueError("majority_support 表没有实际 time_s/n_members。")
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        ax.scatter(data["time_s"], data["n_members"], s=22, alpha=0.8)
        ax.set_xlabel("time_s [s]")
        ax.set_ylabel("n_members [participants]")
        ax.set_title(f"{self._table_name} · n(t)")
        ax.grid(True, alpha=0.22)
        self.canvas.draw_idle()
        self.notice.setText("多人支持度逐点显示；点间不插值，一致程度可在表格查看。")

    def _draw_pair_matrix(self, frame: pd.DataFrame) -> None:
        data = frame.copy()
        for required in ("pair_id", "metric_id", "value"):
            if required not in data:
                raise ValueError("配对矩阵需要实际 pair_id/metric_id/value 列。")
        if "unit" in data and len(_unique_values(data, "unit")) > 1:
            raise ValueError("配对结果包含多个单位；请筛选到一个单位后查看矩阵。")
        data["value"] = pd.to_numeric(data["value"], errors="coerce")
        matrix = data.pivot_table(index="pair_id", columns="metric_id", values="value", aggfunc="first", dropna=False)
        if matrix.empty or matrix.size > 400:
            raise ValueError("配对矩阵为空或超过 20×20 安全范围；具体状态仍在结果表。")
        units = _unique_values(data, "unit")
        if units == ["r"]:
            vmin, vmax = -1.0, 1.0
        else:
            vals = matrix.to_numpy(dtype=float)
            finite = vals[np.isfinite(vals)]
            if not len(finite):
                raise ValueError("配对表没有有限值，NA 不以零显示。")
            vmin, vmax = float(np.min(finite)), float(np.max(finite))
            if vmin == vmax:
                pad = max(abs(vmin) * 0.05, 0.5)
                vmin, vmax = vmin - pad, vmax + pad
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        values = np.ma.masked_invalid(matrix.to_numpy(dtype=float))
        image = ax.imshow(values, aspect="auto", interpolation="nearest", cmap="coolwarm", vmin=vmin, vmax=vmax)
        fig.colorbar(image, ax=ax, label=units[0] if units else "unit")
        ax.set_xticks(np.arange(len(matrix.columns)), matrix.columns.astype(str), rotation=35, ha="right")
        ax.set_yticks(np.arange(len(matrix.index)), matrix.index.astype(str))
        ax.set_title(f"{self._table_name} · 配对指标矩阵")
        for row in range(matrix.shape[0]):
            for column in range(matrix.shape[1]):
                if pd.isna(matrix.iat[row, column]):
                    ax.text(column, row, "NA", ha="center", va="center", fontsize=8,
                            color="#48566a", bbox={"facecolor": "#edf0f4", "edgecolor": "none", "alpha": 0.8})
        self.canvas.draw_idle()
        self.notice.setText("矩阵由真实 pair_id × metric_id 返回值透视；NA 保持遮罩并标记，不替换为 0。")

    def _try_draw_paired(self, frame: pd.DataFrame) -> bool:
        data = frame.copy()
        if "unit" in data and len(_unique_values(data, "unit")) > 1:
            raise ValueError("配对值包含不同单位，请先用筛选选择单一 metric/unit。")
        if "metric_id" in data and data["metric_id"].nunique(dropna=False) > 1:
            return False
        conditions = data["condition"].dropna().drop_duplicates().tolist()
        if len(conditions) != 2:
            return False
        data["value"] = pd.to_numeric(data["value"], errors="coerce")
        if data.duplicated(["subject_id", "condition"]).any():
            return False
        pivot = data.pivot(index="subject_id", columns="condition", values="value").dropna(subset=conditions)
        if pivot.empty or len(pivot) > 500:
            return False
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot(111)
        for subject, row in pivot.iterrows():
            ax.plot([0, 1], [row[conditions[0]], row[conditions[1]]], marker="o", linewidth=0.9,
                    alpha=0.65, label=str(subject) if len(pivot) <= 8 else None)
        ax.set_xticks([0, 1], [str(item) for item in conditions])
        units = _unique_values(data, "unit")
        ax.set_ylabel(f"value [{units[0]}]" if units else "value")
        ax.set_title(f"{self._table_name} · 实际完整配对观测（N={len(pivot)}）")
        ax.grid(True, axis="y", alpha=0.22)
        if len(pivot) <= 8:
            ax.legend(loc="best", fontsize=7)
        self.canvas.draw_idle()
        self.notice.setText("仅连结同一 subject_id 在两个 condition 下各一条的实际返回值；不聚合、不补缺。")
        return True

