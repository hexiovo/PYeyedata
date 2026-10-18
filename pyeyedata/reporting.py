"""Static result figures and paginated PDF reports bound to RunRecord data.

Only values saved in an immutable RunRecord are used. This module does not
repeat analyses, estimate confidence intervals, or infer device validity.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import date, datetime
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re
import os
import tempfile
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from pyeyedata.gui.models import RunRecord
from pyeyedata.exports import _missing, _safe_value, _to_text


FigureKind = Literal["scatter", "interval"]
FigureFormat = Literal["png", "svg", "pdf"]

_OKABE_ITO = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#F0E442", "#000000")
_REPORT_MAX_COLUMNS_PER_PANEL = 6
_DEFAULT_REPORT_TABLE_LIMIT = 100
_REPORT_CELL_TEXT_LIMIT = 512
_REPORT_CELL_PREVIEW_CHARS = 240
_MAX_DEFAULT_FIGURE_CATEGORIES = 36


class ReportingError(ValueError):
    """A figure or report cannot preserve the requested record meaning."""


@dataclass(frozen=True, slots=True)
class FigureSpec:
    """An immutable display request for values in one named result table."""

    run_id: str
    revision_id: str
    config_fingerprint: str
    table_name: str
    kind: FigureKind
    x_column: str | None
    y_column: str
    x_unit: str
    y_unit: str
    group_column: str | None = None
    category_column: str | None = None
    lower_column: str | None = None
    upper_column: str | None = None
    coverage_column: str | None = None
    source_label: str | None = None


@dataclass(frozen=True, slots=True)
class FigureArtifact:
    member_name: str
    media_type: str
    file_format: FigureFormat | Literal["report_pdf"]
    data: bytes
    sha256: str
    byte_length: int
    provenance: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ArchiveArtifactBundle:
    artifacts: tuple[FigureArtifact, ...]
    omissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FigureExportResult:
    formats: Mapping[str, bytes]
    sha256_by_format: Mapping[str, str]
    provenance: Mapping[str, Any]


def _figure_spec_provenance(spec: FigureSpec) -> dict[str, Any]:
    """Return a reversible, field-based audit record for our controlled spec."""
    return {
        "figure_spec": _safe_value(asdict(spec)),
        "selection": _safe_value({
            "kind": spec.kind,
            "x_column": spec.x_column,
            "y_column": spec.y_column,
            "group_column": spec.group_column,
            "category_column": spec.category_column,
            "lower_column": spec.lower_column,
            "upper_column": spec.upper_column,
            "coverage_column": spec.coverage_column,
        }),
        "source_tables": [spec.table_name],
        "source_label": spec.source_label or spec.table_name,
        "units": {"x": spec.x_unit, "y": spec.y_unit},
        "confidence_interval_columns": {
            "lower": spec.lower_column,
            "upper": spec.upper_column,
            "coverage": spec.coverage_column,
        },
    }


def _validate_provenance_value(value: Any, path: str = "selection") -> None:
    """Reject arbitrary objects instead of storing their repr as audit data."""
    if value is None or value is pd.NA or value is pd.NaT:
        return
    if isinstance(value, np.generic):
        _validate_provenance_value(value.item(), path)
        return
    if isinstance(value, (str, bool, int, float, Decimal, bytes, bytearray, date, datetime, pd.Timestamp)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReportingError(f"{path} keys must be strings")
            _validate_provenance_value(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _validate_provenance_value(item, f"{path}[{index}]")
        return
    if _missing(value):
        return
    raise ReportingError(f"{path} contains unsupported provenance value type {type(value).__qualname__}")


@dataclass(frozen=True, slots=True)
class ReportRequest:
    run_id: str
    revision_id: str
    config_fingerprint: str
    title: str
    table_names: tuple[str, ...]
    max_rows_per_table: int
    figure_specs: tuple[FigureSpec, ...]
    omitted_rows_note: str


@dataclass(frozen=True, slots=True)
class ReportResult:
    path: Path
    sha256: str
    byte_length: int


def create_figure_spec(
    record: RunRecord,
    table_name: str,
    *,
    kind: FigureKind,
    y_column: str,
    y_unit: str,
    x_column: str | None = None,
    x_unit: str = "未记录",
    group_column: str | None = None,
    category_column: str | None = None,
    lower_column: str | None = None,
    upper_column: str | None = None,
    coverage_column: str | None = None,
    source_label: str | None = None,
) -> FigureSpec:
    """Bind a plot selection to the current run/table before dispatch."""
    if table_name not in record.table_names():
        raise ReportingError(f"run {record.run_id!r} has no table named {table_name!r}")
    if kind not in ("scatter", "interval"):
        raise ReportingError("figure kind must be 'scatter' or 'interval'")
    frame = record.table_copy(table_name)
    required = [y_column]
    if kind == "scatter":
        if not x_column:
            raise ReportingError("scatter figures require x_column")
        required.append(x_column)
    else:
        if not category_column or not lower_column or not upper_column:
            raise ReportingError("interval figures require category_column, lower_column and upper_column")
        required.extend([category_column, lower_column, upper_column])
    if group_column:
        required.append(group_column)
    if coverage_column:
        required.append(coverage_column)
    absent = [name for name in required if name not in frame.columns]
    if absent:
        raise ReportingError(f"figure columns are absent from {table_name!r}: {', '.join(absent)}")
    return FigureSpec(
        str(record.run_id), str(record.revision_id), str(record.config_fingerprint), str(table_name),
        kind, x_column, str(y_column), str(x_unit or "未记录"), str(y_unit or "未记录"),
        group_column, category_column, lower_column, upper_column, coverage_column, source_label,
    )


def _verify_figure_spec(record: RunRecord, spec: FigureSpec) -> pd.DataFrame:
    if (str(record.run_id), str(record.revision_id), str(record.config_fingerprint)) != (
        spec.run_id, spec.revision_id, spec.config_fingerprint,
    ):
        raise ReportingError("figure specification belongs to a different run revision or configuration")
    if spec.table_name not in record.table_names():
        raise ReportingError(f"figure source table {spec.table_name!r} is unavailable")
    frame = record.table_copy(spec.table_name)
    required = [spec.y_column]
    if spec.kind == "scatter":
        if not spec.x_column:
            raise ReportingError("scatter figures require x_column")
        required.append(spec.x_column)
    elif spec.kind == "interval":
        required.extend([str(spec.category_column), str(spec.lower_column), str(spec.upper_column)])
    else:
        raise ReportingError(f"unsupported figure kind {spec.kind!r}")
    if spec.group_column:
        required.append(spec.group_column)
    if spec.coverage_column:
        required.append(spec.coverage_column)
    absent = [column for column in required if column not in frame.columns]
    if absent:
        raise ReportingError(f"figure columns are absent from {spec.table_name!r}: {', '.join(absent)}")
    return frame


def _number_series(frame: pd.DataFrame, column: str) -> pd.Series:
    try:
        return pd.to_numeric(frame[column], errors="coerce")
    except (TypeError, ValueError) as exc:
        raise ReportingError(f"column {column!r} is not a numeric result field") from exc


def _format_numeric(value: float) -> str:
    return format(float(value), ".17g")


def _record_status(record: RunRecord) -> str:
    metadata = record.metadata()
    parts = [f"record.status={record.status}"]
    for key in ("execution_status", "analysis_status", "overall_status", "gui_effective_status"):
        value = metadata.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={_to_text(value)}")
    return "；".join(parts)


def _row_status_labels(frame: pd.DataFrame) -> tuple[str, ...]:
    labels: list[str] = []
    for column in ("status", "availability_status", "execution_status"):
        if column in frame.columns:
            values = frame[column].dropna().astype(str).unique().tolist()
            if values:
                labels.append(f"{column}=" + ", ".join(values))
    return tuple(labels)


def build_figure(record: RunRecord, spec: FigureSpec):
    """Create a genuine Matplotlib Figure from stored table values, never a widget screenshot."""
    from matplotlib.figure import Figure

    frame = _verify_figure_spec(record, spec)
    figure = Figure(figsize=(8.0, 4.6), constrained_layout=True)
    ax = figure.add_subplot(111)
    row_count = len(frame)
    if spec.kind == "scatter":
        assert spec.x_column is not None
        x_values = _number_series(frame, spec.x_column).to_numpy(dtype=float, na_value=np.nan)
        y_values = _number_series(frame, spec.y_column).to_numpy(dtype=float, na_value=np.nan)
        valid = np.isfinite(x_values) & np.isfinite(y_values)
        if not bool(valid.any()):
            raise ReportingError(f"{spec.x_column!r}/{spec.y_column!r} has no finite returned values")
        if spec.group_column:
            labels = frame[spec.group_column].map(lambda item: "NA" if _missing(item) else str(item)).to_numpy()
            categories = list(dict.fromkeys(labels.tolist()))
            if len(categories) > 12:
                raise ReportingError(f"group column {spec.group_column!r} has {len(categories)} categories; select a narrower scope")
            for index, category in enumerate(categories):
                group_mask = valid & (labels == category)
                if group_mask.any():
                    ax.scatter(x_values[group_mask], y_values[group_mask], s=22, alpha=0.78,
                               color=_OKABE_ITO[index % len(_OKABE_ITO)], label=category)
            ax.legend(loc="best", fontsize=8)
        else:
            ax.scatter(x_values[valid], y_values[valid], s=22, alpha=0.78, color=_OKABE_ITO[0])
        x_name = str(spec.x_column)
        x_range = (float(np.min(x_values[valid])), float(np.max(x_values[valid])))
        y_range = (float(np.min(y_values[valid])), float(np.max(y_values[valid])))
        valid_count = int(valid.sum())
        range_note = (
            f"X={_format_numeric(x_range[0])}..{_format_numeric(x_range[1])}; "
            f"Y={_format_numeric(y_range[0])}..{_format_numeric(y_range[1])}"
        )
        ax.set_xlabel(f"{x_name} ({spec.x_unit})")
        ax.set_ylabel(f"{spec.y_column} ({spec.y_unit})")
    else:
        category_column = str(spec.category_column)
        categories = frame[category_column].map(lambda item: "NA" if _missing(item) else str(item)).tolist()
        estimate = _number_series(frame, spec.y_column).to_numpy(dtype=float, na_value=np.nan)
        lower = _number_series(frame, str(spec.lower_column)).to_numpy(dtype=float, na_value=np.nan)
        upper = _number_series(frame, str(spec.upper_column)).to_numpy(dtype=float, na_value=np.nan)
        valid = np.isfinite(estimate) & np.isfinite(lower) & np.isfinite(upper)
        if not bool(valid.any()):
            raise ReportingError("estimate and stored interval columns have no finite rows")
        positions = np.flatnonzero(valid)
        if len(positions) > _MAX_DEFAULT_FIGURE_CATEGORIES:
            raise ReportingError(
                f"{len(positions):,} stored estimate rows exceed the one-figure label limit "
                f"({_MAX_DEFAULT_FIGURE_CATEGORIES}); choose a narrower table selection."
            )
        invalid_interval = valid & ((lower > estimate) | (estimate > upper))
        if invalid_interval.any():
            raise ReportingError("stored lower/estimate/upper values do not form ordered intervals; the report leaves them unchanged")
        yerr = np.vstack((estimate[valid] - lower[valid], upper[valid] - estimate[valid]))
        ax.errorbar(np.arange(len(positions)), estimate[valid], yerr=yerr, fmt="o",
                    color=_OKABE_ITO[0], ecolor=_OKABE_ITO[1], capsize=3, linewidth=1.1)
        ax.set_xticks(np.arange(len(positions)), [categories[position] for position in positions], rotation=35, ha="right")
        ax.set_xlabel(f"{category_column} (类别)")
        ax.set_ylabel(f"{spec.y_column} ({spec.y_unit})")
        y_range = (float(np.min(lower[valid])), float(np.max(upper[valid])))
        valid_count = int(valid.sum())
        range_note = (
            f"类别数={valid_count:,}; Y={_format_numeric(y_range[0])}..{_format_numeric(y_range[1])}"
        )
    ax.set_title(f"{spec.table_name} · {spec.y_column}")
    ax.grid(True, alpha=0.22)
    ax.margins(x=0.06, y=0.08)
    coverage = "未记录"
    if spec.coverage_column and spec.coverage_column in frame:
        values = frame[spec.coverage_column].dropna().unique().tolist()
        if len(values) == 1:
            coverage = _to_text(values[0])
        elif values:
            coverage = f"多值，详见 {spec.coverage_column} 源列"
        else:
            coverage = "列存在但无非空值"
    row_states = "；".join(_row_status_labels(frame)) or "未提供行状态列"
    source = spec.source_label or spec.table_name
    footer = (
        f"run={record.run_id} · revision={record.revision_id} · {record.status}\n"
        f"来源表={source} · 返回行={row_count:,} · 绘制有效值={valid_count:,} · 其余缺失/非有限值按源表保留\n"
        f"显示数据范围 {range_note} · coverage={coverage}\n"
        f"状态={row_states} · 图表只显示已保存值；未重新估计、插值或跨缺失段连线。"
    )
    figure.text(0.01, -0.035, footer, ha="left", va="top", fontsize=7, wrap=True)
    return figure


def _default_figure_spec(record: RunRecord) -> FigureSpec | None:
    for name in record.table_names():
        frame = record.table_copy(name)
        columns = set(str(column) for column in frame.columns)
        if {"estimate", "ci_lower", "ci_upper"}.issubset(columns):
            category = next((column for column in ("contrast", "metric_id", "term", "condition", "label")
                             if column in columns), None)
            if category is None or len(frame) > _MAX_DEFAULT_FIGURE_CATEGORIES:
                continue
            unit = "未记录"
            if "unit" in columns:
                units = frame["unit"].dropna().astype(str).unique().tolist()
                if len(units) == 1:
                    unit = units[0]
                elif len(units) > 1:
                    continue
            try:
                return create_figure_spec(
                    record, name, kind="interval", category_column=category, y_column="estimate",
                    lower_column="ci_lower", upper_column="ci_upper", y_unit=unit,
                    coverage_column="coverage" if "coverage" in columns else None,
                )
            except ReportingError:
                continue
    for name in record.table_names():
        frame = record.table_copy(name)
        columns = set(str(column) for column in frame.columns)
        if not {"metric_id", "value"}.issubset(columns) or len(frame) > _MAX_DEFAULT_FIGURE_CATEGORIES:
            continue
        unit = "未记录"
        if "unit" in columns:
            units = frame["unit"].dropna().astype(str).unique().tolist()
            if len(units) == 1:
                unit = units[0]
            elif len(units) > 1:
                continue
        try:
            return create_figure_spec(
                record, name, kind="interval", category_column="metric_id", y_column="value",
                lower_column="value", upper_column="value", y_unit=unit,
                coverage_column="coverage" if "coverage" in columns else None,
            )
        except ReportingError:
            continue
    return None


def _registered_font_context():
    """Open C03's licensed font resource for the full lazy render lifetime."""
    from contextlib import contextmanager
    from matplotlib import font_manager, rc_context
    import matplotlib
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    @contextmanager
    def _context():
        try:
            from pyeyedata.resource_paths import resource_path
        except ImportError as exc:
            raise ReportingError("C03 licensed CJK resource_path helper is not available") from exc
        with resource_path("fonts/PYeyedataCJK-Regular.ttf") as font_path:
            path = Path(font_path)
            if not path.is_file():
                raise ReportingError("licensed CJK font resource does not exist")
            font_bytes = path.read_bytes()
            font_hash = hashlib.sha256(font_bytes).hexdigest()
            font_manager.fontManager.addfont(str(path))
            family = font_manager.FontProperties(fname=str(path)).get_name()
            reportlab_name = "PYeyedataCJK_" + font_hash[:12]
            if reportlab_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(reportlab_name, str(path)))
                pdfmetrics.registerFontFamily(
                    reportlab_name, normal=reportlab_name, bold=reportlab_name,
                    italic=reportlab_name, boldItalic=reportlab_name,
                )
            with matplotlib.rc_context({
                "font.family": [family], "font.sans-serif": [family],
                "axes.unicode_minus": False,
            }):
                yield {"matplotlib_family": family, "reportlab_name": reportlab_name,
                       "sha256": font_hash, "path": path}
    return _context()


