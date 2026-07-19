"""Parse claude.ai (Anthropic web) conversation export into normalized messages.

Export format (from Anthropic data export ZIP):
  conversations.json: list of conversations with chat_messages[]
  Each chat_message: {uuid, text, content[], sender, created_at}

Schema differs from ChatGPT (chatgpt_parser.py) and Claude Code (claude_session_parser.py).
"""
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except Exception:
        return None


def _extract_text(msg: dict) -> str | None:
    """Extract text from a claude.ai chat_message entry.

    The entry may have a top-level 'text' field, or a 'content' list of blocks.
    Prefer 'text' when both are present (it is typically the rendered final text).
    """
    text = msg.get('text', '')
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = msg.get('content', [])
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get('type')
            if t == 'text':
                parts.append(block.get('text', ''))
            elif t == 'tool_use':
                tname = block.get('name', '')
                parts.append(f'[tool_use:{tname}]')
            elif t == 'tool_result':
                parts.append('[tool_result]')
        joined = '\n'.join(p for p in parts if p).strip()
        if joined:
            return joined

    return None


def parse_zip(zip_path: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) for each conversation."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open('conversations.json') as f:
            convs = json.load(f)
    for c in convs:
        yield _parse_conv(c)


def parse_dir(dir_path: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) from an unzipped export directory."""
    p = Path(dir_path)
    convs_file = p / 'conversations.json'
    if not convs_file.exists():
        return
    convs = json.loads(convs_file.read_text(encoding='utf-8'))
    for c in convs:
        yield _parse_conv(c)


def _parse_conv(c: dict) -> tuple[dict, list[dict]]:
    conv_id = f"claude_ai:{c.get('uuid', '')}"
    title = (c.get('name') or '')[:500]
    create_time = _ts(c.get('created_at'))
    update_time = _ts(c.get('updated_at'))

    msgs = []
    chat_messages = c.get('chat_messages', [])
    for seq, m in enumerate(chat_messages):
        sender = m.get('sender', '')
        # claude.ai schema: sender is "human" or "assistant"
        if sender == 'human':
            role = 'user'
        elif sender == 'assistant':
            role = 'assistant'
        else:
            continue

        text = _extract_text(m)
        if not text:
            continue
        text = _scrub(text)
        if text is None:
            continue

        msgs.append({
            'conv_id': conv_id,
            'msg_id': m.get('uuid', f'{conv_id}:{seq}'),
            'role': role,
            'content': text,
            'content_type': 'text',
            'ts': _ts(m.get('created_at')),
            'seq': seq,
        })

    conv_row = {
        'conv_id': conv_id,
        'platform': 'claude_ai',
        'title': title,
        'started_at': create_time,
        'ended_at': update_time,
        'msg_count': len(msgs),
        'model': '',  # not exposed in this export schema
    }
    return conv_row, msgs


if __name__ == '__main__':
    import sys
    src = sys.argv[1]
    total_conv = total_msg = 0
    if src.endswith('.zip'):
        gen = parse_zip(src)
    else:
        gen = parse_dir(src)
    for conv, msgs in gen:
        total_conv += 1
        total_msg += len(msgs)
    print(f'conversations: {total_conv}, messages: {total_msg}')
