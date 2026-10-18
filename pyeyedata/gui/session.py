"""Session state for immutable data revisions and read-only run history."""
from __future__ import annotations

from threading import RLock
from typing import Any, Mapping
from pyeyedata.execution_cache import ExecutionCache

from .models import DatasetRevision, RunRecord, RunSnapshot, SelectionSpec


class AnalysisSession:
    """Owns current revision, dispatched generations, and immutable run records."""

    def __init__(self, dataset_revision: DatasetRevision | None = None) -> None:
        self._lock = RLock()
        self._dataset_revision = dataset_revision
        self._generation = 0
        self._active_snapshot: RunSnapshot | None = None
        self._history: list[RunRecord] = []
        self.execution_cache = ExecutionCache()

    @property
    def dataset_revision(self) -> DatasetRevision | None:
        with self._lock:
            return self._dataset_revision

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active_snapshot(self) -> RunSnapshot | None:
        with self._lock:
            return self._active_snapshot

    @property
    def history(self) -> tuple[RunRecord, ...]:
        with self._lock:
            return tuple(self._history)

    def install_dataset(self, revision: DatasetRevision) -> int:
        with self._lock:
            self._dataset_revision = revision
            self._generation += 1
            self._active_snapshot = None
            generation = self._generation
        # Cache eligibility calls is_current under its own lock. Clear outside
        # the session lock so input replacement never inverts that lock order.
        self.execution_cache.clear()
        return generation

    def create_snapshot(
        self,
        *,
        selection: SelectionSpec,
        schema_version: str,
        parameters: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        dataset_revision: DatasetRevision | None = None,
    ) -> RunSnapshot:
        with self._lock:
            revision = dataset_revision or self._dataset_revision
            if revision is None:
                raise RuntimeError("Load and normalize a dataset before creating a run")
            self._generation += 1
            snapshot = RunSnapshot.create(
                revision,
                generation=self._generation,
                selection=selection,
                schema_version=schema_version,
                parameters=parameters,
                context=context,
            )
            self._active_snapshot = snapshot
            return snapshot

    def invalidate_active_run(self) -> int:
        """Mark any in-flight snapshot stale after an input/configuration change."""
        with self._lock:
            self._generation += 1
            self._active_snapshot = None
            return self._generation

    def clear_dataset(self) -> int:
        """Require a new normalized revision after import mapping/source changes."""
        with self._lock:
            self._dataset_revision = None
            self._generation += 1
            self._active_snapshot = None
            generation = self._generation
        self.execution_cache.clear()
        return generation

    def is_current(self, run_id: str, generation: int) -> bool:
        with self._lock:
            snapshot = self._active_snapshot
            return bool(snapshot and snapshot.run_id == run_id and snapshot.generation == generation)

    def record(self, record: RunRecord) -> bool:
        """Keep every completed record; return whether it still targets the active input."""
        with self._lock:
            self._history.append(record)
            return self.is_current(record.run_id, record.generation)

    def finish(self, run_id: str, generation: int) -> None:
        with self._lock:
            if self.is_current(run_id, generation):
                self._active_snapshot = None