def _serialize_figure(figure: Any, formats: Sequence[FigureFormat], dpi: int,
                      provenance: Mapping[str, Any]) -> dict[str, bytes]:
    from matplotlib.figure import Figure

    if not isinstance(figure, Figure):
        raise ReportingError("figure must be a Matplotlib Figure built from actual RunRecord values")
    if not 72 <= int(dpi) <= 1200:
        raise ReportingError("figure DPI must be between 72 and 1200")
    requested = tuple(formats)
    if not requested or len(set(requested)) != len(requested) or any(value not in ("png", "svg", "pdf") for value in requested):
        raise ReportingError("formats must be a nonempty subset of PNG, SVG and figure PDF")
    provenance_text = json.dumps(_safe_value(provenance), ensure_ascii=False, sort_keys=True, allow_nan=False)
    outputs: dict[str, bytes] = {}
    for file_format in requested:
        buffer = io.BytesIO()
        title = str(provenance.get("table_name", "PYeyedata result figure"))
        if file_format == "png":
            metadata = {"Software": "PYeyedata", "Title": title, "Description": provenance_text}
        elif file_format == "svg":
            metadata = {"Creator": "PYeyedata", "Title": title, "Description": provenance_text}
        else:
            metadata = {"Creator": "PYeyedata", "Title": title, "Subject": provenance_text}
        figure.savefig(buffer, format=file_format, dpi=int(dpi), bbox_inches="tight",
                       metadata=metadata)
        outputs[file_format] = buffer.getvalue()
    return outputs


