"""Table and provenance model for the static AOI editor."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from ..aoi_geometry import AOIGeometryError, prepare_static_aois, validate_geometry


AOI_EXPORT_COLUMNS = (
    "aoi_id", "stimulus_id", "shape", "aoi_name", "xmin", "ymin", "xmax", "ymax",
    "vertices", "priority", "overlap_policy", "unit", "coordinate_frame",
)
_INTERNAL_COLUMNS = {
    "_canonical_geometry", "aoi_geometry_fingerprint", "aoi_geometry_version",
    "aoi_domain_schema_version", "aoi_source", "source_row", "source_file",
}


class AOIEditorValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        message = "; ".join(str(issue.get("message", issue.get("code", "Invalid AOI"))) for issue in issues)
        super().__init__(message or "AOI table is invalid")


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _canonical_stimulus(value: Any) -> str | None:
    if _missing(value):
        return None
    text = str(value).strip()
    return text or None


def _json_scalar(value: Any) -> Any:
    if _missing(value):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_file_metadata(path: str | Path, *, stimulus_id: str | None = None) -> dict[str, Any]:
    """Read image dimensions and content hash without guessing its stimulus."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        from PIL import Image

        with Image.open(source) as image:
            width, height = image.size
            image_format = image.format
            image.verify()
    except Exception as exc:
        raise ValueError(f"Cannot read a supported static image: {exc}") from exc
    if width < 1 or height < 1:
        raise ValueError("image dimensions must be positive")
    return {
        "stimulus_id": _canonical_stimulus(stimulus_id),
        "image_path": str(source),
        "sha256": _sha256(source),
        "width": int(width),
        "height": int(height),
        "format": str(image_format or "unknown"),
        "coordinate_unit": "pixel",
        "coordinate_frame": "image_pixel",
        "pixel_origin": "top_left",
        "y_axis_direction": "down",
    }


def _row_geometry(row: Mapping[str, Any]) -> dict[str, Any]:
    shape = str(row.get("shape", "")).strip().lower()
    bounds = None
    if shape == "rectangle":
        bounds = tuple(row.get(name) for name in ("xmin", "ymin", "xmax", "ymax"))
    canonical = validate_geometry(shape, row.get("vertices"), bounds)
    item = dict(row)
    item["shape"] = canonical.shape
    item["xmin"] = canonical.bounds[0] if canonical.bounds else pd.NA
    item["ymin"] = canonical.bounds[1] if canonical.bounds else pd.NA
    item["xmax"] = canonical.bounds[2] if canonical.bounds else pd.NA
    item["ymax"] = canonical.bounds[3] if canonical.bounds else pd.NA
    item["vertices"] = json.dumps(canonical.vertices, ensure_ascii=False, separators=(",", ":")) if canonical.shape == "polygon" else pd.NA
    return item


