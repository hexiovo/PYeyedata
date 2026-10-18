"""Run-scoped `.pyea` archive projection and preview helpers.

The functions here operate on one immutable GUI ``RunRecord``.  They do not
recompute analysis and keep results-only exclusions explicit for the user.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

import pandas as pd

from pyeyedata.app_info import APP_NAME, APP_VERSION, PRD_VERSION
from pyeyedata.gui.models import RunRecord

ArchiveMode = Literal["full", "results_only"]

_RESULTS_ONLY_EXACT_EXCLUSIONS = frozenset({"processed_signals", "analysis_streams"})
_RESULTS_ONLY_EXCLUDED_PREFIXES = ("input_",)

_CONTEXT_CLASSIFICATIONS: dict[str, tuple[str, bool, str]] = {
    "external_events": (
        "analysis_context", True,
        "具名外部事件上下文；保留为只读审计资料，不据此重新运行分析。",
    ),
    "events": (
        "analysis_context", True,
        "具名事件上下文；保留为只读审计资料，不据此重新运行分析。",
    ),
    "target_movement_intervals": (
        "analysis_context", True,
        "具名目标运动区间；保留为结果解释所需的上下文，不声称可重算。",
    ),
    "eyelid_intervals": (
        "analysis_context", True,
        "具名眼睑区间；保留为只读审计资料，不据此重新运行分析。",
    ),
    "reference_fixations": (
        "named_reference", True,
        "具名参考注视表；保留用于解释本次结果，不据此重新运行分析。",
    ),
    "reference_trajectory": (
        "named_reference", True,
        "具名参考轨迹表；保留用于解释本次结果，不据此重新运行分析。",
    ),
}


class ArchiveSessionError(ValueError):
    """A run cannot be represented by the requested archive projection."""


@dataclass(frozen=True, slots=True)
class ArchiveInventoryItem:
    """One named piece of data and its explicit archive decision."""

    name: str
    category: str
    action: Literal["include", "exclude"]
    reason: str
    row_count: int | None = None
    sha256: str | None = None
    byte_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "action": self.action,
            "reason": self.reason,
            "row_count": self.row_count,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True, slots=True)
class ArchivePreview:
    """A pre-save named inventory for one selected run and archive mode."""

    run_id: str
    status: str
    mode: ArchiveMode
    items: tuple[ArchiveInventoryItem, ...]
    recomputable: bool
    completeness_reasons: tuple[str, ...] = ()

    @property
    def included(self) -> tuple[ArchiveInventoryItem, ...]:
        return tuple(item for item in self.items if item.action == "include")

    @property
    def excluded(self) -> tuple[ArchiveInventoryItem, ...]:
        return tuple(item for item in self.items if item.action == "exclude")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "mode": self.mode,
            "recomputable": self.recomputable,
            "items": [item.to_dict() for item in self.items],
            "completeness_reasons": list(self.completeness_reasons),
        }

    def format_text(self) -> str:
        title = "完整档案" if self.mode == "full" else "仅结果档案"
        lines = [
            f"{title} · 运行 {self.run_id} · 状态 {self.status}",
            "可由真实输入和配置重建" if self.recomputable else "只读查看已保存结果；不能重新计算",
            "",
            "将保存：",
        ]
        for item in self.included:
            suffix = f"（{item.row_count:,} 行）" if item.row_count is not None else ""
            if item.byte_size is not None:
                suffix += f" [{item.byte_size:,} bytes]"
            lines.append(f"  + {item.name}{suffix} — {item.category}：{item.reason}")
        if self.excluded:
            lines.extend(("", "不会保存："))
            for item in self.excluded:
                suffix = f"（{item.row_count:,} 行）" if item.row_count is not None else ""
                lines.append(f"  - {item.name}{suffix} — {item.category}：{item.reason}")
        lines.extend(self.completeness_reasons)
        return "\n".join(lines)


def project_result_tables(
    tables: Mapping[str, pd.DataFrame], mode: ArchiveMode
) -> tuple[dict[str, pd.DataFrame], tuple[ArchiveInventoryItem, ...]]:
    """Return detached result tables and a named include/exclude inventory."""
    if mode not in {"full", "results_only"}:
        raise ArchiveSessionError("Archive mode must be 'full' or 'results_only'.")
    if not isinstance(tables, Mapping):
        raise ArchiveSessionError("RunRecord tables must be a mapping.")
    projected: dict[str, pd.DataFrame] = {}
    inventory: list[ArchiveInventoryItem] = []
    for raw_name, frame in tables.items():
        name = str(raw_name)
        if not isinstance(frame, pd.DataFrame):
            raise ArchiveSessionError(f"RunRecord table {name!r} is not a DataFrame.")
        excluded = mode == "results_only" and (
            name.casefold() in _RESULTS_ONLY_EXACT_EXCLUSIONS
            or name.casefold().startswith(_RESULTS_ONLY_EXCLUDED_PREFIXES)
        )
        if excluded:
            inventory.append(ArchiveInventoryItem(
                name=name,
                category="derived_input_or_stream",
                action="exclude",
                reason="仅结果模式不保存输入副本、processed_signals 或 analysis_streams。",
                row_count=len(frame),
            ))
            continue
        category, reason = _classify_result_table(name)
        projected[name] = frame.copy(deep=True)
        inventory.append(ArchiveInventoryItem(
            name=name,
            category=category,
            action="include",
            reason=(
                reason if mode == "full"
                else reason + "仅作为本次运行的静态结果保留，不据此重算。"
            ),
            row_count=len(frame),
        ))
    return projected, tuple(inventory)


def _classify_result_table(name: str) -> tuple[str, str]:
    normalized = name.casefold()
    if normalized == "metrics":
        return "metric_results", "本次 run 的指标值、状态、单位和样本支持。"
    if normalized.startswith("statistics") or normalized in {"descriptives", "diagnostics"}:
        return "statistical_results", "本次 run 实际返回的统计或描述结果。"
    if normalized in {"availability", "quality", "processing_audit", "processing_partitions"}:
        return "quality_and_availability", "本次 run 的质量/可用性/处理审计表。"
    if normalized.startswith(("individual_", "advanced_", "group_")) or normalized in {
        "events", "source_events", "saccade_details", "scanpath_audit", "heatmap_grid",
        "analysis_memberships", "segment_overlap", "segment_scope_audit", "event_interval_audit",
        "label_derivations",
    }:
        return "run_detail", "本次 run 返回的具名审计或模块明细表。"
    return (
        "unclassified_run_table",
        "未映射到已知表类别；按原表名保留为只读 run 输出，不推断其用途。",
    )


def _classify_context(name: str, value: Any, mode: ArchiveMode) -> tuple[str, bool, str]:
    if name in _CONTEXT_CLASSIFICATIONS:
        category, allowed, reason = _CONTEXT_CLASSIFICATIONS[name]
        return category, allowed, reason
    if isinstance(value, pd.DataFrame):
        if mode == "full":
            return (
                "unclassified_context", True,
                "未映射到已知 context 类型；仅在 full 档案中按原名保留，未推断语义。",
            )
        return (
            "unclassified_context", False,
            "未映射到已知 context 类型；results_only 不复制未分类上下文，可改用 full 档案。",
        )
    return (
        "context_configuration", True,
        "具名运行上下文配置；保存实际声明并注明原字段名，不据此推断数据语义。",
    )


__all__ = [
    "ArchiveInventoryItem", "ArchiveMode", "ArchivePreview", "ArchiveSessionError",
    "build_archive_preview", "project_result_tables",
]


APP_ARCHIVE_SCHEMA = "pyeyedata.run-archive/v1"
_SNAPSHOT_KEY = "app_archive_snapshot"


@dataclass(frozen=True, slots=True)
class LoadedRunArchive:
    analysis: Any
    record: RunRecord
    app_archive: Mapping[str, Any]
    archive_mode: str


@dataclass
class PreparedRunArchive:
    """Private validated stage with an exact inventory, awaiting user save."""

    path: Path
    preview: ArchivePreview
    _temporary: Any

    def close(self) -> None:
        self._temporary.cleanup()

    def commit(self, path: str | os.PathLike[str], *, cancel=None, progress=None):
        from pyeyedata.store import read_pyea, write_pyea_extension
        _check_cancel(cancel)
        # The extension writer streams each member, validates its final sibling
        # stage and checks cancellation immediately before atomic replacement.
        result = write_pyea_extension(self.path, path, {}, metadata_update={},
                                      cancel=cancel, progress=progress)
        return result


def _check_cancel(cancel) -> None:
    if cancel and cancel():
        from pyeyedata.store import ArchiveError
        raise ArchiveError("Archive operation cancelled; destination and current view are unchanged")


def _frozen_build_identity():
    from pyeyedata.build_identity import load_build_identity
    from pyeyedata.contracts import CONTRACT_VERSION
    from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION

    return load_build_identity(
        application={"name": APP_NAME, "version": APP_VERSION, "prd_version": PRD_VERSION},
        contracts={"data_contract_version": CONTRACT_VERSION,
                   "pipeline_schema_version": PIPELINE_SCHEMA_VERSION},
    )


def _runtime_versions() -> dict[str, Any]:
    versions = {}
    for name in ("numpy", "pandas", "scipy", "matplotlib", "PySide6-Essentials",
                 "reportlab", "openpyxl", "fonttools", "pyeyedata"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = {"status": "not-installed-as-distribution"}
    identity = None
    if getattr(sys, "frozen", False):
        from pyeyedata.build_identity import BuildIdentityError
        try:
            identity = _frozen_build_identity()
        except BuildIdentityError as exc:
            raise ArchiveSessionError(str(exc)) from exc
        source_hashes = identity.application_source_sha256()
        version_document_sha256 = identity.version_document_sha256
    else:
        package = Path(__file__).resolve().parent
        app_sources = [Path(__file__), package / "app_info.py", *(package / "gui").glob("*.py")]
        source_hashes = {str(path.relative_to(package)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in app_sources}
        version_document = package.parent / "version.md"
        version_document_sha256 = hashlib.sha256(version_document.read_bytes()).hexdigest() if version_document.is_file() else None
    from pyeyedata.contracts import CONTRACT_VERSION
    from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION

    runtime = {"application_name": APP_NAME, "application_version": APP_VERSION,
            "prd_version": PRD_VERSION, "data_contract_version": CONTRACT_VERSION,
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "python": sys.version, "platform": platform.platform(),
            "architecture": platform.machine(), "distributions": versions,
            "application_source_sha256": source_hashes,
            "version_document_sha256": version_document_sha256}
    if identity is not None:
        runtime["build_identity"] = identity.archive_metadata()
    return runtime


def _context_decision(name, value, mode, details):
    if mode == "full":
        return _classify_context(name, value, mode)
    if name in {"reference_fixations", "reference_trajectory", "target_movement_intervals"}:
        return "named_reference", True, "具名方法参考，保留本次结果解释依据；不能完整重算。"
    if name in {"external_events", "events", "eyelid_intervals"}:
        parameters = details.get("parameters", {})
        selected = details.get("selection", {})
        ids = [str(v) for k, values in selected.items() if k.endswith("metric_ids") for v in values]
        event_settings = parameters.get("event_detection", {})
        if hasattr(event_settings, "__dict__"):
            event_settings = vars(event_settings)
        method = " ".join(str(event_settings.get(key, "")) for key in ("method", "source", "fixation_method", "saccade_method")) if isinstance(event_settings, Mapping) else ""
        relevant = any(any(token in item for token in ("fixation", "saccade", "scanpath", "multimatch", "scanmatch", "blink", "aoi", "entropy")) for item in ids)
        if name == "eyelid_intervals":
            relevant = relevant or any("pupil" in item or "blink" in item for item in ids)
        keep = relevant or "external" in method or (isinstance(event_settings, Mapping) and event_settings.get("side_by_side_external") is True)
        return "selected_event_context", keep, ("所选方法或事件配置依赖的具名事件审计。" if keep else "所选方法和事件配置没有声明使用此事件上下文；仅结果模式省略。")
    return _classify_context(name, value, mode)


def _project_context(value, mode, details, decisions, prefix="context"):
    if not isinstance(value, Mapping):
        return value
    output = {}
    for name, item in value.items():
        category, include, reason = _context_decision(str(name), item, mode, details)
        decisions.append(ArchiveInventoryItem(f"{prefix}/{name}", category,
                                             "include" if include else "exclude", reason,
                                             len(item) if isinstance(item, pd.DataFrame) else None))
        if include:
            output[name] = item.copy(deep=True) if isinstance(item, pd.DataFrame) else item
    return output


def _completeness(record, mode, raw_sources):
    from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION
    reasons = []
    if mode != "full":
        return False, ("仅结果：未包含原始数据集和源文件，不能完整重算。",)
    details = record.snapshot_details()
    hashes = details.get("source_hashes", {})
    if not hashes:
        reasons.append("原始来源哈希清单未记录，不能证明原始资料完整。")
    for name, expected in hashes.items():
        payload = raw_sources.get(name)
        if payload is None and record.snapshot is not None:
            for source in record.snapshot.dataset_copy().source_manifest:
                if source.get("source_name") == name and source.get("sha256") == expected:
                    payload = raw_sources.get(source.get("raw_source_key"))
                    if payload is not None:
                        break
        if payload is None:
            reasons.append(f"缺少原始来源：{name}（声明 SHA-256 {expected}）。")
        elif hashlib.sha256(payload).hexdigest() != expected:
            reasons.append(f"原始来源哈希不一致：{name}。")
    metadata = record.metadata()
    prior_archive = metadata.get("app_archive", {})
    if isinstance(prior_archive, Mapping) and prior_archive.get("current_source_differences"):
        reasons.append("所选历史档案与当前软件源码存在版本差异；未验证跨版本完整重算。")
    if record.snapshot and record.snapshot.dataset_copy().metadata.get("aoi_import_status") in {"invalid_draft", "pending_draft"}:
        reasons.append("AOI 草稿尚未验证，依赖 AOI 的方法不能完整重算；主要信号仍可使用。")
    for location in (metadata, details.get("parameters", {}).get("metadata", {})):
        if not isinstance(location, Mapping):
            continue
        for key in ("required_resources", "missing_dependencies", "missing_resources"):
            resources = location.get(key, [])
            if isinstance(resources, Mapping):
                resources = [{"name": k, **(dict(v) if isinstance(v, Mapping) else {"reason": str(v)})} for k, v in resources.items()]
            for resource in resources if isinstance(resources, (list, tuple)) else [resources]:
                reasons.append(f"外部资源尚未嵌入验证：{resource}")
    if not record.snapshot or not details.get("schema_version"):
        reasons.append("缺少已验证数据或保存的算法合同版本。")
    elif details["schema_version"] != PIPELINE_SCHEMA_VERSION:
        reasons.append(f"保存的算法合同 {details['schema_version']} 与当前支持的 {PIPELINE_SCHEMA_VERSION} 不同，未验证当前版本重算资格。")
    return not reasons, tuple(reasons)


def prepare_run_archive(record: RunRecord, *, mode: ArchiveMode = "full", cancel=None,
                        progress=None, static_artifacts=(), static_omissions=()) -> PreparedRunArchive:
    """Serialize one immutable run privately; expose actual names/sizes before commit."""
    from pyeyedata.contracts import AnalysisBundle
    from pyeyedata.gui.bridge import PipelineBridge
    from pyeyedata.pipeline import AnalysisConfig, load_analysis, save_analysis
    from pyeyedata.reporting import build_archive_artifacts
    from pyeyedata.store import read_pyea, write_pyea_extension
    if not isinstance(record, RunRecord):
        raise ArchiveSessionError("Select one immutable RunRecord")
    if mode not in {"full", "results_only"}:
        raise ArchiveSessionError("Archive mode must be full or results_only")
    if mode == "full" and record.snapshot is None:
        raise ArchiveSessionError("No embedded validated dataset; full archive is unavailable")
    _check_cancel(cancel)
    temporary = tempfile.TemporaryDirectory(prefix="pyeyedata-archive-")
    try:
        directory = Path(temporary.name)
        # Fetch included tables individually: results_only must not first copy
        # all excluded million-row input/sample tables just to discard them.
        tables = {}
        decisions = []
        for name in record.table_names():
            excluded = mode == "results_only" and (name.casefold().startswith(_RESULTS_ONLY_EXCLUDED_PREFIXES)
                                                    or name.casefold() in _RESULTS_ONLY_EXACT_EXCLUSIONS)
            if excluded:
                decisions.append(ArchiveInventoryItem(name, "derived_input_or_stream", "exclude",
                    "仅结果模式省略原输入副本和完整采样流；不会为预览复制整表。"))
                continue
            tables[name] = record.table_copy(name)
            category, reason = _classify_result_table(name)
            decisions.append(ArchiveInventoryItem(name, category, "include", reason, len(tables[name])))
        details = dict(record.snapshot_details())
        raw_sources = details.pop("raw_sources", {}) if mode == "full" else {}
        details.pop("raw_sources", None)
        details["context"] = _project_context(details.get("context", {}), mode, details, decisions)
        parameters = dict(details.get("parameters", {}))
        if "context" in parameters:
            parameters["context"] = _project_context(parameters["context"], mode, details, decisions, "parameters/context")
        details["parameters"] = parameters
        dataset = record.snapshot.dataset_copy() if mode == "full" else None
        if record.snapshot is not None:
            config = PipelineBridge().build_config(record.snapshot)
            config = replace(config, context=_project_context(config.context, mode, details, [], "config/context"))
        else:
            # Re-saving detached results does not manufacture scientific defaults.
            # Only the typed app snapshot is carried in the public config codec.
            config = None
        snapshot_metadata = {"details": details}
        if record.snapshot is not None:
            revision = record.snapshot.dataset_revision
            snapshot_metadata.update(snapshot_created_at=record.snapshot.created_at,
                                     dataset_created_at=revision.created_at,
                                     dataset_fingerprint=revision.fingerprint)
        if config is not None:
            config = replace(config, metadata={**dict(config.metadata), _SNAPSHOT_KEY: snapshot_metadata})
        baseline = directory / "baseline.pyea"
        result_metadata = dict(record.metadata())
        if config is None:
            # No defaults are invented when a legacy/detached archive lacks a
            # reconstructible scientific config. The public serializer accepts
            # a saved parameter snapshot directly in this case.
            result_metadata["parameter_snapshot"] = {"metadata": {_SNAPSHOT_KEY: snapshot_metadata},
                                                      "scientific_config_status": "not-recorded"}
        bundle = AnalysisBundle(tables, result_metadata, list(record.logs()))
        serialized_raw = {f"source-{index:04d}-{hashlib.sha256(str(name).encode('utf-8')).hexdigest()[:16]}{Path(str(name)).suffix}": payload
                          for index, (name, payload) in enumerate(raw_sources.items())}
        if progress:
            progress(0.05, "序列化所选运行的 typed 表与配置")
        save_analysis(baseline, bundle, dataset=dataset, config=config, mode=mode,
                      raw_sources=serialized_raw, cancel=cancel)
        base = read_pyea(baseline)
        _check_cancel(cancel)
        if progress:
            progress(0.35, "生成本次运行的静态图和 PDF 报告")
        artifact_record = RunRecord(
            **{key: getattr(record, key) for key in ("run_id", "generation", "revision_id", "config_fingerprint", "phase", "status", "created_at")},
            _metadata=MappingProxyType({**result_metadata, "archive_mode": mode, "archive_note": "仅结果：不能完整重算" if mode == "results_only" else "完整档案；完整性按实际资源清单判断"}),
            _tables=MappingProxyType(tables), _logs=record.logs(), _archived_details=MappingProxyType(details))
        artifacts = build_archive_artifacts(artifact_record)
        for name, reason in static_omissions:
            decisions.append(ArchiveInventoryItem(str(name), "current_view_figure", "exclude", str(reason)))
        extra = {}
        artifact_inventory = []
        for item in (*artifacts.artifacts, *static_artifacts):
            if item.member_name in extra:
                raise ArchiveSessionError(f"Duplicate static artifact: {item.member_name}")
            extra[item.member_name] = item.data
            artifact_inventory.append({"member": item.member_name, "media_type": item.media_type,
                                       "size": item.byte_length, "sha256": item.sha256,
                                       "provenance": dict(item.provenance)})
        recomputable, reasons = _completeness(record, mode, raw_sources)
        actual_inventory = []
        for name, info in base.members.items():
            role = "typed_input" if "input." in name else "raw_source" if name.startswith("raw_sources/") else "typed_result_or_config"
            actual_inventory.append({"member": name, "role": role, "size": info.size, "sha256": info.sha256})
        actual_inventory.extend({"member": item["member"], "role": "static_artifact",
                                 "size": item["size"], "sha256": item["sha256"]} for item in artifact_inventory)
        namespace = {
            "schema": APP_ARCHIVE_SCHEMA, "mode": mode,
            "run": {key: getattr(record, key) for key in ("run_id", "generation", "revision_id", "config_fingerprint", "phase", "status", "created_at")},
            "snapshot_config_key": _SNAPSHOT_KEY,
            "runtime": _runtime_versions(), "recomputable": recomputable,
            "completeness_reasons": list(reasons),
            "projection": [item.to_dict() for item in decisions],
            "inventory": actual_inventory, "artifacts": artifact_inventory,
            "artifact_omissions": [*artifacts.omissions, *[f"{name}: {reason}" for name, reason in static_omissions]],
            "raw_source_bindings": [{"original_name": str(name), "member": item["member"]}
                                    for name, item in zip(raw_sources, base.manifest["metadata"]["raw_sources"])],
        }
        final = directory / "prepared.pyea"
        write_pyea_extension(baseline, final, extra,
                             metadata_update={"app_archive": namespace, "recomputable": recomputable},
                             cancel=cancel)
        loaded = load_run_archive(final, cancel=cancel)
        final_archive = loaded.analysis.archive
        items = [ArchiveInventoryItem(name, next((v["role"] for v in actual_inventory if v["member"] == name), "archive_member"),
                                      "include", "已序列化并校验的实际成员。", sha256=info.sha256,
                                      byte_size=info.size) for name, info in final_archive.members.items()]
        items.extend(item for item in decisions if item.action == "exclude")
        import zipfile
        with zipfile.ZipFile(final) as zip_archive:
            manifest_size = zip_archive.getinfo("manifest.json").file_size
        items.append(ArchiveInventoryItem("manifest.json", "integrity_manifest", "include", "生成的成员、schema、哈希和应用清单。",
                                          byte_size=manifest_size))
        if mode == "results_only":
            for name, payload in record.snapshot_details().get("raw_sources", {}).items():
                items.append(ArchiveInventoryItem(str(name), "original_source_bytes", "exclude", "仅结果模式不保存原始源文件。", byte_size=len(payload)))
        preview = ArchivePreview(record.run_id, record.status, mode, tuple(items), recomputable, reasons)
        if progress:
            progress(1.0, "保存预览已就绪；尚未改变目标文件")
        return PreparedRunArchive(final, preview, temporary)
    except BaseException:
        temporary.cleanup()
        raise


def preview_run_archive(record: RunRecord, *, mode: ArchiveMode = "full") -> ArchivePreview:
    prepared = prepare_run_archive(record, mode=mode)
    try:
        return prepared.preview
    finally:
        prepared.close()


def save_run_archive(path, record: RunRecord, *, mode: ArchiveMode = "full", cancel=None, progress=None):
    prepared = prepare_run_archive(record, mode=mode, cancel=cancel, progress=progress)
    try:
        return prepared.commit(path, cancel=cancel, progress=progress)
    finally:
        prepared.close()


def load_run_archive(path, *, cancel=None, progress=None) -> LoadedRunArchive:
    """Restore detached history from embedded facts; no source-path access/conversion."""
    from pyeyedata.gui.models import DatasetRevision, RunSnapshot, SelectionSpec, _freeze
    from pyeyedata.pipeline import load_analysis
    from pyeyedata.store import ArchiveError
    _check_cancel(cancel)
    if progress:
        progress(0.05, "验证并读取档案（取消后会丢弃迟到结果）")
    analysis = load_analysis(path)
    _check_cancel(cancel)
    archive = analysis.archive
    app = archive.manifest.get("metadata", {}).get("app_archive")
    snapshot = None
    details = {}
    if app is None:
        app = {"schema": "legacy-not-recorded", "mode": archive.mode,
               "recomputable": False, "completeness_reasons": ["旧档案未记录应用快照、选择和依赖事实；只读查看。"]}
        identity = {"run_id": archive.manifest["metadata"].get("run_id") or "",
                    "generation": 0, "revision_id": "", "config_fingerprint": "",
                    "phase": "legacy-not-recorded", "status": analysis.result.metadata.get("execution_status", "unknown"),
                    "created_at": ""}
    else:
        if not isinstance(app, dict) or app.get("schema") != APP_ARCHIVE_SCHEMA or app.get("mode") != archive.mode:
            raise ArchiveError("Invalid or unsupported application archive namespace")
        identity = app.get("run")
        required = {"run_id", "generation", "revision_id", "config_fingerprint", "phase", "status", "created_at"}
        if not isinstance(identity, dict) or set(identity) != required or isinstance(identity["generation"], bool) or not isinstance(identity["generation"], int):
            raise ArchiveError("Invalid application run identity")
        if any(not isinstance(identity[k], str) for k in required - {"generation"}):
            raise ArchiveError("Invalid application run identity field")
        inventory = app.get("inventory")
        if not isinstance(inventory, list) or len(inventory) != len(archive.members) or {item.get("member") for item in inventory if isinstance(item, dict)} != set(archive.members):
            raise ArchiveError("Application member inventory does not match the archive")
        for item in inventory:
            if not isinstance(item, dict) or item.get("member") not in archive.members:
                raise ArchiveError("Invalid application member inventory entry")
            info = archive.members[item["member"]]
            if info.sha256 != item.get("sha256") or info.size != item.get("size"):
                raise ArchiveError("Application inventory hash/size mismatch")
        artifacts = app.get("artifacts")
        if not isinstance(artifacts, list):
            raise ArchiveError("Invalid static artifact inventory")
        seen_artifacts = set()
        for item in artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("member"), str):
                raise ArchiveError("Invalid static artifact link")
            name = item["member"]
            if not name.startswith("app_archive/") or name not in archive.members or name in seen_artifacts:
                raise ArchiveError("Missing or duplicate static artifact link")
            seen_artifacts.add(name)
            if item.get("size") != archive.members[name].size or item.get("sha256") != archive.members[name].sha256:
                raise ArchiveError("Static artifact hash/size mismatch")
            provenance = item.get("provenance")
            if not isinstance(provenance, dict) or any(provenance.get(key) != identity[key] for key in ("run_id", "revision_id", "config_fingerprint")):
                raise ArchiveError("Static artifact run identity mismatch")
        state = analysis.config.get("metadata", {}).get(_SNAPSHOT_KEY)
        if not isinstance(state, dict) or not isinstance(state.get("details"), dict):
            raise ArchiveError("Missing typed application snapshot")
        details = state["details"]
        if archive.mode == "results_only":
            if analysis.dataset is not None or analysis.raw_sources or any(name.startswith("input_") or name in _RESULTS_ONLY_EXACT_EXCLUSIONS for name in analysis.result.tables):
                raise ArchiveError("Results-only archive contains forbidden input data")
        elif analysis.dataset is not None:
            raw = {}
            for binding in app.get("raw_source_bindings", []):
                if not isinstance(binding, dict) or binding.get("member") not in archive.members:
                    raise ArchiveError("Invalid original-source binding")
                raw[binding["original_name"]] = archive.read_member(binding["member"])
            revision = DatasetRevision.create(analysis.dataset, raw_sources=raw, revision_id=identity["revision_id"])
            dataset_identity_matches = revision.fingerprint == state.get("dataset_fingerprint")
            legacy_missing_codec = any(info.schema and info.schema.get("encoding_version", 1) in {1, 2}
                                       and any(column.get("dtype") == "object" for column in info.schema.get("columns", []))
                                       for info in archive.members.values())
            if not dataset_identity_matches and not legacy_missing_codec:
                raise ArchiveError("Embedded dataset fingerprint differs from saved run")
            revision = replace(revision, created_at=state["dataset_created_at"], source_hashes=MappingProxyType(dict(details.get("source_hashes", {}))))
            snapshot = RunSnapshot(revision, identity["generation"], identity["run_id"],
                                   SelectionSpec(**details["selection"]), details["schema_version"],
                                   identity["config_fingerprint"], state["snapshot_created_at"],
                                   _freeze(details["parameters"]), _freeze(details["context"]))
            validated_snapshot = RunSnapshot.create(revision, generation=snapshot.generation,
                selection=snapshot.selection, parameters=snapshot.parameter_values(), context=snapshot.context_values(),
                schema_version=snapshot.schema_version, run_id=snapshot.run_id)
            if not dataset_identity_matches or validated_snapshot.config_fingerprint != snapshot.config_fingerprint:
                if not legacy_missing_codec:
                    raise ArchiveError("Embedded configuration fingerprint differs from saved run")
                # Older object codecs merged None/NaT/NaN into pd.NA. The
                # scientific tables remain valid for review, but exact original
                # identity cannot be recovered by inventing a new fingerprint.
                snapshot = None
                app = {**app, "saved_recomputable": app.get("recomputable"), "recomputable": False,
                       "snapshot_restore_status": "legacy-object-null-identity-unavailable",
                       "completeness_reasons": [*app.get("completeness_reasons", []),
                           "旧 object 缺失值编码不能恢复原始缺失类型/快照指纹；保留原版本和指纹，只读查看表图，不能按该快照重算。"]}
        for key in ("run_id", "generation", "revision_id", "config_fingerprint"):
            if key in details and details[key] != identity[key]:
                raise ArchiveError("Typed snapshot identity differs from run identity")
    record_metadata = dict(analysis.result.metadata)
    record_metadata["app_archive"] = app
    record = RunRecord.from_archive(**identity, metadata=record_metadata, tables=analysis.result.tables,
                                    logs=analysis.result.logs, snapshot=snapshot, snapshot_details=details)
    if app.get("schema") == APP_ARCHIVE_SCHEMA and app.get("recomputable") is True:
        eligible, reasons = _completeness(record, archive.mode, snapshot.dataset_revision.raw_source_bytes() if snapshot else {})
        if not eligible:
            from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION
            if snapshot and snapshot.schema_version != PIPELINE_SCHEMA_VERSION:
                app = {**app, "saved_recomputable": app.get("recomputable"), "recomputable": False,
                       "completeness_reasons": [*app.get("completeness_reasons", []), *reasons]}
            else:
                raise ArchiveError("Archive claims complete recomputation despite missing dependencies: " + "; ".join(reasons))
    saved_sources = archive.manifest.get("metadata", {}).get("software_sha256", {})
    if not isinstance(saved_sources, Mapping):
        raise ArchiveError("Invalid saved software source inventory")
    if getattr(sys, "frozen", False):
        from pyeyedata.build_identity import BuildIdentityError
        try:
            build_identity = _frozen_build_identity()
        except BuildIdentityError as exc:
            current_sources = None
            app = {**app, "saved_recomputable": app.get("saved_recomputable", app.get("recomputable")),
                   "recomputable": False, "current_source_identity_status": "unavailable",
                   "current_source_identity_reason": str(exc),
                   "completeness_reasons": [*app.get("completeness_reasons", []),
                       "当前冻结程序的构建身份无法验证；保留原版本只读查看，不能声明当前版本可复算：" + str(exc)]}
        else:
            current_sources = build_identity.software_sha256()
            app = {**app, "current_source_identity_status": "available",
                   "current_source_identity_origin": "embedded-build-manifest",
                   "current_build_manifest_sha256": build_identity.manifest_sha256}
    else:
        package = Path(__file__).resolve().parent
        current_sources = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (*package.glob("*.py"), *(package / "methods").glob("*.py"))}
    changed_sources = ([name for name, expected in saved_sources.items() if current_sources.get(name) != expected]
                       if current_sources is not None else [])
    if changed_sources:
        app = {**app, "saved_recomputable": app.get("saved_recomputable", app.get("recomputable")),
               "recomputable": False, "current_source_differences": changed_sources,
               "completeness_reasons": [*app.get("completeness_reasons", []),
                   "当前软件源码与档案记录存在差异；按原版本只读查看，未验证跨版本重算：" + ", ".join(changed_sources)]}
    if record_metadata["app_archive"] is not app:
        record_metadata["app_archive"] = app
        record = RunRecord.from_archive(**identity, metadata=record_metadata, tables=analysis.result.tables,
                                        logs=analysis.result.logs, snapshot=snapshot, snapshot_details=details)
    _check_cancel(cancel)
    if progress:
        progress(1.0, "档案已验证；保存的原始参数与运行状态已恢复为只读记录")
    return LoadedRunArchive(analysis, record, MappingProxyType(app), archive.mode)


# Compatibility spelling now uses the same exact inventory as the save path.
def build_archive_preview(record: RunRecord, mode: ArchiveMode) -> ArchivePreview:
    return preview_run_archive(record, mode=mode)


__all__ += ["APP_ARCHIVE_SCHEMA", "LoadedRunArchive", "PreparedRunArchive", "prepare_run_archive",
            "preview_run_archive", "save_run_archive", "load_run_archive"]
