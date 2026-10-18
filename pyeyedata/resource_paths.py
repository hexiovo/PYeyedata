"""Scoped access to bundled package resources, including frozen builds."""
from __future__ import annotations

from contextlib import contextmanager
from importlib import resources as importlib_resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ContextManager, Iterator


_RESOURCE_ROOT = importlib_resources.files("pyeyedata").joinpath("resources")


def _safe_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        raise TypeError("relative_path must be a string")
    if not relative_path or "\x00" in relative_path:
        raise ValueError("relative_path must be a nonempty package resource path")

    windows_path = PureWindowsPath(relative_path)
    normalized = relative_path.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    raw_parts = normalized.split("/")
    if (posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive
            or any(part in {"", ".", ".."} for part in raw_parts)):
        raise ValueError("resource path must stay inside pyeyedata/resources")
    return tuple(posix_path.parts)


@contextmanager
def resource_path(relative_path: str) -> Iterator[Path]:
    """Yield a real path for one bundled resource while its extraction is alive.

    Keep this context open for the full duration of any library operation that may
    reopen the path lazily, especially Matplotlib and ReportLab rendering.
    """
    parts = _safe_parts(relative_path)
    resource = _RESOURCE_ROOT.joinpath(*parts)
    if not resource.is_file():
        raise FileNotFoundError(f"Bundled resource is unavailable: {relative_path}")

    if isinstance(resource, Path):
        root_path = Path(str(_RESOURCE_ROOT)).resolve()
        resolved = resource.resolve(strict=True)
        if not resolved.is_relative_to(root_path) or not resolved.is_file():
            raise ValueError("resource path resolves outside pyeyedata/resources")
        resource = resolved

    with importlib_resources.as_file(resource) as resolved_path:
        yield resolved_path


def read_resource_bytes(relative_path: str) -> bytes:
    """Read one bundled resource while respecting frozen-resource extraction."""
    with resource_path(relative_path) as path:
        return path.read_bytes()


def read_resource_text(relative_path: str) -> str:
    """Read a bundled UTF-8 text resource such as an offline license notice."""
    return read_resource_bytes(relative_path).decode("utf-8-sig")