class AOIEditorModel:
    """Keep a complete multi-stimulus table while editing one bound stimulus."""

    def __init__(self) -> None:
        self._table = pd.DataFrame(columns=AOI_EXPORT_COLUMNS)
        self._active_records: list[dict[str, Any]] = []
        self._invalid_active_records: list[dict[str, Any]] = []
        self._stimulus_id: str | None = None
        self._image_metadata: dict[str, Any] = {}
        self._manifest_by_stimulus: dict[str, dict[str, Any]] = {}

    @property
    def stimulus_id(self) -> str | None:
        return self._stimulus_id

    @property
    def bound(self) -> bool:
        return bool(self._stimulus_id)

    @property
    def image_metadata(self) -> dict[str, Any]:
        return dict(self._image_metadata)

    @property
    def image_path(self) -> str | None:
        value = self._image_metadata.get("image_path")
        return str(value) if value else None

    @property
    def active_records(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._active_records]

    @property
    def stimuli(self) -> tuple[str, ...]:
        if "stimulus_id" not in self._table:
            return ()
        values = {
            stimulus for stimulus in (_canonical_stimulus(value) for value in self._table["stimulus_id"])
            if stimulus is not None
        }
        if self._stimulus_id:
            values.add(self._stimulus_id)
        return tuple(sorted(values))

    def set_image(self, path: str | Path, *, stimulus_id: str | None = None) -> dict[str, Any]:
        explicit_id = _canonical_stimulus(stimulus_id)
        if self.bound:
            self._table = self.table()
        self._image_metadata = image_file_metadata(path, stimulus_id=explicit_id)
        if explicit_id:
            self.bind_stimulus(explicit_id)
        else:
            # An image hash that appeared in a prior manifest is not a binding
            # instruction. The user/host must explicitly confirm a stimulus.
            self._stimulus_id = None
            self._active_records = []
            self._invalid_active_records = []
        return self.image_metadata

    def set_aois(self, table: Any, *, image_metadata: Mapping[str, Any] | None = None) -> None:
        frame = table.copy(deep=True) if isinstance(table, pd.DataFrame) else pd.DataFrame(table)
        self._table = frame.drop(columns=list(_INTERNAL_COLUMNS), errors="ignore").reset_index(drop=True)
        self._active_records = []
        self._invalid_active_records = []
        manifest = dict(image_metadata or {})
        by_stimulus = manifest.get("stimuli")
        if isinstance(by_stimulus, Mapping):
            self._manifest_by_stimulus = {
                str(key): dict(value) for key, value in by_stimulus.items() if isinstance(value, Mapping)
            }
        explicit_id = _canonical_stimulus(manifest.get("stimulus_id"))
        if explicit_id:
            self._stimulus_id = explicit_id
            if self._image_metadata:
                self._image_metadata["stimulus_id"] = explicit_id
                self._manifest_by_stimulus[explicit_id] = dict(self._image_metadata)
            self._load_active_rows()
        elif self._stimulus_id:
            self._load_active_rows()

    def bind_stimulus(self, stimulus_id: Any) -> None:
        value = _canonical_stimulus(stimulus_id)
        if value is None:
            raise ValueError("a confirmed, non-empty stimulus_id is required")
        if self.bound:
            self._table = self.table()
        self._stimulus_id = value
        if self._image_metadata:
            self._image_metadata["stimulus_id"] = value
            self._manifest_by_stimulus[value] = dict(self._image_metadata)
        self._load_active_rows()

    def _load_active_rows(self) -> None:
        if not self._stimulus_id or "stimulus_id" not in self._table:
            self._active_records = []
            self._invalid_active_records = []
            return
        mask = self._table["stimulus_id"].map(_canonical_stimulus).eq(self._stimulus_id)
        rows = self._table.loc[mask].to_dict(orient="records")
        valid: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for row in rows:
            unit = (_canonical_stimulus(row.get("unit")) or "unknown").lower()
            coordinate_frame = (_canonical_stimulus(row.get("coordinate_frame")) or "unknown").lower()
            if unit not in {"pixel", "pixels", "px"} or coordinate_frame not in {"image_pixel", "image_pixels"}:
                # The scene has an identity viewport transform only. Unknown or
                # physical coordinates stay preserved but are not drawn as pixels.
                invalid.append(dict(row))
                continue
            try:
                valid.append(_row_geometry(row))
            except (AOIGeometryError, TypeError, ValueError):
                # Keep malformed source rows even if the canvas cannot draw them;
                # edits to other shapes must never silently discard source data.
                invalid.append(dict(row))
        self._active_records = valid
        self._invalid_active_records = invalid

    def set_active_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        if not self.bound:
            raise ValueError("cannot edit AOIs until an image is explicitly bound to a stimulus")
        normalized: list[dict[str, Any]] = []
        for record in records:
            row = dict(record)
            row["stimulus_id"] = self._stimulus_id
            row.setdefault("unit", self._image_metadata.get("coordinate_unit", "pixel"))
            row.setdefault("coordinate_frame", self._image_metadata.get("coordinate_frame", "image_pixel"))
            row.setdefault("overlap_policy", "unresolved")
            normalized.append(_row_geometry(row))
        self._active_records = normalized

    def table(self) -> pd.DataFrame:
        """Return all stimuli, replacing only the currently bound stimulus rows."""
        result = self._table.drop(columns=list(_INTERNAL_COLUMNS), errors="ignore").copy(deep=True)
        if self.bound:
            if "stimulus_id" not in result:
                result["stimulus_id"] = pd.Series(dtype="string")
            keep = ~result["stimulus_id"].map(_canonical_stimulus).eq(self._stimulus_id)
            result = result.loc[keep].copy()
            active_rows = [*self._active_records, *self._invalid_active_records]
            if active_rows:
                columns = list(dict.fromkeys([*result.columns, *(name for row in active_rows for name in row)]))
                rows = result.to_dict(orient="records")
                rows.extend(active_rows)
                result = pd.DataFrame.from_records(rows, columns=columns)
        return result.drop(columns=list(_INTERNAL_COLUMNS), errors="ignore").reset_index(drop=True)

    def image_metadata_for_table(self) -> dict[str, Any]:
        return {
            "stimulus_id": self._stimulus_id,
            "image_path": self._image_metadata.get("image_path"),
            "sha256": self._image_metadata.get("sha256"),
            "width": self._image_metadata.get("width"),
            "height": self._image_metadata.get("height"),
            "coordinate_unit": self._image_metadata.get("coordinate_unit", "pixel"),
            "coordinate_frame": self._image_metadata.get("coordinate_frame", "image_pixel"),
            "pixel_origin": self._image_metadata.get("pixel_origin", "top_left"),
            "y_axis_direction": self._image_metadata.get("y_axis_direction", "down"),
            "bound": self.bound,
            "stimuli": {key: dict(value) for key, value in self._manifest_by_stimulus.items()},
        }

    def validate(self) -> list[dict[str, Any]]:
        if not self.bound:
            return [{
                "code": "aoi_stimulus_binding_required",
                "message": "请明确确认当前图片对应的 stimulus_id 后再编辑或应用 AOI。",
                "canonical_field": "stimulus_id",
            }]
        if not self._image_metadata.get("sha256") or not self._image_metadata.get("width") or not self._image_metadata.get("height"):
            return [{
                "code": "aoi_image_metadata_required",
                "message": "请先载入实际静态图片，以记录其哈希和原始尺寸。",
                "canonical_field": "image_metadata",
            }]
        frame = self.table()
        if frame.empty:
            return [{"code": "aoi_table_empty", "message": "至少需要一个 AOI。", "canonical_field": None}]
        declaration_issues = []
        for row_number, row in enumerate(frame.to_dict(orient="records"), start=1):
            unit = _canonical_stimulus(row.get("unit")) or "unknown"
            coordinate_frame = _canonical_stimulus(row.get("coordinate_frame")) or "unknown"
            if unit.lower() not in {"pixel", "pixels", "px"}:
                declaration_issues.append({
                    "code": "aoi_coordinate_unit_not_image_pixel",
                    "message": f"AOI 第 {row_number} 行使用 {unit!r}；编辑器不会把其他单位缩放成图像像素。",
                    "canonical_field": "unit",
                    "source_row": row_number,
                })
            if coordinate_frame.lower() not in {"image_pixel", "image_pixels"}:
                declaration_issues.append({
                    "code": "aoi_coordinate_frame_unknown",
                    "message": f"AOI 第 {row_number} 行的坐标框架是 {coordinate_frame!r}；请先明确图像像素映射。",
                    "canonical_field": "coordinate_frame",
                    "source_row": row_number,
                })
        if declaration_issues:
            return declaration_issues
        rows = frame.to_dict(orient="records")
        try:
            prepared = prepare_static_aois(rows, missing_name_policy="require_source")
            return [] if prepared.rows else [{"code": "aoi_table_empty", "message": "至少需要一个 AOI."}]
        except AOIGeometryError as exc:
            issues = getattr(exc, "issues", None)
            if issues:
                return [issue.as_dict() if hasattr(issue, "as_dict") else dict(issue) for issue in issues]
            issue = getattr(exc, "issue", None)
            return [issue.as_dict() if issue is not None and hasattr(issue, "as_dict") else {
                "code": getattr(exc, "code", "aoi_geometry_invalid"), "message": str(exc),
            }]
        except (TypeError, ValueError) as exc:
            return [{"code": "aoi_table_invalid", "message": str(exc)}]

    def export_frame(self) -> pd.DataFrame:
        issues = self.validate()
        if issues:
            raise AOIEditorValidationError(issues)
        frame = self.table().drop(columns=list(_INTERNAL_COLUMNS), errors="ignore")
        for column in AOI_EXPORT_COLUMNS:
            if column not in frame:
                frame[column] = pd.NA
        frame = frame.loc[:, list(AOI_EXPORT_COLUMNS)].copy()
        return frame.reset_index(drop=True)

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": "pyeyedata.aoi-image-manifest/v1",
            "coordinate_declaration": {
                "unit": "pixel",
                "coordinate_frame": "image_pixel",
                "origin": "top_left",
                "y_axis_direction": "down",
                "pixel_coordinates_are_image_native": True,
            },
            "stimuli": {key: dict(value) for key, value in self._manifest_by_stimulus.items()},
        }

    def export_to(self, path: str | Path) -> tuple[Path, Path]:
        frame = self.export_frame()
        csv_path = Path(path).expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = csv_path.with_suffix(csv_path.suffix + ".manifest.json")
        frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
        manifest_path.write_text(json.dumps(self.manifest(), ensure_ascii=False, indent=2, allow_nan=False),
                                 encoding="utf-8")
        return csv_path, manifest_path

