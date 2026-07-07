"""Grok CLI adapter — incremental ingest from ~/.grok sessions.

Incremental contract: item-level diff against the DB in discover()
(new / grown / possibly-truncated / legacy-no-size classification).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterable, Iterator

from ...parsers.grok_session_parser import CHAT_HISTORY, parse_session_dir
from ..base import EmbedParams, IngestContext, SourceItem

GIT_TIMEOUT = 1.5

CONV_UPSERT_SQL = """
    INSERT INTO personal.conversations
        (conv_id, platform, title, started_at, ended_at, msg_count, model,
         last_file_size, project_slug)
    VALUES (%(conv_id)s, %(platform)s, %(title)s, %(started_at)s,
            %(ended_at)s, %(msg_count)s, %(model)s, %(last_file_size)s,
            %(project_slug)s)
    ON CONFLICT (conv_id) DO UPDATE SET
        msg_count      = EXCLUDED.msg_count,
        ended_at       = EXCLUDED.ended_at,
        last_file_size = EXCLUDED.last_file_size,
        project_slug   = COALESCE(personal.conversations.project_slug,
                                  EXCLUDED.project_slug)
"""


class GrokAdapter:
    name = "grok"
    platform = "grok"
    embed_params = EmbedParams(batch_size=64, max_length=512)
    scores = True
    conv_upsert_sql = CONV_UPSERT_SQL

    def __init__(self) -> None:
        self._git_cache: dict[str, str | None] = {}
        self._slug_cache: dict[tuple[str | None, str], str] = {}
        self._excluded_prefixes: list[str] | None = None

    def discover(self, ctx: IngestContext) -> Iterable[SourceItem]:
        grok_dir = Path(os.environ.get(
            "GROK_DIR", os.path.expanduser("~/.grok")))
        sessions_root = grok_dir / "sessions"
        if not sessions_root.exists():
            print(f"sessions dir not found: {sessions_root}", flush=True)
            return

        cur = ctx.conn.cursor()
        cur.execute("""
            SELECT conv_id, coalesce(last_file_size, 0), coalesce(msg_count, 0)
            FROM personal.conversations WHERE platform='grok'
        """)
        known = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        ctx.known = known
        print(f"known conv_ids: {len(known)}", flush=True)

        new_p, grown_p, trunc_p, legacy_p = [], [], [], []
        for chat_path in sorted(sessions_root.rglob(CHAT_HISTORY)):
            session_dir = chat_path.parent
            conv_id = f"grok:{session_dir.name}"
            item = SourceItem(
                path=chat_path,
                meta={"session_dir": session_dir},
            )
            if conv_id not in known:
                new_p.append(item)
            else:
                size, msg_count = known[conv_id]
                if size == 0:
                    legacy_p.append(item)
                elif chat_path.stat().st_size > size:
                    grown_p.append(item)
                elif msg_count > 0 and size / msg_count > 50000:
                    trunc_p.append(item)

        print(f"new: {len(new_p)} grown: {len(grown_p)} "
              f"truncated?: {len(trunc_p)} legacy: {len(legacy_p)}", flush=True)
        for item in new_p + grown_p + trunc_p + legacy_p:
            yield item

    def parse(self, item: SourceItem) -> Iterator[tuple[dict, list[dict]]]:
        session_dir = item.meta.get("session_dir")
        if not session_dir:
            return
        for conv, msgs in parse_session_dir(session_dir):
            if not msgs:
                return
            conv["last_file_size"] = item.path.stat().st_size
            conv["_cwd"] = self._cwd_from_summary(session_dir)
            yield conv, msgs

    def enrich(self, conv: dict, cur) -> dict:
        conv["project_slug"] = self._resolve_slug(cur, conv.pop("_cwd", None))
        return conv

    def should_ingest(self, conv: dict, ctx: IngestContext) -> bool:
        return True

    @staticmethod
    def _cwd_from_summary(session_dir: Path) -> str | None:
        summary_path = session_dir / "summary.json"
        if not summary_path.exists():
            return None
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            info = summary.get("info") or {}
            cwd = info.get("cwd")
            return cwd if isinstance(cwd, str) and cwd else None
        except Exception:
            return None

    def _load_excluded_prefixes(self, cur) -> list[str]:
        if self._excluded_prefixes is None:
            try:
                cur.execute("SELECT path_prefix "
                            "FROM personal.conversation_inject_excluded_paths")
                self._excluded_prefixes = [r[0] for r in cur.fetchall()]
            except Exception:
                self._excluded_prefixes = []
        return self._excluded_prefixes

    def _git_remote_url(self, cwd: str) -> str | None:
        if cwd in self._git_cache:
            return self._git_cache[cwd]
        result: str | None = None
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT,
            )
            if r.returncode == 0 and r.stdout.strip():
                result = r.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        self._git_cache[cwd] = result
        return result

    def _resolve_slug(self, cur, cwd: str | None) -> str:
        if not cwd:
            return "__unresolved__"
        for prefix in self._load_excluded_prefixes(cur):
            if cwd.startswith(prefix):
                return "__excluded__"
        remote_url = self._git_remote_url(cwd)
        basename = Path(cwd).name
        key = (remote_url, basename)
        if key not in self._slug_cache:
            try:
                cur.execute("SELECT personal.canonical_project_slug(%s, %s)",
                            (remote_url, basename))
                self._slug_cache[key] = cur.fetchone()[0]
            except Exception:
                self._slug_cache[key] = "__unresolved__"
        return self._slug_cache[key]
