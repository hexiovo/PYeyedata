"""Early GUI feedback and Windows taskbar metadata for the PYeyedata launcher.

This module intentionally lives outside the ``pyeyedata`` package so the
launcher can create a small Qt startup window before importing scientific
modules. In a PyInstaller one-file build, the bootloader extracts the bundled
runtime before Python starts; this module cannot cover that earlier interval.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Sequence
from uuid import UUID

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget


WINDOWS_APP_USER_MODEL_ID = "PYeyedata.Desktop"

_APP_USER_MODEL_ID_SET = False
_APP_USER_MODEL_FMTID = UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")
_PKEY_RELAUNCH_ICON_RESOURCE_PID = 3
_PKEY_APP_USER_MODEL_ID_PID = 5
_VT_EMPTY = 0
_VT_LPWSTR = 31
_RPC_E_CHANGED_MODE = 0x80010106


def set_windows_application_identity() -> None:
    """Set the stable process AppUserModelID before any Qt window is created."""
    global _APP_USER_MODEL_ID_SET
    if sys.platform != "win32" or _APP_USER_MODEL_ID_SET:
        return

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
    set_app_id.argtypes = [ctypes.c_wchar_p]
    set_app_id.restype = ctypes.c_long
    result = set_app_id(WINDOWS_APP_USER_MODEL_ID)
    if result != 0:
        raise OSError(f"Cannot set Windows application identity: HRESULT {result:#x}")
    _APP_USER_MODEL_ID_SET = True


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: UUID) -> _Guid:
        return cls(
            value.time_low,
            value.time_mid,
            value.time_hi_version,
            (ctypes.c_ubyte * 8)(*value.bytes[8:]),
        )


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", wintypes.DWORD)]


class _PropVariantUnion(ctypes.Union):
    # PROPVARIANT's union is 16 bytes on Windows x64. Keeping the complete
    # size avoids passing a short structure to COM even for VT_LPWSTR/VT_EMPTY.
    _fields_ = [
        ("pwszVal", ctypes.c_wchar_p),
        ("pointer", ctypes.c_void_p),
        ("raw", ctypes.c_ubyte * 16),
    ]


class _PropVariant(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.USHORT),
        ("wReserved2", wintypes.USHORT),
        ("wReserved3", wintypes.USHORT),
        ("value", _PropVariantUnion),
    ]


def _win32_hresult_check(result: int, operation: str) -> None:
    if int(result) < 0:
        raise OSError(f"{operation} failed with HRESULT 0x{int(result) & 0xFFFFFFFF:08x}")


def _set_window_properties(hwnd: int, *, app_id: str | None, icon_resource: str | None) -> None:
    """Set or clear taskbar properties using the window's IPropertyStore."""
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    co_result = int(ole32.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED
    co_code = co_result & 0xFFFFFFFF
    should_uninitialize = co_code in (0, 1)  # S_OK or S_FALSE
    if co_code not in (0, 1, _RPC_E_CHANGED_MODE):
        _win32_hresult_check(co_result, "CoInitializeEx")

    store_pointer = ctypes.c_void_p()
    store_iid = _Guid.from_uuid(UUID("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"))
    get_store = shell32.SHGetPropertyStoreForWindow
    get_store.argtypes = [wintypes.HWND, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
    get_store.restype = ctypes.c_long
    try:
        _win32_hresult_check(
            get_store(hwnd, ctypes.byref(store_iid), ctypes.byref(store_pointer)),
            "SHGetPropertyStoreForWindow",
        )
        vtable = ctypes.cast(
            store_pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        set_value = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_PropertyKey),
            ctypes.POINTER(_PropVariant),
        )(vtable[6])
        release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
        fmtid = _Guid.from_uuid(_APP_USER_MODEL_FMTID)

        def write_string(pid: int, text: str) -> None:
            key = _PropertyKey(fmtid, pid)
            backing = ctypes.create_unicode_buffer(text)
            value = _PropVariant()
            value.vt = _VT_LPWSTR
            value.pwszVal = ctypes.cast(backing, ctypes.c_wchar_p)
            _win32_hresult_check(
                set_value(store_pointer, ctypes.byref(key), ctypes.byref(value)),
                f"IPropertyStore.SetValue({pid})",
            )

        def clear_value(pid: int) -> None:
            key = _PropertyKey(fmtid, pid)
            value = _PropVariant()
            value.vt = _VT_EMPTY
            _win32_hresult_check(
                set_value(store_pointer, ctypes.byref(key), ctypes.byref(value)),
                f"IPropertyStore.SetValue(VT_EMPTY, {pid})",
            )

        if icon_resource is None and app_id is None:
            # Shell window properties hold resources until explicitly cleared.
            clear_value(_PKEY_RELAUNCH_ICON_RESOURCE_PID)
            clear_value(_PKEY_APP_USER_MODEL_ID_PID)
        else:
            # Microsoft documents that relaunch metadata must precede window ID.
            if icon_resource is not None:
                write_string(_PKEY_RELAUNCH_ICON_RESOURCE_PID, icon_resource)
            if app_id is not None:
                write_string(_PKEY_APP_USER_MODEL_ID_PID, app_id)
    finally:
        if store_pointer.value:
            vtable = ctypes.cast(
                store_pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
            release(store_pointer)
        if should_uninitialize:
            ole32.CoUninitialize()


def _write_startup_diagnostic(context: str, exc: BaseException) -> None:
    """Best-effort log for a non-fatal startup integration issue."""
    try:
        from pyeyedata_bootstrap import user_data_root

        occurred = datetime.now(timezone.utc)
        log_directory = user_data_root() / "logs" / "diagnostics"
        log_directory.mkdir(parents=True, exist_ok=True)
        path = log_directory / (
            f"startup-diagnostic-{occurred.strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}.json"
        )
        payload = {
            "schema": "pyeyedata.startup-diagnostic/v1",
            "timestamp_utc": occurred.isoformat(),
            "context": context,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        # Shell metadata must never prevent the main window from opening.
        pass


def bind_windows_taskbar_identity(
    window: QWidget,
    app: QApplication,
    *,
    icon_resource: str,
) -> QObject | None:
    """Apply explicit taskbar identity and relaunch icon metadata to a window.

    ``icon_resource`` must be a stable Shell resource path. Call this after the
    window has a native handle and before it is shown. Failures are diagnostic,
    not fatal to application startup.
    """
    if sys.platform != "win32":
        return None
    hwnd = int(window.winId())
    try:
        _set_window_properties(
            hwnd,
            app_id=WINDOWS_APP_USER_MODEL_ID,
            icon_resource=icon_resource,
        )
        binding = _WindowsTaskbarIdentityCleanup(app, window, hwnd)
        return binding
    except Exception as exc:
        try:
            _set_window_properties(hwnd, app_id=None, icon_resource=None)
        except Exception:
            pass
        _write_startup_diagnostic("windows-taskbar-identity", exc)
        return None


class _WindowsTaskbarIdentityCleanup(QObject):
    """Clear per-window Shell properties before a native window is destroyed."""

    def __init__(self, app: QApplication, window: QWidget, hwnd: int) -> None:
        super().__init__(app)
        self._window = window
        self._hwnd = hwnd
        self._cleared = False
        window.installEventFilter(self)
        app.aboutToQuit.connect(self.clear)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window and event.type() == QEvent.Type.Close:
            # The window may ignore a close while prompting to save an archive.
            # Check after its closeEvent has decided whether it stays visible.
            QTimer.singleShot(0, self._clear_if_closed)
        return False

    def _clear_if_closed(self) -> None:
        try:
            closed = self._window is None or not self._window.isVisible()
        except RuntimeError:
            closed = True
        if closed:
            self.clear()

    def clear(self) -> None:
        if self._cleared:
            return
        self._cleared = True
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            is_window = user32.IsWindow
            is_window.argtypes = [wintypes.HWND]
            is_window.restype = wintypes.BOOL
            if is_window(self._hwnd):
                _set_window_properties(self._hwnd, app_id=None, icon_resource=None)
        except Exception as exc:
            _write_startup_diagnostic("windows-taskbar-identity-cleanup", exc)


class StartupIndicator:
    """Show a small indeterminate startup window before package imports."""

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        set_windows_application_identity()
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication(list(argv) if argv is not None else sys.argv)
        self.window = QWidget(
            None,
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.window.setFixedSize(380, 132)
        self.window.setObjectName("pyeyedataStartupIndicator")

        layout = QVBoxLayout(self.window)
        layout.setContentsMargins(26, 20, 26, 18)
        layout.setSpacing(9)
        title = QLabel("PYeyedata")
        title.setObjectName("startupTitle")
        message = QLabel("正在启动，请稍候…")
        message.setObjectName("startupMessage")
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedHeight(7)
        layout.addWidget(title)
        layout.addWidget(message)
        layout.addWidget(progress)
        self.window.setStyleSheet(
            "QWidget#pyeyedataStartupIndicator { background: #f4f7fa; "
            "border: 1px solid #ccd6df; border-radius: 10px; }"
            "QLabel#startupTitle { color: #123c55; font-size: 18px; font-weight: 600; }"
            "QLabel#startupMessage { color: #536879; font-size: 13px; }"
            "QProgressBar { background: #dfe7ec; border: 0; border-radius: 3px; }"
            "QProgressBar::chunk { background: #18a9bd; border-radius: 3px; }"
        )
        screen = self.app.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            frame = self.window.frameGeometry()
            frame.moveCenter(geometry.center())
            self.window.move(frame.topLeft())
        self._closed = False
        try:
            self.window.show()
            self.app.processEvents()
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.close()
        finally:
            self.window.deleteLater()
            try:
                self.app.processEvents()
            except Exception:
                pass
