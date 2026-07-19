"""
事実抽出レイヤー: 会話から Haiku で重要事実を蒸留 → personal.extracted_facts に保存。
summarize.py と同じ構造。

Usage:
  sops exec-env $CREDS_DIR/llm.enc.yaml \\
    '.venv/bin/hippocampus extract-facts [--limit N] [--platform P] [--dry-run]'

Options:
  --platforms  comma-separated platforms (default: claude_code,chatgpt,claude_ai,codex)
  --limit      max conversations to process (default: all pending)
  --dry-run    print pending count without writing
"""
import json
import sys
import time
import argparse

from psycopg2.extras import execute_values

from ..embed.client import get_default_client
from ..maintenance import assert_not_frozen
from .db import get_conn, resolve_anthropic_key
from .llm_guard import GUARD_LINE, is_role_echo
from .transcript import conversation_seqs, sample_uniform, transcript_lines

FACTS_MODEL = "claude-haiku-4-5-20251001"
FACTS_MAX_TOKENS = 768
EMBED_BATCH_SIZE = 32
SAMPLE_MSGS = 40
MIN_PROSE_LEN = 20

FACTS_PROMPT = """\
以下の会話から重要な事実・決定・嗜好・コンテキストを抽出してください。

除外: コード・ログ・手順・ツール呼び出し結果・雑談。
含める: 誰が何を好む / 何を決めた / 何を作っている / 何がなぜ起きた / どんな問題があった。

JSON形式で返してください (それ以外は不要):
{{"facts": ["fact1", "fact2", ...]}}

制約: 最大8件、各 120 文字以内、日本語または会話の言語で記述。
{guard}

会話:
---
{transcript}
---"""

DEFAULT_PLATFORMS = ('claude_code', 'chatgpt', 'claude_ai', 'codex')


def build_transcript(cur, conv_id: str) -> str:
    seqs = conversation_seqs(cur, conv_id, min_prose_len=MIN_PROSE_LEN)
    sample = sample_uniform(seqs, SAMPLE_MSGS)
    return '\n\n'.join(transcript_lines(
        cur, conv_id, sample, ai_label="AI", max_chars=300,
        skip_diff=True, min_prose_len=MIN_PROSE_LEN)).strip()


def extract_facts_haiku(client, transcript: str) -> list[str]:
    if not transcript:
        return []
    try:
        msg = client.messages.create(
            model=FACTS_MODEL,
            max_tokens=FACTS_MAX_TOKENS,
            messages=[{"role": "user",
                       "content": FACTS_PROMPT.format(
                           transcript=transcript, guard=GUARD_LINE)}]
        )
        raw = msg.content[0].text.strip()
        # strip markdown code fence if Haiku wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        facts = data.get("facts", [])
        return [f.strip() for f in facts
                if isinstance(f, str) and f.strip() and not is_role_echo(f)][:8]
    except (json.JSONDecodeError, KeyError, IndexError) as ex:
        print(f"  parse error: {ex}", flush=True)
        return []
    except Exception as ex:
        print(f"  haiku error: {ex}", flush=True)
        return []


def pending_conversations(conn, platforms: tuple, limit: int | None) -> list[str]:
    plat_ph = ','.join(['%s'] * len(platforms))
    sql = f"""
        SELECT c.conv_id FROM personal.conversations c
        WHERE c.platform IN ({plat_ph})
          AND c.msg_count > 1
          AND NOT EXISTS (
              SELECT 1 FROM personal.extracted_facts f
              WHERE f.conv_id = c.conv_id
          )
        ORDER BY c.ended_at DESC NULLS LAST
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = conn.cursor()
    cur.execute(sql, platforms)
    return [r[0] for r in cur.fetchall()]


def _mark_processed(cur, conn, conv_id: str) -> None:
    """Insert a sentinel row (fact_text='', dense=NULL) to mark conv as processed.
    Prevents infinite re-querying of conversations with no extractable facts.
    search_facts excludes these rows via WHERE dense IS NOT NULL.
    """
    cur.execute("""
        INSERT INTO personal.extracted_facts (conv_id, fact_text)
        VALUES (%s, '')
        ON CONFLICT DO NOTHING
    """, (conv_id,))
    conn.commit()


def process_conversation(client, cur, conn, conv_id: str) -> int:
    """Extract facts for one conversation. Returns number of facts inserted."""
    transcript = build_transcript(cur, conv_id)
    if not transcript:
        _mark_processed(cur, conn, conv_id)
        return 0
    facts = extract_facts_haiku(client, transcript)
    if not facts:
        _mark_processed(cur, conn, conv_id)
        return 0

    vecs = get_default_client().encode_batch(
        facts, where="extract_facts", batch_size=EMBED_BATCH_SIZE, max_length=256
    )
    execute_values(cur, """
        INSERT INTO personal.extracted_facts (conv_id, fact_text, dense)
        VALUES %s
    """, [(conv_id, fact, vec) for fact, vec in zip(facts, vecs)])
    conn.commit()
    return len(facts)


def main():
    parser = argparse.ArgumentParser(prog="hippocampus extract-facts")
    parser.add_argument('--platforms', default=','.join(DEFAULT_PLATFORMS))
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    platforms = tuple(p.strip() for p in args.platforms.split(','))
    api_key = resolve_anthropic_key()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY_INGEST / CF_ANTHROPIC_API_KEY / "
              "ANTHROPIC_API_KEY not set", flush=True)
        sys.exit(1)

    conn = get_conn()
    try:
        conv_ids = pending_conversations(conn, platforms, args.limit)
        print(f"pending: {len(conv_ids)} conversations", flush=True)
        if args.dry_run or not conv_ids:
            return
        assert_not_frozen(conn)

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        cur = conn.cursor()
        total_facts = skip = 0

        for i, conv_id in enumerate(conv_ids):
            try:
                n = process_conversation(client, cur, conn, conv_id)
                if n == 0:
                    skip += 1
                else:
                    total_facts += n
            except Exception as ex:
                skip += 1
                print(f"  FAIL {conv_id}: {ex}", flush=True)
                try:
                    conn.rollback()
                except Exception:
                    pass

            if (i + 1) % 50 == 0:
                print(
                    f"  {i+1}/{len(conv_ids)} | facts={total_facts} skip={skip}",
                    flush=True
                )
            time.sleep(0.3)

        print(f"done: facts={total_facts} skip={skip}", flush=True)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
