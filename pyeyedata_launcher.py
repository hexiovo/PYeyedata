"""Explicit GUI entry point; bootstrap user paths before package imports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys
import traceback

from pyeyedata_bootstrap import prepare_gui_paths, show_startup_error, user_data_root


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


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--diagnostic-config":
            show_startup_error("仅支持 --diagnostic-config <本地 JSON 配置文件>。")
            return 2
        try:
            prepare_gui_paths()
        except Exception as exc:
            _write_startup_exception_log(exc, context="diagnostic-bootstrap")
            show_startup_error(str(exc))
            return 2
        try:
            from pyeyedata.gui.diagnostic import run_diagnostic
            return int(run_diagnostic(arguments[1]))
        except Exception as exc:
            _write_startup_exception_log(exc, context="diagnostic-launch")
            show_startup_error(f"{type(exc).__name__}: {exc}")
            return 2

    try:
        prepare_gui_paths()
    except Exception as exc:
        _write_startup_exception_log(exc, context="gui-bootstrap")
        show_startup_error(str(exc))
        return 2

    try:
        from pyeyedata.gui.app import main as run_gui
        return int(run_gui())
    except Exception as exc:
        _write_startup_exception_log(exc, context="gui-launch")
        show_startup_error(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
