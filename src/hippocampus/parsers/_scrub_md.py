"""Cross-call-site sanitization for text that will be rendered into LLM context.

Strips markdown images, html <img>, ANSI escape sequences, then applies
credential scrub. Single boundary used by server.search_* tools AND by
the SessionStart hook ghost_context_inject.py (= Phase D).

Why this lives in parsers/:
- server.py was the original home of `_sanitize_for_mcp`, but the
  SessionStart hook also needs the same sanitization on rendered ghost
  memory bodies (= Phase D §7). Keeping the helper here makes it
  importable from both call sites without circular dependency.
- `parsers._scrub.scrub_text` is the 12-pattern credential redact-in-place
  layer (= sk-ant / AIza / GitHub PAT / AWS / JWT / bearer / URL-embedded
  creds / password= / api_key= etc).

Markdown images would render in some MCP clients and trigger an HTTP
GET to attacker-controlled URLs (= data exfil via URL params). ANSI
escapes can hide / reorder content in terminals. Both are stripped
before credential scrub (= scrub is the final pass).
"""
from __future__ import annotations

import re

from ._scrub import scrub_text as _credential_scrub

_MARKDOWN_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ANSI_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def sanitize_for_mcp(text: str) -> str:
    """Strip markdown image / html img / ANSI escape, then credential-scrub.

    Returns the cleaned text. **Returns the input unchanged when it is
    falsy** (= None passes through as None, "" as ""). Callers that need a
    guaranteed string should wrap with `(... or "")`.

    Order matters: image/escape strip first (= reduces work for scrub),
    credential scrub last (= catches anything left).
    """
    if not text:
        return text
    text = _MARKDOWN_IMG.sub(r"[image-stripped:\1]", text)
    text = _HTML_IMG.sub("[img-stripped]", text)
    text = _ANSI_ESC.sub("", text)
    text = _credential_scrub(text) or ""
    return text


__all__ = ["sanitize_for_mcp"]
