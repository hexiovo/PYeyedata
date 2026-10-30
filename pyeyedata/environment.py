"""Standard-library environment probe; also runnable without importing the app."""
from __future__ import annotations

import importlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys


RUNTIME_MODULES = {
    "numpy": "numpy", "pandas": "pandas", "scipy": "scipy",
    "matplotlib": "matplotlib", "pyside6-essentials": "PySide6.QtWidgets",
    "shiboken6": "shiboken6", "openpyxl": "openpyxl", "reportlab": "reportlab",
    "fonttools": "fontTools", "statsmodels": "statsmodels.formula.api", "patsy": "patsy",
    "multimatch-gaze": "multimatch_gaze", "pillow": "PIL",
}
BUILD_TOOLS = {"altgraph", "iniconfig", "pefile", "pluggy", "pyinstaller",
               "pyinstaller-hooks-contrib", "pytest", "pywin32-ctypes", "setuptools"}


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)", line)
        if not match or canonical_name(match[1]) in seen:
            raise ValueError("依赖清单必须使用不重复的 name==version 精确版本。")
        pins[match[1]] = match[2]
        seen.add(canonical_name(match[1]))
    if not pins:
        raise ValueError("依赖清单为空，不能判定环境完整。")
    return pins


def inspect_environment(pins: dict[str, str], *, frozen: bool = False) -> dict:
    rows = []
    for name, expected in pins.items():
        module_name = RUNTIME_MODULES.get(canonical_name(name))
        if frozen and module_name is None:
            continue  # Build/test tools are not required inside the EXE.
        actual = ""
        state, detail = "ok", "版本匹配"
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            state, detail = "missing", "未安装"
        except Exception as exc:
            state, detail = "broken", str(exc)
        if module_name:
            try:
                module = importlib.import_module(module_name)
                if frozen and not actual:
                    # Some collected wheels omit dist-info. Report a version
                    # only when the actual loaded package exposes one.
                    root = importlib.import_module(module_name.split(".")[0])
                    actual = str(getattr(root, "__version__", ""))
                    state = "ok" if actual else "unknown"
                if state == "ok":
                    detail = "可导入，版本匹配"
            except Exception as exc:
                state, detail = "broken", f"导入失败：{type(exc).__name__}: {exc}"
        if actual and actual != expected and state == "ok":
            state, detail = "mismatch", "与项目锁定版本不一致"
        if state == "unknown":
            detail = "可导入，但无法确认内置版本"
        rows.append({"name": name, "expected": expected, "actual": actual,
                     "state": state, "detail": detail, "runtime": canonical_name(name) not in BUILD_TOOLS})
    supported = (sys.platform == "win32" and sys.version_info[:2] == (3, 12)
                 and sys.version_info >= (3, 12, 1) and struct.calcsize("P") == 8)
    pip_ok, pip_detail = True, "EXE 使用内置依赖，无需 pip"
    if not frozen:
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-m", "pip", "check"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            pip_ok = result.returncode == 0
            pip_detail = (result.stdout + result.stderr).strip()
        except Exception as exc:
            pip_ok, pip_detail = False, str(exc)
    return {"rows": rows, "python": platform.python_version(), "executable": sys.executable,
            "supported": supported, "frozen": frozen, "pip_ok": pip_ok, "pip_detail": pip_detail,
            "passed": supported and bool(rows) and pip_ok and all(row["state"] == "ok" for row in rows)}


def install_commands(root: Path, *, frozen: bool, report: dict) -> list[list[str]]:
    """Repair only the project's active venv, never a system Python or an EXE."""
    if frozen:
        raise ValueError("EXE 依赖随程序内置；缺失或损坏请重新获取完整发布包。")
    if Path(sys.prefix).resolve() != (root / ".venv").resolve() or sys.prefix == sys.base_prefix:
        raise ValueError("一键安装仅用于项目 .venv；请使用 setup.cmd 准备环境后从 start.cmd 启动。")
    if not report.get("supported"):
        raise ValueError("请先准备 Windows x64 CPython 3.12.1+（仅 3.12 系列）。")
    pins = read_pins(root / "requirements.txt")
    commands = [["-I", "-m", "ensurepip", "--upgrade"],
                ["-I", "-m", "pip", "install", "--disable-pip-version-check", "--timeout", "15", "--retries", "1",
                 "-r", str(root / "requirements.txt")]]
    broken = {row["name"] for row in report.get("rows", ()) if row["state"] == "broken"}
    if broken:
        commands.append(["-I", "-m", "pip", "install", "--force-reinstall", "--no-deps",
                         "--disable-pip-version-check", "--timeout", "15", "--retries", "1",
                         *[f"{name}=={version}" for name, version in pins.items() if name in broken]])
    commands.append(["-I", "-m", "pip", "check"])
    return commands


if __name__ == "__main__":
    try:
        report = inspect_environment(read_pins(Path(sys.argv[1])))
    except Exception as exc:
        report = {"error": f"{type(exc).__name__}: {exc}", "passed": False}
    print("PYEYEDATA_ENV=" + json.dumps(report, ensure_ascii=True))
