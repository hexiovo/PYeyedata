"""Qt desktop shell for the staged PYeyedata workflow."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import tempfile
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Iterator, Mapping

import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, QUrl, QSignalBlocker, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QTextOption
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
from pyeyedata.io import read_table
from pyeyedata.resource_paths import read_resource_bytes, read_resource_text
from pyeyedata_bootstrap import user_directory

from .bridge import MetricReusePlan, PipelineBridge, PipelineOutcome, PipelinePhase, PipelineStatus
from .batch_queue import BatchQueue, BatchQueueError, BatchItem
from .contextual_help import ContextualHelpController
from .dependencies import DependencyTracker
from .design_page import DesignPage
from .group_page import GroupDesignPage
from .import_page import ImportPage
from .metrics_page import MetricsPage
from .models import DatasetRevision, RunRecord, SelectionSpec
from .quality_page import QualityPage
from .remediation import NavigationTarget, _target_for_diagnostic
from .results_page import ResultsPage
from .session import AnalysisSession
from .tasks import TaskManager, OperationTaskManager
from .task_timing import TaskTiming, TimingEstimator, format_duration, format_timing
from .widgets import DataFrameTableModel, DataFrameTableView
from .import_profiles import compare_structure_signatures


WORKFLOW_STEPS = (
    ("数据导入", "选择数据表，告诉软件每列是什么，并确认时间和单位。"),
    ("数据预处理", "检查缺失数据，按需要设置补短缺口、滤波和基线校正。"),
    ("个体分析", "选择要计算的个人指标，例如左瞳孔平均值。"),
    ("多人分析", "选择一起分析的成员和区间，核对时间是否对齐。"),
    ("统计设计", "需要比较差异或关联时，说明分组、样本和结果类型。"),
    ("高级指标", "按研究需要选择方法，并补齐它要求的参考资料。"),
    ("结果与追溯", "查看每次分析的结果、原因和来源，保存运行档案。"),
    ("运行日志", "在独立页面查看运行阶段、后台消息和错误详情；关键失败也会显示在状态区或错误提示中。"),
)

STEP_GUIDANCE = (
    "先做什么：第一次只导入主要信号即可。\n\n例子：time_s 是时间列，单位选秒；pupil_left 是左瞳孔，另填单位 mm。映射就是逐列告诉软件它代表什么。\n\n每个文件区都有“填写说明”和示例。正式导入后再去选择指标。",
    "这一步会影响哪些数字：插值补短缺口，低通滤波减少较快波动，基线校正减去参考时段的水平。\n\n例子：0.1 s 是 100 毫秒；覆盖率 0.8 是 80%。先理解参数，再更改默认值。\n\n点“定位错误参数”可回到出错位置。",
    "先做什么：先选一两个需要的指标，并启用该模块。\n\n例子：只计算左瞳孔平均值，不必准备区域或参考轨迹。\n\n先点“检查可用条件”，读完具体原因，再点“运行分析”。检查本身还没算指标。",
    "先做什么：明确谁和谁在同一次互动中，选好成员和时间范围。\n\n例子：两人都录了 10 秒，不代表他们的第 1 秒能直接对齐；还要核对同步依据。\n\n第一次单人练习可跳过。成员对的数量也不等于独立团队样本数。",
    "先做什么：只有需要比较差异或关联时才配置。\n\n例子：同一个人在 A、B 两条件各测一次，属于重复观测，不能当成两名互不相关的人。\n\n先声明独立样本、条件列和结果类型；不要为了通过检查随意勾选确认。",
    "先做什么：阅读所选方法的条件，按需要补充参考资料。\n\n例子：参考轨迹是已知目标位置随时间的变化，不是随便再导入一份眼动数据。\n\n上下文文件的“填写示例”说明每列和时间单位。只准备真正需要的资料。",
    "先看什么：看具体指标行的状态和原因。complete 表示该行完成，partial 表示部分完成，NA 表示暂无有效数值。\n\n例子：平均值已算出，但未设置的统计推断可能未计算。\n\n保存 .pyea 保留运行；导出表格用于查看；保存 .pyeplan 复用设置。",
    "运行日志保留最近的运行阶段、后台消息和排错详情。日志只读；运行失败也会在状态区或错误提示中给出，不需要一直停留在此页。",
)


class _BatchInspectionSignals(QObject):
    completed = Signal(int, object)
    progress = Signal(int, int, int, str)


class _BatchInputInspector(QRunnable):
    """Hash and parse candidate inputs away from the GUI thread."""

    def __init__(self, generation, paths, source_request, expected_columns, mapped_columns):
        super().__init__()
        self.generation = int(generation)
        self.paths = tuple(str(Path(path).expanduser().resolve()) for path in paths)
        self.source_request = source_request
        self.expected_columns = tuple(expected_columns)
        self.mapped_columns = tuple(str(column) for column in mapped_columns)
        self.signals = _BatchInspectionSignals()

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @Slot()
    def run(self):
        results = []
        total = len(self.paths)
        for index, source_path in enumerate(self.paths, start=1):
            digest = None
            hash_error = None
            preflight_error = None
            structure_confirmed = False
            message = ""
            self.signals.progress.emit(
                self.generation, index - 1, total,
                f"文件 {index}/{total}：正在计算输入文件哈希",
            )
            try:
                before = Path(source_path).stat()
                digest = self._sha256(source_path)
                self.signals.progress.emit(
                    self.generation, index - 1, total,
                    f"文件 {index}/{total}：哈希完成，正在读取表头并核对结构",
                )
                imported = read_table(
                    source_path,
                    role="signals",
                    sheet_name=self.source_request.sheet_name,
                    csv_options=self.source_request.csv_options,
                    preview_rows=100,
                )
                after = Path(source_path).stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError("文件在结构检查期间发生变化；请重新加入该文件。")
                if imported.frame.columns.has_duplicates:
                    raise RuntimeError("信号表存在重复列名，无法安全核对映射。")
                actual_columns = tuple(str(column) for column in imported.frame.columns)
                comparison = compare_structure_signatures(
                    {"signals": self.expected_columns}, {"signals": actual_columns}
                )
                missing_mapping = [column for column in self.mapped_columns if column not in actual_columns]
                if comparison.compatible and not missing_mapping:
                    structure_confirmed = True
                    message = "信号列结构与已确认预设一致。"
                else:
                    details = []
                    if not comparison.compatible:
                        difference = comparison.differences[0]
                        details.append(
                            "列结构变化：缺少 " + (", ".join(difference.removed_columns) or "无")
                            + "；新增 " + (", ".join(difference.added_columns) or "无")
                            + ("；列顺序变化" if difference.reordered else "")
                        )
                    if missing_mapping:
                        details.append("当前映射引用了缺失列：" + ", ".join(missing_mapping))
                    message = "mapping_confirmation_required：" + "；".join(details)
            except Exception as exc:
                preflight_error = f"{type(exc).__name__}: {exc}"
                message = "输入预检失败：" + preflight_error
                if digest is None:
                    hash_error = preflight_error
            results.append({
                "source_path": source_path,
                "source_sha256": digest,
                "source_hash_error": hash_error,
                "preflight_error": preflight_error,
                "structure_confirmed": structure_confirmed,
                "message": message,
            })
            self.signals.progress.emit(
                self.generation, index, total,
                f"已完成 {index}/{total} 个文件的哈希和结构检查",
            )
        self.signals.completed.emit(self.generation, results)


@dataclass(frozen=True, slots=True)
class _BatchExportFailureReceipt:
    """A failed export receipt that keeps already committed paths observable."""

    run_id: str
    error: str
    output_paths: tuple[Path, ...]
    cancelled: bool = False
    complete: bool = False
    commit_complete: bool = True


def _batch_json_safe(value: Any) -> Any:
    """Convert frozen configuration values into finite, manifest-safe JSON data."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _batch_json_safe(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _batch_json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_batch_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_batch_json_safe(item) for item in value), key=str)
    if hasattr(value, "item") and callable(value.item):
        try:
            return _batch_json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not pd.notna(value):
        return str(value)
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _batch_signature_value(value: Any) -> Any:
    """Project only known configuration values into a collision-safe JSON tree."""
    if isinstance(value, np.generic):
        try:
            scalar = value.item()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"批次配置标量 {type(value).__name__} 无法安全核对。") from exc
        if scalar is value:
            raise ValueError(f"批次配置类型 {type(value).__name__} 无法安全核对。")
        return {"$scalar": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": _batch_signature_value(scalar)}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if pd.notna(value) and value not in (float("inf"), float("-inf")) else {
            "$float": "nan" if pd.isna(value) else "inf" if value > 0 else "-inf"
        }
    if value is pd.NA or value is pd.NaT:
        return {"$missing": type(value).__name__}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, Enum):
        return {"$enum": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": _batch_signature_value(value.value)}
    if isinstance(value, (datetime, date, time)):
        return {"$datetime": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$dataclass": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [[field.name, _batch_signature_value(getattr(value, field.name))]
                       for field in fields(value)],
        }
    if isinstance(value, Mapping):
        entries: list[list[Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"批次配置包含非文本映射键 {type(key).__name__}，无法安全核对准备摘要。"
                )
            entries.append([key, _batch_signature_value(item)])
        entries.sort(key=lambda pair: pair[0])
        return {"$mapping": entries}
    if isinstance(value, tuple):
        return {"$tuple": [_batch_signature_value(item) for item in value]}
    if isinstance(value, list):
        return {"$list": [_batch_signature_value(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        members = [_batch_signature_value(item) for item in value]
        members.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return {"$set": members, "$frozen": isinstance(value, frozenset)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {"$bytes_sha256": hashlib.sha256(payload).hexdigest(), "length": len(payload)}
    raise ValueError(f"批次配置类型 {type(value).__module__}.{type(value).__qualname__} 无法安全核对。")


_BATCH_REVISION_METADATA_FIELDS = (
    "time_unit",
    "normalized_time_unit",
    "source_time_unit",
    "time_confirmed",
    "coordinate_unit",
    "coordinate_frame",
    "pupil_units_by_eye",
    "pupil_unit",
    "device_validation_status",
    "device_validation_method",
    "device_validation_evidence",
)


def _batch_parameters_for_revision(
    frozen_parameters: Mapping[str, Any], revision: DatasetRevision,
) -> dict[str, Any]:
    """Bind one item's confirmed source declarations to the frozen analysis config."""
    parameters = deepcopy(dict(frozen_parameters))
    metadata_value = parameters.get("metadata")
    metadata = deepcopy(dict(metadata_value)) if isinstance(metadata_value, Mapping) else {}
    # A placeholder from the currently loaded dataset must never stand in for a
    # queued item's source declaration. Remove the whole whitelist first so
    # missing fields remain missing and nested evidence cannot leak across files.
    for field in _BATCH_REVISION_METADATA_FIELDS:
        metadata.pop(field, None)
    source_metadata = revision.dataset_copy().metadata.get("analysis_metadata", {})
    if isinstance(source_metadata, Mapping):
        for field in _BATCH_REVISION_METADATA_FIELDS:
            if field in source_metadata:
                metadata[field] = deepcopy(source_metadata[field])
    parameters["metadata"] = metadata
    return parameters


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
        self._timing_estimator = TimingEstimator()
        self._active_timing: TaskTiming | None = None
        self._timing_identity: object = None
        self._timing_generation = 0
        self._active_import_timing_identity: tuple[str, int | None] | None = None
        self._timing_timer = QTimer(self)
        self._timing_timer.setInterval(1000)
        self._timing_timer.timeout.connect(self._refresh_timing_display)
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
        self._batch_queue: BatchQueue | None = None
        self._batch_dialog = None
        self._aoi_editor_dialog = None
        self._batch_generation = 0
        self._batch_active = False
        self._batch_prepared = False
        self._batch_prepared_signature: str | None = None
        self._batch_prepared_configuration: dict[str, Any] | None = None
        self._batch_display_notice: str | None = None
        self._batch_waiting_for_review = False
        self._batch_mapping_review = False
        self._batch_review_item_id: str | None = None
        self._batch_input_inspection_generation = 0
        self._batch_inspector = None
        self._batch_inspection_template = None
        self._batch_base_request = None
        self._batch_item_requests: dict[str, Any] = {}
        self._batch_selection: SelectionSpec | None = None
        self._batch_parameters: dict[str, Any] = {}
        self._batch_context: dict[str, Any] = {}
        self._batch_context_raw_sources: dict[str, bytes] = {}
        self._batch_import_metadata: dict[str, Any] = {}
        self._batch_individual_context_parameters: dict[str, Any] = {}
        self._batch_schema_version: str | None = None
        self._batch_configuration_summary: dict[str, Any] = {}
        self._batch_output_store = None
        self._batch_output_root: str | None = None
        self._batch_import_generation: int | None = None
        self._batch_import_result = None
        self._batch_import_error: str | None = None
        self._batch_current_item_id: str | None = None
        self._batch_current_attempt = 0
        self._batch_current_phase: str | None = None
        self._batch_item_revision: DatasetRevision | None = None
        self._batch_expected_task: tuple[str, int, str] | None = None
        self._batch_task_result: dict[str, Any] | None = None
        self._batch_record_by_run: dict[str, RunRecord] = {}
        self._batch_export_generation: int | None = None
        self._batch_manifest_error: str | None = None
        self._batch_cancel_flag = False
        self._batch_retry_ids: list[str] = []
        self._batch_pending_idle_action: str | None = None
        self._batch_export_error: str | None = None
        self._batch_export_cancelled = False

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
        self.file_menu = menu
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
        return (self.task_manager.active or self.operation_manager.active or self.import_page.import_active
                or self._batch_active)

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
        self.help_menu = help_menu
        self.help_action = QAction("新手入门与文件示例", self)
        self.help_action.setShortcut("F1")
        self.help_action.triggered.connect(self._open_help)
        help_menu.addAction(self.help_action)
        export_action = QAction("导出练习资料与模板…", self)
        export_action.triggered.connect(self._export_learning_pack)
        help_menu.addAction(export_action)
        self.pdf_guide_action = QAction("全流程图解手册（PDF）", self)
        self.pdf_guide_action.setObjectName("pdfGuideAction")
        self.pdf_guide_action.setToolTip("打开离线 PDF：截图标注、逐步操作、文件填写实例和常见问题")
        self.pdf_guide_action.triggered.connect(self._open_pdf_guide)
        help_menu.addAction(self.pdf_guide_action)
        help_menu.addSeparator()
        self.environment_action = QAction("环境检查与依赖安装…", self)
        self.environment_action.setObjectName("environmentCheckAction")
        self.environment_action.triggered.connect(self._open_environment)
        help_menu.addAction(self.environment_action)
        about_action = QAction("关于 PYeyedata", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        licenses_action = QAction("许可与第三方声明", self)
        licenses_action.triggered.connect(self._show_licenses)
        help_menu.addAction(licenses_action)

    def _open_environment(self) -> None:
        from .environment_dialog import EnvironmentDialog

        dialog = EnvironmentDialog(self, allow_install=not self._configuration_busy())
        dialog.exec()
        dialog.deleteLater()

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
        toolbar.addAction(self.pdf_guide_action)
        toolbar.addAction(self.help_action)
        toolbar.addAction(self.environment_action)
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
        self.quality_page_scroll = QScrollArea(self.pages)
        self.quality_page_scroll.setObjectName("qualityPageScrollArea")
        self.quality_page_scroll.setWidgetResizable(True)
        self.quality_page_scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.quality_page_scroll.setWidget(self.quality_page)
        schema = self.bridge.schema()
        self.metrics_page = MetricsPage(schema=schema, parent=self.pages)
        self.group_page = GroupDesignPage(self.pages)
        self.group_page_scroll = QScrollArea(self.pages)
        self.group_page_scroll.setObjectName("groupPageScrollArea")
        self.group_page_scroll.setWidgetResizable(True)
        self.group_page_scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.group_page_scroll.setWidget(self.group_page)
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
        self.table_view.setObjectName("datasetPreviewTable")
        self.table_view.setMinimumHeight(110)
        self.table_view.setMaximumHeight(240)
        self.preview_box = QGroupBox("数据预览", self.data_page)
        self.preview_box.setObjectName("datasetPreviewBox")
        preview_layout = QVBoxLayout(self.preview_box)
        self.preview_hint = QLabel(
            "导入后显示本次数据修订的只读预览；此处不运行分析，也不代表设备已验证。",
            self.preview_box,
        )
        self.preview_hint.setWordWrap(True)
        preview_layout.addWidget(self.preview_hint)
        preview_layout.addWidget(self.table_view, 1)
        self.preview_box.setMaximumHeight(285)
        self.preview_box.hide()
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
        data_layout.addWidget(self.preview_box, 1)

        self.logs_page = QWidget(self.pages)
        self.logs_page.setObjectName("runLogPage")
        log_layout = QVBoxLayout(self.logs_page)
        self.log_title = QLabel("运行日志", self.logs_page)
        self.log_title.setObjectName("pageTitle")
        self.log_description = QLabel(WORKFLOW_STEPS[7][1], self.logs_page)
        self.log_description.setWordWrap(True)
        self.log_view = QPlainTextEdit(self.logs_page)
        self.log_view.setObjectName("runLogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setMinimumHeight(110)
        self.log_view.setPlaceholderText("运行阶段、后端消息和错误详情会显示在这里。")
        log_layout.addWidget(self.log_title)
        log_layout.addWidget(self.log_description)
        log_layout.addWidget(self.log_view, 1)
        for widget in (self.data_page, self.quality_page_scroll, self.metrics_page,
                       self.group_page_scroll, self.design_page, self.results_page_scroll,
                       self.logs_page):
            self.pages.addWidget(widget)

        help_panel = QGroupBox("参数说明", workspace)
        help_panel.setMinimumWidth(220)
        help_layout = QVBoxLayout(help_panel)
        self.help_text = QPlainTextEdit(help_panel)
        self.help_text.setObjectName("contextualHelpText")
        self.help_text.setPlainText("指标参数和适用条件按当前方法显示。\n\n前置检查会执行本次数据所需的共享预处理，再核对方法条件；它不会计算所选指标或统计结果，也不会认证设备。")
        self.help_text.setReadOnly(True)
        self.help_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.help_text.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.help_text.setMinimumWidth(0)
        self.help_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        help_layout.addWidget(self.help_text, 1)

        workspace.addWidget(self.step_list)
        workspace.addWidget(self.pages)
        workspace.addWidget(help_panel)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setStretchFactor(2, 0)
        workspace.setSizes([175, 850, 250])
        outer.addWidget(workspace, 1)
        self.contextual_help = ContextualHelpController(self.help_text, self.pages)

        task_row = QHBoxLayout()
        self.status_label = QLabel("未配置")
        self.progress_bar = QProgressBar(root)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        task_row.addWidget(self.status_label)
        task_row.addWidget(self.progress_bar, 1)
        outer.addLayout(task_row)
        self.timing_label = QLabel("当前阶段：空闲 · 已用 0秒 · 预计剩余 —", root)
        self.timing_label.setObjectName("taskTimingLabel")
        self.timing_label.setWordWrap(True)
        outer.addWidget(self.timing_label)

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

    @staticmethod
    def _timing_workload_key(selection: SelectionSpec | None) -> str | None:
        if selection is None:
            return None
        return (
            f"i{int(selection.run_individual)}:{len(selection.individual_metric_ids)}-"
            f"g{int(selection.run_group)}:{len(selection.group_metric_ids)}-"
            f"a{int(selection.run_advanced)}:{len(selection.advanced_metric_ids)}"
        )

    def _start_timing(
        self,
        kind: str,
        phase: str,
        *,
        identity: object = None,
        workload_key: str | None = None,
    ) -> object:
        if self._active_timing is not None:
            previous = self._active_timing.snapshot()
            if previous.status in {"running", "cancelling"}:
                self._active_timing.finish("superseded", train=False)
        self._timing_generation += 1
        actual_identity = identity if identity is not None else ("timing", self._timing_generation)
        self._timing_identity = actual_identity
        self._active_timing = TaskTiming(
            self._timing_estimator, kind, phase,
            identity=actual_identity, workload_key=workload_key,
        )
        self._timing_timer.start()
        self._refresh_timing_display()
        return actual_identity

    def _update_timing_phase(self, message: str, *, identity: object = None) -> None:
        if self._active_timing is None:
            return
        if identity is not None and identity != self._timing_identity:
            return
        self._active_timing.update_phase(str(message))
        self._refresh_timing_display()

    def _request_timing_cancel(self) -> None:
        if self._active_timing is None:
            return
        self._active_timing.request_cancel()
        self._refresh_timing_display()

    def _finish_timing(
        self,
        status: str,
        *,
        train: bool = False,
        identity: object = None,
    ) -> bool:
        if self._active_timing is None:
            return False
        if identity is not None and identity != self._timing_identity:
            return False
        self._active_timing.finish(status, train=train)
        self._timing_timer.stop()
        self._refresh_timing_display()
        return True

    def _refresh_timing_display(self) -> None:
        current_text = "当前阶段：空闲 · 已用 0秒 · 预计剩余 —"
        if self._batch_waiting_for_review and self._batch_review_item_id and self._batch_queue is not None:
            try:
                item = self._batch_queue.get(self._batch_review_item_id)
                current_text = (
                    f"当前阶段：等待映射确认（{Path(item.source_path).name}） · "
                    "预计剩余时间暂停"
                )
            except BatchQueueError:
                current_text = "当前阶段：等待映射确认 · 预计剩余时间暂停"
        elif self._active_timing is not None:
            current_text = format_timing(self._active_timing.snapshot())
        self.timing_label.setText(current_text)
        if self._batch_dialog is not None:
            self._batch_dialog.set_timing_status(
                current_text,
                self._batch_queue_timing_text(),
            )

    def _batch_queue_timing_text(self) -> str:
        queue = self._batch_queue
        if queue is None or (not self._batch_active and self._batch_inspector is None):
            return "队列剩余：—"
        if self._batch_inspector is not None:
            count = len(self._batch_inspector.paths)
            snapshot = self._active_timing.snapshot() if self._active_timing is not None else None
            if (snapshot is not None and snapshot.status == "running"
                    and self._timing_identity == ("batch-inspect", self._batch_inspector.generation)
                    and snapshot.remaining_seconds is not None):
                return (
                    f"队列剩余：正在检查 {count} 个输入 · 本阶段预计剩余 "
                    f"{format_duration(snapshot.remaining_seconds)}；检查后按结构确认情况继续"
                )
            if snapshot is not None and snapshot.status == "cancelling":
                return f"队列剩余：正在取消输入检查 · {count} 个输入等待后台停止"
            return f"队列剩余：正在检查 {count} 个输入；检查后按结构确认情况继续"
        if self._batch_waiting_for_review:
            remaining = sum(
                item.status == "pending"
                or item.status in {"importing", "checking", "running"}
                or item.export_status in {"pending", "exporting"}
                for item in queue.items
            )
            return f"队列剩余：等待映射确认；倒计时暂停 · {remaining} 项仍未结束"

        selection_key = self._timing_workload_key(self._batch_selection)
        total_seconds = 0.0
        remaining_items = 0
        active_snapshot = (
            self._active_timing.snapshot()
            if self._active_timing is not None
            else None
        )
        if self._batch_cancel_flag or (
                active_snapshot is not None and active_snapshot.status == "cancelling"):
            unfinished = sum(
                item.status not in {"complete", "partial", "failed", "cancelled"}
                or item.export_status in {"pending", "exporting"}
                for item in queue.items
            )
            return (
                f"队列剩余：取消中，等待后台停止 · {unfinished} 项未结束；"
                "预计剩余时间暂停"
            )
        active_identity = self._timing_identity
        current_item_id = self._batch_current_item_id

        def estimate(kind: str) -> float:
            workload_key = selection_key if kind in {"preflight", "run"} else None
            return self._timing_estimator.estimate(
                kind,
                workload_key,
            )

        for item in queue.items:
            is_current = item.item_id == current_item_id
            status = item.status
            export_status = item.export_status
            if status == "pending":
                remaining_items += 1
                if item.structure_confirmed and item.source_sha256:
                    total_seconds += estimate("import") + estimate("preflight") + estimate("run")
                    total_seconds += estimate("batch_export")
                continue
            if status in {"importing", "checking", "running"}:
                remaining_items += 1
                phase = self._batch_current_phase
                prefix = active_identity[0] if isinstance(active_identity, tuple) and active_identity else None
                timing_is_current = (
                    is_current and active_snapshot is not None
                    and active_snapshot.status in {"running", "cancelling"}
                    and prefix in {"batch-inspect", "import", "pipeline", "operation"}
                )
                if timing_is_current and active_snapshot.remaining_seconds is not None:
                    total_seconds += active_snapshot.remaining_seconds
                elif is_current:
                    current_phase = phase or "importing"
                    total_seconds += estimate(current_phase if current_phase in {
                        "import", "preflight", "run", "batch_export"
                    } else "import")
                if phase == "importing":
                    total_seconds += estimate("preflight") + estimate("run") + estimate("batch_export")
                elif phase == "preflight":
                    total_seconds += estimate("run") + estimate("batch_export")
                elif phase in {"run", "running"}:
                    total_seconds += estimate("batch_export")
                else:
                    total_seconds += estimate("preflight") + estimate("run") + estimate("batch_export")
                continue
            if status in {"complete", "partial"} and export_status in {"pending", "exporting"}:
                remaining_items += 1
                if (is_current and active_snapshot is not None
                        and active_snapshot.status in {"running", "cancelling"}
                        and self._active_timing is not None
                        and self._active_timing.kind == "batch_export"
                        and active_snapshot.remaining_seconds is not None):
                    total_seconds += active_snapshot.remaining_seconds
                else:
                    total_seconds += estimate("batch_export")

        return (
            f"队列剩余：{remaining_items} 项 · 预计约 {format_duration(total_seconds)}"
            "（按各阶段当前估计；不含用户等待）"
        )

    def _connect_pages(self) -> None:
        self.import_page.import_completed.connect(self._on_import_completed)
        self.import_page.import_failed.connect(self._on_import_failed)
        self.import_page.import_progress.connect(self._on_import_progress)
        self.import_page.import_state_changed.connect(self._on_import_state_changed)
        self.import_page.import_idle.connect(self._on_import_idle)
        self.import_page.import_worker_completed.connect(self._on_batch_import_completed)
        self.import_page.import_worker_failed.connect(self._on_batch_import_failed)
        self.import_page.import_worker_cancelled.connect(self._on_batch_import_cancelled)
        self.import_page.import_worker_finished.connect(self._on_batch_import_finished)
        self.import_page.batch_queue_requested.connect(self._open_batch_queue)
        self.import_page.open_aoi_editor_requested.connect(self._open_static_aoi_editor)
        self.import_page.dataset_invalidated.connect(self._on_dataset_invalidated)
        self.import_page.configuration_changed.connect(self._on_import_configuration_changed)
        self.quality_page.configuration_changed.connect(self._on_processing_configuration_changed)
        self.metrics_page.selection_changed.connect(self._on_selection_changed)
        self.metrics_page.parameters_changed.connect(self._on_metrics_parameters_changed)
        self.metrics_page.preflight_requested.connect(self.start_preflight)
        self.metrics_page.edit_requirements_requested.connect(self._on_edit_requirements)
        self.group_page.configuration_changed.connect(self._on_group_configuration_changed)
        self.group_page.scope_changed.connect(self._on_group_scope_changed)
        self.quality_page.parameter_error_button.clicked.connect(
            lambda: self._activate_parameter_error_page("quality")
        )
        self.group_page.parameter_error_button.clicked.connect(
            lambda: self._activate_parameter_error_page("group")
        )
        self.design_page.configuration_changed.connect(self._on_design_configuration_changed)
        self.results_page.history_selected.connect(self._on_history_selected)
        remediation_signal = getattr(self.results_page, "remediationNavigationRequested", None)
        if remediation_signal is not None:
            remediation_signal.connect(self._route_remediation_navigation)
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

    def _open_static_aoi_editor(self) -> None:
        from .aoi_editor import AOIEditorDialog

        if (self._configuration_busy() or self.import_page._batch_review_mode
                or self.import_page.import_active):
            QMessageBox.information(self, "当前任务繁忙", "请等待导入、分析、归档或批量任务结束后打开 AOI 编辑器。")
            return
        existing = self._aoi_editor_dialog
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        revision = self.session.dataset_revision
        revision_id = revision.revision_id if revision is not None else None
        table = pd.DataFrame()
        image_metadata: dict[str, Any] | None = None
        if revision is not None:
            dataset = revision.dataset_copy()
            if isinstance(dataset.aois, pd.DataFrame):
                table = dataset.aois.copy(deep=True)
            provenance = dataset.metadata.get("aoi_editor_provenance")
            if isinstance(provenance, Mapping):
                manifest = provenance.get("manifest")
                if isinstance(manifest, Mapping):
                    image_metadata = dict(manifest)

        dialog = AOIEditorDialog(self)
        dialog.set_aois(table, image_metadata=image_metadata)
        dialog.apply_button.setText("导出并加入 AOI 导入源…")
        dialog.apply_button.setToolTip(
            "导出规范 AOI CSV 和图片 manifest，并加入导入页 AOI 来源。需再显式导入并通过现有检查；不会直接改当前数据修订。"
        )
        dialog.applyRequested.connect(
            lambda edited_table, metadata, target=dialog, base_revision=revision_id:
                self._apply_aoi_editor_to_import_source(target, edited_table, metadata, base_revision)
        )
        dialog.finished.connect(
            lambda _result, target=dialog:
                setattr(self, "_aoi_editor_dialog", None) if self._aoi_editor_dialog is target else None
        )
        self._aoi_editor_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_aoi_editor_to_import_source(
        self, dialog: QWidget, _edited_table: Any, image_metadata: Mapping[str, Any],
        base_revision_id: str | None,
    ) -> None:
        if (self._configuration_busy() or self.import_page._batch_review_mode
                or self.import_page.import_active):
            QMessageBox.information(dialog, "当前任务繁忙", "AOI 导入源未更改；请等待任务结束后再试。")
            return
        current = self.session.dataset_revision
        current_id = current.revision_id if current is not None else None
        if current_id != base_revision_id:
            QMessageBox.warning(
                dialog, "数据修订已变化",
                "打开编辑器后数据修订已变化。请关闭并重新打开编辑器，再为当前修订编辑；当前导出和导入源未更改。",
            )
            return
        path, _filter = QFileDialog.getSaveFileName(
            dialog, "导出并加入 AOI 导入源", "aois.csv", "CSV (*.csv)",
        )
        if not path:
            return
        if Path(path).suffix.lower() != ".csv":
            path += ".csv"
        csv_path = Path(path).expanduser().resolve()
        manifest_path = csv_path.with_suffix(csv_path.suffix + ".manifest.json")
        if csv_path.exists() or manifest_path.exists():
            QMessageBox.warning(
                dialog, "目标文件已存在",
                "为保留已有数据，请选择一个尚不存在的 CSV 和 manifest 路径。",
            )
            return
        try:
            csv_path, manifest_path = dialog.export_to(csv_path)
            csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            provenance = {
                "source_type": "gui_static_image_editor",
                "csv_path": str(csv_path),
                "csv_sha256": csv_hash,
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_hash,
                "stimulus_id": image_metadata.get("stimulus_id"),
                "image_metadata": deepcopy(dict(image_metadata)),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }
            self.import_page.stage_aoi_editor_export(csv_path, provenance)
        except Exception as exc:
            QMessageBox.warning(dialog, "无法加入 AOI 导入源", f"导出或预览失败：{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(
            dialog, "AOI 来源已准备",
            "AOI CSV 和图片 manifest 已导出，并载入导入页 AOI 来源预览。当前数据修订保持不变；"
            "核对坐标单位/frame 和映射后，点击“导入并规范化”生成新修订。",
        )

    def _open_batch_queue(self) -> None:
        from .batch_dialog import BatchQueueDialog

        if self._batch_dialog is None:
            self._batch_dialog = BatchQueueDialog(self)
            self._batch_dialog.addFilesRequested.connect(self._batch_add_files)
            self._batch_dialog.removeItemsRequested.connect(self._batch_remove_items)
            self._batch_dialog.startRequested.connect(self._batch_start_requested)
            self._batch_dialog.cancelRequested.connect(self._batch_cancel_requested)
            self._batch_dialog.retryItemsRequested.connect(self._batch_retry_items)
            self._batch_dialog.retryExportRequested.connect(self._batch_retry_exports)
            self._batch_dialog.openResultRequested.connect(self._batch_open_result)
            self._batch_dialog.configureRequested.connect(self._batch_configure_item)
            self._batch_dialog.groupWorkflowRequested.connect(self._batch_group_workflow)
        if self._batch_queue is None:
            self._batch_queue = BatchQueue()
        self._refresh_batch_queue_view()
        self._batch_dialog.show()
        self._batch_dialog.raise_()
        self._batch_dialog.activateWindow()

    def _reset_batch_queue_for_new_inputs(self) -> None:
        if (self._batch_active or self._batch_inspector is not None or self.import_page.import_active
                or self.task_manager.active or self.operation_manager.active):
            raise RuntimeError("当前批量队列仍在运行，不能更换队列。")
        self._batch_queue = BatchQueue()
        self._batch_prepared = False
        self._batch_prepared_signature = None
        self._batch_prepared_configuration = None
        self._batch_display_notice = None
        self._batch_output_store = None
        self._batch_output_root = None
        self._batch_item_requests.clear()
        self._batch_record_by_run.clear()
        self._batch_base_request = None
        self._batch_selection = None
        self._batch_parameters = {}
        self._batch_context = {}
        self._batch_context_raw_sources = {}
        self._batch_schema_version = None
        self._batch_configuration_summary = {}
        self._batch_review_item_id = None
        self._batch_waiting_for_review = False
        self._batch_manifest_error = None
        if self._batch_dialog is not None:
            self._batch_dialog.set_prepared(False)
            self._batch_dialog.set_output_directory(None)
            self._batch_dialog.set_configuration_summary("当前批次配置尚未显示。")

    def _batch_add_files(self, paths: list[str]) -> None:
        if (self._batch_active or self._batch_inspector is not None or self.task_manager.active
                or self.operation_manager.active or self.import_page.import_active):
            self.status_label.setText("当前任务或输入结构检查仍在进行，暂不能添加批量输入。")
            return
        if self._batch_queue is not None and self._batch_queue.configuration_fingerprint is not None:
            if all(item.status in {"complete", "partial", "failed", "cancelled"}
                   and item.export_status in {"not_requested", "complete", "failed", "cancelled"}
                   for item in self._batch_queue.items):
                try:
                    self._reset_batch_queue_for_new_inputs()
                except RuntimeError as exc:
                    self._batch_show_error("无法开始新队列", str(exc))
                    return
            else:
                self._batch_show_error("批次已冻结", "当前队列配置已冻结。完成本批后可新建队列；原清单会保留在其输出目录。")
                return
        try:
            profile, request = self.import_page.batch_profile_and_request()
        except Exception as exc:
            self._batch_show_error("需要已确认的导入预设", str(exc))
            return
        signal_source = next((source for source in request.sources if source.role == "signals"), None)
        profile_signal = next((source for source in profile.sources if source.role == "signals"), None)
        if signal_source is None or profile_signal is None:
            self._batch_show_error("信号映射不可用", "所选预设必须包含已确认的主要信号映射。")
            return
        if self._batch_queue is None:
            self._batch_queue = BatchQueue()
        existing = {os.path.normcase(str(Path(item.source_path).resolve())) for item in self._batch_queue.items}
        normalized_paths: list[str] = []
        for value in paths:
            try:
                path = str(Path(value).expanduser().resolve(strict=True))
            except (OSError, RuntimeError):
                path = str(Path(value).expanduser().absolute())
            canonical = os.path.normcase(path)
            if canonical in existing:
                continue
            existing.add(canonical)
            normalized_paths.append(path)
        if not normalized_paths:
            self._refresh_batch_queue_view()
            return

        self._invalidate_batch_preparation("队列文件列表已变化")

        for source_path in normalized_paths:
            try:
                self._batch_queue.create_item(
                    source_path=source_path,
                    source_sha256=None,
                    source_hash_error="正在读取并检查输入文件。",
                    profile_id=profile.profile_id,
                    profile_revision=profile.revision,
                    structure_confirmed=False,
                    error="正在检查输入文件结构。",
                )
            except BatchQueueError as exc:
                self._batch_show_error("无法添加输入", str(exc))
                return

        self._batch_inspection_template = {
            "profile": profile,
            "request": request,
            "expected_columns": tuple(profile.structure_signature.get("signals", ())),
            "mapped_columns": tuple(profile_signal.column_map.values()),
        }
        self._batch_input_inspection_generation += 1
        generation = self._batch_input_inspection_generation
        inspector = _BatchInputInspector(
            generation, normalized_paths, signal_source,
            self._batch_inspection_template["expected_columns"],
            self._batch_inspection_template["mapped_columns"],
        )
        inspector.signals.completed.connect(self._on_batch_inputs_inspected)
        inspector.signals.progress.connect(self._on_batch_input_inspection_progress)
        self._batch_inspector = inspector
        self._start_timing(
            "inspect",
            f"正在检查批量输入（{len(normalized_paths)} 个文件）",
            identity=("batch-inspect", generation),
            workload_key=f"{len(normalized_paths)}-files",
        )
        self.progress_bar.setRange(0, len(normalized_paths))
        self.progress_bar.setValue(0)
        self._batch_set_busy(True)
        self._refresh_batch_queue_view()
        QThreadPool.globalInstance().start(inspector)

    def _on_batch_input_inspection_progress(
        self, generation: int, completed: int, total: int, message: str,
    ) -> None:
        identity = ("batch-inspect", generation)
        if generation != self._batch_input_inspection_generation or identity != self._timing_identity:
            return
        self._update_timing_phase(message, identity=identity)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(max(0, min(total, completed)))
        self.status_label.setText(message)

    def _on_batch_inputs_inspected(self, generation: int, results: object) -> None:
        if generation != self._batch_input_inspection_generation or self._batch_queue is None:
            return
        identity = ("batch-inspect", generation)
        result_rows = results if isinstance(results, list) else []
        by_path = {
            os.path.normcase(str(Path(item.source_path).resolve())): item
            for item in self._batch_queue.items
        }
        for result in result_rows:
            if not isinstance(result, Mapping):
                continue
            source_path = str(result.get("source_path", ""))
            item = by_path.get(os.path.normcase(source_path))
            if item is None:
                continue
            try:
                self._batch_queue.set_input_inspection(
                    item.item_id,
                    source_sha256=result.get("source_sha256"),
                    source_hash_error=result.get("source_hash_error"),
                    structure_confirmed=bool(result.get("structure_confirmed")),
                    error=result.get("message") or None,
                    preflight_error=result.get("preflight_error"),
                )
                if result.get("message"):
                    self._batch_dialog.set_preflight_messages(item.item_id, [result["message"]])
            except BatchQueueError as exc:
                self._batch_show_error("输入结构检查结果无效", str(exc))
        self._batch_inspector = None
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        inspection_complete = bool(result_rows) and all(
            isinstance(result, Mapping)
            and result.get("source_sha256") is not None
            and result.get("source_hash_error") is None
            and result.get("preflight_error") is None
            for result in result_rows
        )
        self._finish_timing(
            "complete" if inspection_complete else "failed",
            train=inspection_complete,
            identity=identity,
        )
        self._batch_set_busy(False)
        self._refresh_batch_queue_view()

    def _batch_remove_items(self, item_ids: list[str]) -> None:
        if self._batch_queue is None or self._batch_inspector is not None:
            return
        try:
            self._batch_queue.remove_items(item_ids)
        except BatchQueueError as exc:
            self._batch_show_error("无法移除输入", str(exc))
            return
        if item_ids:
            self._invalidate_batch_preparation("队列文件列表已变化")
        self._refresh_batch_queue_view()

    def _batch_start_requested(self, output_root: str) -> None:
        from .batch_export import BatchOutputError

        if self._batch_queue is None or not self._batch_queue.items:
            self._batch_show_error("队列为空", "请先添加至少一个输入文件。")
            return
        if self._batch_inspector is not None:
            self._batch_show_error("仍在检查输入", "请等待所有已添加文件完成哈希和结构检查后再启动队列。")
            return
        if self._batch_active:
            if self._batch_output_root and Path(output_root).resolve() != Path(self._batch_output_root).resolve():
                self._batch_show_error("批次已启动", "已启动的批次不能更换输出目录。")
                return
            self._batch_resume_reviewed_item()
            return
        if self._batch_queue.configuration_fingerprint is not None:
            self._batch_show_error("批次已冻结", "此队列已运行过。请重试所选项目，或添加新文件以创建新队列。")
            return
        if self.import_page.import_active or self.task_manager.active or self.operation_manager.active:
            self._batch_show_error("当前操作繁忙", "请等待导入、分析或导出结束后启动批量队列。")
            return
        try:
            candidate = self._capture_batch_preparation(output_root)
            if not self._batch_prepared:
                self._stage_batch_preparation(candidate)
                return
            if (self._batch_prepared_signature != candidate["signature"]
                    or self._batch_prepared_configuration is None):
                self._stage_batch_preparation(
                    candidate,
                    notice="准备摘要与当前配置或文件列表不同，已更新摘要。核对后再次点击“按此配置开始”。",
                )
                return

            prepared = self._batch_prepared_configuration
            self._batch_base_request = prepared["request"]
            self._batch_selection = prepared["selection"]
            self._batch_parameters = prepared["parameters"]
            self._batch_context = prepared["context"]
            self._batch_context_raw_sources = prepared["context_raw_sources"]
            self._batch_import_metadata = prepared["import_metadata"]
            self._batch_individual_context_parameters = prepared["individual_context_parameters"]
            self._batch_schema_version = prepared["schema_version"]
            self._batch_output_store = prepared["output_store"]
            self._batch_output_root = prepared["output_root"]
            self._batch_configuration_summary = prepared["summary"]

            self._batch_queue.freeze_configuration(_batch_json_safe(self._batch_configuration_summary))
            self._batch_prepared = False
            self._batch_prepared_signature = None
            self._batch_prepared_configuration = None
            self._batch_display_notice = None
            if self._batch_dialog is not None:
                self._batch_dialog.set_prepared(False)
            self._batch_generation += 1
            self._batch_active = True
            self._batch_waiting_for_review = False
            self._batch_cancel_flag = False
            self._batch_retry_ids = []
            self._batch_manifest_error = None
            self._write_batch_manifest()
            if self._batch_manifest_error:
                self._batch_active = False
                self._refresh_busy_state()
                return
            self._refresh_busy_state()
            self._batch_dispatch_next()
        except (RuntimeError, ValueError, OSError, BatchOutputError) as exc:
            if self._batch_prepared:
                self._invalidate_batch_preparation("重新核对失败")
            self._batch_show_error("无法准备或启动批量队列", str(exc))

    def _capture_batch_preparation(self, output_root: str) -> dict[str, Any]:
        from .batch_export import BatchOutputStore

        if self._batch_queue is None:
            raise RuntimeError("批量队列尚未准备好。")
        profile, request = self.import_page.batch_profile_and_request()
        if any(item.profile_id != profile.profile_id or item.profile_revision != profile.revision
               for item in self._batch_queue.items):
            raise RuntimeError("队列中的输入使用不同导入预设版本；请清空并重新添加，使同一预设覆盖整批输入。")
        if not any(item.status == "pending" for item in self._batch_queue.items):
            raise RuntimeError("队列中没有待处理的输入。")
        selection = self.metrics_page.selection_spec()
        if selection.run_group or request.scope.mode == "shared_interaction":
            raise RuntimeError("该批次按文件独立分析。多人/合并流程请切换到独立的多人工作流。")
        output_store = BatchOutputStore(output_root)
        output_store.validate_root()
        schema = self.bridge.schema()
        parameters = self._compose_parameters(None, selection=selection)
        parameters["selection"] = selection.to_dict()
        context = {
            name: frame.copy(deep=True)
            for name, frame in self._current_context_tables().items()
            if isinstance(frame, pd.DataFrame)
        }
        context_raw_sources = dict(self._current_context_raw_sources())
        import_metadata = deepcopy(self._current_import_metadata())
        individual_context_parameters = deepcopy(self._current_individual_context_parameters())
        schema_version = str(schema["pipeline_schema_version"])
        output_root_value = str(output_store.output_root)
        source_roles = {
            source.role: {"path": source.path, "sheet": source.sheet_name}
            for source in request.sources if source.role != "signals"
        }
        summary = {
            "workflow": "逐文件独立分析",
            "group_workflow": "合并/多人统计为单独工作流，不在此队列中推断。",
            "profile": {"id": profile.profile_id, "name": profile.name, "revision": profile.revision},
            "selection": selection.to_dict(),
            "parameters": _batch_json_safe(parameters),
            "auxiliary_sources_reused": source_roles,
            "subject_id_default": request.signal_subject_id,
            "session_id_default": request.signal_session_id,
            "output_root": output_root_value,
        }
        frame_hashes: dict[str, str] = {}
        for name, frame in context.items():
            frame_payload = {
                "index": list(frame.index),
                "columns": list(frame.columns),
                "data": frame.to_numpy().tolist(),
                "dtypes": [str(dtype) for dtype in frame.dtypes],
            }
            encoded_frame = json.dumps(
                _batch_signature_value(frame_payload), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            frame_hashes[name] = hashlib.sha256(encoded_frame).hexdigest()
        signature_payload = {
            "profile": profile,
            "request": request,
            "selection": selection,
            "parameters": parameters,
            "context_hashes": frame_hashes,
            "context_raw_hashes": {
                name: hashlib.sha256(data).hexdigest() for name, data in context_raw_sources.items()
            },
            "import_metadata": import_metadata,
            "individual_context_parameters": individual_context_parameters,
            "pipeline_schema_version": schema_version,
            "items": list(self._batch_queue.items),
            "output_root": output_store.output_root,
        }
        encoded = json.dumps(
            _batch_signature_value(signature_payload), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return {
            "signature": hashlib.sha256(encoded).hexdigest(),
            "request": deepcopy(request),
            "selection": selection,
            "parameters": deepcopy(parameters),
            "context": context,
            "context_raw_sources": context_raw_sources,
            "import_metadata": import_metadata,
            "individual_context_parameters": individual_context_parameters,
            "schema_version": schema_version,
            "output_store": output_store,
            "output_root": output_root_value,
            "summary": summary,
        }

    def _stage_batch_preparation(self, prepared: dict[str, Any], *, notice: str | None = None) -> None:
        self._batch_prepared = True
        self._batch_prepared_signature = str(prepared["signature"])
        self._batch_prepared_configuration = prepared
        self._batch_configuration_summary = deepcopy(prepared["summary"])
        self._batch_output_store = prepared["output_store"]
        self._batch_output_root = str(prepared["output_root"])
        self._batch_display_notice = notice
        if self._batch_dialog is not None:
            self._batch_dialog.set_output_directory(self._batch_output_root)
            self._batch_dialog.set_prepared(True)
            self._refresh_batch_queue_view()
        self.status_label.setText(notice or "批次配置已准备并显示；尚未启动分析。核对摘要后点击“按此配置开始”。")

    def _invalidate_batch_preparation(self, reason: str) -> None:
        if not self._batch_prepared or self._batch_active:
            return
        self._batch_prepared = False
        self._batch_prepared_signature = None
        self._batch_prepared_configuration = None
        self._batch_output_store = None
        self._batch_output_root = None
        self._batch_configuration_summary = {}
        self._batch_display_notice = (
            "已取消准备；尚未启动分析或写入批次结果。"
            if reason == "用户取消准备"
            else f"准备摘要已失效（{reason}）；请重新核对配置并准备批量队列。"
        )
        if self._batch_dialog is not None:
            self._batch_dialog.set_prepared(False)
            self._batch_dialog.set_output_directory(None)
            self._batch_dialog.set_configuration_summary(self._batch_display_notice)
        self.status_label.setText(self._batch_display_notice)

    def _batch_resume_reviewed_item(self) -> None:
        if self._batch_queue is None or self._batch_review_item_id is None:
            return
        try:
            item, request = self._capture_batch_review(self._batch_review_item_id)
            self._batch_queue.confirm_structure(item.item_id)
            self._batch_item_requests[item.item_id] = request
            self._batch_waiting_for_review = False
            self._batch_review_item_id = None
            self._write_batch_manifest()
            self._refresh_busy_state()
            self._refresh_batch_queue_view()
            self._batch_dispatch_next()
        except Exception as exc:
            self._batch_show_error("映射尚未确认", str(exc))

    def _capture_batch_review(self, item_id: str):
        if self._batch_queue is None:
            raise RuntimeError("批量队列尚未准备好。")
        item = self._batch_queue.get(item_id)
        profile, reviewed = self.import_page.batch_profile_and_request()
        if profile.profile_id != item.profile_id or profile.revision != item.profile_revision:
            raise RuntimeError("当前导入预设版本与此队列条目不一致；请保持启动时的预设版本。")
        if self._batch_review_item_id not in {None, item_id}:
            raise RuntimeError("请先在队列中选择此文件并打开“配置导入映射”。")
        panel = self.import_page.sources["signals"]
        if (os.path.normcase(str(Path(panel.source_path).resolve()))
                != os.path.normcase(str(Path(item.source_path).resolve()))
                or os.path.normcase(str(Path(panel._preview_path or "").resolve()))
                != os.path.normcase(str(Path(item.source_path).resolve()))):
            raise RuntimeError("请先预览当前队列条目的实际文件，并在预设候选中核对字段。")
        signal_source = next((source for source in reviewed.sources if source.role == "signals"), None)
        if signal_source is None:
            raise RuntimeError("已确认的映射没有主要信号源。")
        # Only per-file signal parsing/mapping may differ after explicit review;
        # auxiliary sources and all semantic defaults remain frozen for the batch.
        base_request = self._batch_base_request or reviewed
        frozen = self.import_page.batch_request_for_source(base_request, item.source_path)
        sources = tuple(
            signal_source if source.role == "signals" else source
            for source in frozen.sources
        )
        signal_source = replace(signal_source, path=str(Path(item.source_path).resolve()))
        sources = tuple(signal_source if source.role == "signals" else source for source in sources)
        return item, replace(frozen, sources=sources)

    def _batch_configure_item(self) -> None:
        if self._batch_dialog is None or self._batch_queue is None:
            return
        selected = self._batch_dialog.selected_item_ids()
        if len(selected) != 1:
            self._batch_show_error("选择一个输入", "请只选择一个待确认的文件，再配置其导入映射。")
            return
        item_id = selected[0]
        item = self._batch_queue.get(item_id)
        if item.status != "pending" or item.preflight_error:
            self._batch_show_error("当前条目无法配置", "只有待处理且可读取的输入可以进行结构核对。")
            return
        if self._batch_active and not self._batch_waiting_for_review:
            self._batch_show_error("队列正在运行", "请等待当前文件结束后再进行结构核对。")
            return
        self._batch_review_item_id = item_id
        self._batch_waiting_for_review = bool(self._batch_active)
        self.import_page.set_batch_review_mode(True)
        self.step_list.setCurrentRow(0)
        self.pages.setCurrentIndex(0)
        self.import_page.source_tabs.setCurrentIndex(0)
        panel = self.import_page.sources["signals"]
        panel.path_edit.setText(item.source_path)
        panel.clear_preview(keep_path=True)
        panel.request_preview()
        self._batch_set_busy(False)
        self._batch_dialog.set_preflight_messages(
            item_id,
            ["已打开该文件的信号预览。请选择启动时的导入预设，预览并应用；结构变化需在候选映射中明确确认，然后回到队列重试。"],
        )
        self.status_label.setText(f"正在核对批量输入：{Path(item.source_path).name}。")

    def _batch_group_workflow(self) -> None:
        if self._batch_active:
            self._batch_show_error("队列配置已冻结", "当前队列逐文件独立分析；请在队列结束后进入多人工作流。")
            return
        self.step_list.setCurrentRow(3)
        self.pages.setCurrentIndex(3)
        self.status_label.setText("已打开独立多人工作流。它不会合并或修改批量队列中的逐文件结果。")

    def _batch_open_result(self, run_id: str) -> None:
        record = next((item for item in self.session.history if item.run_id == run_id), None)
        if record is None:
            self._batch_show_error("运行记录不可用", f"找不到批量运行记录：{run_id}")
            return
        self.results_page.set_history(self.session.history)
        self.results_page.show_record(record)
        self.step_list.setCurrentRow(6)
        self.pages.setCurrentWidget(self.results_page_scroll)

    def _batch_retry_items(self, item_ids: list[str]) -> None:
        if self._batch_queue is None or not item_ids:
            return
        if self._batch_active and not self._batch_waiting_for_review:
            self._batch_show_error("队列正在运行", "请等待当前条目结束后再安排重试。")
            return
        was_waiting_for_review = self._batch_active and self._batch_waiting_for_review
        try:
            first_attempt_ids: list[str] = []
            for item_id in item_ids:
                item = self._batch_queue.get(item_id)
                if item.status == "pending":
                    if item.structure_confirmed:
                        first_attempt_ids.append(item_id)
                    else:
                        _reviewed, request = self._capture_batch_review(item_id)
                        self._batch_queue.confirm_structure(item_id)
                        self._batch_item_requests[item_id] = request
                        first_attempt_ids.append(item_id)
                    continue
                if item.status not in {"failed", "partial", "cancelled"}:
                    continue
                if not item.structure_confirmed:
                    self._batch_review_item_id = item_id
                    raise RuntimeError("此条目需要先配置并明确确认导入映射。")
            self._refresh_batch_queue_view()
            if was_waiting_for_review:
                self._batch_waiting_for_review = False
                self._batch_review_item_id = None
                self._batch_dispatch_next()
                return
            if self._batch_queue.configuration_fingerprint is None:
                self.import_page.set_batch_review_mode(False)
                self._batch_review_item_id = None
                self.status_label.setText("所选条目的导入结构已确认。选择“开始批量分析”并指定输出文件夹以冻结配置并启动。")
                return
            self._batch_retry_ids.extend(
                f"first:{item_id}" for item_id in first_attempt_ids
                if item_id not in {token.removeprefix("first:") for token in self._batch_retry_ids}
            )
            self._batch_retry_ids.extend(
                item_id for item_id in item_ids
                if self._batch_queue.get(item_id).status in {"failed", "partial", "cancelled"}
                and item_id not in self._batch_retry_ids
            )
            if self._batch_retry_ids:
                self._batch_queue.resume()
                self._batch_active = True
                self._batch_cancel_flag = False
                self._batch_dispatch_next()
        except Exception as exc:
            self._batch_show_error("无法安排批量重试", str(exc))

    def _batch_retry_exports(self, item_ids: list[str]) -> None:
        if self._batch_queue is None or not item_ids:
            return
        if self._batch_active or self.task_manager.active or self.operation_manager.active or self.import_page.import_active:
            self._batch_show_error("队列正在运行", "请等待当前条目结束后重试导出。")
            return
        selected = next((item_id for item_id in item_ids
                         if self._batch_queue.get(item_id).export_status in {"failed", "cancelled"}), None)
        if selected is None:
            return
        item = self._batch_queue.get(selected)
        if not item.run_id or item.run_id not in self._batch_record_by_run:
            self._batch_show_error("运行记录不可用", "当前窗口中没有可供重试导出的不可变 RunRecord。")
            return
        self._batch_retry_ids = [
            f"export:{item_id}" for item_id in item_ids
            if self._batch_queue.get(item_id).export_status in {"failed", "cancelled"}
            and self._batch_queue.get(item_id).run_id in self._batch_record_by_run
        ]
        if self._batch_retry_ids:
            self._batch_queue.resume()
        self._batch_active = True
        self._batch_cancel_flag = False
        self._batch_dispatch_next()

    def _batch_cancel_requested(self) -> None:
        if self._batch_prepared and not self._batch_active:
            self._invalidate_batch_preparation("用户取消准备")
            self._refresh_batch_queue_view()
            return
        if not self._batch_active or self._batch_queue is None:
            return
        self._request_timing_cancel()
        self._batch_cancel_flag = True
        self._batch_queue.cancel()
        if self.import_page.import_active:
            self.import_page.cancel_import()
        if self.task_manager.active:
            self.task_manager.cancel()
        if self.operation_manager.active:
            self.operation_manager.cancel()
        self._write_batch_manifest()
        self._refresh_batch_queue_view()
        if not (self.import_page.import_active or self.task_manager.active or self.operation_manager.active):
            self._batch_finish_or_dispatch()

    def _batch_dispatch_next(self) -> None:
        if not self._batch_active or self._batch_queue is None:
            return
        if self.import_page.import_active or self.task_manager.active or self.operation_manager.active:
            return
        if self._batch_queue.cancelled or self._batch_cancel_flag:
            self._batch_finish_or_dispatch()
            return

        while self._batch_retry_ids:
            token = self._batch_retry_ids.pop(0)
            if token.startswith("export:"):
                item_id = token.split(":", 1)[1]
                if self._start_batch_export(item_id, retry=True):
                    return
                continue
            if token.startswith("first:"):
                item_id = token.split(":", 1)[1]
                item = self._batch_queue.get(item_id)
                if item.status != "pending":
                    continue
                if item.preflight_error:
                    try:
                        self._batch_queue.fail_pending_preflight(item_id, item.preflight_error)
                        self._write_batch_manifest()
                    except Exception as exc:
                        self._batch_stop_due_manifest(f"无法记录输入预检失败：{exc}")
                        return
                    self._refresh_batch_queue_view()
                    continue
                if not item.structure_confirmed:
                    self._batch_enter_mapping_review(item)
                    return
                self._start_batch_item(item_id, retry=False)
                return
            item = self._batch_queue.get(token)
            if item.status not in {"failed", "partial", "cancelled"}:
                continue
            if not item.structure_confirmed:
                self._batch_enter_mapping_review(item)
                return
            self._start_batch_item(item.item_id, retry=True)
            return

        pending = [item for item in self._batch_queue.items if item.status == "pending"]
        preflight_failure = next((item for item in pending if item.preflight_error), None)
        if preflight_failure is not None:
            try:
                self._batch_queue.fail_pending_preflight(
                    preflight_failure.item_id,
                    preflight_failure.preflight_error or "输入预检失败。",
                )
                self._write_batch_manifest()
            except Exception as exc:
                self._batch_stop_due_manifest(f"无法记录输入预检失败：{exc}")
                return
            self._refresh_batch_queue_view()
            self._batch_dispatch_next()
            return
        next_item = next((item for item in pending if item.structure_confirmed), None)
        if next_item is not None:
            self._start_batch_item(next_item.item_id, retry=False)
            return
        if pending:
            self._batch_enter_mapping_review(pending[0])
            return
        self._batch_finish_or_dispatch()

    def _batch_enter_mapping_review(self, item: BatchItem) -> None:
        self._batch_active = True
        self._batch_waiting_for_review = True
        self._batch_review_item_id = item.item_id
        self._refresh_busy_state()
        self._batch_set_busy(False)
        self._refresh_batch_queue_view()
        if self._batch_dialog is not None:
            self._batch_dialog.select_item(item.item_id)
            self._batch_dialog.set_preflight_messages(
                item.item_id,
                [item.error or "mapping_confirmation_required：请在导入页核对实际列结构后再重试。"],
            )
        self.status_label.setText(
            f"批量队列等待映射确认：{Path(item.source_path).name}。队列不会静默套用旧列映射。"
        )

    def _start_batch_item(self, item_id: str, *, retry: bool) -> None:
        if self._batch_queue is None or self._batch_base_request is None:
            return
        item = self._batch_queue.get(item_id)
        try:
            request = self._batch_item_requests.get(item_id)
            if request is None:
                request = self.import_page.batch_request_for_source(
                    self._batch_base_request, item.source_path,
                )
            updated = self._batch_queue.retry_analysis(item_id) if retry else self._batch_queue.start_item(item_id)
            self._batch_current_item_id = item_id
            self._batch_current_attempt = updated.current_attempt.number if updated.current_attempt else 0
            self._batch_current_phase = "importing"
            self._batch_item_revision = None
            self._batch_task_result = None
            self._batch_import_result = None
            self._batch_import_error = None
            self._batch_import_generation = self.import_page.start_batch_import(request)
            if not self._batch_import_generation:
                self._batch_queue.transition(
                    item_id, "failed", error="批量导入 worker 未能启动。", export_requested=False,
                )
                self._batch_current_item_id = None
                self._batch_current_phase = None
                self._write_batch_manifest()
                self._refresh_batch_queue_view()
                self._batch_dispatch_next()
                return
            self._batch_set_busy(True)
            self._refresh_busy_state()
            self._write_batch_manifest()
            self._refresh_batch_queue_view()
        except Exception as exc:
            self._batch_show_error("无法启动条目", str(exc))
            if self._batch_queue.active_item_id == item_id:
                self._batch_queue.transition(item_id, "failed", error=str(exc), export_requested=False)
                self._write_batch_manifest()
                self._batch_dispatch_next()

    def _on_batch_import_completed(self, generation: int, revision: object, preview: object) -> None:
        if generation != self._batch_import_generation or self._batch_current_phase != "importing":
            return
        if isinstance(revision, DatasetRevision) and isinstance(preview, pd.DataFrame):
            self._batch_import_result = (revision, preview.copy(deep=True))

    def _on_batch_import_failed(self, generation: int, message: str) -> None:
        if generation == self._batch_import_generation:
            self._batch_import_error = str(message)

    def _on_batch_import_cancelled(self, generation: int) -> None:
        if generation == self._batch_import_generation:
            self._batch_import_error = "cancelled"
        self._finish_timing(
            "cancelled", train=False, identity=("import", generation),
        )

    def _on_batch_import_finished(self, generation: int) -> None:
        if generation != self._batch_import_generation or self._batch_queue is None:
            return
        item_id = self._batch_current_item_id
        if item_id is None or self._batch_current_phase != "importing":
            return
        self._batch_import_generation = None
        try:
            if self._batch_import_error == "cancelled" or self._batch_cancel_flag:
                self._batch_queue.transition(item_id, "cancelled", error="批量导入已取消。", export_requested=False)
                self._batch_current_phase = None
                self._batch_current_item_id = None
                self._write_batch_manifest()
                self._refresh_batch_queue_view()
                self._batch_finish_or_dispatch()
                return
            if self._batch_import_error:
                self._batch_queue.transition(
                    item_id, "failed", error="导入失败：" + self._batch_import_error,
                    export_requested=False,
                )
                self._batch_current_phase = None
                self._batch_current_item_id = None
                self._write_batch_manifest()
                self._refresh_batch_queue_view()
                self._batch_dispatch_next()
                return
            if self._batch_import_result is None:
                raise RuntimeError("导入 worker 未返回可用的 DatasetRevision。")
            revision, preview = self._batch_import_result
            item = self._batch_queue.get(item_id)
            source_key = f"signals/{Path(item.source_path).name}"
            imported_hash = revision.source_hashes.get(source_key)
            if imported_hash != item.source_sha256:
                raise RuntimeError("导入时的信号源哈希与加入队列时不一致；文件可能已改变。")
            revision = self._install_batch_context_revision(revision)
            self.set_dataset_revision(revision, preview=preview)
            self._restore_batch_context_page_state()
            self._batch_item_revision = revision
            self._batch_queue.transition(item_id, "checking")
            self._batch_current_phase = "preflight"
            self._write_batch_manifest()
            self._refresh_batch_queue_view()
            QTimer.singleShot(0, self._start_batch_preflight)
        except Exception as exc:
            try:
                if self._batch_queue.get(item_id).status == "importing":
                    self._batch_queue.transition(
                        item_id, "failed", error=f"导入/来源核对失败：{type(exc).__name__}: {exc}",
                        export_requested=False,
                    )
            except Exception:
                pass
            self._batch_current_phase = None
            self._batch_current_item_id = None
            self._write_batch_manifest()
            self._refresh_batch_queue_view()
            self._batch_dispatch_next()

    def _install_batch_context_revision(self, revision: DatasetRevision) -> DatasetRevision:
        raw_sources = {**revision.raw_source_bytes(), **self._batch_context_raw_sources}
        if raw_sources != revision.raw_source_bytes():
            return DatasetRevision.create(revision.dataset_copy(), raw_sources=raw_sources)
        return revision

    def _restore_batch_context_page_state(self) -> None:
        self._context_tables = {
            name: frame.copy(deep=True) for name, frame in self._batch_context.items()
        }
        self._context_raw_sources = dict(self._batch_context_raw_sources)
        self._import_metadata = deepcopy(getattr(self, "_batch_import_metadata", {}))
        self._individual_context_parameters = deepcopy(
            getattr(self, "_batch_individual_context_parameters", {})
        )
        with self._suppress_configuration_events():
            self.quality_page.set_context_tables(
                self._context_tables, self.import_page.context_source_manifest(),
            )
            self.group_page.set_dataset(
                self.session.dataset_revision,
                events=self._context_tables.get("external_events"),
            )
            self.design_page.set_performance_table(
                self.session.dataset_revision.dataset_copy().performance
                if self.session.dataset_revision is not None else None
            )

    def _start_batch_preflight(self) -> None:
        self._start_batch_pipeline(PipelinePhase.PREFLIGHT)

    def _start_batch_run(self) -> None:
        self._start_batch_pipeline(PipelinePhase.RUN)

    def _start_batch_pipeline(self, phase: PipelinePhase) -> None:
        if (not self._batch_active or self._batch_queue is None or self._batch_current_item_id is None
                or self._batch_item_revision is None or self.task_manager.active):
            return
        try:
            snapshot_parameters = _batch_parameters_for_revision(
                self._batch_parameters, self._batch_item_revision,
            )
            individual_value = snapshot_parameters.get("individual", {})
            individual = deepcopy(dict(individual_value)) if isinstance(individual_value, Mapping) else {}
            metadata_value = individual.get("metadata", {})
            individual_metadata = deepcopy(dict(metadata_value)) if isinstance(metadata_value, Mapping) else {}
            for key in (
                "aoi_mapping_confirmed", "aoi_mapping_refs",
                "aoi_mapping_confirmed_at", "aoi_mapping_revision_fingerprint",
            ):
                individual_metadata.pop(key, None)
            individual_metadata.update(
                self.import_page.individual_aoi_mapping_metadata(self._batch_item_revision)
            )
            individual["metadata"] = individual_metadata
            snapshot_parameters["individual"] = individual
            snapshot = self.session.create_snapshot(
                selection=self._batch_selection,
                schema_version=str(self._batch_schema_version),
                parameters=snapshot_parameters,
                context={name: frame.copy(deep=True) for name, frame in self._batch_context.items()},
                dataset_revision=self._batch_item_revision,
            )
            key = (snapshot.run_id, snapshot.generation)
            self._snapshots[key] = snapshot
            self._snapshot_phases[key] = phase
            self._batch_expected_task = (snapshot.run_id, snapshot.generation, phase.value)
            self._batch_current_phase = phase.value
            reuse_plan = self._metric_reuse_plan(snapshot) if phase is PipelinePhase.RUN else None
            timing_kind = "preflight" if phase is PipelinePhase.PREFLIGHT else "run"
            phase_label = (
                f"{Path(self._batch_queue.get(self._batch_current_item_id).source_path).name} · "
                + ("正在检查可用条件" if phase is PipelinePhase.PREFLIGHT else "正在运行分析")
            )
            timing_identity = ("pipeline", snapshot.run_id, snapshot.generation)
            self._start_timing(
                timing_kind,
                phase_label,
                identity=timing_identity,
                workload_key=self._timing_workload_key(self._batch_selection),
            )
            self.task_manager.start(snapshot, phase=phase, bridge=self.bridge, reuse_plan=reuse_plan)
            self._batch_set_busy(True)
            self._refresh_busy_state()
            self._refresh_batch_queue_view()
        except Exception as exc:
            if "snapshot" in locals():
                self._finish_timing(
                    "failed", train=False,
                    identity=("pipeline", snapshot.run_id, snapshot.generation),
                )
            if "key" in locals():
                self._snapshots.pop(key, None)
                self._snapshot_phases.pop(key, None)
            item = self._batch_queue.get(self._batch_current_item_id)
            if item.status in {"checking", "running"}:
                self._batch_queue.transition(
                    item.item_id, "failed", error=f"任务无法启动：{type(exc).__name__}: {exc}",
                    export_requested=False,
                )
            self._batch_current_item_id = None
            self._batch_current_phase = None
            self._write_batch_manifest()
            self._refresh_batch_queue_view()
            self._batch_dispatch_next()

    def _on_batch_task_result(self, run_id: str, generation: int, result: Any) -> bool:
        expected = self._batch_expected_task
        if not (self._batch_active and expected is not None
                and expected[:2] == (run_id, generation)):
            return False
        item_id = self._batch_current_item_id
        if item_id is None or self._batch_queue is None:
            return False
        item = self._batch_queue.get(item_id)
        if item.current_attempt is None or item.current_attempt.number != self._batch_current_attempt:
            return False
        self._batch_task_result = {"result": result, "run_id": run_id, "generation": generation}
        return True

    def _batch_task_failure(self, run_id: str, generation: int, error_type: str, details: str) -> None:
        if self._on_batch_task_result(run_id, generation, {
            "failure_type": str(error_type), "failure_reason": str(details),
        }):
            self._batch_task_result = {
                "failure_type": str(error_type), "failure_reason": str(details),
                "run_id": run_id, "generation": generation,
            }

    def _batch_task_finished(self, run_id: str, generation: int) -> None:
        expected = self._batch_expected_task
        if not (self._batch_active and expected is not None and expected[:2] == (run_id, generation)):
            return
        item_id = self._batch_current_item_id
        if item_id is None or self._batch_queue is None:
            return
        item = self._batch_queue.get(item_id)
        if item.current_attempt is None or item.current_attempt.number != self._batch_current_attempt:
            self._append_log("忽略批量队列中的迟到任务回调；条目尝试已变化。")
            self._batch_expected_task = None
            return
        phase = expected[2]
        task_result = self._batch_task_result or {}
        result = task_result.get("result")
        if task_result.get("failure_type"):
            status = "failed"
            reason = f"{task_result['failure_type']}: {task_result.get('failure_reason', '')}"
        elif isinstance(result, PipelineOutcome):
            if result.status in {PipelineStatus.COMPLETE, PipelineStatus.PARTIAL}:
                status = result.status.value
                reason = None
            elif result.status is PipelineStatus.CANCELLED:
                status, reason = "cancelled", "任务已取消。"
            else:
                status, reason = "failed", f"后端状态未完成：{result.backend_status}"
        else:
            status, reason = "failed", "任务未返回可识别的状态。"
        self._batch_expected_task = None
        self._batch_task_result = None
        self._batch_pending_idle_action = None
        if phase == PipelinePhase.PREFLIGHT.value:
            if status in {"complete", "partial"} and not self._batch_cancel_flag:
                try:
                    self._batch_queue.transition(item_id, "running")
                    self._batch_pending_idle_action = "run"
                except BatchQueueError as exc:
                    status, reason = "failed", str(exc)
                    self._batch_queue.transition(item_id, "failed", error=reason, export_requested=False)
                    self._batch_pending_idle_action = "next"
            elif status == "cancelled" or self._batch_cancel_flag:
                status, reason = "cancelled", reason or "队列已取消。"
                self._batch_queue.transition(item_id, "cancelled", error=reason, export_requested=False)
                self._batch_pending_idle_action = "next"
            else:
                status = "failed"
                reason = reason or "前提检查没有返回可继续运行的状态。"
                self._batch_queue.transition(item_id, "failed", error=reason, export_requested=False)
                self._batch_pending_idle_action = "next"
        elif phase == PipelinePhase.RUN.value:
            record = self._batch_record_by_run.get(run_id)
            if status in {"complete", "partial"} and record is not None:
                self._batch_queue.transition(item_id, status, run_id=run_id)
                self._batch_pending_idle_action = "export"
            elif status == "cancelled" or self._batch_cancel_flag:
                self._batch_queue.transition(
                    item_id, "cancelled", run_id=run_id, error=reason or "队列已取消。",
                    export_requested=False,
                )
                self._batch_pending_idle_action = "next"
            else:
                self._batch_queue.transition(
                    item_id, "failed", run_id=run_id, error=reason or "分析失败。",
                    export_requested=False,
                )
                self._batch_pending_idle_action = "next"
        self._batch_current_phase = None
        self._write_batch_manifest()
        self._refresh_batch_queue_view()

    def _batch_start_pending_after_idle(self) -> None:
        action, self._batch_pending_idle_action = getattr(self, "_batch_pending_idle_action", None), None
        if not self._batch_active:
            return
        if self._batch_cancel_flag or (self._batch_queue is not None and self._batch_queue.cancelled):
            self._batch_finish_or_dispatch()
        elif action == "run":
            self._start_batch_run()
        elif action == "export" and self._batch_current_item_id is not None:
            if not self._start_batch_export(self._batch_current_item_id, retry=False):
                self._batch_current_item_id = None
                self._batch_current_attempt = 0
                self._batch_item_revision = None
                self._batch_dispatch_next()
        else:
            self._batch_current_item_id = None
            self._batch_current_attempt = 0
            self._batch_item_revision = None
            self._batch_dispatch_next()

    def _start_batch_export(self, item_id: str, *, retry: bool) -> bool:
        from .batch_export import BatchOutputFailure

        if self._batch_queue is None or self._batch_output_store is None:
            return False
        item = self._batch_queue.get(item_id)
        record = self._batch_record_by_run.get(str(item.run_id))
        if record is None:
            self._batch_show_error("运行记录不可用", f"无法为 {item_id} 找到可导出的 RunRecord。")
            return False
        try:
            item = self._batch_queue.retry_export(item_id) if retry else self._batch_queue.start_export(item_id)
            attempt = item.current_attempt
            if attempt is None:
                raise RuntimeError("批量导出缺少分析尝试记录。")
            attempt_dir = self._batch_output_store.reserve_attempt_dir(
                Path(item.source_path).name, item.item_id,
                f"a{attempt.number}-e{attempt.export_attempts}",
            )
            self._batch_current_item_id = item_id
            self._batch_current_attempt = attempt.number
            self._batch_current_phase = "exporting"
            self._batch_export_generation = None
            next_generation = self.operation_manager.generation + 1
            timing_identity = ("operation", next_generation)
            self._start_timing(
                "batch_export", "正在更新批量队列清单",
                identity=timing_identity,
            )
            if not self._write_batch_manifest(timing_identity=timing_identity):
                raise RuntimeError("批量队列清单无法保存，已停止当前导出。")
            def export_operation(cancel, progress):
                try:
                    return self._batch_output_store.export_record(
                        record, attempt_dir, cancel_requested=cancel, progress=progress,
                    )
                except BatchOutputFailure as exc:
                    return _BatchExportFailureReceipt(
                        run_id=str(record.run_id), error=str(exc),
                        output_paths=tuple(exc.committed_paths),
                    )
            self._start_operation(
                "batch_export", export_operation, timing_already_started=True,
            )
            self._batch_export_generation = self.operation_manager.generation
            self._batch_set_busy(True)
            self._refresh_batch_queue_view()
            return True
        except Exception as exc:
            if "timing_identity" in locals():
                self._finish_timing("failed", train=False, identity=timing_identity)
            if item.export_status == "exporting":
                try:
                    self._batch_queue.finish_export(
                        item_id, "failed", error=f"{type(exc).__name__}: {exc}",
                    )
                except BatchQueueError:
                    pass
            self._batch_current_phase = None
            self._write_batch_manifest()
            self._refresh_batch_queue_view()
            return False

    def _batch_handle_export_result(self, result: Any) -> None:
        if self._batch_queue is None or self._batch_current_item_id is None:
            return
        item_id = self._batch_current_item_id
        if isinstance(result, _BatchExportFailureReceipt):
            status = "failed"
            output_paths = [str(path) for path in result.output_paths]
            error = result.error
        elif getattr(result, "cancelled", False):
            status = "cancelled"
            output_paths = [str(path) for path in getattr(result, "output_paths", ())]
            error = None
        elif getattr(result, "complete", False) is True:
            status = "complete"
            output_paths = [str(path) for path in getattr(result, "output_paths", ())]
            error = None
        else:
            status = "failed"
            output_paths = [str(path) for path in getattr(result, "output_paths", ())]
            error = "导出未返回完整或取消终态收据。"
        try:
            self._batch_queue.finish_export(item_id, status, output_paths=output_paths, error=error)
        except BatchQueueError as exc:
            self._batch_show_error("无法记录导出终态", str(exc))
        self._batch_current_phase = None
        manifest_saved = self._write_batch_manifest()
        self._finish_timing(
            "failed" if not manifest_saved else status,
            train=manifest_saved and status == "complete",
            identity=("operation", self._batch_export_generation)
            if self._batch_export_generation is not None else None,
        )
        self._refresh_batch_queue_view()

    def _batch_finish_or_dispatch(self) -> None:
        if self._batch_active and (self.import_page.import_active or self.task_manager.active or self.operation_manager.active):
            return
        if self._batch_cancel_flag or (self._batch_queue is not None and self._batch_queue.cancelled):
            if self._batch_queue is not None:
                active_item_id = self._batch_queue.active_item_id
                if active_item_id is not None:
                    active_item = self._batch_queue.get(active_item_id)
                    if active_item.status in {"importing", "checking", "running"}:
                        self._batch_queue.transition(
                            active_item_id, "cancelled", error="队列已取消。",
                            export_requested=False,
                        )
                for queued_item in self._batch_queue.items:
                    if (queued_item.status in {"complete", "partial"}
                            and queued_item.export_status == "pending"):
                        self._batch_queue.cancel_pending_export(
                            queued_item.item_id, "队列已取消；导出尚未启动。",
                        )
            self._batch_current_item_id = None
            self._batch_current_attempt = 0
            self._batch_current_phase = None
            self._batch_expected_task = None
            self._batch_pending_idle_action = None
            self._batch_waiting_for_review = False
            self._batch_active = False
            self._batch_set_busy(False)
            self._write_batch_manifest()
            self._refresh_batch_queue_view()
            self._refresh_busy_state()
            return
        has_retry = bool(self._batch_retry_ids)
        has_pending = bool(self._batch_queue and any(item.status == "pending" for item in self._batch_queue.items))
        if has_retry or has_pending:
            self._batch_dispatch_next()
            return
        self._batch_current_item_id = None
        self._batch_current_attempt = 0
        self._batch_current_phase = None
        self._batch_waiting_for_review = False
        self._batch_active = False
        self._batch_set_busy(False)
        self._write_batch_manifest()
        if self._batch_dialog is not None:
            self._batch_dialog.set_preflight_messages("__summary__", [
                "批量队列已结束。请按分析状态和导出状态分别查看成功、部分结果与失败条目。"
            ])
        self._refresh_batch_queue_view()
        self._refresh_busy_state()

    def _write_batch_manifest(self, *, timing_identity: object = None) -> bool:
        if self._batch_queue is None or self._batch_output_store is None:
            return True
        try:
            def progress(_fraction: float, message: str) -> None:
                identity = timing_identity
                if identity is None:
                    if (self._active_timing is None
                            or self._active_timing.kind != "batch_export"):
                        return
                    identity = self._timing_identity
                if (self._active_timing is not None
                        and self._active_timing.kind == "batch_export"
                        and self._active_timing.snapshot().status == "running"):
                    self._update_timing_phase(str(message), identity=identity)

            path = self._batch_output_store.write_manifest(
                self._batch_queue.manifest(), progress=progress,
            )
            self._batch_manifest_error = None
            if self._batch_dialog is not None:
                self._batch_dialog.set_output_directory(str(path.parent))
            return True
        except Exception as exc:
            self._batch_manifest_error = f"清单写入失败：{type(exc).__name__}: {exc}"
            self._append_log(self._batch_manifest_error)
            self.status_label.setText(self._batch_manifest_error + "；队列已暂停以免静默丢失记录。")
            if self._batch_dialog is not None:
                self._batch_dialog.set_preflight_messages("__manifest__", [self._batch_manifest_error])
            if self._batch_active:
                self._batch_cancel_flag = True
                self._request_timing_cancel()
                if self.import_page.import_active:
                    self.import_page.cancel_import()
                if self.task_manager.active:
                    self.task_manager.cancel()
                if self.operation_manager.active:
                    self.operation_manager.cancel()
            return False

    def _batch_stop_due_manifest(self, message: str) -> None:
        self._batch_manifest_error = str(message)
        self._batch_cancel_flag = True
        self._request_timing_cancel()
        if self._batch_queue is not None:
            self._batch_queue.cancel()
        self._write_batch_manifest()
        self._batch_finish_or_dispatch()

    def _refresh_batch_queue_view(self) -> None:
        if self._batch_dialog is None:
            return
        self._batch_dialog.set_queue_view(self._batch_queue.items if self._batch_queue is not None else ())
        if self._batch_prepared and self._batch_configuration_summary:
            heading = "准备摘要：尚未派发任务。核对辅助来源、默认身份和输出位置后，点击“按此配置开始”。"
            display_summary: Any = heading + "\n\n" + json.dumps(
                self._batch_configuration_summary, ensure_ascii=False, sort_keys=True, indent=2, default=str,
            )
        else:
            display_summary = (
                self._batch_display_notice or self._batch_configuration_summary
                or "按文件独立分析；开始前会先显示配置摘要。"
            )
        self._batch_dialog.set_configuration_summary(display_summary)
        if self._batch_output_store is not None:
            self._batch_dialog.set_output_directory(
                str(self._batch_output_store.queue_directory or self._batch_output_store.output_root)
            )
        if self._batch_manifest_error:
            self._batch_dialog.set_preflight_messages("__manifest__", [self._batch_manifest_error])
        self._batch_set_busy(
            self._batch_active and not self._batch_waiting_for_review
            or self._batch_inspector is not None or self.import_page.import_active
            or self.task_manager.active or self.operation_manager.active
        )
        self._batch_dialog.set_prepared(self._batch_prepared)
        self._refresh_timing_display()

    def _batch_set_busy(self, busy: bool) -> None:
        if self._batch_dialog is not None:
            self._batch_dialog.set_busy(bool(busy), cancelable=bool(self._batch_active))

    def _batch_show_error(self, title: str, message: str) -> None:
        self.status_label.setText(str(message))
        self._append_log(f"{title}：{message}")
        if self._batch_dialog is not None:
            QMessageBox.warning(self._batch_dialog, str(title), str(message))

    def _set_batch_review_controls(self, review: bool) -> None:
        if not hasattr(self, "pages"):
            return
        active = bool(self._batch_active)
        self.import_page.set_batch_review_mode(active and review)
        self.step_list.setEnabled(not active)
        if active:
            self.step_list.setCurrentRow(0 if review else self.step_list.currentRow())
            self.pages.setCurrentIndex(0 if review else self.pages.currentIndex())
        for index in range(self.pages.count()):
            widget = self.pages.widget(index)
            widget.setEnabled(not active or (review and index == 0))

    def _cancel_current_task(self):
        self._request_timing_cancel()
        if self._batch_active:
            self._batch_cancel_requested()
            return
        self.task_manager.cancel()
        self.operation_manager.cancel()
        self.import_page.cancel_import()

    def _start_operation(self, kind, operation, *, timing_already_started=False, workload_key=None):
        if (self.task_manager.active or self.operation_manager.active or self.import_page.import_active
                or (self._batch_active and kind != "batch_export")):
            raise RuntimeError("请先完成或取消当前后台任务。")
        self._operation_kind = kind
        self._operation_session_generation = self.session.generation
        self._operation_result = None
        self.cancel_action.setEnabled(True)
        self.results_page.set_c_capabilities(busy=True)
        self.preflight_action.setEnabled(False)
        self.run_action.setEnabled(False)
        next_generation = self.operation_manager.generation + 1
        phase_labels = {
            "prepare": "正在准备归档内容",
            "save": "正在提交并保存归档",
            "load": "正在读取并校验归档",
            "export": "正在导出结果表",
            "report": "正在生成 PDF 报告",
            "batch_export": "正在导出本批次结果",
        }
        operation_identity = ("operation", next_generation)
        if timing_already_started:
            if self._timing_identity != operation_identity:
                raise RuntimeError("预启动的操作计时身份已失效。")
            self._update_timing_phase(
                phase_labels.get(kind, "正在处理"), identity=operation_identity,
            )
        else:
            self._start_timing(
                kind,
                phase_labels.get(kind, "正在处理"),
                identity=operation_identity,
                workload_key=workload_key,
            )
        try:
            self.operation_manager.start(operation)
        except Exception:
            self._finish_timing("failed", train=False, identity=("operation", next_generation))
            raise
        self._refresh_busy_state()

    def _on_operation_progress(self, generation, fraction, message):
        if generation == self.operation_manager.generation:
            self.progress_bar.setValue(round(float(fraction) * 100))
            self.status_label.setText(message)
            self._update_timing_phase(message, identity=("operation", generation))

    def _on_operation_completed(self, generation, result):
        if generation != self.operation_manager.generation:
            if hasattr(result, "close"):
                result.close()
            return
        self._operation_result = (generation, result)
        identity = ("operation", generation)
        kind = self._operation_kind
        if kind == "batch_export":
            # Keep the timer live through the final queue-manifest commit; the
            # batch receipt determines the true terminal state.
            pass
        elif kind == "load" and self.session.generation != self._operation_session_generation:
            self._finish_timing("stale", train=False, identity=identity)
        elif getattr(result, "cancelled", False) is True:
            self._finish_timing("cancelled", train=False, identity=identity)
        elif isinstance(result, _BatchExportFailureReceipt):
            self._finish_timing("failed", train=False, identity=identity)
        else:
            self._finish_timing(
                "complete",
                train=True,
                identity=identity,
            )

    def _on_operation_failed(self, generation, details):
        if generation == self.operation_manager.generation:
            self._finish_timing("failed", train=False, identity=("operation", generation))
            if self._operation_kind == "batch_export" and generation == self._batch_export_generation:
                self._batch_export_error = str(details)
                self._operation_result = None
                self._append_log("批量结果导出失败：" + str(details))
                return
            self._archive_before_close = False
            self.status_label.setText("归档/导出失败；原文件和当前结果保持不变")
            self._append_log(details)
            QMessageBox.warning(self, "操作失败", details)

    def _on_operation_cancelled(self, generation):
        if generation == self.operation_manager.generation:
            self._finish_timing("cancelled", train=False, identity=("operation", generation))
            if self._operation_kind == "batch_export" and generation == self._batch_export_generation:
                self._batch_export_cancelled = True
                self._append_log("批量结果导出已取消。")
                return
            self._archive_before_close = False
            self._append_log("归档/导出任务已取消；当前结果保持不变。")

    def _on_operation_idle(self):
        self._refresh_dependency_state()
        result, self._operation_result = self._operation_result, None
        result_generation = None
        if result is not None:
            result_generation, result = result
            if result_generation != self.operation_manager.generation:
                if hasattr(result, "close"):
                    result.close()
                result = None
        kind = self._operation_kind
        if kind == "batch_export":
            if result is not None:
                if result_generation == self._batch_export_generation:
                    self._batch_handle_export_result(result)
            elif self._batch_export_error:
                self._batch_handle_export_result(_BatchExportFailureReceipt(
                    run_id=str(self._batch_queue.get(self._batch_current_item_id).run_id)
                    if self._batch_queue is not None and self._batch_current_item_id else "unknown",
                    error=self._batch_export_error,
                    output_paths=(),
                ))
            elif self._batch_export_cancelled:
                self._batch_handle_export_result(type("_CancelledReceipt", (), {
                    "cancelled": True, "complete": False, "output_paths": (),
                })())
            self._batch_export_error = None
            self._batch_export_cancelled = False
            self._batch_export_generation = None
            self._batch_current_item_id = None
            self._batch_current_attempt = 0
            self._batch_current_phase = None
            self._batch_start_pending_after_idle()
            self._refresh_busy_state()
            if getattr(self, "_close_pending", False):
                self._maybe_finish_close()
            return
        if result is not None:
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
                    "report",
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
        from .help_center import open_pdf_guide
        open_pdf_guide(self)

    def set_dataset_revision(
        self, revision: DatasetRevision, *, preview: pd.DataFrame | None = None,
        allow_aoi_confirmation: bool = False,
    ) -> None:
        self.session.install_dataset(revision)
        self.import_page.set_aoi_confirmation_revision(
            revision, allow_user_confirmation=allow_aoi_confirmation,
        )
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
        self._refresh_preview_state("data")
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
        timing_identity = self._import_timing_identity()
        self._context_tables = self._current_context_tables()
        self._context_raw_sources = self._current_context_raw_sources()
        self._import_metadata = self._current_import_metadata()
        self._individual_context_parameters = self._current_individual_context_parameters()
        self.set_dataset_revision(
            revision, preview=preview, allow_aoi_confirmation=not self._batch_active,
        )
        self._append_log(
            f"导入完成：{len(revision.dataset_copy().signals):,} 个规范化信号行；"
            f"具名上下文表 {', '.join(sorted(self._context_tables)) or '无'}。"
        )
        self._finish_timing("complete", train=True, identity=timing_identity)

    def _on_import_progress(self, percent: int, message: str) -> None:
        if self.import_page.import_active:
            self.progress_bar.setValue(max(0, min(100, int(percent))))
            self.status_label.setText(message)
            self._update_timing_phase(message, identity=self._import_timing_identity())

    def _import_timing_identity(self) -> tuple[str, int | None]:
        generation = getattr(self.import_page, "_worker_generation", None)
        if generation is not None:
            return ("import", generation)
        if self._active_import_timing_identity is not None:
            return self._active_import_timing_identity
        return ("import", None)

    def _on_import_failed(self, message: str) -> None:
        self._finish_timing("failed", train=False, identity=self._import_timing_identity())
        self._append_log("导入失败：" + str(message))

    def _on_import_state_changed(self, active: bool, message: str) -> None:
        if active and message:
            identity = self._import_timing_identity()
            self._active_import_timing_identity = identity
            if self._timing_identity != identity or self._active_timing is None:
                self._start_timing("import", message, identity=identity)
            if "已请求取消" in message or "正在取消" in message:
                self._request_timing_cancel()
            else:
                self._update_timing_phase(message, identity=identity)
            self.status_label.setText(message)
            self.statusBar().showMessage(message)
        elif not active and not (self.task_manager.active or self.operation_manager.active):
            identity = self._import_timing_identity()
            snapshot = self._active_timing.snapshot() if self._active_timing is not None else None
            if (self._timing_identity == identity
                    and snapshot is not None and snapshot.status in {"running", "cancelling"}):
                self._finish_timing(
                    "cancelled" if snapshot.status == "cancelling" else "stopped",
                    train=False,
                    identity=identity,
                )
            if self._active_import_timing_identity == identity:
                self._active_import_timing_identity = None
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
        batch_busy = self._batch_active and not self._batch_waiting_for_review
        other_busy = pipeline_busy or operation_busy or batch_busy
        self.import_page.set_external_busy(other_busy)
        busy = other_busy or import_busy or self._batch_active
        self.save_plan_action.setEnabled(not busy)
        self.load_plan_action.setEnabled(not busy)
        ready = self.session.dataset_revision is not None
        self.preflight_action.setEnabled(bool(ready and not busy))
        self.run_action.setEnabled(bool(ready and not busy))
        self.cancel_action.setEnabled(busy)
        self.results_page.set_c_capabilities(
            archive=True, reopen=True, full_export=True, busy=busy
        )
        if self._batch_active and self._batch_waiting_for_review:
            self._set_batch_review_controls(True)
        elif self._batch_active:
            self._set_batch_review_controls(False)
        else:
            self._set_batch_review_controls(False)

    def _on_dataset_invalidated(self, reason: str) -> None:
        self.import_page.clear_aoi_confirmation_revision(
            "数据修订已失效；重新导入后再核对 AOI 映射。"
        )
        if self.session.dataset_revision is None:
            return
        self.session.clear_dataset()
        self._preview_model.set_frame(None)
        self._refresh_preview_state("empty")
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

    def _refresh_preview_state(self, kind: str) -> None:
        has_rows = self._preview_model.rowCount() > 0
        self.preview_box.setVisible(has_rows)
        if not has_rows:
            return
        if kind == "metrics":
            self.preview_box.setTitle("指标结果预览")
            self.preview_hint.setText("显示当前运行返回的只读指标表；完整结果、状态和来源请查看“结果与追溯”。")
        else:
            self.preview_box.setTitle("数据预览")
            self.preview_hint.setText("显示当前数据修订的导入预览；此处只读，不运行分析，也不代表设备已验证。")

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
        had_preflight = getattr(self.metrics_page, "_availability", None) is not None
        had_active_run = self.session.active_snapshot is not None
        had_prepared_batch = self._batch_prepared and not self._batch_active
        if self._batch_prepared and not self._batch_active:
            self._invalidate_batch_preparation("分析配置已变化")
        if had_active_run:
            self.session.invalidate_active_run()
        self.metrics_page.invalidate_preflight()
        if self.import_page.import_active:
            self.status_label.setText(self.import_page.pending_import_message)
        elif self.session.dataset_revision is not None:
            self.status_label.setText("配置已变化；当前结果保留为历史，需重新前检或运行")
            if had_preflight or had_active_run or had_prepared_batch:
                self._append_log("配置已变化：原前检或运行结果状态已更新为历史；请重新前检或运行。")
        self._refresh_dependency_state()

    def _on_step_changed(self, row: int) -> None:
        if row < 0:
            return
        page_index = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 2, 6: 5, 7: 6}.get(row, 0)
        if row == 2:
            self.metrics_page.set_module("individual")
        elif row == 5:
            self.metrics_page.set_module("advanced")
        self.pages.setCurrentIndex(page_index)
        name, description = WORKFLOW_STEPS[row]
        self.page_title.setText(name)
        self.page_description.setText(description)
        self.contextual_help.set_default_text(
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
        if self.task_manager.active or self.operation_manager.active or self.import_page.import_active or self._batch_active:
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
            timing_kind = "preflight" if phase is PipelinePhase.PREFLIGHT else "run"
            timing_phase = "正在检查数据与方法条件" if phase is PipelinePhase.PREFLIGHT else "正在运行分析"
            timing_identity = ("pipeline", snapshot.run_id, snapshot.generation)
            self._start_timing(
                timing_kind,
                timing_phase,
                identity=timing_identity,
                workload_key=self._timing_workload_key(selection),
            )
            self.task_manager.start(snapshot, phase=phase, bridge=self.bridge, reuse_plan=reuse_plan)
        except Exception as exc:
            if "snapshot" in locals():
                self._finish_timing(
                    "failed", train=False,
                    identity=("pipeline", snapshot.run_id, snapshot.generation),
                )
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
        individual_metadata_value = individual.get("metadata", {})
        individual_metadata = (
            deepcopy(dict(individual_metadata_value))
            if isinstance(individual_metadata_value, Mapping) else {}
        )
        for key in (
            "aoi_mapping_confirmed", "aoi_mapping_refs",
            "aoi_mapping_confirmed_at", "aoi_mapping_revision_fingerprint",
        ):
            individual_metadata.pop(key, None)
        individual_metadata.update(
            self.import_page.individual_aoi_mapping_metadata(self.session.dataset_revision)
        )
        individual["metadata"] = individual_metadata
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
        self._update_timing_phase(message, identity=("pipeline", run_id, generation))
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
        identity = ("pipeline", run_id, generation)
        if status == "cancel_requested":
            self._request_timing_cancel()
        elif status == "running":
            phase = "正在检查数据与方法条件" if message == PipelinePhase.PREFLIGHT.value else "正在运行分析"
            self._update_timing_phase(phase, identity=identity)
        self.status_label.setText(labels.get(status, status))
        self.statusBar().showMessage(message or labels.get(status, status))

    def _on_completed(self, run_id: str, generation: int, outcome: PipelineOutcome) -> None:
        self._on_batch_task_result(run_id, generation, outcome)
        current = self._is_current(run_id, generation)
        self._finish_timing(
            outcome.status.value if current else "stale",
            train=current and outcome.status in {PipelineStatus.COMPLETE, PipelineStatus.PARTIAL},
            identity=("pipeline", run_id, generation),
        )
        if current:
            audit = outcome.metadata.get("execution_cache", {})
            if isinstance(audit, Mapping):
                from .cache_feedback import cache_miss_feedback
                for message in cache_miss_feedback(audit):
                    self._append_log(message)
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
                if self._batch_expected_task is not None and self._batch_expected_task[:2] == (run_id, generation):
                    self._batch_record_by_run[run_id] = record
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
                self._refresh_preview_state("metrics")
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
        self._finish_timing("failed", train=False, identity=("pipeline", run_id, generation))
        self._batch_task_failure(run_id, generation, error_type, details)
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
            if self._batch_expected_task is not None and self._batch_expected_task[:2] == (run_id, generation):
                self._batch_record_by_run[run_id] = record
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
        self._batch_task_finished(run_id, generation)
        self._refresh_busy_state()

    def _on_history_selected(self, record: RunRecord) -> None:
        if not isinstance(record, RunRecord):
            return
        if self.operation_manager.active and self._operation_kind == "load":
            self._request_timing_cancel()
            self.operation_manager.cancel()
        try:
            stale_nodes = self._dependency_tracker.changed_nodes_since(record.run_id)
        except KeyError:
            stale_nodes = ()
        if record.run_id in self._stale_run_ids and not stale_nodes:
            stale_nodes = ("stale_at_return",)
        self.results_page.show_record(record, stale_nodes=stale_nodes)

    def _legacy_metric_navigation_request(self, values: object) -> dict[str, Any]:
        """Convert the older MetricsPage reason action into the shared target contract."""
        payload = dict(values) if isinstance(values, Mapping) else {}
        module = payload.get("module")
        metric_id = payload.get("metric_id")
        entry = payload.get("entry")
        diagnostic = dict(entry) if isinstance(entry, Mapping) else {}
        if isinstance(module, str):
            diagnostic.setdefault("module", module)
        if isinstance(metric_id, str):
            diagnostic.setdefault("metric_id", metric_id)

        raw_rows = payload.get("availability_rows", ())
        if isinstance(raw_rows, pd.DataFrame):
            rows = [dict(row) for row in raw_rows.to_dict(orient="records")]
        elif isinstance(raw_rows, (list, tuple)):
            rows = [dict(row) for row in raw_rows if isinstance(row, Mapping)]
        else:
            rows = []
        matching = [row for row in rows
                    if (metric_id is None or str(row.get("metric_id")) == str(metric_id))
                    and (module is None or row.get("module") in (None, module))]

        candidates: list[NavigationTarget] = []
        # A single scope row can provide exact availability facts. With multiple
        # scopes, only a common page/section/role is safe; never choose row zero.
        rows_to_inspect = matching if len(matching) == 1 else ([{}] if not matching else matching)
        for availability in rows_to_inspect:
            _primary, targets, _code = _target_for_diagnostic(
                diagnostic, availability, str(metric_id) if metric_id is not None else None
            )
            for target in targets:
                if target not in candidates:
                    candidates.append(target)
        target: NavigationTarget | None = None
        if len(candidates) == 1:
            target = candidates[0]
        elif candidates:
            destinations = {(item.page, item.section, item.source_role, item.metric_id, repr(item.aoi_id))
                            for item in candidates}
            if len(destinations) == 1:
                first = candidates[0]
                target = NavigationTarget(first.page, section=first.section, source_role=first.source_role,
                                          metric_id=first.metric_id, aoi_id=first.aoi_id)

        revision = self.session.dataset_revision
        diagnostic["availability_rows"] = rows
        return {
            "target": target,
            "run_id": "",
            "revision_id": revision.revision_id if revision is not None else None,
            "diagnostic": diagnostic,
        }

    @staticmethod
    def _coerce_navigation_target(value: object) -> NavigationTarget | None:
        if isinstance(value, NavigationTarget):
            target = value
        elif isinstance(value, Mapping):
            page = value.get("page")
            if not isinstance(page, str):
                return None
            target = NavigationTarget(
                page=page,
                section=value.get("section") if isinstance(value.get("section"), str) else None,
                field=value.get("field") if isinstance(value.get("field"), str) else None,
                source_role=value.get("source_role") if isinstance(value.get("source_role"), str) else None,
                aoi_id=value.get("aoi_id"),
                metric_id=value.get("metric_id") if isinstance(value.get("metric_id"), str) else None,
            )
        else:
            return None
        if target.page not in {"import", "quality", "metrics", "group", "design", "results"}:
            return None
        return target

    def _on_edit_requirements(self, values: object) -> None:
        self._route_remediation_navigation(self._legacy_metric_navigation_request(values))

    def _route_remediation_navigation(self, request: object) -> None:
        """Open a known settings location without applying data or changing values."""
        payload = dict(request) if isinstance(request, Mapping) else {}
        target = self._coerce_navigation_target(payload.get("target"))
        diagnostic = payload.get("diagnostic")
        diagnostic = dict(diagnostic) if isinstance(diagnostic, Mapping) else {}
        run_id = str(payload.get("run_id") or "")
        requested_revision = payload.get("revision_id")
        current_revision = self.session.dataset_revision
        current_revision_id = current_revision.revision_id if current_revision is not None else None

        if target is None:
            message = "没有可确认的设置入口；保留原始诊断，未推测目标位置。"
            self.status_label.setText(message)
            self.statusBar().showMessage(message, 12000)
            self._append_log(f"修复导航未执行（run_id={run_id or '当前前检'}）：结构化诊断没有确定目标。")
            return

        # Existing AOI remediation rules were created before the editor entry was
        # wired. Route those AOI targets to the editable source panel, never bind
        # a prior run's image metadata to the current dataset.
        target_page = target.page
        if target.section == "aois" and target.page in {"design", "import"}:
            target_page = "import"
        if target_page == "import" and target.field == "screen_size_px" and target.metric_id:
            module = self.metrics_page.module_for_metric(target.metric_id)
            if module in {"individual", "advanced"}:
                target_page = "metrics"
                target = NavigationTarget("metrics", section=module, field="screen_size_px",
                                          metric_id=target.metric_id)
            elif module == "group":
                target_page = "group"
                target = NavigationTarget("group", section="group", field="screen_size_px",
                                          metric_id=target.metric_id)

        step_row = {"import": 0, "quality": 1, "metrics": 2, "group": 3,
                    "design": 4, "results": 6}.get(target_page)
        if step_row is None:
            self._append_log(f"修复导航拒绝未知页面：{target_page!r}。")
            return
        if target_page == "metrics" and target.section == "advanced":
            step_row = 5
        self.step_list.setCurrentRow(step_row)

        busy = self._configuration_busy()
        focused = False
        detail = "已打开对应工作流页面。"
        if busy:
            detail = "当前任务锁定了设置；页面已打开，任务结束后才能编辑。"
        elif target_page == "import":
            focused, detail = self._focus_import_remediation_target(target, diagnostic)
        elif target_page == "quality":
            field = target.field
            if field:
                self.quality_page._report_parameter_error(f"processing.{field}")
                if self.quality_page._parameter_error_name == field:
                    QTimer.singleShot(0, self.quality_page._focus_parameter_error)
                    focused = True
                    detail = f"已定位到数据预处理参数 {field}。"
                else:
                    detail = f"已打开数据预处理页；当前页面没有可精确定位的控件 {field}。"
        elif target_page == "metrics":
            module = target.section if target.section in {"individual", "advanced"} else None
            if target.field and module:
                focused = self.metrics_page.focus_requirement(module, target.field)
                detail = (f"已定位到 {module} 参数 {target.field}。" if focused
                          else f"已打开指标页；当前 schema 没有可精确定位的参数 {target.field}。")
            elif target.metric_id:
                focused = self.metrics_page.focus_metric(target.metric_id, module)
                detail = (f"已定位到指标 {target.metric_id}。" if focused
                          else f"已打开指标页；指标 {target.metric_id} 在当前目录中不存在或模块不唯一。")
        elif target_page == "group":
            if target.field:
                self.group_page._report_parameter_error(f"group.{target.field}:")
                if self.group_page._parameter_error_location is not None:
                    QTimer.singleShot(0, self.group_page._focus_parameter_error)
                    focused = True
                    detail = f"已定位到多人分析参数 {target.field}。"
                else:
                    detail = f"已打开多人分析页；当前页面没有可精确定位的参数 {target.field}。"
        elif target_page == "design":
            field = target.field
            editor = self.design_page._design_editors.get(field) if field else None
            if editor is None and field:
                editor = self.design_page._option_editors.get(field)
            if editor is not None:
                self.design_page.tabs.setCurrentIndex(1 if field in self.design_page._option_editors else 0)
                QTimer.singleShot(0, lambda control=editor: control.setFocus(Qt.FocusReason.OtherFocusReason))
                focused = True
                detail = f"已定位到研究设计字段 {field}。"
            elif target.section == "identifiers":
                detail = "已打开研究设计页；该身份条件由导入来源和实验设计共同确认，请核对现有字段。"
        revision_note = ""
        if requested_revision is not None and current_revision_id is not None \
                and str(requested_revision) != str(current_revision_id):
            revision_note = (f" 该诊断来自修订 {requested_revision}，当前数据为 {current_revision_id}；"
                             "当前设置仍对应当前数据，旧运行不会自动载入。")
        elif requested_revision is not None and current_revision_id is None:
            revision_note = f" 该诊断来自修订 {requested_revision}，当前尚无已导入数据。"
        elif requested_revision is None and run_id:
            revision_note = " 该运行未提供可核验的数据修订ID，请自行核对当前来源。"
        aoi_id = target.aoi_id
        identity_note = f" AOI={aoi_id}" if aoi_id is not None else ""
        message = detail + identity_note + revision_note
        self.status_label.setText(message)
        self.statusBar().showMessage(message, 15000)
        self._append_log(f"修复导航 run_id={run_id or '当前前检'} target={target!r}：{message}")

    def _focus_import_remediation_target(
        self, target: NavigationTarget, diagnostic: Mapping[str, Any]
    ) -> tuple[bool, str]:
        page = self.import_page
        field = target.field
        role = target.source_role if target.source_role in page.sources else None
        if role is None and target.section in page.sources:
            role = target.section

        if target.section in {"validity", "validity_codes"}:
            page._coordinate_layout.setCurrentIndex(1)
            table = page._validity_codes.validity_table
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item is not None and item.text() == field:
                    table.setCurrentCell(row, 1)
                    table.scrollToItem(item)
                    widget = table.cellWidget(row, 1)
                    (widget or table).setFocus(Qt.FocusReason.OtherFocusReason)
                    return True, f"已定位到有效性码字段 {field}。"
            return False, "已打开有效性码表；当前映射中没有该字段行。"

        if target.section in {"reading_direction", "reading_directions"}:
            page._coordinate_layout.setCurrentIndex(2)
            table = page._reading_directions.table
            source_code = diagnostic.get("source_code")
            matches = [row for row in range(table.rowCount())
                       if source_code is not None and table.cellWidget(row, 0) is not None
                       and table.cellWidget(row, 0).text() == str(source_code)]
            if len(matches) == 1:
                row = matches[0]
                table.setCurrentCell(row, 1)
                table.scrollToItem(table.item(row, 0))
                table.cellWidget(row, 1).setFocus(Qt.FocusReason.OtherFocusReason)
                return True, f"已定位到阅读方向代码 {source_code}。"
            return False, "已打开阅读方向映射；诊断未提供唯一源代码，未猜测具体行。"

        if target.field == "timebase_id":
            page.scope.timebase_id.setFocus(Qt.FocusReason.OtherFocusReason)
            return True, "已定位到导入页的 timebase_id 设置。"

        if target.section == "context" or field in {
            "time_unit", "timestamp_unit", "time_origin", "timezone", "clock_source",
            "coordinate_unit", "coordinate_frame", "pupil_unit", "pupil_measure",
            "device_verified", "device_validation_status", "device_validation_method",
            "device_validation_evidence", "stimulus_mapping_confirmed",
        }:
            if field == "stimulus_mapping_confirmed":
                role = "aois"
            else:
                coordinate = page.coordinate
                controls = {
                    "time_unit": coordinate.time_unit,
                    "timestamp_unit": coordinate.time_unit,
                    "time_origin": coordinate.time_origin,
                    "timezone": coordinate.timezone,
                    "clock_source": coordinate.clock_source,
                    "coordinate_unit": coordinate.coordinate_unit,
                    "coordinate_frame": coordinate.coordinate_frame,
                    "device_verified": coordinate.device_status,
                    "device_validation_status": coordinate.device_status,
                    "device_validation_method": coordinate.device_method,
                    "device_validation_evidence": coordinate.device_evidence,
                }
                if field == "pupil_unit":
                    metric_id = str(target.metric_id or "").casefold()
                    if "pupil_left" in metric_id:
                        controls[field] = coordinate.pupil_left_unit
                    elif "pupil_right" in metric_id:
                        controls[field] = coordinate.pupil_right_unit
                control = controls.get(field) if field else None
                page._coordinate_layout.setCurrentIndex(0)
                if control is not None:
                    control.setFocus(Qt.FocusReason.OtherFocusReason)
                    return True, f"已定位到导入元数据字段 {field}。"
                if field == "pupil_unit":
                    return False, "已打开坐标/单位声明；该指标没有唯一眼别，未选择左眼或右眼字段。"
                return False, f"已打开导入元数据页；当前界面没有可精确定位的字段 {field or '（未记录）'}。"

        if role is None:
            return False, f"已打开数据导入页；没有可信来源角色可定位字段 {field or '（未记录）'}。"
        panel = page.sources[role]
        index = page.source_tabs.indexOf(panel)
        if index >= 0:
            page.source_tabs.setCurrentIndex(index)
        if role == "aois":
            exact = False
            frame = panel.table_model.frame
            source_row = diagnostic.get("source_row")
            matches: list[int] = []
            if source_row is not None and "source_row" in frame.columns:
                matches = [row for row, value in enumerate(frame["source_row"].tolist())
                           if str(value) == str(source_row)]
            if not matches and target.aoi_id is not None and "aoi_id" in frame.columns:
                matches = [row for row, value in enumerate(frame["aoi_id"].tolist())
                           if str(value) == str(target.aoi_id)]
            if len(matches) == 1:
                panel.table_view.selectRow(matches[0])
                panel.table_view.scrollTo(panel.table_model.index(matches[0], 0))
                exact = True
            button = panel.aoi_editor_button
            button.setFocus(Qt.FocusReason.OtherFocusReason)
            if exact:
                selected_identity = target.aoi_id if target.aoi_id is not None else source_row
                return True, f"已打开当前 AOI 导入源并选中记录 {selected_identity}；旧运行图片不会自动绑定。"
            if matches:
                return False, "已打开当前 AOI 导入源；标识对应多行，未猜测具体行。"
            return False, "已打开当前 AOI 导入源及编辑入口；当前源中找不到唯一标识，旧运行图片不会自动绑定。"

        if role == "performance":
            widget = panel.performance_mapping
            if widget is not None and field:
                matches = [row for row in range(widget.table.rowCount())
                           if (widget.table.item(row, 0) is not None
                               and widget.table.item(row, 0).text() == field)
                           or (hasattr(widget.table.cellWidget(row, 1), "currentText")
                               and widget.table.cellWidget(row, 1).currentText() == field)]
                if len(matches) == 1:
                    row = matches[0]
                    widget.table.setCurrentCell(row, 1)
                    widget.table.scrollToItem(widget.table.item(row, 0))
                    widget.table.cellWidget(row, 1).setFocus(Qt.FocusReason.OtherFocusReason)
                    return True, f"已定位到绩效源字段 {field}。"
            return False, "已打开参与者/绩效来源；当前映射中没有该字段的唯一行。"

        if field and panel.mapping is not None and panel.mapping.focus_field(field):
            return True, f"已定位到 {role} 映射字段 {field}。"
        return False, f"已打开 {role} 来源映射；当前界面没有可精确定位的字段 {field or '（未记录）'}。"

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
            if self._batch_active:
                self._batch_cancel_requested()
                self.status_label.setText("已请求取消批量队列；等待当前条目结束并更新清单后关闭")
            else:
                self._request_timing_cancel()
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
        self._batch_start_pending_after_idle()
        self._maybe_finish_close()

    def _maybe_finish_close(self) -> None:
        if getattr(self, "_close_pending", False):
            if not (self.task_manager.active or self.operation_manager.active
                    or self.import_page.import_active or self._batch_active):
                self._close_pending = False
                self.close()

