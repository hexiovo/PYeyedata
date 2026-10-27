"""Static-image AOI editor using image-native pixel scene coordinates."""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .aoi_editor_model import AOIEditorModel, AOIEditorValidationError
from ..aoi_geometry import AOIGeometryError, validate_geometry


class _EditableAOIItem(QGraphicsObject):
    """Movable shape item with direct vertex/corner editing in natural pixels."""

    geometryChanged = Signal(object)

    def __init__(self, shape: str, points: list[tuple[float, float]], record: Mapping[str, Any],
                 image_size: tuple[int, int], parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self.shape_name = shape
        self.points = [(float(x), float(y)) for x, y in points]
        self.record = dict(record)
        self.image_size = image_size
        self.editable = True
        self._drag_mode: str | None = None
        self._drag_index = -1
        self._last_scene_pos: QPointF | None = None
        self._last_valid_points = list(self.points)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setZValue(2)

    @staticmethod
    def _rect_points(bounds: tuple[float, float, float, float]) -> list[tuple[float, float]]:
        xmin, ymin, xmax, ymax = bounds
        return [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]

    @classmethod
    def from_record(cls, record: Mapping[str, Any], image_size: tuple[int, int]) -> "_EditableAOIItem":
        shape = str(record.get("shape", "")).strip().lower()
        if shape == "rectangle":
            bounds = tuple(float(record[name]) for name in ("xmin", "ymin", "xmax", "ymax"))
            canonical = validate_geometry(shape, record.get("vertices"), bounds)
            points = list(canonical.vertices)
        else:
            canonical = validate_geometry(shape, record.get("vertices"), None)
            points = list(canonical.vertices)
        return cls(canonical.shape, points, record, image_size)

    def set_editable(self, editable: bool) -> None:
        self.editable = bool(editable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, self.editable)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if self.editable else Qt.MouseButton.NoButton)
        if not editable:
            self.setSelected(False)
        self.update()

    def _path(self) -> QPainterPath:
        path = QPainterPath()
        if not self.points:
            return path
        path.moveTo(QPointF(*self.points[0]))
        for point in self.points[1:]:
            path.lineTo(QPointF(*point))
        path.closeSubpath()
        return path

    def boundingRect(self) -> QRectF:
        return self._path().boundingRect().adjusted(-6.0, -6.0, 6.0, 6.0)

    def shape(self) -> QPainterPath:
        path = self._path()
        if self.editable:
            # A narrow stroked path keeps the whole outline selectable; filling
            # is already part of the item path for interior clicks.
            return path
        return path

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        del option, widget
        selected = self.isSelected()
        color = QColor("#f2a43b" if selected else "#e15454")
        pen = QPen(color, 1.7 if selected else 1.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(52 if selected else 34)
        painter.setBrush(fill)
        painter.drawPath(self._path())
        if selected and self.editable:
            painter.setBrush(QColor("white"))
            for x, y in self.points:
                painter.drawEllipse(QPointF(x, y), 3.2, 3.2)
        name = str(self.record.get("aoi_name", ""))
        if name and self.points:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color))
            painter.drawText(QPointF(self.points[0][0] + 2, self.points[0][1] - 3), name)

    def _nearest_point(self, position: QPointF) -> int:
        if not self.points:
            return -1
        distances = [math.hypot(position.x() - x, position.y() - y) for x, y in self.points]
        index = min(range(len(distances)), key=distances.__getitem__)
        return index if distances[index] <= 7.0 else -1

    def mousePressEvent(self, event: Any) -> None:
        if not self.editable:
            event.ignore()
            return
        self.setSelected(True)
        self._last_valid_points = list(self.points)
        self._last_scene_pos = event.scenePos()
        vertex = self._nearest_point(event.pos())
        if vertex >= 0:
            self._drag_mode, self._drag_index = "vertex", vertex
        elif self.shape_name == "rectangle":
            self._drag_mode = "move"
        else:
            self._drag_mode = "move"
        event.accept()

    def mouseMoveEvent(self, event: Any) -> None:
        if not self.editable or self._last_scene_pos is None:
            event.ignore()
            return
        current = event.scenePos()
        dx, dy = current.x() - self._last_scene_pos.x(), current.y() - self._last_scene_pos.y()
        self._last_scene_pos = current
        width, height = self.image_size
        self.prepareGeometryChange()
        if self._drag_mode == "vertex" and 0 <= self._drag_index < len(self.points):
            point = (min(max(current.x(), 0.0), float(width)), min(max(current.y(), 0.0), float(height)))
            points = list(self.points)
            points[self._drag_index] = point
            self.points = points
        elif self._drag_mode == "move":
            self.points = [
                (min(max(x + dx, 0.0), float(width)), min(max(y + dy, 0.0), float(height)))
                for x, y in self.points
            ]
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._drag_mode:
            try:
                self.geometry_record()
                self._last_valid_points = list(self.points)
                self.geometryChanged.emit(self.geometry_record())
            except (AOIGeometryError, TypeError, ValueError):
                self.prepareGeometryChange()
                self.points = list(self._last_valid_points)
                self.update()
        self._drag_mode = None
        self._drag_index = -1
        self._last_scene_pos = None
        event.accept()

    def geometry_record(self) -> dict[str, Any]:
        if self.shape_name == "rectangle":
            xs = [point[0] for point in self.points]
            ys = [point[1] for point in self.points]
            bounds = (min(xs), min(ys), max(xs), max(ys))
            canonical = validate_geometry("rectangle", None, bounds)
            return {"shape": "rectangle", "vertices": None,
                    "xmin": canonical.bounds[0], "ymin": canonical.bounds[1],
                    "xmax": canonical.bounds[2], "ymax": canonical.bounds[3]}
        canonical = validate_geometry("polygon", self.points, None)
        return {"shape": "polygon", "vertices": json.dumps(canonical.vertices, separators=(",", ":")),
                "xmin": None, "ymin": None, "xmax": None, "ymax": None}


