# PyInstaller one-file/windowed release specification for PYeyedata.
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import sys
from types import SimpleNamespace


PROJECT_ROOT = Path(os.environ["PYEYEDATA_PROJECT_ROOT"]).resolve()
RUN_ROOT = Path(os.environ["PYEYEDATA_RUN_ROOT"]).resolve()
MANIFEST_PATH = Path(os.environ["PYEYEDATA_MANIFEST_PATH"]).resolve()
VERSION_RESOURCE_PATH = Path(os.environ["PYEYEDATA_VERSION_RESOURCE"]).resolve()
BUILD_PHASE = os.environ.get("PYEYEDATA_BUILD_PHASE", "unknown")
sys.path.insert(0, str(PROJECT_ROOT))

if sys.version_info[:2] != (3, 12) or sys.version_info < (3, 12, 1):
    raise RuntimeError(
        "Build requires CPython 3.12.1 or later in the 3.12 series, "
        f"got {sys.version}"
    )

from build_manifest import (  # noqa: E402
    MANIFEST_ARCHIVE_PATH,
    canonical_json_bytes,
    collect_analysis_records,
    collect_resource_files,
    create_build_manifest,
)
from PyInstaller.utils.hooks import copy_metadata  # noqa: E402


# These are the exact API-set stubs and UCRT file observed in the clean-path
# Analysis from Windows System32. Windows 10/11 supplies these OS contracts;
# do not place copies of the build host's OS files in the application archive.
# The source-parent check is mandatory so same-named application/runtime files
# from CPython, Qt, or a wheel remain eligible for packaging.
WINDOWS_SYSTEM_OS_ARCHIVE_NAMES = frozenset(
    {
        "api-ms-win-core-console-l1-1-0.dll",
        "api-ms-win-core-datetime-l1-1-0.dll",
        "api-ms-win-core-debug-l1-1-0.dll",
        "api-ms-win-core-errorhandling-l1-1-0.dll",
        "api-ms-win-core-fibers-l1-1-0.dll",
        "api-ms-win-core-file-l1-1-0.dll",
        "api-ms-win-core-file-l1-2-0.dll",
        "api-ms-win-core-file-l2-1-0.dll",
        "api-ms-win-core-handle-l1-1-0.dll",
        "api-ms-win-core-heap-l1-1-0.dll",
        "api-ms-win-core-interlocked-l1-1-0.dll",
        "api-ms-win-core-libraryloader-l1-1-0.dll",
        "api-ms-win-core-localization-l1-2-0.dll",
        "api-ms-win-core-memory-l1-1-0.dll",
        "api-ms-win-core-namedpipe-l1-1-0.dll",
        "api-ms-win-core-processenvironment-l1-1-0.dll",
        "api-ms-win-core-processthreads-l1-1-0.dll",
        "api-ms-win-core-processthreads-l1-1-1.dll",
        "api-ms-win-core-profile-l1-1-0.dll",
        "api-ms-win-core-rtlsupport-l1-1-0.dll",
        "api-ms-win-core-string-l1-1-0.dll",
        "api-ms-win-core-synch-l1-1-0.dll",
        "api-ms-win-core-synch-l1-2-0.dll",
        "api-ms-win-core-sysinfo-l1-1-0.dll",
        "api-ms-win-core-timezone-l1-1-0.dll",
        "api-ms-win-core-util-l1-1-0.dll",
        "api-ms-win-crt-conio-l1-1-0.dll",
        "api-ms-win-crt-convert-l1-1-0.dll",
        "api-ms-win-crt-environment-l1-1-0.dll",
        "api-ms-win-crt-filesystem-l1-1-0.dll",
        "api-ms-win-crt-heap-l1-1-0.dll",
        "api-ms-win-crt-locale-l1-1-0.dll",
        "api-ms-win-crt-math-l1-1-0.dll",
        "api-ms-win-crt-private-l1-1-0.dll",
        "api-ms-win-crt-process-l1-1-0.dll",
        "api-ms-win-crt-runtime-l1-1-0.dll",
        "api-ms-win-crt-stdio-l1-1-0.dll",
        "api-ms-win-crt-string-l1-1-0.dll",
        "api-ms-win-crt-time-l1-1-0.dll",
        "api-ms-win-crt-utility-l1-1-0.dll",
        "ucrtbase.dll",
    }
)
if len(WINDOWS_SYSTEM_OS_ARCHIVE_NAMES) != 41:
    raise RuntimeError("The Windows System32 exclusion inventory must contain exactly 41 names")

