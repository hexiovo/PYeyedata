"""Entry points for launching the local PySide6 desktop application."""
from __future__ import annotations

import sys
from typing import Sequence

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pyeyedata.app_info import APP_NAME, APP_VERSION
from pyeyedata.resource_paths import resource_path
from pyeyedata_bootstrap import prepare_gui_paths

from .main_window import MainWindow
from .theme import install_theme


WINDOWS_APP_USER_MODEL_ID = "PYeyedata.Desktop"


def _set_windows_application_identity() -> None:
    """Give source and frozen launches the same taskbar identity before Qt starts."""
    if sys.platform != "win32":
        return
    import ctypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
    set_app_id.argtypes = [ctypes.c_wchar_p]
    set_app_id.restype = ctypes.c_long
    result = set_app_id(WINDOWS_APP_USER_MODEL_ID)
    if result != 0:
        raise OSError(f"Cannot set Windows application identity: HRESULT {result:#x}")


def create_application(argv: Sequence[str] | None = None) -> QApplication:
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
    install_theme(app)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = create_application(argv)
    window = MainWindow()
    window.show()
    return app.exec()
