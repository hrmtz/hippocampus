"""Unified embed client — single boundary point for the L2-normalize invariant.

All consumers (server.py, ingest scripts, ghost dub, backfill, chatbot)
should route through `EmbedClient.encode()` / `encode_batch()` instead of
hitting the BGE-M3 model or `/embed` endpoint directly.

The client asserts L2-normalize + dim=1024 at every return path, so the
halfvec_ip_ops schema invariant (= cosine via inner product on unit
vectors) holds for every write that touches the DB.

Selection logic (explicit since epic #43 Phase 1, r2-codex-2):
- `BGE_EMBED_URL` set: HTTP path (= remote, `/embed` and `/embed_batch`)
- else `EMBED_PROVIDER=bge-ondemand`: start/reuse the local compose BGE-M3
  service on first semantic use, then call it over HTTP
- else `EMBED_PROVIDER=bge-inprocess`: in-process BGE-M3 model (= local,
  lazy-loaded, thread-safe singleton; requires the `bge-local` extra)
- else: EmbedClientError — there is NO silent in-process fallback; an
  unconfigured install must not surprise-download a 6GB model

See docs/EMBED_CONTRACT.md for the contract.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Sequence

from .norm import (
    EXPECTED_DIM,
    EmbeddingNotNormalizedError,
    assert_batch_normalized,
    assert_normalized,
)

DEFAULT_MAX_LENGTH = 512
DEFAULT_BATCH_SIZE = 64
DEFAULT_TIMEOUT_SINGLE = 10.0
DEFAULT_TIMEOUT_BATCH = 60.0
# Retry policy mirrors scripts/_ghost_common.bge_embed_retry so the unified
# boundary is no less resilient than the older helper it supersedes
# (= ultrareview bug_008): 3 attempts, exponential backoff 1s/2s/4s, 5xx
# transient, 4xx permanent.
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0


class EmbedClientError(RuntimeError):
    """Raised on configuration / transport failure (distinct from norm violation)."""


def _pick_device() -> str:
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class EmbedClient:
    """Single embed boundary for hippocampus-mcp.

    Use the module-level `encode()` / `encode_batch()` for the process-wide
    singleton, or instantiate directly for per-script configuration (e.g.,
    custom max_length for long-document ingest).
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        use_fp16: bool = True,
        max_length: int = DEFAULT_MAX_LENGTH,
    ) -> None:
        self.url = (url if url is not None else os.environ.get("BGE_EMBED_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("BGE_EMBED_TOKEN", "")
        self.provider = os.environ.get("EMBED_PROVIDER", "").strip().lower()
        self.use_fp16 = use_fp16
        self.max_length = max_length
        self._model = None
        self._lock = threading.Lock()

    @property
    def is_remote(self) -> bool:
        return bool(self.url)

    def _load_model(self):
        if self.provider != "bge-inprocess":
            raise EmbedClientError(
                "no embed backend configured: set BGE_EMBED_URL (HTTP backend) "
                "or EMBED_PROVIDER=bge-ondemand (local compose backend) "
                "or EMBED_PROVIDER=bge-inprocess (local model, requires the "
                "'bge-local' extra)"
            )
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from FlagEmbedding import BGEM3FlagModel  # noqa: PLC0415
                    device = _pick_device()
                    with contextlib.redirect_stdout(sys.stderr):
                        self._model = BGEM3FlagModel(
                            "BAAI/bge-m3", use_fp16=self.use_fp16, device=device,
                        )
        return self._model

    def encode(
        self,
        text: str,
        *,
        where: str = "embed_client.encode",
        max_length: int | None = None,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> list[float]:
        ml = max_length or self.max_length
        if self.is_remote:
            vec = self._post_embed(text, max_length=ml, retries=retries, timeout=timeout)
        elif self.provider == "bge-ondemand":
            from .ondemand import OnDemandError, ensure_endpoint  # noqa: PLC0415

            try:
                endpoint = ensure_endpoint()
            except OnDemandError as exc:
                raise EmbedClientError(str(exc)) from exc
            vec = self._post_embed(
                text, max_length=ml, retries=retries, timeout=timeout,
                url=endpoint.url, token=endpoint.token,
            )
        else:
            model = self._load_model()
            out = model.encode(
                [text], batch_size=1, max_length=ml,
                return_dense=True, return_sparse=False, return_colbert_vecs=False,
            )
            vec = out["dense_vecs"][0].tolist()
        assert_normalized(vec, where=where)
        return vec

    def encode_batch(
        self,
        texts: Sequence[str],
        *,
        where: str = "embed_client.encode_batch",
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_length: int | None = None,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        ml = max_length or self.max_length
        if self.is_remote:
            vecs = self._post_embed_batch(list(texts), max_length=ml, retries=retries, timeout=timeout)
        elif self.provider == "bge-ondemand":
            from .ondemand import OnDemandError, ensure_endpoint  # noqa: PLC0415

            try:
                endpoint = ensure_endpoint()
            except OnDemandError as exc:
                raise EmbedClientError(str(exc)) from exc
            vecs = self._post_embed_batch(
                list(texts), max_length=ml, retries=retries, timeout=timeout,
                url=endpoint.url, token=endpoint.token,
            )
        else:
            model = self._load_model()
            out = model.encode(
                list(texts), batch_size=batch_size, max_length=ml,
                return_dense=True, return_sparse=False, return_colbert_vecs=False,
            )
            vecs = out["dense_vecs"].tolist()
        assert_batch_normalized(vecs, where=where)
        return vecs

    def _post(
        self,
        path: str,
        payload_obj: dict,
        *,
        timeout: float,
        url: str | None = None,
        token: str | None = None,
        retries: int | None = None,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
    ) -> dict:
        """POST with exponential backoff. 5xx + URLError loop; 4xx propagates.

        retries=None → DEFAULT_RETRIES (= 3 attempts, ~7s worst case).
        retries=1    → single attempt, no backoff (= SessionStart hook ≤5s budget).

        Mirrors scripts/_ghost_common.bge_embed_retry semantics so any of
        the unified-boundary consumers (server.embed_query / ingest_* /
        future scripts) survive transient BGE backend blips identically.
        """
        if retries is None:
            retries = DEFAULT_RETRIES
        post_url = (url if url is not None else self.url).rstrip("/")
        post_token = token if token is not None else self.token
        if not post_url or not post_token:
            raise EmbedClientError(
                f"BGE_EMBED_URL and BGE_EMBED_TOKEN required for remote {path}"
            )
        payload = json.dumps(payload_obj).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {post_token}",
            "Content-Type": "application/json",
        }
        last_err: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(
                f"{post_url}{path}",
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 4xx = permanent (= retry pointless); 5xx = transient
                if 400 <= e.code < 500:
                    raise EmbedClientError(f"BGE 4xx: HTTP {e.code}") from e
                last_err = e
            except urllib.error.URLError as e:
                last_err = e
            if attempt < retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
        raise EmbedClientError(
            f"BGE {path} failed after {retries} attempts: {last_err}"
        )

    def _post_embed(
        self, text: str, *, max_length: int, retries: int | None = None,
        timeout: float | None = None, url: str | None = None,
        token: str | None = None,
    ) -> list[float]:
        body = self._post(
            "/embed",
            {"query": text, "max_length": max_length},
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_SINGLE,
            retries=retries,
            url=url,
            token=token,
        )
        return body["dense"]

    def _post_embed_batch(
        self, texts: list[str], *, max_length: int, retries: int | None = None,
        timeout: float | None = None, url: str | None = None,
        token: str | None = None,
    ) -> list[list[float]]:
        body = self._post(
            "/embed_batch",
            {"texts": texts, "max_length": max_length},
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_BATCH,
            retries=retries,
            url=url,
            token=token,
        )
        return body["dense"]


_default_client: EmbedClient | None = None
_default_lock = threading.Lock()


def get_default_client() -> EmbedClient:
    """Process-wide singleton — same env-derived config across all importers."""
    global _default_client
    if _default_client is None:
        with _default_lock:
            if _default_client is None:
                _default_client = EmbedClient()
    return _default_client


def encode(
    text: str,
    *,
    where: str = "embed_client.encode",
    max_length: int = DEFAULT_MAX_LENGTH,
) -> list[float]:
    return get_default_client().encode(text, where=where, max_length=max_length)


def encode_batch(
    texts: Sequence[str],
    *,
    where: str = "embed_client.encode_batch",
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> list[list[float]]:
    return get_default_client().encode_batch(
        texts, where=where, batch_size=batch_size, max_length=max_length,
    )


__all__ = [
    "EXPECTED_DIM",
    "EmbedClient",
    "EmbedClientError",
    "EmbeddingNotNormalizedError",
    "encode",
    "encode_batch",
    "get_default_client",
]