def _serialize_existing_figure_with_font(
    figure: Any,
    formats: Sequence[FigureFormat],
    dpi: int,
    provenance: Mapping[str, Any],
    font_family: str,
) -> dict[str, bytes]:
    """Apply the licensed family only while saving, preserving the live GUI Figure."""
    from matplotlib.figure import Figure
    from matplotlib.text import Text

    if not isinstance(figure, Figure):
        raise ReportingError("figure must be an existing Matplotlib Figure")
    text_artists = figure.findobj(match=Text)
    previous = [(artist, artist.get_fontproperties().copy()) for artist in text_artists]
    axes_positions = [
        (axes, axes.get_position().frozen(), axes.get_position(original=True).frozen())
        for axes in figure.axes
    ]
    try:
        for artist, _font_properties in previous:
            artist.set_fontfamily(font_family)
        return _serialize_figure(figure, formats, dpi, provenance)
    finally:
        for artist, font_properties in previous:
            artist.set_fontproperties(font_properties)
        for axes, active_position, original_position in axes_positions:
            axes.set_position(original_position, which="original")
            axes.set_position(active_position, which="active")


def render_figure_artifact(
    record: RunRecord,
    spec: FigureSpec,
    *,
    formats: Sequence[FigureFormat] = ("png", "svg", "pdf"),
    dpi: int = 300,
) -> FigureExportResult:
    """Render a data Figure to high-resolution/vector bytes under the licensed font context."""
    provenance = {
        "run_id": record.run_id, "generation": record.generation,
        "revision_id": record.revision_id, "config_fingerprint": record.config_fingerprint,
        "record_status": record.status, "artifact_scope": "this RunRecord only",
        "table_name": spec.table_name,
        "data_source": "RunRecord.table_copy; values were not reanalyzed",
        **_figure_spec_provenance(spec),
    }
    with _registered_font_context() as font:
        figure = build_figure(record, spec)
        output = _serialize_figure(figure, formats, dpi, provenance)
    return FigureExportResult(
        output, {name: hashlib.sha256(value).hexdigest() for name, value in output.items()},
        {**provenance, "font_sha256": font["sha256"], "dpi": int(dpi)},
    )