class _AOICanvas(QGraphicsView):
    shapeCreated = Signal(str, object)
    deleteRequested = Signal()
    modeChanged = Signal(str)

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#20242a"))
        self._mode = "select"
        self._image_size = (0, 0)
        self._rect_start: QPointF | None = None
        self._polygon_points: list[QPointF] = []
        self._preview: QGraphicsPathItem | None = None
        self._pan_position: QPointF | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def set_image_size(self, width: int, height: int) -> None:
        self._image_size = (int(width), int(height))

    def set_mode(self, mode: str) -> None:
        if mode not in {"select", "rectangle", "polygon"}:
            raise ValueError(f"unsupported AOI drawing mode: {mode}")
        self.cancel_drawing()
        self._mode = mode
        self.modeChanged.emit(mode)

    def cancel_drawing(self) -> None:
        self._rect_start = None
        self._polygon_points.clear()
        if self._preview is not None and self._preview.scene() is self.scene():
            self.scene().removeItem(self._preview)
        self._preview = None
        self.viewport().update()

    def finish_polygon(self) -> bool:
        if len(self._polygon_points) < 3:
            return False
        points = [(point.x(), point.y()) for point in self._polygon_points]
        try:
            validate_geometry("polygon", points, None)
        except (AOIGeometryError, TypeError, ValueError):
            return False
        self.shapeCreated.emit("polygon", points)
        self.cancel_drawing()
        self._mode = "select"
        self.modeChanged.emit(self._mode)
        return True

    def _scene_position(self, event: Any) -> QPointF:
        point = self.mapToScene(event.position().toPoint())
        width, height = self._image_size
        return QPointF(min(max(point.x(), 0.0), float(width)), min(max(point.y(), 0.0), float(height)))

    def _set_preview(self, points: list[QPointF], *, close: bool) -> None:
        if self._preview is None:
            self._preview = QGraphicsPathItem()
            pen = QPen(QColor("#ffe082"), 1.6, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._preview.setPen(pen)
            self._preview.setZValue(10)
            self.scene().addItem(self._preview)
        path = QPainterPath()
        if points:
            path.moveTo(points[0])
            for point in points[1:]:
                path.lineTo(point)
            if close:
                path.closeSubpath()
        self._preview.setPath(path)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_position = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._mode == "rectangle":
            self._rect_start = self._scene_position(event)
            self._set_preview([self._rect_start], close=False)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._mode == "polygon":
            point = self._scene_position(event)
            if not self._polygon_points or math.hypot(point.x() - self._polygon_points[-1].x(),
                                                       point.y() - self._polygon_points[-1].y()) > 0.25:
                self._polygon_points.append(point)
            self._set_preview(self._polygon_points, close=False)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._pan_position is not None:
            current = event.position()
            delta = current - self._pan_position
            self._pan_position = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        if self._mode == "rectangle" and self._rect_start is not None:
            end = self._scene_position(event)
            self._set_preview([self._rect_start, QPointF(end.x(), self._rect_start.y()), end,
                               QPointF(self._rect_start.x(), end.y())], close=True)
            event.accept()
            return
        if self._mode == "polygon" and self._polygon_points:
            self._set_preview([*self._polygon_points, self._scene_position(event)], close=False)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_position is not None:
            self._pan_position = None
            self.unsetCursor()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._mode == "rectangle" and self._rect_start is not None:
            end = self._scene_position(event)
            xmin, xmax = sorted((self._rect_start.x(), end.x()))
            ymin, ymax = sorted((self._rect_start.y(), end.y()))
            self.cancel_drawing()
            if xmax - xmin >= 1.0 and ymax - ymin >= 1.0:
                self.shapeCreated.emit("rectangle", [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])
            self._mode = "select"
            self.modeChanged.emit(self._mode)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._mode == "polygon":
            point = self._scene_position(event)
            if len(self._polygon_points) < 3 or math.hypot(point.x() - self._polygon_points[-1].x(),
                                                            point.y() - self._polygon_points[-1].y()) > 0.25:
                self._polygon_points.append(point)
            if not self.finish_polygon():
                self.modeChanged.emit("polygon_invalid")
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: Any) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        event.accept()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deleteRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_drawing()
            self.set_mode("select")
            event.accept()
            return
        super().keyPressEvent(event)


