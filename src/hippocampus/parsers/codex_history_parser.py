"""Parse Codex history.jsonl into normalized conversations/messages."""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub


def _ts(epoch: int | float | None) -> datetime | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except Exception:
        return None


def _title_from_entries(entries: list[dict]) -> str:
    for entry in entries:
        text = (entry.get("text") or "").strip()
        if text and text not in {"exit", "quit"}:
            title = text.replace("\n", " ").strip()
            return title[:80]
    return "Codex history"


def parse_history_file(history_path: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) grouped by session_id.

    Codex currently persists a compact history stream at `~/.codex/history.jsonl`.
    Each row carries `session_id`, `ts`, and `text`. We treat each session_id as
    one conversation and preserve the raw chronological order within it.
    """
    path = Path(history_path)
    by_session: dict[str, list[dict]] = defaultdict(list)

    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            session_id = str(row.get("session_id") or "").strip()
            text = row.get("text")
            if not session_id or not isinstance(text, str):
                continue
            by_session[session_id].append(row)

    def _sort_key(item: tuple[str, list[dict]]) -> tuple[int, str]:
        session_id, entries = item
        first_ts = next((r.get("ts") for r in entries if r.get("ts") is not None), 0)
        return int(first_ts), session_id

    for session_id, entries in sorted(by_session.items(), key=_sort_key):
        entries = sorted(entries, key=lambda r: (r.get("ts") or 0, r.get("text") or ""))
        started_at = _ts(entries[0].get("ts"))
        ended_at = _ts(entries[-1].get("ts"))
        conv_id = f"codex:{session_id}"
        conv_row = {
            "conv_id": conv_id,
            "platform": "codex",
            "title": _title_from_entries(entries),
            "started_at": started_at,
            "ended_at": ended_at,
            "msg_count": len(entries),
            "model": "",
        }

        msgs = []
        for seq, row in enumerate(entries):
            text = _scrub((row.get("text") or "").strip())
            if not text or text in {"exit", "quit"}:
                continue
            ts = _ts(row.get("ts"))
            msgs.append({
                "conv_id": conv_id,
                "msg_id": f"{session_id}:{seq}",
                "role": "user",
                "content": text,
                "content_type": "text",
                "ts": ts,
                "seq": seq,
            })

        if msgs:
            conv_row["msg_count"] = len(msgs)
            yield conv_row, msgs


if __name__ == "__main__":
    import sys

    total_conv = total_msg = 0
    for conv, msgs in parse_history_file(sys.argv[1]):
        total_conv += 1
        total_msg += len(msgs)
    print(f"sessions: {total_conv}, messages: {total_msg}")
