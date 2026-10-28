"""Explicit GUI entry point; bootstrap user paths before package imports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys
import traceback
from typing import Any

from pyeyedata_bootstrap import prepare_gui_paths, show_startup_error, user_data_root, verify_runtime_sources


def _write_startup_exception_log(exc: Exception, *, context: str) -> None:
    """Write only the current startup exception to the user's diagnostics log."""
    try:
        occurred = datetime.now(timezone.utc)
        log_directory = user_data_root() / "logs" / "diagnostics"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / (
            f"startup-error-{occurred.strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}.json"
        )
        record = {
            "schema": "pyeyedata.startup-error/v1",
            "timestamp_utc": occurred.isoformat(),
            "context": context,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        }
        log_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # Logging must never hide the original startup failure or its dialog.
        pass


def _close_startup_indicator(indicator: Any | None) -> None:
    if indicator is None:
        return
    try:
        indicator.close()
    except Exception:
        # Preserve the startup exception or normal exit result.
        pass


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--diagnostic-config":
            show_startup_error("仅支持 --diagnostic-config <本地 JSON 配置文件>。", interactive=False)
            return 2
        try:
            prepare_gui_paths()
        except Exception as exc:
            _write_startup_exception_log(exc, context="diagnostic-bootstrap")
            show_startup_error(str(exc), interactive=False)
            return 2
        try:
            verify_runtime_sources()
            from pyeyedata.gui.diagnostic import run_diagnostic
            verify_runtime_sources()
            return int(run_diagnostic(arguments[1]))
        except Exception as exc:
            _write_startup_exception_log(exc, context="diagnostic-launch")
            show_startup_error(f"{type(exc).__name__}: {exc}", interactive=False)
            return 2

    startup_indicator = None
    try:
        verify_runtime_sources()
        from pyeyedata_startup import StartupIndicator

        startup_indicator = StartupIndicator()
    except Exception as exc:
        _write_startup_exception_log(exc, context="gui-startup-indicator")
        show_startup_error(f"{type(exc).__name__}: {exc}")
        return 1

    try:
        prepare_gui_paths()
    except Exception as exc:
        _close_startup_indicator(startup_indicator)
        _write_startup_exception_log(exc, context="gui-bootstrap")
        show_startup_error(str(exc))
        return 2

    try:
        from pyeyedata.gui.app import main as run_gui

        verify_runtime_sources()

        return int(run_gui(on_ready=startup_indicator.close))
    except Exception as exc:
        _close_startup_indicator(startup_indicator)
        _write_startup_exception_log(exc, context="gui-launch")
        show_startup_error(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        _close_startup_indicator(startup_indicator)


if __name__ == "__main__":
    raise SystemExit(main())
