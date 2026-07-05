"""Parse Kimi Code CLI session logs into normalized messages.

Kimi stores sessions under ~/.kimi-code/:
  session_index.jsonl              -> maps sessionId -> sessionDir + workDir
  sessions/<hash>/<sessionId>/
    state.json                     -> title, createdAt, updatedAt, lastPrompt
    agents/main/wire.jsonl         -> turn-level events

wire.jsonl event types used here:
  context.append_message          -> user messages (role == "user")
  context.append_loop_event       -> assistant streaming events
    event.type == "content.part"
    part.type == "text"           -> assistant text
    part.type == "think"          -> internal reasoning (skipped)
    event.type == "tool.call"     -> skipped
    event.type == "tool.result"   -> skipped
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub


WIRE_PATH = "agents/main/wire.jsonl"
STATE_NAME = "state.json"


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_text_from_content(content) -> str | None:
    """Extract plain text from a Kimi message content field."""
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
    """Return user text from a context.append_message event, or None."""
    if entry.get("type") != "context.append_message":
        return None
    msg = entry.get("message") or {}
    if msg.get("role") != "user":
        return None
    return _extract_text_from_content(msg.get("content"))


def _extract_assistant_text(entry: dict) -> str | None:
    """Return assistant text from a content.part event, or None."""
    if entry.get("type") != "context.append_loop_event":
        return None
    event = entry.get("event") or {}
    if event.get("type") != "content.part":
        return None
    part = event.get("part") or {}
    if part.get("type") != "text":
        return None
    return (part.get("text") or "").strip() or None


def _part_key(entry: dict) -> tuple[str | None, str | None]:
    """Grouping key for assistant text parts (turnId + stepUuid)."""
    event = (entry.get("event") or {})
    return event.get("turnId"), event.get("stepUuid")


def _event_ts(entry: dict, base_time: datetime | None, anchor: int | None) -> datetime | None:
    """Derive an absolute timestamp from the monotonic time field."""
    if base_time is None:
        return None
    event_time = entry.get("time")
    if event_time is not None and anchor is not None:
        try:
            offset_ms = int(event_time) - int(anchor)
            return base_time + timedelta(milliseconds=offset_ms)
        except Exception:
            pass
    return base_time


def _parse_wire_jsonl(
    wire_path: Path,
    session_id: str,
    base_time: datetime | None,
) -> tuple[list[dict], datetime | None, datetime | None]:
    """Parse wire.jsonl into normalized message rows.

    User messages come from context.append_message. Assistant responses are
    aggregated from consecutive content.part text events belonging to the same
    turn/step, so a streamed multi-chunk reply becomes a single message.

    Per-message timestamps are derived from state.createdAt plus the monotonic
    offset between the metadata.created_at anchor and each event's time field.
    If the anchor is missing we fall back to base_time for every message.
    """
    msgs: list[dict] = []
    started_at: datetime | None = None
    ended_at: datetime | None = None
    metadata_anchor: int | None = None

    pending_parts: list[str] = []
    pending_key: tuple[str | None, str | None] = (None, None)
    pending_seq: int = 0
    pending_ts: datetime | None = None
    pending_active: bool = False

    def flush_pending() -> None:
        nonlocal pending_parts, pending_key, pending_seq, pending_ts, pending_active
        pending_active = False
        if not pending_parts:
            pending_key = (None, None)
            return
        text = _scrub("\n".join(pending_parts).strip())
        pending_parts = []
        if not text:
            pending_key = (None, None)
            return
        msgs.append({
            "conv_id": "",  # filled by caller
            "msg_id": f"{session_id}:{pending_seq}",
            "role": "assistant",
            "content": text,
            "content_type": "text",
            "ts": pending_ts,
            "seq": pending_seq,
        })
        pending_key = (None, None)

    def emit_user(seq: int, text: str, ts: datetime | None) -> None:
        text = _scrub(text)
        if not text:
            return
        msgs.append({
            "conv_id": "",  # filled by caller
            "msg_id": f"{session_id}:{seq}",
            "role": "user",
            "content": text,
            "content_type": "text",
            "ts": ts,
            "seq": seq,
        })

    with open(wire_path, encoding="utf-8") as f:
        for seq, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") == "metadata":
                metadata_anchor = entry.get("created_at")
                continue

            user_text = _extract_user_text(entry)
            assistant_text = _extract_assistant_text(entry)

            if user_text is not None:
                flush_pending()
                ts = _event_ts(entry, base_time, metadata_anchor)
                emit_user(seq, user_text, ts)
                if ts:
                    if started_at is None:
                        started_at = ts
                    ended_at = ts
                continue

            if assistant_text is not None:
                key = _part_key(entry)
                ts = _event_ts(entry, base_time, metadata_anchor)
                if not pending_active or key != pending_key:
                    flush_pending()
                    pending_key = key
                    pending_seq = seq
                    pending_ts = ts
                    pending_active = True
                pending_parts.append(assistant_text)
                if ts:
                    if started_at is None:
                        started_at = ts
                    ended_at = ts
                continue

            # Other events (tool.call, tool.result, step.end, etc.) don't
            # carry text, but a step.end with the current key could flush.
            # We keep it simple: flush only on key change or user message.

        flush_pending()

    return msgs, started_at, ended_at


def _parse_session(session_dir: Path) -> tuple[dict, list[dict]]:
    """Parse a single Kimi session directory."""
    state_path = session_dir / STATE_NAME
    wire_path = session_dir / WIRE_PATH

    if not state_path.exists() or not wire_path.exists():
        return {}, []

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, []

    session_id = session_dir.name
    conv_id = f"kimi:{session_id}"
    base_time = _ts(state.get("createdAt"))

    msgs, started_at, ended_at = _parse_wire_jsonl(
        wire_path, session_id, base_time
    )
    if not msgs:
        return {}, []

    for m in msgs:
        m["conv_id"] = conv_id

    title = (state.get("title") or state.get("lastPrompt") or session_id)[:500]

    conv_row = {
        "conv_id": conv_id,
        "platform": "kimi",
        "title": title,
        "started_at": started_at or base_time,
        "ended_at": ended_at or _ts(state.get("updatedAt")) or started_at or base_time,
        "msg_count": len(msgs),
        "model": "",
    }
    return conv_row, msgs


def parse_session_dir(session_dir: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) for a Kimi session directory."""
    conv, msgs = _parse_session(Path(session_dir))
    if msgs:
        yield conv, msgs


def parse_all_sessions(kimi_dir: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield sessions from the Kimi home directory via session_index.jsonl.

    Falls back to scanning the sessions/ tree when the index is missing.
    """
    root = Path(kimi_dir)
    index_path = root / "session_index.jsonl"

    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    session_dir = Path(entry.get("sessionDir", ""))
                    if session_dir.exists():
                        yield from parse_session_dir(session_dir)
                except Exception:
                    continue
        return

    for wire_path in sorted(root.rglob(WIRE_PATH)):
        if "subagents" in wire_path.parts:
            continue
        yield from parse_session_dir(wire_path.parent.parent.parent)


if __name__ == "__main__":
    import sys

    total_conv = total_msg = 0
    for conv, msgs in parse_all_sessions(sys.argv[1]):
        total_conv += 1
        total_msg += len(msgs)
    print(f"sessions: {total_conv}, messages: {total_msg}")
