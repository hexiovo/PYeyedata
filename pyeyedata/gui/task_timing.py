"""Small, Qt-independent timing helpers for GUI background tasks.

Estimates use only successful/partial completions held in this process and are
heuristics, not a promise of the remaining time. With no matching samples the
task uses the explicitly heuristic defaults below. Cancellation and failure
must not be recorded as completed samples.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass


# 初始值是启发值，不是性能保证；后续估计仅使用本次会话内的完成样本。
DEFAULT_SECONDS: dict[str, float] = {
    "preview": 5.0,
    "inspect": 10.0,
    "import": 10.0,
    "preflight": 10.0,
    "run": 30.0,
    "prepare": 15.0,
    "save": 10.0,
    "load": 15.0,
    "export": 10.0,
    "report": 30.0,
    "batch_export": 20.0,
}

_MIN_SAMPLE_SECONDS = 0.1
_OVERTIME_FLOOR_SECONDS = 1.0
_ACTIVE_STATUSES = frozenset({"running", "cancelling"})
_NON_TRAINING_STATUSES = frozenset({"failed", "cancelled"})


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _require_workload_key(value: str | None) -> str | None:
    if value is None:
        return None
    return _require_text(value, "workload_key")


def _finite_number(value: object, name: str, *, allow_zero: bool) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number < 0 or (not allow_zero and number == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _clock_number(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError("clock value must be a finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("clock value must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError("clock value must be finite")
    return number


class TimingEstimator:
    """Keep bounded, isolated completion samples and produce heuristic ETAs."""

    def __init__(
        self,
        default_seconds: Mapping[str, float] | None = None,
        sample_limit: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(sample_limit, bool) or not isinstance(sample_limit, int):
            raise TypeError("sample_limit must be an integer")
        if sample_limit <= 0:
            raise ValueError("sample_limit must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")

        defaults = dict(DEFAULT_SECONDS)
        if default_seconds is not None:
            if not isinstance(default_seconds, Mapping):
                raise TypeError("default_seconds must be a mapping or None")
            for raw_kind, raw_seconds in default_seconds.items():
                kind = _require_text(raw_kind, "kind")
                defaults[kind] = _finite_number(
                    raw_seconds, f"default_seconds[{kind!r}]", allow_zero=False
                )

        self._defaults = defaults
        self._sample_limit = sample_limit
        self._clock = clock
        self._samples: dict[tuple[str, str | None], deque[float]] = {}

    def now(self) -> float:
        """Read the injected monotonic clock, rejecting unusable clock values."""
        raw_now = self._clock()
        return _clock_number(raw_now)

    def _validated_bucket(
        self, kind: str, workload_key: str | None
    ) -> tuple[str, str | None]:
        normalized_kind = _require_text(kind, "kind")
        normalized_key = _require_workload_key(workload_key)
        if normalized_kind not in self._defaults:
            raise ValueError(f"no default estimate configured for kind {normalized_kind!r}")
        return normalized_kind, normalized_key

    def estimate(self, kind: str, workload_key: str | None = None) -> float:
        """Return the mean of recent matching samples or the heuristic default."""
        bucket = self._validated_bucket(kind, workload_key)
        samples = self._samples.get(bucket)
        if not samples:
            return self._defaults[bucket[0]]
        # Divide before summing so a finite mean remains finite for large samples.
        estimate = math.fsum(sample / len(samples) for sample in samples)
        return max(_MIN_SAMPLE_SECONDS, estimate)

    def sample_count(self, kind: str, workload_key: str | None = None) -> int:
        """Return the number of retained samples for exactly this kind/key pair."""
        bucket = self._validated_bucket(kind, workload_key)
        samples = self._samples.get(bucket)
        return len(samples) if samples is not None else 0

    def record_completed(
        self,
        kind: str,
        elapsed_seconds: float,
        workload_key: str | None = None,
    ) -> None:
        """Store one successful/partial duration, clamping tiny durations to 0.1s."""
        bucket = self._validated_bucket(kind, workload_key)
        elapsed = _finite_number(elapsed_seconds, "elapsed_seconds", allow_zero=True)
        sample = max(_MIN_SAMPLE_SECONDS, elapsed)
        samples = self._samples.get(bucket)
        if samples is None:
            samples = deque(maxlen=self._sample_limit)
            self._samples[bucket] = samples
        samples.append(sample)


@dataclass(frozen=True, slots=True)
class TimingSnapshot:
    """A stable view of phase timing and its current forecast."""

    phase: str
    elapsed_seconds: float
    remaining_seconds: float | None
    estimate_seconds: float
    sample_count: int
    initial: bool
    overtime: bool
    status: str


class TaskTiming:
    """Track one task's phase, cancellation state, and frozen terminal timing."""

    def __init__(
        self,
        estimator: TimingEstimator,
        kind: str,
        phase: str,
        *,
        identity: object = None,
        workload_key: str | None = None,
    ) -> None:
        if not isinstance(estimator, TimingEstimator):
            raise TypeError("estimator must be a TimingEstimator")
        self._estimator = estimator
        self._kind, self._workload_key = estimator._validated_bucket(kind, workload_key)
        self._phase = _require_text(phase, "phase")
        self._identity = identity
        self._start_time = estimator.now()
        self._initial_estimate = estimator.estimate(self._kind, self._workload_key)
        self._sample_count = estimator.sample_count(self._kind, self._workload_key)
        self._initial = self._sample_count == 0
        self._status = "running"
        self._terminal_elapsed: float | None = None
        self._terminal_estimate: float | None = None
        self._overtime = False

    @property
    def identity(self) -> object:
        return self._identity

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def phase(self) -> str:
        return self._phase

    def update_phase(self, message: str) -> None:
        """Update the visible phase unless cancellation or completion has begun."""
        phase = _require_text(message, "message")
        if self._status != "running" or self._terminal_elapsed is not None:
            return
        self._phase = phase

    def request_cancel(self) -> None:
        """Mark cancellation as pending; elapsed time continues until the worker stops."""
        if self._status == "running":
            self._status = "cancelling"

    @staticmethod
    def _forecast(initial_estimate: float, elapsed: float) -> tuple[float, float, bool]:
        if elapsed < initial_estimate:
            return initial_estimate, max(0.0, initial_estimate - elapsed), False
        # Do not infer completion from elapsed time. Keep a positive conservative
        # cushion and let the forecast move forward as the task continues.
        remaining = max(initial_estimate * 0.25, _OVERTIME_FLOOR_SECONDS)
        return elapsed + remaining, remaining, True

    def snapshot(self) -> TimingSnapshot:
        status = self._status
        phase = self._phase
        terminal_elapsed = self._terminal_elapsed
        terminal_estimate = self._terminal_estimate
        terminal_overtime = self._overtime
        if terminal_elapsed is not None:
            assert terminal_estimate is not None
            return TimingSnapshot(
                phase=phase,
                elapsed_seconds=terminal_elapsed,
                remaining_seconds=0.0,
                estimate_seconds=terminal_estimate,
                sample_count=self._sample_count,
                initial=self._initial,
                overtime=terminal_overtime,
                status=status,
            )

        elapsed = max(0.0, self._estimator.now() - self._start_time)
        estimate, remaining, overtime = self._forecast(self._initial_estimate, elapsed)
        return TimingSnapshot(
            phase=phase,
            elapsed_seconds=elapsed,
            remaining_seconds=None if status == "cancelling" else remaining,
            estimate_seconds=estimate,
            sample_count=self._sample_count,
            initial=self._initial,
            overtime=overtime,
            status=status,
        )

    def finish(self, status: str, *, train: bool = False) -> None:
        """Freeze the first terminal result and optionally learn its duration."""
        terminal_status = _require_text(status, "status")
        folded_status = terminal_status.strip().casefold()
        if folded_status in _ACTIVE_STATUSES:
            raise ValueError("finish status must be terminal")
        if not isinstance(train, bool):
            raise TypeError("train must be a bool")

        if self._terminal_elapsed is not None:
            return
        elapsed = max(0.0, self._estimator.now() - self._start_time)
        estimate, _remaining, overtime = self._forecast(self._initial_estimate, elapsed)
        self._terminal_elapsed = elapsed
        self._terminal_estimate = estimate
        self._overtime = overtime
        self._status = terminal_status.strip()
        if train and folded_status not in _NON_TRAINING_STATUSES:
            self._estimator.record_completed(self._kind, elapsed, self._workload_key)


