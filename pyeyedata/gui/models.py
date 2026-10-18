"""Immutable GUI revisions and run records around the public analysis contracts."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
import uuid

import numpy as np
import pandas as pd

from pyeyedata import AnalysisBundle, NormalizedDataset


def _clone_dataset(dataset: NormalizedDataset) -> NormalizedDataset:
    return NormalizedDataset(
        signals=dataset.signals.copy(deep=True),
        metadata=json.loads(json.dumps(dataset.metadata, ensure_ascii=False, default=_json_default)),
        segments=dataset.segments.copy(deep=True),
        aois=dataset.aois.copy(deep=True),
        performance=dataset.performance.copy(deep=True),
        source_manifest=json.loads(json.dumps(dataset.source_manifest, ensure_ascii=False, default=_json_default)),
    )


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _cell_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return {"$missing": "null" if value is None else type(value).__name__}
    if isinstance(value, np.generic):
        return _cell_value(value.item())
    if is_dataclass(value):
        return {item.name: _cell_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (pd.Timestamp, datetime)):
        return {"$datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": str(value)}
    if isinstance(value, Mapping):
        return {str(key): _cell_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, tuple):
        return {"$tuple": [_cell_value(item) for item in value]}
    if isinstance(value, list):
        return [_cell_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_cell_value(item) for item in value]
        return {"$set": sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))}
    if isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported dataset cell value: {type(value).__name__}")


def _table_hash(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([str(column) for column in frame.columns], ensure_ascii=False).encode("utf-8"))
    digest.update(json.dumps([str(dtype) for dtype in frame.dtypes], ensure_ascii=False).encode("utf-8"))
    for row in frame.itertuples(index=True, name=None):
        packed = json.dumps([_cell_value(value) for value in row], ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest.update(packed.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _dataset_hash(dataset: NormalizedDataset) -> str:
    digest = hashlib.sha256()
    for name, frame in (
        ("signals", dataset.signals),
        ("segments", dataset.segments),
        ("aois", dataset.aois),
        ("performance", dataset.performance),
    ):
        digest.update(name.encode("ascii"))
        digest.update(_table_hash(frame).encode("ascii"))
    metadata = json.dumps(_cell_value(dataset.metadata), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    manifest = json.dumps(_cell_value(dataset.source_manifest), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    digest.update(metadata.encode("utf-8"))
    digest.update(manifest.encode("utf-8"))
    return digest.hexdigest()


def _freeze(value: Any) -> Any:
    if is_dataclass(value):
        return MappingProxyType({item.name: _freeze(getattr(value, item.name)) for item in fields(value)})
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, pd.DataFrame):
        return value.copy(deep=True)
    if isinstance(value, pd.Series):
        return value.copy(deep=True)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_thaw(item) for item in value)
    if isinstance(value, frozenset):
        return {_thaw(item) for item in value}
    if isinstance(value, pd.DataFrame):
        return value.copy(deep=True)
    if isinstance(value, pd.Series):
        return value.copy(deep=True)
    return value


@dataclass(frozen=True, slots=True)
class SelectionSpec:
    """Explicit per-module selection; empty selections never mean “run defaults”."""

    individual_metric_ids: tuple[str, ...] = ()
    group_metric_ids: tuple[str, ...] = ()
    advanced_metric_ids: tuple[str, ...] = ()
    run_individual: bool = False
    run_group: bool = False
    run_advanced: bool = False

    def __post_init__(self) -> None:
        for name in ("individual_metric_ids", "group_metric_ids", "advanced_metric_ids"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name} must contain nonempty metric IDs")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicate metric IDs")
            object.__setattr__(self, name, values)
        for name in ("run_individual", "run_group", "run_advanced"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "individual_metric_ids": list(self.individual_metric_ids),
            "group_metric_ids": list(self.group_metric_ids),
            "advanced_metric_ids": list(self.advanced_metric_ids),
            "run_individual": self.run_individual,
            "run_group": self.run_group,
            "run_advanced": self.run_advanced,
        }


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    """A stable input revision whose data are only exposed as detached copies."""

    revision_id: str
    fingerprint: str
    created_at: str
    source_hashes: Mapping[str, str]
    _dataset: NormalizedDataset = field(repr=False, compare=False)
    _raw_sources: Mapping[str, bytes] = field(repr=False, compare=False)

    @classmethod
    def create(
        cls,
        dataset: NormalizedDataset,
        *,
        raw_sources: Mapping[str, bytes] | None = None,
        revision_id: str | None = None,
    ) -> "DatasetRevision":
        dataset.validate()
        frozen_dataset = _clone_dataset(dataset)
        raw_copy = {str(name): bytes(content) for name, content in (raw_sources or {}).items()}
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in raw_copy.items()}
        for item in frozen_dataset.source_manifest:
            if isinstance(item, Mapping):
                name, digest = item.get("source_name"), item.get("sha256")
                if isinstance(name, str) and isinstance(digest, str) and name not in hashes:
                    hashes[name] = digest
        return cls(
            revision_id=revision_id or "data-" + uuid.uuid4().hex,
            fingerprint=_dataset_hash(frozen_dataset),
            created_at=datetime.now(timezone.utc).isoformat(),
            source_hashes=MappingProxyType(hashes),
            _dataset=frozen_dataset,
            _raw_sources=MappingProxyType(raw_copy),
        )

    def dataset_copy(self) -> NormalizedDataset:
        return _clone_dataset(self._dataset)

    def raw_source_bytes(self) -> dict[str, bytes]:
        return dict(self._raw_sources)


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """The exact immutable data/configuration revision dispatched to one backend run."""

    dataset_revision: DatasetRevision
    generation: int
    run_id: str
    selection: SelectionSpec
    schema_version: str
    config_fingerprint: str
    created_at: str
    _parameters: Mapping[str, Any] = field(repr=False, compare=False)
    _context: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def create(
        cls,
        dataset_revision: DatasetRevision,
        *,
        generation: int,
        selection: SelectionSpec,
        parameters: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        schema_version: str,
        run_id: str | None = None,
    ) -> "RunSnapshot":
        if generation < 1:
            raise ValueError("generation must be positive")
        if not schema_version:
            raise ValueError("schema_version must be nonempty")
        parameter_copy = _freeze(dict(parameters or {}))
        context_copy = _freeze(dict(context or {}))
        run_key = run_id or "run-" + uuid.uuid4().hex
        identity = {
            "dataset": dataset_revision.fingerprint,
            "schema_version": schema_version,
            "parameters": _fingerprint_value(_thaw(parameter_copy)),
            "context": _fingerprint_value(_thaw(context_copy)),
            "selection": selection.to_dict(),
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()
        return cls(
            dataset_revision=dataset_revision,
            generation=generation,
            run_id=run_key,
            selection=selection,
            schema_version=schema_version,
            config_fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
            _parameters=parameter_copy,
            _context=context_copy,
        )

    def parameter_values(self) -> dict[str, Any]:
        return _thaw(self._parameters)

    def context_values(self) -> dict[str, Any]:
        return _thaw(self._context)

    def dataset_copy(self) -> NormalizedDataset:
        return self.dataset_revision.dataset_copy()


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return {"$dataframe_sha256": _table_hash(value)}
    if isinstance(value, pd.Series):
        return {"$series_sha256": _table_hash(value.to_frame())}
    if is_dataclass(value):
        return {item.name: _fingerprint_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _fingerprint_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_fingerprint_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    return _cell_value(value)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Read-only history metadata and detached result tables for one run."""

    run_id: str
    generation: int
    revision_id: str
    config_fingerprint: str
    phase: str
    status: str
    created_at: str
    _metadata: Mapping[str, Any] = field(repr=False, compare=False)
    _tables: Mapping[str, pd.DataFrame] = field(repr=False, compare=False)
    _snapshot: RunSnapshot | None = field(default=None, repr=False, compare=False)
    _logs: tuple[str, ...] = field(default=(), repr=False, compare=False)
    _archived_details: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_bundle(
        cls,
        snapshot: RunSnapshot,
        bundle: AnalysisBundle,
        *,
        effective_status: str | None = None,
        stale_at_return: bool = False,
    ) -> "RunRecord":
        metadata_values = dict(bundle.metadata)
        if effective_status is not None and effective_status != str(metadata_values.get("execution_status", "unknown")):
            metadata_values["gui_effective_status"] = effective_status
        if stale_at_return:
            metadata_values["gui_stale_at_return"] = True
        metadata = _freeze(metadata_values)
        tables = MappingProxyType({name: frame.copy(deep=True) for name, frame in bundle.tables.items()})
        return cls(
            run_id=snapshot.run_id,
            generation=snapshot.generation,
            revision_id=snapshot.dataset_revision.revision_id,
            config_fingerprint=snapshot.config_fingerprint,
            phase="run",
            status=effective_status or str(bundle.metadata.get("execution_status", "unknown")),
            created_at=snapshot.created_at,
            _metadata=metadata,
            _tables=tables,
            _snapshot=snapshot,
            _logs=tuple(bundle.logs),
        )

    @classmethod
    def from_failure(
        cls,
        snapshot: RunSnapshot,
        *,
        phase: str,
        failure_type: str,
        failure_reason: str,
        stale_at_return: bool = False,
    ) -> "RunRecord":
        """Keep a failed task in history without fabricating an empty result table."""
        if not phase:
            raise ValueError("phase must be nonempty")
        metadata = {
            "execution_status": "failed",
            "failure_type": str(failure_type),
            "failure_reason": str(failure_reason),
            "run_id": snapshot.run_id,
            "generation": snapshot.generation,
            "revision_id": snapshot.dataset_revision.revision_id,
            "config_fingerprint": snapshot.config_fingerprint,
        }
        if stale_at_return:
            metadata["gui_stale_at_return"] = True
        return cls(
            run_id=snapshot.run_id,
            generation=snapshot.generation,
            revision_id=snapshot.dataset_revision.revision_id,
            config_fingerprint=snapshot.config_fingerprint,
            phase=phase,
            status="failed",
            created_at=snapshot.created_at,
            _metadata=_freeze(metadata),
            _tables=MappingProxyType({}),
            _snapshot=snapshot,
            _logs=(f"{failure_type}: {failure_reason}",),
        )

    @classmethod
    def from_archive(
        cls,
        *,
        run_id: str,
        generation: int,
        revision_id: str,
        config_fingerprint: str,
        phase: str,
        status: str,
        created_at: str,
        metadata: Mapping[str, Any],
        tables: Mapping[str, pd.DataFrame],
        logs: tuple[str, ...] | list[str] = (),
        snapshot: RunSnapshot | None = None,
        snapshot_details: Mapping[str, Any] | None = None,
    ) -> "RunRecord":
        """Restore one immutable run-history row without inventing a dataset."""
        if not isinstance(metadata, Mapping) or not isinstance(tables, Mapping):
            raise TypeError("archive metadata and tables must be mappings")
        copied_tables: dict[str, pd.DataFrame] = {}
        for name, frame in tables.items():
            if not isinstance(frame, pd.DataFrame):
                raise TypeError(f"archive result table {name!r} must be a DataFrame")
            copied_tables[str(name)] = frame.copy(deep=True)
        return cls(
            run_id=str(run_id),
            generation=int(generation),
            revision_id=str(revision_id),
            config_fingerprint=str(config_fingerprint),
            phase=str(phase),
            status=str(status),
            created_at=str(created_at),
            _metadata=_freeze(dict(metadata)),
            _tables=MappingProxyType(copied_tables),
            _snapshot=snapshot,
            _logs=tuple(str(line) for line in logs),
            _archived_details=_freeze(dict(snapshot_details)) if snapshot_details is not None else None,
        )

    def metadata(self) -> Mapping[str, Any]:
        return MappingProxyType(_thaw(self._metadata))

    @property
    def snapshot(self) -> RunSnapshot | None:
        """The immutable run snapshot retained for history and archive handoff."""
        return self._snapshot

    def snapshot_details(self) -> Mapping[str, Any]:
        """Return detached configuration, named context, and source provenance for this run."""
        snapshot = self._snapshot
        if snapshot is None:
            if self._archived_details is None:
                return MappingProxyType({})
            return MappingProxyType(_thaw(self._archived_details))
        revision = snapshot.dataset_revision
        details = {
            "run_id": snapshot.run_id,
            "generation": snapshot.generation,
            "revision_id": revision.revision_id,
            "dataset_fingerprint": revision.fingerprint,
            "source_hashes": dict(revision.source_hashes),
            "raw_sources": revision.raw_source_bytes(),
            "schema_version": snapshot.schema_version,
            "config_fingerprint": snapshot.config_fingerprint,
            "selection": snapshot.selection.to_dict(),
            "parameters": snapshot.parameter_values(),
            "context": snapshot.context_values(),
        }
        return MappingProxyType(details)

    def logs(self) -> tuple[str, ...]:
        """Execution messages retained verbatim with the history record."""
        return self._logs

    def table_names(self) -> tuple[str, ...]:
        return tuple(self._tables)

    def table_copy(self, name: str) -> pd.DataFrame:
        return self._tables[name].copy(deep=True)

    def tables(self) -> Mapping[str, pd.DataFrame]:
        return MappingProxyType({name: frame.copy(deep=True) for name, frame in self._tables.items()})