def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def _exclude_windows_system_os_binaries(analysis: Analysis) -> dict[str, object]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    system32 = (system_root / "System32").resolve()
    system32_key = _normalized_path(system32)
    retained: list[tuple[object, ...]] = []
    excluded: list[dict[str, object]] = []

    for entry in analysis.binaries:
        if len(entry) < 2:
            retained.append(entry)
            continue
        archive_path = str(entry[0]).replace("\\", "/")
        source_path = Path(str(entry[1]))
        archive_name = PurePosixPath(archive_path).name.casefold()
        source_name = source_path.name.casefold()
        if (
            archive_name in WINDOWS_SYSTEM_OS_ARCHIVE_NAMES
            and source_name == archive_name
            and _normalized_path(source_path.parent.resolve()) == system32_key
        ):
            resolved_source = source_path.resolve()
            excluded.append(
                {
                    "archive_path": archive_path,
                    "source_group": "windows-system",
                    "source_path": str(resolved_source),
                    "source_sha256": hashlib.sha256(resolved_source.read_bytes()).hexdigest(),
                    "source_size": resolved_source.stat().st_size,
                }
            )
        else:
            retained.append(entry)

    # Keep the Analysis TOC object, while removing only exact names resolved
    # from the host's System32 directory. A newer Windows build can yield any
    # subset of the reviewed names; unknown System32 binaries remain fatal.
    analysis.binaries[:] = retained
    remaining_system32 = [
        str(row[0]).replace("\\", "/")
        for row in analysis.binaries
        if len(row) >= 2 and _normalized_path(Path(str(row[1])).parent) == system32_key
    ]
    if remaining_system32:
        raise RuntimeError(
            "Unexpected System32-sourced binary remains in the application TOC: "
            + ", ".join(sorted(remaining_system32))
        )

    return {
        "schema": "pyeyedata.windows-system-dependency-policy/v1",
        "target_os": ["Windows 10", "Windows 11"],
        "reviewed_system_archive_names": sorted(WINDOWS_SYSTEM_OS_ARCHIVE_NAMES),
        "system32_source_root": str(system32.resolve()),
        "rationale": (
            "Windows 10/11 supplies the API-set contracts and Universal CRT used by this build. "
            "Only binaries with reviewed names whose source parent is this build host's System32 "
            "directory are omitted; same-named files from CPython, the application venv, Qt, and "
            "other packages are retained. Any unreviewed System32-sourced binary fails the build."
        ),
        "excluded_count": len(excluded),
        "excluded_binaries": sorted(excluded, key=lambda row: str(row["archive_path"]).casefold()),
        "remaining_system32_binary_count": len(remaining_system32),
    }


identity = create_build_manifest(PROJECT_ROOT)
MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
MANIFEST_PATH.write_bytes(canonical_json_bytes(identity))

resource_root = PROJECT_ROOT / "pyeyedata" / "resources"
datas = [
    (
        str(path),
        (Path("pyeyedata") / "resources" / path.relative_to(resource_root).parent).as_posix(),
    )
    for path in collect_resource_files(PROJECT_ROOT)
]
runtime_metadata_distributions = (
    # _runtime_versions() records these as actual frozen-runtime observations.
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "PySide6-Essentials",
    "reportlab",
    "openpyxl",
    "fonttools",
    # These are shipped runtime backends, not build/test tools.
    "statsmodels",
    "patsy",
    "shiboken6",
    "multimatch-gaze",
)
for distribution_name in runtime_metadata_distributions:
    datas.extend(copy_metadata(distribution_name))

