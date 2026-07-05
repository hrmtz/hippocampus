"""Parse ChatGPT sharded export (conversations-NNN.json) into normalized messages."""
import json
import zipfile
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ._scrub import scrub_text as _scrub


def _ts(epoch: float | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _extract_text(content: dict) -> str | None:
    ct = content.get('content_type', 'text')
    if ct == 'text':
        parts = content.get('parts', [])
        text = ' '.join(p for p in parts if isinstance(p, str))
        return text.strip() or None
    if ct == 'code':
        return content.get('text', '').strip() or None
    if ct == 'tether_quote':
        return content.get('text', '').strip() or None
    return None


def parse_zip(zip_path: str | Path) -> Iterator[tuple[dict, list[dict]]]:
    """Yield (conversation_row, [message_rows]) for each conversation."""
    with zipfile.ZipFile(zip_path) as zf:
        shards = sorted(n for n in zf.namelist() if re.match(r'conversations-\d+\.json', n))
        for shard in shards:
            with zf.open(shard) as f:
                convs = json.load(f)
            for c in convs:
                yield _parse_conv(c)


def _parse_conv(c: dict) -> tuple[dict, list[dict]]:
    conv_id = c.get('conversation_id') or c.get('id', '')
    title = c.get('title') or ''
    model = c.get('default_model_slug') or ''
    create_time = _ts(c.get('create_time'))
    update_time = _ts(c.get('update_time'))

    mapping = c.get('mapping', {})
    msgs = []
    for seq, (node_id, node) in enumerate(mapping.items()):
        raw_msg = node.get('message')
        if not raw_msg:
            continue
        role = raw_msg.get('author', {}).get('role', '')
        if role not in ('user', 'assistant', 'system', 'tool'):
            continue
        content = raw_msg.get('content') or {}
        text = _extract_text(content)
        if not text:
            continue
        text = _scrub(text)
        if text is None:
            continue
        msgs.append({
            'conv_id': conv_id,
            'msg_id': raw_msg.get('id', node_id),
            'role': role,
            'content': text,
            'content_type': content.get('content_type', 'text'),
            'ts': _ts(raw_msg.get('create_time')),
            'seq': seq,
        })

    conv_row = {
        'conv_id': conv_id,
        'platform': 'chatgpt',
        'title': title[:500],
        'started_at': create_time,
        'ended_at': update_time,
        'msg_count': len(msgs),
        'model': model[:100],
    }
    return conv_row, msgs


if __name__ == '__main__':
    import sys
    total_conv = total_msg = scrubbed = 0
    for conv, msgs in parse_zip(sys.argv[1]):
        total_conv += 1
        total_msg += len(msgs)
    print(f'conversations: {total_conv}, messages: {total_msg}, scrubbed: {scrubbed}')
