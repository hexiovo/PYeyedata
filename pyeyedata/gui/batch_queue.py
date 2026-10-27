"""Pure batch-analysis state model with independent export status tracking."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Any, Mapping
import uuid


BATCH_MANIFEST_SCHEMA = "pyeyedata.batch-manifest/v1"
ANALYSIS_STATUSES = frozenset({
    "pending", "importing", "checking", "running", "complete", "partial", "failed", "cancelled",
})
EXPORT_STATUSES = frozenset({
    "not_requested", "pending", "exporting", "complete", "failed", "cancelled",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BatchQueueError(ValueError):
    """A batch state transition or manifest input is invalid."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchQueueError(f"{name} must be a nonempty string.")
    return value


def _json_copy(value: Any, name: str) -> Any:
    if not isinstance(value, Mapping):
        raise BatchQueueError(f"{name} must be a mapping.")
    try:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False)
        return json.loads(content)
    except (TypeError, ValueError) as exc:
        raise BatchQueueError(f"{name} must contain finite JSON-compatible values: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BatchAttempt:
    """One independently retryable analysis attempt and its export attempts."""

    number: int
    status: str = "importing"
    run_id: str | None = None
    error: str | None = None
    export_status: str = "not_requested"
    export_attempts: int = 0
    output_paths: tuple[str, ...] = ()
    export_error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise BatchQueueError("attempt number must be a positive integer.")
        if self.status not in ANALYSIS_STATUSES:
            raise BatchQueueError(f"Unsupported analysis status: {self.status!r}")
        if self.export_status not in EXPORT_STATUSES:
            raise BatchQueueError(f"Unsupported export status: {self.export_status!r}")
        if isinstance(self.export_attempts, bool) or not isinstance(self.export_attempts, int) or self.export_attempts < 0:
            raise BatchQueueError("export_attempts must be a nonnegative integer.")
        object.__setattr__(self, "output_paths", tuple(_text(path, "output path") for path in self.output_paths))

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "analysis_status": self.status,
            "run_id": self.run_id,
            "error": self.error,
            "export_status": self.export_status,
            "export_attempts": self.export_attempts,
            "output_paths": list(self.output_paths),
            "export_error": self.export_error,
        }


@dataclass(frozen=True, slots=True)
class BatchItem:
    """Manifest-ready provenance and the current state for one source file."""

    item_id: str
    source_path: str
    source_sha256: str | None
    profile_id: str
    profile_revision: int
    structure_confirmed: bool = False
    attempts: tuple[BatchAttempt, ...] = ()
    status: str = "pending"
    revision: int = 0
    run_id: str | None = None
    error: str | None = None
    export_status: str = "not_requested"
    output_paths: tuple[str, ...] = ()
    export_error: str | None = None
    source_hash_error: str | None = None
    preflight_error: str | None = None

    def __post_init__(self) -> None:
        _text(self.item_id, "item_id")
        _text(self.source_path, "source_path")
        if self.source_sha256 is None:
            if not isinstance(self.source_hash_error, str) or not self.source_hash_error.strip():
                raise BatchQueueError("A missing source hash must include its read failure reason.")
        elif not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256):
            raise BatchQueueError("source_sha256 must be a lowercase SHA-256 hex digest or null.")
        _text(self.profile_id, "profile_id")
        if (isinstance(self.profile_revision, bool) or not isinstance(self.profile_revision, int)
                or self.profile_revision < 1):
            raise BatchQueueError("profile_revision must be a positive integer.")
        if type(self.structure_confirmed) is not bool:
            raise BatchQueueError("structure_confirmed must be an explicit boolean.")
        if self.status not in ANALYSIS_STATUSES:
            raise BatchQueueError(f"Unsupported analysis status: {self.status!r}")
        if self.export_status not in EXPORT_STATUSES:
            raise BatchQueueError(f"Unsupported export status: {self.export_status!r}")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise BatchQueueError("revision must be a nonnegative integer.")
        attempts = tuple(self.attempts)
        if any(not isinstance(attempt, BatchAttempt) for attempt in attempts):
            raise BatchQueueError("attempts must contain BatchAttempt values.")
        if tuple(attempt.number for attempt in attempts) != tuple(range(1, len(attempts) + 1)):
            raise BatchQueueError("attempt numbers must be consecutive and start at 1.")
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "output_paths", tuple(_text(path, "output path") for path in self.output_paths))

    @property
    def analysis_status(self) -> str:
        """Explicit alias used by the UI and summary manifests."""
        return self.status

    @property
    def current_attempt(self) -> BatchAttempt | None:
        return self.attempts[-1] if self.attempts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "structure_confirmed": self.structure_confirmed,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "status": self.status,
            "analysis_status": self.status,
            "export_status": self.export_status,
            "revision": self.revision,
            "run_id": self.run_id,
            "error": self.error,
            "output_paths": list(self.output_paths),
            "export_error": self.export_error,
            "source_hash_error": self.source_hash_error,
            "preflight_error": self.preflight_error,
        }


