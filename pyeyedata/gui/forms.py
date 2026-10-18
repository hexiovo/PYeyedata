"""Schema-driven typed editors that preserve backend Python values."""
from __future__ import annotations

import ast
import json
import math
import numbers
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True, slots=True)
class TypedFieldSpec:
    name: str
    type_name: str
    default: Any = None
    description: str = ""
    unit: str | None = None
    choices: tuple[Any, ...] = ()
    minimum: float | int | None = None
    maximum: float | int | None = None
    minimum_exclusive: float | int | None = None
    maximum_exclusive: float | int | None = None

    @classmethod
    def from_schema(cls, record: Mapping[str, Any], *, fallback_name: str = "") -> "TypedFieldSpec":
        return cls(
            name=str(record.get("name", fallback_name)),
            type_name=str(record.get("type", "str")),
            default=record.get("default"),
            description=str(record.get("description", record.get("note", "")) or ""),
            unit=str(record["unit"]) if record.get("unit") is not None else None,
            choices=tuple(record.get("choices", ())),
            minimum=record.get("minimum"),
            maximum=record.get("maximum"),
            minimum_exclusive=record.get("minimum_exclusive"),
            maximum_exclusive=record.get("maximum_exclusive"),
        )


def _literal_choices(type_name: str) -> tuple[Any, ...]:
    try:
        node = ast.parse(type_name, mode="eval").body
    except SyntaxError:
        return ()
    if not isinstance(node, ast.Subscript) or ast.unparse(node.value) != "Literal":
        return ()
    values = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    parsed: list[Any] = []
    for value in values:
        try:
            parsed.append(ast.literal_eval(value))
        except (ValueError, TypeError):
            parsed.append(ast.unparse(value))
    return tuple(parsed)


def _is_optional(type_name: str, default: Any) -> bool:
    return default is None or "None" in type_name or "Optional[" in type_name


def _base_type(type_name: str) -> str:
    value = type_name.strip()
    value = re.sub(r"\s*\|\s*None", "", value)
    value = re.sub(r"None\s*\|\s*", "", value)
    value = re.sub(r"Optional\[(.*)\]", r"\1", value)
    return value.strip()


def _element_type(type_name: str) -> str:
    start, end = type_name.find("["), type_name.rfind("]")
    if start < 0 or end <= start:
        return "str"
    body = type_name[start + 1:end]
    return body.split(",", 1)[0].strip() or "str"


def _generic_type(type_name: str) -> tuple[str, tuple[str, ...]] | None:
    try:
        node = ast.parse(_base_type(type_name), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Subscript):
        return None
    container = ast.unparse(node.value).rsplit(".", 1)[-1].casefold()
    raw_arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    return container, tuple(ast.unparse(argument) for argument in raw_arguments)


