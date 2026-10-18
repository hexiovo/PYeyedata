"""Qt desktop shell for the staged PYeyedata workflow."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import tempfile
from typing import Any, Iterator, Mapping

import pandas as pd
from PySide6.QtCore import Qt, QTimer, QUrl, QSignalBlocker
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QGroupBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from pyeyedata import AnalysisBundle, DesignSpec, NormalizedDataset
from pyeyedata.app_info import APP_NAME, APP_VERSION, PRD_VERSION
from pyeyedata.resource_paths import read_resource_bytes, read_resource_text
from pyeyedata_bootstrap import user_directory

from .bridge import MetricReusePlan, PipelineBridge, PipelineOutcome, PipelinePhase, PipelineStatus
from .dependencies import DependencyTracker
from .design_page import DesignPage
from .group_page import GroupDesignPage
from .import_page import ImportPage
from .metrics_page import MetricsPage
from .models import DatasetRevision, RunRecord, SelectionSpec
from .quality_page import QualityPage
from .results_page import ResultsPage
from .session import AnalysisSession
from .tasks import TaskManager, OperationTaskManager
from .widgets import DataFrameTableModel, DataFrameTableView


WORKFLOW_STEPS = (
    ("数据导入", "选择数据表，告诉软件每列是什么，并确认时间和单位。"),
    ("数据预处理", "检查缺失数据，按需要设置补短缺口、滤波和基线校正。"),
    ("个体分析", "选择要计算的个人指标，例如左瞳孔平均值。"),
    ("多人分析", "选择一起分析的成员和区间，核对时间是否对齐。"),
    ("统计设计", "需要比较差异或关联时，说明分组、样本和结果类型。"),
    ("高级指标", "按研究需要选择方法，并补齐它要求的参考资料。"),
    ("结果与追溯", "查看每次分析的结果、原因和来源，保存运行档案。"),
)

STEP_GUIDANCE = (
    "先做什么：第一次只导入主要信号即可。\n\n例子：time_s 是时间列，单位选秒；pupil_left 是左瞳孔，另填单位 mm。映射就是逐列告诉软件它代表什么。\n\n每个文件区都有“填写说明”和示例。正式导入后再去选择指标。",
    "这一步会影响哪些数字：插值补短缺口，低通滤波减少较快波动，基线校正减去参考时段的水平。\n\n例子：0.1 s 是 100 毫秒；覆盖率 0.8 是 80%。先理解参数，再更改默认值。\n\n点“定位错误参数”可回到出错位置。",
    "先做什么：先选一两个需要的指标，并启用该模块。\n\n例子：只计算左瞳孔平均值，不必准备区域或参考轨迹。\n\n先点“检查可用条件”，读完具体原因，再点“运行分析”。检查本身还没算指标。",
    "先做什么：明确谁和谁在同一次互动中，选好成员和时间范围。\n\n例子：两人都录了 10 秒，不代表他们的第 1 秒能直接对齐；还要核对同步依据。\n\n第一次单人练习可跳过。成员对的数量也不等于独立团队样本数。",
    "先做什么：只有需要比较差异或关联时才配置。\n\n例子：同一个人在 A、B 两条件各测一次，属于重复观测，不能当成两名互不相关的人。\n\n先声明独立样本、条件列和结果类型；不要为了通过检查随意勾选确认。",
    "先做什么：阅读所选方法的条件，按需要补充参考资料。\n\n例子：参考轨迹是已知目标位置随时间的变化，不是随便再导入一份眼动数据。\n\n上下文文件的“填写示例”说明每列和时间单位。只准备真正需要的资料。",
    "先看什么：看具体指标行的状态和原因。complete 表示该行完成，partial 表示部分完成，NA 表示暂无有效数值。\n\n例子：平均值已算出，但未设置的统计推断可能未计算。\n\n保存 .pyea 保留运行；导出表格用于查看；保存 .pyeplan 复用设置。",
)


def _license_materials() -> tuple[tuple[str, str], ...]:
    """Read and verify the text license materials declared by the resource index."""
    index = json.loads(read_resource_text("resource-index.json"))
    if not isinstance(index, dict) or index.get("schema") != "pyeyedata.resource-index/v1":
        raise ValueError("resource-index.json has an unsupported schema")
    license_index = index.get("licenses")
    if not isinstance(license_index, dict):
        raise ValueError("resource-index.json has no licenses entry")
    material_hashes = license_index.get("material_sha256")
    if not isinstance(material_hashes, dict) or not material_hashes:
        raise ValueError("resource-index.json has no indexed license materials")

    materials: list[tuple[str, str]] = []
    for resource_path, expected_digest in material_hashes.items():
        if (not isinstance(resource_path, str) or "\\" in resource_path
                or not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(character not in "0123456789abcdef" for character in expected_digest)):
            raise ValueError("resource-index.json contains an invalid license material entry")
        parsed_path = PurePosixPath(resource_path)
        if (parsed_path.is_absolute() or parsed_path.as_posix() != resource_path
                or any(part in {"", ".", ".."} for part in resource_path.split("/"))
                or len(parsed_path.parts) < 2
                or parsed_path.parts[0] not in {"licenses", "fonts"}
                or parsed_path.suffix.lower() not in {".json", ".md", ".txt"}):
            raise ValueError(f"Invalid canonical license resource path: {resource_path!r}")

        content = read_resource_bytes(resource_path)
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(f"License material hash mismatch: {resource_path}")
        if parsed_path.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"License material is not UTF-8 text: {resource_path}") from exc
        if not text.strip():
            raise ValueError(f"License material is empty: {resource_path}")
        materials.append((resource_path, text.strip()))

    preferred = {
        "licenses/THIRD-PARTY-NOTICES.md": 0,
        "licenses/QT-SOURCE-AND-REBUILD.md": 1,
    }
    materials.sort(key=lambda item: (preferred.get(item[0], 2), item[0]))
    if not materials:
        raise ValueError("resource-index.json contains no displayable license text materials")
    return tuple(materials)

# Individual analysis always returns module side tables (for example source
# events, fixation audits, heatmap cells and scanpath evidence).  These basic
# pupil summaries are derived only from the current shared pupil processing and
# their metric fingerprints; they do not consume those individual side tables.
# Keep this list narrow so a cached metric can never stand in for omitted audit
# or event-level evidence.
_DETAIL_INDEPENDENT_METRICS = {
    "individual": frozenset({
        "individual.coverage.pupil_left",
        "individual.coverage.pupil_right",
        "individual.pupil_left.mean",
        "individual.pupil_left.median",
        "individual.pupil_left.sd",
        "individual.pupil_left.iqr",
        "individual.pupil_right.mean",
        "individual.pupil_right.median",
        "individual.pupil_right.sd",
        "individual.pupil_right.iqr",
        "individual.pupil.interocular_difference",
        "individual.pupil.interocular_difference.median",
        "individual.pupil.interocular_difference.sd",
        "individual.pupil.interocular_difference.iqr",
    }),
}


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        bridge: PipelineBridge | None = None,
        session: AnalysisSession | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge or PipelineBridge()
        self.session = session or AnalysisSession()
        self.bridge.bind_execution_cache(self.session.execution_cache, self.session.is_current)
        self.task_manager = TaskManager(self)
        self.operation_manager = OperationTaskManager(self)
        self._operation_result = None
        self._archive_paths = {}
        self._prepared_archive = None
        self._archived_records: set[int] = set()
        self._archive_saving_record = None
        self._archive_before_close = False
        self._close_decision_active = False
        self._selection = SelectionSpec()
        self._parameters: dict[str, Any] = {}
        self._context_tables: dict[str, pd.DataFrame] = {}
        self._context_raw_sources: dict[str, bytes] = {}
        self._import_metadata: dict[str, Any] = {}
        self._individual_context_parameters: dict[str, Any] = {}
        self._group_configuration: dict[str, Any] = {"group": {}, "metadata": {}}
        self._statistics_design: dict[str, Any] = {}
        self._snapshots: dict[tuple[str, int], Any] = {}
        self._snapshot_phases: dict[tuple[str, int], PipelinePhase] = {}
        self._stale_run_ids: set[str] = set()
        self._suppress_config_updates = False
        self._dependency_tracker = DependencyTracker()
        self._latest_dependency_change = None
        self._preview_model = DataFrameTableModel(parent=self)

        self.setWindowTitle("PYeyedata · 眼动数据分析")
        self._build_file_menu()
        self._build_help_menu()
        self._fitted_on_first_show = False
        self._build_toolbar()
        self._build_central_layout()
        self._fit_to_available_screen()
        self._connect_tasks()
        self._connect_pages()
        self._refresh_dependency_state()
        self.statusBar().showMessage("未载入数据")

    def _build_file_menu(self) -> None:
        menu = self.menuBar().addMenu("文件")
        self.save_plan_action = QAction("保存分析方案…", self)
        self.save_plan_action.setShortcut("Ctrl+Shift+S")
        self.save_plan_action.setToolTip("保存预处理、指标、方法参数和统计设计，供下次复用；不保存数据或结果。")
        self.load_plan_action = QAction("加载分析方案…", self)
        self.load_plan_action.setShortcut("Ctrl+Shift+O")
        self.save_plan_action.triggered.connect(self._save_plan_dialog)
        self.load_plan_action.triggered.connect(self._load_plan_dialog)
        menu.addAction(self.save_plan_action)
        menu.addAction(self.load_plan_action)

    def _configuration_busy(self) -> bool:
        return self.task_manager.active or self.operation_manager.active or self.import_page.import_active

    def analysis_plan(self, name: str = "我的分析方案") -> dict[str, Any]:
        from .analysis_plan import make_plan, validate_plan
        if self._configuration_busy():
            raise RuntimeError("请等待当前任务结束后再保存方案。")
        selection = self.metrics_page.selection_spec()
        configuration = {
            "selection": selection.to_dict(),
            "experimental": dict(self.metrics_page._allow_experimental),
            "processing": self.quality_page.processing_config(),
            "metrics": self.metrics_page.all_parameter_values(),
            "group": self.group_page.analysis_plan_parameters(selection.group_metric_ids),
            "statistics_design": self.design_page.configuration_values(),
        }
        schema = self.bridge.schema()
        payload = make_plan(configuration, pipeline_schema=str(schema["pipeline_schema_version"]), name=name)
        validate_plan(payload, schema)
        return payload

    def apply_analysis_plan(self, payload: Mapping[str, Any]) -> None:
        from .analysis_plan import validate_plan
        if self._configuration_busy():
            raise RuntimeError("请等待当前任务结束后再加载方案。")
        configuration = validate_plan(payload, self.bridge.schema())
        selection = SelectionSpec(**configuration["selection"])
        # All setters have already succeeded on isolated pages. Block cross-page
        # signals until the complete configuration has been installed.
        blockers = [QSignalBlocker(page) for page in
                    (self.metrics_page, self.quality_page, self.group_page, self.design_page)]
        try:
            with self._suppress_configuration_events():
                self.metrics_page.invalidate_preflight()
                self.quality_page.set_processing_config(configuration["processing"])
                self.metrics_page.set_parameter_values(configuration["metrics"], emit=False, strict=True)
                self.metrics_page._allow_experimental = dict(configuration["experimental"])
                self.metrics_page.set_selection(selection)
                self.group_page.set_group_metric_ids(selection.group_metric_ids)
                self.group_page.set_parameter_values(configuration["group"])
                self.design_page.set_design_spec(configuration["statistics_design"])
                self._selection = self.metrics_page.selection_spec()
                self._parameters = {}
                self._statistics_design = deepcopy(configuration["statistics_design"])
        finally:
            del blockers
        self._invalidate_configuration("已加载分析方案；请按本次数据重新检查可用条件")
        self.status_label.setText("方案已加载。请核对当前数据、多人范围和统计设计，再点“检查可用条件”。")

    def _save_plan_dialog(self) -> None:
        from .analysis_plan import write_plan
        try:
            payload = self.analysis_plan()
            path, _ = QFileDialog.getSaveFileName(self, "保存分析方案（不含数据和结果）", "我的分析方案.pyeplan", "分析方案 (*.pyeplan)")
            if not path:
                return
            if not path.lower().endswith(".pyeplan"):
                path += ".pyeplan"
            payload["name"] = Path(path).stem[:200]
            write_plan(path, payload)
            self.status_label.setText("方案已保存：" + path + "；分析结果请另存为运行档案。")
        except Exception as exc:
            self._route_configuration_error(exc)
            QMessageBox.warning(self, "方案未保存", "请先修正提示的设置，再保存方案。\n\n" + str(exc))

    def _load_plan_dialog(self) -> None:
        from .analysis_plan import read_plan, validate_plan, plan_summary
        path, _ = QFileDialog.getOpenFileName(self, "加载分析方案", "", "分析方案 (*.pyeplan)")
        if not path:
            return
        try:
            payload = read_plan(path)
            validate_plan(payload, self.bridge.schema())
            box = QMessageBox(self)
            box.setWindowTitle("确认应用方案")
            box.setText(plan_summary(payload))
            apply_button = box.addButton("应用方案", QMessageBox.ButtonRole.AcceptRole)
            keep_button = box.addButton("保持当前设置", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep_button)
            box.setEscapeButton(keep_button)
            box.exec()
            if box.clickedButton() is apply_button:
                self.apply_analysis_plan(payload)
        except Exception as exc:
            QMessageBox.warning(self, "方案未加载", str(exc) + "\n当前设置保持原样。")

    def _build_help_menu(self) -> None:
        help_menu = self.menuBar().addMenu("帮助")
        self.help_action = QAction("新手入门与文件示例", self)
        self.help_action.setShortcut("F1")
        self.help_action.triggered.connect(self._open_help)
        help_menu.addAction(self.help_action)
        export_action = QAction("导出练习资料与模板…", self)
        export_action.triggered.connect(self._export_learning_pack)
        help_menu.addAction(export_action)
        pdf_action = QAction("离线使用手册（PDF）", self)
        pdf_action.triggered.connect(self._open_pdf_guide)
        help_menu.addAction(pdf_action)
        help_menu.addSeparator()
        about_action = QAction("关于 PYeyedata", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        licenses_action = QAction("许可与第三方声明", self)
        licenses_action.triggered.connect(self._show_licenses)
        help_menu.addAction(licenses_action)

    def _show_about(self) -> None:
        from pyeyedata.contracts import CONTRACT_VERSION
        from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION

        QMessageBox.about(
            self,
            f"关于 {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n"
            f"产品需求基线 PRD {PRD_VERSION}\n\n"
            f"数据契约版本：{CONTRACT_VERSION}\n"
            f"分析流程 schema：{PIPELINE_SCHEMA_VERSION}\n"
            "具体方法算法版本随运行结果和档案一并记录。",
        )

    def _show_licenses(self) -> None:
        try:
            text = "\n\n".join(
                f"{'=' * 18} {path} {'=' * 18}\n\n{content}"
                for path, content in _license_materials()
            )
        except Exception as exc:
            QMessageBox.warning(self, "许可材料不可用", f"无法读取或校验索引中的许可材料：{exc}")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("许可与第三方声明")
        dialog.resize(900, 680)
        layout = QVBoxLayout(dialog)
        viewer = QPlainTextEdit(dialog)
        viewer.setReadOnly(True)
        viewer.setPlainText(text)
        layout.addWidget(viewer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主操作", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        self.open_action = QAction("打开数据", self)
        self.open_action.setToolTip("打开数据导入步骤")
        self.open_action.setEnabled(True)
        self.preflight_action = QAction("检查可用条件", self)
        self.preflight_action.setEnabled(False)
        self.preflight_action.setToolTip("执行共享预处理并核对各方法的可用条件；不会计算所选指标。")
        self.run_action = QAction("运行分析", self)
        self.run_action.setEnabled(False)
        self.cancel_action = QAction("取消当前任务", self)
        self.cancel_action.setEnabled(False)
        for action in (self.open_action, self.preflight_action, self.run_action, self.cancel_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.help_action)
        self.preflight_action.triggered.connect(self.start_preflight)
        self.run_action.triggered.connect(self.start_analysis)
        self.cancel_action.triggered.connect(self._cancel_current_task)
        self.open_action.triggered.connect(lambda: self.step_list.setCurrentRow(0))

    def _build_central_layout(self) -> None:
        root = QWidget(self)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 8)

        workspace = QSplitter(Qt.Orientation.Horizontal, root)
        self.step_list = QListWidget(workspace)
        self.step_list.setMinimumWidth(150)
        for name, _ in WORKFLOW_STEPS:
            self.step_list.addItem(name)
        self.step_list.setCurrentRow(0)

        self.pages = QStackedWidget(workspace)
        self.import_page = ImportPage(self.pages)
        self.quality_page = QualityPage(self.pages)
        schema = self.bridge.schema()
        self.metrics_page = MetricsPage(schema=schema, parent=self.pages)
        self.group_page = GroupDesignPage(self.pages)
        self.design_page = DesignPage(schema=schema, parent=self.pages)
        self.results_page = ResultsPage(self.pages)
        self.results_page_scroll = QScrollArea(self.pages)
        self.results_page_scroll.setObjectName("resultsPageScrollArea")
        self.results_page_scroll.setWidgetResizable(True)
        self.results_page_scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.results_page_scroll.setWidget(self.results_page)
        self.data_page = QWidget(self.pages)
        data_layout = QVBoxLayout(self.data_page)
        self.page_title = QLabel("数据导入", self.data_page)
        self.page_title.setObjectName("pageTitle")
        self.page_description = QLabel(WORKFLOW_STEPS[0][1], self.data_page)
        self.page_description.setWordWrap(True)
        self.dataset_summary = QLabel("尚未载入标准化数据集。", self.data_page)
        self.dataset_summary.setObjectName("datasetSummary")
        self.table_view = DataFrameTableView(self.data_page)
        self.table_view.setModel(self._preview_model)
        self.table_view.setSortingEnabled(False)
        self.import_page_scroll = QScrollArea(self.data_page)
        self.import_page_scroll.setObjectName("importPageScrollArea")
        self.import_page_scroll.setWidgetResizable(True)
        self.import_page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.import_page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.import_page_scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.import_page_scroll.setWidget(self.import_page)
        data_layout.addWidget(self.page_title)
        data_layout.addWidget(self.page_description)
        data_layout.addWidget(self.dataset_summary)
        data_layout.addWidget(self.import_page_scroll, 3)
        data_layout.addWidget(self.table_view, 2)
        for widget in (self.data_page, self.quality_page, self.metrics_page,
                       self.group_page, self.design_page, self.results_page_scroll):
            self.pages.addWidget(widget)

        help_panel = QGroupBox("参数说明", workspace)
        help_panel.setMinimumWidth(220)
        help_layout = QVBoxLayout(help_panel)
        self.help_text = QLabel("指标参数和适用条件按当前方法显示。\n\n前置检查会执行本次数据所需的共享预处理，再核对方法条件；它不会计算所选指标或统计结果，也不会认证设备。", help_panel)
        self.help_text.setWordWrap(True)
        self.help_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        help_layout.addWidget(self.help_text)

        workspace.addWidget(self.step_list)
        workspace.addWidget(self.pages)
        workspace.addWidget(help_panel)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setStretchFactor(2, 0)
        workspace.setSizes([175, 850, 250])
        outer.addWidget(workspace, 1)

        task_row = QHBoxLayout()
        self.status_label = QLabel("未配置")
        self.progress_bar = QProgressBar(root)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        task_row.addWidget(self.status_label)
        task_row.addWidget(self.progress_bar, 1)
        outer.addLayout(task_row)

        log_box = QGroupBox("运行日志", root)
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit(log_box)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setMinimumHeight(110)
        self.log_view.setPlaceholderText("运行阶段、后端消息和错误详情会显示在这里。")
        log_layout.addWidget(self.log_view)
        outer.addWidget(log_box)
        self.setCentralWidget(root)

        self.step_list.currentRowChanged.connect(self._on_step_changed)
        self._on_step_changed(0)

    def _fit_to_available_screen(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        client = self.geometry()
        frame_width = max(8, frame.width() - client.width())
        frame_height = max(40, frame.height() - client.height())
        width = max(1, min(1360, available.width() - frame_width))
        height = max(1, min(860, available.height() - frame_height))
        self.setMinimumSize(min(1050, width), min(700, height))
        self.resize(width, height)
        resized_frame = self.frameGeometry()
        has_native_frame = resized_frame.width() != width or resized_frame.height() != height
        frame_width_after = resized_frame.width() if has_native_frame else width + frame_width
        frame_height_after = resized_frame.height() if has_native_frame else height + frame_height
        self.move(
            available.left() + max(0, (available.width() - frame_width_after) // 2),
            available.top() + max(0, (available.height() - frame_height_after) // 2),
        )

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self._fitted_on_first_show:
            self._fit_to_available_screen()
            self._fitted_on_first_show = True
            # The platform frame metrics can still be pending during the first
            # show event. Refit once the native window has entered the event loop.
            QTimer.singleShot(0, self._fit_to_available_screen)

    def _connect_tasks(self) -> None:
        self.task_manager.progress_changed.connect(self._on_progress)
        self.task_manager.state_changed.connect(self._on_state_changed)
        self.task_manager.completed.connect(self._on_completed)
        self.task_manager.failed.connect(self._on_failed)
        self.task_manager.task_finished.connect(self._on_task_finished)
        self.task_manager.idle.connect(self._on_task_manager_idle)

    def _connect_pages(self) -> None:
        self.import_page.import_completed.connect(self._on_import_completed)
        self.import_page.import_failed.connect(lambda message: self._append_log("导入失败：" + message))
        self.import_page.import_progress.connect(self._on_import_progress)
        self.import_page.import_state_changed.connect(self._on_import_state_changed)
        self.import_page.import_idle.connect(self._on_import_idle)
        self.import_page.dataset_invalidated.connect(self._on_dataset_invalidated)
        self.import_page.configuration_changed.connect(self._on_import_configuration_changed)
        self.quality_page.configuration_changed.connect(self._on_processing_configuration_changed)
        self.metrics_page.selection_changed.connect(self._on_selection_changed)
        self.metrics_page.parameters_changed.connect(self._on_metrics_parameters_changed)
        self.metrics_page.preflight_requested.connect(self.start_preflight)
        self.metrics_page.edit_requirements_requested.connect(self._on_edit_requirements)
        self.group_page.configuration_changed.connect(self._on_group_configuration_changed)
        self.group_page.scope_changed.connect(self._on_group_scope_changed)
        self.metrics_page.parameter_error_button.clicked.connect(
            lambda: self._activate_parameter_error_page("metrics")
        )
        self.quality_page.parameter_error_button.clicked.connect(
            lambda: self._activate_parameter_error_page("quality")
        )
        self.group_page.parameter_error_button.clicked.connect(
            lambda: self._activate_parameter_error_page("group")
        )
        self.design_page.configuration_changed.connect(self._on_design_configuration_changed)
        self.results_page.history_selected.connect(self._on_history_selected)
        self.results_page.archive_requested.connect(self._save_archive_dialog)
        self.results_page.reopen_requested.connect(self._open_archive_dialog)
        self.results_page.full_export_requested.connect(self._export_dialog)
        self.results_page.artifact_button.clicked.connect(self._view_archive_artifact)
        self.results_page.help_requested.connect(self._open_help)
        self.results_page.set_c_capabilities(archive=True, reopen=True, full_export=True)
        self.operation_manager.progress.connect(self._on_operation_progress)
        self.operation_manager.completed.connect(self._on_operation_completed)
        self.operation_manager.failed.connect(self._on_operation_failed)
        self.operation_manager.cancelled.connect(self._on_operation_cancelled)
        self.operation_manager.idle.connect(self._on_operation_idle)

    def _cancel_current_task(self):
        self.task_manager.cancel()
        self.operation_manager.cancel()
        self.import_page.cancel_import()

    def _start_operation(self, kind, operation):
        if self.task_manager.active or self.operation_manager.active or self.import_page.import_active:
            raise RuntimeError("请先完成或取消当前后台任务。")
        self._operation_kind = kind
        self._operation_session_generation = self.session.generation
        self._operation_result = None
        self.cancel_action.setEnabled(True)
        self.results_page.set_c_capabilities(busy=True)
        self.preflight_action.setEnabled(False)
        self.run_action.setEnabled(False)
        self.operation_manager.start(operation)
        self._refresh_busy_state()

    def _on_operation_progress(self, generation, fraction, message):
        if generation == self.operation_manager.generation:
            self.progress_bar.setValue(round(float(fraction) * 100))
            self.status_label.setText(message)

    def _on_operation_completed(self, generation, result):
        if generation != self.operation_manager.generation:
            if hasattr(result, "close"):
                result.close()
            return
        self._operation_result = (generation, result)

    def _on_operation_failed(self, generation, details):
        if generation == self.operation_manager.generation:
            self._archive_before_close = False
            self.status_label.setText("归档/导出失败；原文件和当前结果保持不变")
            self._append_log(details)
            QMessageBox.warning(self, "操作失败", details)

    def _on_operation_cancelled(self, generation):
        if generation == self.operation_manager.generation:
            self._archive_before_close = False
            self._append_log("归档/导出任务已取消；当前结果保持不变。")

    def _on_operation_idle(self):
        self._refresh_dependency_state()
        result, self._operation_result = self._operation_result, None
        if result is not None:
            generation, result = result
            if generation != self.operation_manager.generation:
                if hasattr(result, "close"):
                    result.close()
                result = None
        if result is not None:
            kind = self._operation_kind
            if kind == "prepare":
                self._prepared_archive = result
                dialog = QDialog(self)
                dialog.setWindowTitle("保存前检查实际档案内容")
                dialog.resize(900, 640)
                layout = QVBoxLayout(dialog)
                text = QPlainTextEdit(dialog); text.setReadOnly(True)
                text.setPlainText(result.preview.format_text()); layout.addWidget(text)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, dialog)
                buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    prepared = result
                    self._start_operation("save", lambda cancel, progress: prepared.commit(self._archive_destination, cancel=cancel, progress=progress))
                else:
                    self._archive_before_close = False
                    result.close(); self._prepared_archive = None
            elif kind == "load":
                if self.session.generation == self._operation_session_generation:
                    self.session.execution_cache.clear()
                    self.session.record(result.record)
                    self._archived_records.add(id(result.record))
                    self._archive_paths[id(result.record)] = Path(self._archive_open_path)
                    self.results_page.set_history(self.session.history)
                    self.results_page.show_record(result.record)
                    self.step_list.setCurrentRow(6)
                    reasons = result.app_archive.get("completeness_reasons", [])
                    self.status_label.setText(("仅结果：不能完整重算" if result.archive_mode == "results_only" else "已打开完整档案的只读运行记录") + ("；" + "；".join(reasons) if reasons else ""))
                else:
                    self._append_log("档案读取期间输入/配置已改变；已丢弃迟到结果。")
            elif kind == "save":
                if self._archive_saving_record is not None:
                    self._mark_archive_committed(self._archive_saving_record, Path(self._archive_destination))
                    self._archive_saving_record = None
                if self._prepared_archive is not None:
                    self._prepared_archive.close(); self._prepared_archive = None
                self.status_label.setText("运行档案已校验并安全保存")
                if self._archive_before_close:
                    QTimer.singleShot(0, self.close)
            else:
                self.status_label.setText("导出完成：" + str(getattr(result, "path", result)))
        elif self._operation_kind == "save" and self._prepared_archive is not None:
            self._prepared_archive.close(); self._prepared_archive = None
        self._refresh_busy_state()
        if getattr(self, "_close_pending", False):
            self._maybe_finish_close()

    def _save_archive_dialog(self, record):
        from pyeyedata.archive_session import prepare_run_archive
        if self._configuration_busy():
            self.status_label.setText("请等待当前任务结束后再归档。")
            return False
        choices = ["完整档案（含真实输入）", "仅结果（不能完整重算）"] if record.snapshot is not None else ["仅结果（不能完整重算）"]
        choice, accepted = QInputDialog.getItem(self, "档案内容", "选择本次运行的保存方式", choices, 0, False)
        if not accepted:
            self._archive_before_close = False
            return False
        mode = "full" if choice.startswith("完整") else "results_only"
        path, _ = QFileDialog.getSaveFileName(self, "保存所选运行", record.run_id + ".pyea", "PYeyedata 档案 (*.pyea)")
        if not path:
            self._archive_before_close = False
            return False
        if not path.lower().endswith(".pyea"):
            path += ".pyea"
        self._archive_destination = path
        self._archive_saving_record = record
        try:
            static_artifacts = self._capture_current_figures(record)
            static_omissions = tuple(self._current_figure_omissions)
            self._start_operation("prepare", lambda cancel, progress: prepare_run_archive(record, mode=mode, cancel=cancel, progress=progress, static_artifacts=static_artifacts, static_omissions=static_omissions))
            return True
        except Exception as exc:
            self._archive_before_close = False
            QMessageBox.warning(self, "档案保存失败", str(exc))
            return False

    def _capture_current_figures(self, record):
        from pyeyedata.reporting import FigureArtifact, render_existing_figure_artifact
        artifacts = []
        self._current_figure_omissions = []
        for index, tab in enumerate((self.results_page.overview_tab, self.results_page.qc_tab,
                                     self.results_page.individual_tab, self.results_page.group_tab,
                                     self.results_page.statistics_tab)):
            if getattr(tab, "record", None) is not record:
                self._current_figure_omissions.append((f"view/{tab.tab_name}", "该图属于另一运行或配置；没有绑定到所选运行。"))
                continue
            if not tab.chart.chart_available:
                self._current_figure_omissions.append((f"view/{tab.tab_name}", "此结果分类没有可保存的实际图；保留原表及静态报告，不创建示意图。"))
                continue
            table_name, _positions = tab.source_binding()
            # A live Qt Figure remains on the GUI thread throughout serialization.
            rendered = render_existing_figure_artifact(record, tab.chart.figure,
                         table_name=table_name, source_tables=(table_name,),
                         source_label="保存时的实际可见结果图", selection=tab.figure_selection(),
                         formats=("png",), dpi=300)
            data = rendered.formats["png"]
            artifacts.append(FigureArtifact(f"app_archive/figures/current-view-{index}.png", "image/png", "png", data,
                                            hashlib.sha256(data).hexdigest(), len(data), rendered.provenance))
        return tuple(artifacts)

    def _open_archive_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开运行档案", "", "PYeyedata 档案 (*.pyea)")
        if path:
            self.open_run_archive(path)

    def open_run_archive(self, path):
        from pyeyedata.archive_session import load_run_archive
        self._archive_open_path = str(path)
        self._start_operation("load", lambda cancel, progress: load_run_archive(path, cancel=cancel, progress=progress))

    def start_table_export(self, record, request, preview):
        from pyeyedata.exports import export_table
        message = (f"表：{preview.table_name}\n范围：{preview.scope}\n行数：{preview.row_count:,}/{preview.source_row_count:,}"
                   f"\n列数：{len(preview.output_columns)}\n公式风险文本：{preview.formula_like_text_cells}"
                   f"\n工作表：{preview.worksheet_count}\n" + "\n".join(preview.warnings))
        if QMessageBox.question(self, "导出内容预览", message) == QMessageBox.StandardButton.Yes:
            self._start_operation(
                "export",
                lambda cancel, progress: export_table(record, request, cancel=cancel, progress=progress),
            )

    def _export_dialog(self, _format=None):
        from pyeyedata.exports import create_table_export_request, preview_table_export
        from pyeyedata.reporting import write_analysis_report
        record = self.results_page.current_record()
        if record is None:
            return
        choice, ok = QInputDialog.getItem(self, "导出", "选择实际结果范围", ["CSV 表", "XLSX 表", "PDF 分析报告"], 0, False)
        if not ok:
            return
        fmt = "pdf" if choice.startswith("PDF") else "xlsx" if choice.startswith("XLSX") else "csv"
        if fmt == "pdf":
            path, _ = QFileDialog.getSaveFileName(self, "保存分析报告", record.run_id + ".pdf", "PDF (*.pdf)")
            if path:
                self._start_operation(
                    "export",
                    lambda cancel, progress: write_analysis_report(
                        record, path, overwrite=True, cancel=cancel, progress=progress
                    ),
                )
            return
        names = record.table_names()
        name, ok = QInputDialog.getItem(self, "结果表", "选择完整 typed 结果表", names, 0, False)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出完整表", name + "." + fmt, f"{fmt.upper()} (*.{fmt})")
        if path:
            request = create_table_export_request(record, name, path, file_format=fmt, overwrite=True)
            self.start_table_export(record, request, preview_table_export(record, request))

    def _view_archive_artifact(self):
        from pyeyedata.store import read_pyea
        record = self.results_page.current_record()
        if record is None:
            return
        path = self._archive_paths.get(id(record))
        items = record.metadata().get("app_archive", {}).get("artifacts", [])
        names = [item["member"] for item in items]
        name, ok = QInputDialog.getItem(self, "档案内静态图/PDF", "选择已保存成员", names, 0, False)
        if ok and path:
            try:
                content = read_pyea(path).read_member(name)
                self._open_cached_bytes(Path(name).name, content)
            except Exception as exc:
                QMessageBox.warning(self, "无法查看档案成员", str(exc))

    def _open_cached_bytes(self, name, content):
        root = user_directory("cache", "viewed-artifacts")
        destination = root / (hashlib.sha256(content).hexdigest()[:16] + "-" + Path(name).name)
        destination.write_bytes(content)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination))):
            raise RuntimeError("系统没有可打开此文件的查看器；可在缓存目录中另存或打开：" + str(destination))

    def _open_help(self, _message=None):
        from .help_center import show_help
        show_help(self)

    def _export_learning_pack(self, _checked=False):
        from .help_center import export_learning_pack_dialog
        export_learning_pack_dialog(self)

    def _open_pdf_guide(self, _checked=False):
        from pyeyedata.resource_paths import resource_path
        try:
            with resource_path("help/PYeyedata-user-guide-zh.pdf") as path:
                self._open_cached_bytes(path.name, path.read_bytes())
        except Exception as exc:
            QMessageBox.information(self, "离线使用说明", str(exc))

    def set_dataset_revision(self, revision: DatasetRevision, *, preview: pd.DataFrame | None = None) -> None:
        self.session.install_dataset(revision)
        dataset = revision.dataset_copy()
        self._context_tables = self._current_context_tables()
        self._context_raw_sources = self._current_context_raw_sources()
        self._import_metadata = self._current_import_metadata()
        self._individual_context_parameters = self._current_individual_context_parameters()
        self.dataset_summary.setText(
            f"修订 {revision.revision_id} · {len(dataset.signals):,} 信号行 · "
            f"源指纹 {revision.fingerprint[:12]} · 设备/生理验证状态按数据声明保留"
        )
        self._preview_model.set_frame(preview if preview is not None else dataset.signals.head(500))
        with self._suppress_configuration_events():
            self.quality_page.set_dataset(revision)
            self.quality_page.set_context_tables(self._context_tables, self.import_page.context_source_manifest())
            self.group_page.set_dataset(revision, events=self._context_tables.get("external_events"))
            self.group_page.set_group_metric_ids(self.metrics_page.selection_spec().group_metric_ids)
            self.design_page.set_performance_table(dataset.performance)
            self.metrics_page.invalidate_preflight()
        self.preflight_action.setEnabled(True)
        self.run_action.setEnabled(True)
        self.status_label.setText("数据已载入，等待显式配置")
        self.statusBar().showMessage("已载入数据修订 " + revision.revision_id)
        self._refresh_dependency_state()
        self._refresh_busy_state()

    def _current_context_tables(self) -> dict[str, pd.DataFrame]:
        return {
            str(name): frame.copy(deep=True)
            for name, frame in self.import_page.context_tables().items()
            if isinstance(frame, pd.DataFrame)
        }

    def _current_context_raw_sources(self) -> dict[str, bytes]:
        return {str(name): bytes(data) for name, data in self.import_page.context_raw_source_bytes().items()}

    def _current_import_metadata(self) -> dict[str, Any]:
        value = self.import_page.context_metadata()
        result = dict(value) if isinstance(value, Mapping) else {}
        result.update(self.import_page.analysis_metadata())
        return result

    def _current_individual_context_parameters(self) -> dict[str, Any]:
        value = self.import_page.context_individual_parameters()
        return dict(value) if isinstance(value, Mapping) else {}

    def _on_import_completed(self, revision: DatasetRevision, preview: pd.DataFrame) -> None:
        self._context_tables = self._current_context_tables()
        self._context_raw_sources = self._current_context_raw_sources()
        self._import_metadata = self._current_import_metadata()
        self._individual_context_parameters = self._current_individual_context_parameters()
        self.set_dataset_revision(revision, preview=preview)
        self._append_log(
            f"导入完成：{len(revision.dataset_copy().signals):,} 个规范化信号行；"
            f"具名上下文表 {', '.join(sorted(self._context_tables)) or '无'}。"
        )

    def _on_import_progress(self, percent: int, message: str) -> None:
        if self.import_page.import_active:
            self.progress_bar.setValue(max(0, min(100, int(percent))))
            self.status_label.setText(message)

    def _on_import_state_changed(self, active: bool, message: str) -> None:
        if active and message:
            self.status_label.setText(message)
            self.statusBar().showMessage(message)
        elif not active and not (self.task_manager.active or self.operation_manager.active):
            self.status_label.setText(self.import_page.status.text())
            self.statusBar().showMessage(self.import_page.status.text())
        self.cancel_action.setText("取消导入" if active else "取消当前任务")
        self._refresh_busy_state()

    def _on_import_idle(self) -> None:
        self._refresh_busy_state()
        self._maybe_finish_close()

    def _refresh_busy_state(self) -> None:
        if not hasattr(self, "import_page"):
            return
        pipeline_busy = self.task_manager.active
        operation_busy = self.operation_manager.active
        import_busy = self.import_page.import_active
        other_busy = pipeline_busy or operation_busy
        self.import_page.set_external_busy(other_busy)
        busy = other_busy or import_busy
        self.save_plan_action.setEnabled(not busy)
        self.load_plan_action.setEnabled(not busy)
        ready = self.session.dataset_revision is not None
        self.preflight_action.setEnabled(bool(ready and not busy))
        self.run_action.setEnabled(bool(ready and not busy))
        self.cancel_action.setEnabled(busy)
        self.results_page.set_c_capabilities(
            archive=True, reopen=True, full_export=True, busy=busy
        )

    def _on_dataset_invalidated(self, reason: str) -> None:
        if self.session.dataset_revision is None:
            return
        self.session.clear_dataset()
        self._preview_model.set_frame(None)
        self.dataset_summary.setText("来源、映射或作用域配置已变化；请重新导入以生成新修订。")
        with self._suppress_configuration_events():
            self.quality_page.set_dataset(None)
            self.group_page.set_dataset(pd.DataFrame())
            self.design_page.set_performance_table(None)
            self.metrics_page.invalidate_preflight()
        self.preflight_action.setEnabled(False)
        self.run_action.setEnabled(False)
        self._append_log("数据修订已失效：" + str(reason))
        self._invalidate_configuration("normalized input changed")

    def _on_import_configuration_changed(self) -> None:
        if self._suppress_config_updates:
            return
        self._context_tables = self._current_context_tables()
        self._context_raw_sources = self._current_context_raw_sources()
        self._import_metadata = self._current_import_metadata()
        self._individual_context_parameters = self._current_individual_context_parameters()
        if self.session.dataset_revision is not None:
            with self._suppress_configuration_events():
                self.quality_page.set_context_tables(self._context_tables, self.import_page.context_source_manifest())
                self.group_page.set_dataset(self.session.dataset_revision, events=self._context_tables.get("external_events"))
        self._invalidate_configuration("import context or metadata changed")

    def _on_processing_configuration_changed(self, values: object) -> None:
        if self._suppress_config_updates:
            return
        if isinstance(values, Mapping) and values:
            self._parameters["processing"] = deepcopy(dict(values))
        self._invalidate_configuration("processing configuration changed")

    def _on_selection_changed(self, selection: SelectionSpec) -> None:
        if self._suppress_config_updates:
            return
        self._selection = selection
        with self._suppress_configuration_events():
            self.group_page.set_group_metric_ids(selection.group_metric_ids)
        self._invalidate_configuration("metric selection changed")

    def _on_metrics_parameters_changed(self, _values: object) -> None:
        if self._suppress_config_updates:
            return
        self._invalidate_configuration("typed metric parameter changed")

    def _on_group_configuration_changed(self, values: object) -> None:
        if self._suppress_config_updates:
            return
        if isinstance(values, Mapping):
            self._group_configuration = deepcopy(dict(values))
        self._invalidate_configuration("group configuration changed")

    def _on_group_scope_changed(self, _values: object) -> None:
        if self._suppress_config_updates:
            return
        self._invalidate_configuration("group scope changed")

    def _on_design_configuration_changed(self, values: object) -> None:
        if self._suppress_config_updates:
            return
        if isinstance(values, Mapping):
            self._statistics_design = deepcopy(dict(values))
        self._invalidate_configuration("statistical design changed")

    @contextmanager
    def _suppress_configuration_events(self) -> Iterator[None]:
        previous = self._suppress_config_updates
        self._suppress_config_updates = True
        try:
            yield
        finally:
            self._suppress_config_updates = previous

    def _invalidate_configuration(self, reason: str) -> None:
        if self.session.active_snapshot is not None:
            self.session.invalidate_active_run()
        self.metrics_page.invalidate_preflight()
        if self.import_page.import_active:
            self.status_label.setText(self.import_page.pending_import_message)
        elif self.session.dataset_revision is not None:
            self.status_label.setText("配置已变化；当前结果保留为历史，需重新前检或运行")
        self._append_log("配置失效：" + reason)
        self._refresh_dependency_state()

    def _on_step_changed(self, row: int) -> None:
        if row < 0:
            return
        page_index = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 2, 6: 5}.get(row, 0)
        if row == 2:
            self.metrics_page.set_module("individual")
        elif row == 5:
            self.metrics_page.set_module("advanced")
        self.pages.setCurrentIndex(page_index)
        name, description = WORKFLOW_STEPS[row]
        self.page_title.setText(name)
        self.page_description.setText(description)
        self.help_text.setText(
            STEP_GUIDANCE[row] + "\n\n按 F1 查看完整练习和文件示例。"
        )

    def set_selection(self, selection: SelectionSpec) -> None:
        self._selection = selection
        with self._suppress_configuration_events():
            self.metrics_page.set_selection(selection)
            self.group_page.set_group_metric_ids(selection.group_metric_ids)
        self._invalidate_configuration("host supplied selection changed")

    def set_parameters(self, values: Mapping[str, Any]) -> None:
        parameters = deepcopy(dict(values))
        with self._suppress_configuration_events():
            self.quality_page.set_processing_config(parameters.get("processing"))
            self.metrics_page.set_parameter_values(parameters)
            self.group_page.set_parameter_values(parameters)
            design = parameters.get("statistics_design")
            if design is None:
                design = parameters.get("statistics")
            if isinstance(design, (DesignSpec, Mapping)):
                self.design_page.set_design_spec(design)
        self._parameters = parameters
        self._invalidate_configuration("host supplied parameters changed")

    def start_preflight(self) -> None:
        self._start_pipeline(PipelinePhase.PREFLIGHT)

    def start_analysis(self) -> None:
        self._start_pipeline(PipelinePhase.RUN)

    def _start_pipeline(self, phase: PipelinePhase) -> None:
        if self.task_manager.active or self.operation_manager.active or self.import_page.import_active:
            self.status_label.setText("已有任务运行中")
            return
        try:
            schema = self.bridge.schema()
            selection = self.metrics_page.selection_spec()
            if selection.run_group and self.group_page.scope_state().get("mode") != "group":
                raise ValueError("群体指标已启用，但群体页处于个体范围预览；请切回‘群体分析’后再检查或运行。")
            parameters, context, scoped_revision = self._snapshot_configuration(selection)
            snapshot = self.session.create_snapshot(
                selection=selection,
                schema_version=str(schema["pipeline_schema_version"]),
                parameters=parameters,
                context=context,
                dataset_revision=scoped_revision,
            )
            snapshot_key = (snapshot.run_id, snapshot.generation)
            self._snapshots[snapshot_key] = snapshot
            self._snapshot_phases[snapshot_key] = phase
            self._refresh_dependency_state()
            reuse_plan = self._metric_reuse_plan(snapshot) if phase is PipelinePhase.RUN else None
            self.task_manager.start(snapshot, phase=phase, bridge=self.bridge, reuse_plan=reuse_plan)
        except Exception as exc:
            if "snapshot_key" in locals():
                self._snapshots.pop(snapshot_key, None)
                self._snapshot_phases.pop(snapshot_key, None)
            self._append_log(f"任务未启动：{type(exc).__name__}: {exc}")
            self.status_label.setText("配置失败")
            self._route_configuration_error(exc)
            return
        self.preflight_action.setEnabled(False)
        self.run_action.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("运行中：" + ("前提检查" if phase is PipelinePhase.PREFLIGHT else "分析"))
        self._refresh_busy_state()

    def _route_configuration_error(self, error: Exception) -> None:
        """Surface parameter errors on their owning page and focus the field."""
        message = str(error)
        quality_name = self.quality_page._parameter_error_name
        if message.startswith("processing.") or (quality_name is not None and quality_name in message):
            if self.quality_page._parameter_error_name is None:
                self.quality_page._report_parameter_error(message)
            self._activate_parameter_error_page("quality")
            return
        metric_location = self.metrics_page._parameter_error_location
        if metric_location is not None and metric_location[1] in message:
            self._activate_parameter_error_page("metrics")
            return
        group_location = self.group_page._parameter_error_location
        if "group." in message or (group_location is not None and group_location[1] in message):
            if self.group_page._parameter_error_location is None:
                self.group_page._report_parameter_error(message)
            self._activate_parameter_error_page("group")

    def _activate_parameter_error_page(self, page: str) -> None:
        if page == "quality":
            row, focus = 1, self.quality_page._focus_parameter_error
        elif page == "group":
            row, focus = 3, self.group_page._focus_parameter_error
        elif page == "metrics":
            location = self.metrics_page._parameter_error_location
            row = 5 if location is not None and location[0] == "advanced" else 2
            focus = self.metrics_page._focus_parameter_error
        else:
            return
        self.step_list.setCurrentRow(row)
        QTimer.singleShot(0, focus)

    def _snapshot_configuration(
        self, selection: SelectionSpec
    ) -> tuple[dict[str, Any], dict[str, Any], DatasetRevision]:
        revision = self.session.dataset_revision
        if revision is None:
            raise RuntimeError("请先完成 CSV/XLSX 导入并规范化。")
        dataset = revision.dataset_copy()
        context = {name: frame.copy(deep=True) for name, frame in self._context_tables.items()}
        parameters = self._compose_parameters(dataset, selection=selection)
        run_revision = revision
        normalized_input_changed = False
        if selection.run_group:
            scoped_signals = self.group_page.slice_signals(dataset.signals)
            scoped_segments = self.group_page.slice_segments(dataset.segments)
            normalized_input_changed = not scoped_signals.equals(dataset.signals) or not scoped_segments.equals(dataset.segments)
            reduces_scope = normalized_input_changed
            dataset.signals = scoped_signals
            dataset.segments = scoped_segments
            for name in ("external_events", "events"):
                if isinstance(context.get(name), pd.DataFrame):
                    source_events = context[name]
                    scoped_events = self.group_page.slice_events(source_events)
                    reduces_scope = reduces_scope or not scoped_events.equals(source_events)
                    context[name] = scoped_events
            if reduces_scope and (selection.run_individual or selection.run_advanced):
                raise ValueError(
                    "当前多人范围会缩小信号、片段或事件输入，不能与全量个体/高级模块放在同一运行中；"
                    "请将多人与全量个体/高级分析分开运行，避免改变其输入范围。"
                )
            context["group_scope"] = self.group_page.scope_state()
        raw_sources = {**revision.raw_source_bytes(), **self._context_raw_sources}
        if normalized_input_changed or raw_sources != revision.raw_source_bytes():
            run_revision = DatasetRevision.create(dataset, raw_sources=raw_sources)
        context_manifest = self.import_page.context_source_manifest()
        if context_manifest:
            parameters.setdefault("metadata", {})["context_source_manifest"] = context_manifest
        return parameters, context, run_revision

    def _compose_parameters(
        self, dataset: NormalizedDataset, *, selection: SelectionSpec | None = None
    ) -> dict[str, Any]:
        # Restored host values are the baseline; the values visible in typed
        # pages are layered over that baseline immediately before a snapshot.
        output: dict[str, Any] = deepcopy(self._parameters)

        def merge_into(target: dict[str, Any], source: Mapping[str, Any]) -> None:
            for key, value in source.items():
                if isinstance(value, Mapping) and isinstance(target.get(key), Mapping):
                    nested = deepcopy(dict(target[key]))
                    merge_into(nested, value)
                    target[key] = nested
                else:
                    target[key] = deepcopy(value)

        selection = selection or self.metrics_page.selection_spec()
        quality = self.quality_page.processing_config()
        metric_values = self.metrics_page.parameter_values()
        merge_into(output, metric_values)
        group_values = self.group_page.configuration_values(include_group=selection.run_group)
        if isinstance(group_values.get("group"), Mapping):
            group = dict(output.get("group", {})) if isinstance(output.get("group"), Mapping) else {}
            merge_into(group, group_values["group"])
            output["group"] = group
        output["processing"] = deepcopy(quality)
        individual = dict(output.get("individual", {}))
        merge_into(individual, self._individual_context_parameters)
        live_individual = metric_values.get("individual")
        if isinstance(live_individual, Mapping):
            merge_into(individual, live_individual)
        if individual:
            output["individual"] = individual
        design_values = self.design_page.configuration_values()
        if isinstance(design_values, Mapping):
            output["statistics_design"] = deepcopy(dict(design_values))
        metadata = dict(output.get("metadata", {})) if isinstance(output.get("metadata"), Mapping) else {}
        merge_into(metadata, self._import_metadata)
        group_metadata = group_values.get("metadata")
        if isinstance(group_metadata, Mapping) and group_metadata:
            # Group evidence describes cross-member comparability and must not
            # overwrite source-level import declarations used by individual
            # metrics. Keep it with the group consumer configuration.
            group = dict(output.get("group", {}))
            nested_metadata = dict(group.get("metadata", {})) if isinstance(group.get("metadata"), Mapping) else {}
            nested_metadata.update(deepcopy(dict(group_metadata)))
            group["metadata"] = nested_metadata
            output["group"] = group
        if metadata:
            output["metadata"] = metadata
        return output

    def _metric_dependency_specs(self) -> dict[str, dict[str, list[str]]]:
        """Map typed controls to the method IDs whose calculations consume them."""
        from .method_registry import group_parameter_fields, individual_parameter_fields

        specs: dict[str, dict[str, list[str]]] = {}
        for module, provider in (
            ("individual", individual_parameter_fields),
            ("group", group_parameter_fields),
        ):
            for field in provider():
                if not isinstance(field, Mapping):
                    continue
                path = field.get("config_path")
                methods = field.get("methods", ())
                if not isinstance(path, str) or not isinstance(methods, (list, tuple)):
                    continue
                for metric_id in methods:
                    if not isinstance(metric_id, str) or not metric_id.startswith(module + "."):
                        continue
                    spec = specs.setdefault(metric_id, {
                        "parameter_prefixes": [], "input_prefixes": [], "table_refs": [],
                    })
                    spec["parameter_prefixes"].append(path)

        schema = self.bridge.schema()
        advanced = schema.get("advanced", {}) if isinstance(schema, Mapping) else {}
        entries = (
            [*advanced.get("methods", ()), *advanced.get("parent_child_specs", ())]
            if isinstance(advanced, Mapping) else []
        )
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("metric_id"), str):
                continue
            metric_id = str(entry["metric_id"])
            definitions = entry.get("parameter_definitions", ())
            paths: list[str] = []
            if isinstance(definitions, (list, tuple)):
                for field in definitions:
                    if not isinstance(field, Mapping):
                        continue
                    path = field.get("config_path")
                    if not isinstance(path, str):
                        name = field.get("name")
                        path = "advanced." + name if isinstance(name, str) else None
                    if isinstance(path, str):
                        paths.append(path)
            if paths:
                spec = specs.setdefault(metric_id, {
                    "parameter_prefixes": [], "input_prefixes": [], "table_refs": [],
                })
                spec["parameter_prefixes"].extend(paths)
        return specs

    def _metric_reuse_plan(self, snapshot: Any) -> MetricReusePlan | None:
        """Build a conservative per-metric plan from an earlier complete run."""
        current_nodes = self._dependency_tracker.node_fingerprints
        dependencies = {record.run_id: record for record in self._dependency_tracker.history}
        requested = snapshot.selection
        module_values = {
            "individual": (requested.individual_metric_ids, requested.run_individual),
            "group": (requested.group_metric_ids, requested.run_group),
            "advanced": (requested.advanced_metric_ids, requested.run_advanced),
        }
        candidates: list[tuple[int, int, MetricReusePlan]] = []
        for order, record in enumerate(reversed(self.session.history)):
            if record.phase != "run" or record.status != "complete" or record.snapshot is None:
                continue
            dependency = dependencies.get(record.run_id)
            if dependency is None or dependency.status != "complete":
                continue
            try:
                old_metrics = record.table_copy("metrics")
            except KeyError:
                continue
            if not isinstance(old_metrics, pd.DataFrame) or "metric_id" not in old_metrics:
                continue

            old_selection = record.snapshot.selection
            old_selected = {
                "individual": set(old_selection.individual_metric_ids),
                "group": set(old_selection.group_metric_ids),
                "advanced": set(old_selection.advanced_metric_ids),
            }
            old_fingerprints = dependency.node_fingerprints
            modules_with_details: set[str] = set()
            for table_name in record.table_names():
                module = next((name for name in ("individual", "group", "advanced")
                               if table_name.startswith(name + "_")), None)
                if module is None:
                    continue
                try:
                    detail = record.table_copy(table_name)
                except KeyError:
                    continue
                if isinstance(detail, pd.DataFrame) and not detail.empty:
                    # Without a proven metric/scope join, keep the whole module
                    # on the recompute path instead of omitting side evidence.
                    modules_with_details.add(module)
            reused: dict[str, tuple[str, ...]] = {}
            for module, (metric_ids, enabled) in module_values.items():
                if not enabled:
                    continue
                matching: list[str] = []
                for metric_id in metric_ids:
                    node = f"metric.{module}.{metric_id}"
                    if (module in modules_with_details
                            and metric_id not in _DETAIL_INDEPENDENT_METRICS.get(module, frozenset())):
                        continue
                    if metric_id not in old_selected[module]:
                        continue
                    if node not in old_fingerprints or node not in current_nodes:
                        continue
                    if old_fingerprints.get(node) != current_nodes.get(node):
                        continue
                    if old_metrics["metric_id"].astype("string").eq(metric_id).any():
                        matching.append(metric_id)
                if matching:
                    reused[module] = tuple(sorted(set(matching)))
            score = sum(len(values) for values in reused.values())
            if not score:
                continue

            requested_ids = {
                "individual": requested.individual_metric_ids,
                "group": requested.group_metric_ids,
                "advanced": requested.advanced_metric_ids,
            }
            calculation_ids: dict[str, tuple[str, ...]] = {}
            calculation_flags: dict[str, bool] = {}
            for module, metric_ids in requested_ids.items():
                enabled = module_values[module][1]
                if not enabled:
                    calculation_ids[module] = ()
                    calculation_flags[module] = False
                    continue
                uncached = tuple(metric_id for metric_id in metric_ids
                                 if metric_id not in reused.get(module, ()))
                calculation_ids[module] = uncached
                calculation_flags[module] = bool(uncached) if reused.get(module) else True

            calculation_selection = SelectionSpec(
                individual_metric_ids=calculation_ids["individual"],
                group_metric_ids=calculation_ids["group"],
                advanced_metric_ids=calculation_ids["advanced"],
                run_individual=calculation_flags["individual"],
                run_group=calculation_flags["group"],
                run_advanced=calculation_flags["advanced"],
            )
            reused_ids = {metric_id for values in reused.values() for metric_id in values}
            cached = old_metrics.loc[old_metrics["metric_id"].astype("string").isin(reused_ids)].copy()
            candidates.append((score, order, MetricReusePlan(
                source_run_id=record.run_id,
                metrics=cached,
                reused_selection=reused,
                calculation_selection=calculation_selection,
                source_record=record,
            )))
        if not candidates:
            return None
        # Score first, then prefer the newest source when coverage is tied.
        return max(candidates, key=lambda item: (item[0], -item[1]))[2]

    def _refresh_dependency_state(self) -> None:
        if not hasattr(self, "results_page"):
            return
        revision = self.session.dataset_revision
        inputs: dict[str, Any] = {}
        table_refs: dict[str, Any] = {
            "context": self._context_tables,
            "metric_dependencies": self._metric_dependency_specs(),
        }
        if revision is not None:
            dataset = revision.dataset_copy()
            inputs = {
                "normalized": {
                    "fingerprint": revision.fingerprint,
                    "signals": dataset.signals,
                    "segments": dataset.segments,
                    "aois": dataset.aois,
                    "performance": dataset.performance,
                },
                "time_metadata": {key: value for key, value in dataset.metadata.items()
                                  if key.startswith(("time", "clock", "source_time", "normalized_time", "synchronization"))},
                "source_identity": [item for item in dataset.source_manifest
                                    if item.get("role") == "signals" or str(item.get("raw_source_key", "")).startswith("signals/")]
                                   or ([item for item in dataset.source_manifest if not item.get("role") and not item.get("raw_source_key")]),
                "raw_sources": revision.raw_source_bytes(),
            }
            table_refs["normalized"] = {
                "signals": dataset.signals,
                "segments": dataset.segments,
                "aois": dataset.aois,
                "performance": dataset.performance,
            }
            table_refs["raw_sources"] = self._context_raw_sources
            table_refs["context"] = self._context_tables
        inputs["group_scope"] = self.group_page.scope_state()
        try:
            parameters = (
                self._compose_parameters(
                    revision.dataset_copy(), selection=self.metrics_page.selection_spec()
                ) if revision is not None else self._parameters
            )
        except Exception:
            parameters = deepcopy(self._parameters)
        parameters = {**dict(parameters), "selection": self.metrics_page.selection_spec().to_dict()}
        schema = self.bridge.schema()
        change = self._dependency_tracker.update(
            inputs=inputs,
            parameters=parameters,
            algorithm_versions={
                "pipeline": schema.get("pipeline_schema_version"),
                "contract": schema.get("contract_version"),
                "aoi_geometry": schema.get("aoi_geometry"),
            },
            table_refs=table_refs,
        )
        self._latest_dependency_change = change
        self.results_page.set_dependency_state(
            change.stale_nodes,
            baseline_run_id=self._dependency_tracker.baseline_run_id,
            changed_nodes=change.changed_nodes,
        )

    def _is_current(self, run_id: str, generation: int) -> bool:
        return self.session.is_current(run_id, generation)

    def _on_progress(self, run_id: str, generation: int, fraction: float, message: str) -> None:
        if not self._is_current(run_id, generation):
            return
        if 0.0 <= fraction <= 1.0:
            self.progress_bar.setValue(round(fraction * 100))
        self._append_log(f"{message} · 后端进度 {fraction:.0%}")

    def _on_state_changed(self, run_id: str, generation: int, status: str, message: str) -> None:
        if not self._is_current(run_id, generation):
            return
        labels = {
            "running": "运行中",
            "cancel_requested": "已请求取消，等待当前计算结束",
            "complete": "完成",
            "partial": "部分完成",
            "failed": "失败",
            "cancelled": "已取消",
            "unknown": "状态未确认",
        }
        self.status_label.setText(labels.get(status, status))
        self.statusBar().showMessage(message or labels.get(status, status))

    def _on_completed(self, run_id: str, generation: int, outcome: PipelineOutcome) -> None:
        current = self._is_current(run_id, generation)
        if current:
            audit = outcome.metadata.get("execution_cache", {})
            if isinstance(audit, Mapping):
                stages = audit.get("stages", [])
                reused = list(dict.fromkeys(item.get("stage") for item in stages
                                           if isinstance(item, Mapping) and item.get("outcome") == "hit"))
                labels = {"base": "质量与预处理", "eyes": "眼动数据流", "events": "事件检测",
                          "artifact:individual": "个体完整结果", "artifact:group": "群体完整结果",
                          "artifact:advanced": "高级完整结果"}
                if reused:
                    self._append_log("本次实际复用：" + "、".join(labels.get(name, name) for name in reused)
                                     + "；当前条件检查及统计按本次选择执行。")
                if audit.get("commit_status") == "discarded_incomplete_cancelled_or_stale":
                    self._append_log("本次未发布新缓存；先前有效缓存按原状态保留。")
        if outcome.phase is PipelinePhase.PREFLIGHT:
            if not current:
                self._append_log(f"过期前提检查 {run_id} 已返回；未覆盖当前页面状态。")
                return
            if outcome.availability is not None:
                with self._suppress_configuration_events():
                    self.metrics_page.set_availability(outcome.availability)
                    self._selection = self.metrics_page.selection_spec()
            with self._suppress_configuration_events():
                self.quality_page.set_outcome(outcome)
                self.results_page.set_preflight(outcome)
            self.status_label.setText("前提检查返回：" + outcome.backend_status)
            self._append_log(f"共享预处理及方法条件检查结束，状态={outcome.backend_status}；尚未计算所选指标或统计结果。")
            self.step_list.setCurrentRow(6)
            self._refresh_dependency_state()
            return
        if isinstance(outcome.payload, AnalysisBundle):
            snapshot = self._snapshots.get((run_id, generation))
            if snapshot is not None:
                record = RunRecord.from_bundle(
                    snapshot,
                    outcome.payload,
                    effective_status=outcome.status.value,
                    stale_at_return=not current,
                )
                self.session.record(record)
                if current:
                    self._dependency_tracker.record_run(run_id, outcome.status.value)
                else:
                    self._stale_run_ids.add(run_id)
                self.results_page.set_history(self.session.history)
            if not current:
                if snapshot is not None:
                    self._append_log(f"过期运行 {run_id} 已作为只读历史保存；未覆盖当前视图。")
                self._refresh_dependency_state()
                return
            if outcome.status not in {PipelineStatus.COMPLETE, PipelineStatus.PARTIAL}:
                labels = {
                    PipelineStatus.CANCELLED: "已取消",
                    PipelineStatus.FAILED: "失败",
                    PipelineStatus.UNKNOWN: "状态未确认",
                }
                self.status_label.setText(labels.get(outcome.status, "状态未确认"))
                self._append_log(
                    f"分析未形成可展示的完成结果：界面任务状态={outcome.status.value}；"
                    "已有结果视图保持不变，真实返回已保留在历史中。"
                )
                self._refresh_dependency_state()
                return
            with self._suppress_configuration_events():
                metrics = outcome.payload.tables.get("metrics")
                self._preview_model.set_frame(metrics)
                if isinstance(metrics, pd.DataFrame):
                    self.design_page.set_metric_table(metrics)
                active = snapshot or self.session.active_snapshot
                if active is not None:
                    self.design_page.set_performance_table(active.dataset_copy().performance)
                design_tables = dict(outcome.payload.tables)
                for public_name in ("statistics", "descriptives", "diagnostics", "audit"):
                    current_table = design_tables.get(public_name)
                    if not isinstance(current_table, pd.DataFrame):
                        current_table = design_tables.get("statistics_" + public_name)
                    if isinstance(current_table, pd.DataFrame):
                        design_tables[public_name] = current_table
                self.design_page.set_statistical_results(design_tables, outcome.payload.metadata)
                self.group_page.set_results(
                    metrics=(
                        metrics.loc[metrics["metric_id"].astype("string").str.startswith("group.")].copy()
                        if isinstance(metrics, pd.DataFrame) and "metric_id" in metrics else pd.DataFrame()
                    ),
                    lag_curve=outcome.payload.tables.get("group_lag_curve"),
                )
                self.quality_page.set_outcome(outcome)
                self.results_page.set_bundle(outcome.payload, snapshot=snapshot)
            self.step_list.setCurrentRow(6)
            self._append_log(
                f"分析返回，后端状态={outcome.backend_status}，界面任务状态={outcome.status.value}。"
            )
            self.status_label.setText({
                PipelineStatus.COMPLETE: "完成",
                PipelineStatus.PARTIAL: "部分完成",
            }.get(outcome.status, "状态未确认"))
            self._refresh_dependency_state()
        elif current:
            self.status_label.setText("状态未确认")
            self._append_log(f"分析未返回可展示的 AnalysisBundle；后端状态={outcome.backend_status}。")

    def _on_failed(self, run_id: str, generation: int, error_type: str, details: str) -> None:
        key = (run_id, generation)
        snapshot = self._snapshots.get(key)
        if snapshot is not None:
            record = RunRecord.from_failure(
                snapshot,
                phase=self._snapshot_phases.get(key, PipelinePhase.RUN).value,
                failure_type=error_type,
                failure_reason=details,
                stale_at_return=not self._is_current(run_id, generation),
            )
            self.session.record(record)
            if self._is_current(run_id, generation):
                self._dependency_tracker.record_run(run_id, "failed")
            else:
                self._stale_run_ids.add(run_id)
            self.results_page.set_history(self.session.history)
        if self._is_current(run_id, generation):
            self.status_label.setText("失败")
            self.statusBar().showMessage("分析失败；已有结果视图保持不变")
        self._append_log(f"{error_type}: {details}")

    def _on_task_finished(self, run_id: str, generation: int) -> None:
        self.session.finish(run_id, generation)
        key = (run_id, generation)
        self._snapshots.pop(key, None)
        self._snapshot_phases.pop(key, None)
        self._refresh_busy_state()

    def _on_history_selected(self, record: RunRecord) -> None:
        if not isinstance(record, RunRecord):
            return
        if self.operation_manager.active and self._operation_kind == "load":
            self.operation_manager.cancel()
        try:
            stale_nodes = self._dependency_tracker.changed_nodes_since(record.run_id)
        except KeyError:
            stale_nodes = ()
        if record.run_id in self._stale_run_ids and not stale_nodes:
            stale_nodes = ("stale_at_return",)
        self.results_page.show_record(record, stale_nodes=stale_nodes)

    def _on_edit_requirements(self, _values: object) -> None:
        self.step_list.setCurrentRow(0)
        self.import_page.source_tabs.setCurrentIndex(self.import_page.source_tabs.count() - 1)
        self._append_log("条件详情已定位到上下文导入面板；添加目标/参考资料后重新前检。")

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def closeEvent(self, event: Any) -> None:
        if self._close_decision_active:
            event.ignore()
            return
        if self.task_manager.active or self.operation_manager.active or self.import_page.import_active:
            if self._archive_before_close and self.operation_manager.active:
                self.status_label.setText("正在保存运行档案，保存完成后继续退出检查。")
                event.ignore()
                return
            self._close_pending = True
            self.task_manager.cancel()
            self.operation_manager.cancel()
            if self.import_page.import_active:
                self.import_page.cancel_import()
                self.status_label.setText("已请求取消导入；等待当前文件读取结束后关闭")
            else:
                self.status_label.setText("已请求取消；等待当前后端任务结束后关闭")
            event.ignore()
            return
        records = self.unarchived_records()
        if records:
            event.ignore()
            self._close_decision_active = True
            try:
                decision = "archive" if self._archive_before_close else self._confirm_unarchived_close(records)
            finally:
                self._close_decision_active = False
            if decision == "archive":
                self._archive_before_close = True
                self._save_archive_dialog(records[0])
                return
            self._archive_before_close = False
            if decision != "discard":
                return
        self.session.execution_cache.clear()
        super().closeEvent(event)

    def unarchived_records(self) -> tuple[RunRecord, ...]:
        return tuple(record for record in self.session.history if id(record) not in self._archived_records)

    def _mark_archive_committed(self, record: RunRecord, path: Path) -> None:
        # Reusing a filename replaces its earlier run. That earlier record must
        # become unsaved again rather than silently disappearing on exit.
        canonical = os.path.normcase(str(path.resolve()))
        for record_id, previous in tuple(self._archive_paths.items()):
            if record_id != id(record) and os.path.normcase(str(previous.resolve())) == canonical:
                self._archived_records.discard(record_id)
                del self._archive_paths[record_id]
        self._archived_records.add(id(record))
        self._archive_paths[id(record)] = path

    def _confirm_unarchived_close(self, records: tuple[RunRecord, ...]) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("还有分析记录未归档")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"还有 {len(records)} 次分析记录只保存在本次窗口中，退出后无法从历史列表找回。")
        box.setInformativeText("保存方案或导出 CSV/PDF 不等于归档。选择“逐个归档后退出”可以保存结果；完整档案还会包含本次输入。")
        archive = box.addButton("逐个归档后退出", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("不归档，直接退出", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("返回继续使用", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        return "archive" if box.clickedButton() is archive else "discard" if box.clickedButton() is discard else "cancel"

    def _on_task_manager_idle(self) -> None:
        self._refresh_busy_state()
        self._maybe_finish_close()

    def _maybe_finish_close(self) -> None:
        if getattr(self, "_close_pending", False):
            if not (self.task_manager.active or self.operation_manager.active or self.import_page.import_active):
                self._close_pending = False
                self.close()

