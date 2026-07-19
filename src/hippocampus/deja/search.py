"""Bounded cross-repository search used by the disabled deja-code hook MVP."""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from hippocampus.deja.policy import load_allowlist


@dataclass(frozen=True)
class RepoIdentity:
    name: str
    root: str


@dataclass(frozen=True)
class SearchHit:
    proposed_index: int
    chunk_id: int
    repo: str
    path: str
    root_path: str
    symbol: str
    start_line: int
    similarity: float


class SearchUnavailable(RuntimeError):
    """Expected fail-open condition, safe to expose only as a reason code."""


def canonical_repo(cwd: str, *, timeout: float = 2.0) -> RepoIdentity | None:
    """Resolve a checkout or worktree to the indexed main repository."""
    try:
        common_proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        root_proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if common_proc.returncode or root_proc.returncode:
            return None
        checkout_root = os.path.realpath(root_proc.stdout.strip())
        common = common_proc.stdout.strip()
        common = os.path.realpath(
            common if os.path.isabs(common) else os.path.join(cwd, common)
        )
        if os.path.basename(common) == ".git":
            main_root = os.path.dirname(common)
            return RepoIdentity(os.path.basename(main_root), checkout_root)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def repo_is_allowlisted(repo: RepoIdentity) -> bool:
    return repo.name in set(load_allowlist())


def _hnsw_healthy(cur) -> bool:
    cur.execute("""
        SELECT EXISTS (
          SELECT 1 FROM pg_index i
          WHERE i.indexrelid = to_regclass('code.idx_code_chunks_dense')
            AND i.indrelid = 'code.chunks'::regclass
            AND i.indisvalid AND i.indisready)""")
    return bool(cur.fetchone()[0])


def query_cross_repo(
    texts: Sequence[str],
    cwd_repo: str,
    *,
    deadline: float,
    top_k: int = 3,
    embed_client=None,
    connect: Callable | None = None,
) -> list[SearchHit]:
    """Embed once and execute one LATERAL kNN query for all proposed chunks.

    The LATERAL form is deliberate: each proposed vector retains an indexable
    ``ORDER BY dense <#> vector LIMIT k``. A window over a cross join would
    silently turn this into a corpus-wide sequence scan.
    """
    if not texts:
        return []
    pg_url = os.environ.get("PG_URL_CODE_READ", "")
    if not pg_url:
        raise SearchUnavailable("missing_pg_url")

    remaining = deadline - time.monotonic()
    if remaining <= 0.25:
        raise SearchUnavailable("deadline")
    if embed_client is None:
        from hippocampus.embed.client import EmbedClient
        embed_client = EmbedClient(max_length=1024)
    vecs = embed_client.encode_batch(
        list(texts), where="hook.deja-pretool", retries=1,
        timeout=max(0.1, min(6.0, remaining - 0.2)), max_length=1024,
    )

    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        raise SearchUnavailable("deadline")
    if connect is None:
        import psycopg2
        connect = psycopg2.connect
    conn = connect(pg_url, connect_timeout=max(1, min(4, int(remaining))))
    try:
        cur = conn.cursor()
        if not _hnsw_healthy(cur):
            raise SearchUnavailable("hnsw_unhealthy")
        remaining_ms = max(1, int((deadline - time.monotonic() - 0.1) * 1000))
        if remaining_ms <= 1:
            raise SearchUnavailable("deadline")
        values = ",".join(["(%s, %s::halfvec)"] * len(vecs))
        params: list[object] = []
        for index, vector in enumerate(vecs):
            params.extend((index, "[" + ",".join(f"{v:.6f}" for v in vector) + "]"))
        params.extend((cwd_repo, top_k))
        cur.execute(
            f"""SET LOCAL statement_timeout = '{remaining_ms}ms';
            WITH proposed(proposed_index, vec) AS (VALUES {values})
            SELECT p.proposed_index, hit.id, hit.repo_id, hit.path,
                   hit.root_path, hit.symbol, hit.start_line, hit.sim
            FROM proposed p
            CROSS JOIN LATERAL (
                SELECT ck.id, f.repo_id, f.path, r.root_path, ck.symbol,
                       ck.start_line, -(ck.dense <#> p.vec) AS sim
                FROM code.chunks ck
                JOIN code.files f ON f.file_id = ck.file_id
                JOIN code.repos r ON r.repo_id = f.repo_id
                WHERE ck.dense IS NOT NULL AND f.repo_id <> %s
                ORDER BY ck.dense <#> p.vec
                LIMIT %s
            ) hit
            ORDER BY p.proposed_index, hit.sim DESC""",
            params,
        )
        return [SearchHit(*row) for row in cur.fetchall()]
    finally:
        conn.close()
