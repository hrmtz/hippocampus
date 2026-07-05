"""codex adapter — ported from scripts/ingest_codex_history.py.

Incremental contract: conv_id-known skip; APPEND-BLIND (appended history
lines for a known session are never re-read) — known limitation carried
over verbatim from the legacy script; behavior changes are out of scope
for an equivalence-gated port (gh #45).

project_slug: history.jsonl carries no cwd; it is resolved from the
matching ~/.codex/sessions/**/rollout-*.jsonl session_meta line, then
normalized SQL-side via personal.canonical_project_slug() (SoT).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Iterator

from ...parsers.codex_history_parser import parse_history_file
from ..base import EmbedParams, IngestContext, SourceItem

CONV_UPSERT_SQL = """
    INSERT INTO personal.conversations
        (conv_id, platform, title, started_at, ended_at, msg_count, model,
         project_slug)
    VALUES (%(conv_id)s, %(platform)s, %(title)s, %(started_at)s,
            %(ended_at)s, %(msg_count)s, %(model)s,
            personal.canonical_project_slug(NULL, %(cwd)s))
    ON CONFLICT (conv_id) DO UPDATE SET
        msg_count = EXCLUDED.msg_count,
        ended_at  = EXCLUDED.ended_at,
        project_slug = COALESCE(personal.conversations.project_slug,
                                EXCLUDED.project_slug)
"""


class CodexAdapter:
    name = "codex"
    platform = "codex"
    embed_params = EmbedParams(batch_size=64, max_length=512)
    scores = True
    conv_upsert_sql = CONV_UPSERT_SQL

    def __init__(self) -> None:
        self._cwd_map: dict[str, str] = {}

    def discover(self, ctx: IngestContext) -> Iterable[SourceItem]:
        history_file = Path(os.environ.get(
            "CODEX_HISTORY_FILE", os.path.expanduser("~/.codex/history.jsonl")))
        if not history_file.exists():
            print(f"history file not found: {history_file}", flush=True)
            return
        self._cwd_map = _build_session_cwd_map()
        print(f"codex session cwd map: {len(self._cwd_map)} sessions", flush=True)
        cur = ctx.conn.cursor()
        cur.execute("SELECT conv_id FROM personal.conversations "
                    "WHERE platform='codex'")
        ctx.known = {r[0] for r in cur.fetchall()}
        print(f"known codex conv_ids: {len(ctx.known)}", flush=True)
        yield SourceItem(path=history_file)

    def parse(self, item: SourceItem) -> Iterator[tuple[dict, list[dict]]]:
        for conv, msgs in parse_history_file(item.path):
            conv["cwd"] = self._cwd_map.get(_session_id_of(conv["conv_id"]))
            yield conv, msgs

    def enrich(self, conv: dict, cur) -> dict:
        return conv  # slug resolved SQL-side in the upsert

    def should_ingest(self, conv: dict, ctx: IngestContext) -> bool:
        return conv["conv_id"] not in (ctx.known or set())


def _session_id_of(conv_id: str) -> str:
    return conv_id.split("codex:", 1)[-1]


def _build_session_cwd_map() -> dict[str, str]:
    """session_id → cwd from rollout-*.jsonl session_meta first lines."""
    sessions_dir = Path(os.environ.get(
        "CODEX_SESSIONS_DIR", os.path.expanduser("~/.codex/sessions")))
    cwd_map: dict[str, str] = {}
    if not sessions_dir.exists():
        return cwd_map
    for f in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            with f.open(encoding="utf-8") as fh:
                rec = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        if rec.get("type") != "session_meta":
            continue
        payload = rec.get("payload") or {}
        sid, cwd = payload.get("id"), payload.get("cwd")
        if sid and cwd:
            cwd_map[str(sid)] = str(cwd)
    return cwd_map
