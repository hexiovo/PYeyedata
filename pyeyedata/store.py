"""Safe, ZIP-compatible ``.pyea`` archives with checksums and atomic replacement.

Archives are never extracted or executed by this module. Callers exchange bytes, text,
or paths through the explicit member mapping and consume contents with ``read_member``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
import numbers
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from typing import Any, BinaryIO, Callable, Mapping
import zipfile

import numpy as np
import pandas as pd

from .contracts import CONTRACT_VERSION, SUPPORTED_CONTRACT_VERSIONS

ARCHIVE_FORMAT = "pyeyedata.pyea"
ARCHIVE_SCHEMA_VERSION = 1
MEMBER_SCHEMA_VERSION = "1.0.0"
STRUCTURED_MEMBER_SCHEMA_VERSION = "1.1.0"
IDENTITY_MEMBER_SCHEMA_VERSION = "1.2.0"
SUPPORTED_MEMBER_SCHEMA_VERSIONS = frozenset({MEMBER_SCHEMA_VERSION, STRUCTURED_MEMBER_SCHEMA_VERSION, IDENTITY_MEMBER_SCHEMA_VERSION})
MANIFEST_NAME = "manifest.json"
MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024**3
MAX_ARCHIVE_MEMBERS = 10_000
MAX_COMPRESSION_RATIO = 1_000
MAX_MANIFEST_BYTES = 1024**2
CHUNK_SIZE = 1024**2
_NULL_TOKEN = "__PYEADATA_NULL_v1__"
_ESCAPE_TOKEN = "__PYEADATA_ESC_v1__"
_TYPE_PREFIX = "__PYEADATA_TYPE_v1__"

_FORBIDDEN_SUFFIXES = frozenset({
    ".py", ".pyc", ".pyo", ".pyd", ".exe", ".dll", ".so", ".dylib",
    ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".js", ".msi", ".com", ".scr",
    ".pkl", ".pickle", ".xlsm", ".xlsb", ".docm", ".pptm", ".html", ".htm",
    ".svg",
})


class ArchiveError(ValueError):
    """A ``.pyea`` archive is unsafe, incomplete, or does not match its manifest."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = MAX_ARCHIVE_BYTES
    max_member_bytes: int = MAX_MEMBER_BYTES
    max_total_uncompressed_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES
    max_members: int = MAX_ARCHIVE_MEMBERS
    max_compression_ratio: float = MAX_COMPRESSION_RATIO
    max_manifest_bytes: int = MAX_MANIFEST_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_archive_bytes", "max_member_bytes", "max_total_uncompressed_bytes",
            "max_members", "max_compression_ratio", "max_manifest_bytes",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ArchiveError(f"{name} must be positive")


@dataclass(frozen=True)
class ArchiveMemberInfo:
    name: str
    size: int
    sha256: str
    compressed_size: int
    compression: str
    schema: dict[str, Any] | None = None


@dataclass
class PyeaArchive:
    """Validated archive metadata; members are read on demand and re-hashed."""

    path: Path
    manifest: dict[str, Any]
    members: dict[str, ArchiveMemberInfo]
    limits: ArchiveLimits

    @property
    def mode(self) -> str:
        return str(self.manifest["mode"])

    def read_member(self, name: str) -> bytes:
        if name not in self.members:
            raise ArchiveError(f"Archive member not found: {name}")
        info = self.members[name]
        if info.size > self.limits.max_member_bytes:
            raise ArchiveError(f"Archive member exceeds the read limit: {name}")
        digest = hashlib.sha256()
        contents = bytearray()
        try:
            with zipfile.ZipFile(self.path, "r") as archive:
                with archive.open(name, "r") as source:
                    while chunk := source.read(CHUNK_SIZE):
                        contents.extend(chunk)
                        digest.update(chunk)
                        if len(contents) > self.limits.max_member_bytes:
                            raise ArchiveError(f"Archive member expanded beyond the read limit: {name}")
        except (OSError, zipfile.BadZipFile, RuntimeError, zipfile.LargeZipFile) as exc:
            raise ArchiveError(f"Could not read archive member {name!r}: {exc}") from exc
        if len(contents) != info.size or digest.hexdigest() != info.sha256:
            raise ArchiveError(f"Archive member checksum mismatch: {name}")
        return bytes(contents)


