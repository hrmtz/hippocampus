"""
会話レベルのrollup embed backfill。
- 全会話: conversations.conv_dense (均等サンプリング)
- 長い会話 (>SEG_SIZE msgs): conversation_segments に200件ごとのセグメントも作成

Usage:
  sops exec-env secrets.enc.yaml '.venv/bin/python3 scripts/backfill_conv_summaries.py'

Options:
  --platforms    comma-separated (default: claude_code,chatgpt,claude_ai,codex)
  --limit        max conversations to process (default: all)
  --seg-size     messages per segment (default: 200)
  --dry-run      print stats without writing
  --segments-only  only build missing segments, skip conv_dense updates
"""
import sys, time, argparse

from ..embed.client import get_default_client
from ..maintenance import assert_not_frozen
from .db import get_conn, resolve_anthropic_key
from .llm_guard import GUARD_LINE, is_role_echo
from .transcript import conversation_seqs, sample_uniform, transcript_lines

SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_MAX_TOKENS = 320  # 話者帰属型要約は本質的に冗長(user/AI双方を主語明示)。Haikuの自然長は~290-350字(~1.32字/token)で文途中切断を避けるにはこの余裕が必要(200/256では切断発生)。
EMBED_BATCH_SIZE = 32
SAMPLE_PER_SEGMENT = 20  # messages sampled per segment for Haiku summary
MIN_PROSE_LEN = 20

SUMMARY_PROMPT = """以下の会話の一部を200〜300字程度の日本語で、要点を簡潔に要約してください。
**誰の発話か(user / AI)を主語で明示**し、user自身の発言・判断・体験と、AIの提案・主張を区別してください。
AIが述べた事実主張は「AIによれば」等、出自がわかる形にし、userの記憶・判断として断定しないでください(= source monitoring: 後で思い出した時に「自分が考えた」か「AIが言った」かを取り違えないため)。
主要なトピック・結論・感情的な高まりがあれば含めてください。
冗長な列挙は避け、前置き・見出し(「要約」等)を付けず、要約本文のみを返してください。
{guard}

会話:
---
{transcript}
---"""

DEFAULT_PLATFORMS = ('claude_code', 'chatgpt', 'claude_ai', 'codex')


def embed_texts(texts: list[str]) -> list[list[float]]:
    # Process-wide singleton, not a fresh EmbedClient per flush: under
    # EMBED_PROVIDER=bge-inprocess the model is cached in instance state, so
    # a new client per batch would reload BGE-M3 every 32 summaries.
    return get_default_client().encode_batch(
        texts, where="summarize", batch_size=EMBED_BATCH_SIZE, max_length=512)


def summarize(client, transcript: str) -> str | None:
    if not transcript:
        return None
    try:
        msg = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=SUMMARY_MAX_TOKENS,
            messages=[{"role": "user",
                       "content": SUMMARY_PROMPT.format(
                           transcript=transcript, guard=GUARD_LINE)}]
        )
        summary = msg.content[0].text.strip()
        # role-echo (transcript hijack) のみ弾く。長さ下限は設けない: 正規の短い
        # 要約を None 化すると conv_dense が NULL のまま再選択され Haiku を毎晩
        # 再課金してしまう (pending = conv_dense IS NULL、sentinel なし)。
        if is_role_echo(summary):
            print(f"  summarize: role-echo output skipped: {summary[:40]!r}",
                  flush=True)
            return None
        return summary
    except Exception as ex:
        print(f"  summarize error: {ex}", flush=True)
        return None


class EmbedBatcher:
    """Accumulates (key, summary) pairs and flushes to DB in batches."""

    def __init__(self, conn, batch_size: int = EMBED_BATCH_SIZE):
        self.conn = conn
        self.batch_size = batch_size
        self._keys: list = []
        self._texts: list[str] = []
        self.ok = self.fail = 0

    def add(self, key, summary: str):
        self._keys.append(key)
        self._texts.append(summary)
        if len(self._texts) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self._texts:
            return
        try:
            vecs = embed_texts(self._texts)
            cur = self.conn.cursor()
            for key, summary, vec in zip(self._keys, self._texts, vecs):
                if isinstance(key, str):
                    # conversations.conv_dense update
                    cur.execute("""
                        UPDATE personal.conversations
                        SET summary_text=%s, conv_dense=%s
                        WHERE conv_id=%s
                    """, (summary, vec, key))
                else:
                    # conversation_segments insert
                    conv_id, seg_idx, start_seq, end_seq = key
                    cur.execute("""
                        INSERT INTO personal.conversation_segments
                            (conv_id, seg_idx, start_seq, end_seq, summary_text, seg_dense)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (conv_id, seg_idx) DO UPDATE SET
                            summary_text = EXCLUDED.summary_text,
                            seg_dense    = EXCLUDED.seg_dense
                    """, (conv_id, seg_idx, start_seq, end_seq, summary, vec))
            self.conn.commit()
            self.ok += len(self._texts)
        except Exception as ex:
            self.conn.rollback()
            self.fail += len(self._texts)
            print(f"  batch embed error: {ex}", flush=True)
        self._keys.clear()
        self._texts.clear()


