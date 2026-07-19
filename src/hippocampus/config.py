"""Single configuration boundary for hippocampus (epic #43 Phase 1).

Precedence: process environment > .env in the current working directory
(python-dotenv, no override). The operator's sops wrappers inject process
env, so sops always wins over a stray .env.

Rules (plan §3.3):
- `Settings.load()` fails loudly on missing PG_URL — no half-configured boot.
- Values are never logged or echoed; error messages name the variable, not
  the value. DSNs must be redacted before appearing in any output.
- Semantic embedding is OFF unless explicitly configured: BGE_EMBED_URL
  (HTTP backend), EMBED_PROVIDER=bge-ondemand (local compose service started
  on first semantic use), or EMBED_PROVIDER=bge-inprocess (local model,
  requires the `bge-local` extra). There is no silent in-process fallback
  (r2-codex-2).

The full variable audit (core / feature-optional / personal-script-only)
lives in docs/CONFIG.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or contradictory."""


def redact_dsn(dsn: str) -> str:
    """Strip userinfo from a DSN/URL for safe display (r3-privacy-5)."""
    if "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if not rest:
        return "<redacted>"
    tail = rest.rsplit("@", 1)[-1]
    return f"{scheme}://***@{tail}"


# ── error-text redaction (shared by doctor + migrate; r3-privacy-5) ─────
# psycopg2/libpq error text can reproduce the DSN password in raw,
# %-decoded, or re-encoded form. Every CLI subcommand that prints a
# connection error routes through here so no secret reaches a terminal or
# a pasted bug report.

def secret_substrings(dsn: str) -> list[str]:
    """Every form of the DSN password that could appear in error text."""
    import urllib.parse

    out: list[str] = []
    try:
        parts = urllib.parse.urlsplit(dsn)
        if parts.password:
            out.append(parts.password)
            out.append(urllib.parse.unquote(parts.password))
            out.append(urllib.parse.quote(parts.password, safe=""))
            out.append(urllib.parse.quote(
                urllib.parse.unquote(parts.password), safe=""))
        if "@" in dsn and "://" in dsn:
            userinfo = dsn.split("://", 1)[1].rsplit("@", 1)[0]
            if ":" in userinfo:
                out.append(userinfo.split(":", 1)[1])
    except ValueError:
        pass
    # longest first so partial overlaps cannot resurrect a substring
    return sorted({s for s in out if s}, key=len, reverse=True)


def sanitize_error_text(text: str, *, dsns: "list[str]" = (),
                        extra_secrets: "list[str]" = ()) -> str:
    """Scrub passwords / full DSNs / extra secrets out of an error message."""
    for dsn in dsns:
        if dsn:
            text = text.replace(dsn, redact_dsn(dsn))
            for secret in secret_substrings(dsn):
                text = text.replace(secret, "***")
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def format_pg_error(dsn: str, exc: BaseException,
                    *, extra_secrets: "list[str]" = ()) -> str:
    """One-line, paste-safe failure description: redacted target +
    exception class + scrubbed first line of the error text."""
    detail = sanitize_error_text(str(exc), dsns=[dsn],
                                 extra_secrets=list(extra_secrets))
    first_line = detail.strip().splitlines()[0] if detail.strip() else ""
    msg = f"{redact_dsn(dsn)} — {type(exc).__name__}"
    if first_line:
        msg += f": {first_line}"
    return msg