def write_pyea(
    path: str | os.PathLike[str],
    members: Mapping[str, bytes | bytearray | memoryview | str | os.PathLike[str]],
    *,
    mode: str = "full",
    metadata: Mapping[str, Any] | None = None,
    member_schemas: Mapping[str, Mapping[str, Any]] | None = None,
    limits: ArchiveLimits | None = None,
) -> PyeaArchive:
    """Write a checksummed archive via a verified sibling temp file then atomic replace.

    ``str`` member values are UTF-8 text; use ``Path`` values to stream larger files.
    The manifest is generated here and cannot be supplied as an ordinary member.
    """
    settings = limits or ArchiveLimits()
    destination = Path(path).expanduser().absolute()
    if destination.suffix.lower() != ".pyea":
        raise ArchiveError("Archive filename must use the .pyea suffix")
    if mode not in {"full", "results_only"}:
        raise ArchiveError("mode must be 'full' or 'results_only'")
    if not isinstance(members, Mapping) or not members:
        raise ArchiveError("At least one archive member is required")
    try:
        metadata_dict = dict(metadata or {})
    except (TypeError, ValueError) as exc:
        raise ArchiveError("metadata must be an object") from exc
    try:
        _validate_json_data(metadata_dict, "metadata")
        _encode_json(metadata_dict)
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"metadata is not JSON-safe: {exc}") from exc

    normalized: dict[str, bytes | bytearray | memoryview | str | os.PathLike[str]] = {}
    seen_casefolded: set[str] = set()
    for raw_name, source in members.items():
        name = _validate_member_name(raw_name)
        folded = name.casefold()
        if folded in seen_casefolded:
            raise ArchiveError(f"Duplicate or case-colliding archive member: {name}")
        seen_casefolded.add(folded)
        if folded == MANIFEST_NAME.casefold():
            raise ArchiveError("manifest.json is generated by write_pyea and cannot be overridden")
        suffix = PurePosixPath(name).suffix.casefold()
        if suffix in _FORBIDDEN_SUFFIXES:
            raise ArchiveError(f"Executable, macro-enabled, or active content is not allowed: {name}")
        if not isinstance(source, (bytes, bytearray, memoryview, str, os.PathLike)):
            raise ArchiveError(f"Unsupported source value for archive member {name!r}")
        normalized[name] = source
    schemas: dict[str, dict[str, Any]] = {}
    for raw_name, schema in (member_schemas or {}).items():
        name = _validate_member_name(raw_name)
        if name not in normalized:
            raise ArchiveError(f"Schema supplied for a missing archive member: {name}")
        if not isinstance(schema, Mapping):
            raise ArchiveError(f"Member schema must be an object: {name}")
        schema_dict = dict(schema)
        schema_dict.setdefault("member_schema_version", MEMBER_SCHEMA_VERSION)
        _validate_member_schema(schema_dict, CONTRACT_VERSION, name=name)
        _validate_json_data(schema_dict, f"member_schemas[{name!r}]")
        schemas[name] = schema_dict
    if len(normalized) + 1 > settings.max_members:
        raise ArchiveError("Archive has too many members")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".pyea", dir=destination.parent)
        temporary_path = Path(temp_name)
        with os.fdopen(fd, "w+b") as raw_output:
            records: dict[str, dict[str, Any]] = {}
            with zipfile.ZipFile(raw_output, "w", allowZip64=True) as archive:
                for name in sorted(normalized):
                    source = normalized[name]
                    digest = hashlib.sha256()
                    size = 0
                    with _open_source(source) as input_stream:
                        zip_info = zipfile.ZipInfo(name)
                        zip_info.compress_type = zipfile.ZIP_DEFLATED
                        zip_info.external_attr = 0o600 << 16
                        with archive.open(zip_info, "w", force_zip64=True) as target:
                            while chunk := input_stream.read(CHUNK_SIZE):
                                size += len(chunk)
                                if size > settings.max_member_bytes:
                                    raise ArchiveError(f"Member exceeds size limit while writing: {name}")
                                target.write(chunk)
                                digest.update(chunk)
                    record = {"size": size, "sha256": digest.hexdigest()}
                    if name in schemas:
                        record["schema"] = schemas[name]
                    records[name] = record
                    if raw_output.tell() > settings.max_archive_bytes:
                        raise ArchiveError("Archive exceeds the compressed-size limit while writing")
                manifest = {
                    "format": ARCHIVE_FORMAT,
                    "schema_version": ARCHIVE_SCHEMA_VERSION,
                    "contract_version": CONTRACT_VERSION,
                    "mode": mode,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "metadata": metadata_dict,
                    "members": records,
                }
                manifest_bytes = _encode_json(manifest)
                if len(manifest_bytes) > settings.max_manifest_bytes:
                    raise ArchiveError("Archive manifest exceeds its size limit")
                manifest_info = zipfile.ZipInfo(MANIFEST_NAME)
                manifest_info.compress_type = zipfile.ZIP_STORED
                manifest_info.external_attr = 0o600 << 16
                archive.writestr(manifest_info, manifest_bytes)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        if temporary_path.stat().st_size > settings.max_archive_bytes:
            raise ArchiveError("Archive exceeds the compressed-size limit")
        # Reopen and verify every member before replacing an existing archive.
        read_pyea(temporary_path, limits=settings)
        os.replace(temporary_path, destination)
        temporary_path = None
        return read_pyea(destination, limits=settings)
    except ArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        raise ArchiveError(f"Could not write archive: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def write_pyea_extension(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    members: Mapping[str, bytes | bytearray | memoryview | str | os.PathLike[str]],
    *,
    metadata_update: Mapping[str, Any],
    member_schemas: Mapping[str, Mapping[str, Any]] | None = None,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
    limits: ArchiveLimits | None = None,
) -> PyeaArchive:
    """Safely copy a validated archive while streaming in additional members.

    Existing and added member payloads are copied one at a time. The source is
    validated before copying, each copied member is re-hashed, and the final
    sibling stage is reopened through :func:`read_pyea` before atomically
    replacing the destination.
    """
    settings = limits or ArchiveLimits()
    source = Path(source_path).expanduser().resolve(strict=True)
    destination = Path(destination_path).expanduser().absolute()
    if destination.suffix.lower() != ".pyea":
        raise ArchiveError("Archive filename must use the .pyea suffix")
    base = read_pyea(source, limits=settings)
    if not isinstance(metadata_update, Mapping):
        raise ArchiveError("metadata_update must be an object")
    try:
        metadata = json.loads(_encode_json(base.manifest["metadata"]).decode("utf-8"))
        metadata.update(dict(metadata_update))
        _validate_json_data(metadata, "metadata")
        manifest_preview = _encode_json(metadata)
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"metadata_update is not JSON-safe: {exc}") from exc
    if len(manifest_preview) > settings.max_manifest_bytes:
        raise ArchiveError("Archive metadata exceeds the manifest limit")
    if not isinstance(members, Mapping):
        raise ArchiveError("members must be a mapping")

    normalized: dict[str, bytes | bytearray | memoryview | str | os.PathLike[str]] = {}
    schemas: dict[str, dict[str, Any]] = {}
    occupied = {name.casefold() for name in base.members}
    for raw_name, value in members.items():
        name = _validate_member_name(raw_name)
        folded = name.casefold()
        if folded in occupied:
            raise ArchiveError(f"Duplicate or case-colliding archive member: {name}")
        occupied.add(folded)
        if name.casefold() == MANIFEST_NAME.casefold():
            raise ArchiveError("manifest.json is generated by the archive writer")
        if PurePosixPath(name).suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise ArchiveError(f"Executable, macro-enabled, or active content is not allowed: {name}")
        if not isinstance(value, (bytes, bytearray, memoryview, str, os.PathLike)):
            raise ArchiveError(f"Unsupported source value for archive member {name!r}")
        normalized[name] = value
    if len(base.members) + len(normalized) + 1 > settings.max_members:
        raise ArchiveError("Archive has too many members")
    for raw_name, schema in (member_schemas or {}).items():
        name = _validate_member_name(raw_name)
        if name not in normalized or not isinstance(schema, Mapping):
            raise ArchiveError(f"Invalid added member schema: {name}")
        schema_dict = dict(schema)
        schema_dict.setdefault("member_schema_version", MEMBER_SCHEMA_VERSION)
        _validate_member_schema(schema_dict, CONTRACT_VERSION, name=name)
        _validate_json_data(schema_dict, f"member_schemas[{name!r}]")
        schemas[name] = schema_dict

    base_total = sum(info.size for info in base.members.values())
    total_uncompressed = base_total
    new_member_sizes: dict[str, int] = {}
    for name, value in normalized.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            size = len(value)
        elif isinstance(value, os.PathLike):
            try:
                size = Path(value).expanduser().stat().st_size
            except OSError as exc:
                raise ArchiveError(f"Could not inspect added member {name!r}: {exc}") from exc
        else:
            size = len(value.encode("utf-8"))
        if size > settings.max_member_bytes:
            raise ArchiveError(f"Member exceeds size limit while writing: {name}")
        total_uncompressed += size
        if total_uncompressed > settings.max_total_uncompressed_bytes:
            raise ArchiveError("Archive exceeds the total uncompressed-size limit")
        new_member_sizes[name] = size

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    total_bytes = max(1, total_uncompressed)
    copied_bytes = 0
    records: dict[str, dict[str, Any]] = {}
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".pyea", dir=destination.parent)
        temporary_path = Path(temp_name)
        with os.fdopen(fd, "w+b") as raw_output:
            with zipfile.ZipFile(source, "r") as source_archive, zipfile.ZipFile(raw_output, "w", allowZip64=True) as target_archive:
                for name in sorted(base.members):
                    if cancel and cancel():
                        raise ArchiveError("Archive extension cancelled; existing destination is untouched")
                    info = base.members[name]
                    old_zip_info = source_archive.getinfo(name)
                    zip_info = zipfile.ZipInfo(name, date_time=old_zip_info.date_time)
                    zip_info.compress_type = old_zip_info.compress_type
                    zip_info.external_attr = 0o600 << 16
                    digest = hashlib.sha256()
                    size = 0
                    with source_archive.open(name, "r") as source_member, target_archive.open(zip_info, "w", force_zip64=True) as target_member:
                        while chunk := source_member.read(CHUNK_SIZE):
                            if cancel and cancel():
                                raise ArchiveError("Archive extension cancelled; existing destination is untouched")
                            target_member.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                            copied_bytes += len(chunk)
                            if progress:
                                progress(min(0.98, copied_bytes / total_bytes), f"扩展档案成员：{name}")
                    if size != info.size or digest.hexdigest() != info.sha256:
                        raise ArchiveError(f"Archive member changed during extension: {name}")
                    record = {"size": size, "sha256": digest.hexdigest()}
                    if info.schema is not None:
                        record["schema"] = info.schema
                    records[name] = record

                for name in sorted(normalized):
                    if cancel and cancel():
                        raise ArchiveError("Archive extension cancelled; existing destination is untouched")
                    digest = hashlib.sha256()
                    size = 0
                    with _open_source(normalized[name]) as source_member, target_archive.open(name, "w", force_zip64=True) as target_member:
                        while chunk := source_member.read(CHUNK_SIZE):
                            if cancel and cancel():
                                raise ArchiveError("Archive extension cancelled; existing destination is untouched")
                            size += len(chunk)
                            if size > settings.max_member_bytes:
                                raise ArchiveError(f"Member exceeds size limit while writing: {name}")
                            target_member.write(chunk)
                            digest.update(chunk)
                            copied_bytes += len(chunk)
                            if progress:
                                progress(min(0.98, copied_bytes / total_bytes), f"写入档案成员：{name}")
                    if size != new_member_sizes[name]:
                        raise ArchiveError(f"Added archive member changed during extension: {name}")
                    record = {"size": size, "sha256": digest.hexdigest()}
                    if name in schemas:
                        record["schema"] = schemas[name]
                    records[name] = record
                manifest = {
                    "format": ARCHIVE_FORMAT,
                    "schema_version": ARCHIVE_SCHEMA_VERSION,
                    "contract_version": CONTRACT_VERSION,
                    "mode": base.mode,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "metadata": metadata,
                    "members": records,
                }
                manifest_bytes = _encode_json(manifest)
                if len(manifest_bytes) > settings.max_manifest_bytes:
                    raise ArchiveError("Archive manifest exceeds its size limit")
                manifest_info = zipfile.ZipInfo(MANIFEST_NAME)
                manifest_info.compress_type = zipfile.ZIP_STORED
                manifest_info.external_attr = 0o600 << 16
                target_archive.writestr(manifest_info, manifest_bytes)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        if temporary_path.stat().st_size > settings.max_archive_bytes:
            raise ArchiveError("Archive exceeds the compressed-size limit")
        read_pyea(temporary_path, limits=settings)
        if cancel and cancel():
            raise ArchiveError("Archive extension cancelled; existing destination is untouched")
        os.replace(temporary_path, destination)
        temporary_path = None
        if progress:
            progress(1.0, "档案已验证并保存")
        return read_pyea(destination, limits=settings)
    except ArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, ValueError) as exc:
        raise ArchiveError(f"Could not extend archive safely: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def read_pyea(path: str | os.PathLike[str], *, limits: ArchiveLimits | None = None) -> PyeaArchive:
    """Validate ZIP structure, manifest, path safety, size limits, CRC, and every SHA-256."""
    settings = limits or ArchiveLimits()
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ArchiveError("Archive path is not a regular file")
    if source.suffix.lower() != ".pyea":
        raise ArchiveError("Archive filename must use the .pyea suffix")
    if source.stat().st_size > settings.max_archive_bytes:
        raise ArchiveError("Archive exceeds the compressed-size limit")
    try:
        with zipfile.ZipFile(source, "r") as archive:
            entries = archive.infolist()
            if len(entries) > settings.max_members:
                raise ArchiveError("Archive has too many members")
            by_name: dict[str, zipfile.ZipInfo] = {}
            seen_casefolded: set[str] = set()
            total_size = 0
            for entry in entries:
                name = _validate_member_name(entry.filename)
                folded = name.casefold()
                if folded in seen_casefolded:
                    raise ArchiveError(f"Duplicate or case-colliding archive member: {name}")
                seen_casefolded.add(folded)
                if entry.flag_bits & 0x1:
                    raise ArchiveError("Encrypted archive members are not supported")
                if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ArchiveError(f"Unsupported ZIP compression method for {name}")
                if PurePosixPath(name).suffix.casefold() in _FORBIDDEN_SUFFIXES:
                    raise ArchiveError(f"Executable, macro-enabled, or active content is not allowed: {name}")
                mode_bits = entry.external_attr >> 16
                if mode_bits and (mode_bits & 0o170000) == 0o120000:
                    raise ArchiveError(f"Symbolic links are not allowed in archives: {name}")
                if entry.file_size < 0 or entry.compress_size < 0 or entry.file_size > settings.max_member_bytes:
                    raise ArchiveError(f"Invalid or oversized archive member: {name}")
                total_size += entry.file_size
                if total_size > settings.max_total_uncompressed_bytes:
                    raise ArchiveError("Archive exceeds the total uncompressed-size limit")
                if entry.file_size and entry.compress_size == 0:
                    raise ArchiveError(f"Invalid compressed-size metadata for {name}")
                if entry.compress_size and entry.file_size / entry.compress_size > settings.max_compression_ratio:
                    raise ArchiveError(f"Suspicious compression ratio for {name}")
                if name != entry.filename:
                    raise ArchiveError(f"Non-canonical archive member path: {entry.filename}")
                by_name[name] = entry
            if MANIFEST_NAME not in by_name:
                raise ArchiveError("Archive manifest.json is missing")
            manifest_entry = by_name[MANIFEST_NAME]
            if manifest_entry.file_size > settings.max_manifest_bytes:
                raise ArchiveError("Archive manifest exceeds its size limit")
            manifest_bytes = archive.read(manifest_entry)
            try:
                manifest = json.loads(
                    manifest_bytes.decode("utf-8"),
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number {value}")),
                )
            except (UnicodeError, ValueError) as exc:
                raise ArchiveError(f"Archive manifest is not valid UTF-8 JSON: {exc}") from exc
            if not isinstance(manifest, dict):
                raise ArchiveError("Archive manifest must be a JSON object")
            required_manifest_keys = {
                "format", "schema_version", "contract_version", "mode", "created_at_utc", "metadata", "members"
            }
            if not required_manifest_keys.issubset(manifest):
                raise ArchiveError("Archive manifest is missing required keys")
            if manifest.get("format") != ARCHIVE_FORMAT or manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
                raise ArchiveError("Unsupported archive format or schema version")
            if manifest.get("contract_version") not in SUPPORTED_CONTRACT_VERSIONS:
                raise ArchiveError("Archive data-contract version is unsupported")
            if manifest.get("mode") not in {"full", "results_only"}:
                raise ArchiveError("Archive manifest has an invalid mode")
            if not isinstance(manifest.get("metadata"), dict):
                raise ArchiveError("Archive manifest metadata must be an object")
            _validate_json_data(manifest["metadata"], "manifest.metadata")
            if not isinstance(manifest.get("created_at_utc"), str):
                raise ArchiveError("Archive manifest created_at_utc must be a string")
            try:
                datetime.fromisoformat(manifest["created_at_utc"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ArchiveError("Archive manifest created_at_utc is invalid") from exc
            expected = manifest.get("members")
            if not isinstance(expected, dict):
                raise ArchiveError("Archive manifest members must be an object")
            if set(expected) != set(by_name) - {MANIFEST_NAME}:
                raise ArchiveError("Archive member list does not match the manifest")
            member_infos: dict[str, ArchiveMemberInfo] = {}
            for name, entry in by_name.items():
                if name == MANIFEST_NAME:
                    continue
                record = expected[name]
                if not isinstance(record, dict):
                    raise ArchiveError(f"Manifest record must be an object for {name}")
                if not {"size", "sha256"}.issubset(record) or set(record) - {"size", "sha256", "schema"}:
                    raise ArchiveError(f"Manifest record is missing required keys or has unknown keys for {name}")
                if "schema" in record and not isinstance(record["schema"], dict):
                    raise ArchiveError(f"Manifest schema must be an object for {name}")
                if "schema" in record:
                    _validate_json_data(record["schema"], f"manifest.members[{name!r}].schema")
                    _validate_member_schema(record["schema"], manifest["contract_version"], name=name)
                size = record.get("size")
                sha256 = record.get("sha256")
                if isinstance(size, bool) or not isinstance(size, int) or size != entry.file_size:
                    raise ArchiveError(f"Manifest size mismatch for {name}")
                if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
                    raise ArchiveError(f"Manifest SHA-256 is invalid for {name}")
                digest = hashlib.sha256()
                count = 0
                try:
                    with archive.open(entry, "r") as member:
                        while chunk := member.read(CHUNK_SIZE):
                            digest.update(chunk)
                            count += len(chunk)
                            if count > settings.max_member_bytes:
                                raise ArchiveError(f"Member expanded beyond the size limit: {name}")
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise ArchiveError(f"Could not verify archive member {name!r}: {exc}") from exc
                if count != size or digest.hexdigest() != sha256:
                    raise ArchiveError(f"SHA-256 mismatch for archive member {name}")
                member_infos[name] = ArchiveMemberInfo(
                    name=name,
                    size=size,
                    sha256=sha256,
                    compressed_size=entry.compress_size,
                    compression="stored" if entry.compress_type == zipfile.ZIP_STORED else "deflate",
                    schema=record.get("schema"),
                )
    except ArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise ArchiveError(f"Could not open archive safely: {exc}") from exc
    return PyeaArchive(source, manifest, member_infos, settings)


def _validate_member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise ArchiveError("Archive member names must be nonempty relative POSIX paths")
    path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in path.parts)
        or name != path.as_posix()
        or name.endswith("/")
    ):
        raise ArchiveError(f"Unsafe archive member path: {name!r}")
    return name


def _open_source(source: bytes | bytearray | memoryview | str | os.PathLike[str]) -> BinaryIO:
    if isinstance(source, bytes):
        import io
        return io.BytesIO(source)
    if isinstance(source, (bytearray, memoryview)):
        import io
        return io.BytesIO(bytes(source))
    if isinstance(source, str):
        import io
        return io.BytesIO(source.encode("utf-8"))
    path = Path(source).expanduser().resolve(strict=True)
    if path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
        raise ArchiveError(f"Executable, macro-enabled, or active source content is not allowed: {path.suffix}")
    if not path.is_file():
        raise ArchiveError(f"Archive source is not a regular file: {path.name}")
    return path.open("rb")


def _encode_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def dataframe_to_csv_member(frame: pd.DataFrame) -> tuple[bytes, dict[str, Any]]:
    """Encode a DataFrame as UTF-8 CSV with a dtype schema and reversible null/formula tags.

    Text cells that could be interpreted as spreadsheet formulas are prefixed with a
    reserved token; ``dataframe_from_csv_member`` removes it when the archive is reopened.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ArchiveError("CSV archive members require a pandas DataFrame")
    if len(frame.columns) == 0:
        raise ArchiveError("CSV archive tables must have at least one column")
    if frame.columns.has_duplicates or any(not isinstance(column, str) for column in frame.columns):
        raise ArchiveError("CSV archive columns must be unique strings")
    prepared = frame.copy(deep=True)
    structured_cells = any(
        str(prepared[name].dtype) == "object"
        and any(isinstance(value, (tuple, list, dict)) for value in prepared[name].tolist())
        for name in prepared.columns
    )
    identity_missing = any(
        str(prepared[name].dtype) == "object"
        and any(value is None or value is pd.NaT or isinstance(value, (float, np.floating)) and math.isnan(float(value))
                for value in prepared[name].tolist()) for name in prepared.columns
    )
    columns: list[dict[str, str]] = []
    for name in prepared.columns:
        series = prepared[name]
        dtype_name = str(series.dtype)
        columns.append({"name": name, "dtype": dtype_name})
        if dtype_name == "object":
            prepared[name] = series.map(lambda value: _encode_object_cell(value, allow_structured=structured_cells or identity_missing,
                                                                          preserve_missing=identity_missing))
        elif pd.api.types.is_string_dtype(series.dtype):
            prepared[name] = series.map(_encode_text_cell).astype("object")
        elif pd.api.types.is_float_dtype(series.dtype):
            numeric = pd.to_numeric(series, errors="coerce")
            finite = numeric.dropna().map(lambda value: math.isfinite(float(value)))
            if not finite.all():
                raise ArchiveError(f"Nonfinite numeric value in CSV member column {name!r}")
        elif isinstance(series.dtype, pd.CategoricalDtype):
            raise ArchiveError(f"Categorical CSV column requires explicit conversion: {name}")
    content = prepared.to_csv(index=False, lineterminator="\n", na_rep=_NULL_TOKEN).encode("utf-8")
    schema = {
        "member_schema_version": IDENTITY_MEMBER_SCHEMA_VERSION if identity_missing else STRUCTURED_MEMBER_SCHEMA_VERSION if structured_cells else MEMBER_SCHEMA_VERSION,
        "format": "csv",
        "encoding": "utf-8",
        "encoding_version": 3 if identity_missing else 2 if structured_cells else 1,
        "row_count": int(len(frame)),
        "columns": columns,
        "null_token": _NULL_TOKEN,
        "escape_token": _ESCAPE_TOKEN,
        "type_prefix": _TYPE_PREFIX,
    }
    return content, schema


def dataframe_from_csv_member(content: bytes, schema: Mapping[str, Any]) -> pd.DataFrame:
    """Restore a DataFrame produced by ``dataframe_to_csv_member`` and validate its schema."""
    required = {"format", "encoding", "encoding_version", "row_count", "columns", "null_token", "escape_token", "type_prefix"}
    allowed = required | {"member_schema_version"}
    if not isinstance(schema, Mapping) or not required.issubset(schema) or set(schema) - allowed:
        raise ArchiveError("CSV member schema is missing required keys or contains unknown keys")
    if "member_schema_version" in schema and schema["member_schema_version"] not in SUPPORTED_MEMBER_SCHEMA_VERSIONS:
        raise ArchiveError("Unsupported CSV member schema version")
    encoding_version = schema["encoding_version"]
    member_schema_version = schema.get("member_schema_version", MEMBER_SCHEMA_VERSION)
    expected_encoding = 3 if member_schema_version == IDENTITY_MEMBER_SCHEMA_VERSION else 2 if member_schema_version == STRUCTURED_MEMBER_SCHEMA_VERSION else 1
    if (schema["format"] != "csv" or schema["encoding"] != "utf-8"
            or isinstance(encoding_version, bool) or encoding_version != expected_encoding):
        raise ArchiveError("Unsupported CSV member schema")
    if (schema["null_token"], schema["escape_token"], schema["type_prefix"]) != (_NULL_TOKEN, _ESCAPE_TOKEN, _TYPE_PREFIX):
        raise ArchiveError("CSV member uses an unsupported value-encoding schema")
    try:
        frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, na_filter=False)
    except (UnicodeError, ValueError, pd.errors.ParserError) as exc:
        raise ArchiveError(f"Could not parse archived CSV member: {exc}") from exc
    descriptors = schema["columns"]
    if not isinstance(descriptors, list) or any(
        not isinstance(item, dict) or set(item) != {"name", "dtype"}
        or not isinstance(item["name"], str) or not isinstance(item["dtype"], str)
        for item in descriptors
    ):
        raise ArchiveError("CSV member schema columns are invalid")
    names = [item["name"] for item in descriptors]
    if frame.columns.tolist() != names or len(names) != len(set(names)):
        raise ArchiveError("CSV member columns do not match their declared schema")
    if isinstance(schema["row_count"], bool) or not isinstance(schema["row_count"], int) or len(frame) != schema["row_count"]:
        raise ArchiveError("CSV member row count does not match its schema")
    restored = pd.DataFrame(index=frame.index)
    for descriptor in descriptors:
        name, dtype = descriptor["name"], descriptor["dtype"]
        values = frame[name].tolist()
        if dtype == "object":
            decoded = [_decode_object_cell(value, allow_structured=encoding_version in {2, 3}) for value in values]
            restored[name] = pd.Series(decoded, index=frame.index, dtype="object")
        elif dtype.startswith("string"):
            decoded = [_decode_text_cell(value) for value in values]
            restored[name] = pd.Series(pd.array(decoded, dtype=dtype), index=frame.index)
        elif dtype in {"boolean", "bool"}:
            decoded = [_decode_bool(value) for value in values]
            if dtype == "bool" and any(pd.isna(value) for value in decoded):
                raise ArchiveError(f"Non-nullable boolean column contains missing values: {name}")
            restored[name] = pd.Series(decoded, index=frame.index, dtype=dtype)
        elif dtype.startswith(("Int", "UInt", "int", "uint", "Float", "float")):
            try:
                # Parse the saved decimal directly: pandas' fast numeric parser
                # can change float64 by one ULP, or route nullable integers
                # through float64 and lose values above 2**53.
                declared = pd.api.types.pandas_dtype(dtype)
                native = np.dtype(getattr(declared, "numpy_dtype", declared))
                if native.kind not in "iuf" or native.itemsize > 8:
                    raise ValueError("unsupported numeric dtype")
                decoded = []
                integer_bounds = np.iinfo(native) if native.kind in "iu" else None
                for value in values:
                    if value == _NULL_TOKEN:
                        decoded.append(np.nan if isinstance(declared, np.dtype) and native.kind == "f" else pd.NA)
                        continue
                    if integer_bounds is not None:
                        if re.fullmatch(r"[+-]?[0-9]+", value) is None:
                            raise ValueError("invalid integer text")
                        number = int(value)
                        if not integer_bounds.min <= number <= integer_bounds.max:
                            raise ValueError("integer outside declared dtype range")
                    else:
                        if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value) is None:
                            raise ValueError("invalid finite floating-point text")
                        number = float(value)
                        if not math.isfinite(number):
                            raise ValueError("nonfinite floating-point value")
                        if number == 0.0 and any(digit in "123456789" for digit in re.split("[eE]", value)[0]):
                            raise ValueError("floating-point value underflows declared dtype")
                        with np.errstate(over="raise", invalid="raise"):
                            narrowed = native.type(number)
                        if not math.isfinite(float(narrowed)) or (number != 0.0 and narrowed == 0.0):
                            raise ValueError("floating-point value outside declared dtype range")
                        number = narrowed
                    decoded.append(number)
                restored[name] = pd.Series(decoded, index=frame.index, dtype=dtype)
            except (TypeError, ValueError, OverflowError, FloatingPointError) as exc:
                raise ArchiveError(f"Could not restore numeric dtype {dtype!r} for {name!r}") from exc
        elif dtype.startswith("datetime64"):
            decoded = [_decode_nullable(value) for value in values]
            parsed = pd.to_datetime(pd.Series(decoded, index=frame.index), errors="raise", utc=("," in dtype))
            try:
                restored[name] = parsed.astype(dtype)
            except (TypeError, ValueError) as exc:
                raise ArchiveError(f"Could not restore datetime dtype {dtype!r} for {name!r}") from exc
        else:
            raise ArchiveError(f"Unsupported archived DataFrame dtype {dtype!r} for {name!r}")
    return restored


def _encode_nested_cell(value: Any) -> Any:
    """Encode nested object cells with explicit JSON tags; no repr or executable format."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return None
    if value is pd.NA:
        return {"kind": "pd.NA"}
    if value is pd.NaT:
        return {"kind": "pd.NaT"}
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        if math.isnan(number):
            return {"kind": "nan"}
        if not math.isfinite(number):
            raise ArchiveError("Infinite values cannot be serialized inside structured CSV data")
        return number
    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return {"kind": "tuple", "items": [_encode_nested_cell(item) for item in value]}
    if isinstance(value, list):
        return {"kind": "list", "items": [_encode_nested_cell(item) for item in value]}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ArchiveError("Structured CSV mapping keys must be strings")
        return {"kind": "dict", "items": [[key, _encode_nested_cell(item)] for key, item in value.items()]}
    raise ArchiveError(f"Unsupported nested CSV object value: {type(value).__name__}")


def _decode_nested_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ArchiveError("Nonfinite structured value in CSV member")
        return value
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ArchiveError("Invalid structured JSON value in CSV member")
    kind = value["kind"]
    if kind == "pd.NA" and set(value) == {"kind"}:
        return pd.NA
    if kind == "pd.NaT" and set(value) == {"kind"}:
        return pd.NaT
    if kind == "nan" and set(value) == {"kind"}:
        return float("nan")
    if kind in {"datetime", "date"} and set(value) == {"kind", "value"} and isinstance(value["value"], str):
        return datetime.fromisoformat(value["value"]) if kind == "datetime" else date.fromisoformat(value["value"])
    if kind in {"tuple", "list"} and set(value) == {"kind", "items"} and isinstance(value["items"], list):
        items = [_decode_nested_cell(item) for item in value["items"]]
        return tuple(items) if kind == "tuple" else items
    if kind == "dict" and set(value) == {"kind", "items"} and isinstance(value["items"], list):
        result: dict[str, Any] = {}
        for entry in value["items"]:
            if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[0], str) or entry[0] in result:
                raise ArchiveError("Invalid structured mapping in CSV member")
            result[entry[0]] = _decode_nested_cell(entry[1])
        return result
    raise ArchiveError("Unsupported structured JSON value in CSV member")


