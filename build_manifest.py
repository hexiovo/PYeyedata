"""Create the embedded release identity and a factual PyInstaller Analysis record.

This module deliberately uses only the standard library so the spec can produce
the same manifest with the pinned build interpreter before the executable is
assembled.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any, Iterable


MANIFEST_SCHEMA = "pyeyedata.build-manifest/v1"
MANIFEST_ARCHIVE_PATH = "pyeyedata/resources/build/build-manifest.json"
_PIN_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^=\s]+)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_key(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def collect_resource_files(project_root: Path) -> list[Path]:
    """Return exactly the non-Python product assets collected by the spec."""
    resource_root = project_root / "pyeyedata" / "resources"
    files = [
        path for path in resource_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in {".py", ".pyc", ".pyo"}
        and path.relative_to(resource_root).as_posix() != "build/build-manifest.json"
    ]
    return sorted(files, key=lambda path: path.relative_to(resource_root).as_posix())


def _read_application_identity(project_root: Path) -> dict[str, str]:
    source = project_root / "pyeyedata" / "app_info.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    values: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id in {"APP_NAME", "APP_VERSION", "PRD_VERSION"}:
                if target.id in values:
                    raise ValueError(f"{target.id} is assigned more than once in app_info.py")
                value = ast.literal_eval(statement.value)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{target.id} must be a nonempty string constant")
                values[target.id] = value
    if set(values) != {"APP_NAME", "APP_VERSION", "PRD_VERSION"}:
        raise ValueError("app_info.py must define APP_NAME, APP_VERSION and PRD_VERSION")
    return {"name": values["APP_NAME"], "version": values["APP_VERSION"],
            "prd_version": values["PRD_VERSION"]}


def _read_contract_versions(project_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for relative, names in (
        ("pyeyedata/contracts.py", {"CONTRACT_VERSION": "data_contract_version"}),
        ("pyeyedata/pipeline.py", {"PIPELINE_SCHEMA_VERSION": "pipeline_schema_version"}),
    ):
        source = project_root / relative
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        found: dict[str, str] = {}
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    value = ast.literal_eval(statement.value)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{target.id} must be a nonempty string constant")
                    found[target.id] = value
        if set(found) != set(names):
            raise ValueError(f"Could not read required contract version from {relative}")
        for name, field in names.items():
            values[field] = found[name]
    return values


def _pinned_requirements(project_root: Path) -> dict[str, str]:
    requirements = project_root / "requirements.txt"
    pins: dict[str, str] = {}
    canonical: set[str] = set()
    for line_number, raw in enumerate(requirements.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"requirements.txt:{line_number} is not an exact name==version pin")
        name, expected = match.groups()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in canonical:
            raise ValueError(f"requirements.txt has duplicate normalized distribution {name!r}")
        canonical.add(normalized)
        try:
            from importlib import metadata
            actual = metadata.version(name)
        except Exception as exc:
            raise RuntimeError(f"Pinned distribution {name} is not installed in this build environment") from exc
        if actual != expected:
            raise RuntimeError(f"Pinned distribution {name} is {expected}, but the build environment has {actual}")
        pins[name] = actual
    if not pins:
        raise ValueError("requirements.txt contains no exact pins")
    return dict(sorted(pins.items(), key=lambda item: item[0].casefold()))


def create_build_manifest(project_root: Path) -> dict[str, Any]:
    """Return the bounded JSON identity expected by the frozen runtime reader."""
    project_root = project_root.resolve()
    package_root = project_root / "pyeyedata"
    source_files = [
        path for path in package_root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    source_files.extend(
        project_root / relative for relative in (
            "pyeyedata_launcher.py", "pyeyedata_bootstrap.py", "build_windows.py",
            "build_windows.spec", "build_manifest.py", "build_windows_version_resource.py",
            "requirements.txt", "version.md",
            "setup.cmd", "start.cmd", "release.cmd", "tools/manage.ps1",
            "tools/run_checks.py", "tools/release_windows.py",
            "README.md", "AGENTS.md", "CODEX_HANDOFF.md",
            "docs/DEPLOYMENT.md", "docs/RELEASE_NOTES.md", "docs/WORKFLOW_ENHANCEMENTS.md",
            "docs/COMMON_METRICS.md", "plans/common-eye-metrics.md",
            ".github/workflows/release.yml",
        )
    )
    missing = [path for path in source_files if not path.is_file()]
    if missing:
        raise FileNotFoundError("Required source identity inputs are missing: "
                                + ", ".join(str(path) for path in missing))
    source_hashes = {
        _relative_key(project_root, path): sha256_file(path)
        for path in sorted(set(source_files), key=lambda item: _relative_key(project_root, item))
    }
    resource_hashes = {
        _relative_key(project_root, path): sha256_file(path)
        for path in collect_resource_files(project_root)
    }
    version_path = project_root / "version.md"
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "application": _read_application_identity(project_root),
        "contracts": _read_contract_versions(project_root),
        "source_sha256": source_hashes,
        "resource_sha256": resource_hashes,
        "version_document_sha256": sha256_file(version_path),
        "build_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "architecture": platform.machine() or "unknown",
            "distributions": _pinned_requirements(project_root),
        },
    }
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError("Generated build manifest exceeds the 4 MiB runtime limit")
    return manifest


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _source_hash(path_text: str) -> str | None:
    path = Path(path_text)
    if not path.is_file():
        return None
    return sha256_file(path)


def collect_analysis_records(analysis: Any, project_root: Path) -> dict[str, Any]:
    """Capture PyInstaller's actual Analysis/TOC rows with source hashes."""
    project_root = project_root.resolve()

    def rows(toc: Iterable[tuple[Any, ...]], kind: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in toc:
            if len(item) < 2:
                continue
            if kind == "pure":
                name, source = str(item[0]), str(item[1])
                typecode = str(item[2]) if len(item) > 2 else "PYMODULE"
            else:
                name, source = str(item[0]), str(item[1])
                typecode = str(item[2]) if len(item) > 2 else kind.upper()
            record: dict[str, Any] = {
                "archive_path": name.replace("\\", "/"),
                "kind": kind,
                "typecode": typecode,
                "source_path": source,
            }
            source_path = Path(source)
            if source_path.is_file():
                try:
                    record["source_sha256"] = sha256_file(source_path)
                    record["source_size"] = source_path.stat().st_size
                    if source_path.resolve().is_relative_to(project_root):
                        record["source_repo_path"] = _relative_key(project_root, source_path)
                except OSError as exc:
                    record["source_hash_error"] = f"{type(exc).__name__}: {exc}"
            result.append(record)
        return sorted(result, key=lambda row: (row["archive_path"], row["source_path"]))

    return {
        "schema": "pyeyedata.pyinstaller-analysis/v1",
        "pyinstaller": __import__("PyInstaller").__version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "architecture": platform.machine() or "unknown",
        "scripts": rows(analysis.scripts, "script"),
        "pure": rows(analysis.pure, "pure"),
        "binaries": rows(analysis.binaries, "binary"),
        "datas": rows(analysis.datas, "data"),
    }