def pending_conversations(conn, platforms: tuple, limit: int | None,
                          segments_only: bool, seg_size: int = 200) -> list[tuple]:
    """Return (conv_id, msg_count) needing work."""
    plat_ph = ','.join(['%s'] * len(platforms))
    if segments_only:
        sql = f"""
            SELECT c.conv_id, c.msg_count FROM personal.conversations c
            WHERE c.platform IN ({plat_ph})
              AND c.msg_count > %s
              AND NOT EXISTS (
                  SELECT 1 FROM personal.conversation_segments s
                  WHERE s.conv_id = c.conv_id
              )
            ORDER BY c.msg_count DESC
        """
        params: tuple = platforms + (seg_size,)
    else:
        sql = f"""
            SELECT conv_id, msg_count FROM personal.conversations
            WHERE platform IN ({plat_ph})
              AND conv_dense IS NULL
              AND msg_count > 1
            ORDER BY ended_at DESC NULLS LAST
        """
        params = platforms
    if limit:
        sql += f" LIMIT {limit}"
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def process_conversation(client, conn_cur, batcher: EmbedBatcher,
                         conv_id: str, msg_count: int, seg_size: int,
                         segments_only: bool) -> tuple[int, int]:
    """Returns (conv_summaries_added, segments_added)."""
    cur = conn_cur
    seqs = conversation_seqs(cur, conv_id, min_prose_len=MIN_PROSE_LEN)
    if not seqs:
        return 0, 0

    conv_added = seg_added = 0

    def _transcript(sample_seqs_list):
        return '\n\n'.join(transcript_lines(
            cur, conv_id, sample_seqs_list, ai_label="AI", max_chars=300,
            skip_diff=False, min_prose_len=MIN_PROSE_LEN)).strip()

    # --- whole-conversation summary (skip if segments_only) ---
    if not segments_only:
        sample = sample_uniform(seqs, SAMPLE_PER_SEGMENT * 2)  # 40 msgs across full conv
        transcript = _transcript(sample)
        summary = summarize(client, transcript) if transcript else None
        if summary:
            batcher.add(conv_id, summary)
            conv_added = 1

    # --- segments for long conversations ---
    if msg_count > seg_size:
        # Split seqs into windows of seg_size
        windows = [seqs[i:i + seg_size] for i in range(0, len(seqs), seg_size)]
        for seg_idx, window_seqs in enumerate(windows):
            sample = sample_uniform(window_seqs, SAMPLE_PER_SEGMENT)
            transcript = _transcript(sample)
            summary = summarize(client, transcript) if transcript else None
            if summary:
                batcher.add(
                    (conv_id, seg_idx, window_seqs[0], window_seqs[-1]),
                    summary
                )
                seg_added += 1
            time.sleep(0.05)  # rate limit between segment Haiku calls

    return conv_added, seg_added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platforms', default=','.join(DEFAULT_PLATFORMS))
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--seg-size', type=int, default=200)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--segments-only', action='store_true',
                        help='Only build missing segments for long conversations')
    args = parser.parse_args()

    platforms = tuple(p.strip() for p in args.platforms.split(','))
    api_key = resolve_anthropic_key()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY_INGEST / CF_ANTHROPIC_API_KEY / "
              "ANTHROPIC_API_KEY not set", flush=True)
        sys.exit(1)

    conn = get_conn()
    try:
        rows = pending_conversations(conn, platforms, args.limit, args.segments_only, args.seg_size)
        print(f"pending: {len(rows)} conversations", flush=True)
        if args.dry_run or not rows:
            return
        assert_not_frozen(conn)

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        batcher = EmbedBatcher(conn)
        cur = conn.cursor()

        total_convs = total_segs = skip = 0
        for i, (conv_id, msg_count) in enumerate(rows):
            try:
                c, s = process_conversation(
                    client, cur, batcher, conv_id, msg_count,
                    args.seg_size, args.segments_only
                )
                total_convs += c
                total_segs += s
                if c == 0 and s == 0:
                    skip += 1
            except Exception as ex:
                skip += 1
                print(f"  FAIL {conv_id}: {ex}", flush=True)

            if (i + 1) % 50 == 0:
                batcher.flush()
                print(
                    f"  {i+1}/{len(rows)} | convs={total_convs} segs={total_segs} "
                    f"skip={skip} ok={batcher.ok} fail={batcher.fail}",
                    flush=True
                )
            time.sleep(0.1)

        batcher.flush()
        print(
            f"done: convs={total_convs} segs={total_segs} skip={skip} "
            f"ok={batcher.ok} fail={batcher.fail}",
            flush=True
        )
    finally:
        conn.close()


if __name__ == '__main__':
    main()
