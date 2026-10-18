"""Opt-in, bounded, transactional cache for proven complete computation artifacts.

Keys extend the project's typed JSON fingerprints into a GUI-independent domain.
Unknown values are a safe miss; paths and arbitrary repr/str are never keys.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
import hashlib
import math
import sys
from threading import RLock
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

CACHE_KEY_SCHEMA_VERSION = "pyeyedata.execution-cache-key/v2"
CACHE_AUDIT_SCHEMA_VERSION = "pyeyedata.execution-cache-audit/v1"
DEFAULT_CACHE_BYTES = 256 * 1024**2
DEFAULT_KEY_INPUT_BYTES = 16 * 1024**2


class CacheIdentityError(ValueError):
    """The input is outside the explicitly supported semantic-key domain."""


class _KeyInputBudgetExceeded(CacheIdentityError):
    def __init__(self, byte_size: int, limit: int) -> None:
        super().__init__("input_key_memory_budget")
        self.byte_size, self.limit = byte_size, limit


def _dtype_identity(dtype: Any) -> Any:
    if isinstance(dtype, pd.CategoricalDtype):
        return ["category", dtype.categories, dtype.ordered]
    if isinstance(dtype, np.dtype):
        if dtype.fields is not None or dtype.kind not in "biufcOSUmM":
            raise CacheIdentityError("Unsupported NumPy dtype")
        return ["numpy", dtype.str]
    if isinstance(dtype, (pd.StringDtype, pd.BooleanDtype, pd.DatetimeTZDtype)) or (
        type(dtype).__module__.startswith("pandas.core.arrays")
        and hasattr(dtype, "numpy_dtype")
    ):
        return ["pandas", type(dtype).__name__, str(dtype), getattr(dtype, "storage", None)]
    raise CacheIdentityError("Unsupported extension dtype")


class _SemanticHasher:
    """Typed streaming encoding: bounded buffers and cancellation every 128 nodes.

    Mapping/set ordering retains at most 4096 fixed-size digests. Larger unordered
    containers are a safe miss. Tables and ordered arrays have no row-size tree.
    Actual attrs are included as returned content, never trusted as fingerprints.
    """

    def __init__(self, cancel: Callable[[], bool] | None = None) -> None:
        self.digest = hashlib.sha256()
        self.cancel = cancel
        self.nodes = 0
        self.active: set[int] = set()

    def check(self) -> None:
        if self.cancel and self.cancel():
            raise CacheIdentityError("cancelled during semantic key")

    def token(self, tag: str) -> None:
        self.digest.update(tag.encode("ascii") + b";")

    def blob(self, value: bytes) -> None:
        self.token(str(len(value)))
        for start in range(0, len(value), 32768):
            self.check()
            self.digest.update(value[start:start + 32768])

    def child_digest(self, value: Any) -> bytes:
        child = _SemanticHasher(self.cancel)
        child.active = set(self.active)
        child.feed(value)
        return child.digest.digest()

    def feed(self, value: Any) -> None:
        self.nodes += 1
        if self.nodes % 128 == 1:
            self.check()
        if len(self.active) > 128:
            raise CacheIdentityError("semantic nesting limit")
        if value is None or value is pd.NA or value is pd.NaT:
            self.token("None" if value is None else "pd.NA" if value is pd.NA else "pd.NaT")
            return
        if type(value) is str:
            self.token("str")
            self.token(str(len(value)))
            for start in range(0, len(value), 8192):
                self.check()
                self.blob(value[start:start + 8192].encode("utf-8", "surrogatepass"))
            return
        if type(value) is bytes:
            self.token("bytes")
            self.blob(value)
            return
        if isinstance(value, np.generic):
            self.token("numpy-scalar")
            self.feed(_dtype_identity(value.dtype))
            self.blob(value.tobytes())
            return
        if isinstance(value, pd.Timestamp):
            self.token("Timestamp")
            self.feed((value.isoformat(), str(value.tz)))
            return
        if isinstance(value, pd.Timedelta):
            self.token("Timedelta")
            self.feed(value.value)
            return
        if type(value) in {bool, int, float}:
            self.token(type(value).__name__)
            self.blob((value.hex() if not math.isnan(value) else np.float64(value).tobytes().hex()).encode("ascii")
                      if type(value) is float else str(value).encode("ascii"))
            return
        if type(value) in {datetime, date, timedelta}:
            self.token(type(value).__name__)
            self.feed(value.isoformat() if hasattr(value, "isoformat") else (value.days, value.seconds, value.microseconds))
            return
        identity = id(value)
        if identity in self.active:
            raise CacheIdentityError("cyclic semantic input")
        self.active.add(identity)
        try:
            if isinstance(value, pd.DataFrame):
                self.token("DataFrame")
                self.feed(value.shape)
                self.feed(value.index)
                self.feed(value.columns)
                for dtype in value.dtypes:
                    self.feed(_dtype_identity(dtype))
                for row in value.itertuples(index=False, name=None):
                    for cell in row:
                        self.feed(cell)
                self.feed(value.attrs)
            elif isinstance(value, pd.Series):
                self.token("Series")
                self.feed((value.name, value.index, _dtype_identity(value.dtype)))
                for cell in value:
                    self.feed(cell)
                self.feed(value.attrs)
            elif isinstance(value, pd.MultiIndex):
                self.token("MultiIndex")
                self.feed((tuple(value.names), tuple(value.levels), tuple(value.codes), value.sortorder))
            elif isinstance(value, pd.Index):
                self.token(type(value).__name__)
                self.feed((value.name, len(value), _dtype_identity(value.dtype)))
                for cell in value:
                    self.feed(cell)
            elif isinstance(value, np.ndarray):
                self.token("ndarray")
                self.feed((value.shape, _dtype_identity(value.dtype)))
                if value.dtype.kind == "O":
                    for cell in value.flat:
                        self.feed(cell)
                elif value.size:
                    # nditer buffers noncontiguous arrays in bounded C-order chunks.
                    with np.nditer(value, flags=["external_loop", "buffered", "zerosize_ok"],
                                   op_flags=["readonly"], order="C", buffersize=4096) as iterator:
                        for chunk in iterator:
                            self.check()
                            self.digest.update(chunk.tobytes())
            elif is_dataclass(value) and not isinstance(value, type):
                self.token("dataclass")
                self.feed((type(value).__module__, type(value).__qualname__))
                for item in fields(value):
                    self.feed(item.name)
                    self.feed(getattr(value, item.name))
            elif isinstance(value, Mapping):
                if len(value) > 4096:
                    raise CacheIdentityError("unordered mapping exceeds key budget")
                self.token("mapping")
                self.token(str(len(value)))
                ordered = sorted((self.child_digest(key), key) for key in value)
                for digest, key in ordered:
                    self.digest.update(digest)
                    self.feed(value[key])
            elif type(value) in {tuple, list}:
                self.token(type(value).__name__)
                self.token(str(len(value)))
                for cell in value:
                    self.feed(cell)
            elif type(value) in {set, frozenset}:
                if len(value) > 4096:
                    raise CacheIdentityError("unordered set exceeds key budget")
                self.token(type(value).__name__)
                self.token(str(len(value)))
                for digest in sorted(self.child_digest(cell) for cell in value):
                    self.digest.update(digest)
            else:
                raise CacheIdentityError(f"Unsupported semantic input type: {type(value).__name__}")
            self.token("end")
        finally:
            self.active.remove(identity)


def semantic_key(value: Any, cancel: Callable[[], bool] | None = None) -> str:
    hasher = _SemanticHasher(cancel)
    hasher.feed(CACHE_KEY_SCHEMA_VERSION)
    hasher.feed(value)
    hasher.check()
    return hasher.digest.hexdigest()


def _clone(value: Any, cancel: Callable[[], bool] | None = None, _memo: dict[int, Any] | None = None) -> Any:
    if cancel and cancel():
        raise CacheIdentityError("cancelled while isolating artifact")
    memo = _memo if _memo is not None else {}
    if id(value) in memo:
        return memo[id(value)]
    clone = lambda item: _clone(item, cancel, memo)
    if isinstance(value, pd.DataFrame):
        out = value.copy(deep=True)
        out.index = clone(value.index)
        out.columns = clone(value.columns)
        for position, dtype in enumerate(value.dtypes):
            if dtype == object:
                out.isetitem(position, pd.Series([clone(cell) for cell in value.iloc[:, position]],
                                               index=value.index, dtype=object))
        # Preserved for callers, never used as semantic equivalence authority.
        out.attrs = clone(value.attrs)
    elif isinstance(value, pd.Series):
        out = value.copy(deep=True)
        if value.dtype == object:
            out = pd.Series([clone(cell) for cell in value], index=value.index, name=clone(value.name), dtype=object)
        out.index = clone(value.index)
        out.attrs = clone(value.attrs)
    elif isinstance(value, pd.MultiIndex):
        out = pd.MultiIndex(levels=[clone(level) for level in value.levels],
                             codes=[code.copy() for code in value.codes], names=list(value.names),
                             sortorder=value.sortorder)
    elif isinstance(value, pd.Index):
        if value.dtype == object:
            out = pd.Index([clone(cell) for cell in value],
                            dtype=object, name=value.name, tupleize_cols=False)
        else:
            out = value.copy(deep=True)
    elif isinstance(value, np.ndarray):
        if value.dtype == object:
            out = np.empty(value.shape, dtype=object)
            for position, item in enumerate(value.flat):
                out.flat[position] = clone(item)
        else:
            out = value.copy()
    elif is_dataclass(value) and not isinstance(value, type):
        out = type(value)(**{item.name: clone(getattr(value, item.name)) for item in fields(value)})
    elif isinstance(value, Mapping):
        out = {clone(key): clone(item) for key, item in value.items()}
    elif type(value) in {tuple, list, set, frozenset}:
        out = type(value)(clone(item) for item in value)
    else:
        semantic_key(value, cancel)  # Unknown/custom mutable objects are never retained.
        out = value
    memo[id(value)] = out
    return out


def _memory_bytes(value: Any, seen: set[int] | None = None, active: set[int] | None = None,
                  cancel: Callable[[], bool] | None = None, *, limit_bytes: int | None = None,
                  _counter: list[int] | None = None) -> int:
    if cancel and cancel():
        raise CacheIdentityError("cancelled while measuring artifact")
    visited = seen if seen is not None else set()
    parents = active if active is not None else set()
    counter = _counter if _counter is not None else [0]
    initial = counter[0]
    def charge(size: int) -> None:
        counter[0] += size
        if limit_bytes is not None and counter[0] > limit_bytes:
            raise _KeyInputBudgetExceeded(counter[0], limit_bytes)
    if id(value) in parents:
        raise CacheIdentityError("cyclic retained artifact")
    if len(parents) > 128:
        raise CacheIdentityError("retained artifact nesting limit")
    if id(value) in visited:
        return 0
    visited.add(id(value))
    parents.add(id(value))
    measure = lambda item: _memory_bytes(item, visited, parents, cancel,
                                        limit_bytes=limit_bytes, _counter=counter)
    try:
        if isinstance(value, pd.DataFrame):
            charge(object.__sizeof__(value) + 1024 + 256 * len(value.columns)
                   + int(value.memory_usage(index=False, deep=False).sum()))
            measure(value.index); measure(value.columns); measure(value.attrs)
            for position, dtype in enumerate(value.dtypes):
                if dtype == object:
                    for cell in value.iloc[:, position]:
                        measure(cell)
        elif isinstance(value, pd.Series):
            charge(object.__sizeof__(value) + 1024 + int(value.memory_usage(index=False, deep=False)))
            measure(value.index); measure(value.name); measure(value.attrs)
            if value.dtype == object:
                for cell in value:
                    measure(cell)
        elif isinstance(value, pd.MultiIndex):
            charge(object.__sizeof__(value) + 1024)
            for item in (*value.names, *value.levels, *value.codes):
                measure(item)
        elif isinstance(value, pd.Index):
            charge(object.__sizeof__(value) + 512 + int(value.memory_usage(deep=False)))
            measure(value.name)
            if value.dtype == object:
                for cell in value:
                    measure(cell)
        elif isinstance(value, np.ndarray):
            _dtype_identity(value.dtype)
            charge(object.__sizeof__(value) + int(value.nbytes))
            if value.dtype == object:
                for cell in value.flat:
                    measure(cell)
        elif is_dataclass(value) and not isinstance(value, type):
            charge(sys.getsizeof(value))
            for item in fields(value):
                measure(getattr(value, item.name))
        elif isinstance(value, Mapping):
            charge(sys.getsizeof(value))
            for key, item in value.items():
                measure(key); measure(item)
        elif type(value) in {tuple, list, set, frozenset}:
            charge(sys.getsizeof(value))
            for item in value:
                measure(item)
        elif (value is None or value is pd.NA or value is pd.NaT
              or type(value) in {str, bytes, bool, int, float, datetime, date, timedelta}
              or isinstance(value, (pd.Timestamp, pd.Timedelta, np.generic))):
            if isinstance(value, np.generic):
                _dtype_identity(value.dtype)
            charge(sys.getsizeof(value))
        else:
            raise CacheIdentityError(f"Unsupported retained input type: {type(value).__name__}")
        return counter[0] - initial
    finally:
        parents.remove(id(value))


@dataclass
class _Entry:
    value: Any
    integrity: str
    byte_size: int
    source_run_id: str
    source_phase: str


class ExecutionCache:
    """One session's private cache; explicit opt-in at the public core boundary."""

    def __init__(self, *, max_bytes: int = DEFAULT_CACHE_BYTES, max_artifacts: int = 16,
                 max_key_input_bytes: int = DEFAULT_KEY_INPUT_BYTES) -> None:
        if (type(max_bytes) is not int or max_bytes < 0 or type(max_artifacts) is not int or max_artifacts < 0
                or type(max_key_input_bytes) is not int or max_key_input_bytes < 0):
            raise ValueError("Cache budgets must be nonnegative integers")
        self.max_bytes = max_bytes
        self.max_artifacts = max_artifacts
        self.max_key_input_bytes = max_key_input_bytes
        self._lock = RLock()
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._epoch = 0
        self._serial = 0
        self._reserved_bytes = 0

    @property
    def byte_size(self) -> int:
        with self._lock:
            return sum(entry.byte_size for entry in self._entries.values())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._epoch += 1

    def begin(self, *, run_id: str, phase: str, cancel: Callable[[], bool] | None = None,
              eligible: Callable[[], bool] | None = None) -> "CacheTransaction":
        if phase not in {"preflight", "run"} or not isinstance(run_id, str) or not run_id:
            raise ValueError("A cache transaction needs explicit source run and phase")
        with self._lock:
            self._serial += 1
            return CacheTransaction(self, run_id, phase, self._epoch, self._serial, cancel, eligible)


