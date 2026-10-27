"""Exclusive per-attempt output storage for batch runs.

The host owns queue state and worker scheduling. This module only validates and
reserves paths, exports immutable RunRecords, and atomically writes a manifest.
"""
from __future__ import annotations

from dataclasses import dataclass, is_dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .batch_queue import BATCH_MANIFEST_SCHEMA
from .models import RunRecord


CancelCallback = Callable[[], bool]
ProgressCallback = Callable[[float, str], None]
_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")
_ATTEMPT_TOKEN = re.compile(r"^a([1-9][0-9]*)-e([1-9][0-9]*)$")
_RESULTS_ONLY_EXCLUDED = frozenset({"processed_signals", "analysis_streams"})


class BatchOutputError(RuntimeError):
    """A batch output could not be created without risking prior output."""


class BatchOutputFailure(BatchOutputError):
    """An export failed; already committed files remain available for audit."""

    def __init__(self, message: str, *, committed_paths: Sequence[Path] = ()) -> None:
        super().__init__(message)
        self.committed_paths = tuple(Path(path) for path in committed_paths)


@dataclass(frozen=True, slots=True)
class BatchExportResult:
    """Terminal receipt; cancelled exports retain every file committed so far."""

    run_id: str
    attempt_dir: Path
    archive_path: Path
    table_paths: tuple[Path, ...]
    output_paths: tuple[Path, ...]
    output_sha256: Mapping[str, str]
    cancelled: bool = False
    complete: bool = True

    @property
    def commit_complete(self) -> bool:
        """The terminal receipt is ready for the host to record.

        The complete flag distinguishes all-files export from cancellation
        after zero or more atomic file commits.
        """
        return True


def _cancelled(cancel_requested: CancelCallback | None) -> bool:
    return bool(cancel_requested is not None and cancel_requested())


def _stage_progress(
    callback: ProgressCallback | None, start: float, width: float,
) -> ProgressCallback | None:
    if callback is None:
        return None

    def report(fraction: float, message: str) -> None:
        value = min(1.0, max(0.0, float(fraction)))
        callback(start + width * value, str(message))

    return report