def _restore_json_shape(value: Any, type_name: str) -> Any:
    """Restore only tuple containers declared by the live schema."""
    generic = _generic_type(type_name)
    if generic is None or value is None:
        return value
    container, arguments = generic
    if container == "tuple":
        if not isinstance(value, (list, tuple)) or not arguments:
            return value
        if len(arguments) >= 2 and arguments[-1].strip() == "...":
            items = [_restore_json_shape(item, arguments[0]) for item in value]
        elif len(value) == len(arguments):
            items = [_restore_json_shape(item, annotation)
                     for item, annotation in zip(value, arguments)]
        else:
            return value
        return tuple(items)
    if container in {"list", "sequence"} and arguments:
        if not isinstance(value, (list, tuple)):
            return value
        items = [_restore_json_shape(item, arguments[0]) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    if container in {"dict", "mapping"} and len(arguments) == 2 and isinstance(value, Mapping):
        return {key: _restore_json_shape(item, arguments[1]) for key, item in value.items()}
    return value


def _validate_typed_value(value: Any, type_name: str, label: str) -> None:
    """Validate one schema-typed container member without coercing it."""
    choices = _literal_choices(type_name)
    if choices:
        if not any(type(choice) is type(value) and choice == value for choice in choices):
            raise ValueError(f"{label} 必须是已知选项之一：{choices!r}")
        return

    kind = _base_type(type_name).casefold()
    if kind in {"any", "typing.any", "object"}:
        return
    if kind in {"str", "string", "text"}:
        if not isinstance(value, str):
            raise ValueError(f"{label} 必须是文本")
        return
    if kind in {"bool", "boolean"}:
        if not isinstance(value, bool):
            raise ValueError(f"{label} 必须是布尔值 true/false")
        return
    if kind in {"int", "integer"}:
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Integral):
            raise ValueError(f"{label} 必须是整数")
        return
    if kind in {"float", "number", "positive_float", "nonnegative_float", "optional_float"}:
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Real):
            raise ValueError(f"{label} 必须是实数")
        try:
            finite = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            raise ValueError(f"{label} 必须是有限数值")
        return

    generic = _generic_type(type_name)
    if generic is None:
        raise ValueError(f"{label} 的 schema 类型 {type_name!r} 无法验证")
    container, arguments = generic
    if container == "tuple":
        if not isinstance(value, tuple) or not arguments:
            raise ValueError(f"{label} 必须是 tuple")
        if len(arguments) >= 2 and arguments[-1].strip() == "...":
            for index, item in enumerate(value, 1):
                _validate_typed_value(item, arguments[0], f"{label} 第 {index} 项")
            return
        if len(value) != len(arguments):
            raise ValueError(f"{label} 必须有 {len(arguments)} 项")
        for index, (item, annotation) in enumerate(zip(value, arguments), 1):
            _validate_typed_value(item, annotation, f"{label} 第 {index} 项")
        return
    if container in {"list", "sequence"}:
        if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
            raise ValueError(f"{label} 必须是序列")
        if arguments:
            for index, item in enumerate(value, 1):
                _validate_typed_value(item, arguments[0], f"{label} 第 {index} 项")
        return
    if container in {"dict", "mapping"}:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} 必须是对象")
        if len(arguments) == 2:
            for key, item in value.items():
                _validate_typed_value(key, arguments[0], f"{label} 的键")
                _validate_typed_value(item, arguments[1], f"{label}[{key!r}]")
        return
    raise ValueError(f"{label} 的 schema 类型 {type_name!r} 无法验证")


