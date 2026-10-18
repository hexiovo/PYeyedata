"""Queued pipeline workers with cooperative cancellation and generation-tagged signals."""
from __future__ import annotations

from dataclasses import replace
from threading import Event, Lock
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .bridge import MetricReusePlan, PipelineBridge, PipelineOutcome, PipelinePhase, PipelineStatus
from .models import RunSnapshot


class CancellationToken:
    """Thread-safe zero-argument predicate passed to the public pipeline API."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._requested = False

    def cancel(self) -> None:
        with self._lock:
            self._requested = True
            self._event.set()

    def is_requested(self) -> bool:
        return self._event.is_set()

    def __call__(self) -> bool:
        return self._event.is_set()


class PipelineTaskWorker(QObject):
    progress_changed = Signal(str, int, float, str)
    state_changed = Signal(str, int, str, str)
    completed = Signal(str, int, object)
    failed = Signal(str, int, str, str)
    finished = Signal(str, int)

    def __init__(
        self,
        bridge: PipelineBridge,
        snapshot: RunSnapshot,
        phase: PipelinePhase,
        token: CancellationToken,
        reuse_plan: MetricReusePlan | None = None,
    ) -> None:
        super().__init__()
        if phase not in {PipelinePhase.PREFLIGHT, PipelinePhase.RUN}:
            raise ValueError("PipelineTaskWorker supports preflight and run phases")
        self._bridge = bridge
        self._snapshot = snapshot
        self._phase = phase
        self._token = token
        self._reuse_plan = reuse_plan

    @Slot()
    def execute(self) -> None:
        run_id = self._snapshot.run_id
        generation = self._snapshot.generation
        self.state_changed.emit(run_id, generation, "running", self._phase.value)

        def report(fraction: float, message: str) -> None:
            self.progress_changed.emit(run_id, generation, float(fraction), str(message))

        try:
            operation: Callable[..., PipelineOutcome]
            operation = self._bridge.preflight if self._phase is PipelinePhase.PREFLIGHT else self._bridge.run
            if self._phase is PipelinePhase.RUN and self._reuse_plan is not None:
                outcome = operation(
                    self._snapshot,
                    cancel=self._token,
                    progress=report,
                    reuse_plan=self._reuse_plan,
                )
            else:
                outcome = operation(self._snapshot, cancel=self._token, progress=report)
            if self._token.is_requested() and outcome.status in {
                PipelineStatus.COMPLETE,
                PipelineStatus.PARTIAL,
            }:
                outcome = replace(outcome, status=PipelineStatus.CANCELLED)
            self.completed.emit(run_id, generation, outcome)
            self.state_changed.emit(run_id, generation, outcome.status.value, self._phase.value)
        except Exception as exc:
            details = traceback.format_exc()
            self.failed.emit(run_id, generation, type(exc).__name__, str(exc) + "\n\n" + details)
            self.state_changed.emit(run_id, generation, "failed", self._phase.value)
        finally:
            self.finished.emit(run_id, generation)


class TaskManager(QObject):
    """Runs one backend task at a time; signals carry the run and generation identity."""

    progress_changed = Signal(str, int, float, str)
    state_changed = Signal(str, int, str, str)
    completed = Signal(str, int, object)
    failed = Signal(str, int, str, str)
    task_finished = Signal(str, int)
    idle = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active: tuple[QThread, PipelineTaskWorker, CancellationToken, RunSnapshot] | None = None

    @property
    def active(self) -> bool:
        return self._active is not None

    def start(
        self,
        snapshot: RunSnapshot,
        *,
        phase: PipelinePhase,
        bridge: PipelineBridge | None = None,
        reuse_plan: MetricReusePlan | None = None,
    ) -> CancellationToken:
        if self._active is not None:
            raise RuntimeError("A pipeline task is already active")
        token = CancellationToken()
        thread = QThread(self)
        worker = PipelineTaskWorker(bridge or PipelineBridge(), snapshot, phase, token, reuse_plan)
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)
        worker.progress_changed.connect(self.progress_changed)
        worker.state_changed.connect(self.state_changed)
        worker.completed.connect(self.completed)
        worker.failed.connect(self.failed)
        worker.finished.connect(self.task_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_active)
        self._active = (thread, worker, token, snapshot)
        thread.start()
        return token

    @Slot()
    def _clear_active(self) -> None:
        active = self._active
        self._active = None
        if active is not None:
            thread = active[0]
            thread.deleteLater()
        self.idle.emit()

    def cancel(self) -> bool:
        if self._active is None:
            return False
        token = self._active[2]
        snapshot = self._active[3]
        token.cancel()
        self.state_changed.emit(snapshot.run_id, snapshot.generation, "cancel_requested", "等待当前计算结束")
        return True


class _OperationWorker(QObject):
    progress = Signal(int, float, str)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    finished = Signal()

    def __init__(self, generation, operation, token):
        super().__init__()
        self.generation, self.operation, self.token = generation, operation, token

    @Slot()
    def execute(self):
        result = None
        try:
            result = self.operation(self.token, lambda fraction, message: self.progress.emit(self.generation, float(fraction), str(message)))
            # Export/report receipts are produced only after their atomic output
            # promotion. A late cancellation must not relabel committed bytes as
            # cancelled or discard the destination receipt.
            committed = getattr(result, "commit_complete", False) is True
            if self.token() and not committed:
                if hasattr(result, "close"):
                    result.close()
                self.cancelled.emit(self.generation)
            else:
                self.completed.emit(self.generation, result)
        except Exception as exc:
            if self.token():
                self.cancelled.emit(self.generation)
            else:
                self.failed.emit(self.generation, f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class OperationTaskManager(QObject):
    """One archive/export operation with logical cancellation and late-result guards."""

    progress = Signal(int, float, str)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = None
        self._generation = 0

    @property
    def active(self):
        return self._active is not None

    @property
    def generation(self):
        return self._generation

    def start(self, operation):
        if self.active:
            raise RuntimeError("An archive/export operation is already active")
        self._generation += 1
        token = CancellationToken()
        thread = QThread(self)
        worker = _OperationWorker(self._generation, operation, token)
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)
        worker.progress.connect(self.progress)
        worker.completed.connect(self._deliver)
        worker.failed.connect(self.failed)
        worker.cancelled.connect(self.cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear)
        self._active = (thread, worker, token)
        thread.start()
        return self._generation

    @Slot(int, object)
    def _deliver(self, generation, result):
        committed = getattr(result, "commit_complete", False) is True
        if generation != self._generation or not self._active or (self._active[2]() and not committed):
            if hasattr(result, "close"):
                result.close()
            return
        self.completed.emit(generation, result)

    def cancel(self):
        if not self._active:
            return False
        self._active[2].cancel()
        return True

    @Slot()
    def _clear(self):
        active, self._active = self._active, None
        if active:
            active[0].deleteLater()
        self.idle.emit()

