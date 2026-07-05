"""Parse Grok CLI session logs into normalized messages.

Grok stores sessions under ~/.grok/sessions/<url-encoded-cwd>/<session-id>/:
  summary.json         -> cwd, model, timestamps, title
  chat_history.jsonl   -> turn-level messages

chat_history.jsonl entry types used here:
  user                  -> extract <user_query>…</user_query> when present
  assistant             -> plain text content (tool_calls-only rows skipped)
  system / reasoning / tool_result -> skipped
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub

CHAT_HISTORY = "chat_history.jsonl"
SUMMARY_NAME = "summary.json"
USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_text_from_content(content) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(p for p in parts if p).strip()
        return text or None
    return None


def _extract_user_text(entry: dict) -> str | None:
    if entry.get("type") != "user":
        return None
    text = _extract_text_from_content(entry.get("content"))
    if not text:
        return None
    m = USER_QUERY_RE.search(text)
    if not m:
        return None
    return (m.group(1) or "").strip() or None


def _extract_assistant_text(entry: dict) -> str | None:
    if entry.get("type") != "assistant":
        return None
    text = (entry.get("content") or "").strip()
    return text or None


def _parse_chat_history(
    chat_path: Path,
    session_id: str,
    base_time: datetime | None,
    end_time: datetime | None,
) -> tuple[list[dict], datetime | None, datetime | None, str]:
    """Parse chat_history.jsonl into normalized message rows.

    NOTE: grok's chat_history.jsonl entries carry NO per-message timestamp, so
    per-message `ts` is SYNTHETIC — linearly interpolated between summary
    created_at (base_time) and updated_at (end_time). It is strictly monotonic
    and bounded by [base_time, end_time], but is not a real event clock; do not
    treat sub-conversation ts deltas as accurate. (Kimi, by contrast, derives ts
    from a monotonic offset field and is more precise.)
    """
    msgs: list[dict] = []
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model = ""
    span = None
    if base_time and end_time and base_time < end_time:
        span = end_time - base_time

    with open(chat_path, encoding="utf-8") as f:
        raw_lines = [ln.strip() for ln in f if ln.strip()]

    entries: list[tuple[int, dict]] = []
    for seq, line in enumerate(raw_lines):
        try:
            entries.append((seq, json.loads(line)))
        except json.JSONDecodeError:
            continue

    msg_slots = sum(
        1 for _, entry in entries
        if _extract_user_text(entry) is not None
        or _extract_assistant_text(entry) is not None
    )
    slot = 0
    for seq, entry in entries:

        if entry.get("type") == "assistant" and entry.get("model_id"):
            model = entry["model_id"]

        user_text = _extract_user_text(entry)
        assistant_text = _extract_assistant_text(entry)
        if user_text is None and assistant_text is None:
            continue

        ts = None
        if base_time and span and msg_slots > 1:
            ts = base_time + span * (slot / max(msg_slots - 1, 1))
        elif base_time:
            ts = base_time
        slot += 1

        role = "user" if user_text is not None else "assistant"
        text = user_text if user_text is not None else assistant_text
        text = _scrub(text or "")
        if not text:
            continue

        msgs.append({
            "conv_id": "",
            "msg_id": f"{session_id}:{seq}",
            "role": role,
            "content": text,
            "content_type": "text",
            "ts": ts,
            "seq": seq,
        })
        if ts:
            if started_at is None:
                started_at = ts
            ended_at = ts

    return msgs, started_at, ended_at, model


def _parse_session(session_dir: Path) -> tuple[dict, list[dict]]:
    summary_path = session_dir / SUMMARY_NAME
    chat_path = session_dir / CHAT_HISTORY
    if not summary_path.exists() or not chat_path.exists():
        return {}, []

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, []

    info = summary.get("info") or {}
    session_id = info.get("id") or session_dir.name
    conv_id = f"grok:{session_id}"
    base_time = _ts(summary.get("created_at"))
    end_time = _ts(summary.get("updated_at") or summary.get("last_active_at"))

    msgs, started_at, ended_at, model = _parse_chat_history(
        chat_path, session_id, base_time, end_time,
    )
    if not msgs:
        return {}, []

    for m in msgs:
        m["conv_id"] = conv_id

    title = (
        summary.get("generated_title")
        or summary.get("session_summary")
        or session_id
    )[:500]
    model = model or summary.get("current_model_id") or ""

    conv_row = {
        "conv_id": conv_id,
        "platform": "grok",
        "title": title,
        "started_at": started_at or base_time,
        "ended_at": ended_at or end_time or started_at or base_time,
        "msg_count": len(msgs),
        "model": model,
    }
    return conv_row, msgs


def parse_session_dir(session_dir: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) for a Grok session directory."""
    conv, msgs = _parse_session(Path(session_dir))
    if msgs:
        yield conv, msgs


def parse_all_sessions(grok_dir: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield sessions from the Grok home directory."""
    root = Path(grok_dir)
    sessions_root = root / "sessions"
    if not sessions_root.exists():
        return
    for chat_path in sorted(sessions_root.rglob(CHAT_HISTORY)):
        yield from parse_session_dir(chat_path.parent)


if __name__ == "__main__":
    import sys

    total_conv = total_msg = 0
    for conv, msgs in parse_all_sessions(sys.argv[1] if len(sys.argv) > 1 else "~/.grok"):
        total_conv += 1
        total_msg += len(msgs)
    print(f"sessions: {total_conv}, messages: {total_msg}")