def render_existing_figure_artifact(
    record: RunRecord,
    figure: Any,
    *,
    table_name: str,
    source_label: str | None = None,
    source_tables: Sequence[str] | None = None,
    selection: Mapping[str, Any] | None = None,
    x_unit: str | None = None,
    y_unit: str | None = None,
    lower_column: str | None = None,
    upper_column: str | None = None,
    coverage_column: str | None = None,
    formats: Sequence[FigureFormat] = ("png", "svg", "pdf"),
    dpi: int = 300,
) -> FigureExportResult:
    """Serialize an existing GUI Matplotlib Figure with explicit RunRecord provenance.

    The Figure is saved as-is under the licensed font context. This function does
    not rebuild, clear, or recompute the chart. ``selection`` should describe
    the displayed table/column/filter choices using JSON-safe scalar values.
    """
    if not isinstance(table_name, str):
        raise ReportingError("table_name must be a string")
    primary_table = table_name.strip()
    if not primary_table:
        raise ReportingError("table_name must identify the RunRecord table used by the figure")
    if isinstance(source_tables, str):
        raise ReportingError("source_tables must be a sequence of table names, not one string")
    raw_names = source_tables if source_tables is not None else (primary_table,)
    if not isinstance(raw_names, Sequence):
        raise ReportingError("source_tables must be a sequence of table names")
    if any(not isinstance(name, str) for name in raw_names):
        raise ReportingError("source_tables entries must be strings")
    names = tuple(name.strip() for name in raw_names)
    if not names or any(not name for name in names):
        raise ReportingError("source_tables must contain at least one nonempty RunRecord table name")
    if len(set(names)) != len(names):
        raise ReportingError("source_tables must not contain duplicates")
    missing = [name for name in names if name not in record.table_names()]
    if missing:
        raise ReportingError(f"figure source tables are absent from this RunRecord: {', '.join(missing)}")
    if primary_table not in names:
        raise ReportingError("source_tables must include table_name")
    if selection is not None and not isinstance(selection, Mapping):
        raise ReportingError("selection must be a mapping of field names to JSON-safe values")
    selected = dict(selection or {})
    _validate_provenance_value(selected)
    for field_name, value in (
        ("source_label", source_label), ("x_unit", x_unit), ("y_unit", y_unit),
        ("lower_column", lower_column), ("upper_column", upper_column),
        ("coverage_column", coverage_column),
    ):
        if value is not None and not isinstance(value, str):
            raise ReportingError(f"{field_name} must be a string or None")
    provenance = {
        "run_id": record.run_id,
        "generation": record.generation,
        "revision_id": record.revision_id,
        "config_fingerprint": record.config_fingerprint,
        "record_status": record.status,
        "artifact_scope": "this RunRecord only",
        "artifact_source": "existing_matplotlib_figure",
        "table_name": primary_table,
        "source_tables": list(names),
        "source_label": source_label or primary_table,
        "data_source": "existing Matplotlib Figure supplied by the caller; source RunRecord tables are identified above; no analysis recomputation",
        "selection": _safe_value(selected),
        "units": {"x": x_unit, "y": y_unit},
        "confidence_interval_columns": {
            "lower": lower_column,
            "upper": upper_column,
            "coverage": coverage_column,
        },
    }
    with _registered_font_context() as font:
        output = _serialize_existing_figure_with_font(
            figure, formats, dpi, provenance, str(font["matplotlib_family"]),
        )
    safe_provenance = _safe_value({**provenance, "font_sha256": font["sha256"], "dpi": int(dpi)})
    return FigureExportResult(
        output,
        {name: hashlib.sha256(value).hexdigest() for name, value in output.items()},
        safe_provenance,
    )


