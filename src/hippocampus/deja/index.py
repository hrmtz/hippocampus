"""deja-code indexer — incremental cross-repo function index (Phase 0).

Design: docs/designs/DEJA_CODE.md §5.1. The ingest pipeline contract is
RE-IMPLEMENTED here (pipeline.run is conversation-shaped and cannot carry code
chunks — dual-magi R1 r1-pipeline-5): embed strictly BEFORE any DB write,
per-file transaction with rollback-and-continue, loud dense-NULL verification
at the end.

Concurrency: the cron wrapper's flock only serializes cron against itself; a
manual `hippocampus deja index` can overlap the nightly run. Because the
incremental writer is delete-reinsert (NOT idempotent upsert), each repo is
guarded by a PG advisory lock — the loser skips the repo (r1-schema-3).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time

from .chunker import Chunk, chunk_source
from .policy import (ALLOWLIST_PATH, MAX_FILE_BYTES, content_is_secret,
                     load_allowlist, path_allowed)

PROJECTS_ROOT = (os.environ.get("HIPPOCAMPUS_DEJA_PROJECTS_ROOT")
                 or os.path.expanduser("~/projects"))
EMBED_BATCH = 16
EMBED_MAX_LENGTH = 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo_root: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", repo_root, *args],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def list_repo_files(repo_root: str) -> list[str]:
    """Tracked files only (policy layer 2), filtered by path policy (layer 3)."""
    paths = _git(repo_root, "ls-files", "-z").split("\0")
    return [p for p in paths if p and path_allowed(p)]


def chunk_repo_file(repo_root: str, rel_path: str):
    """Returns (file_sha, lang, chunks) or None to skip (size / unreadable /
    symlink / outside-repo / secret-only). Secret chunks are dropped with a
    warning.

    Symlinks are rejected and the real path must stay inside the repo (R5
    codex: git ls-files lists tracked symlinks, and following one would index
    content from OUTSIDE the allowlisted repo — an allowlist-boundary escape).
    """
    abs_path = os.path.join(repo_root, rel_path)
    try:
        if os.path.islink(abs_path):
            return None
        real = os.path.realpath(abs_path)
        if not (real + os.sep).startswith(os.path.realpath(repo_root) + os.sep):
            return None
        size = os.path.getsize(abs_path)
        if size > MAX_FILE_BYTES:
            return None
        with open(abs_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
    try:
        chunks = chunk_source(raw, ext)
    except Exception as exc:  # tree-sitter edge: fail-soft per file
        print(f"  warn: parse failed {rel_path}: {type(exc).__name__}",
              file=sys.stderr)
        return None
    kept = []
    for c in chunks:
        if content_is_secret(c.content):
            print(f"  warn: secret-pattern chunk skipped {rel_path}:{c.symbol}",
                  file=sys.stderr)
            continue
        kept.append(c)
    lang = {"py": "python", "js": "javascript", "mjs": "javascript",
            "cjs": "javascript", "ts": "typescript", "sh": "bash",
            "bash": "bash", "html": "html"}.get(ext, ext)
    return _sha256(raw), lang, kept


def _advisory_lock(cur, repo_id: str) -> bool:
    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)::bigint)",
                (f"deja:{repo_id}",))
    return cur.fetchone()[0]


def _advisory_unlock(cur, repo_id: str) -> None:
    cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                (f"deja:{repo_id}",))


def index_repo(conn, client, repo_id: str, repo_root: str,
               dry_run: bool = False, full: bool = False) -> dict:
    """Index one repo incrementally. Returns a stats dict."""
    stats = {"repo": repo_id, "files_seen": 0, "files_changed": 0,
             "chunks_embedded": 0, "chunks_reused": 0, "files_pruned": 0,
             "skipped_lock": False}
    cur = conn.cursor()
    if not dry_run:
        if not _advisory_lock(cur, repo_id):
            print(f"  warn: {repo_id} locked by another indexer, skipping",
                  file=sys.stderr)
            stats["skipped_lock"] = True
            return stats
    try:
        head = _git(repo_root, "rev-parse", "HEAD").strip()
        files = list_repo_files(repo_root)
        stats["files_seen"] = len(files)

        if not dry_run:
            cur.execute(
                """INSERT INTO code.repos (repo_id, root_path, head_commit)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (repo_id) DO UPDATE
                   SET root_path = EXCLUDED.root_path,
                       head_commit = EXCLUDED.head_commit""",
                (repo_id, repo_root, head))
            conn.commit()

        cur.execute("SELECT path, file_sha, file_id FROM code.files "
                    "WHERE repo_id = %s", (repo_id,))
        existing = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        seen_paths = set()
        for rel_path in files:
            parsed = chunk_repo_file(repo_root, rel_path)
            if parsed is None:
                continue
            file_sha, lang, chunks = parsed
            seen_paths.add(rel_path)
            prev = existing.get(rel_path)
            if prev and prev[0] == file_sha and not full:
                continue  # unchanged — the hot path
            stats["files_changed"] += 1
            if dry_run:
                stats["chunks_embedded"] += len(chunks)
                continue
            try:
                _write_file(conn, cur, client, repo_id, rel_path, lang,
                            file_sha, chunks, stats,
                            prev[1] if prev else None)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                print(f"  warn: {repo_id}/{rel_path} failed, continuing: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)

        # prune files that vanished (or fell out of policy)
        gone = set(existing) - seen_paths
        if gone and not dry_run:
            cur.execute("DELETE FROM code.files WHERE repo_id = %s "
                        "AND path = ANY(%s)", (repo_id, list(gone)))
            conn.commit()
        stats["files_pruned"] = len(gone)

        if not dry_run:
            cur.execute(
                """UPDATE code.repos SET
                     file_count = (SELECT COUNT(*) FROM code.files
                                   WHERE repo_id = %s),
                     chunk_count = (SELECT COUNT(*) FROM code.chunks ck
                                    JOIN code.files f ON f.file_id = ck.file_id
                                    WHERE f.repo_id = %s),
                     indexed_at = NOW()
                   WHERE repo_id = %s""", (repo_id, repo_id, repo_id))
            conn.commit()
    finally:
        if not dry_run:
            # a failed statement above leaves the txn aborted; roll back first
            # or the unlock itself raises InFailedSqlTransaction and masks the
            # root cause (and the advisory lock would stay held)
            try:
                conn.rollback()
                _advisory_unlock(cur, repo_id)
                conn.commit()
            except Exception:
                pass  # lock dies with the connection at worst
    return stats


def _write_file(conn, cur, client, repo_id: str, rel_path: str, lang: str,
                file_sha: str, chunks: list[Chunk], stats: dict,
                prev_file_id):
    """Chunk-diff write for one file. Embeds BEFORE any destructive write."""
    if prev_file_id is not None:
        cur.execute("SELECT symbol, content_sha FROM code.chunks "
                    "WHERE file_id = %s ORDER BY seq", (prev_file_id,))
        old = [(r[0], r[1]) for r in cur.fetchall()]
    else:
        old = []
    # ORDERED comparison, multiplicity-preserving: a pure reorder (or a
    # duplicate collapse) must take the rewrite path, else the seq=i metadata
    # UPDATE below would attach line numbers to the wrong rows
    new = [(c.symbol, c.content_sha) for c in chunks]

    if prev_file_id is not None and old == new:
        # content moved / comments edited outside functions: refresh metadata
        # only, never touch vector rows (r1-pipeline-7 HNSW churn)
        cur.execute("UPDATE code.files SET file_sha = %s, indexed_at = NOW() "
                    "WHERE file_id = %s", (file_sha, prev_file_id))
        for i, c in enumerate(chunks):
            cur.execute("UPDATE code.chunks SET start_line = %s, end_line = %s "
                        "WHERE file_id = %s AND seq = %s",
                        (c.start_line, c.end_line, prev_file_id, i))
        return

    # dense-reuse map BEFORE any DELETE (r1-schema-6: an intra-file move must
    # not lose its reusable embedding to our own delete)
    reuse: dict[str, list] = {}
    shas = sorted({c.content_sha for c in chunks})
    if shas:
        cur.execute("SELECT DISTINCT ON (content_sha) content_sha, dense "
                    "FROM code.chunks WHERE content_sha = ANY(%s) "
                    "AND dense IS NOT NULL", (shas,))
        reuse = {r[0]: r[1] for r in cur.fetchall()}

    need_embed = [c for c in chunks if c.content_sha not in reuse]
    dense_by_sha = dict(reuse)
    if need_embed:
        # slice client-side: encode_batch's batch_size only binds on the
        # in-process path — the remote HTTP path posts the whole list in one
        # request, and a big file (hundreds of chunks × up to 6KB) would blow
        # the server's request/timeout limits and fail every night
        vecs: list = []
        for i in range(0, len(need_embed), EMBED_BATCH):
            batch = need_embed[i:i + EMBED_BATCH]
            vecs.extend(client.encode_batch(
                [c.content for c in batch], where="ingest.deja-code",
                batch_size=EMBED_BATCH, max_length=EMBED_MAX_LENGTH))
        for c, v in zip(need_embed, vecs):
            dense_by_sha[c.content_sha] = v
    stats["chunks_embedded"] += len(need_embed)
    stats["chunks_reused"] += len(chunks) - len(need_embed)

    # embed done — now the destructive part, inside the caller's transaction
    if prev_file_id is not None:
        cur.execute("DELETE FROM code.chunks WHERE file_id = %s",
                    (prev_file_id,))
        cur.execute("UPDATE code.files SET file_sha = %s, lang = %s, "
                    "indexed_at = NOW() WHERE file_id = %s",
                    (file_sha, lang, prev_file_id))
        file_id = prev_file_id
    else:
        cur.execute("INSERT INTO code.files (repo_id, path, lang, file_sha) "
                    "VALUES (%s, %s, %s, %s) RETURNING file_id",
                    (repo_id, rel_path, lang, file_sha))
        file_id = cur.fetchone()[0]

    for i, c in enumerate(chunks):
        cur.execute(
            """INSERT INTO code.chunks
                 (file_id, seq, symbol, kind, start_line, end_line,
                  content, content_sha, dense)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (file_id, i, c.symbol, c.kind, c.start_line, c.end_line,
             c.content, c.content_sha, dense_by_sha[c.content_sha]))


