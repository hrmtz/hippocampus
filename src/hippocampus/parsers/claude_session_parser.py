"""Parse Claude Code session jsonl files into normalized messages."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub


# Phase 2 (#14): cwd extraction for project_slug resolution.
# Scan caps prevent unbounded I/O on malformed jsonls + ReDoS-style edge cases.
MAX_HEADER_LINES = 50
MAX_HEADER_BYTES = 256 * 1024
CWD_BEARING_TYPES = frozenset(('user', 'attachment', 'system'))
# Strip C0 control chars (0x00-0x1F) + DEL (0x7F). Keep multi-byte UTF-8 intact.
_CONTROL_STRIP_TABLE = str.maketrans('', '', ''.join(chr(c) for c in range(0x20)) + '\x7f')


def extract_cwd_from_jsonl(path: Path) -> str | None:
    """Scan jsonl header for the first cwd field on a user/attachment/system entry.

    Returns:
        Cleaned cwd string (NUL/control chars stripped, surrounding whitespace
        trimmed) or None when no cwd is found within the scan caps.

    Caps:
        MAX_HEADER_LINES lines OR MAX_HEADER_BYTES bytes, whichever hits first.
        PG TEXT rejects 0x00 — strip at bytes layer before decode.
    """
    bytes_read = 0
    try:
        with open(path, 'rb') as f:
            for lineno, raw in enumerate(f):
                if lineno >= MAX_HEADER_LINES:
                    return None
                bytes_read += len(raw)
                if bytes_read > MAX_HEADER_BYTES:
                    return None
                raw = raw.replace(b'\x00', b'')
                try:
                    entry = json.loads(raw.decode('utf-8', errors='replace'))
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get('type') not in CWD_BEARING_TYPES:
                    continue
                cwd = entry.get('cwd')
                if not isinstance(cwd, str) or not cwd:
                    continue
                cleaned = cwd.translate(_CONTROL_STRIP_TABLE).strip()
                if cleaned:
                    return cleaned
    except OSError:
        pass
    return None


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except Exception:
        return None


def _extract_text(msg: dict) -> str | None:
    """Extract text from Claude Code session jsonl message entry."""
    # Format: {"type": "user"|"assistant", "message": {...}, "timestamp": "..."}
    role = msg.get('type', '')
    inner = msg.get('message', {})
    if not inner:
        return None

    content = inner.get('content', '')
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(block.get('text', ''))
                elif block.get('type') == 'tool_result':
                    # summarize tool results as metadata only
                    tool_name = block.get('tool_use_id', '')
                    parts.append(f'[tool_result:{tool_name}]')
            elif isinstance(block, str):
                parts.append(block)
        text = '\n'.join(p for p in parts if p).strip()
        return text or None
    return None


def parse_session_dir(sessions_root: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) for each .jsonl session file."""
    root = Path(sessions_root)
    for jsonl_path in sorted(root.rglob('*.jsonl')):
        if 'subagents' in jsonl_path.parts:
            continue
        yield _parse_session(jsonl_path)


def _parse_session(path: Path) -> tuple[dict, list[dict]]:
    conv_id = f'claude_code:{path.stem}'
    # project dir from path: .claude/projects/<proj>/<session>.jsonl
    parts = path.parts
    proj_name = ''
    for i, p in enumerate(parts):
        if p == 'projects' and i + 2 < len(parts):
            proj_name = parts[i + 1].lstrip('-').replace('-', '/', 2)
            break

    msgs = []
    started_at = ended_at = None

    with open(path, encoding='utf-8') as f:
        for seq, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            try:
                role = entry.get('type', '')
                if role not in ('user', 'assistant'):
                    continue

                text = _extract_text(entry)
                if not text:
                    continue
                text = _scrub(text)
                if text is None:
                    continue

                ts = _ts(entry.get('timestamp'))
                if ts:
                    if started_at is None:
                        started_at = ts
                    ended_at = ts

                msgs.append({
                    'conv_id': conv_id,
                    'msg_id': f'{path.stem}:{seq}',
                    'role': 'user' if role == 'user' else 'assistant',
                    'content': text,
                    'content_type': 'text',
                    'ts': ts,
                    'seq': seq,
                })
            except Exception:
                continue

    conv_row = {
        'conv_id': conv_id,
        'platform': 'claude_code',
        'title': proj_name or path.stem,
        'started_at': started_at,
        'ended_at': ended_at,
        'msg_count': len(msgs),
        'model': '',
    }
    return conv_row, msgs


if __name__ == '__main__':
    import sys
    total_conv = total_msg = 0
    for conv, msgs in parse_session_dir(sys.argv[1]):
        total_conv += 1
        total_msg += len(msgs)
    print(f'sessions: {total_conv}, messages: {total_msg}')