class AOIEditorDialog(QDialog):
    """Edit static AOIs against an explicitly bound image/stimulus pair."""

    applyRequested = Signal(object, object)
    exportRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("静态 AOI 编辑器")
        self.resize(1120, 760)
        self.model = AOIEditorModel()
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._items: list[_EditableAOIItem] = []
        self._image_loaded = False
        self._loading_properties = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        binding = QHBoxLayout()
        binding.addWidget(QLabel("当前图片刺激 ID", self))
        self.binding_selector = QComboBox(self)
        self.binding_selector.setEditable(True)
        self.binding_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.binding_selector.setMinimumWidth(230)
        binding.addWidget(self.binding_selector)
        self.confirm_binding_button = QPushButton("确认绑定", self)
        binding.addWidget(self.confirm_binding_button)
        self.image_button = QPushButton("打开静态图片…", self)
        binding.addWidget(self.image_button)
        self.metadata_label = QLabel("尚未载入图片。", self)
        self.metadata_label.setWordWrap(True)
        binding.addWidget(self.metadata_label, 1)
        root.addLayout(binding)

        tools = QHBoxLayout()
        self.select_button = QPushButton("选择 / 编辑", self)
        self.select_button.setCheckable(True)
        self.select_button.setChecked(True)
        self.rect_button = QPushButton("画矩形", self)
        self.rect_button.setCheckable(True)
        self.polygon_button = QPushButton("画多边形", self)
        self.polygon_button.setCheckable(True)
        self.finish_polygon_button = QPushButton("完成多边形", self)
        self.finish_polygon_button.setEnabled(False)
        self.delete_button = QPushButton("删除选中", self)
        self.fit_button = QPushButton("适应窗口", self)
        self.zoom_in_button = QPushButton("放大 +", self)
        self.zoom_out_button = QPushButton("缩小 −", self)
        for widget in (self.select_button, self.rect_button, self.polygon_button,
                       self.finish_polygon_button, self.delete_button, self.fit_button,
                       self.zoom_in_button, self.zoom_out_button):
            tools.addWidget(widget)
        tools.addStretch(1)
        root.addLayout(tools)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.scene = QGraphicsScene(self)
        self.canvas = _AOICanvas(self.scene, splitter)
        self.canvas.setMinimumSize(560, 400)
        splitter.addWidget(self.canvas)
        side = QWidget(splitter)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 4, 4, 4)
        side_layout.addWidget(QLabel("当前绑定刺激的 AOI", side))
        self.aoi_list = QListWidget(side)
        side_layout.addWidget(self.aoi_list, 1)
        form = QFormLayout()
        self.id_edit = QLineEdit(side)
        self.name_edit = QLineEdit(side)
        self.priority_edit = QLineEdit(side)
        self.priority_edit.setPlaceholderText("留空表示未指定")
        self.overlap_selector = QComboBox(side)
        for label, value in (("未决", "unresolved"), ("互斥", "exclusive"), ("多标签", "multi_label")):
            self.overlap_selector.addItem(label, value)
        form.addRow("AOI ID", self.id_edit)
        form.addRow("名称", self.name_edit)
        form.addRow("优先级", self.priority_edit)
        form.addRow("重叠策略", self.overlap_selector)
        side_layout.addLayout(form)
        side_layout.addWidget(QLabel("坐标声明：原始图像像素；原点左上；Y 向下。分析坐标映射由宿主 preflight 验证。", side))
        splitter.addWidget(side)
        splitter.setSizes([780, 320])
        root.addWidget(splitter, 1)

        self.status_label = QLabel("先打开图片，再明确确认其 stimulus_id。画布坐标保持在原图像素空间。", self)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.validate_button = QPushButton("验证 AOI", self)
        self.export_button = QPushButton("导出 CSV + manifest…", self)
        self.apply_button = QPushButton("应用到宿主", self)
        self.close_button = QPushButton("关闭", self)
        footer.addWidget(self.validate_button)
        footer.addStretch(1)
        footer.addWidget(self.export_button)
        footer.addWidget(self.apply_button)
        footer.addWidget(self.close_button)
        root.addLayout(footer)

        self.image_button.clicked.connect(self._choose_image)
        self.confirm_binding_button.clicked.connect(self._confirm_binding)
        self.select_button.clicked.connect(lambda: self._set_mode("select"))
        self.rect_button.clicked.connect(lambda: self._set_mode("rectangle"))
        self.polygon_button.clicked.connect(lambda: self._set_mode("polygon"))
        self.finish_polygon_button.clicked.connect(self._finish_polygon)
        self.delete_button.clicked.connect(self._delete_selected)
        self.fit_button.clicked.connect(self.fit_image)
        self.zoom_in_button.clicked.connect(lambda: self.canvas.scale(1.2, 1.2))
        self.zoom_out_button.clicked.connect(lambda: self.canvas.scale(1 / 1.2, 1 / 1.2))
        self.canvas.shapeCreated.connect(self._shape_created)
        self.canvas.deleteRequested.connect(self._delete_selected)
        self.canvas.modeChanged.connect(self._mode_changed)
        self.scene.selectionChanged.connect(self._scene_selection_changed)
        self.aoi_list.currentRowChanged.connect(self._list_selection_changed)
        self.id_edit.editingFinished.connect(self._properties_changed)
        self.name_edit.editingFinished.connect(self._properties_changed)
        self.priority_edit.editingFinished.connect(self._properties_changed)
        self.overlap_selector.currentIndexChanged.connect(self._properties_changed)
        self.validate_button.clicked.connect(self._show_validation)
        self.export_button.clicked.connect(self._choose_export)
        self.apply_button.clicked.connect(self._apply)
        self.close_button.clicked.connect(self.reject)
        self._sync_editing_state()

    def set_image(self, path: str | Path, *, stimulus_id: str | None = None) -> dict[str, Any]:
        self._commit_active()
        metadata = self.model.set_image(path, stimulus_id=stimulus_id)
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            raise ValueError("Qt cannot decode this image format")
        pixmap.setDevicePixelRatio(1.0)
        if self._pixmap_item is not None and self._pixmap_item.scene() is self.scene:
            self.scene.removeItem(self._pixmap_item)
        self._pixmap_item = self.scene.addPixmap(pixmap)
        self._pixmap_item.setZValue(-100)
        self.scene.setSceneRect(QRectF(0, 0, metadata["width"], metadata["height"]))
        self.canvas.set_image_size(metadata["width"], metadata["height"])
        self._image_loaded = True
        self._reload_binding_selector()
        self._load_active_items()
        self.fit_image()
        self._update_metadata_label()
        self._sync_editing_state()
        return metadata

    def set_aois(self, table: Any, *, image_metadata: Mapping[str, Any] | None = None) -> None:
        self._commit_active()
        self.model.set_aois(table, image_metadata=image_metadata)
        self._reload_binding_selector()
        self._load_active_items()
        self._update_metadata_label()
        self._sync_editing_state()

    @property
    def aoi_table(self) -> pd.DataFrame:
        self._commit_active()
        return self.model.table()

    @property
    def image_metadata(self) -> dict[str, Any]:
        return self.model.image_metadata_for_table()

    def validate(self) -> list[dict[str, Any]]:
        self._commit_active()
        return self.model.validate()

    def export_to(self, path: str | Path) -> tuple[Path, Path]:
        self._commit_active()
        return self.model.export_to(path)

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开静态刺激图片", "", "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)"
        )
        if not path:
            return
        try:
            self.set_image(path)
            self.status_label.setText("图片已载入。请从列表选择或输入 stimulus_id，再点击“确认绑定”；不会从文件名推断刺激 ID。")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法载入图片", str(exc))

    def _reload_binding_selector(self) -> None:
        current = self.model.stimulus_id or ""
        self.binding_selector.blockSignals(True)
        self.binding_selector.clear()
        for stimulus in self.model.stimuli:
            self.binding_selector.addItem(stimulus, stimulus)
        self.binding_selector.setCurrentText(current)
        self.binding_selector.blockSignals(False)

    def _confirm_binding(self) -> None:
        if not self._image_loaded:
            QMessageBox.information(self, "需要图片", "请先载入静态图片。")
            return
        pending = str(self.binding_selector.currentText()).strip()
        if not pending:
            QMessageBox.warning(self, "需要 stimulus_id", "请明确选择或输入 stimulus_id，再确认图片绑定。")
            return
        self._commit_active()
        try:
            self.model.bind_stimulus(pending)
        except ValueError as exc:
            QMessageBox.warning(self, "绑定失败", str(exc))
            return
        self._reload_binding_selector()
        self._load_active_items()
        self._update_metadata_label()
        self._sync_editing_state()
        self.status_label.setText(f"已明确绑定当前图片与 stimulus_id={pending}。坐标为原始图像像素，不代表已映射到分析坐标。")

    def _update_metadata_label(self) -> None:
        metadata = self.model.image_metadata
        if not self._image_loaded:
            self.metadata_label.setText("尚未载入图片。")
            return
        bound = self.model.stimulus_id or "未绑定"
        self.metadata_label.setText(
            f"{Path(str(metadata.get('image_path', ''))).name} · {metadata.get('width')}×{metadata.get('height')} px · "
            f"SHA-256 {str(metadata.get('sha256', ''))[:12]}… · stimulus: {bound}"
        )

    def _sync_editing_state(self) -> None:
        enabled = self._image_loaded and self.model.bound
        for button in (self.select_button, self.rect_button, self.polygon_button,
                       self.delete_button, self.finish_polygon_button):
            button.setEnabled(enabled and (button is not self.finish_polygon_button or
                                           (self.canvas.mode == "polygon" and len(self.canvas._polygon_points) >= 3)))
        self.id_edit.setEnabled(enabled)
        self.name_edit.setEnabled(enabled)
        self.priority_edit.setEnabled(enabled)
        self.overlap_selector.setEnabled(enabled)
        self.apply_button.setEnabled(enabled)
        for item in self._items:
            item.set_editable(enabled)
        if not enabled:
            self.status_label.setText("当前图片只读。确认图片与 stimulus_id 的绑定后，才可显示/编辑 AOI、新建或应用。")

    def _set_mode(self, mode: str) -> None:
        if not (self._image_loaded and self.model.bound):
            return
        self.canvas.set_mode(mode)
        self._mode_changed(mode)

    def _mode_changed(self, mode: str) -> None:
        self.select_button.blockSignals(True)
        self.rect_button.blockSignals(True)
        self.polygon_button.blockSignals(True)
        self.select_button.setChecked(mode == "select")
        self.rect_button.setChecked(mode == "rectangle")
        self.polygon_button.setChecked(mode == "polygon")
        self.select_button.blockSignals(False)
        self.rect_button.blockSignals(False)
        self.polygon_button.blockSignals(False)
        self.finish_polygon_button.setEnabled(mode == "polygon" and len(self.canvas._polygon_points) >= 3 and
                                              self.model.bound and self._image_loaded)
        if mode == "polygon_invalid":
            self.status_label.setText("多边形无效：至少需要三个不同顶点，且边不能自交。请继续编辑或取消。")
        elif mode in {"rectangle", "polygon"}:
            self.status_label.setText("在图像上拖动画矩形，或逐点绘制多边形；中键拖动平移，滚轮缩放。")

    def _finish_polygon(self) -> None:
        if not self.canvas.finish_polygon():
            self.status_label.setText("多边形不能完成：至少需要三个不同顶点，且几何必须有效。")

    def _shape_created(self, shape: str, points: Any) -> None:
        if not self.model.bound:
            return
        if shape == "rectangle":
            xs, ys = [point[0] for point in points], [point[1] for point in points]
            geometry = {"shape": "rectangle", "vertices": None,
                        "xmin": min(xs), "ymin": min(ys), "xmax": max(xs), "ymax": max(ys)}
        else:
            try:
                canonical = validate_geometry("polygon", points, None)
            except (AOIGeometryError, TypeError, ValueError) as exc:
                self.status_label.setText(f"多边形无效：{exc}")
                return
            geometry = {"shape": "polygon", "vertices": json.dumps(canonical.vertices, separators=(",", ":")),
                        "xmin": pd.NA, "ymin": pd.NA, "xmax": pd.NA, "ymax": pd.NA}
        record = {
            "aoi_id": f"aoi-{uuid.uuid4().hex[:12]}",
            "stimulus_id": self.model.stimulus_id,
            "aoi_name": f"AOI {len(self._items) + 1}",
            "priority": pd.NA,
            "overlap_policy": "unresolved",
            "unit": "pixel",
            "coordinate_frame": "image_pixel",
            **geometry,
        }
        self._add_item(record)
        self._select_item(self._items[-1])
        self._commit_active()
        self.status_label.setText("AOI 已创建。可拖动形状，拖动顶点/角点编辑，再填写名称与优先级。")

    def _load_active_items(self) -> None:
        self.scene.clearSelection()
        for item in self._items:
            if item.scene() is self.scene:
                self.scene.removeItem(item)
        self._items.clear()
        self.aoi_list.clear()
        if self._pixmap_item is not None and self._pixmap_item.scene() is not self.scene:
            self.scene.addItem(self._pixmap_item)
        if not self.model.bound or not self._image_loaded:
            self._sync_editing_state()
            return
        metadata = self.model.image_metadata
        image_size = (int(metadata.get("width") or 0), int(metadata.get("height") or 0))
        for record in self.model.active_records:
            try:
                self._add_item(record, image_size=image_size)
            except (AOIGeometryError, TypeError, ValueError):
                continue
        self._sync_editing_state()
        self._update_list()

    def _add_item(self, record: Mapping[str, Any], *, image_size: tuple[int, int] | None = None) -> None:
        dimensions = image_size or (int(self.model.image_metadata.get("width") or 0),
                                    int(self.model.image_metadata.get("height") or 0))
        try:
            item = _EditableAOIItem.from_record(record, dimensions)
        except (AOIGeometryError, TypeError, ValueError):
            return
        item.geometryChanged.connect(lambda geometry, target=item: self._geometry_changed(target, geometry))
        self.scene.addItem(item)
        self._items.append(item)
        self._update_list()

    def _update_list(self) -> None:
        current = next((i for i, item in enumerate(self._items) if item.isSelected()), -1)
        self.aoi_list.blockSignals(True)
        self.aoi_list.clear()
        for item in self._items:
            name = str(item.record.get("aoi_name") or "未命名")
            shape = "矩形" if item.shape_name == "rectangle" else "多边形"
            self.aoi_list.addItem(QListWidgetItem(f"{name} · {shape} · {item.record.get('aoi_id', '')}"))
        self.aoi_list.setCurrentRow(current)
        self.aoi_list.blockSignals(False)

    def _select_item(self, item: _EditableAOIItem) -> None:
        self.scene.clearSelection()
        item.setSelected(True)
        self._update_properties(item)
        self._update_list()

    def _scene_selection_changed(self) -> None:
        item = next((candidate for candidate in self._items if candidate.isSelected()), None)
        if item is not None:
            self._update_properties(item)
            self._update_list()

    def _list_selection_changed(self, row: int) -> None:
        if 0 <= row < len(self._items):
            self._select_item(self._items[row])

    def _update_properties(self, item: _EditableAOIItem) -> None:
        self._loading_properties = True
        record = item.record
        self.id_edit.setText(str(record.get("aoi_id") or ""))
        self.name_edit.setText(str(record.get("aoi_name") or ""))
        priority = record.get("priority")
        self.priority_edit.setText("" if priority is None or priority is pd.NA or pd.isna(priority) else str(priority))
        overlap = str(record.get("overlap_policy", "unresolved"))
        index = self.overlap_selector.findData(overlap)
        self.overlap_selector.setCurrentIndex(index if index >= 0 else 0)
        self._loading_properties = False

    def _properties_changed(self, *_: Any) -> None:
        if self._loading_properties or not self.model.bound:
            return
        item = next((candidate for candidate in self._items if candidate.isSelected()), None)
        if item is None:
            return
        aoi_id = self.id_edit.text().strip()
        name = self.name_edit.text().strip()
        priority_text = self.priority_edit.text().strip()
        try:
            priority = pd.NA if not priority_text else float(priority_text)
            if not pd.isna(priority) and not math.isfinite(float(priority)):
                raise ValueError("优先级必须是有限数值。")
        except (TypeError, ValueError) as exc:
            self.status_label.setText(f"优先级无效：{exc}")
            self._update_properties(item)
            return
        item.record.update({
            "aoi_id": aoi_id,
            "aoi_name": name,
            "priority": priority,
            "overlap_policy": self.overlap_selector.currentData() or "unresolved",
        })
        item.update()
        self._update_list()
        self._commit_active()

    def _geometry_changed(self, item: _EditableAOIItem, geometry: Mapping[str, Any]) -> None:
        item.record.update(geometry)
        item.record["stimulus_id"] = self.model.stimulus_id
        item.record["unit"] = "pixel"
        item.record["coordinate_frame"] = "image_pixel"
        self._commit_active()

    def _commit_active(self) -> None:
        if not self.model.bound:
            return
        records = []
        for item in self._items:
            try:
                geometry = item.geometry_record()
            except (AOIGeometryError, TypeError, ValueError):
                continue
            record = dict(item.record)
            record.update(geometry)
            record["stimulus_id"] = self.model.stimulus_id
            record["unit"] = "pixel"
            record["coordinate_frame"] = "image_pixel"
            records.append(record)
        self.model.set_active_records(records)

    def _delete_selected(self) -> None:
        if not self.model.bound:
            return
        item = next((candidate for candidate in self._items if candidate.isSelected()), None)
        if item is None:
            return
        previous_index = self._items.index(item)
        self._items.remove(item)
        self.scene.removeItem(item)
        self._commit_active()
        self._update_list()
        if self._items:
            self._select_item(self._items[min(previous_index, len(self._items) - 1)])

    def fit_image(self) -> None:
        if self._pixmap_item is not None:
            self.canvas.resetTransform()
            self.canvas.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _show_validation(self) -> None:
        issues = self.validate()
        if not issues:
            self.status_label.setText("AOI 结构和几何校验通过；分析坐标映射仍由宿主 preflight 验证。")
            QMessageBox.information(self, "AOI 验证", "AOI 表结构和几何校验通过。")
            return
        message = "\n".join(f"{issue.get('code', 'invalid')}: {issue.get('message', '')}" for issue in issues[:20])
        if len(issues) > 20:
            message += f"\n…以及另外 {len(issues) - 20} 个问题。"
        self.status_label.setText(f"发现 {len(issues)} 个问题。")
        QMessageBox.warning(self, "AOI 验证未通过", message)

    def _apply(self) -> None:
        issues = self.validate()
        if issues:
            message = "\n".join(str(issue.get("message", issue.get("code", "invalid"))) for issue in issues[:10])
            QMessageBox.warning(self, "无法应用 AOI", message)
            return
        self.applyRequested.emit(self.aoi_table, self.image_metadata)
        self.status_label.setText("已向宿主发出 AOI 表和图片元数据；分析前仍需宿主验证坐标映射。")

    def _choose_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出 AOI 表", "aois.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            csv_path, manifest_path = self.export_to(path)
        except (AOIEditorValidationError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导出 AOI", str(exc))
            return
        payload = {
            "csv_path": str(csv_path),
            "manifest_path": str(manifest_path),
            "aoi_table": self.aoi_table,
            "image_metadata": self.image_metadata,
        }
        self.exportRequested.emit(payload)
        self.status_label.setText(f"已导出 {csv_path.name} 和 {manifest_path.name}。")


__all__ = ["AOIEditorDialog"]

