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

# Baseline conversation upsert (chatgpt / claude-ai). Adapters with extra
# columns (claude-code: last_file_size + project_slug; codex: SQL-side slug
# resolution) declare their own conv_upsert_sql — the pipeline executes
# whatever the adapter declares with the conv dict as parameters.
DEFAULT_CONV_UPSERT_SQL = """
    INSERT INTO personal.conversations
        (conv_id, platform, title, started_at, ended_at, msg_count, model)
    VALUES (%(conv_id)s, %(platform)s, %(title)s, %(started_at)s,
            %(ended_at)s, %(msg_count)s, %(model)s)
    ON CONFLICT (conv_id) DO UPDATE SET
        msg_count = EXCLUDED.msg_count,
        ended_at  = EXCLUDED.ended_at
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