class MatrixEditor(QWidget):
    """Small numeric table editor for real matrix-valued parameters."""

    value_changed = Signal()

    def __init__(self, value: Any = None, *, rows: int = 2, columns: int = 2, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(self)
        self.table.setRowCount(0)
        self.table.setColumnCount(max(1, columns))
        self.table.horizontalHeader().setStretchLastSection(True)
        controls = QHBoxLayout()
        add_row = QPushButton("添加行", self)
        remove_row = QPushButton("删除行", self)
        add_column = QPushButton("添加列", self)
        remove_column = QPushButton("删除列", self)
        add_row.clicked.connect(self._add_row)
        remove_row.clicked.connect(self._remove_row)
        add_column.clicked.connect(self._add_column)
        remove_column.clicked.connect(self._remove_column)
        self.table.itemChanged.connect(self._on_item_changed)
        for button in (add_row, remove_row, add_column, remove_column):
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addWidget(self.table)
        layout.addLayout(controls)
        if isinstance(value, (list, tuple)) and value and all(isinstance(row, (list, tuple)) for row in value):
            self.set_value(value)
        else:
            self.table.setRowCount(max(1, rows))
            for row in range(self.table.rowCount()):
                for column in range(self.table.columnCount()):
                    self.table.setItem(row, column, QTableWidgetItem(""))

    def _add_row(self) -> None:
        self.table.insertRow(self.table.rowCount())
        self.value_changed.emit()

    def _remove_row(self) -> None:
        if not self.table.rowCount():
            return
        self.table.removeRow(max(0, self.table.currentRow()))
        self.value_changed.emit()

    def _add_column(self) -> None:
        self.table.insertColumn(self.table.columnCount())
        self.value_changed.emit()

    def _remove_column(self) -> None:
        if self.table.columnCount() <= 1:
            return
        self.table.removeColumn(max(0, self.table.currentColumn()))
        self.value_changed.emit()

    def _on_item_changed(self, *_: Any) -> None:
        self.value_changed.emit()

    def set_value(self, value: Any) -> None:
        rows = [list(row) for row in value]
        width = max((len(row) for row in rows), default=1)
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(max(1, width))
        for row_index, row in enumerate(rows):
            for column_index in range(self.table.columnCount()):
                cell = row[column_index] if column_index < len(row) else ""
                self.table.setItem(row_index, column_index, QTableWidgetItem(str(cell)))

    def value(self) -> list[list[float]]:
        output: list[list[float]] = []
        for row_index in range(self.table.rowCount()):
            row: list[float] = []
            for column_index in range(self.table.columnCount()):
                item = self.table.item(row_index, column_index)
                text = item.text().strip() if item else ""
                if not text:
                    raise ValueError(f"矩阵第 {row_index + 1} 行第 {column_index + 1} 列不能为空")
                try:
                    row.append(float(text))
                except ValueError as exc:
                    raise ValueError(f"矩阵单元格不是数值：{text}") from exc
            output.append(row)
        return output


class BandListEditor(QWidget):
    """Editable rows for the public advanced pupil-PSD band-list parameter."""

    value_changed = Signal()

    def __init__(self, value: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["名称", "low_hz", "high_hz"])
        self.table.horizontalHeader().setStretchLastSection(True)
        controls = QHBoxLayout()
        add_button = QPushButton("添加频带", self)
        remove_button = QPushButton("删除选中频带", self)
        add_button.clicked.connect(self._add_row)
        remove_button.clicked.connect(self._remove_row)
        self.table.itemChanged.connect(self._on_item_changed)
        controls.addWidget(add_button)
        controls.addWidget(remove_button)
        controls.addStretch(1)
        layout.addWidget(self.table)
        layout.addLayout(controls)
        if value:
            self.set_value(value)

    def _add_row(self) -> None:
        self.table.insertRow(self.table.rowCount())
        self.value_changed.emit()

    def _remove_row(self) -> None:
        if self.table.currentRow() < 0:
            return
        self.table.removeRow(self.table.currentRow())
        self.value_changed.emit()

    def _on_item_changed(self, *_: Any) -> None:
        self.value_changed.emit()

    def set_value(self, value: Any) -> None:
        rows = list(value)
        self.table.setRowCount(len(rows))
        for index, band in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(str(band.get("name", ""))))
            self.table.setItem(index, 1, QTableWidgetItem(str(band.get("low_hz", ""))))
            self.table.setItem(index, 2, QTableWidgetItem(str(band.get("high_hz", ""))))

    def value(self) -> list[dict[str, Any]]:
        bands: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            low_item = self.table.item(row, 1)
            high_item = self.table.item(row, 2)
            try:
                low_hz = float(low_item.text().strip()) if low_item else None
                high_hz = float(high_item.text().strip()) if high_item else None
            except ValueError as exc:
                raise ValueError(f"频带第 {row + 1} 行的频率必须是数值") from exc
            bands.append({
                "name": name_item.text().strip() if name_item else "",
                "low_hz": low_hz,
                "high_hz": high_hz,
            })
        return bands