a = Analysis(
    [str(PROJECT_ROOT / "pyeyedata_launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "openpyxl",
        "matplotlib.backends.backend_svg",
        "matplotlib.backends.backend_pdf",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pluggy", "iniconfig", "setuptools", "pip", "tkinter", "test"],
    noarchive=False,
    optimize=0,
)

# Keep host OS API-set stubs and the System32 UCRT out of the application
# application archive. This is deliberately applied after Analysis has found
# dependencies and before PYZ/EXE are formed.
windows_system_exclusion_policy = _exclude_windows_system_os_binaries(a)

# The manifest depends on actual pinned inputs and resources. Append it after
# Analysis so it cannot hash itself or be mistaken for a source-tree resource.
a.datas.append((MANIFEST_ARCHIVE_PATH, str(MANIFEST_PATH), "DATA"))

splash_image = resource_root / "branding" / "pyeyedata-splash.png"
splash = Splash(
    str(splash_image),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    always_on_top=True,
)

# Splash performs a separate Tcl/Tk dependency scan. Apply the same reviewed
# System32 policy to that TOC before handing it to EXE, and record it alongside
# the regular Analysis inventory below.
splash_analysis = SimpleNamespace(binaries=list(splash.binaries))
splash_windows_system_exclusion_policy = _exclude_windows_system_os_binaries(
    splash_analysis
)
splash.binaries[:] = splash_analysis.binaries
splash_binary_rows = [
    row for row in splash.binaries if len(row) > 2 and str(row[2]).upper() == "BINARY"
]
splash_data_rows = [
    row for row in splash.binaries if len(row) > 2 and str(row[2]).upper() != "BINARY"
]

analysis_record = collect_analysis_records(
    a,
    PROJECT_ROOT,
    extra_binaries=splash_binary_rows,
    extra_datas=splash_data_rows,
)
analysis_record["build_phase"] = BUILD_PHASE
analysis_record["manifest_archive_path"] = MANIFEST_ARCHIVE_PATH
analysis_record["manifest_sha256"] = __import__("hashlib").sha256(
    MANIFEST_PATH.read_bytes()
).hexdigest()
analysis_record["manifest_resource_count"] = len(identity["resource_sha256"])
analysis_record["windows_system_exclusion_policy"] = windows_system_exclusion_policy
analysis_record["analysis_counts"] = {
    "scripts": len(a.scripts),
    "pure": len(a.pure),
    "binaries": len(analysis_record["binaries"]),
    "datas": len(analysis_record["datas"]),
    "splash_binary_toc_rows": len(splash_binary_rows),
    "splash_data_toc_rows": len(splash_data_rows),
}
analysis_record["splash_target"] = {
    "enabled": True,
    "image_resource": "pyeyedata/resources/branding/pyeyedata-splash.png",
    "image_sha256": hashlib.sha256(splash_image.read_bytes()).hexdigest(),
    "tcl_shared_library": str(splash.tcl_lib),
    "tk_shared_library": str(splash.tk_lib),
    "requirements": sorted(splash.splash_requirements, key=str.casefold),
    "system32_exclusion_policy": splash_windows_system_exclusion_policy,
}
evidence_path = RUN_ROOT / "evidence" / f"d02-analysis-{BUILD_PHASE}-v1.json"
evidence_path.parent.mkdir(parents=True, exist_ok=True)
evidence_path.write_bytes(canonical_json_bytes(analysis_record))

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash,
    splash.binaries,
    [],
    name="PYeyedata",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(VERSION_RESOURCE_PATH),
    icon=str(resource_root / "branding" / "pyeyedata.ico"),
)

