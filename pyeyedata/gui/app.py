"""Entry points for launching the local PySide6 desktop application."""
from __future__ import annotations

import sys
from typing import Sequence

from PySide6.QtWidgets import QApplication

from pyeyedata.app_info import APP_NAME, APP_VERSION
from pyeyedata_bootstrap import prepare_gui_paths

from .main_window import MainWindow
from .theme import install_theme


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    prepare_gui_paths()
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    install_theme(app)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = create_application(argv)
    window = MainWindow()
    window.show()
    return app.exec()