def _report_tables(record: RunRecord, request: ReportRequest, reportlab_font: str):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, Spacer

    story: list[Any] = []
    body = ParagraphStyle("ReportBody", fontName=reportlab_font, fontSize=8, leading=10,
                         wordWrap="CJK", splitLongWords=1, spaceAfter=2)
    table_cell = ParagraphStyle("ReportTableCell", parent=body, fontSize=7, leading=8)
    header = ParagraphStyle("ReportTableHeader", parent=body, fontName=reportlab_font,
                            fontSize=7, leading=8, textColor=colors.white)
    small = ParagraphStyle("ReportSmall", parent=body, fontSize=7, leading=8)

    def cell_text(value: Any, *, limit: int = _REPORT_CELL_TEXT_LIMIT,
                  preview_chars: int = _REPORT_CELL_PREVIEW_CHARS) -> tuple[str, bool]:
        text = _to_text(value)
        if len(text) <= limit:
            return text, False
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return (
            f"{text[:preview_chars]}… [报告摘要；原文 {len(text):,} 字符；SHA-256={digest}]",
            True,
        )

    for table_name in request.table_names:
        if table_name not in record.table_names():
            story.append(Paragraph(f"结果表 {html.escape(table_name)} 不在此 RunRecord 中，未生成占位数据。", body))
            continue
        frame = record.table_copy(table_name)
        story.append(Spacer(1, 7))
        table_heading = Paragraph(
            f"<b>结果表：{html.escape(table_name)}</b> · 显示 {min(len(frame), request.max_rows_per_table):,}/{len(frame):,} 行",
            body,
        )
        table_heading.keepWithNext = True
        story.append(table_heading)
        if frame.empty:
            story.append(Paragraph("源结果表为空；未创建示例值或占位行。", small))
            continue
        all_columns = [str(column) for column in frame.columns]
        if len(set(all_columns)) != len(all_columns):
            story.append(Paragraph(
                "源表包含重复或文本化后重名的列；为避免错配，本报告未展示该表的行数据。", small,
            ))
            continue
        column_positions = {column: index for index, column in enumerate(all_columns)}
        groups = [all_columns[start:start + _REPORT_MAX_COLUMNS_PER_PANEL]
                  for start in range(0, len(all_columns), _REPORT_MAX_COLUMNS_PER_PANEL)]
        displayed = min(len(frame), request.max_rows_per_table)
        positions = range(displayed)
        abbreviated_cells = 0
        for group_index, columns in enumerate(groups, 1):
            if len(groups) > 1:
                group_heading = Paragraph(f"列组 {group_index}/{len(groups)} · 保留源列顺序", small)
                group_heading.keepWithNext = True
                story.append(group_heading)
            header_cells: list[Any] = []
            for column in columns:
                displayed_column, abbreviated = cell_text(column, limit=160, preview_chars=80)
                abbreviated_cells += int(abbreviated)
                header_cells.append(Paragraph(html.escape(displayed_column), header))
            data = [header_cells]
            for position in positions:
                row = frame.iloc[position]
                rendered_cells: list[Any] = []
                for column in columns:
                    displayed_value, abbreviated = cell_text(row.iloc[column_positions[column]])
                    abbreviated_cells += int(abbreviated)
                    escaped = html.escape(displayed_value).replace("\n", "<br/>")
                    rendered_cells.append(Paragraph(escaped, table_cell))
                data.append(rendered_cells)
            available_width = A4[0] - 32 * mm - 12
            widths = [available_width / max(1, len(columns))] * len(columns)
            table = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#31556f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#aebbc5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ])
            story.append(table)
        if abbreviated_cells:
            story.append(Paragraph(
                f"为保持分页可读，本表有 {abbreviated_cells:,} 个超长值仅显示摘要；摘要保留前缀、原文字符数与 SHA-256。"
                "完整值仍保留在原始运行记录及表格导出中。",
                small,
            ))
        if displayed < len(frame):
            story.append(Paragraph(
                f"本报告省略其余 {len(frame) - displayed:,} 行；未截断源记录。完整数值表见该运行档案或 CSV/XLSX 导出。",
                small,
            ))
    return story