def prune_delisted_repos(conn, allowed: list[str], dry_run: bool) -> list[str]:
    """Repos removed from the allowlist lose ALL their data on the next run
    (right-to-forget, design §5.1/§13.4).

    Belt-and-suspenders: an empty allowlist NEVER prunes — deleting the whole
    index must be an explicit act (delist repos one by one or drop the schema),
    not the side effect of a failed file read."""
    if not allowed:
        return []
    cur = conn.cursor()
    cur.execute("SELECT repo_id FROM code.repos")
    indexed = [r[0] for r in cur.fetchall()]
    gone = [r for r in indexed if r not in allowed]
    if gone and not dry_run:
        cur.execute("DELETE FROM code.repos WHERE repo_id = ANY(%s)", (gone,))
        conn.commit()
    return gone


def verify_dense_complete(conn) -> int:
    """Loud dense-NULL check (library embed pipeline lesson): returns the
    number of NULL-dense chunks; caller exits rc=1 if nonzero."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM code.chunks WHERE dense IS NULL")
    return cur.fetchone()[0]


def run_index(repo_filter: str | None, dry_run: bool, full: bool) -> int:
    import psycopg2
    from pgvector.psycopg2 import register_vector

    from ..config import Settings
    from ..embed.client import EmbedClient

    allowed = load_allowlist()
    if not allowed:
        print(f"allowlist empty or missing ({ALLOWLIST_PATH}) — nothing to "
              "index. deja-code is opt-in per repo (design §4.3).",
              file=sys.stderr)
        return 2
    full_allowlist = list(allowed)  # pre-narrowing snapshot, used for prune
    if repo_filter:
        if repo_filter not in allowed:
            print(f"repo {repo_filter!r} is not in the allowlist "
                  f"({ALLOWLIST_PATH}) — add it first (opt-in).",
                  file=sys.stderr)
            return 2
        allowed = [repo_filter]

    settings = Settings.load()
    conn = psycopg2.connect(settings.pg_url, connect_timeout=10)
    register_vector(conn)
    client = EmbedClient()
    rc = 0
    try:
        t0 = time.monotonic()
        for repo_id in allowed:
            repo_root = os.path.join(PROJECTS_ROOT, repo_id)
            if not os.path.isdir(os.path.join(repo_root, ".git")):
                print(f"  warn: {repo_id} is not a git repo under "
                      f"{PROJECTS_ROOT}, skipping", file=sys.stderr)
                continue
            try:
                stats = index_repo(conn, client, repo_id, repo_root,
                                   dry_run=dry_run, full=full)
            except Exception as exc:
                # per-repo isolation: one broken repo (zero-commit git, git
                # timeout, ...) must not starve every repo after it
                conn.rollback()
                print(f"  warn: {repo_id} failed, continuing: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            print(f"{repo_id}: files={stats['files_seen']} "
                  f"changed={stats['files_changed']} "
                  f"embedded={stats['chunks_embedded']} "
                  f"reused={stats['chunks_reused']} "
                  f"pruned={stats['files_pruned']}"
                  + (" (dry-run)" if dry_run else "")
                  + (" (LOCKED, skipped)" if stats["skipped_lock"] else ""))
        if not repo_filter:
            # prune against the STARTUP snapshot, never a re-read: a transient
            # allowlist read failure here would return [] and delete the
            # entire index (review finding). Empty snapshot cannot reach this
            # point (guarded above).
            gone = prune_delisted_repos(conn, full_allowlist, dry_run)
            if gone:
                print(f"pruned delisted repos: {gone}")
        if not dry_run:
            nulls = verify_dense_complete(conn)
            if nulls:
                print(f"ERROR: {nulls} chunks have dense IS NULL — embed "
                      "silently failed somewhere. Fix and re-run.",
                      file=sys.stderr)
                rc = 1
        print(f"done in {time.monotonic() - t0:.1f}s")
    finally:
        conn.close()
    return rc
