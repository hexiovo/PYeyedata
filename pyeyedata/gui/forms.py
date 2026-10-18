"""Schema-driven typed editors that preserve backend Python values."""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

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
        if spec.unit:
            layout.addWidget(QLabel(spec.unit, self))
        if spec.description:
            self.setToolTip(spec.description)
            self._editor.setToolTip(spec.description)
        self._connect_editor_changes()

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
            return editor.currentData()
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, MatrixEditor):
            values = editor.value()
            return tuple(tuple(row) for row in values) if _base_type(self.spec.type_name).startswith("tuple[") else values
        if isinstance(editor, BandListEditor):
            return editor.value()
        if isinstance(editor, QPlainTextEdit):
            try:
                value = json.loads(editor.toPlainText() or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"{self.spec.name}: JSON 格式错误：{exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{self.spec.name} 必须是对象")
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
            return int(text)
        if type_name in {"float", "optional_float", "positive_float", "nonnegative_float", "number"}:
            return float(text)
        if type_name.startswith("tuple[") or type_name.startswith("list[") or "sequence[" in type_name:
            parts = [part.strip() for part in text.split(",") if part.strip()]
            element_type = _element_type(type_name).casefold()
            if element_type in {"float", "int", "integer"}:
                converted = [float(item) if element_type == "float" else int(item) for item in parts]
            else:
                converted = parts
            if "tuple[float, float]" in type_name and len(converted) != 2:
                raise ValueError(f"{self.spec.name} 需要两个数值组成时间窗")
            return tuple(converted) if type_name.startswith("tuple[") else converted
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

    def values(self) -> dict[str, Any]:
        return {name: editor.value() for name, editor in self._editors.items()}

    def set_values(self, values: Mapping[str, Any]) -> None:
        self._suppress_edits += 1
        try:
            for name, value in values.items():
                editor = self._editors.get(name)
                if editor is not None:
                    editor.set_value(value)
        finally:
            self._suppress_edits -= 1

    def _on_editor_value_changed(self, name: str) -> None:
        if self._suppress_edits == 0:
            # Keep this as an input signal: parsing may fail while the user is
            # midway through typing, but the prior result must still go stale.
            self.edited.emit(name)