def _pretty_json(value: Any) -> str:
    return json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, indent=2, default=str)


def _paragraph_text(value: Any) -> str:
    text = html.escape(_pretty_json(value) if isinstance(value, (Mapping, list, tuple)) else _to_text(value))
    return text.replace("\n", "<br/>")


def _choose_report_tables(record: RunRecord) -> tuple[str, ...]:
    names = tuple(record.table_names())
    preferred_tokens = ("statistic", "metrics", "availability", "quality", "design", "performance")
    preferred = [name for name in names if any(token in name.casefold() for token in preferred_tokens)]
    candidates = preferred or list(names)
    return tuple(candidates[:8])


def create_report_request(
    record: RunRecord,
    *,
    title: str = "眼动分析结果报告",
    table_names: Sequence[str] | None = None,
    max_rows_per_table: int = _DEFAULT_REPORT_TABLE_LIMIT,
    figure_specs: Sequence[FigureSpec] | None = None,
    omitted_rows_note: str = "完整数据保存在本次运行档案中。",
) -> ReportRequest:
    if max_rows_per_table < 0:
        raise ReportingError("max_rows_per_table must be zero or greater")
    selected_names = tuple(str(name) for name in (table_names if table_names is not None else _choose_report_tables(record)))
    if len(set(selected_names)) != len(selected_names):
        raise ReportingError("table_names must not contain duplicates")
    missing = [name for name in selected_names if name not in record.table_names()]
    if missing:
        raise ReportingError(f"requested report tables are missing: {', '.join(missing)}")
    for name in selected_names:
        columns = tuple(str(column) for column in record.table_copy(name).columns)
        if len(set(columns)) != len(columns):
            raise ReportingError(
                f"report table {name!r} has duplicate or string-colliding column names"
            )
    if figure_specs is None:
        suggested = _default_figure_spec(record)
        specs = () if suggested is None else (suggested,)
    else:
        specs = tuple(figure_specs)
    for spec in specs:
        _verify_figure_spec(record, spec)
    return ReportRequest(
        str(record.run_id), str(record.revision_id), str(record.config_fingerprint),
        str(title), selected_names, int(max_rows_per_table), specs, str(omitted_rows_note),
    )


def _verify_report_request(record: RunRecord, request: ReportRequest) -> None:
    if (str(record.run_id), str(record.revision_id), str(record.config_fingerprint)) != (
        request.run_id, request.revision_id, request.config_fingerprint,
    ):
        raise ReportingError("report request belongs to a different run revision or configuration")
    missing = [name for name in request.table_names if name not in record.table_names()]
    if missing:
        raise ReportingError(f"report tables are missing from this run: {', '.join(missing)}")
    for spec in request.figure_specs:
        _verify_figure_spec(record, spec)