def format_duration(seconds: float) -> str:
    """Format a non-negative duration in Chinese without hiding positive time."""
    value = _finite_number(seconds, "seconds", allow_zero=True)
    if value == 0:
        return "0秒"
    if value < 60:
        rounded_seconds = max(1, math.floor(value + 0.5))
        return f"{rounded_seconds}秒"

    if value < 3600:
        minutes = int(value // 60)
        rounded_seconds = math.floor(value - minutes * 60 + 0.5)
        if rounded_seconds >= 60:
            minutes += 1
            rounded_seconds = 0
        if minutes >= 60:
            return "1小时"
        suffix = f"{rounded_seconds}秒" if rounded_seconds else ""
        return f"{minutes}分{suffix}"

    hours = int(value // 3600)
    minutes = math.floor((value - hours * 3600) / 60 + 0.5)
    if minutes >= 60:
        hours += 1
        minutes = 0
    suffix = f"{minutes}分" if minutes else ""
    return f"{hours}小时{suffix}"


_STATUS_LABELS = {
    "complete": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def format_timing(snapshot: TimingSnapshot) -> str:
    """Render a task timing snapshot for Chinese GUI status text."""
    if not isinstance(snapshot, TimingSnapshot):
        raise TypeError("snapshot must be a TimingSnapshot")

    elapsed = format_duration(snapshot.elapsed_seconds)
    if snapshot.status == "cancelling":
        return f"取消中，等待后台停止 · 原阶段：{snapshot.phase} · 已用 {elapsed}"

    if snapshot.status == "running":
        if snapshot.remaining_seconds is None:
            raise ValueError("a running snapshot must include remaining_seconds")
        source = (
            "初始估计"
            if snapshot.initial
            else f"基于{snapshot.sample_count}次完成"
        )
        text = (
            f"当前阶段：{snapshot.phase} · 已用 {elapsed} · 预计剩余 "
            f"{format_duration(snapshot.remaining_seconds)}（{source}）"
        )
        if snapshot.overtime:
            text += " · 已超过估计，正在更新"
        return text

    status_key = snapshot.status.strip().casefold()
    status_label = _STATUS_LABELS.get(status_key, snapshot.status)
    if status_label != snapshot.status:
        status_text = f"{status_label}（{snapshot.status}）"
    else:
        status_text = status_label
    return f"当前阶段：{snapshot.phase} · 状态：{status_text} · 实际耗时 {elapsed}"

