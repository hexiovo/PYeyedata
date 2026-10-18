"""Build and verify the pinned PYeyedata Windows one-file release candidate."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback
from typing import Any

from build_manifest import canonical_json_bytes, create_build_manifest, sha256_file


PROJECT_ROOT = Path(__file__).resolve().parent
APPLICATION_INFO = PROJECT_ROOT / "pyeyedata" / "app_info.py"
MANIFEST_ARCHIVE_PATH = "pyeyedata/resources/build/build-manifest.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_application_identity() -> dict[str, str]:
    from build_manifest import _read_application_identity as read_identity
    return read_identity(PROJECT_ROOT)


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = version.split(".")
    if len(parts) != 3 or any(not item.isdigit() for item in parts):
        raise ValueError("APP_VERSION must have three numeric components")
    return tuple(int(item) for item in (*parts, "0"))  # type: ignore[return-value]


def _verify_pe_version(executable: Path, app_version: str) -> dict[str, Any]:
    import pefile

    pe = pefile.PE(str(executable), fast_load=False)
    version_entries: dict[str, str] = {}
    if pe.VS_VERSIONINFO is None:
        raise RuntimeError("Built EXE has no PE VS_VERSIONINFO resource")
    fixed = pe.VS_FIXEDFILEINFO
    if not fixed:
        raise RuntimeError("Built EXE has no fixed PE version information")
    fixed_info = fixed[0]
    file_version = (
        fixed_info.FileVersionMS >> 16,
        fixed_info.FileVersionMS & 0xFFFF,
        fixed_info.FileVersionLS >> 16,
        fixed_info.FileVersionLS & 0xFFFF,
    )
    product_version = (
        fixed_info.ProductVersionMS >> 16,
        fixed_info.ProductVersionMS & 0xFFFF,
        fixed_info.ProductVersionLS >> 16,
        fixed_info.ProductVersionLS & 0xFFFF,
    )
    expected = _version_tuple(app_version)
    if file_version != expected or product_version != expected:
        raise RuntimeError(
            f"Built PE version mismatch: file={file_version}, product={product_version}, expected={expected}"
        )
    for file_info_group in getattr(pe, "FileInfo", []) or []:
        resources = file_info_group if isinstance(file_info_group, (list, tuple)) else (file_info_group,)
        for resource in resources:
            for table in getattr(resource, "StringTable", []) or []:
                # pefile exposes StringTable.entries as a bytes-to-bytes mapping.
                for raw_key, raw_value in (getattr(table, "entries", {}) or {}).items():
                    key = raw_key.decode("utf-8", "replace") if isinstance(raw_key, bytes) else str(raw_key)
                    value = raw_value.decode("utf-8", "replace") if isinstance(raw_value, bytes) else str(raw_value)
                    version_entries[key] = value
    expected_text = ".".join(str(item) for item in expected)
    for key in ("FileVersion", "ProductVersion"):
        if version_entries.get(key) != expected_text:
            raise RuntimeError(f"Built PE {key} string is {version_entries.get(key)!r}, expected {expected_text!r}")
    if version_entries.get("OriginalFilename") != "PYeyedata.exe":
        raise RuntimeError("Built PE OriginalFilename does not identify PYeyedata.exe")
    result = {
        "file_version": list(file_version),
        "product_version": list(product_version),
        "string_versions": {key: version_entries[key] for key in ("FileVersion", "ProductVersion")},
        "original_filename": version_entries.get("OriginalFilename"),
        "product_name": version_entries.get("ProductName"),
    }
    pe.close()
    return result


def _find_archive_member(archive: Any, expected_path: str) -> str:
    expected = expected_path.replace("\\", "/")
    matches = [
        member for member in archive.toc
        if member.replace("\\", "/") == expected
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Frozen archive must contain exactly one {expected_path}; found {len(matches)}"
        )
    return matches[0]


def _verify_embedded_manifest(executable: Path, run_root: Path) -> dict[str, Any]:
    from PyInstaller.archive.readers import CArchiveReader
    from pyeyedata.app_info import APP_NAME, APP_VERSION, PRD_VERSION
    from pyeyedata.build_identity import parse_build_manifest
    from pyeyedata.contracts import CONTRACT_VERSION
    from pyeyedata.pipeline import PIPELINE_SCHEMA_VERSION

    archive = CArchiveReader(str(executable))
    archive_path = _find_archive_member(archive, MANIFEST_ARCHIVE_PATH)
    embedded = archive.extract(archive_path)
    if not isinstance(embedded, bytes):
        raise RuntimeError("PyInstaller archive reader returned non-byte manifest content")
    generated_path = run_root / "build" / "build-manifest.json"
    generated = generated_path.read_bytes()
    if embedded != generated:
        raise RuntimeError("Embedded build manifest differs from the manifest written by the spec")
    identity = parse_build_manifest(
        embedded,
        application={"name": APP_NAME, "version": APP_VERSION, "prd_version": PRD_VERSION},
        contracts={"data_contract_version": CONTRACT_VERSION,
                   "pipeline_schema_version": PIPELINE_SCHEMA_VERSION},
    )
    project_root = PROJECT_ROOT.resolve()
    source_mismatches: dict[str, dict[str, str | None]] = {}
    for relative, expected in identity.source_sha256.items():
        path = project_root / relative
        current = sha256_file(path) if path.is_file() else None
        if current != expected:
            source_mismatches[relative] = {"manifest": expected, "current": current}
    resource_mismatches = {}
    for relative, expected in identity.resource_sha256.items():
        path = project_root / relative
        actual = sha256_file(path) if path.is_file() else None
        if actual != expected:
            resource_mismatches[relative] = {"manifest": expected, "current": actual}
    if source_mismatches or resource_mismatches:
        raise RuntimeError("Embedded manifest source/resource digests differ from the current build inputs")
    return {
        "archive_path": MANIFEST_ARCHIVE_PATH,
        "manifest_sha256": _sha256_bytes(embedded),
        "schema": "pyeyedata.build-manifest/v1",
        "source_count": len(identity.source_sha256),
        "resource_count": len(identity.resource_sha256),
        "source_resource_hashes_match_checkout": True,
    }


def _controlled_build_environment(
    run_root: Path, phase: str, cache_root: Path
) -> tuple[dict[str, str], dict[str, Any]]:
    """Keep build PATH and interpreter scratch files inside the selected RUN."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    path_candidates = [
        Path(sys.executable).resolve().parent,
        Path(sys.base_prefix).resolve(),
        Path(sys.base_prefix).resolve() / "DLLs",
        system_root / "System32",
        system_root,
    ]
    path_entries: list[str] = []
    seen_path_entries: set[str] = set()
    for candidate in path_candidates:
        if not candidate.is_dir():
            continue
        value = str(candidate.resolve())
        normalized = os.path.normcase(os.path.normpath(value))
        if normalized in seen_path_entries:
            continue
        if "codex" in normalized.casefold():
            raise RuntimeError(f"A Codex runtime path entered the controlled build PATH: {value}")
        seen_path_entries.add(normalized)
        path_entries.append(value)

    if not path_entries or not any(
        os.path.normcase(os.path.normpath(str(system_root / "System32")))
        == os.path.normcase(os.path.normpath(path))
        for path in path_entries
    ):
        raise RuntimeError("The controlled build PATH must include the Windows System32 directory")

    temp_root = run_root / "tmp" / f"pyinstaller-{phase}"
    pycache_root = run_root / "tmp" / "python-pycache" / phase
    temp_root.mkdir(parents=True, exist_ok=True)
    pycache_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "PATH": os.pathsep.join(path_entries),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "PYTHONPYCACHEPREFIX": str(pycache_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYINSTALLER_CONFIG_DIR": str(cache_root),
        }
    )
    record = {
        "path_entries": path_entries,
        "tmp": str(temp_root),
        "temp": str(temp_root),
        "python_cache": str(pycache_root),
        "pyinstaller_config": str(cache_root),
        "unset_environment_variables": ["PYTHONHOME", "PYTHONPATH"],
        "pythondontwritebytecode": True,
        "python_no_user_site": True,
    }
    return env, record


