"""Build, check and package a clean Git commit as a Windows x64 release."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from build_manifest import _read_application_identity, sha256_file
from build_windows import _run_build, _verify_embedded_manifest
from run_checks import run_checks


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True,
                                   encoding="utf-8").strip()


def clean_commit() -> str:
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise RuntimeError("Commit all source changes before releasing; the Git source ZIP must match the EXE.")
    return git("rev-parse", "HEAD")


def verify_native_notices(build: dict) -> list[dict]:
    """Reconcile the actual native inventory with the maintained notices map."""
    resources = ROOT / "pyeyedata/resources"
    notices = json.loads((resources / "licenses/source-manifest.json").read_text(encoding="utf-8"))
    analysis = json.loads(Path(build["analysis_evidence"]).read_text(encoding="utf-8"))
    expected = {row["archive_path"].replace("\\", "/").casefold(): row
                for row in notices["native_components"]}
    actual = {row["archive_path"].replace("\\", "/").casefold(): row
              for row in analysis["binaries"]}
    if actual.keys() != expected.keys():
        raise RuntimeError("Native component inventory changed; reconcile source-manifest.json before publishing.")
    for key, row in actual.items():
        declared = expected[key]
        if row["source_sha256"] != declared["source_sha256"] or not declared["license_materials"]:
            raise RuntimeError(f"Native component hash or notice mapping changed: {row['archive_path']}")
        for relative in declared["license_materials"]:
            if not (resources / relative).is_file():
                raise RuntimeError(f"Native notice missing: {relative}")
    index = json.loads((resources / "resource-index.json").read_text(encoding="utf-8"))
    for relative, expected_hash in index["licenses"]["material_sha256"].items():
        if sha256_file(resources / relative) != expected_hash:
            raise RuntimeError(f"Indexed license hash mismatch: {relative}")
    return notices["native_components"]


def package(output: Path) -> Path:
    commit = clean_commit()
    identity = _read_application_identity(ROOT)
    version = identity["version"]
    output = output.resolve()
    if output == ROOT or output.is_relative_to(ROOT):
        raise ValueError("Release output must be outside the source checkout.")
    output.mkdir(parents=True, exist_ok=False)
    source_checks = run_checks(output / "source-checks")
    print("Building final executable (logs are written after PyInstaller completes)...", flush=True)
    build = _run_build("final", output)
    native_components = verify_native_notices(build)
    executable = Path(build["executable"]["path"])
    frozen_checks = run_checks(output / "frozen-checks", executable)
    _verify_embedded_manifest(executable, output)
    if clean_commit() != commit:
        raise RuntimeError("Git HEAD changed during release; refuse to package mixed inputs.")
    if any(case["application"] != identity for checks in (source_checks, frozen_checks)
           for case in checks["scenarios"]):
        raise RuntimeError("Diagnostic application version does not match release identity.")

    assets = output / "assets"
    assets.mkdir()
    stem = f"PYeyedata-{version}"
    binary_name = f"{stem}-windows-x64.exe"
    shutil.copy2(executable, assets / binary_name)
    info = {
        "schema": "pyeyedata.release/v1", "application": identity,
        "git_commit": commit, "tag": f"v{version}",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "target": "Windows 10/11 x64", "signed": False,
        "python": build["python"], "pyinstaller": build["pyinstaller"],
        "executable_sha256": sha256_file(assets / binary_name),
        "manifest": build["manifest"], "pe_version": build["pe_version"],
        "native_notice_map": {"status": "pass", "components": len(native_components)},
        "source_checks": source_checks, "frozen_checks": frozen_checks,
        "limits": ["Synthetic offscreen checks only; no real-device validation.",
                   "Clean-host, native DPI and multi-monitor acceptance are not claimed."],
    }
    info_path = assets / "release-info.json"
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    native_path = assets / "native-components.json"
    native_path.write_text(json.dumps(native_components, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(assets / f"{stem}-windows-x64.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, "PYeyedata.exe")
        archive.write(ROOT / "docs" / "DEPLOYMENT.md", "DEPLOYMENT.md")
        archive.write(ROOT / "docs" / "RELEASE_NOTES.md", "RELEASE_NOTES.md")
        archive.write(ROOT / "docs" / "COMMON_METRICS.md", "COMMON_METRICS.md")
        archive.write(ROOT / "docs" / "WORKFLOW_ENHANCEMENTS.md", "WORKFLOW_ENHANCEMENTS.md")
        archive.write(info_path, "release-info.json")
        archive.write(native_path, "native-components.json")
        for path in sorted((ROOT / "pyeyedata" / "resources" / "licenses").rglob("*")):
            if path.is_file():
                archive.write(path, "licenses/" + path.relative_to(ROOT / "pyeyedata" / "resources" / "licenses").as_posix())
        archive.write(ROOT / "pyeyedata" / "resources" / "fonts" / "OFL.txt", "licenses/fonts/OFL.txt")
    source_zip = assets / f"{stem}-source.zip"
    subprocess.run(["git", "-C", str(ROOT), "archive", "--format=zip",
                    f"--prefix={stem}/", "-o", str(source_zip), commit], check=True)
    with zipfile.ZipFile(source_zip) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Source ZIP integrity check failed")
        archived_identity = archive.read(f"{stem}/pyeyedata/app_info.py").replace(b"\r\n", b"\n")
        if archived_identity != (ROOT / "pyeyedata/app_info.py").read_bytes().replace(b"\r\n", b"\n"):
            raise RuntimeError("Source ZIP application identity differs from checkout")
    with zipfile.ZipFile(assets / f"{stem}-windows-x64.zip") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Portable ZIP integrity check failed")
    (assets / "SHA256SUMS.txt").write_text("".join(
        f"{sha256_file(path)}  {path.name}\n" for path in sorted(assets.iterdir()) if path.is_file()
    ), encoding="utf-8")
    print(f"Release assets: {assets}", flush=True)
    return assets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        help="New directory outside the source checkout (default: unique TEMP directory)")
    args = parser.parse_args()
    output = args.output or Path(tempfile.gettempdir()) / f"PYeyedata-release-{uuid.uuid4().hex}"
    package(output)


if __name__ == "__main__":
    main()
