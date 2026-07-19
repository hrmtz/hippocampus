"""hippocampus sync-edges — build agent.memory_edges from ghost_memories.body.

Decoupled from the dub pipeline (design §9, codex-2): the dub loop early-returns
on unchanged body_hash before upsert, so an in-dub edge write would never cover
the existing rows. This pass reads the already-stored, already-scan-passed
``ghost_memories.body`` and rebuilds edges idempotently. Run after dub in cron.

Connects as ``agent_dub`` (PG_URL_AGENT_DUB). Resolves ``[[target]]`` by FILESTEM
within ``(chassis_id, source_project)``, bge-m3 only.

Usage:
    sops exec-env $CREDS_DIR/hippocampus.enc.yaml \\
        '.venv/bin/hippocampus sync-edges [--dry-run] [--limit N]'
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg2

from .ingest.wikilinks import extract_wikilinks

# Self-contained defense-in-depth secret check on target_slug/link_text. The body
# already passed dub's scan_blocked (Wall 2) before storage, so this is a narrow
# belt-and-suspenders for credential-shaped link aliases (codex/privsec-8).
# Mirrors the high-signal patterns of scripts/_ghost_common.BLOCKLIST_PATTERNS
# (kept self-contained to avoid importing the scripts/ tree from the package).
# Includes the generic SECRET/TOKEN/API_KEY/PASSWORD assignment class + openai keys.
_SECRET_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9]{20,}"
    r"|AGE-SECRET-KEY-[A-Z0-9]+|gh[posu]_[A-Za-z0-9]{30,}"
    r"|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|(?:postgres(?:ql)?)://[^\s]+"
    r"|(?:password|secret|token|api[_-]?key)\s*=\s*\S+)",
    re.IGNORECASE,
)


def _looks_secret(*vals: str | None) -> bool:
    return any(v and _SECRET_RE.search(v) for v in vals)


def _gc_stale_edges(conn) -> int:
    """Delete edges whose source row is gone / soft-deleted / non-bge-m3 (codex-r3-1).
    CASCADE only covers hard DELETE; ghost uses soft-delete."""
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM agent.memory_edges e
            WHERE NOT EXISTS (
                SELECT 1 FROM agent.ghost_memories m
                WHERE m.id = e.source_memory_pk
                  AND m.deleted_at IS NULL AND m.embed_model = 'bge-m3'
            )
        """)
        n = cur.rowcount
    conn.commit()
    return n


def _resolve_target(cur, chassis_id: str, project: str, slug: str) -> int | None:
    cur.execute("""
        SELECT id FROM agent.ghost_memories
        WHERE chassis_id = %s::agent.chassis_id
          AND source_project = %s
          AND lower(source_stem) = lower(%s)
          AND deleted_at IS NULL AND embed_model = 'bge-m3'
        LIMIT 1
    """, (chassis_id, project, slug))
    row = cur.fetchone()
    return row[0] if row else None


def _sync_one(cur, src_pk: int, chassis_id: str, project: str,
              links: list[tuple[str, str | None]]) -> tuple[int, int, int]:
    """Sync edges for one source memory. Returns (upserted, deleted, dangling)."""
    desired: dict[str, tuple[int | None, str | None]] = {}
    dangling = 0
    for target, alias in links:
        if _looks_secret(target, alias):
            print(f"  skip secret-shaped link from pk={src_pk}: {target!r}", flush=True)
            continue
        tpk = _resolve_target(cur, chassis_id, project, target)
        if tpk is None:
            dangling += 1
        desired[target] = (tpk, alias)

    up = 0
    for target, (tpk, alias) in desired.items():
        cur.execute("""
            INSERT INTO agent.memory_edges
                (source_memory_pk, source_chassis_id, source_project,
                 target_slug, target_memory_pk, link_text)
            VALUES (%s, %s::agent.chassis_id, %s, %s, %s, %s)
            ON CONFLICT (source_memory_pk, target_slug) DO UPDATE
              SET target_memory_pk = EXCLUDED.target_memory_pk,
                  link_text        = EXCLUDED.link_text,
                  updated_at        = now()
        """, (src_pk, chassis_id, project, target, tpk, alias))
        up += 1

    # delete edges whose target_slug is no longer present in the body
    if desired:
        cur.execute("""
            DELETE FROM agent.memory_edges
            WHERE source_memory_pk = %s AND target_slug <> ALL(%s)
        """, (src_pk, list(desired.keys())))
    else:
        cur.execute("DELETE FROM agent.memory_edges WHERE source_memory_pk = %s", (src_pk,))
    deleted = cur.rowcount
    return up, deleted, dangling


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hippocampus sync-edges")
    ap.add_argument("--dry-run", action="store_true",
                    help="report counts without writing")
    ap.add_argument("--limit", type=int, default=None, help="cap sources processed")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    pg_url = os.environ.get("PG_URL_AGENT_DUB")
    if not pg_url:
        print("ERROR: PG_URL_AGENT_DUB not in env (run via sops exec-env)",
              file=sys.stderr)
        return 1

    conn = psycopg2.connect(pg_url, connect_timeout=10)
    try:
        sql = """
            SELECT id, body, chassis_id::text, source_project, source_stem
            FROM agent.ghost_memories
            WHERE deleted_at IS NULL AND embed_model = 'bge-m3'
            ORDER BY id
        """
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        except (psycopg2.errors.UndefinedColumn, psycopg2.errors.UndefinedTable):
            print("ERROR: link-graph schema absent — run `hippocampus migrate` "
                  "(migration 025 not applied)", file=sys.stderr)
            return 3

        if args.dry_run:
            total_links = 0
            for _id, body, _ch, _proj, stem in rows:
                total_links += len(extract_wikilinks(body or "", own_stem=stem))
            print(f"dry-run: {len(rows)} sources, {total_links} body links "
                  f"(no GC/write)", flush=True)
            return 0

        gc = _gc_stale_edges(conn)
        print(f"GC: removed {gc} stale edges", flush=True)

        srcs = up_tot = del_tot = dang_tot = 0
        for src_pk, body, chassis_id, project, stem in rows:
            links = extract_wikilinks(body or "", own_stem=stem)
            try:
                with conn.cursor() as cur:
                    up, deleted, dang = _sync_one(cur, src_pk, chassis_id, project, links)
                conn.commit()
                srcs += 1
                up_tot += up
                del_tot += deleted
                dang_tot += dang
            except psycopg2.Error as exc:
                conn.rollback()
                print(f"  FAIL pk={src_pk}: {exc}", file=sys.stderr, flush=True)

        print(f"done: {srcs} sources | edges upserted={up_tot} deleted={del_tot} "
              f"dangling={dang_tot}", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