def _encode_object_cell(value: Any, *, allow_structured: bool = False, preserve_missing: bool = False) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if preserve_missing and (value is None or value is pd.NaT or isinstance(value, float) and math.isnan(value)):
        payload = json.dumps(_encode_nested_cell(value), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        return f"{_TYPE_PREFIX}json:{payload}"
    if value is None or value is pd.NA or value is pd.NaT:
        return pd.NA
    if isinstance(value, float) and math.isnan(value):
        return pd.NA
    if isinstance(value, bool):
        return f"{_TYPE_PREFIX}bool:{str(value).lower()}"
    if isinstance(value, numbers.Integral):
        return f"{_TYPE_PREFIX}int:{int(value)}"
    if isinstance(value, numbers.Real):
        if not math.isfinite(float(value)):
            raise ArchiveError("Infinite values cannot be serialized as CSV data")
        return f"{_TYPE_PREFIX}float:{float(value)!r}"
    if isinstance(value, datetime):
        return f"{_TYPE_PREFIX}datetime:{value.isoformat()}"
    if isinstance(value, date):
        return f"{_TYPE_PREFIX}date:{value.isoformat()}"
    if isinstance(value, str):
        return _encode_text_cell(value)
    if isinstance(value, (tuple, list, dict)):
        if not allow_structured:
            raise ArchiveError(f"Unsupported object value in CSV member: {type(value).__name__}")
        payload = json.dumps(_encode_nested_cell(value), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        return f"{_TYPE_PREFIX}json:{payload}"
    raise ArchiveError(f"Unsupported object value in CSV member: {type(value).__name__}")


def _encode_text_cell(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return pd.NA
    if not isinstance(value, str):
        value = str(value)
    if value.startswith((_NULL_TOKEN, _ESCAPE_TOKEN, _TYPE_PREFIX)) or value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return _ESCAPE_TOKEN + value
    return value


def _decode_nullable(value: str) -> Any:
    return pd.NA if value == _NULL_TOKEN else value


def _decode_bool(value: str) -> Any:
    if value == _NULL_TOKEN:
        return pd.NA
    if value.casefold() in {"true", "1"}:
        return True
    if value.casefold() in {"false", "0"}:
        return False
    raise ArchiveError(f"Invalid boolean value in CSV member: {value!r}")


def _decode_text_cell(value: str) -> Any:
    if value == _NULL_TOKEN:
        return pd.NA
    if value.startswith(_ESCAPE_TOKEN):
        return value[len(_ESCAPE_TOKEN):]
    return value


def _decode_object_cell(value: str, *, allow_structured: bool = False) -> Any:
    if value == _NULL_TOKEN:
        return pd.NA
    if value.startswith(_ESCAPE_TOKEN):
        return value[len(_ESCAPE_TOKEN):]
    if not value.startswith(_TYPE_PREFIX):
        return value
    tag, separator, payload = value[len(_TYPE_PREFIX):].partition(":")
    if not separator:
        raise ArchiveError("Malformed typed object value in CSV member")
    if tag == "bool" and payload in {"true", "false"}:
        return payload == "true"
    if tag == "int":
        return int(payload)
    if tag == "float":
        parsed = float(payload)
        if not math.isfinite(parsed):
            raise ArchiveError("Nonfinite typed value in CSV member")
        return parsed
    if tag == "datetime":
        return datetime.fromisoformat(payload)
    if tag == "date":
        return date.fromisoformat(payload)
    if tag == "json" and allow_structured:
        try:
            return _decode_nested_cell(json.loads(payload))
        except (TypeError, ValueError, KeyError) as exc:
            raise ArchiveError("Invalid structured object value in CSV member") from exc
    raise ArchiveError("Unsupported typed object value in CSV member")


def _validate_json_data(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ArchiveError(f"{label} contains NaN or infinity")
    if isinstance(value, list) or isinstance(value, tuple):
        for item in value:
            _validate_json_data(item, label)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ArchiveError(f"{label} keys must be strings")
            _validate_json_data(item, label)
        return
    raise ArchiveError(f"{label} contains unsupported value {type(value).__name__}")


def _validate_member_schema(schema: Mapping[str, Any], contract_version: str, *, name: str) -> None:
    """Validate a member's schema envelope while keeping pre-versioned 1.0/1.1 archives readable."""
    if not isinstance(schema, Mapping):
        raise ArchiveError(f"Manifest schema must be an object for {name}")
    version = schema.get("member_schema_version")
    if version is None:
        if contract_version not in {"1.0.0", "1.1.0"}:
            raise ArchiveError(f"Member schema version is missing for {name}")
    elif not isinstance(version, str) or version not in SUPPORTED_MEMBER_SCHEMA_VERSIONS:
        raise ArchiveError(f"Unsupported member schema version for {name}")
    if schema.get("format") == "csv":
        encoding_version = schema.get("encoding_version")
        expected_encoding = 3 if version == IDENTITY_MEMBER_SCHEMA_VERSION else 2 if version == STRUCTURED_MEMBER_SCHEMA_VERSION else 1
        if isinstance(encoding_version, bool) or encoding_version != expected_encoding:
            raise ArchiveError(f"Unsupported CSV member encoding version for {name}")
