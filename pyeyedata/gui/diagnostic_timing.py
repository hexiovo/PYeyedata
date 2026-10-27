"""Deterministic diagnostics for task timing estimates and GUI lifecycle hooks."""

from __future__ import annotations

from typing import Any

from pyeyedata.gui.task_timing import (
    TaskTiming,
    TimingEstimator,
    format_duration,
    format_timing,
)


def _check_window_timing(window: Any) -> dict[str, bool]:
    """Exercise MainWindow's timing identity guards without starting work."""
    required = (
        "_start_timing",
        "_update_timing_phase",
        "_finish_timing",
        "_refresh_timing_display",
        "_active_timing",
        "_timing_identity",
        "_timing_timer",
        "timing_label",
    )
    missing = [name for name in required if not hasattr(window, name)]
    if missing:
        raise AttributeError("MainWindow is missing timing members: " + ", ".join(missing))

    timer = window._timing_timer
    old_active = window._active_timing
    old_identity = window._timing_identity
    old_label = window.timing_label.text()
    if old_active is not None and old_active.snapshot().status in {"running", "cancelling"}:
        # Do not replace a live operation's timing state during a diagnostic.
        return {"gui_window_idle_for_diagnostic": False}

    was_active = timer.isActive()
    timer.stop()
    checks: dict[str, bool] = {}
    old_token = ("diagnostic", "cancel")
    new_token = ("diagnostic", "retry")
    try:
        window._start_timing(
            "run", "诊断取消阶段", identity=old_token, workload_key="diagnostic"
        )
        old_timing = window._active_timing
        window._refresh_timing_display()
        checks["gui_start_shows_real_phase_and_eta"] = bool(
            old_timing is not None
            and old_timing.snapshot().status == "running"
            and "诊断取消阶段" in window.timing_label.text()
            and "预计剩余" in window.timing_label.text()
        )

        window._update_timing_phase("诊断取消等待", identity=old_token)
        window._refresh_timing_display()
        checks["gui_matching_phase_update_is_visible"] = "诊断取消等待" in window.timing_label.text()

        old_timing.request_cancel()
        window._refresh_timing_display()
        cancel_text = window.timing_label.text()
        checks["gui_cancelling_hides_eta"] = bool(
            old_timing.snapshot().status == "cancelling"
            and old_timing.snapshot().remaining_seconds is None
            and "取消中" in cancel_text
            and "预计剩余" not in cancel_text
        )
        window._update_timing_phase("迟到阶段不得覆盖", identity=old_token)
        window._refresh_timing_display()
        checks["gui_late_phase_does_not_replace_cancelling"] = (
            old_timing.phase == "诊断取消等待"
            and "诊断取消等待" in window.timing_label.text()
            and "迟到阶段不得覆盖" not in window.timing_label.text()
        )
        window._finish_timing("cancelled", train=True, identity=old_token)
        cancelled_snapshot = old_timing.snapshot()
        checks["gui_cancel_finish_is_terminal"] = bool(
            cancelled_snapshot.status == "cancelled"
            and cancelled_snapshot.remaining_seconds == 0.0
            and "已取消" in window.timing_label.text()
            and "实际耗时" in window.timing_label.text()
        )

        window._start_timing(
            "run", "诊断重试阶段", identity=new_token, workload_key="diagnostic"
        )
        retry_timing = window._active_timing
        window._update_timing_phase("旧任务迟到进度", identity=old_token)
        window._finish_timing("failed", train=True, identity=old_token)
        checks["gui_stale_identity_cannot_change_retry"] = bool(
            retry_timing is not None
            and window._active_timing is retry_timing
            and retry_timing.snapshot().status == "running"
            and retry_timing.phase == "诊断重试阶段"
        )

        window._update_timing_phase("诊断重试分析", identity=new_token)
        window._refresh_timing_display()
        checks["gui_retry_phase_update_is_visible"] = "诊断重试分析" in window.timing_label.text()
        window._finish_timing("partial", train=False, identity=new_token)
        retry_terminal = retry_timing.snapshot()
        window._finish_timing("failed", train=True, identity=new_token)
        checks["gui_retry_partial_and_duplicate_finish_are_preserved"] = bool(
            retry_terminal.status == "partial"
            and retry_terminal.remaining_seconds == 0.0
            and retry_timing.snapshot().status == "partial"
            and "部分完成" in window.timing_label.text()
        )
        checks["gui_terminal_timer_stops"] = not timer.isActive()
    finally:
        timer.stop()
        window._active_timing = old_active
        window._timing_identity = old_identity
        window.timing_label.setText(old_label)
        if was_active:
            timer.start()

    return checks


