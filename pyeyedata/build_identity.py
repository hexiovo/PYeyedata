"""Validate embedded build provenance without importing GUI or scientific code.

These hashes describe files supplied to the build, not physical Python sources
observed inside a frozen process. Runtime versions are collected separately.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Mapping

BUILD_MANIFEST_SCHEMA = "pyeyedata.build-manifest/v1"
BUILD_MANIFEST_RESOURCE = "build/build-manifest.json"
MAX_BUILD_MANIFEST_BYTES = 4 * 1024 * 1024
_MANIFEST_REPOSITORY_PATH = "pyeyedata/resources/" + BUILD_MANIFEST_RESOURCE
_CORE_NAMES = (
    "__init__.py", "advanced_signals.py", "aoi_geometry.py", "app_info.py",
    "archive_session.py", "build_identity.py", "contracts.py", "event_queries.py",
    "events.py", "environment.py", "execution_cache.py", "exports.py", "eye_streams.py", "group.py",
    "individual.py", "io.py", "pipeline.py", "processing.py", "reporting.py",
    "resource_paths.py", "scopes.py", "statistics.py", "store.py",
    "methods/__init__.py", "methods/scanpath.py",
)
_GUI_NAMES = ("__init__.py", "app.py", "main_window.py", "bridge.py", "models.py", "session.py", "tasks.py", "environment_dialog.py")
REQUIRED_SOURCE_KEYS = frozenset(
    ["pyeyedata/" + name for name in _CORE_NAMES]
    + ["pyeyedata/gui/" + name for name in _GUI_NAMES]
    + ["pyeyedata_launcher.py", "pyeyedata_bootstrap.py", "pyeyedata_startup.py"]
)
_REQUIRED_DISTRIBUTIONS = frozenset({
    "numpy", "pandas", "scipy", "matplotlib", "pyside6-essentials",
    "reportlab", "openpyxl", "fonttools",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BuildIdentityError(ValueError):
    """The frozen build cannot establish truthful, compatible provenance."""


def _fail(message: str) -> None:
    raise BuildIdentityError("Frozen build identity: " + message)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key " + repr(key))
        result[key] = value
    return result


def _nonfinite(value):
    _fail("nonfinite JSON constant " + str(value))


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(label + " must be nonempty text")
    return value


def _path(value: Any) -> str:
    value = _text(value, "inventory path")
    if ("\\" in value or "\x00" in value or PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).drive
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        _fail("inventory path must be canonical repository-relative POSIX: " + repr(value))
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(label + " must be a lowercase SHA-256 hex digest")
    return value


def _hash_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        _fail(label + " must be a nonempty hash map")
    return {_path(key): _digest(digest, label + "[" + repr(key) + "]")
            for key, digest in value.items()}


@dataclass(frozen=True)
class BuildIdentity:
    """One validated resource; projections return detached legacy-key maps."""

    _manifest: dict[str, Any]
    manifest_sha256: str

    @property
    def version_document_sha256(self) -> str:
        return self._manifest["version_document_sha256"]

    @property
    def source_sha256(self) -> Mapping[str, str]:
        """Return a complete, read-only source hash projection detached from state."""
        return MappingProxyType(dict(self._manifest["source_sha256"]))

    @property
    def resource_sha256(self) -> Mapping[str, str]:
        """Return a complete, read-only resource hash projection detached from state."""
        return MappingProxyType(dict(self._manifest["resource_sha256"]))

    def software_sha256(self) -> dict[str, str]:
        output = {}
        for path, digest in sorted(self._manifest["source_sha256"].items()):
            if not path.startswith("pyeyedata/"):
                continue
            relative = path[len("pyeyedata/"):]
            parts = relative.split("/")
            if relative.endswith(".py") and (len(parts) == 1 or len(parts) == 2 and parts[0] == "methods"):
                output[relative] = digest
        return output

    def application_source_sha256(self) -> dict[str, str]:
        output = {}
        for path, digest in sorted(self._manifest["source_sha256"].items()):
            if not path.startswith("pyeyedata/"):
                continue
            relative = path[len("pyeyedata/"):]
            parts = relative.split("/")
            if relative in {"archive_session.py", "app_info.py"} or (
                    len(parts) == 2 and parts[0] == "gui" and relative.endswith(".py")):
                output[relative] = digest
        return output

    def archive_metadata(self) -> dict[str, Any]:
        return {"origin": "embedded-build-manifest", "manifest_sha256": self.manifest_sha256,
                "manifest": copy.deepcopy(self._manifest)}


def parse_build_manifest(
    payload: bytes, *, application: Mapping[str, str], contracts: Mapping[str, str],
) -> BuildIdentity:
    """Validate a bounded JSON payload against actual running version constants."""
    if not isinstance(payload, bytes):
        _fail("resource payload must be bytes")
    if not payload or len(payload) > MAX_BUILD_MANIFEST_BYTES:
        _fail("resource exceeds the nonempty 4 MiB JSON limit")
    try:
        manifest = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_object_pairs,
                              parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BuildIdentityError("Frozen build identity: invalid UTF-8 JSON resource") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != BUILD_MANIFEST_SCHEMA:
        _fail("missing or unsupported manifest schema")
    for section, expected, names in (
        ("application", application, ("name", "version", "prd_version")),
        ("contracts", contracts, ("data_contract_version", "pipeline_schema_version")),
    ):
        actual = manifest.get(section)
        if not isinstance(actual, dict) or not isinstance(expected, Mapping):
            _fail(section + " must be an object with expected runtime constants")
        for name in names:
            wanted = _text(expected.get(name), "expected " + section + "." + name)
            if _text(actual.get(name), section + "." + name) != wanted:
                _fail(section + "." + name + " differs from the running application")
    sources = _hash_map(manifest.get("source_sha256"), "source_sha256")
    missing = sorted(REQUIRED_SOURCE_KEYS - sources.keys())
    if missing:
        _fail("missing critical source entries: " + ", ".join(missing))
    resources = _hash_map(manifest.get("resource_sha256"), "resource_sha256")
    if any(not path.startswith("pyeyedata/resources/") for path in resources):
        _fail("resource inventory must stay under pyeyedata/resources/")
    if _MANIFEST_REPOSITORY_PATH in sources or _MANIFEST_REPOSITORY_PATH in resources:
        _fail("manifest inventory cannot contain a self-reference")
    version_digest = _digest(manifest.get("version_document_sha256"), "version_document_sha256")
    if "version.md" in sources and sources["version.md"] != version_digest:
        _fail("version document digest disagrees with the source inventory")
    environment = manifest.get("build_environment")
    if not isinstance(environment, dict):
        _fail("build_environment must be an object")
    for name in ("python", "platform", "architecture"):
        _text(environment.get(name), "build_environment." + name)
    distributions = environment.get("distributions")
    if not isinstance(distributions, dict) or not distributions:
        _fail("build_environment.distributions must contain observed version strings")
    normalized = set()
    for name, version in distributions.items():
        name = _text(name, "distribution name")
        _text(version, "build distribution version")
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if canonical in normalized:
            _fail("duplicate canonical distribution name " + name)
        normalized.add(canonical)
    if not _REQUIRED_DISTRIBUTIONS.issubset(normalized):
        _fail("build environment lacks critical distribution versions: "
              + ", ".join(sorted(_REQUIRED_DISTRIBUTIONS - normalized)))
    return BuildIdentity(copy.deepcopy(manifest), hashlib.sha256(payload).hexdigest())


def load_build_identity(
    *, application: Mapping[str, str], contracts: Mapping[str, str],
) -> BuildIdentity:
    """Read only the package resource; never consult a checkout or adjacent files."""
    from .resource_paths import read_resource_bytes

    try:
        payload = read_resource_bytes(BUILD_MANIFEST_RESOURCE)
    except (OSError, ValueError, TypeError) as exc:
        raise BuildIdentityError("Frozen build identity: bundled build manifest is unavailable") from exc
    return parse_build_manifest(payload, application=application, contracts=contracts)