def _safe_component(value: Any, *, fallback: str) -> str:
    text = _SAFE_PART.sub("_", str(value).strip()).strip("._-")
    return text[:80] or fallback


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        return value
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _manifest_item(value: Any, recorded_at: str) -> Any:
    item = _json_safe(value)
    if isinstance(item, dict):
        item.setdefault("manifest_observed_at", recorded_at)
    return item


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class BatchOutputStore:
    """Reserve a unique queue directory and export immutable results into it."""

    def __init__(self, output_root: str | os.PathLike[str]) -> None:
        self.output_root = Path(output_root).expanduser().absolute()
        self._queue_dir: Path | None = None
        self._reserved: set[Path] = set()

    @property
    def queue_directory(self) -> Path | None:
        return self._queue_dir

    def validate_root(self) -> Path:
        """Create the selected output root if needed and verify real write access."""
        root = self.output_root
        if root.exists() and not root.is_dir():
            raise BatchOutputError(f"批量输出位置不是文件夹：{root}")
        try:
            root.mkdir(parents=True, exist_ok=True)
            fd, probe_name = tempfile.mkstemp(prefix=".pyeyedata-write-test-", dir=root)
            os.close(fd)
            Path(probe_name).unlink(missing_ok=True)
        except OSError as exc:
            raise BatchOutputError(f"批量输出文件夹不可写：{root}（{type(exc).__name__}: {exc}）") from exc
        return root

    def _ensure_queue_dir(self) -> Path:
        if self._queue_dir is not None:
            return self._queue_dir
        root = self.validate_root()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for _ in range(20):
            candidate = root / f"batch-{stamp}-{uuid4().hex[:10]}"
            try:
                candidate.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            self._queue_dir = candidate
            return candidate
        raise BatchOutputError("无法保留唯一的批量输出目录。")

    def reserve_attempt_dir(
        self,
        source_name: str | os.PathLike[str],
        item_id: str,
        attempt: str | int,
    ) -> Path:
        """Create a unique item/analysis-export attempt directory; never reuse one."""
        if isinstance(attempt, bool):
            raise ValueError("attempt must be a safe token such as 'a1-e1'.")
        token = f"a{attempt}-e1" if isinstance(attempt, int) and attempt > 0 else str(attempt)
        if not _ATTEMPT_TOKEN.fullmatch(token):
            raise ValueError("attempt must be a safe token such as 'a1-e2'.")
        source_stem = Path(str(source_name)).stem or "source"
        item_component = _safe_component(item_id, fallback="item")
        source_component = _safe_component(source_stem, fallback="source")
        queue_dir = self._ensure_queue_dir()
        item_dir = queue_dir / "items" / f"{item_component}-{source_component}"
        attempts_dir = item_dir / "attempts"
        attempts_dir.mkdir(parents=True, exist_ok=True)
        target = attempts_dir / token
        target.mkdir(exist_ok=False)
        self._reserved.add(target.resolve())
        return target

    def export_record(
        self,
        record: RunRecord,
        attempt_dir: str | os.PathLike[str],
        *,
        cancel_requested: CancelCallback | None = None,
        progress: ProgressCallback | None = None,
    ) -> BatchExportResult:
        """Save results-only .pyea plus each included result table using CSV audit schema.

        File commits are atomic at the archive or table-output-group boundary.
        Cancellation between boundaries returns a cancelled terminal receipt
        carrying paths for any files that were already committed. Optional
        progress reports the archive phase and each table's real export phase.
        """
        if not isinstance(record, RunRecord):
            raise TypeError("record must be an immutable RunRecord")
        if progress is not None and not callable(progress):
            raise TypeError("progress must be callable or None")
        directory = Path(attempt_dir).expanduser().absolute()
        queue_dir = self._ensure_queue_dir().resolve()
        resolved = directory.resolve()
        if resolved not in self._reserved or not resolved.is_relative_to(queue_dir):
            raise BatchOutputError("Export path was not reserved by this BatchOutputStore.")
        if not directory.is_dir():
            raise BatchOutputError("Reserved batch attempt folder no longer exists.")
        if any(directory.iterdir()):
            raise BatchOutputError("Refusing to reuse a batch attempt folder that already contains files.")
        from pyeyedata.archive_session import save_run_archive
        from pyeyedata.exports import (
            ExportCancelled,
            create_table_export_request,
            export_table,
        )
        from pyeyedata.store import ArchiveError

        archive_path = directory / "results-only.pyea"
        output_paths: list[Path] = []
        table_paths: list[Path] = []
        output_hashes: dict[str, str] = {}
        last_fraction = 0.0

        def report(fraction: float, message: str) -> None:
            nonlocal last_fraction
            bounded = min(1.0, max(last_fraction, float(fraction)))
            last_fraction = bounded
            if progress is not None:
                progress(bounded, str(message))

        def terminal_receipt(*, cancelled: bool) -> BatchExportResult:
            return BatchExportResult(
                run_id=str(record.run_id), attempt_dir=directory,
                archive_path=archive_path, table_paths=tuple(table_paths),
                output_paths=tuple(output_paths), output_sha256=dict(output_hashes),
                cancelled=cancelled, complete=not cancelled,
            )

        if _cancelled(cancel_requested):
            report(0.0, "批量导出已取消，尚未写出文件")
            return terminal_receipt(cancelled=True)
        try:
            if archive_path.exists():
                raise FileExistsError(f"输出已存在：{archive_path}")
            archive_width = 0.32
            report(0.0, "正在保存 results-only 运行档案")
            try:
                save_run_archive(
                    archive_path,
                    record,
                    mode="results_only",
                    cancel=cancel_requested,
                    progress=_stage_progress(
                        report if progress is not None else None, 0.0, archive_width
                    ),
                )
            except Exception as exc:
                is_archive_cancel = (
                    isinstance(exc, ArchiveError) and "cancel" in str(exc).casefold()
                )
                if is_archive_cancel and _cancelled(cancel_requested):
                    report(last_fraction, "档案保存已取消，正在保留已提交输出")
                    if archive_path.is_file():
                        output_paths.append(archive_path)
                        output_hashes[str(archive_path)] = _sha256(archive_path)
                    return terminal_receipt(cancelled=True)
                raise
            # save_run_archive returns only after the archive's atomic commit.
            output_paths.append(archive_path)
            report(archive_width, "运行档案已提交，正在记录输出摘要")
            output_hashes[str(archive_path)] = _sha256(archive_path)

            names = tuple(str(name) for name in record.table_names())
            included = [name for name in names if name.casefold() not in _RESULTS_ONLY_EXCLUDED
                        and not name.casefold().startswith("input_")]
            table_width = 0.66 / max(1, len(included))
            if included:
                report(archive_width, f"档案摘要已记录，准备导出 {len(included)} 个结果表")
            if included:
                tables_dir = directory / "tables"
                tables_dir.mkdir(exist_ok=False)
            used_names: set[str] = set()
            for index, table_name in enumerate(included):
                if _cancelled(cancel_requested):
                    report(last_fraction, "批量导出已取消，保留已提交的部分输出")
                    return terminal_receipt(cancelled=True)
                table_start = archive_width + table_width * index
                table_end = table_start + table_width
                report(table_start, f"正在导出结果表 {index + 1}/{len(included)}：{table_name}")
                file_stem = _safe_component(table_name, fallback="table")
                collision_key = file_stem.casefold()
                if collision_key in used_names:
                    suffix = hashlib.sha256(table_name.encode("utf-8")).hexdigest()[:8]
                    file_stem = f"{file_stem}-{suffix}"
                    collision_key = file_stem.casefold()
                if collision_key in used_names:
                    raise BatchOutputError(f"无法为结果表 {table_name!r} 生成唯一文件名。")
                used_names.add(collision_key)
                destination = tables_dir / f"{file_stem}.csv"
                request = create_table_export_request(
                    record, table_name, destination, file_format="csv", scope="full", overwrite=False,
                )
                try:
                    receipt = export_table(
                        record,
                        request,
                        cancel=cancel_requested,
                        progress=_stage_progress(
                            report if progress is not None else None, table_start, table_width
                        ),
                    )
                except ExportCancelled:
                    report(last_fraction, "结果表导出已取消，保留已提交的部分输出")
                    return terminal_receipt(cancelled=True)
                report(
                    table_start + table_width * 0.99,
                    f"结果表已提交，正在记录输出摘要 {index + 1}/{len(included)}",
                )
                table_paths.append(receipt.path)
                output_paths.append(receipt.path)
                output_hashes[str(receipt.path)] = receipt.sha256
                sidecars = tuple(sidecar for sidecar in (receipt.audit_path, receipt.references_path)
                                 if sidecar is not None)
                output_paths.extend(sidecars)
                for sidecar in sidecars:
                    output_hashes[str(sidecar)] = _sha256(sidecar)
                report(table_end, f"结果表已提交 {index + 1}/{len(included)}：{table_name}")
            if not included:
                report(0.98, "运行档案已保存；该运行没有可单独导出的结果表")
            result = terminal_receipt(cancelled=False)
            report(1.0, "批量档案与结果表已全部提交")
            return result
        except BatchOutputFailure:
            raise
        except Exception as exc:
            raise BatchOutputFailure(
                f"批量结果导出失败：{type(exc).__name__}: {exc}",
                committed_paths=output_paths,
            ) from exc

    def write_manifest(
        self,
        rows: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Atomically snapshot every queue row, with optional write-boundary progress."""
        if progress is not None and not callable(progress):
            raise TypeError("progress must be callable or None")

        def report(fraction: float, message: str) -> None:
            if progress is not None:
                progress(float(fraction), message)

        report(0.0, "正在准备批量清单")
        queue_dir = self._ensure_queue_dir()
        recorded_at = datetime.now(timezone.utc).isoformat()
        if isinstance(rows, Mapping):
            document = dict(rows)
            items = document.get("items", [])
            if not isinstance(items, (list, tuple)):
                raise TypeError("manifest items must be a sequence")
            document["items"] = [_manifest_item(item, recorded_at) for item in items]
            document.setdefault("schema", BATCH_MANIFEST_SCHEMA)
        else:
            document = {
                "schema": BATCH_MANIFEST_SCHEMA,
                "items": [_manifest_item(item, recorded_at) for item in rows],
            }
        document["updated_at"] = recorded_at
        manifest_path = queue_dir / "manifest.json"
        fd, temporary_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=queue_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                report(0.1, "正在写入批量清单临时文件")
                json.dump(_json_safe(document), stream, ensure_ascii=False, sort_keys=True,
                          indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                report(0.9, "正在同步并原子提交批量清单")
                os.fsync(stream.fileno())
            os.replace(temporary, manifest_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        report(1.0, "批量清单已原子更新")
        return manifest_path


ExportResult = BatchExportResult


__all__ = [
    "BatchExportResult", "BatchOutputError", "BatchOutputFailure",
    "BatchOutputStore", "ExportResult",
]
