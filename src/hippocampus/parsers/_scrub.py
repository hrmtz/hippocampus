"""Shared credential scrubber for all parsers.

Behavior change vs prior per-parser CREDENTIAL_PATTERNS lists:
  - Was: scan text → if any match, return None (drop entire message).
  - Now: redact each match in place with a typed marker, return the redacted
    text. Dropping the whole message lost too much context (a long script with
    one stray key would vanish entirely). Matches are replaced with the marker
    `[REDACTED:<kind>]` so downstream readers can see the structure.

Used by every parser; also re-exposed via `scrub_text()` for the post-hoc
defense-in-depth layer in server.py and scripts/audit_credentials.py.
"""
from __future__ import annotations

import re

# (kind, compiled-pattern). Order matters only when patterns overlap; the more
# specific prefix wins because we substitute as we iterate.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # — Anthropic / OpenAI / Google / GitHub / AWS — high-confidence prefixes
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("openai-proj-key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("google-key", re.compile(r"AIza[A-Za-z0-9\-_]{35}")),
    ("github-pat", re.compile(r"gh[posu]_[A-Za-z0-9]{36,}")),
    ("aws-akid", re.compile(r"AKIA[A-Z0-9]{16}")),
    # Discord webhook: preserve URL prefix (channel id is not secret), redact
    # only the trailing token. Group 1 captures the prefix; the token portion
    # after the last `/` is replaced.
    ("discord-webhook", re.compile(r"(https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/)(?!\[REDACTED)[A-Za-z0-9_\-]{40,}")),
    # — Generic credentials —
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9\-_]{20,}\.[A-Za-z0-9\-_]{20,}\.[A-Za-z0-9\-_]{20,}")),
    ("bearer-token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    # URL-embedded credentials. The `\1` capture preserves the scheme+user so
    # readers still see it was a URL, just without the password. Negative
    # lookahead `(?!\[REDACTED)` makes scrub idempotent — re-scanning already
    # scrubbed text won't re-match the marker.
    ("url-creds", re.compile(r"(?i)(https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|ftp)://([^:/\s@]+):(?!\[REDACTED)([^@\s]+)@")),
    # Inline assignments
    ("password-assign", re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*['\"]?(?!\[REDACTED)([^\s'\"]{6,})")),
    ("api-key-assign", re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[=:]\s*['\"]?(?!\[REDACTED)([A-Za-z0-9_\-]{16,})")),
]


def scrub_text(text: str | None) -> str | None:
    """Redact credentials in place. Returns redacted text (or None for None/empty)."""
    if not text:
        return text
    for kind, pat in _PATTERNS:
        if kind == "url-creds":
            text = pat.sub(lambda m: f"{m.group(1)}://{m.group(2)}:[REDACTED:{kind}]@", text)
        elif kind == "discord-webhook":
            text = pat.sub(lambda m: f"{m.group(1)}[REDACTED:{kind}]", text)
        elif kind == "password-assign":
            text = pat.sub(lambda m: f"{m.group(1)}=[REDACTED:{kind}]", text)
        elif kind == "api-key-assign":
            text = pat.sub(lambda m: f"{m.group(1)}=[REDACTED:{kind}]", text)
        else:
            text = pat.sub(f"[REDACTED:{kind}]", text)
    return text


def has_credential(text: str | None) -> bool:
    """Cheap pre-check (used by audit script)."""
    if not text:
        return False
    return any(pat.search(text) for _, pat in _PATTERNS)


# Backwards-compatible alias for parsers that still call `_scrub`.
# Old semantic: return None if credential found.  New semantic: redact in place.
def _scrub(text: str | None) -> str | None:
    return scrub_text(text)
