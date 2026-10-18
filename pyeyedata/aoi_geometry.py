"""Exact, bounded geometry handling for static areas of interest.

The public helpers in this module are shared by file normalization and typed
analysis.  Coordinates are the exact real values represented by finite
binary64 numbers; no coordinate tolerance, snapping, or geometry repair is
used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable


AOI_GEOMETRY_VERSION = "static-aoi-geometry-2.0.0"
AOI_DOMAIN_SCHEMA_VERSION = "2.0.0"
AOI_BOUNDARY_POLICY = "closed"


@dataclass(frozen=True)
class GeometryLimits:
    """Versioned capacity bounds; these values are not scientific tolerances."""

    max_json_utf8_bytes_per_polygon: int = 262_144
    max_polygon_vertices: int = 1_024
    max_aoi_rows: int = 1_024
    max_total_vertices_per_table: int = 16_384
    max_topology_edge_pair_tests_per_table: int = 2_000_000
    max_membership_edge_tests_per_scope: int = 5_000_000
    max_exact_predicate_evaluations_per_task: int = 200_000


DEFAULT_LIMITS = GeometryLimits()


@dataclass(frozen=True)
class GeometrySource:
    source_file: str | None = None
    source_name: str | None = None
    source_sha256: str | None = None
    sheet: str | None = None
    source_row: Any = None
    source_columns: Mapping[str, str] = field(default_factory=dict)
    origin: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_name": self.source_name,
            "sha256": self.source_sha256,
            "sheet": self.sheet,
            "source_row": _json_safe_value(self.source_row),
            "source_columns": dict(self.source_columns),
            "origin": self.origin,
        }


@dataclass(frozen=True)
class AOIGeometryIssue:
    code: str
    message: str
    source: Mapping[str, Any] = field(default_factory=dict)
    canonical_field: str | None = None
    source_column: str | None = None
    aoi_id: Any = None
    vertex_index: int | None = None
    edge_index: tuple[int, int] | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        source = dict(self.source)
        return {
            "code": self.code,
            "message": self.message,
            "source_file": source.get("source_file"),
            "sheet": source.get("sheet"),
            "source_row": source.get("source_row"),
            "canonical_field": self.canonical_field,
            "source_column": self.source_column,
            "aoi_id": _json_safe_value(self.aoi_id),
            "vertex_index": self.vertex_index,
            "edge_index": list(self.edge_index) if self.edge_index is not None else None,
            "source": source,
            "details": _json_safe_value(dict(self.details)),
        }


class AOIGeometryError(ValueError):
    """A single fail-closed geometry, membership, or resource-limit issue."""

    def __init__(self, issue: AOIGeometryIssue):
        super().__init__(issue.message)
        self.issue = issue
        self.code = issue.code


class AOITableGeometryError(AOIGeometryError):
    """A malformed AOI table; no partially accepted table is returned."""

    def __init__(self, issues: Sequence[AOIGeometryIssue]):
        if not issues:
            raise ValueError("AOITableGeometryError requires at least one issue")
        self.issues = tuple(issues)
        super().__init__(issues[0])


@dataclass(frozen=True)
class CanonicalGeometry:
    shape: str
    bounds: tuple[float, float, float, float] | None
    vertices: tuple[tuple[float, float], ...]
    signed_area_sign: int | None
    input_closure: str
    geometry_version: str = AOI_GEOMETRY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "vertices": [[x, y] for x, y in self.vertices],
            "signed_area_sign": self.signed_area_sign,
            "input_closure": self.input_closure,
            "geometry_version": self.geometry_version,
        }


@dataclass(frozen=True)
class PreparedStaticAOIs:
    rows: tuple[Mapping[str, Any], ...]
    geometries: tuple[CanonicalGeometry, ...]
    audit: Mapping[str, Any]


class AOIGeometryBudget:
    """Task-local deterministic operation counters with cooperative cancel checks."""

    def __init__(
        self,
        limits: GeometryLimits = DEFAULT_LIMITS,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.limits = limits
        self.cancel_check = cancel_check
        self.exact_predicates = 0
        self.topology_edge_pair_tests = 0
        self.membership_edge_tests = 0
        self.membership_edge_tests_by_scope: dict[str, int] = {}
        self._active_membership_scope = "unscoped"
        self._work_units = 0

    def checkpoint(self, source: Mapping[str, Any] | None = None) -> None:
        self._work_units += 1
        if self._work_units == 1 or self._work_units % 64 == 0:
            if self.cancel_check is not None and self.cancel_check():
                raise _resource_error(
                    "aoi_geometry_cancelled",
                    "AOI geometry work was cancelled at a bounded checkpoint",
                    source=source,
                    limit_name="cancellation_checkpoint",
                    limit=64,
                    observed=self._work_units,
                )

    def reserve_topology_pairs(
        self, count: int, source: Mapping[str, Any] | None = None,
    ) -> None:
        # The frozen topology bound is per input table. The caller preflights
        # that table's complete pair count before reserving work. This counter
        # remains task-cumulative for audit, but must not apply a second,
        # task-wide cap when the same table is prepared by individual and
        # group analyses in one run.
        self.topology_edge_pair_tests += count

    def consume_membership_edge(self, source: Mapping[str, Any] | None = None) -> None:
        scope = self._active_membership_scope
        scope_count = self.membership_edge_tests_by_scope.get(scope, 0) + 1
        self.membership_edge_tests_by_scope[scope] = scope_count
        self.membership_edge_tests += 1
        if scope_count > self.limits.max_membership_edge_tests_per_scope:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "AOI membership edge-test budget exceeded",
                source=source,
                limit_name="max_membership_edge_tests_per_scope",
                limit=self.limits.max_membership_edge_tests_per_scope,
                observed=scope_count,
            )
        self.checkpoint(source)

    def begin_membership_scope(self, scope_id: str | None = None) -> None:
        """Select an explicit cumulative membership scope without resetting it."""
        if scope_id is not None and (not isinstance(scope_id, str) or not scope_id.strip()):
            raise ValueError("membership scope ID must be a nonempty string")
        self._active_membership_scope = scope_id or "unscoped"

    def preflight_membership_scope(
        self,
        edge_upper_bound: int,
        exact_upper_bound: int,
        source: Mapping[str, Any] | None = None,
        *,
        scope_id: str | None = None,
    ) -> None:
        scope = scope_id or self._active_membership_scope
        if scope_id is not None:
            self.begin_membership_scope(scope_id)
        edge_observed = self.membership_edge_tests_by_scope.get(scope, 0) + edge_upper_bound
        if edge_observed > self.limits.max_membership_edge_tests_per_scope:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "AOI membership scope exceeds its cumulative edge-test capacity before labeling",
                source=source,
                limit_name="max_membership_edge_tests_per_scope",
                limit=self.limits.max_membership_edge_tests_per_scope,
                observed=edge_observed,
            )
        exact_observed = self.exact_predicates + exact_upper_bound
        if exact_observed > self.limits.max_exact_predicate_evaluations_per_task:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "AOI membership scope exceeds the task exact-predicate budget before labeling",
                source=source,
                limit_name="max_exact_predicate_evaluations_per_task",
                limit=self.limits.max_exact_predicate_evaluations_per_task,
                observed=exact_observed,
            )

    def consume_exact(self, source: Mapping[str, Any] | None = None) -> None:
        self.exact_predicates += 1
        if self.exact_predicates > self.limits.max_exact_predicate_evaluations_per_task:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "AOI exact-predicate budget exceeded",
                source=source,
                limit_name="max_exact_predicate_evaluations_per_task",
                limit=self.limits.max_exact_predicate_evaluations_per_task,
                observed=self.exact_predicates,
            )
        self.checkpoint(source)


@dataclass(frozen=True)
class _GeometryDraft:
    geometry: CanonicalGeometry
    source: Mapping[str, Any]


def validate_geometry(
    shape: str,
    vertices: Any = None,
    bounds: Any = None,
    *,
    source: GeometrySource | Mapping[str, Any] | None = None,
    limits: GeometryLimits = DEFAULT_LIMITS,
    budget: AOIGeometryBudget | None = None,
    allow_legacy_shape_aliases: bool = False,
) -> CanonicalGeometry:
    """Decode and validate one rectangle or simple polygon.

    ``vertices`` may be a strict finite JSON point array or a typed numeric
    ``Nx2`` list/tuple/NumPy array.  The returned vertices are detached tuples
    of canonical binary64 values with one optional terminal closure removed.
    """
    task_budget = budget or AOIGeometryBudget(limits)
    src = _source_dict(source)
    draft = _decode_geometry(
        shape, vertices, bounds, src, limits,
        allow_legacy_shape_aliases=allow_legacy_shape_aliases,
    )
    pairs = _topology_pair_count(draft.geometry)
    task_budget.reserve_topology_pairs(pairs, src)
    if draft.geometry.shape == "polygon":
        _validate_polygon_topology(draft.geometry.vertices, task_budget, src)
        area_sign = _signed_area_sign(draft.geometry.vertices, task_budget, src)
        if area_sign == 0:
            raise _geometry_error(
                "aoi_geometry_zero_area",
                "Polygon has exact zero signed area",
                source=src,
                canonical_field="vertices",
            )
        return CanonicalGeometry(
            draft.geometry.shape, None, draft.geometry.vertices, area_sign,
            draft.geometry.input_closure,
        )
    return draft.geometry


def prepare_static_aois(
    rows: Iterable[Mapping[str, Any]],
    *,
    source: GeometrySource | Mapping[str, Any] | None = None,
    missing_name_policy: str = "require_source",
    limits: GeometryLimits = DEFAULT_LIMITS,
    budget: AOIGeometryBudget | None = None,
    cancel_check: Callable[[], bool] | None = None,
    allow_legacy_shape_aliases: bool = False,
) -> PreparedStaticAOIs:
    """Prepare a whole AOI table or raise structured issues; never skip bad rows.

    The operation pre-counts rows, vertices, and all polygon edge-pairs before
    exact topology checks.  ``aoi_name`` remains source metadata and is never
    synthesized from ``aoi_id``.
    """
    if missing_name_policy not in {"require_source", "allow_legacy_missing"}:
        raise _geometry_error(
            "aoi_name_policy_invalid",
            "missing_name_policy must be require_source or allow_legacy_missing",
            source=_source_dict(source),
            canonical_field="aoi_name",
        )
    task_budget = budget or AOIGeometryBudget(limits, cancel_check)
    if budget is not None and cancel_check is not None and budget.cancel_check is None:
        budget.cancel_check = cancel_check
    base_source = _source_dict(source)
    raw_rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        task_budget.checkpoint(base_source)
        if index >= limits.max_aoi_rows:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "AOI row limit exceeded; no rows were truncated",
                source=base_source,
                limit_name="max_aoi_rows",
                limit=limits.max_aoi_rows,
                observed=index + 1,
            )
        if not isinstance(row, Mapping):
            raw_rows.append({"__invalid_row_value__": True})
        else:
            raw_rows.append(dict(row))

    issues: list[AOIGeometryIssue] = []
    drafts: list[tuple[dict[str, Any], _GeometryDraft, dict[str, Any]]] = []
    seen_ids: set[tuple[Any, Any]] = set()
    total_vertices = 0
    total_topology_pairs = 0

    for position, row in enumerate(raw_rows):
        row_source = _row_source(row, base_source, position)
        if row.get("__invalid_row_value__"):
            issues.append(_issue(
                "aoi_row_invalid", "AOI rows must be mapping records",
                source=row_source, canonical_field=None,
            ))
            continue
        aoi_id = _plain_scalar(row.get("aoi_id"))
        stimulus_id = _plain_scalar(row.get("stimulus_id"))
        if not _valid_identity(aoi_id) or not _valid_identity(stimulus_id):
            issues.append(_issue(
                "aoi_identity_missing",
                "aoi_id and stimulus_id must be explicit nonblank scalar values",
                source=row_source, canonical_field="aoi_id" if not _valid_identity(aoi_id) else "stimulus_id",
                aoi_id=aoi_id,
            ))
        else:
            identity = (aoi_id, stimulus_id)
            try:
                duplicate = identity in seen_ids
                seen_ids.add(identity)
            except TypeError:
                duplicate = True
            if duplicate:
                issues.append(_issue(
                    "aoi_duplicate_definition",
                    "Duplicate (stimulus_id, aoi_id) AOI definitions are not merged",
                    source=row_source, canonical_field="aoi_id", aoi_id=aoi_id,
                ))

        name = row.get("aoi_name")
        name_is_source_text = isinstance(name, str) and bool(name.strip())
        if not name_is_source_text and missing_name_policy == "require_source":
            issues.append(_issue(
                "aoi_name_required",
                "A new AOI source requires a nonblank source aoi_name",
                source=row_source, canonical_field="aoi_name", aoi_id=aoi_id,
            ))
        elif name_is_source_text:
            row["aoi_name_status"] = "source"
        else:
            row["aoi_name_status"] = "legacy_missing"

        shape = row.get("shape")
        raw_shape = shape
        raw_vertices = row.get("vertices")
        raw_bounds = tuple(row.get(k) for k in ("xmin", "ymin", "xmax", "ymax"))
        try:
            draft = _decode_geometry(
                shape, raw_vertices, raw_bounds, row_source, limits,
                allow_legacy_shape_aliases=allow_legacy_shape_aliases,
            )
        except AOIGeometryError as exc:
            exc.issue = AOIGeometryIssue(
                code=exc.issue.code,
                message=exc.issue.message,
                source=row_source,
                canonical_field=exc.issue.canonical_field,
                source_column=_source_column(row_source, exc.issue.canonical_field),
                aoi_id=aoi_id,
                vertex_index=exc.issue.vertex_index,
                edge_index=exc.issue.edge_index,
                details=exc.issue.details,
            )
            issues.append(exc.issue)
            continue

        vertex_count = len(draft.geometry.vertices)
        total_vertices += vertex_count
        total_topology_pairs += _topology_pair_count(draft.geometry)
        if total_vertices > limits.max_total_vertices_per_table:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "AOI total-vertex limit exceeded; no rows were truncated",
                source=row_source,
                limit_name="max_total_vertices_per_table",
                limit=limits.max_total_vertices_per_table,
                observed=total_vertices,
            )
        row["aoi_id"] = aoi_id
        row["stimulus_id"] = stimulus_id
        if name_is_source_text:
            row["aoi_name"] = name
            row["aoi_name_status"] = "source"
        row["shape"] = draft.geometry.shape
        row["vertices"] = [[x, y] for x, y in draft.geometry.vertices] if draft.geometry.shape == "polygon" else None
        if draft.geometry.bounds is not None:
            row.update(dict(zip(("xmin", "ymin", "xmax", "ymax"), draft.geometry.bounds)))
        row["aoi_geometry_version"] = AOI_GEOMETRY_VERSION
        row["aoi_domain_schema_version"] = AOI_DOMAIN_SCHEMA_VERSION
        row["aoi_boundary_policy"] = AOI_BOUNDARY_POLICY
        row["aoi_input_closure"] = draft.geometry.input_closure
        raw_geometry = row.get("aoi_geometry_raw")
        if raw_geometry is None:
            raw_geometry = _raw_geometry(raw_shape, raw_vertices, raw_bounds, draft.geometry.shape)
        row["aoi_geometry_raw"] = raw_geometry
        row["aoi_source"] = row_source
        row["_canonical_geometry"] = draft.geometry
        drafts.append((row, draft, row_source))

    if total_topology_pairs > limits.max_topology_edge_pair_tests_per_table:
        raise _resource_error(
            "aoi_geometry_resource_limit",
            "AOI topology edge-pair budget exceeded before validation",
            source=base_source,
            limit_name="max_topology_edge_pair_tests_per_table",
            limit=limits.max_topology_edge_pair_tests_per_table,
            observed=total_topology_pairs,
        )
    if issues:
        raise AOITableGeometryError(issues)

    task_budget.reserve_topology_pairs(total_topology_pairs, base_source)
    prepared_rows: list[dict[str, Any]] = []
    geometries: list[CanonicalGeometry] = []
    for row, draft, row_source in drafts:
        task_budget.checkpoint(row_source)
        geometry = draft.geometry
        if geometry.shape == "polygon":
            _validate_polygon_topology(geometry.vertices, task_budget, row_source)
            area_sign = _signed_area_sign(geometry.vertices, task_budget, row_source)
            if area_sign == 0:
                issues.append(_issue(
                    "aoi_geometry_zero_area", "Polygon has exact zero signed area",
                    source=row_source, canonical_field="vertices", aoi_id=row.get("aoi_id"),
                ))
                continue
            geometry = CanonicalGeometry(
                geometry.shape, None, geometry.vertices, area_sign,
                geometry.input_closure,
            )
        row["_canonical_geometry"] = geometry
        row["aoi_geometry_fingerprint"] = geometry_fingerprint(
            row.get("aoi_id"), row.get("stimulus_id"), geometry,
            unit=row.get("unit"), coordinate_frame=row.get("coordinate_frame"),
            policy=row.get("overlap_policy"), priority=row.get("priority"),
        )
        row["aoi_geometry_audit"] = {
            "geometry_version": AOI_GEOMETRY_VERSION,
            "domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION,
            "boundary_policy": AOI_BOUNDARY_POLICY,
            "input_closure": geometry.input_closure,
            "signed_area_sign": geometry.signed_area_sign,
            "fingerprint": row["aoi_geometry_fingerprint"],
        }
        prepared_rows.append(row)
        geometries.append(geometry)

    if issues:
        raise AOITableGeometryError(issues)
    audit = {
        "geometry_version": AOI_GEOMETRY_VERSION,
        "domain_schema_version": AOI_DOMAIN_SCHEMA_VERSION,
        "boundary_policy": AOI_BOUNDARY_POLICY,
        "limits": _limits_dict(limits),
        "counts": {
            "aoi_rows": len(prepared_rows),
            "total_vertices": total_vertices,
            "topology_edge_pair_tests": total_topology_pairs,
            "exact_predicate_evaluations": task_budget.exact_predicates,
        },
    }
    return PreparedStaticAOIs(tuple(prepared_rows), tuple(geometries), audit)


def point_in_geometry(
    point: Any,
    geometry: CanonicalGeometry,
    *,
    budget: AOIGeometryBudget | None = None,
    limits: GeometryLimits = DEFAULT_LIMITS,
    source: GeometrySource | Mapping[str, Any] | None = None,
) -> bool:
    """Return exact closed-boundary membership for one finite typed point."""
    src = _source_dict(source)
    task_budget = budget or AOIGeometryBudget(limits)
    p = _point_from_typed(point, src, field_name="point")
    if geometry.shape == "rectangle":
        assert geometry.bounds is not None
        xmin, ymin, xmax, ymax = geometry.bounds
        for _ in range(4):
            task_budget.consume_membership_edge(src)
        return xmin <= p[0] <= xmax and ymin <= p[1] <= ymax

    winding = 0
    vertices = geometry.vertices
    for i, a in enumerate(vertices):
        b = vertices[(i + 1) % len(vertices)]
        task_budget.consume_membership_edge(src)
        orient_value: int | None = None
        if (_between(a[0], b[0], p[0]) and _between(a[1], b[1], p[1])):
            orient_value = _orientation(a, b, p, task_budget, src)
            if orient_value == 0:
                return True
        if (a[1] <= p[1] < b[1]) or (b[1] <= p[1] < a[1]):
            if orient_value is None:
                orient_value = _orientation(a, b, p, task_budget, src)
            if b[1] > a[1] and orient_value > 0:
                winding += 1
            elif b[1] < a[1] and orient_value < 0:
                winding -= 1
    return winding != 0


def geometry_fingerprint(
    aoi_id: Any,
    stimulus_id: Any,
    geometry: CanonicalGeometry,
    *,
    unit: Any,
    coordinate_frame: Any,
    policy: Any,
    priority: Any,
    method_version: str = AOI_GEOMETRY_VERSION,
) -> str:
    """Hash scientific AOI identity and canonical semantics, excluding provenance/name."""
    payload = {
        "aoi_id": _json_safe_value(_plain_scalar(aoi_id)),
        "stimulus_id": _json_safe_value(_plain_scalar(stimulus_id)),
        "shape": geometry.shape,
        "bounds": list(geometry.bounds) if geometry.bounds is not None else None,
        "vertices": [[x, y] for x, y in geometry.vertices],
        "unit": _json_safe_value(_plain_scalar(unit)),
        "coordinate_frame": _json_safe_value(_plain_scalar(coordinate_frame)),
        "overlap_policy": _json_safe_value(_plain_scalar(policy)),
        "priority": _fingerprint_priority(priority),
        "method_version": method_version,
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _decode_geometry(
    shape: Any,
    vertices_value: Any,
    bounds_value: Any,
    source: Mapping[str, Any],
    limits: GeometryLimits,
    *,
    allow_legacy_shape_aliases: bool,
) -> _GeometryDraft:
    if not isinstance(shape, str):
        raise _geometry_error("aoi_geometry_shape_invalid", "AOI shape must be rectangle or polygon",
                              source=source, canonical_field="shape")
    normalized = shape.strip().lower()
    if allow_legacy_shape_aliases:
        normalized = {"rect": "rectangle", "box": "rectangle", "poly": "polygon"}.get(normalized, normalized)
    if normalized == "rectangle":
        bounds = tuple(_finite_binary64(value, source, field_name=name)
                       for value, name in zip(bounds_value, ("xmin", "ymin", "xmax", "ymax")))
        xmin, ymin, xmax, ymax = bounds
        if not (xmax > xmin and ymax > ymin):
            raise _geometry_error(
                "aoi_geometry_rectangle_extent_invalid",
                "Rectangle bounds must have strictly positive width and height",
                source=source, canonical_field="shape",
            )
        points = ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))
        return _GeometryDraft(CanonicalGeometry(
            "rectangle", bounds, points, 1, "rectangle_bounds",
        ), source)
    if normalized != "polygon":
        raise _geometry_error("aoi_geometry_shape_invalid", "AOI shape must be rectangle or polygon",
                              source=source, canonical_field="shape")

    raw_points = _decode_vertices_value(vertices_value, source, limits)
    point_rows = _points_array(raw_points, source, from_json=isinstance(vertices_value, str))
    input_closure = "implicit"
    if len(point_rows) >= 2 and point_rows[-1] == point_rows[0]:
        point_rows = point_rows[:-1]
        input_closure = "explicit_single_terminal_vertex_removed"
    if len(point_rows) < 3:
        raise _geometry_error(
            "aoi_geometry_too_few_vertices",
            "Polygon requires at least three distinct vertices after optional closure removal",
            source=source, canonical_field="vertices",
        )
    if len(point_rows) > limits.max_polygon_vertices:
        raise _resource_error(
            "aoi_geometry_resource_limit",
            "Polygon vertex limit exceeded; vertices were not truncated",
            source=source,
            limit_name="max_polygon_vertices",
            limit=limits.max_polygon_vertices,
            observed=len(point_rows),
        )
    seen: dict[tuple[float, float], int] = {}
    for index, point in enumerate(point_rows):
        previous = seen.get(point)
        if previous is not None:
            raise _geometry_error(
                "aoi_geometry_repeated_vertex",
                "Repeated polygon vertices are invalid except one terminal closure point",
                source=source, canonical_field="vertices", vertex_index=index,
                details={"first_vertex_index": previous},
            )
        seen[point] = index
    return _GeometryDraft(CanonicalGeometry(
        "polygon", None, tuple(point_rows), None, input_closure,
    ), source)


def _decode_vertices_value(value: Any, source: Mapping[str, Any], limits: GeometryLimits) -> Any:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _geometry_error("aoi_geometry_json_invalid", "Polygon JSON is not valid UTF-8 text",
                                  source=source, canonical_field="vertices") from exc
        if len(encoded) > limits.max_json_utf8_bytes_per_polygon:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "Polygon JSON byte limit exceeded; source text was not truncated",
                source=source,
                limit_name="max_json_utf8_bytes_per_polygon",
                limit=limits.max_json_utf8_bytes_per_polygon,
                observed=len(encoded),
            )
        try:
            return json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise _geometry_error(
                "aoi_geometry_json_invalid",
                "vertices must be a safe JSON array of finite numeric coordinate pairs",
                source=source, canonical_field="vertices",
                details={"parse_error": str(exc)[:240]},
            ) from exc
    if _is_numpy_array(value):
        if getattr(value, "ndim", None) != 2:
            raise _geometry_error("aoi_geometry_vertices_shape_invalid",
                                  "Typed polygon vertices must be a two-dimensional Nx2 array",
                                  source=source, canonical_field="vertices")
        if len(value) > limits.max_polygon_vertices + 1:
            raise _resource_error(
                "aoi_geometry_resource_limit",
                "Polygon vertex limit exceeded; vertices were not truncated",
                source=source,
                limit_name="max_polygon_vertices",
                limit=limits.max_polygon_vertices,
                observed=len(value) - 1,
            )
        return value.tolist()
    if isinstance(value, (list, tuple)) and len(value) > limits.max_polygon_vertices + 1:
        raise _resource_error(
            "aoi_geometry_resource_limit",
            "Polygon vertex limit exceeded; vertices were not truncated",
            source=source,
            limit_name="max_polygon_vertices",
            limit=limits.max_polygon_vertices,
            observed=len(value) - 1,
        )
    return value


def _points_array(value: Any, source: Mapping[str, Any], *, from_json: bool) -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)):
        raise _geometry_error("aoi_geometry_vertices_shape_invalid",
                              "Polygon vertices must be an Nx2 array",
                              source=source, canonical_field="vertices")
    points: list[tuple[float, float]] = []
    for index, pair in enumerate(value):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise _geometry_error("aoi_geometry_vertices_shape_invalid",
                                  "Every polygon vertex must contain exactly two coordinates",
                                  source=source, canonical_field="vertices", vertex_index=index)
        x = _finite_binary64(pair[0], source, field_name="vertices", vertex_index=index)
        y = _finite_binary64(pair[1], source, field_name="vertices", vertex_index=index)
        points.append((x, y))
    return points


def _point_from_typed(value: Any, source: Mapping[str, Any], *, field_name: str) -> tuple[float, float]:
    if _is_numpy_array(value):
        if getattr(value, "ndim", None) != 1 or len(value) != 2:
            raise _geometry_error("aoi_geometry_point_invalid", "Membership point must have two coordinates",
                                  source=source, canonical_field=field_name)
        value = value.tolist()
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _geometry_error("aoi_geometry_point_invalid", "Membership point must have two coordinates",
                              source=source, canonical_field=field_name)
    return (_finite_binary64(value[0], source, field_name=field_name),
            _finite_binary64(value[1], source, field_name=field_name))


def _finite_binary64(
    value: Any,
    source: Mapping[str, Any],
    *,
    field_name: str,
    vertex_index: int | None = None,
) -> float:
    value = _plain_scalar(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _geometry_error(
            "aoi_geometry_coordinate_invalid",
            "Coordinates must be numeric binary64 values; booleans and text are not coordinates",
            source=source, canonical_field=field_name, vertex_index=vertex_index,
        )
    try:
        converted = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise _geometry_error("aoi_geometry_coordinate_nonfinite",
                              "Coordinate cannot be represented as finite binary64",
                              source=source, canonical_field=field_name,
                              vertex_index=vertex_index) from exc
    if not math.isfinite(converted):
        raise _geometry_error("aoi_geometry_coordinate_nonfinite",
                              "Coordinates must be finite binary64 values",
                              source=source, canonical_field=field_name,
                              vertex_index=vertex_index)
    return 0.0 if converted == 0.0 else converted


def _validate_polygon_topology(
    vertices: tuple[tuple[float, float], ...],
    budget: AOIGeometryBudget,
    source: Mapping[str, Any],
) -> None:
    count = len(vertices)
    for i in range(count):
        previous = vertices[(i - 1) % count]
        shared = vertices[i]
        following = vertices[(i + 1) % count]
        if _orientation(previous, shared, following, budget, source) == 0:
            budget.consume_exact(source)
            ax, ay = Fraction(previous[0]) - Fraction(shared[0]), Fraction(previous[1]) - Fraction(shared[1])
            bx, by = Fraction(following[0]) - Fraction(shared[0]), Fraction(following[1]) - Fraction(shared[1])
            if ax * bx + ay * by > 0:
                raise _geometry_error(
                    "aoi_geometry_adjacent_backtrack",
                    "Adjacent collinear edges overlap by backtracking",
                    source=source, canonical_field="vertices",
                    vertex_index=i,
                    edge_index=((i - 1) % count, i),
                )

    for i in range(count):
        a, b = vertices[i], vertices[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            budget.checkpoint(source)
            c, d = vertices[j], vertices[(j + 1) % count]
            if _segments_intersect(a, b, c, d, budget, source):
                raise _geometry_error(
                    "aoi_geometry_self_intersection",
                    "Nonadjacent polygon edges intersect, touch, or overlap",
                    source=source, canonical_field="vertices",
                    edge_index=(i, j),
                )


def _segments_intersect(
    a: tuple[float, float], b: tuple[float, float],
    c: tuple[float, float], d: tuple[float, float],
    budget: AOIGeometryBudget, source: Mapping[str, Any],
) -> bool:
    if (_max(a[0], b[0]) < _min(c[0], d[0]) or _max(c[0], d[0]) < _min(a[0], b[0])
            or _max(a[1], b[1]) < _min(c[1], d[1]) or _max(c[1], d[1]) < _min(a[1], b[1])):
        return False
    o1 = _orientation(a, b, c, budget, source)
    o2 = _orientation(a, b, d, budget, source)
    o3 = _orientation(c, d, a, budget, source)
    o4 = _orientation(c, d, b, budget, source)
    if o1 == 0 and _on_segment(a, b, c):
        return True
    if o2 == 0 and _on_segment(a, b, d):
        return True
    if o3 == 0 and _on_segment(c, d, a):
        return True
    if o4 == 0 and _on_segment(c, d, b):
        return True
    return o1 * o2 < 0 and o3 * o4 < 0


def _signed_area_sign(
    vertices: tuple[tuple[float, float], ...],
    budget: AOIGeometryBudget,
    source: Mapping[str, Any],
) -> int:
    budget.consume_exact(source)
    twice_area = Fraction(0)
    for i, (x1, y1) in enumerate(vertices):
        x2, y2 = vertices[(i + 1) % len(vertices)]
        twice_area += Fraction(x1) * Fraction(y2) - Fraction(y1) * Fraction(x2)
    return (twice_area > 0) - (twice_area < 0)


def _orientation(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float],
    budget: AOIGeometryBudget, source: Mapping[str, Any],
) -> int:
    budget.consume_exact(source)
    determinant = ((Fraction(b[0]) - Fraction(a[0])) * (Fraction(c[1]) - Fraction(a[1]))
                   - (Fraction(b[1]) - Fraction(a[1])) * (Fraction(c[0]) - Fraction(a[0])))
    return (determinant > 0) - (determinant < 0)


def _on_segment(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> bool:
    return (_between(a[0], b[0], p[0]) and _between(a[1], b[1], p[1]))


def _between(a: float, b: float, value: float) -> bool:
    return min(a, b) <= value <= max(a, b)


def _topology_pair_count(geometry: CanonicalGeometry) -> int:
    if geometry.shape != "polygon":
        return 0
    count = len(geometry.vertices)
    return count * (count - 1) // 2


def _source_dict(source: GeometrySource | Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(source, GeometrySource):
        return source.as_dict()
    if isinstance(source, Mapping):
        result = dict(source)
        if "source_sha256" in result and "sha256" not in result:
            result["sha256"] = result.pop("source_sha256")
        if "source_path" in result and "source_file" not in result:
            result["source_file"] = result.pop("source_path")
        if "sheet_name" in result and "sheet" not in result:
            result["sheet"] = result.pop("sheet_name")
        return _json_safe_value(result)
    return {}


def _row_source(row: Mapping[str, Any], base_source: Mapping[str, Any], position: int) -> dict[str, Any]:
    row_source = row.get("aoi_source")
    if isinstance(row_source, Mapping):
        result = _source_dict(row_source)
    else:
        result = dict(base_source)
    if result.get("source_row") is None:
        if row.get("source_row") is not None:
            result["source_row"] = _json_safe_value(row.get("source_row"))
        elif row.get("_aoi_direct_row_label") is not None:
            result["source_row"] = _json_safe_value(row.get("_aoi_direct_row_label"))
            result.setdefault("origin", "typed")
    if not result and row.get("_aoi_direct_row_label") is not None:
        result = {"source_row": _json_safe_value(row.get("_aoi_direct_row_label")), "origin": "typed"}
    if "source_columns" not in result:
        result["source_columns"] = {}
    return result


def _source_column(source: Mapping[str, Any], canonical_field: str | None) -> str | None:
    columns = source.get("source_columns", {})
    if canonical_field and isinstance(columns, Mapping):
        value = columns.get(canonical_field)
        return value if isinstance(value, str) else None
    return None


def _issue(
    code: str, message: str, *, source: Mapping[str, Any],
    canonical_field: str | None = None, aoi_id: Any = None,
    vertex_index: int | None = None, edge_index: tuple[int, int] | None = None,
    details: Mapping[str, Any] | None = None,
) -> AOIGeometryIssue:
    return AOIGeometryIssue(
        code, message, dict(source), canonical_field,
        _source_column(source, canonical_field), aoi_id,
        vertex_index, edge_index, dict(details or {}),
    )


def _geometry_error(code: str, message: str, *, source: Mapping[str, Any],
                    canonical_field: str | None = None, aoi_id: Any = None,
                    vertex_index: int | None = None, edge_index: tuple[int, int] | None = None,
                    details: Mapping[str, Any] | None = None) -> AOIGeometryError:
    return AOIGeometryError(_issue(
        code, message, source=source, canonical_field=canonical_field,
        aoi_id=aoi_id, vertex_index=vertex_index, edge_index=edge_index,
        details=details,
    ))


def _resource_error(code: str, message: str, *, source: Mapping[str, Any] | None,
                    limit_name: str, limit: int, observed: int) -> AOIGeometryError:
    return _geometry_error(
        code, message, source=dict(source or {}), canonical_field="vertices",
        details={"limit_name": limit_name, "limit": limit, "observed": observed},
    )


def _raw_geometry(shape_raw: Any, vertices: Any, bounds: Sequence[Any], shape: str) -> Any:
    if shape == "polygon":
        return {"shape": _raw_copy(shape_raw), "vertices": _raw_copy(vertices)}
    return {"shape": _raw_copy(shape_raw), "bounds": {
        key: _raw_copy(value)
        for key, value in zip(("xmin", "ymin", "xmax", "ymax"), bounds)
    }}


def _raw_copy(value: Any) -> Any:
    value = _plain_scalar(value)
    if _is_numpy_array(value):
        return _raw_copy(value.tolist())
    if isinstance(value, Mapping):
        return {str(k): _raw_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_raw_copy(x) for x in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_safe_value(value: Any) -> Any:
    value = _plain_scalar(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    return str(value)


def _fingerprint_priority(value: Any) -> float | int | None:
    value = _plain_scalar(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    if not math.isfinite(converted):
        return None
    return 0.0 if converted == 0.0 else converted


def _plain_scalar(value: Any) -> Any:
    if type(value).__module__.split(".", 1)[0] == "numpy" and hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            return value
    return value


def _valid_identity(value: Any) -> bool:
    value = _plain_scalar(value)
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _is_numpy_array(value: Any) -> bool:
    return type(value).__module__.split(".", 1)[0] == "numpy" and hasattr(value, "ndim") and hasattr(value, "tolist")


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant {value} is not allowed")


def _limits_dict(limits: GeometryLimits) -> dict[str, int]:
    return {
        "max_json_utf8_bytes_per_polygon": limits.max_json_utf8_bytes_per_polygon,
        "max_polygon_vertices": limits.max_polygon_vertices,
        "max_aoi_rows": limits.max_aoi_rows,
        "max_total_vertices_per_table": limits.max_total_vertices_per_table,
        "max_topology_edge_pair_tests_per_table": limits.max_topology_edge_pair_tests_per_table,
        "max_membership_edge_tests_per_scope": limits.max_membership_edge_tests_per_scope,
        "max_exact_predicate_evaluations_per_task": limits.max_exact_predicate_evaluations_per_task,
    }


def _min(a: float, b: float) -> float:
    return a if a <= b else b


def _max(a: float, b: float) -> float:
    return a if a >= b else b


__all__ = [
    "AOI_BOUNDARY_POLICY", "AOI_DOMAIN_SCHEMA_VERSION", "AOI_GEOMETRY_VERSION",
    "AOIGeometryBudget", "AOIGeometryError", "AOIGeometryIssue", "AOITableGeometryError",
    "CanonicalGeometry", "DEFAULT_LIMITS", "GeometryLimits", "GeometrySource",
    "PreparedStaticAOIs", "geometry_fingerprint", "point_in_geometry",
    "prepare_static_aois", "validate_geometry",
]
