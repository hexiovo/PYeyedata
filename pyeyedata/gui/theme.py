"""Compact, high-contrast palette for the desktop workflow."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


_FONT_REGISTRATION_ATTEMPTED = False
_REGISTERED_FONT_FAMILY: str | None = None


def _install_local_chinese_font() -> str | None:
    """Register an installed system font when Qt's platform database omits it."""
    global _FONT_REGISTRATION_ATTEMPTED, _REGISTERED_FONT_FAMILY
    if _FONT_REGISTRATION_ATTEMPTED:
        return _REGISTERED_FONT_FAMILY
    _FONT_REGISTRATION_ATTEMPTED = True
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows_dir / "Fonts" / "msyh.ttc",
        windows_dir / "Fonts" / "msyhbd.ttc",
        windows_dir / "Fonts" / "msyhl.ttc",
    )
    preferred = ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑")
    for path in candidates:
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            continue
        registered = QFontDatabase.applicationFontFamilies(font_id)
        _REGISTERED_FONT_FAMILY = next((name for name in preferred if name in registered), None)
        if _REGISTERED_FONT_FAMILY is None and registered:
            _REGISTERED_FONT_FAMILY = str(registered[0])
        if _REGISTERED_FONT_FAMILY:
            break
    return _REGISTERED_FONT_FAMILY


def install_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    font_family = _install_local_chinese_font()
    if font_family:
        app.setFont(QFont(font_family, 10))
    app.setStyleSheet(
        """
        QMainWindow, QWidget { background: #f5f7fa; color: #1b2430; }
        QToolBar { background: #ffffff; border: 0; border-bottom: 1px solid #d7dee8; spacing: 8px; padding: 6px; }
        QToolButton, QPushButton { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 5px; padding: 6px 10px; }
        QToolButton:hover, QPushButton:hover { background: #edf4ff; border-color: #7aa7e8; }
        QToolButton:disabled, QPushButton:disabled { color: #758195; background: #edf0f4; }
        QListWidget, QTableView, QPlainTextEdit, QLineEdit, QComboBox, QTableWidget {
            background: #ffffff; border: 1px solid #d7dee8; border-radius: 4px;
        }
        QListWidget::item { padding: 9px 8px; }
        QListWidget::item:selected { background: #dceafe; color: #102f5f; border-left: 3px solid #3169b1; }
        QHeaderView::section { background: #edf1f6; border: 0; border-right: 1px solid #d7dee8; padding: 5px; }
        QGroupBox { background: #ffffff; border: 1px solid #d7dee8; border-radius: 6px; margin-top: 10px; padding-top: 8px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #3b4a5f; }
        QLabel#pageTitle { font-size: 20px; font-weight: 600; color: #172b4d; padding: 4px 0; }
        QLabel#pageDescription, QLabel#pagePlaceholder { color: #58677d; }
        QLabel#datasetSummary { color: #315c91; padding: 8px 4px; }
        QProgressBar { background: #e8edf4; border: 0; border-radius: 4px; min-height: 14px; text-align: center; }
        QProgressBar::chunk { background: #3678c9; border-radius: 4px; }
        """
    )
