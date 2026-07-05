"""chatgpt adapter — ported from scripts/ingest_chatgpt.py.

Incremental contract: one-shot ZIP set-diff (should_ingest drops known
conv_ids). Embed params 8/2048 are source-owned — long-form web chats.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

from ...parsers.chatgpt_parser import parse_zip
from ..base import DEFAULT_CONV_UPSERT_SQL, EmbedParams, IngestContext, SourceItem


class ChatGPTAdapter:
    name = "chatgpt"
    platform = "chatgpt"
    embed_params = EmbedParams(batch_size=8, max_length=2048)
    scores = False
    conv_upsert_sql = DEFAULT_CONV_UPSERT_SQL

    def discover(self, ctx: IngestContext) -> Iterable[SourceItem]:
        if not ctx.args:
            raise SystemExit("usage: hippocampus ingest chatgpt <export.zip>")
        zip_path = Path(ctx.args[0])
        if not zip_path.exists():
            raise SystemExit(f"not found: {zip_path}")
        cur = ctx.conn.cursor()
        cur.execute("SELECT conv_id FROM personal.conversations "
                    "WHERE platform = %s", (self.platform,))
        ctx.known = {r[0] for r in cur.fetchall()}
        print(f"known conv_ids: {len(ctx.known)}", flush=True)
        yield SourceItem(path=zip_path)

    def parse(self, item: SourceItem) -> Iterator[tuple[dict, list[dict]]]:
        yield from parse_zip(item.path)

    def enrich(self, conv: dict, cur) -> dict:
        return conv

    def should_ingest(self, conv: dict, ctx: IngestContext) -> bool:
        return conv["conv_id"] not in (ctx.known or set())
