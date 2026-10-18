"""Pure-stdlib user-data bootstrap for the PYeyedata desktop launcher.

Call :func:`prepare_gui_paths` before importing ``pyeyedata``. Keeping this
module outside the package leaves core-library imports free of filesystem
side effects while letting a frozen GUI set Matplotlib's cache first.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
from typing import Literal


APP_NAME = "PYeyedata"
PathKind = Literal["cache", "logs", "settings"]
_PATH_KINDS = frozenset({"cache", "logs", "settings"})


def user_data_root() -> Path:
    """Return the per-user product root without consulting Qt or the CWD."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).expanduser() / APP_NAME
    if os.name == "nt":
        raise RuntimeError(
            "Windows did not provide %LOCALAPPDATA%; PYeyedata cannot choose a safe per-user data location."
        )
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def user_directory(kind: PathKind, *parts: str) -> Path:
    """Create and return one owned directory below the product data root."""
    if kind not in _PATH_KINDS:
        raise ValueError(f"Unsupported PYeyedata user-data category: {kind!r}")
    path = user_data_root() / kind
    for part in parts:
        child = Path(part)
        if not part or child.is_absolute() or child.drive or any(
            piece in {"", ".", ".."} for piece in child.parts
        ):
            raise ValueError("PYeyedata user-data path parts must be safe relative components")
        path = path / child
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"无法在 {path} 创建 PYeyedata {kind} 目录。请检查 %LOCALAPPDATA% 的可写权限和可用空间。原因：{exc}"
        ) from exc
    return path


def prepare_gui_paths() -> Path:
    """Create persistent product directories and configure Matplotlib."""
    root = user_data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        for kind in ("cache", "logs", "settings"):
            (root / kind).mkdir(parents=True, exist_ok=True)
        mpl_config = root / "cache" / "matplotlib"
        mpl_config.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"无法在 {root} 初始化 PYeyedata 用户目录。请检查 %LOCALAPPDATA% 的可写权限和可用空间。原因：{exc}"
        ) from exc
    os.environ["MPLCONFIGDIR"] = str(mpl_config)
    return root


def show_startup_error(message: str) -> None:
    """Show a readable startup failure even for a windowed executable."""
    title = f"{APP_NAME} 启动失败"
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"{title}: {message}", file=sys.stderr)