def run_timing_diagnostic(window: Any | None = None) -> dict[str, Any]:
    """Run bounded, no-file timing checks; optionally smoke-test a MainWindow.

    The local clock is injected, so estimates, overtime, cancellation, retries,
    and terminal freezing are checked without waiting for wall-clock timers.
    ``window`` is optional and is used only by reliability diagnostics to verify
    the current MainWindow timing hooks. This helper never starts a backend job
    or changes scientific data.
    """
    clock = [0.0]
    estimator = TimingEstimator(
        {"run": 4.0, "preview": 2.0}, sample_limit=2, clock=lambda: clock[0]
    )
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    first = TaskTiming(
        estimator, "run", "预检", identity="sample-1", workload_key="small"
    )
    initial = first.snapshot()
    checks["heuristic_initial_estimate"] = bool(
        initial.initial
        and initial.sample_count == 0
        and initial.estimate_seconds == 4.0
        and initial.remaining_seconds == 4.0
    )
    first.update_phase("参数检查")
    clock[0] = 2.0
    halfway = first.snapshot()
    checks["phase_and_remaining_update"] = bool(
        halfway.phase == "参数检查" and halfway.remaining_seconds == 2.0
    )
    first.finish("complete", train=True)
    first_terminal = first.snapshot()
    clock[0] = 100.0
    first.finish("failed", train=True)
    duplicate_terminal = first.snapshot()
    checks["first_completion_trains_once_and_freezes"] = bool(
        estimator.sample_count("run", "small") == 1
        and first_terminal.status == "complete"
        and first_terminal.remaining_seconds == 0.0
        and duplicate_terminal.status == "complete"
        and duplicate_terminal.elapsed_seconds == first_terminal.elapsed_seconds
        and estimator.sample_count("run", "small") == 1
    )

    second = TaskTiming(
        estimator, "run", "分析", identity="sample-2", workload_key="small"
    )
    second_start = second.snapshot()
    clock[0] = 104.0
    second.finish("partial", train=True)
    checks["one_sample_calibrates_and_partial_trains"] = bool(
        not second_start.initial
        and second_start.sample_count == 1
        and second_start.estimate_seconds == 2.0
        and estimator.sample_count("run", "small") == 2
        and estimator.estimate("run", "small") == 3.0
        and "部分完成" in format_timing(second.snapshot())
    )
    estimator.record_completed("run", 8.0, "small")
    checks["recent_sample_window_is_bounded"] = bool(
        estimator.sample_count("run", "small") == 2
        and estimator.estimate("run", "small") == 6.0
    )

    checks["kind_and_workload_are_isolated"] = bool(
        estimator.sample_count("run", "large") == 0
        and estimator.estimate("run", "large") == 4.0
        and estimator.sample_count("import", "small") == 0
        and estimator.estimate("import", "small") == 10.0
    )

    overtime = TaskTiming(
        estimator, "run", "分析", identity="overtime", workload_key="overtime"
    )
    clock[0] += 4.0
    overtime_first = overtime.snapshot()
    clock[0] += 1.0
    overtime_later = overtime.snapshot()
    checks["overtime_keeps_positive_revised_eta"] = bool(
        overtime_first.overtime
        and overtime_first.remaining_seconds is not None
        and overtime_first.remaining_seconds > 0
        and overtime_later.overtime
        and overtime_later.remaining_seconds is not None
        and overtime_later.remaining_seconds > 0
        and overtime_later.estimate_seconds > overtime_first.estimate_seconds
        and "已超过估计，正在更新" in format_timing(overtime_later)
    )

    cancelling = TaskTiming(
        estimator, "run", "导出", identity="cancel-1", workload_key="cancelled"
    )
    cancelling.request_cancel()
    cancelling.update_phase("取消后迟到进度")
    cancel_snapshot = cancelling.snapshot()
    cancel_text = format_timing(cancel_snapshot)
    clock[0] += 2.0
    cancelling.finish("cancelled", train=True)
    cancelling.finish("complete", train=True)
    checks["cancellation_waits_without_eta_and_does_not_train"] = bool(
        cancel_snapshot.status == "cancelling"
        and cancel_snapshot.phase == "导出"
        and cancel_snapshot.remaining_seconds is None
        and "取消中，等待后台停止" in cancel_text
        and "预计剩余" not in cancel_text
        and cancelling.snapshot().status == "cancelled"
        and cancelling.snapshot().phase == "导出"
        and estimator.sample_count("run", "cancelled") == 0
    )

    failed = TaskTiming(
        estimator, "run", "运行", identity="retry-1", workload_key="retry"
    )
    clock[0] += 2.0
    failed.finish("failed", train=True)
    retry = TaskTiming(
        estimator, "run", "运行", identity="retry-2", workload_key="retry"
    )
    clock[0] += 3.0
    retry.finish("complete", train=True)
    checks["failed_attempt_not_trained_retry_trains_once"] = bool(
        estimator.sample_count("run", "retry") == 1
        and estimator.estimate("run", "retry") == 3.0
        and failed.identity != retry.identity
    )

    preview_a = TaskTiming(
        estimator, "preview", "预览来源 A", identity="preview-A", workload_key="source-A"
    )
    preview_b = TaskTiming(
        estimator, "preview", "预览来源 B", identity="preview-B", workload_key="source-B"
    )
    clock[0] += 1.0
    preview_a.update_phase("读取来源 A")
    preview_b.update_phase("读取来源 B")
    preview_a.finish("complete", train=True)
    preview_b_running = preview_b.snapshot()
    checks["concurrent_previews_keep_independent_timing"] = bool(
        preview_a.snapshot().status == "complete"
        and preview_b_running.status == "running"
        and preview_a.identity != preview_b.identity
        and estimator.sample_count("preview", "source-A") == 1
        and estimator.sample_count("preview", "source-B") == 0
        and preview_b_running.phase == "读取来源 B"
    )

    checks["duration_rounding_keeps_positive_time_visible"] = bool(
        format_duration(0.01) == "1秒"
        and format_duration(90) == "1分30秒"
        and format_duration(7200) == "2小时"
    )
    details.update({
        "retained_small_run_mean_seconds": estimator.estimate("run", "small"),
        "retry_sample_count": estimator.sample_count("run", "retry"),
        "overtime_remaining_seconds": overtime_later.remaining_seconds,
        "preview_a_text": format_timing(preview_a.snapshot()),
        "preview_b_text": format_timing(preview_b_running),
    })

    if window is not None:
        checks.update(_check_window_timing(window))

    failures = [name for name, passed in checks.items() if not passed]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "details": details,
    }

