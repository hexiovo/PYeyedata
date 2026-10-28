"""Bounded regressions for mixed runtime trees and truthful cache feedback."""
from pathlib import Path
import io
import sys
from types import ModuleType
from unittest.mock import patch

from pyeyedata_bootstrap import show_startup_error, verify_runtime_sources
from pyeyedata.execution_cache import ExecutionCache
from .cache_feedback import cache_miss_feedback


def run_runtime_checks() -> dict:
    checks = {}
    origins = verify_runtime_sources()
    checks["actual_loaded_tree_recorded"] = "pyeyedata.gui.main_window" in origins
    # Reproduce an old dependency already loaded before the formal entry call.
    fake = ModuleType("pyeyedata.old_helper")
    fake.__file__ = str(Path(origins["pyeyedata"]).parent.parent / "old-copy" / "old_helper.py")
    with patch.dict(sys.modules, {fake.__name__: fake}):
        try:
            verify_runtime_sources()
        except RuntimeError as exc:
            checks["old_dependency_rejected"] = fake.__name__ in str(exc) and fake.__file__ in str(exc)
        else:
            checks["old_dependency_rejected"] = False
    package = sys.modules["pyeyedata"]
    with patch.object(package, "__path__", [*package.__path__, str(Path.cwd() / "old-copy")]):
        try:
            verify_runtime_sources()
        except RuntimeError:
            checks["mixed_package_search_path_rejected"] = True
        else:
            checks["mixed_package_search_path_rejected"] = False
    checks["original_tree_restored"] = verify_runtime_sources() == origins

    bundle_root = Path(origins["pyeyedata"]).parent.parent
    compiled_origin = str(bundle_root / "pyeyedata" / "__init__.pyc")
    with (patch.object(sys, "frozen", True, create=True),
          patch.object(sys, "_MEIPASS", str(bundle_root), create=True),
          patch.object(package, "__file__", compiled_origin)):
        checks["frozen_compiled_origin_accepted_in_exact_tree"] = (
            verify_runtime_sources()["pyeyedata"] == compiled_origin
        )
        with patch.object(package, "__file__", str(bundle_root / "old-copy" / "__init__.pyc")):
            try:
                verify_runtime_sources()
            except RuntimeError:
                checks["frozen_compiled_origin_outside_tree_rejected"] = True
            else:
                checks["frozen_compiled_origin_outside_tree_rejected"] = False
    if not getattr(sys, "frozen", False):
        with patch.object(package, "__file__", compiled_origin):
            try:
                verify_runtime_sources()
            except RuntimeError:
                checks["source_does_not_accept_compiled_origin"] = True
            else:
                checks["source_does_not_accept_compiled_origin"] = False
    captured = io.StringIO()
    with patch("sys.stderr", captured), patch("pyeyedata_bootstrap.ctypes.windll.user32.MessageBoxW") as dialog:
        show_startup_error("synthetic diagnostic startup failure", interactive=False)
        checks["diagnostic_failure_is_noninteractive"] = (
            not dialog.called and "synthetic diagnostic startup failure" in captured.getvalue()
        )

    if not getattr(sys, "frozen", False):
        # Exercise both real launcher branches, not just the guard helper.
        # The frozen entry script is not an importable Python module; its
        # successful branch is exercised by run_checks --exe when releasing.
        import pyeyedata_launcher as launcher
        reports = []
        with (patch.dict(sys.modules, {fake.__name__: fake}),
              patch.object(launcher, "show_startup_error", lambda message, **kwargs: reports.append(message)),
              patch.object(launcher, "_write_startup_exception_log"),
              patch.object(launcher, "prepare_gui_paths"),
              patch("pyeyedata.gui.diagnostic.run_diagnostic") as dispatch,
              patch("pyeyedata_startup.StartupIndicator") as indicator):
            diagnostic_code = launcher.main(["--diagnostic-config", "unused.json"])
            gui_code = launcher.main([])
            checks["formal_launcher_blocks_old_dependency_before_work"] = (
                diagnostic_code == 2 and gui_code == 1
                and not dispatch.called and not indicator.called
                and len(reports) == 2 and all(fake.__file__ in report for report in reports)
            )

    cache = ExecutionCache()
    first = cache.begin(run_id="synthetic-old", phase="run")
    key = first.key("base", {"input": 1})
    first.put("base", key, {"value": [1]})
    first.finish(complete=True)
    # Deliberately corrupt private retained data; no user history is involved.
    cache._entries[("base", key)].value["value"][0] = 2
    second = cache.begin(run_id="synthetic-new", phase="run")
    checks["corrupt_cache_never_reused"] = second.get("base", key) is None
    feedback = " ".join(cache_miss_feedback(second.audit))
    checks["corruption_and_recalculation_visible"] = "完整性核验失败" in feedback and "按当前输入计算" in feedback
    second.finish(complete=False)
    if not all(checks.values()):
        raise RuntimeError(f"Runtime regression failed: {checks}")
    return checks
