"""Shared transcript assembly for ingest LLM passes (single SoT).

The uniform-step sampling and prose-line formatting were triplicated across
diary / summarize / extract_facts (with subtle drift in label/skip_diff).
Hoisted here and parameterized.

Seq-first by design: select the qualifying message seqs, uniformly sample,
then fetch content for only the sampled seqs — avoids pulling a whole
conversation's prose over the wire just to discard most of it after sampling.
"""
from .prose import extract_prose

DEFAULT_MIN_PROSE_LEN = 20


def sample_uniform(items: list, n: int) -> list:
    """Uniformly pick n items by index step (preserves first..last spread)."""
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def conversation_seqs(cur, conv_id: str, *,
                      min_prose_len: int = DEFAULT_MIN_PROSE_LEN) -> list[int]:
    """Sorted seqs of prose messages (non-tool_result, length >= min_prose_len)."""
    cur.execute("""
        SELECT seq FROM personal.messages
        WHERE conv_id = %s AND content IS NOT NULL
          AND content NOT LIKE '[tool_result%%'
          AND length(content) >= %s
        ORDER BY seq
    """, (conv_id, min_prose_len))
    return [r[0] for r in cur.fetchall()]


def transcript_lines(cur, conv_id: str, seqs: list[int], *, ai_label: str,
                     max_chars: int, skip_diff: bool,
                     min_prose_len: int = DEFAULT_MIN_PROSE_LEN) -> list[str]:
    """'[USER]/[<ai_label>] <prose>' lines for the given seqs (content fetched
    only for those seqs)."""
    if not seqs:
        return []
    cur.execute("""
        SELECT role, content FROM personal.messages
        WHERE conv_id = %s AND seq = ANY(%s) AND content IS NOT NULL
        ORDER BY seq
    """, (conv_id, seqs))
    lines = []
    for role, content in cur.fetchall():
        if not content or content.strip().startswith('[tool_result'):
            continue
        prose = extract_prose(content, max_chars=max_chars, skip_diff=skip_diff)
        if len(prose) < min_prose_len:
            continue
        lines.append(f"[{'USER' if role == 'user' else ai_label}] {prose}")
    return lines
