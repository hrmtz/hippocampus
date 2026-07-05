"""hippocampus curate-memories — link/staleness curation report (suggest-only).

OPERATOR tool: connects as ``agent_dub``, NEVER exposed via MCP, read-only on DB
and filesystem. It NEVER mutates ``.md`` files (design invariant #2). Output is a
human report (or ``--json``).

Sections (design §6):
  1. Suggested links — cosine-nearest unlinked pairs in the SAME project+chassis
     (cross-project pairs are NEVER proposed — security invariant).
  2. Dangling links — unresolved targets OR targets pointing at deleted memories.
  3. Stale memories — last_dubbed/last_activated older than N days. HONEST LIMIT:
     time/structural only; does NOT verify body content against reality.
  4. Digest — counts.

Usage:
    sops exec-env $CREDS_DIR/hippocampus.enc.yaml \\
        '.venv/bin/hippocampus curate-memories [--stale-days 90] \\
            [--sim-threshold 0.60] [--project P] [--limit N] [--json]'
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2
import psycopg2.extras


def _suggest_links(cur, project: str | None, threshold: float, limit: int):
    cur.execute("""
        SELECT a.id AS a_id, a.source_stem AS a_stem, a.source_project AS proj,
               b.id AS b_id, b.b_stem, b.sim
        FROM agent.ghost_memories a
        JOIN LATERAL (
            SELECT b.id, b.source_stem AS b_stem,
                   1 - (a.dense <=> b.dense) AS sim
            FROM agent.ghost_memories b
            WHERE b.id <> a.id
              AND b.chassis_id = a.chassis_id
              AND b.source_project = a.source_project
              AND b.deleted_at IS NULL AND b.embed_model = 'bge-m3'
              AND b.dense IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM agent.memory_edges e
                  WHERE (e.source_memory_pk = a.id AND e.target_memory_pk = b.id)
                     OR (e.source_memory_pk = b.id AND e.target_memory_pk = a.id)
              )
            ORDER BY a.dense <=> b.dense
            LIMIT 3
        ) b ON TRUE
        WHERE a.deleted_at IS NULL AND a.embed_model = 'bge-m3'
          AND a.dense IS NOT NULL
          AND (%(project)s IS NULL OR a.source_project = %(project)s)
          AND 1 - (a.dense <=> b.dense) >= %(thr)s
        ORDER BY sim DESC
        LIMIT %(lim)s
    """, {"project": project, "thr": threshold, "lim": limit * 2})  # over-fetch for post-dedup (F4)
    seen: set[tuple[int, int]] = set()
    out = []
    for r in cur.fetchall():
        key = (min(r["a_id"], r["b_id"]), max(r["a_id"], r["b_id"]))
        if key in seen:  # drop symmetric duplicate (a~b and b~a)
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:  # honest --limit after dedup
            break
    return out


def _dangling(cur, project: str | None):
    cur.execute("""
        SELECT e.source_project AS proj, s.source_stem AS src, e.target_slug,
               CASE WHEN e.target_memory_pk IS NULL THEN 'unresolved'
                    ELSE 'deleted-target' END AS kind
        FROM agent.memory_edges e
        JOIN agent.ghost_memories s ON s.id = e.source_memory_pk
        LEFT JOIN agent.ghost_memories t ON t.id = e.target_memory_pk
        WHERE (e.target_memory_pk IS NULL OR t.deleted_at IS NOT NULL)
          AND (%(project)s IS NULL OR e.source_project = %(project)s)
        ORDER BY e.source_project, s.source_stem
    """, {"project": project})
    return cur.fetchall()


def _stale(cur, project: str | None, days: int):
    cur.execute("""
        SELECT m.source_project AS proj, m.source_stem AS stem,
               m.last_dubbed_at, tel.last_activated_at
        FROM agent.ghost_memories m
        LEFT JOIN agent.ghost_telemetry tel ON tel.memory_pk = m.id
        WHERE m.deleted_at IS NULL AND m.embed_model = 'bge-m3'
          AND m.last_dubbed_at < now() - make_interval(days => %(days)s)
          AND (%(project)s IS NULL OR m.source_project = %(project)s)
        ORDER BY m.last_dubbed_at
    """, {"project": project, "days": days})
    return cur.fetchall()


def _digest(cur):
    cur.execute("""
        SELECT
          (SELECT count(*) FROM agent.ghost_memories
             WHERE deleted_at IS NULL AND embed_model='bge-m3') AS memories,
          (SELECT count(*) FROM agent.memory_edges) AS edges,
          -- same predicate as _dangling() so digest count == listed rows
          (SELECT count(*) FROM agent.memory_edges e
             LEFT JOIN agent.ghost_memories t ON t.id = e.target_memory_pk
             WHERE e.target_memory_pk IS NULL OR t.deleted_at IS NOT NULL) AS dangling
    """)
    return cur.fetchone()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hippocampus curate-memories")
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--sim-threshold", type=float, default=0.60)
    ap.add_argument("--project", default=None)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    pg_url = os.environ.get("PG_URL_AGENT_DUB")
    if not pg_url:
        print("ERROR: PG_URL_AGENT_DUB not in env (run via sops exec-env)",
              file=sys.stderr)
        return 1

    conn = psycopg2.connect(pg_url, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            suggestions = _suggest_links(cur, args.project, args.sim_threshold, args.limit)
            dangling = _dangling(cur, args.project)
            stale = _stale(cur, args.project, args.stale_days)
            digest = _digest(cur)
    except (psycopg2.errors.UndefinedColumn, psycopg2.errors.UndefinedTable):
        print("ERROR: link-graph schema absent — run `hippocampus migrate` "
              "(migration 025 not applied)", file=sys.stderr)
        return 3
    finally:
        conn.close()

    if args.json:
        def _ser(rows):
            return [{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                     for k, v in r.items()} for r in rows]
        print(json.dumps({
            "digest": dict(digest),
            "suggestions": _ser(suggestions),
            "dangling": _ser(dangling),
            "stale": _ser(stale),
        }, ensure_ascii=False, indent=2))
        return 0

    d = digest
    print(f"=== digest === memories={d['memories']} edges={d['edges']} "
          f"dangling={d['dangling']}")
    print(f"\n=== suggested links (sim>={args.sim_threshold}, same project, suggest-only) ===")
    if not suggestions:
        print("  (none)")
    for r in suggestions:
        print(f"  [{r['proj']}] {r['a_stem']}  ~  {r['b_stem']}  (sim={r['sim']:.2f})")
    print(f"\n=== dangling links ({len(dangling)}) ===")
    for r in dangling:
        print(f"  [{r['proj']}] {r['src']} -> [[{r['target_slug']}]]  ({r['kind']})")
    print(f"\n=== stale memories (> {args.stale_days}d since dub; time-based only, "
          f"NOT content-verified) ===")
    for r in stale:
        dub = r["last_dubbed_at"].strftime("%Y-%m-%d") if r["last_dubbed_at"] else "?"
        act = r["last_activated_at"].strftime("%Y-%m-%d") if r["last_activated_at"] else "never"
        print(f"  [{r['proj']}] {r['stem']}  dubbed={dub} activated={act}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