class TypedFieldWidget(QWidget):
    """A single typed field with exact optional, enum, tuple, matrix, or source values."""

    value_changed = Signal(str)

    def __init__(self, spec: TypedFieldSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._suppress_value_changed = 0
        self._optional = _is_optional(spec.type_name, spec.default)
        literal_choices = spec.choices or _literal_choices(spec.type_name)
        self._literal_choices = literal_choices
        base_type = _base_type(spec.type_name)
        initial = spec.default
        if self._optional and initial is None:
            if base_type == "bool":
                initial = False
            elif base_type == "str":
                initial = ""
            elif base_type.startswith(("tuple[", "list[", "dict[")):
                initial = [] if not base_type.startswith("dict[") else {}
            else:
                initial = 0
        self._editor = self._make_base_editor(base_type, initial)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._enabled: QCheckBox | None = None
        if self._optional:
            self._enabled = QCheckBox("设置", self)
            self._enabled.setChecked(spec.default is not None)
            self._enabled.toggled.connect(self._editor.setEnabled)
            self._enabled.toggled.connect(self._on_value_changed)
            self._editor.setEnabled(spec.default is not None)
            layout.addWidget(self._enabled)
        layout.addWidget(self._editor, 1)
        suffix = " · ".join(part for part in (spec.unit or "", self._range_hint()) if part)
        if suffix:
            layout.addWidget(QLabel(suffix, self))
        if spec.description:
            hint = spec.description + (f"\n单位/范围：{suffix}" if suffix else "")
            self.setToolTip(hint)
            self._editor.setToolTip(hint)
        self._connect_editor_changes()

    def _range_hint(self) -> str:
        limits: list[str] = []
        if self.spec.minimum is not None:
            limits.append(f"≥ {self.spec.minimum}")
        elif self.spec.minimum_exclusive is not None:
            limits.append(f"> {self.spec.minimum_exclusive}")
        if self.spec.maximum is not None:
            limits.append(f"≤ {self.spec.maximum}")
        elif self.spec.maximum_exclusive is not None:
            limits.append(f"< {self.spec.maximum_exclusive}")
        if self._optional:
            limits.append("可选 None")
        return "范围 " + "，".join(limits) if limits else ("可选 None" if self._optional else "")

    @staticmethod
    def _is_bool(value: Any) -> bool:
        return isinstance(value, (bool, np.bool_))

    @classmethod
    def _is_real(cls, value: Any) -> bool:
        return isinstance(value, numbers.Real) and not cls._is_bool(value)

    def validate_value(self, value: Any) -> None:
        """Validate a preset/restore value before mutating its editor."""
        name = self.spec.name
        if value is None:
            if self._optional or any(choice is None for choice in self._literal_choices):
                return
            raise ValueError(f"{name} 不允许 None")

        if self._literal_choices:
            if not any(type(choice) is type(value) and choice == value for choice in self._literal_choices):
                raise ValueError(f"{name} 必须是已知选项之一：{self._literal_choices!r}")
            return

        kind = _base_type(self.spec.type_name).casefold()
        if kind in {"bool", "boolean"}:
            if not isinstance(value, bool):
                raise ValueError(f"{name} 必须是布尔值 true/false")
            return
        if kind in {"int", "integer", "optional_int"}:
            if self._is_bool(value) or not isinstance(value, numbers.Integral):
                raise ValueError(f"{name} 必须是整数")
            numeric: Any = value
        elif kind in {"float", "optional_float", "positive_float", "nonnegative_float", "number"}:
            if not self._is_real(value):
                raise ValueError(f"{name} 必须是实数")
            try:
                if not math.isfinite(float(value)):
                    raise ValueError(f"{name} 必须是有限数值")
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError(f"{name} 必须是有限数值") from exc
            numeric = value
        elif kind == "band_list":
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{name} 必须是频带列表")
            for index, band in enumerate(value, 1):
                if not isinstance(band, Mapping):
                    raise ValueError(f"{name} 第 {index} 项必须是对象")
                low, high = band.get("low_hz"), band.get("high_hz")
                try:
                    finite = math.isfinite(float(low)) and math.isfinite(float(high))
                except (TypeError, ValueError, OverflowError):
                    finite = False
                if (not isinstance(band.get("name", ""), str)
                        or not self._is_real(low) or not self._is_real(high)
                        or not finite or high <= low):
                    raise ValueError(f"{name} 第 {index} 项需有名称及有限且递增的 low_hz/high_hz")
            return
        elif "matrix" in kind or "list[list[" in kind or "tuple[tuple[" in kind:
            if kind.startswith("tuple[tuple["):
                _validate_typed_value(value, kind, name)
            if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
                    or any(not isinstance(row, Sequence) or isinstance(row, (str, bytes)) for row in value)):
                raise ValueError(f"{name} 必须是二维矩阵")
            widths = {len(row) for row in value}
            if len(widths) > 1:
                raise ValueError(f"{name} 矩阵各行列数必须相同")
            for row in value:
                for cell in row:
                    try:
                        finite = math.isfinite(float(cell))
                    except (TypeError, ValueError, OverflowError):
                        finite = False
                    if not self._is_real(cell) or not finite:
                        raise ValueError(f"{name} 矩阵单元格必须是有限实数")
            return
        elif kind.startswith("tuple[") or kind.startswith("list[") or "sequence[" in kind:
            expected_tuple = kind.startswith("tuple[")
            if not isinstance(value, (list, tuple)) or (expected_tuple and not isinstance(value, tuple)):
                expected = "tuple" if expected_tuple else "list"
                raise ValueError(f"{name} 必须是 {expected}")
            if _generic_type(kind) is not None:
                _validate_typed_value(value, kind, name)
            else:
                element = _element_type(kind).casefold()
                if "tuple[float, float]" in kind and len(value) != 2:
                    raise ValueError(f"{name} 需要两个数值组成时间窗")
                if element in {"float", "int", "integer"}:
                    for index, item in enumerate(value, 1):
                        is_integer = isinstance(item, numbers.Integral) and not self._is_bool(item)
                        valid = is_integer if element in {"int", "integer"} else self._is_real(item)
                        if not valid:
                            raise ValueError(f"{name} 第 {index} 项必须是有效 {element}")
                        if element == "float":
                            try:
                                finite = math.isfinite(float(item))
                            except (TypeError, ValueError, OverflowError) as exc:
                                raise ValueError(f"{name} 第 {index} 项必须是有限数值") from exc
                            if not finite:
                                raise ValueError(f"{name} 第 {index} 项必须是有限数值")
                elif element in {"str", "string"} and any(not isinstance(item, str) for item in value):
                    raise ValueError(f"{name} 的各项必须是文本")
            return
        elif kind.startswith("dict[") or kind in {"mapping", "dict"}:
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} 必须是对象")
            generic = _generic_type(kind)
            if generic is not None and len(generic[1]) == 2:
                key_type, value_type = generic[1]
                for key, item in value.items():
                    _validate_typed_value(key, key_type, f"{name} 的键")
                    _validate_typed_value(item, value_type, f"{name}[{key!r}]")
            return
        elif kind in {"str", "string", "text"}:
            if not isinstance(value, str):
                raise ValueError(f"{name} 必须是文本")
            return
        else:
            if isinstance(value, (Mapping, list, tuple)):
                raise ValueError(f"{name} 的值类型与 schema {self.spec.type_name!r} 不匹配")
            return

        if self.spec.minimum is not None and numeric < self.spec.minimum:
            raise ValueError(f"{name} 不能小于 {self.spec.minimum}")
        if self.spec.minimum_exclusive is not None and numeric <= self.spec.minimum_exclusive:
            raise ValueError(f"{name} 必须大于 {self.spec.minimum_exclusive}")
        if self.spec.maximum is not None and numeric > self.spec.maximum:
            raise ValueError(f"{name} 不能大于 {self.spec.maximum}")
        if self.spec.maximum_exclusive is not None and numeric >= self.spec.maximum_exclusive:
            raise ValueError(f"{name} 必须小于 {self.spec.maximum_exclusive}")

    def normalize_serialized_value(self, value: Any) -> Any:
        """Restore JSON arrays to tuple shapes declared by this field's schema."""
        return _restore_json_shape(value, self.spec.type_name)

    def draft_state(self) -> dict[str, Any]:
        """Capture editor contents without parsing, so incomplete drafts survive form rebuilds."""
        editor = self._editor
        if isinstance(editor, QLineEdit):
            kind, content = "line", editor.text()
        elif isinstance(editor, QPlainTextEdit):
            kind, content = "plain", editor.toPlainText()
        elif isinstance(editor, QComboBox):
            kind, content = "combo", editor.currentIndex()
        elif isinstance(editor, QCheckBox):
            kind, content = "check", editor.isChecked()
        elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
            kind, content = "spin", editor.value()
        elif isinstance(editor, MatrixEditor):
            kind = "matrix"
            content = [[editor.table.item(row, col).text() if editor.table.item(row, col) else ""
                        for col in range(editor.table.columnCount())]
                       for row in range(editor.table.rowCount())]
        elif isinstance(editor, BandListEditor):
            kind = "bands"
            content = [[editor.table.item(row, col).text() if editor.table.item(row, col) else ""
                        for col in range(editor.table.columnCount())]
                       for row in range(editor.table.rowCount())]
        else:
            raise TypeError(f"Unsupported typed editor for {self.spec.name}")
        return {
            "enabled": self._enabled.isChecked() if self._enabled is not None else None,
            "kind": kind,
            "content": content,
        }

    def restore_draft_state(self, state: Mapping[str, Any]) -> None:
        """Restore raw editor contents without schema validation or value coercion."""
        if not isinstance(state, Mapping):
            raise TypeError("typed field draft state must be a mapping")
        kind, content = state.get("kind"), state.get("content")
        editor = self._editor
        expected = (
            "line" if isinstance(editor, QLineEdit) else
            "plain" if isinstance(editor, QPlainTextEdit) else
            "combo" if isinstance(editor, QComboBox) else
            "check" if isinstance(editor, QCheckBox) else
            "spin" if isinstance(editor, (QSpinBox, QDoubleSpinBox)) else
            "matrix" if isinstance(editor, MatrixEditor) else
            "bands" if isinstance(editor, BandListEditor) else None
        )
        if kind != expected:
            return
        self._suppress_value_changed += 1
        try:
            enabled = state.get("enabled")
            if self._enabled is not None and isinstance(enabled, bool):
                self._enabled.setChecked(enabled)
            if isinstance(editor, QLineEdit):
                editor.setText(str(content))
            elif isinstance(editor, QPlainTextEdit):
                editor.setPlainText(str(content))
            elif isinstance(editor, QComboBox):
                index = content if isinstance(content, int) else -1
                editor.setCurrentIndex(index)
            elif isinstance(editor, QCheckBox):
                editor.setChecked(bool(content))
            elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                editor.setValue(content)
            elif isinstance(editor, (MatrixEditor, BandListEditor)):
                rows = content if isinstance(content, list) else []
                columns = editor.table.columnCount()
                if isinstance(editor, MatrixEditor):
                    columns = max(1, max((len(row) for row in rows if isinstance(row, list)), default=1))
                editor.table.setRowCount(len(rows))
                editor.table.setColumnCount(columns)
                for row_index, row in enumerate(rows):
                    cells = row if isinstance(row, list) else []
                    for column in range(columns):
                        cell = cells[column] if column < len(cells) else ""
                        editor.table.setItem(row_index, column, QTableWidgetItem(str(cell)))
        finally:
            self._suppress_value_changed -= 1

    def _connect_editor_changes(self) -> None:
        editor = self._editor
        if isinstance(editor, QLineEdit):
            editor.textChanged.connect(self._on_value_changed)
        elif isinstance(editor, QPlainTextEdit):
            editor.textChanged.connect(self._on_value_changed)
        elif isinstance(editor, QComboBox):
            editor.currentIndexChanged.connect(self._on_value_changed)
        elif isinstance(editor, QCheckBox):
            editor.toggled.connect(self._on_value_changed)
        elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
            editor.valueChanged.connect(self._on_value_changed)
        elif isinstance(editor, (MatrixEditor, BandListEditor)):
            editor.value_changed.connect(self._on_value_changed)

    def _on_value_changed(self, *_: Any) -> None:
        if self._suppress_value_changed == 0:
            self.value_changed.emit(self.spec.name)

    def _make_base_editor(self, type_name: str, value: Any) -> QWidget:
        choices = self._literal_choices
        if choices:
            editor = QComboBox(self)
            for choice in choices:
                label = "是" if choice is True else "否" if choice is False else "未设置" if choice is None else str(choice)
                editor.addItem(label, choice)
            self._select_combo(editor, value)
            return editor
        lower = type_name.casefold()
        if lower == "bool" or lower in {"boolean", "bool"}:
            editor = QCheckBox(self)
            editor.setChecked(bool(value))
            return editor
        if lower == "band_list":
            return BandListEditor(value, self)
        if "matrix" in lower or "list[list[" in lower or "tuple[tuple[" in lower:
            return MatrixEditor(value if value else None, parent=self)
        if lower.startswith("tuple[") or lower.startswith("list[") or "sequence[" in lower:
            editor = QLineEdit(self)
            if isinstance(value, (list, tuple)):
                editor.setText(", ".join(str(item) for item in value))
            editor.setPlaceholderText("用逗号分隔各项")
            return editor
        if lower.startswith("dict[") or lower in {"mapping", "dict"}:
            editor = QPlainTextEdit(self)
            editor.setMaximumHeight(72)
            editor.setPlainText(json.dumps(value or {}, ensure_ascii=False, indent=2))
            return editor
        editor = QLineEdit(self)
        if value is not None:
            editor.setText(str(value))
        if lower in {"int", "integer", "optional_int"}:
            validator = QIntValidator(self)
            if self.spec.minimum is not None:
                validator.setBottom(int(self.spec.minimum))
            if self.spec.maximum is not None:
                validator.setTop(int(self.spec.maximum))
            editor.setValidator(validator)
        elif lower in {"float", "optional_float", "positive_float", "nonnegative_float", "number"}:
            validator = QDoubleValidator(self)
            validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
            if self.spec.minimum is not None:
                validator.setBottom(float(self.spec.minimum))
            elif self.spec.minimum_exclusive is not None:
                validator.setBottom(float(self.spec.minimum_exclusive))
            if self.spec.maximum is not None:
                validator.setTop(float(self.spec.maximum))
            elif self.spec.maximum_exclusive is not None:
                validator.setTop(float(self.spec.maximum_exclusive))
            editor.setValidator(validator)
        if "ref" in self.spec.name.casefold() or "source" in self.spec.name.casefold():
            editor.setPlaceholderText("来源或参数引用")
        return editor

    @staticmethod
    def _select_combo(editor: QComboBox, value: Any) -> None:
        for index in range(editor.count()):
            if editor.itemData(index) == value and type(editor.itemData(index)) is type(value):
                editor.setCurrentIndex(index)
                return
        if editor.count():
            editor.setCurrentIndex(0)

    def set_value(self, value: Any) -> None:
        self.validate_value(value)
        self._suppress_value_changed += 1
        try:
            if self._enabled is not None:
                self._enabled.setChecked(value is not None)
            if value is None:
                return
            editor = self._editor
            if isinstance(editor, QComboBox):
                self._select_combo(editor, value)
            elif isinstance(editor, QCheckBox):
                editor.setChecked(bool(value))
            elif isinstance(editor, MatrixEditor):
                editor.set_value(value)
            elif isinstance(editor, BandListEditor):
                editor.set_value(value)
            elif isinstance(editor, QPlainTextEdit):
                editor.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            elif isinstance(editor, QLineEdit):
                if isinstance(value, (list, tuple)):
                    editor.setText(", ".join(str(item) for item in value))
                else:
                    editor.setText(str(value))
        finally:
            self._suppress_value_changed -= 1

    def value(self) -> Any:
        if self._enabled is not None and not self._enabled.isChecked():
            return None
        editor = self._editor
        if isinstance(editor, QComboBox):
            value = editor.currentData()
            self.validate_value(value)
            return value
        if isinstance(editor, QCheckBox):
            value = editor.isChecked()
            self.validate_value(value)
            return value
        if isinstance(editor, MatrixEditor):
            values = editor.value()
            value = tuple(tuple(row) for row in values) if _base_type(self.spec.type_name).startswith("tuple[") else values
            self.validate_value(value)
            return value
        if isinstance(editor, BandListEditor):
            value = editor.value()
            self.validate_value(value)
            return value
        if isinstance(editor, QPlainTextEdit):
            try:
                value = json.loads(editor.toPlainText() or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"{self.spec.name}: JSON 格式错误：{exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{self.spec.name} 必须是对象")
            self.validate_value(value)
            return value
        if not isinstance(editor, QLineEdit):
            raise TypeError("Unsupported typed editor")
        type_name = _base_type(self.spec.type_name).casefold()
        text = editor.text().strip()
        if not text and self._optional:
            return None
        if not text:
            if type_name.startswith("tuple["):
                return ()
            if type_name.startswith("list[") or "sequence[" in type_name:
                return []
            raise ValueError(f"{self.spec.name} 不能为空")
        if type_name in {"int", "integer", "optional_int"}:
            value = int(text)
            self.validate_value(value)
            return value
        if type_name in {"float", "optional_float", "positive_float", "nonnegative_float", "number"}:
            value = float(text)
            self.validate_value(value)
            return value
        if type_name.startswith("tuple[") or type_name.startswith("list[") or "sequence[" in type_name:
            parts = [part.strip() for part in text.split(",") if part.strip()]
            element_type = _element_type(type_name).casefold()
            if element_type in {"float", "int", "integer"}:
                converted = [float(item) if element_type == "float" else int(item) for item in parts]
            else:
                converted = parts
            if "tuple[float, float]" in type_name and len(converted) != 2:
                raise ValueError(f"{self.spec.name} 需要两个数值组成时间窗")
            value = tuple(converted) if type_name.startswith("tuple[") else converted
            self.validate_value(value)
            return value
        self.validate_value(text)
        return text