def _run_build(phase: str, run_root: Path) -> dict[str, Any]:
    if sys.version_info[:2] != (3, 12) or sys.version_info < (3, 12, 1):
        raise RuntimeError(
            "Build requires CPython 3.12.1 or later in the 3.12 series, "
            f"got {sys.version}"
        )
    manifest = create_build_manifest(PROJECT_ROOT)
    expected_pyinstaller = manifest["build_environment"]["distributions"].get("pyinstaller")
    if expected_pyinstaller != "6.11.1":
        raise RuntimeError(f"D02 pins PyInstaller 6.11.1, found {expected_pyinstaller!r}")

    build_root = run_root / "build"
    work_root = build_root / f"pyinstaller-work-{phase}"
    dist_root = run_root / "dist"
    cache_root = run_root / "cache" / "pyinstaller"
    log_root = run_root / "logs"
    evidence_root = run_root / "evidence"
    for path in (build_root, work_root, dist_root, cache_root, log_root, evidence_root):
        path.mkdir(parents=True, exist_ok=True)

    env, build_environment_controls = _controlled_build_environment(run_root, phase, cache_root)

    version_resource = build_root / "PYeyedata.versioninfo"
    version_cmd = [
        sys.executable,
        "-X",
        "utf8",
        "-B",
        str(PROJECT_ROOT / "build_windows_version_resource.py"),
        str(version_resource),
    ]
    version_process = subprocess.run(
        version_cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True
    )
    if version_process.returncode:
        raise RuntimeError("Version resource generation failed:\n" + version_process.stdout + version_process.stderr)

    env.update({
        "PYEYEDATA_PROJECT_ROOT": str(PROJECT_ROOT),
        "PYEYEDATA_RUN_ROOT": str(run_root),
        "PYEYEDATA_MANIFEST_PATH": str(build_root / "build-manifest.json"),
        "PYEYEDATA_VERSION_RESOURCE": str(version_resource),
        "PYEYEDATA_BUILD_PHASE": phase,
    })
    command = [
        sys.executable, "-X", "utf8", "-B", "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", str(dist_root), "--workpath", str(work_root),
        str(PROJECT_ROOT / "build_windows.spec"),
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    process = subprocess.run(command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    log_path = log_root / f"d02-pyinstaller-{phase}.log"
    log_path.write_text(process.stdout + "\n--- STDERR ---\n" + process.stderr, encoding="utf-8")
    if process.returncode:
        raise RuntimeError(
            f"PyInstaller {phase} pass failed with exit {process.returncode}; see {log_path}\n"
            + "\n".join((process.stdout + process.stderr).splitlines()[-80:])
        )

    executable = dist_root / "PYeyedata.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"PyInstaller completed without {executable}")
    app = manifest["application"]
    pe = _verify_pe_version(executable, app["version"])
    result: dict[str, Any] = {
        "schema": "pyeyedata.release-build-evidence/v1",
        "phase": phase,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "working_directory": str(PROJECT_ROOT),
        "python": sys.version,
        "pyinstaller": expected_pyinstaller,
        "build_environment_controls": build_environment_controls,
        "application": app,
        "executable": {
            "path": str(executable),
            "size_bytes": executable.stat().st_size,
            "sha256": sha256_file(executable),
        },
        "pe_version": pe,
        "manifest": None,
        "analysis_evidence": str(evidence_root / f"d02-analysis-{phase}-v1.json"),
        "warning_log": None,
        "warning_log_source": None,
        "pyinstaller_log": str(log_path),
    }
    if phase == "final":
        result["manifest"] = _verify_embedded_manifest(executable, run_root)

    warn_candidates = sorted(path for path in work_root.rglob("warn-*.txt") if path.is_file())
    if len(warn_candidates) > 1:
        raise RuntimeError(f"Expected one PyInstaller warning log under {work_root}, found {len(warn_candidates)}")
    if warn_candidates:
        warning_path = evidence_root / f"d02-pyinstaller-warnings-{phase}-v1.txt"
        warning_path.write_bytes(warn_candidates[0].read_bytes())
        result["warning_log"] = str(warning_path)
        result["warning_log_source"] = str(warn_candidates[0].resolve())
    toc_files = sorted(work_root.rglob("*.toc"))
    result["toc_files"] = [
        {"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in toc_files
    ]
    evidence_path = evidence_root / f"d02-build-{phase}-v1.json"
    evidence_path.write_bytes(canonical_json_bytes(result))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="RUN directory for all build outputs")
    parser.add_argument("--phase", choices=("inventory", "final"), required=True,
                        help="inventory creates the first Analysis; final includes the frozen manifest")
    args = parser.parse_args(argv)
    try:
        run_root = args.run.resolve()
        if not run_root.is_dir():
            raise NotADirectoryError(f"RUN directory does not exist: {run_root}")
        result = _run_build(args.phase, run_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"Build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
