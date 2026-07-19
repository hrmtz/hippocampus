"""Common ingest pipeline: embed → upsert → score → verify (plan §3.4 v3).

Transaction contract (pinned to the legacy scripts' semantics — this is what
makes mid-run death safe):
- embed strictly BEFORE any DB write (no zombie conversation rows with NULL
  dense); DB *reads* (known-set, exclusion prefixes, slug resolution) may
  happen earlier
- per-conversation commit; rollback-and-continue on error
- scoring is BOUNDED: only conversations ingested by THIS run, only for
  adapters that score (claude-code / codex — chatgpt / claude-ai have never
  scored). A bare `scored_at IS NULL` backlog sweep is forbidden
  (r3-pipeline-2); `--score-backlog` is the explicit opt-in.
- unscoreable conversations get a terminal marker (scored_at set, NULL
  scores) so they are attempted once, not retried forever
- verify: dense-NULL post-check over this run's conversations — the known
  silent-failure class (embed server down → unsearchable rows) exits loudly
"""
from __future__ import annotations

import json
import os
import socket
import time

from psycopg2.extras import execute_values

from ..embed.client import EmbedClient
from ..maintenance import assert_not_frozen
from .prose import extract_prose
from .base import (
    MSG_INSERT_SQL,
    MSG_INSERT_SQL_MULTIUSER,
    IngestContext,
    SourceAdapter,
    source_identity_hash,
    splice_multiuser_conv_cols,
)

SCORE_MODEL = "claude-haiku-4-5-20251001"
SCORE_MAX_TOKENS = 150
MAX_TRANSCRIPT_MSGS = 60
MIN_PROSE_LEN = 20

SCORE_PROMPT = """以下はユーザーとAIの会話の抜粋です。

この会話をスコアリングしてください:
1. 感情強度 (1-10): 会話全体の熱量・没入感
2. AI引っかかり度 (1-10): AIの応答が密度が上がっているか

JSON形式のみで返してください:
{{"intensity": <1-10>, "ai_engagement": <1-10>, "topic": "<一言>"}}

会話:
---
{transcript}
---"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def run(adapter: SourceAdapter, ctx: IngestContext) -> int:
    """Run the full pipeline for one source. Returns process exit code."""
    assert_not_frozen(ctx.conn)
    client = EmbedClient(max_length=adapter.embed_params.max_length)
    conn = ctx.conn
    ingested: list[str] = []
    ok = fail = skipped = 0

    # Slice 2: in multi-user mode every write carries the caller identity so the
    # write-identity trigger accepts it and the read predicate can scope it.
    # SQL shape is constant per run, so resolve it once here.
    mu = ctx.settings.multiuser
    conv_upsert_sql = (splice_multiuser_conv_cols(adapter.conv_upsert_sql)
                       if mu else adapter.conv_upsert_sql)
    msg_insert_sql = MSG_INSERT_SQL_MULTIUSER if mu else MSG_INSERT_SQL

    for item in adapter.discover(ctx):
        # parse() is a generator; a single corrupt/disappeared source file
        # must not abort the whole run (legacy scripts caught per-item and
        # continued). Drive it through an iterator so a raise inside the
        # generator is caught here, not at the for-loop header.
        parsed = adapter.parse(item)
        while True:
            try:
                conv, msgs = next(parsed)
            except StopIteration:
                break
            except Exception as ex:  # noqa: BLE001 — one bad file, keep going
                fail += 1
                _log(f"  FAIL parsing {getattr(item, 'path', item)}: {ex}")
                break
            if not msgs:
                continue
            if not adapter.should_ingest(conv, ctx):
                skipped += 1
                continue
            try:
                cur = conn.cursor()
                conv = adapter.enrich(conv, cur)
                # 1. embed (before any write)
                texts = [m["content"] or "" for m in msgs]
                vecs = client.encode_batch(
                    texts,
                    where=f"ingest.{adapter.name}",
                    batch_size=adapter.embed_params.batch_size,
                    max_length=adapter.embed_params.max_length,
                )
                # Preserve the physical/container origin when multiple agent
                # installations write into one hippocampus. An explicit label
                # is useful for containers whose kernel hostname is ephemeral.
                conv["source_host"] = (os.environ.get("HIPPOCAMPUS_SOURCE_HOST")
                                       or socket.gethostname())
                if mu:
                    conv["tenant_id"] = ctx.settings.tenant_id
                    conv["owner_user_id"] = ctx.settings.user_id
                    # ingest only ever stamps private; sharing is an explicit,
                    # separate action (write-identity trigger enforces this).
                    conv["visibility"] = "private"
                    conv["source_conv_id"] = conv["conv_id"]
                    conv["source_platform"] = conv["platform"]
                    conv["source_adapter"] = adapter.name
                    conv["source_identity_hash"] = source_identity_hash(
                        conv["tenant_id"], conv["owner_user_id"],
                        conv["source_platform"], conv["source_conv_id"])
                # 2. upsert (one tx, per-conversation commit)
                cur.execute(conv_upsert_sql, conv)
                mu_tail = ((conv["tenant_id"], conv["owner_user_id"]) if mu else ())
                rows = [(m["conv_id"], m["msg_id"], m["role"], m["content"],
                         m.get("content_type", "text"), m.get("ts"), m.get("seq"), v)
                        + mu_tail
                        for m, v in zip(msgs, vecs)]
                execute_values(cur, msg_insert_sql, rows)
                conn.commit()
                ingested.append(conv["conv_id"])
                ok += 1
            except Exception as ex:  # noqa: BLE001 — rollback-and-continue contract
                conn.rollback()
                fail += 1
                _log(f"  FAIL {conv.get('conv_id', '?')}: {ex}")
            if (ok + fail) % 20 == 0 and (ok + fail) > 0:
                _log(f"  {ok + fail} processed (ok={ok} fail={fail})")

    _log(f"ingest done: source={adapter.name} ok={ok} fail={fail} skipped={skipped}")

    # 3. score (bounded; optional)
    if adapter.scores and ingested:
        score_run(conn, ctx, ingested)

    # 4. verify (dense-NULL post-check on this run's conversations)
    rc = 0
    if ingested:
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) FROM personal.messages
            WHERE conv_id = ANY(%s) AND dense IS NULL
        """, (ingested,))
        nulls = cur.fetchone()[0]
        if nulls:
            _log(f"VERIFY FAIL: {nulls} dense-NULL messages in this run's "
                 f"conversations — embed coverage is broken")
            rc = 1
        else:
            _log(f"verify: dense coverage complete for {len(ingested)} conversations")
    return 1 if fail and not ok else rc