class CacheTransaction:
    """Private stage work is published only after the true operation succeeds."""

    def __init__(self, cache: ExecutionCache, run_id: str, phase: str, epoch: int, serial: int,
                 cancel: Callable[[], bool] | None, eligible: Callable[[], bool] | None) -> None:
        self._cache, self.run_id, self.phase = cache, run_id, phase
        self._epoch, self._serial, self._cancel, self._eligible = epoch, serial, cancel, eligible
        self._pending: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._reserved_bytes = 0
        self._closed = False
        self.audit: dict[str, Any] = {"schema": CACHE_AUDIT_SCHEMA_VERSION,
                                      "key_schema": CACHE_KEY_SCHEMA_VERSION,
                                      "run_id": run_id, "phase": phase, "stages": [],
                                      "commit_status": "pending", "budget_bytes": cache.max_bytes,
                                      "key_input_budget_bytes": cache.max_key_input_bytes}

    def key(self, namespace: str, inputs: Any) -> str | None:
        try:
            if self._cancel and self._cancel():
                raise CacheIdentityError("cancelled before semantic key")
            _memory_bytes(inputs, cancel=self._cancel, limit_bytes=self._cache.max_key_input_bytes)
            return semantic_key(inputs, self._cancel)
        except _KeyInputBudgetExceeded as exc:
            self.audit["stages"].append({"stage": namespace, "outcome": "miss", "reason": str(exc),
                                          "input_bytes": exc.byte_size, "input_bytes_is_lower_bound": True,
                                          "limit_bytes": exc.limit})
            return None
        except (CacheIdentityError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            self.audit["stages"].append({"stage": namespace, "outcome": "miss", "reason": str(exc)})
            return None

    def get(self, namespace: str, key: str | None) -> Any | None:
        if key is None:
            return None
        if self._closed:
            return None
        with self._cache._lock:
            entry = self._cache._entries.get((namespace, key))
            if entry is None:
                self.audit["stages"].append({"stage": namespace, "key": key, "outcome": "miss", "reason": "not_retained"})
                return None
            epoch = self._cache._epoch
        try:
            if semantic_key(entry.value, self._cancel) != entry.integrity:
                raise CacheIdentityError("artifact_integrity_mismatch")
            value = _clone(entry.value, self._cancel)
        except (CacheIdentityError, TypeError, ValueError, RecursionError) as exc:
            with self._cache._lock:
                if not (self._cancel and self._cancel()):
                    if self._cache._entries.get((namespace, key)) is entry:
                        self._cache._entries.pop((namespace, key), None)
            self.audit["stages"].append({"stage": namespace, "key": key, "outcome": "miss", "reason": str(exc)})
            return None
        with self._cache._lock:
            if (epoch != self._cache._epoch or self._epoch != epoch
                    or self._cache._entries.get((namespace, key)) is not entry
                    or (self._cancel and self._cancel())
                    or (self._eligible is not None and not self._eligible())):
                self.audit["stages"].append({"stage": namespace, "key": key, "outcome": "miss", "reason": "cleared_cancelled_or_stale"})
                return None
            self.audit["stages"].append({"stage": namespace, "key": key, "outcome": "hit",
                                          "source_run_id": entry.source_run_id,
                                          "source_phase": entry.source_phase, "bytes": entry.byte_size})
            return value

    def put(self, namespace: str, key: str | None, value: Any) -> None:
        if key is None or self._closed:
            return
        reserved = 0
        try:
            size = _memory_bytes(value, cancel=self._cancel)
            with self._cache._lock:
                if self._epoch != self._cache._epoch or self._serial != self._cache._serial:
                    return
                old = self._pending.pop((namespace, key), None)
                if old is not None:
                    self._reserved_bytes -= old.byte_size
                    self._cache._reserved_bytes -= old.byte_size
                if size + self._cache.byte_size + self._cache._reserved_bytes > self._cache.max_bytes:
                    self.audit["stages"].append({"stage": namespace, "key": key, "outcome": "not_retained",
                                                  "reason": "oversized" if size > self._cache.max_bytes else "transaction_memory_budget",
                                                  "bytes": size})
                    return
                reserved = size
                self._reserved_bytes += size
                self._cache._reserved_bytes += size
            isolated = _clone(value, self._cancel)
            integrity = semantic_key(isolated, self._cancel)
            with self._cache._lock:
                if self._closed or self._epoch != self._cache._epoch or self._serial != self._cache._serial:
                    if not self._closed:
                        self._reserved_bytes -= reserved
                        self._cache._reserved_bytes -= reserved
                    return
                self._pending[(namespace, key)] = _Entry(isolated, integrity, size, self.run_id, self.phase)
        except (CacheIdentityError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            with self._cache._lock:
                if not self._closed:
                    self._reserved_bytes -= reserved
                    self._cache._reserved_bytes -= reserved
            self.audit["stages"].append({"stage": namespace, "key": key, "outcome": "not_retained", "reason": str(exc)})

    def finish(self, *, complete: bool) -> None:
        if self._closed:
            return
        self._closed = True
        with self._cache._lock:
            self._cache._reserved_bytes -= self._reserved_bytes
            self._reserved_bytes = 0
            valid = (complete and not (self._cancel and self._cancel())
                     and self._epoch == self._cache._epoch and self._serial == self._cache._serial
                     and (self._eligible is None or self._eligible()))
            if not valid:
                self._pending.clear()
                self.audit["commit_status"] = "discarded_incomplete_cancelled_or_stale"
                return
            evicted = []
            for (namespace, key), entry in self._pending.items():
                if namespace in {"base", "eyes", "events"}:
                    for old in list(self._cache._entries):
                        if old[0] == namespace:
                            evicted.append({"stage": old[0], "key": old[1], "reason": "latest_stage_replaced"})
                            self._cache._entries.pop(old)
                self._cache._entries[(namespace, key)] = entry
                self._cache._entries.move_to_end((namespace, key))
            while self._cache._entries and (
                self._cache.byte_size > self._cache.max_bytes
                or sum(name[0] not in {"base", "eyes", "events"} for name in self._cache._entries) > self._cache.max_artifacts
            ):
                (namespace, key), _ = self._cache._entries.popitem(last=False)
                evicted.append({"stage": namespace, "key": key, "reason": "memory_or_artifact_budget"})
            self._pending.clear()
            self.audit.update(commit_status="committed_preflight_complete" if self.phase == "preflight" else "committed_analysis_complete",
                              retained_bytes=self._cache.byte_size, evictions=evicted)
