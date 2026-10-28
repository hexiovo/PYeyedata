"""Entry points for launching the local PySide6 desktop application."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pyeyedata.app_info import APP_NAME, APP_VERSION
from pyeyedata.resource_paths import resource_path
from pyeyedata_startup import (
    WINDOWS_APP_USER_MODEL_ID,
    bind_windows_taskbar_identity,
    set_windows_application_identity,
)
from pyeyedata_bootstrap import prepare_gui_paths

from .main_window import MainWindow
from .theme import install_theme


_WINDOWS_RELAUNCH_ICON_RESOURCE: str | None = None
_WINDOWS_TASKBAR_BINDING = None


def _set_windows_application_identity() -> None:
    """Give source and frozen launches the same taskbar identity before Qt starts."""
    set_windows_application_identity()


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    global _WINDOWS_RELAUNCH_ICON_RESOURCE
    prepare_gui_paths()
    _set_windows_application_identity()
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    with resource_path("branding/pyeyedata.ico") as icon_path:
        icon = QIcon(str(icon_path))
        if icon.isNull():
            raise RuntimeError("Cannot load the bundled PYeyedata application icon")
        app.setWindowIcon(icon)
        if sys.platform == "win32":
            if getattr(sys, "frozen", False):
                # PyInstaller writes the first RT_GROUP_ICON as resource ID 1.
                _WINDOWS_RELAUNCH_ICON_RESOURCE = f"{Path(sys.executable).resolve()},-1"
            else:
                # Use the stable package file, never an importlib temporary path.
                source_icon = (
                    Path(__file__).resolve().parents[1]
                    / "resources"
                    / "branding"
                    / "pyeyedata.ico"
                )
                _WINDOWS_RELAUNCH_ICON_RESOURCE = (
                    f"{source_icon.resolve()},0" if source_icon.is_file() else None
                )
    install_theme(app)
    return app


def main(
    argv: Sequence[str] | None = None,
    *,
    on_ready: Callable[[], None] | None = None,
) -> int:
    global _WINDOWS_TASKBAR_BINDING
    app = create_application(argv)
    window = MainWindow()
    window.setWindowIcon(app.windowIcon())
    if sys.platform == "win32" and _WINDOWS_RELAUNCH_ICON_RESOURCE is not None:
        _WINDOWS_TASKBAR_BINDING = bind_windows_taskbar_identity(
            window,
            app,
            icon_resource=_WINDOWS_RELAUNCH_ICON_RESOURCE,
        )
    window.show()
    app.processEvents()
    if on_ready is not None:
        on_ready()
    return app.exec()