# ── scoring stage (ported from ingest_new_sessions.py, behavior-identical
#    except: terminal marker for unscoreable convs instead of retry-forever) ─

def score_run(conn, ctx: IngestContext, conv_ids: list[str]) -> None:
    import os

    api_key = (os.environ.get("CF_ANTHROPIC_API_KEY")
               or os.environ.get("ANTHROPIC_API_KEY"))
    if not api_key:
        _log("scoring skipped: no API key (CF_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY)")
        return
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    _log(f"scoring {len(conv_ids)} new conversations...")
    cur = conn.cursor()
    for conv_id in conv_ids:
        score_conv(client, conn, cur, conv_id)
        time.sleep(0.3)
    _log("scoring done.")


def score_conv(client, conn, cur, conv_id: str) -> None:
    transcript = build_transcript(cur, conv_id)
    try:
        if not transcript:
            # Terminal marker: attempted once, nothing scoreable. NULL scores +
            # scored_at set, so no future selection ever retries it.
            cur.execute("""
                UPDATE personal.conversations SET scored_at = NOW()
                WHERE conv_id = %s AND scored_at IS NULL
            """, (conv_id,))
            conn.commit()
            return
        msg = client.messages.create(
            model=SCORE_MODEL, max_tokens=SCORE_MAX_TOKENS,
            messages=[{"role": "user",
                       "content": SCORE_PROMPT.format(transcript=transcript)}],
        )
        score = parse_score(msg.content[0].text.strip())
        cur.execute("""
            UPDATE personal.conversations
            SET intensity=%s, ai_engagement=%s, dominant_topic=%s, scored_at=NOW()
            WHERE conv_id=%s
        """, (score["intensity"], score["ai_engagement"],
              score.get("topic", ""), conv_id))
        conn.commit()
    except Exception as ex:  # noqa: BLE001
        conn.rollback()
        _log(f"  score error {conv_id}: {ex}")


def format_transcript(rows) -> str:
    parts = []
    for role, content in rows:
        if not content or content.strip().startswith("[tool_result"):
            continue
        prose = extract_prose(content, max_chars=400, skip_diff=True)
        if len(prose) < MIN_PROSE_LEN:
            continue
        parts.append(f"[{'USER' if role == 'user' else 'AI'}] {prose}")
    return "\n\n".join(parts).strip()


def build_transcript(cur, conv_id: str) -> str:
    cur.execute("""
        SELECT role, content FROM personal.messages
        WHERE conv_id = %s ORDER BY seq LIMIT %s
    """, (conv_id, MAX_TRANSCRIPT_MSGS))
    return format_transcript(cur.fetchall())


def parse_score(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}") + 1
    if s < 0 or e <= s:
        raise ValueError(f"no JSON object in: {text!r}")
    return json.loads(text[s:e])
