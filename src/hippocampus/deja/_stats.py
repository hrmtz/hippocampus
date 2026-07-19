"""Small statistical helpers shared by deja-code measurement instruments."""
from __future__ import annotations

import math


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Return ``(point, low, high)`` for a Wilson score interval.

    A zero-sized sample has no estimable interval; measurement callers use the
    all-zero tuple and surface the accompanying sample size separately.
    """
    if n == 0:
        return 0.0, 0.0, 0.0
    if n < 0 or k < 0 or k > n:
        raise ValueError("wilson_ci requires 0 <= k <= n")
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return p, max(0.0, center - half), min(1.0, center + half)
