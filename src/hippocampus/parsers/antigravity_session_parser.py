"""Parse Antigravity session jsonl files into normalized messages."""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub


MAX_HEADER_LINES = 50
MAX_HEADER_BYTES = 256 * 1024
_CONTROL_STRIP_TABLE = str.maketrans('', '', ''.join(chr(c) for c in range(0x20)) + '\x7f')


def extract_cwd_from_jsonl(path: Path) -> str | None:
    """Scan jsonl header for path-bearing tool call args to resolve cwd."""
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
                tool_calls = entry.get('tool_calls')
                if not isinstance(tool_calls, list):
                    continue
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    args = tc.get('args')
                    if not isinstance(args, dict):
                        continue
                    
                    path_val = None
                    is_file = False
                    for key in ('Cwd', 'DirectoryPath', 'SearchPath'):
                        if key in args and isinstance(args[key], str) and args[key]:
                            path_val = args[key]
                            break
                    if not path_val:
                        for key in ('AbsolutePath', 'TargetFile'):
                            if key in args and isinstance(args[key], str) and args[key]:
                                path_val = args[key]
                                is_file = True
                                break
                    if path_val:
                        path_val = path_val.strip()
                        if path_val.startswith('"') and path_val.endswith('"'):
                            path_val = path_val[1:-1].strip()
                        cleaned = path_val.translate(_CONTROL_STRIP_TABLE).strip()
                        if cleaned:
                            if is_file:
                                try:
                                    cleaned = str(Path(cleaned).parent)
                                except Exception:
                                    pass
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


def _extract_text(entry: dict) -> str | None:
    """Extract text from Antigravity session jsonl message entry."""
    content = entry.get('content')
    if not content:
        return None
    if isinstance(content, str):
        t = content.strip()
        if entry.get('type') == 'USER_INPUT':
            match = re.search(r'<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>', t, re.DOTALL)
            if match:
                t = match.group(1).strip()
        return t or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(block.get('text', ''))
                elif block.get('type') == 'tool_result':
                    tool_name = block.get('tool_use_id', '')
                    parts.append(f'[tool_result:{tool_name}]')
            elif isinstance(block, str):
                parts.append(block)
        text = '\n'.join(p for p in parts if p).strip()
        if entry.get('type') == 'USER_INPUT':
            match = re.search(r'<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>', text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        return text or None
    return None


def parse_session_dir(sessions_root: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) for each transcript.jsonl file."""
    root = Path(sessions_root)
    for jsonl_path in sorted(root.rglob('transcript.jsonl')):
        if 'subagents' in jsonl_path.parts:
            continue
        yield _parse_session(jsonl_path)


def _parse_session(path: Path) -> tuple[dict, list[dict]]:
    # Path format: ~/.gemini/antigravity-cli/brain/<session_id>/.system_generated/logs/transcript.jsonl
    session_id = path.parts[-4] if len(path.parts) >= 4 else path.parent.parent.name
    conv_id = f'antigravity:{session_id}'

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
                entry_type = entry.get('type', '')
                if entry_type not in ('USER_INPUT', 'PLANNER_RESPONSE'):
                    continue

                text = _extract_text(entry)
                if not text:
                    continue
                text = _scrub(text)
                if text is None:
                    continue

                ts = _ts(entry.get('created_at'))
                if ts:
                    if started_at is None:
                        started_at = ts
                    ended_at = ts

                msgs.append({
                    'conv_id': conv_id,
                    'msg_id': f'{session_id}:{seq}',
                    'role': 'user' if entry_type == 'USER_INPUT' else 'assistant',
                    'content': text,
                    'content_type': 'text',
                    'ts': ts,
                    'seq': seq,
                })
            except Exception:
                continue

    conv_row = {
        'conv_id': conv_id,
        'platform': 'antigravity',
        'title': session_id,
        'started_at': started_at,
        'ended_at': ended_at,
        'msg_count': len(msgs),
        'model': '',
    }
    return conv_row, msgs
