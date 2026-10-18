"""Shared reference-based MultiMatch and ScanMatch implementations."""
from __future__ import annotations

from typing import Any, Callable

import numpy as np


MULTIMATCH_DIMENSIONS = ("shape", "direction", "length", "position", "duration")


def multimatch_dimensions(fixations_a: np.ndarray, fixations_b: np.ndarray,
                          screen_size_px: tuple[float, float] | list[float],
                          parameters: dict[str, Any]) -> np.ndarray:
    """Return the five published MultiMatch dimensions from multimatch-gaze."""
    from multimatch_gaze import docomparison

    return np.asarray(
        docomparison(
            fixations_a,
            fixations_b,
            screen_size_px,
            grouping=bool(parameters.get("grouping", False)),
            TDir=float(parameters.get("TDir", 0)),
            TDur=float(parameters.get("TDur", 0)),
            TAmp=float(parameters.get("TAmp", 0)),
        ),
        dtype=float,
    )


def fixation_vectors(start_x: np.ndarray, start_y: np.ndarray,
                     duration_s: np.ndarray) -> np.ndarray:
    """Build the exact structured input expected by multimatch-gaze 0.1.3."""
    x = np.asarray(start_x, dtype=float)
    y = np.asarray(start_y, dtype=float)
    duration = np.asarray(duration_s, dtype=float)
    if x.ndim != 1 or y.ndim != 1 or duration.ndim != 1 or not (len(x) == len(y) == len(duration)):
        raise ValueError("fixation vectors must be equal-length one-dimensional arrays")
    if not (np.isfinite(x).all() and np.isfinite(y).all() and np.isfinite(duration).all()):
        raise ValueError("MultiMatch fixation vectors must be finite")
    if np.any(duration <= 0):
        raise ValueError("MultiMatch fixation durations must be positive")
    dtype = [("start_x", "<f8"), ("start_y", "<f8"), ("duration", "<f8")]
    return np.array(list(zip(x, y, duration)), dtype=dtype)


def scanmatch_cell_tokens(times: np.ndarray, x: np.ndarray, y: np.ndarray,
                          valid: np.ndarray, screen_size_px: tuple[float, float] | list[float],
                          grid_cols: int, grid_rows: int, time_bin_s: float,
                          start_s: float, end_s: float,
                          cancel: Callable[[], bool] | None = None) -> list[tuple[int, int]] | None:
    """Encode the first observed position in each explicit equal-duration bin.

    The bin domain is half-open. Empty bins and positions outside the explicit
    pixel screen block the sequence; no interpolation or gap bridging is used.
    """
    t = np.asarray(times, dtype=float)
    xv = np.asarray(x, dtype=float)
    yv = np.asarray(y, dtype=float)
    mask = np.asarray(valid, dtype=bool)
    if not (t.ndim == xv.ndim == yv.ndim == mask.ndim == 1):
        return None
    if not (len(t) == len(xv) == len(yv) == len(mask)):
        return None
    if not (np.isfinite(start_s) and np.isfinite(end_s) and end_s > start_s
            and np.isfinite(time_bin_s) and time_bin_s > 0):
        return None
    if len(screen_size_px) != 2:
        return None
    width, height = map(float, screen_size_px)
    if not (np.isfinite(width) and np.isfinite(height) and width > 0 and height > 0
            and isinstance(grid_cols, (int, np.integer)) and not isinstance(grid_cols, bool) and grid_cols > 0
            and isinstance(grid_rows, (int, np.integer)) and not isinstance(grid_rows, bool) and grid_rows > 0):
        return None
    n_bins = int(np.floor((end_s - start_s) / time_bin_s))
    if n_bins <= 0:
        return None
    tokens: list[tuple[int, int]] = []
    for bin_index in range(n_bins):
        if cancel and cancel():
            return None
        left = start_s + bin_index * time_bin_s
        right = left + time_bin_s
        indices = np.flatnonzero((t >= left) & (t < right) & mask & np.isfinite(xv) & np.isfinite(yv))
        if not len(indices):
            return None
        idx = int(indices[0])
        col = int(np.floor(xv[idx] / width * grid_cols))
        row = int(np.floor(yv[idx] / height * grid_rows))
        if not (0 <= col < grid_cols and 0 <= row < grid_rows):
            return None
        tokens.append((row, col))
    return tokens


def needleman_wunsch_score(sequence_a: list[tuple[int, int]],
                           sequence_b: list[tuple[int, int]],
                           parameters: dict[str, Any],
                           cancel: Callable[[], bool] | None = None) -> tuple[float | None, bool]:
    """Global Needleman-Wunsch score with the group's symmetric cell matrix."""
    matrix = parameters.get("substitution_matrix")
    gap = parameters.get("gap_penalty")
    if not isinstance(matrix, dict) or not isinstance(gap, (int, float, np.number)) or isinstance(gap, bool):
        return None, False
    gap = float(gap)
    if not np.isfinite(gap) or gap > 0:
        return None, False
    n, m = len(sequence_a), len(sequence_b)
    if n == 0 or m == 0:
        return None, False
    dp = np.full((n + 1, m + 1), -np.inf)
    dp[0, 0] = 0.0
    for i in range(1, n + 1):
        dp[i, 0] = dp[i - 1, 0] + gap
    for j in range(1, m + 1):
        dp[0, j] = dp[0, j - 1] + gap
    for i in range(1, n + 1):
        if cancel and cancel():
            return None, True
        for j in range(1, m + 1):
            a = sequence_a[i - 1]
            b = sequence_b[j - 1]
            key_ab = f"{a[0]},{a[1]}|{b[0]},{b[1]}"
            key_ba = f"{b[0]},{b[1]}|{a[0]},{a[1]}"
            if key_ab in matrix:
                score = matrix[key_ab]
            elif key_ba in matrix:
                score = matrix[key_ba]
            else:
                return None, False
            if isinstance(score, bool) or not isinstance(score, (int, float, np.number)) or not np.isfinite(float(score)):
                return None, False
            dp[i, j] = max(
                dp[i - 1, j - 1] + float(score),
                dp[i - 1, j] + gap,
                dp[i, j - 1] + gap,
            )
    return float(dp[n, m]), False


__all__ = [
    "MULTIMATCH_DIMENSIONS",
    "fixation_vectors",
    "multimatch_dimensions",
    "needleman_wunsch_score",
    "scanmatch_cell_tokens",
]