def _build_report_pdf(record: RunRecord, request: ReportRequest, font: Mapping[str, Any],
                      figure_pngs: Sequence[tuple[str, bytes]], omissions: Sequence[str] = ()) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    _verify_report_request(record, request)
    buffer = io.BytesIO()
    status = _record_status(record)
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm, title=request.title,
        author="PYeyedata", subject=f"RunRecord {record.run_id}",
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle("RunTitle", parent=base["Title"], fontName=font["reportlab_name"],
                                 fontSize=17, leading=21, alignment=TA_CENTER, textColor=colors.HexColor("#24384a"))
    heading = ParagraphStyle("RunHeading", parent=base["Heading2"], fontName=font["reportlab_name"],
                             fontSize=12, leading=15, spaceBefore=9, spaceAfter=5, textColor=colors.HexColor("#31556f"))
    body = ParagraphStyle("RunBody", parent=base["BodyText"], fontName=font["reportlab_name"],
                          fontSize=8.5, leading=11, wordWrap="CJK", splitLongWords=1, spaceAfter=3)
    small = ParagraphStyle("RunSmall", parent=body, fontSize=7, leading=8, textColor=colors.HexColor("#455a64"))

    details: Mapping[str, Any]
    try:
        details = record.snapshot_details()
        details_error = None
    except Exception as exc:
        details = {}
        details_error = f"{type(exc).__name__}: {exc}"
    # Raw source bytes are never copied into a human report.
    report_details = {key: value for key, value in details.items() if key != "raw_sources"}
    metadata = record.metadata()
    story: list[Any] = [Paragraph(html.escape(request.title), title_style), Spacer(1, 5)]
    story.append(Paragraph("本报告忠实展示本次运行已经保存的返回值，不重新计算统计量、置信区间或 p 值。", body))
    story.append(Paragraph("运行身份与状态", heading))
    identity_rows = [
        ["运行 ID", record.run_id], ["Generation", record.generation],
        ["Revision", record.revision_id], ["Config fingerprint", record.config_fingerprint],
        ["创建时间", record.created_at], ["运行阶段", record.phase], ["RunRecord 状态", record.status],
    ]
    for key in ("execution_status", "analysis_status", "overall_status", "gui_effective_status"):
        if metadata.get(key) not in (None, ""):
            identity_rows.append([key, _to_text(metadata[key])])
    identity_table = Table(
        [[Paragraph(html.escape(str(left)), body), Paragraph(html.escape(str(right)), body)]
         for left, right in identity_rows], colWidths=[42 * mm, 130 * mm], hAlign="LEFT",
    )
    identity_table.setStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#b7c4cc")),
                             ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#edf2f5")),
                             ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 5),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 5)])
    story.append(identity_table)
    story.append(Paragraph("整体状态字段沿用运行记录原值；统计表中的分析状态和流水线状态分别保留。", small))

    story.append(Paragraph("数据来源与质量", heading))
    sources = report_details.get("source_hashes")
    if sources:
        story.append(Paragraph("来源哈希（不包含原始文件字节）：", body))
        story.append(Paragraph(_paragraph_text(sources), small))
    else:
        story.append(Paragraph("运行记录没有提供来源哈希。", body))
    if details_error:
        story.append(Paragraph(f"快照详情不可用：{html.escape(details_error)}。未推测或补造来源信息。", small))
    quality_values = {key: value for key, value in metadata.items()
                      if any(token in str(key).casefold() for token in ("quality", "qc", "coverage", "data_loss"))}
    if quality_values:
        story.append(Paragraph("实际质量字段：", body))
        story.append(Paragraph(_paragraph_text(quality_values), small))

    story.append(Paragraph("方法、参数与复用记录", heading))
    parameters = report_details.get("parameters", metadata.get("parameters", metadata.get("parameter_values")))
    context = report_details.get("context")
    if parameters is not None:
        story.append(Paragraph("本次运行保存的参数：", body))
        story.append(Paragraph(_paragraph_text(parameters), small))
    if context is not None:
        story.append(Paragraph("本次运行保存的上下文：", body))
        story.append(Paragraph(_paragraph_text(context), small))
    method_values = {key: value for key, value in metadata.items()
                     if any(token in str(key).casefold() for token in
                            ("method", "algorithm", "reuse", "reused", "parameter", "seed", "version", "family"))}
    if method_values:
        story.append(Paragraph("方法、参数、随机种子、版本及复用来源字段（保留原值）：", body))
        story.append(Paragraph(_paragraph_text(method_values), small))
    if parameters is None and context is None and not method_values:
        story.append(Paragraph("运行记录没有提供可展示的参数/方法字段；本报告未补推默认参数。", body))

    for figure_index, (figure_title, png_data) in enumerate(figure_pngs):
        figure_story: list[Any] = []
        if figure_index == 0:
            figure_story.append(Paragraph("实际结果图", heading))
        figure_story.append(Paragraph(html.escape(figure_title), body))
        img = Image(io.BytesIO(png_data))
        max_width, max_height = 172 * mm, 112 * mm
        scale = min(max_width / img.imageWidth, max_height / img.imageHeight, 1.0)
        img.drawWidth = img.imageWidth * scale
        img.drawHeight = img.imageHeight * scale
        figure_story.extend([img, Spacer(1, 4)])
        story.append(KeepTogether(figure_story))
    for note in omissions:
        story.append(Paragraph(f"图表未生成：{html.escape(note)}", small))

    story.append(Paragraph("结果表与统计审计", heading))
    if not request.table_names:
        story.append(Paragraph("本报告未选择结果表。所有表名：" + html.escape(", ".join(record.table_names())), body))
    story.extend(_report_tables(record, request, font["reportlab_name"]))
    if record.logs():
        story.append(Paragraph("运行日志", heading))
        shown = list(record.logs()[-80:])
        for line in shown:
            story.append(Paragraph(html.escape(str(line)), small))
        if len(record.logs()) > len(shown):
            story.append(Paragraph(
                f"报告显示最近 {len(shown)} 条日志；其余 {len(record.logs()) - len(shown):,} 条仍保存在本次运行记录/档案中。",
                small,
            ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"说明：{html.escape(request.omitted_rows_note)}", small))
    story.append(Paragraph(
        "设备/生理验证状态按运行原记录呈现；没有记录的验证不视为已完成。统计结果仅反映此运行实际保存的分析表。",
        small,
    ))

    def draw_page(canvas: Any, _doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(font["reportlab_name"], 7)
        canvas.setFillColor(colors.HexColor("#546b7b"))
        canvas.drawString(16 * mm, 10 * mm, f"PYeyedata · {record.run_id} · {_record_status(record)}")
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return buffer.getvalue()


def build_analysis_report_bytes(record: RunRecord, request: ReportRequest | None = None) -> bytes:
    """Render a paginated PDF with the C03 licensed font held through PDF generation."""
    request = request or create_report_request(record)
    _verify_report_request(record, request)
    with _registered_font_context() as font:
        figure_pngs: list[tuple[str, bytes]] = []
        omissions: list[str] = []
        for spec in request.figure_specs:
            try:
                figure = build_figure(record, spec)
                provenance = {
                    "run_id": record.run_id,
                    "revision_id": record.revision_id,
                    "config_fingerprint": record.config_fingerprint,
                    "record_status": record.status,
                    **_figure_spec_provenance(spec),
                }
                png_data = _serialize_figure(figure, ("png",), 300, provenance)["png"]
                figure_pngs.append((f"{spec.table_name} · {spec.y_column}", png_data))
            except (ReportingError, ValueError, TypeError, RuntimeError) as exc:
                omissions.append(f"{spec.table_name}: {type(exc).__name__}: {exc}")
        return _build_report_pdf(record, request, font, figure_pngs, omissions)


def _slug(value: str) -> str:
    ascii_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-._")[:48]
    return ascii_slug or "table"


def _artifact(member_name: str, media_type: str, file_format: str, data: bytes,
              provenance: Mapping[str, Any]) -> FigureArtifact:
    return FigureArtifact(
        member_name, media_type, file_format, data, hashlib.sha256(data).hexdigest(), len(data),
        {**_safe_value(provenance), "member_name": member_name, "media_type": media_type,
         "file_format": file_format, "byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    )


def build_archive_artifacts(
    record: RunRecord,
    *,
    figure_specs: Sequence[FigureSpec] | None = None,
    max_rows_per_table: int = 50,
) -> ArchiveArtifactBundle:
    """Build static app_archive PNG/PDF bytes from one immutable RunRecord.

    Artifact names are safe archive members. Hashes and complete source identity
    travel with the bytes so the archive owner can add them to its inventory.
    """
    if max_rows_per_table < 0:
        raise ReportingError("max_rows_per_table must be zero or greater")
    if figure_specs is None:
        suggested = _default_figure_spec(record)
        specs = () if suggested is None else (suggested,)
    else:
        specs = tuple(figure_specs)
    for spec in specs:
        _verify_figure_spec(record, spec)
    report_request = create_report_request(
        record, max_rows_per_table=max_rows_per_table, figure_specs=specs,
        omitted_rows_note="本报告按表展示所选范围；超过显示上限的行保留在同一运行档案中。",
    )
    base_provenance = {
        "run_id": record.run_id, "generation": record.generation,
        "revision_id": record.revision_id, "config_fingerprint": record.config_fingerprint,
        "record_status": record.status, "artifact_scope": "this RunRecord only",
    }
    artifacts: list[FigureArtifact] = []
    omissions: list[str] = []
    with _registered_font_context() as font:
        figure_pngs: list[tuple[str, bytes]] = []
        for spec in specs:
            try:
                figure = build_figure(record, spec)
                provenance = {
                    **base_provenance, "table_name": spec.table_name,
                    "data_source": "RunRecord.table_copy; stored values only; no analysis recomputation",
                    "dpi": 300, "font_sha256": font["sha256"],
                    **_figure_spec_provenance(spec),
                }
                png = _serialize_figure(figure, ("png",), 300, provenance)["png"]
                slug = _slug(spec.table_name)
                digest = hashlib.sha256(png).hexdigest()[:12]
                member = f"app_archive/figures/{slug}-{digest}.png"
                artifacts.append(_artifact(member, "image/png", "png", png, provenance))
                figure_pngs.append((f"{spec.table_name} · {spec.y_column}", png))
            except (ReportingError, ValueError, TypeError, RuntimeError, OSError) as exc:
                omissions.append(f"{spec.table_name}: {type(exc).__name__}: {exc}")
        if not specs:
            omissions.append("本次运行没有可由已保存 typed 结果生成的支持图表；未生成示意图。")
        report_pdf = _build_report_pdf(record, report_request, font, figure_pngs, omissions)
        run_slug = _slug(record.run_id)
        revision_slug = _slug(record.revision_id)
        report_member = f"app_archive/reports/{run_slug}-{revision_slug}-analysis-report.pdf"
        report_provenance = {
            **base_provenance, "report_title": report_request.title,
            "included_tables": list(report_request.table_names),
            "max_rows_per_table": max_rows_per_table,
            "included_figures": [item.member_name for item in artifacts],
            "omissions": list(omissions), "font_sha256": font["sha256"],
            "statistics_recomputed": False,
        }
        artifacts.append(_artifact(report_member, "application/pdf", "report_pdf", report_pdf, report_provenance))
    return ArchiveArtifactBundle(tuple(artifacts), tuple(omissions))


def write_analysis_report(
    record: RunRecord,
    destination: str | Path,
    *,
    request: ReportRequest | None = None,
    overwrite: bool = False,
) -> ReportResult:
    """Write a PDF report with sibling staging and atomic replacement."""
    output = Path(destination).expanduser().absolute()
    if output.exists() and not overwrite:
        raise FileExistsError(str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    data = build_analysis_report_bytes(record, request)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".pdf.tmp", dir=output.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if output.exists() and not overwrite:
            raise FileExistsError(str(output))
        os.replace(temp, output)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return ReportResult(output, hashlib.sha256(data).hexdigest(), len(data))


__all__ = [
    "ArchiveArtifactBundle", "FigureArtifact", "FigureExportResult", "FigureSpec",
    "ReportRequest", "ReportResult", "ReportingError", "build_archive_artifacts",
    "build_analysis_report_bytes", "build_figure", "create_figure_spec", "create_report_request",
    "render_existing_figure_artifact", "render_figure_artifact", "write_analysis_report",
]
