"""Repeatable environment checks without downloading or changing packages."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtTest import QTest

from pyeyedata import environment as env
from . import environment_dialog as ui


def run_environment_checks(app, workspace: Path) -> dict:
    checks = {}
    workspace.mkdir(parents=True, exist_ok=True)
    pins_path = workspace / "pins.txt"
    pins_path.write_text("numpy==1.26.4\n", encoding="utf-8")
    checks["exact_pins_read"] = env.read_pins(pins_path) == {"numpy": "1.26.4"}
    for text in ("numpy>=1", "numpy==1\nNUMPY==2", "# empty"):
        pins_path.write_text(text, encoding="utf-8")
        try:
            env.read_pins(pins_path)
        except ValueError:
            continue
        raise RuntimeError("Environment probe accepted malformed or duplicate pins")
    checks["invalid_pins_rejected"] = True
    with patch.object(env.metadata, "version", side_effect=metadata.PackageNotFoundError()):
        report = env.inspect_environment({"numpy": "1.26.4"}, frozen=True)
    checks["frozen_missing_metadata_uses_loaded_version"] = report["rows"][0]["state"] == "ok"
    with patch.object(env.metadata, "version", return_value="wrong"):
        report = env.inspect_environment({"numpy": "1.26.4"}, frozen=True)
    checks["version_mismatch_reported"] = report["rows"][0]["state"] == "mismatch"
    with (patch.object(env.metadata, "version", return_value="1.26.4"),
          patch.object(env.importlib, "import_module", side_effect=ImportError("synthetic DLL error"))):
        report = env.inspect_environment({"numpy": "1.26.4"}, frozen=True)
    checks["import_failure_reported"] = report["rows"][0]["state"] == "broken"
    with (patch.object(env.metadata, "version", side_effect=metadata.PackageNotFoundError()),
          patch.object(env.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="broken dependency", stderr=""))):
        report = env.inspect_environment({"synthetic-missing": "1"})
    checks["missing_package_and_pip_conflict_reported"] = (
        report["rows"][0]["state"] == "missing" and not report["pip_ok"] and not report["passed"])
    root = Path(__file__).resolve().parents[2]
    for frozen, prefix in ((True, sys.prefix), (False, str(workspace / "system-python"))):
        with patch.object(sys, "prefix", prefix):
            try:
                env.install_commands(root, frozen=frozen, report={"supported": True})
            except ValueError:
                continue
        raise RuntimeError("Installer accepted an EXE or non-project Python")
    checks["frozen_and_system_python_install_refused"] = True

    def wait_idle(dialog):
        deadline = time.monotonic() + 190
        while dialog.busy and time.monotonic() < deadline:
            QTest.qWait(25)
        if dialog.busy:
            dialog.cancel()
            raise RuntimeError("Environment diagnostic did not finish in time")

    dialog = ui.EnvironmentDialog()
    dialog.show()
    app.processEvents()
    wait_idle(dialog)
    checks["real_environment_probe_completes"] = bool(dialog.report and dialog.report.get("passed"))
    checks["healthy_environment_needs_no_install"] = not dialog.install_button.isEnabled()
    dialog.grab().save(str(workspace / "environment-check.png"))
    if not getattr(sys, "frozen", False):
        commands = env.install_commands(root, frozen=False, report={"supported": True,
            "rows": [{"name": "numpy", "state": "broken"}]})
        checks["repair_uses_exact_project_requirements"] = (
            commands[1][-2:] == ["-r", str(root / "requirements.txt")]
            and "numpy==1.26.4" in commands[2] and "--force-reinstall" in commands[2])
        with patch.object(ui, "install_commands", return_value=[["-c", "print('synthetic install success')"]]):
            dialog.install()
            wait_idle(dialog)
        checks["successful_install_rechecks_and_requests_restart"] = (
            dialog.restart_required and dialog.report.get("passed") and "重新启动" in dialog.status.text())
        dialog.restart_required = False
        with patch.object(ui, "install_commands", return_value=[["-c", "import sys; print('synthetic failure'); sys.exit(1)"]]):
            dialog.install()
            wait_idle(dialog)
        checks["failed_install_not_reported_as_success"] = (
            "失败" in dialog.status.text() and "synthetic failure" in dialog.log.toPlainText())
        dialog.restart_required = False
        with patch.object(ui, "install_commands", return_value=[["-c", "import time; time.sleep(30)"]]):
            dialog.install()
            dialog.process.waitForStarted(5000)
            dialog.reject()
            checks["cannot_close_during_install"] = dialog.isVisible()
            dialog.cancel()
            wait_idle(dialog)
        checks["cancellation_keeps_incomplete_state"] = dialog.cancelled and "部分" in dialog.status.text()
    dialog.close()
    dialog.deleteLater()
    if not all(checks.values()):
        raise RuntimeError(f"Environment diagnostics failed: {checks}")
    return {"status": "passed", "checks": checks, "package_installation_performed": False}