class TypedParameterForm(QWidget):
    """A reusable QFormLayout generated from the live public schema records."""

    edited = Signal(str)

    def __init__(
        self,
        fields: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        records = list(fields.values()) if isinstance(fields, Mapping) else list(fields)
        self._suppress_edits = 0
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self._editors: dict[str, TypedFieldWidget] = {}
        for record in records:
            spec = TypedFieldSpec.from_schema(record)
            if not spec.name:
                continue
            editor = TypedFieldWidget(spec, self)
            editor.value_changed.connect(self._on_editor_value_changed)
            self._editors[spec.name] = editor
            layout.addRow(QLabel(spec.name, self), editor)

    @property
    def editors(self) -> Mapping[str, TypedFieldWidget]:
        return dict(self._editors)

    def focus_field(self, name: str) -> bool:
        editor = self._editors.get(name)
        if editor is None:
            return False
        target = editor._editor if editor._editor.isEnabled() else editor._enabled
        if target is None:
            target = editor
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def values(self, names: Iterable[str] | None = None) -> dict[str, Any]:
        requested = tuple(self._editors) if names is None else tuple(dict.fromkeys(names))
        unknown = [name for name in requested if name not in self._editors]
        if unknown:
            raise ValueError("未知参数字段：" + ", ".join(unknown))
        output: dict[str, Any] = {}
        for name in requested:
            try:
                output[name] = self._editors[name].value()
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}: {exc}") from exc
        return output

    def validate_values(self, values: Mapping[str, Any], *, strict: bool = True) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise TypeError("typed parameter values must be a mapping")
        unknown = [str(name) for name in values if name not in self._editors]
        if strict and unknown:
            raise ValueError("未知参数字段：" + ", ".join(unknown))
        accepted: dict[str, Any] = {}
        for name, value in values.items():
            editor = self._editors.get(name)
            if editor is None:
                continue
            try:
                editor.validate_value(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}: {exc}") from exc
            accepted[name] = value
        return accepted

    def set_values(self, values: Mapping[str, Any], *, strict: bool = False) -> None:
        accepted = self.validate_values(values, strict=strict)
        self._suppress_edits += 1
        try:
            for name, value in accepted.items():
                self._editors[name].set_value(value)
        finally:
            self._suppress_edits -= 1

    def draft_values(self) -> dict[str, dict[str, Any]]:
        """Capture each editor's raw UI state, including values mid-edit."""
        return {name: editor.draft_state() for name, editor in self._editors.items()}

    def restore_draft_values(self, values: Mapping[str, Mapping[str, Any]]) -> None:
        if not isinstance(values, Mapping):
            raise TypeError("typed parameter draft values must be a mapping")
        self._suppress_edits += 1
        try:
            for name, state in values.items():
                editor = self._editors.get(name)
                if editor is not None:
                    editor.restore_draft_state(state)
        finally:
            self._suppress_edits -= 1

    def _on_editor_value_changed(self, name: str) -> None:
        if self._suppress_edits == 0:
            # Keep this as an input signal: parsing may fail while the user is
            # midway through typing, but the prior result must still go stale.
            self.edited.emit(name)

