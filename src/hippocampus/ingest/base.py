"""SourceAdapter protocol + shared records (plan §3.4, v3).

Per-adapter incremental contracts (documented here on purpose — the skip is
ADAPTER-OWNED, r3-pipeline-5; the pipeline never second-guesses it):

- claude-code: item-level diff in discover() against the DB
  (last_file_size / msg_count) — re-yields KNOWN sessions that have grown,
  look truncated (>50KB/msg), or predate last_file_size recording. Message
  dedupe relies on ON CONFLICT (conv_id, msg_id) DO NOTHING.
- chatgpt / claude-ai: one-shot ZIP set-diff — should_ingest() drops
  conversations whose conv_id is already known.
- codex: conv_id-known skip; APPEND-BLIND by design of the port (appended
  lines to a known session are never re-read). Known limitation carried
  over from the legacy script (gh #45) — an equivalence-gated port must not
  change behavior.

Embed params are SOURCE-OWNED: max_length changes truncation and therefore
the vectors themselves; they must never be silently unified (r1-pipeline-9).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol


@dataclass(frozen=True)
class EmbedParams:
    batch_size: int
    max_length: int


@dataclass
class SourceItem:
    """One unit of discovered work (a session file, a ZIP, a history file)."""
    path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestContext:
    settings: Any            # hippocampus.config.Settings
    conn: Any                # psycopg2 connection (pipeline-owned)
    args: list[str]          # source-specific CLI args (e.g. ZIP path)
    known: dict | set | None = None   # adapter-populated incremental state


# Standard message insert shared by every source (column set is identical).
MSG_INSERT_SQL = """
    INSERT INTO personal.messages
        (conv_id, msg_id, role, content, content_type, ts, seq, dense)
    VALUES %s
    ON CONFLICT (conv_id, msg_id) DO NOTHING
"""

# Multi-user (Slice 2): messages carry the owning identity so the read-side
# owner-only predicate can scope the trgm-only branch without a conversation
# join. Two extra trailing values per row (tenant_id, owner_user_id).
MSG_INSERT_SQL_MULTIUSER = """
    INSERT INTO personal.messages
        (conv_id, msg_id, role, content, content_type, ts, seq, dense,
         tenant_id, owner_user_id)
    VALUES %s
    ON CONFLICT (conv_id, msg_id) DO NOTHING
"""

# Identity columns stamped on every conversation INSERT in multi-user mode.
# Order is the splice order for both the column list and the VALUES list.
MULTIUSER_CONV_COLS = (
    "tenant_id", "owner_user_id", "visibility",
    "source_conv_id", "source_platform", "source_adapter", "source_identity_hash",
)


def source_identity_hash(tenant_id: str, owner_user_id: str,
                         source_platform: str, source_conv_id: str) -> str:
    """Python side of the shared digest contract (migration 031 §5).

    Must byte-match personal.multiuser_source_identity_hash():
    sha256(tenant \\x00 owner \\x00 platform \\x00 source_conv_id) as lowercase hex.
    """
    payload = b"\x00".join(
        s.encode("utf-8")
        for s in (tenant_id, owner_user_id, source_platform, source_conv_id)
    )
    return hashlib.sha256(payload).hexdigest()


def splice_multiuser_conv_cols(sql: str) -> str:
    """Add the MULTIUSER_CONV_COLS to a conversation upsert's column + VALUES lists.

    Works on both canonical shapes (DEFAULT_CONV_UPSERT_SQL and the adapters'
    CONV_UPSERT_SQL) by anchoring on the fixed keywords VALUES and ON CONFLICT,
    so the `%(name)s` parens in the VALUES tuple don't confuse the match. The
    ON CONFLICT clause is left untouched: identity columns are immutable per the
    write-identity trigger, so they only need stamping on INSERT.
    """
    cols = ", ".join(MULTIUSER_CONV_COLS)
    vals = ", ".join(f"%({c})s" for c in MULTIUSER_CONV_COLS)
    sql, n1 = re.subn(r"(INSERT INTO personal\.conversations\s*\(.*?)(\)\s*VALUES)",
                      rf"\1, {cols}\2", sql, count=1, flags=re.DOTALL)
    sql, n2 = re.subn(r"(VALUES\s*\(.*?)(\)\s*ON CONFLICT)",
                      rf"\1, {vals}\2", sql, count=1, flags=re.DOTALL)
    if n1 != 1 or n2 != 1:
        raise ValueError("conv upsert SQL shape not spliceable for multi-user")
    return sql

# Baseline conversation upsert (chatgpt / claude-ai). Adapters with extra
# columns (claude-code: last_file_size + project_slug; codex: SQL-side slug
# resolution) declare their own conv_upsert_sql — the pipeline executes
# whatever the adapter declares with the conv dict as parameters.
DEFAULT_CONV_UPSERT_SQL = """
    INSERT INTO personal.conversations
        (conv_id, platform, title, started_at, ended_at, msg_count, model, source_host)
    VALUES (%(conv_id)s, %(platform)s, %(title)s, %(started_at)s,
            %(ended_at)s, %(msg_count)s, %(model)s, %(source_host)s)
    ON CONFLICT (conv_id) DO UPDATE SET
        msg_count = EXCLUDED.msg_count,
        ended_at  = EXCLUDED.ended_at,
        source_host = COALESCE(personal.conversations.source_host, EXCLUDED.source_host)
"""


class SourceAdapter(Protocol):
    name: str                 # CLI name: "claude-code", "chatgpt", ...
    platform: str             # personal.conversations.platform value
    embed_params: EmbedParams
    scores: bool              # True → pipeline runs the Haiku scoring stage
    conv_upsert_sql: str

    def discover(self, ctx: IngestContext) -> Iterable[SourceItem]:
        """Yield work units. DB reads via ctx.conn are allowed (and expected
        for incremental sources); writes are not."""
        ...

    def parse(self, item: SourceItem) -> Iterator[tuple[dict, list[dict]]]:
        """Yield (conv, msgs). Scrub happens INSIDE the parsers this wraps."""
        ...

    def enrich(self, conv: dict, cur) -> dict:
        """Per-conversation DB-read enrichment (slug resolution etc.)."""
        return conv

    def should_ingest(self, conv: dict, ctx: IngestContext) -> bool:
        """Adapter-owned conversation-level skip (see module docstring)."""
        return True