@dataclass(frozen=True)
class Settings:
    pg_url: str
    pg_url_agent_read_mcp: str   # "" = ghost layer reader unconfigured
    bge_embed_url: str           # "" = no HTTP embed backend
    bge_embed_token: str
    embed_provider: str          # "" | "bge" | "bge-ondemand" | "bge-inprocess"
    mcp_sse_token: str
    fastmcp_host: str
    fastmcp_port: int
    calling_chassis: str
    debug_timing: bool
    eager_load: bool
    multiuser: bool
    tenant_id: str
    user_id: str
    team_ids: tuple[str, ...]
    role: str
    default_visibility: str

    @property
    def embed_configured(self) -> bool:
        """True when a semantic embed backend is explicitly available."""
        return bool(self.bge_embed_url) or self.embed_provider in (
            "bge-ondemand", "bge-inprocess")

    @classmethod
    def load(cls, *, require_pg: bool = True) -> "Settings":
        # Explicit cwd path: bare load_dotenv() resolves relative to the
        # CALLING FILE (site-packages for an installed copy), which silently
        # never finds the user's ./.env — caught by the Phase 3
        # clean-container e2e. The documented contract is "cwd .env,
        # process env wins"; this makes the code match it in both install
        # shapes (no parent-directory walk-up).
        load_dotenv(os.path.join(os.getcwd(), ".env"))
        pg_url = os.environ.get("PG_URL", "")
        if require_pg and not pg_url:
            raise ConfigError(
                "PG_URL is not set. Copy .env.example to .env and fill it in, "
                "or run under your secrets wrapper (sops exec-env ...)."
            )
        provider = os.environ.get("EMBED_PROVIDER", "").strip().lower()
        if provider not in ("", "bge", "bge-ondemand", "bge-inprocess"):
            raise ConfigError(
                f"EMBED_PROVIDER={provider!r} is not supported "
                "(expected: bge | bge-ondemand | bge-inprocess)"
            )
        multiuser = os.environ.get("HIPPOCAMPUS_MULTIUSER", "0") == "1"
        tenant_id = os.environ.get("HIPPOCAMPUS_TENANT_ID", "default").strip()
        user_id = os.environ.get("HIPPOCAMPUS_USER_ID", "default").strip()
        team_ids = tuple(
            team_id.strip()
            for team_id in os.environ.get("HIPPOCAMPUS_TEAM_IDS", "").split(",")
            if team_id.strip()
        )
        role = os.environ.get("HIPPOCAMPUS_ROLE", "user").strip()
        default_visibility = os.environ.get(
            "HIPPOCAMPUS_DEFAULT_VISIBILITY", "private"
        ).strip().lower()
        _validate_principal_settings(
            multiuser=multiuser,
            tenant_id=tenant_id,
            user_id=user_id,
            team_ids=team_ids,
            default_visibility=default_visibility,
        )
        return cls(
            pg_url=pg_url,
            pg_url_agent_read_mcp=os.environ.get("PG_URL_AGENT_READ_MCP", ""),
            bge_embed_url=os.environ.get("BGE_EMBED_URL", "").rstrip("/"),
            bge_embed_token=os.environ.get("BGE_EMBED_TOKEN", ""),
            embed_provider=provider,
            mcp_sse_token=os.environ.get("MCP_SSE_TOKEN", ""),
            fastmcp_host=os.environ.get("FASTMCP_HOST", "127.0.0.1"),
            fastmcp_port=int(os.environ.get("FASTMCP_PORT", "8091")),
            calling_chassis=os.environ.get("CALLING_CHASSIS", ""),
            debug_timing=os.environ.get("HIPPOCAMPUS_DEBUG_TIMING", "0") == "1",
            eager_load=os.environ.get("HIPPOCAMPUS_EAGER_LOAD", "1") != "0",
            multiuser=multiuser,
            tenant_id=tenant_id,
            user_id=user_id,
            team_ids=team_ids,
            role=role,
            default_visibility=default_visibility,
        )


def _validate_principal_settings(*, multiuser: bool, tenant_id: str,
                                 user_id: str, team_ids: tuple[str, ...],
                                 default_visibility: str) -> None:
    if default_visibility not in ("private", "team", "org"):
        raise ConfigError(
            "HIPPOCAMPUS_DEFAULT_VISIBILITY must be one of: private, team, org"
        )
    if default_visibility == "team" and not team_ids:
        raise ConfigError(
            "HIPPOCAMPUS_DEFAULT_VISIBILITY=team requires at least one "
            "HIPPOCAMPUS_TEAM_IDS entry"
        )
    if multiuser and not tenant_id:
        raise ConfigError(
            "HIPPOCAMPUS_TENANT_ID must be non-empty when "
            "HIPPOCAMPUS_MULTIUSER=1"
        )
    if multiuser and not user_id:
        raise ConfigError(
            "HIPPOCAMPUS_USER_ID must be non-empty when "
            "HIPPOCAMPUS_MULTIUSER=1"
        )


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    team_ids: tuple[str, ...]
    role: str
    default_visibility: str
    multiuser: bool


def current_principal(settings: Settings) -> Principal:
    """Return the process principal parsed by ``Settings.load()``."""
    _validate_principal_settings(
        multiuser=settings.multiuser,
        tenant_id=settings.tenant_id,
        user_id=settings.user_id,
        team_ids=settings.team_ids,
        default_visibility=settings.default_visibility,
    )
    return Principal(
        tenant_id=settings.tenant_id,
        user_id=settings.user_id,
        team_ids=settings.team_ids,
        role=settings.role,
        default_visibility=settings.default_visibility,
        multiuser=settings.multiuser,
    )
