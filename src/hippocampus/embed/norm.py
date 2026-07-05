"""L2-normalize invariant assertion at EmbedBackend boundary.

Schema contract: dense vectors are stored in halfvec(1024) with
halfvec_ip_ops indexes (= cosine via inner product on normalized vectors).
Un-normalized input causes silent ranking corruption — ranking decisions
get driven by vector magnitude instead of semantic similarity.

BGE-M3 produces L2-normalized output by default. This assertion catches
provider swaps (Voyage / OpenAI / locally-finetuned models) that ship
un-normalized vectors before they reach the DB.

See docs/EMBED_CONTRACT.md for the full contract.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EXPECTED_DIM = 1024
DEFAULT_TOL = 1e-3  # comfortably above BGE-M3 fp16 numeric drift (~1e-4)


class EmbeddingNotNormalizedError(ValueError):
    """Raised when an embed backend returns a non-unit-norm vector."""


def _l2_norm(vec: Sequence[float]) -> float:
    # numpy fast path — ~15x speedup on the search hot path, plus
    # pairwise summation has better fp32 accumulation behavior than the
    # naive Python loop. Falls back transparently for plain lists.
    if hasattr(vec, "__array__"):
        try:
            import numpy as np  # noqa: PLC0415
            return float(np.linalg.norm(np.asarray(vec, dtype=np.float32)))
        except Exception:
            pass
    s = 0.0
    for x in vec:
        s += float(x) * float(x)
    return math.sqrt(s)


def assert_normalized(
    vec: Sequence[float],
    *,
    where: str,
    tol: float = DEFAULT_TOL,
    expected_dim: int | None = EXPECTED_DIM,
) -> Sequence[float]:
    """Validate vec is L2-unit (||vec||₂ ≈ 1) and of expected dim.

    Returns vec unchanged on success. Raises EmbeddingNotNormalizedError
    with the call-site label so the trap is locatable.
    """
    if vec is None:
        raise EmbeddingNotNormalizedError(f"{where}: vec is None")
    if expected_dim is not None and len(vec) != expected_dim:
        raise EmbeddingNotNormalizedError(
            f"{where}: dim mismatch — got {len(vec)}, expected {expected_dim}"
        )
    norm = _l2_norm(vec)
    if abs(norm - 1.0) > tol:
        raise EmbeddingNotNormalizedError(
            f"{where}: ||vec||_2={norm:.6f} (tol={tol}); "
            "embed backend must L2-normalize before returning — "
            "see docs/EMBED_CONTRACT.md"
        )
    return vec


def assert_batch_normalized(
    vecs: Iterable[Sequence[float]],
    *,
    where: str,
    tol: float = DEFAULT_TOL,
    expected_dim: int | None = EXPECTED_DIM,
) -> list[Sequence[float]]:
    """Batch-mode assert. Returns the materialized list on success."""
    out: list[Sequence[float]] = []
    for i, v in enumerate(vecs):
        assert_normalized(v, where=f"{where}[{i}]", tol=tol, expected_dim=expected_dim)
        out.append(v)
    return out