class BatchQueue:
    """Serial queue state; worker execution and filesystem export stay in the GUI layer."""

    _TRANSITIONS = {
        "pending": frozenset({"importing", "cancelled"}),
        "importing": frozenset({"checking", "failed", "cancelled"}),
        "checking": frozenset({"running", "failed", "cancelled"}),
        "running": frozenset({"complete", "partial", "failed", "cancelled"}),
        "complete": frozenset(),
        "partial": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
    }

    def __init__(self, items: tuple[BatchItem, ...] | list[BatchItem] = ()) -> None:
        self._items: list[BatchItem] = []
        self._active_item_id: str | None = None
        self._active_export_item_id: str | None = None
        self._cancelled = False
        self._frozen_configuration: dict[str, Any] | None = None
        self._configuration_fingerprint: str | None = None
        for item in items:
            self.add(item)

    @property
    def items(self) -> tuple[BatchItem, ...]:
        return tuple(self._items)

    @property
    def active_item_id(self) -> str | None:
        return self._active_item_id

    @property
    def active_export_item_id(self) -> str | None:
        return self._active_export_item_id

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def configuration_fingerprint(self) -> str | None:
        return self._configuration_fingerprint

    @property
    def frozen_configuration(self) -> dict[str, Any] | None:
        return _json_copy(self._frozen_configuration, "frozen_configuration") if self._frozen_configuration is not None else None

    def freeze_configuration(self, configuration: Mapping[str, Any]) -> str:
        """Freeze the one analysis configuration for every file in this queue."""
        frozen = _json_copy(configuration, "configuration")
        encoded = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if self._frozen_configuration is not None:
            if fingerprint != self._configuration_fingerprint:
                raise BatchQueueError("A batch configuration is already frozen; create a new queue to change it.")
            return fingerprint
        self._frozen_configuration = frozen
        self._configuration_fingerprint = fingerprint
        return fingerprint

    def add(self, item: BatchItem) -> BatchItem:
        if not isinstance(item, BatchItem):
            raise BatchQueueError("Only BatchItem values can be added.")
        if self._cancelled or self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Cannot add an item while the queue is cancelled or a worker is active.")
        if item.status != "pending" or item.attempts or item.export_status != "not_requested":
            raise BatchQueueError("New queue items must begin in the pending state without attempts.")
        if any(existing.item_id == item.item_id for existing in self._items):
            raise BatchQueueError("Batch item IDs must be unique.")
        self._items.append(item)
        return item

    def remove_items(self, item_ids: tuple[str, ...] | list[str]) -> tuple[BatchItem, ...]:
        """Remove selected unstarted items before the shared configuration freezes."""
        if self._frozen_configuration is not None:
            raise BatchQueueError("A started batch keeps its input manifest and cannot remove items.")
        if self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Wait for active batch work before removing items.")
        wanted = set(item_ids)
        if not wanted:
            return ()
        unknown = wanted - {item.item_id for item in self._items}
        if unknown:
            raise BatchQueueError("Unknown batch item IDs: " + ", ".join(sorted(unknown)))
        removed = tuple(item for item in self._items if item.item_id in wanted)
        self._items = [item for item in self._items if item.item_id not in wanted]
        return removed

    def create_item(
        self, *, source_path: str, source_sha256: str | None, profile_id: str, profile_revision: int,
        structure_confirmed: bool = False, item_id: str | None = None,
        source_hash_error: str | None = None, preflight_error: str | None = None,
        error: str | None = None,
    ) -> BatchItem:
        item = BatchItem(
            item_id=item_id or uuid.uuid4().hex[:10], source_path=source_path,
            source_sha256=source_sha256, profile_id=profile_id,
            profile_revision=profile_revision, structure_confirmed=structure_confirmed,
            source_hash_error=source_hash_error, preflight_error=preflight_error,
            error=error,
        )
        return self.add(item)

    def get(self, item_id: str) -> BatchItem:
        for item in self._items:
            if item.item_id == item_id:
                return item
        raise BatchQueueError(f"Unknown batch item: {item_id!r}")

    def confirm_structure(self, item_id: str) -> BatchItem:
        """Record the user's explicit confirmation after a profile mismatch review."""
        item = self.get(item_id)
        if item.status != "pending":
            raise BatchQueueError("Source structure can be confirmed only before an analysis attempt.")
        if item.structure_confirmed:
            return item
        return self._replace(replace(item, structure_confirmed=True, error=None,
                                     preflight_error=None, revision=item.revision + 1))

    def set_input_inspection(
        self, item_id: str, *, source_sha256: str | None, source_hash_error: str | None,
        structure_confirmed: bool, error: str | None = None,
        preflight_error: str | None = None,
    ) -> BatchItem:
        """Install the asynchronous file hash and profile-structure review result."""
        item = self.get(item_id)
        if item.status != "pending" or item.attempts:
            raise BatchQueueError("Input inspection can update only an unstarted pending item.")
        if type(structure_confirmed) is not bool:
            raise BatchQueueError("structure_confirmed must be an explicit boolean.")
        if structure_confirmed and (source_sha256 is None or preflight_error):
            raise BatchQueueError("An input cannot be confirmed without a readable, hashed source.")
        return self._replace(replace(
            item, source_sha256=source_sha256, source_hash_error=source_hash_error,
            structure_confirmed=structure_confirmed, error=error,
            preflight_error=preflight_error, revision=item.revision + 1,
        ))

    def require_structure_confirmation(self, item_id: str, message: str) -> BatchItem:
        """Fail closed after a shared mapping change invalidates an unstarted review."""
        item = self.get(item_id)
        if item.status != "pending" or item.attempts:
            raise BatchQueueError("Only a pending unstarted item can require structure review.")
        if not isinstance(message, str) or not message.strip():
            raise BatchQueueError("A structure review reason is required.")
        return self._replace(replace(
            item, structure_confirmed=False, error=message,
            revision=item.revision + 1,
        ))

    def fail_pending_preflight(self, item_id: str, error: str) -> BatchItem:
        """Record an unreadable or malformed source without claiming a run exists."""
        item = self.get(item_id)
        if item.status != "pending" or item.attempts:
            raise BatchQueueError("Only an unstarted pending item can fail preflight.")
        if self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Wait for active batch work before recording preflight failure.")
        if not isinstance(error, str) or not error.strip():
            raise BatchQueueError("A preflight failure needs a reason.")
        attempt = BatchAttempt(number=1, status="failed", error=error)
        return self._replace(replace(
            item, attempts=(attempt,), status="failed", revision=item.revision + 1,
            error=error, export_status="not_requested", export_error=None,
        ))

    def next_pending(self) -> BatchItem | None:
        if self._cancelled or self._active_item_id is not None or self._active_export_item_id is not None:
            return None
        return next((item for item in self._items if item.status == "pending"), None)

    def start_next(self) -> BatchItem | None:
        item = self.next_pending()
        return self.start_item(item.item_id) if item is not None else None

    def start_item(self, item_id: str) -> BatchItem:
        if self._cancelled:
            raise BatchQueueError("The queue is cancelled; resume it before starting work.")
        if self._frozen_configuration is None:
            raise BatchQueueError("Freeze the shared analysis configuration before starting a batch.")
        if self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Only one batch item may run at a time.")
        item = self.get(item_id)
        if item.status != "pending":
            raise BatchQueueError("Only a pending item can start its first attempt.")
        if not item.structure_confirmed:
            raise BatchQueueError("Confirm the source structure against its import profile before starting.")
        updated = self._begin_attempt(item)
        self._active_item_id = item_id
        return self._replace(updated)

    def transition(
        self, item_id: str, status: str, *, run_id: str | None = None,
        error: str | None = None, export_requested: bool = True,
    ) -> BatchItem:
        item = self.get(item_id)
        if status not in self._TRANSITIONS:
            raise BatchQueueError(f"Unsupported analysis status: {status!r}")
        if status not in self._TRANSITIONS[item.status]:
            raise BatchQueueError(f"Invalid analysis transition {item.status!r} → {status!r}.")
        if item.status != "pending" and self._active_item_id != item_id:
            raise BatchQueueError("Only the active item can advance its analysis state.")
        if status == "failed" and not (isinstance(error, str) and error.strip()):
            raise BatchQueueError("A failed analysis attempt needs its failure reason.")
        actual_run_id = run_id.strip() if isinstance(run_id, str) else item.run_id
        if status in {"complete", "partial"} and not actual_run_id:
            raise BatchQueueError("A complete or partial analysis result needs its run ID.")
        export_status = item.export_status
        if status in {"complete", "partial"}:
            export_status = "pending" if export_requested else "not_requested"
        elif status in {"failed", "cancelled"}:
            export_status = "not_requested"
        if item.current_attempt is None:
            if not (item.status == "pending" and status == "cancelled"):
                raise BatchQueueError("Start an analysis attempt before advancing it.")
            attempts = item.attempts
        else:
            attempts = self._replace_current_attempt(
                item, status=status, run_id=actual_run_id, error=error,
                export_status=export_status,
            )
        updated = replace(
            item, attempts=attempts, status=status, revision=item.revision + 1,
            run_id=actual_run_id, error=error, export_status=export_status,
            output_paths=() if status in {"complete", "partial"} else item.output_paths,
            export_error=None if status in {"complete", "partial"} else item.export_error,
        )
        if status in {"complete", "partial", "failed", "cancelled"} and self._active_item_id == item_id:
            self._active_item_id = None
        if status == "cancelled" and item.status == "pending":
            self._cancelled = True
        return self._replace(updated)

    def retry_analysis(self, item_id: str) -> BatchItem:
        if self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Wait for the active item before retrying another analysis.")
        if self._frozen_configuration is None:
            raise BatchQueueError("Freeze the shared analysis configuration before retrying a batch item.")
        item = self.get(item_id)
        if item.status not in {"failed", "partial", "cancelled"}:
            raise BatchQueueError("Only failed, partial, or cancelled analysis can be retried.")
        if not item.structure_confirmed:
            raise BatchQueueError("Confirm the source structure against its import profile before retrying.")
        # A user-initiated retry after a cancelled batch is an explicit resume.
        self._cancelled = False
        updated = self._begin_attempt(item)
        self._active_item_id = item_id
        return self._replace(updated)

    def resume(self) -> None:
        """Allow explicitly requested retry work after queue cancellation."""
        if self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Wait for active batch work before resuming the queue.")
        self._cancelled = False

    def start_export(self, item_id: str) -> BatchItem:
        if self._active_item_id is not None or self._active_export_item_id is not None:
            raise BatchQueueError("Wait for the active batch operation before starting an export.")
        item = self.get(item_id)
        if item.status not in {"complete", "partial"}:
            raise BatchQueueError("Only complete or partial analysis results can be exported.")
        if item.export_status not in {"pending", "failed", "cancelled"}:
            raise BatchQueueError("Export is not pending or retryable.")
        attempt = item.current_attempt
        if attempt is None:
            raise BatchQueueError("An analysis attempt is required before export.")
        next_export_attempt = attempt.export_attempts + 1
        updated_attempt = replace(attempt, export_status="exporting", export_attempts=next_export_attempt,
                                  export_error=None)
        attempts = (*item.attempts[:-1], updated_attempt)
        updated = replace(item, attempts=attempts, export_status="exporting",
                          export_error=None, revision=item.revision + 1)
        self._active_export_item_id = item_id
        return self._replace(updated)

    def finish_export(
        self, item_id: str, status: str, *, output_paths: tuple[str, ...] | list[str] = (),
        error: str | None = None,
    ) -> BatchItem:
        if status not in {"complete", "failed", "cancelled"}:
            raise BatchQueueError("Export can finish only as complete, failed, or cancelled.")
        item = self.get(item_id)
        if item.export_status != "exporting" or self._active_export_item_id != item_id:
            raise BatchQueueError("Only an active export can finish.")
        paths = tuple(_text(path, "output path") for path in output_paths)
        if status == "complete" and not paths:
            raise BatchQueueError("A completed export must record at least one output path.")
        if status == "failed" and not (isinstance(error, str) and error.strip()):
            raise BatchQueueError("A failed export needs its failure reason.")
        attempt = item.current_attempt
        if attempt is None:
            raise BatchQueueError("An analysis attempt is required before export.")
        updated_attempt = replace(
            attempt, export_status=status, output_paths=paths,
            export_error=error if status == "failed" else None,
        )
        updated = replace(
            item, attempts=(*item.attempts[:-1], updated_attempt),
            export_status=status, output_paths=paths,
            export_error=error if status == "failed" else None,
            revision=item.revision + 1,
        )
        self._active_export_item_id = None
        return self._replace(updated)

    def retry_export(self, item_id: str) -> BatchItem:
        item = self.get(item_id)
        if item.export_status not in {"failed", "cancelled"}:
            raise BatchQueueError("Only a failed or cancelled export can be retried.")
        # Export retry belongs to the same analysis attempt and never changes its status.
        return self.start_export(item_id)

    def cancel_pending_export(self, item_id: str, error: str = "队列已取消；导出未启动。") -> BatchItem:
        """Mark a queued export cancelled when cancellation arrives before worker start."""
        if self._active_export_item_id is not None or self._active_item_id is not None:
            raise BatchQueueError("Wait for active batch work before cancelling a pending export.")
        item = self.get(item_id)
        if item.status not in {"complete", "partial"} or item.export_status != "pending":
            raise BatchQueueError("Only an unstarted export for a completed analysis can be cancelled.")
        if not isinstance(error, str) or not error.strip():
            raise BatchQueueError("A cancelled export needs a reason.")
        attempt = item.current_attempt
        if attempt is None:
            raise BatchQueueError("An analysis attempt is required before cancelling its export.")
        updated_attempt = replace(
            attempt, export_status="cancelled", export_error=error,
        )
        return self._replace(replace(
            item, attempts=(*item.attempts[:-1], updated_attempt),
            export_status="cancelled", export_error=error,
            revision=item.revision + 1,
        ))

    def cancel(self) -> tuple[BatchItem, ...]:
        """Stop future dispatch and mark pending items cancelled; active work reports its result."""
        self._cancelled = True
        updated_items: list[BatchItem] = []
        for item in tuple(self._items):
            if item.status == "pending":
                updated_items.append(self.transition(item.item_id, "cancelled", error="队列已取消"))
        return tuple(updated_items)

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": BATCH_MANIFEST_SCHEMA,
            "configuration_fingerprint": self._configuration_fingerprint,
            "cancelled": self._cancelled,
            "items": [item.to_dict() for item in self._items],
        }

    def _begin_attempt(self, item: BatchItem) -> BatchItem:
        attempt = BatchAttempt(number=len(item.attempts) + 1, status="importing")
        return replace(
            item, attempts=(*item.attempts, attempt), status="importing",
            revision=item.revision + 1, run_id=None, error=None,
            export_status="not_requested", output_paths=(), export_error=None,
        )

    def _replace_current_attempt(
        self, item: BatchItem, *, status: str, run_id: str | None,
        error: str | None, export_status: str,
    ) -> tuple[BatchAttempt, ...]:
        attempt = item.current_attempt
        if attempt is None:
            raise BatchQueueError("Start an analysis attempt before advancing it.")
        updated_attempt = replace(attempt, status=status, run_id=run_id, error=error,
                                  export_status=export_status)
        return (*item.attempts[:-1], updated_attempt)

    def _replace(self, updated: BatchItem) -> BatchItem:
        for index, item in enumerate(self._items):
            if item.item_id == updated.item_id:
                self._items[index] = updated
                return updated
        raise BatchQueueError(f"Unknown batch item: {updated.item_id!r}")

