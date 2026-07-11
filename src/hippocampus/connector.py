"""claude.ai connector transport — streamable HTTP entrypoint.

Separate entrypoint from server.py (stdio) and sse.py (legacy Bearer) so the
public OAuth-gated surface never mutates the stdio tool set (design C5).

Two invariants live here, both enforced at boot (design C2):

1. **fail-closed tool allowlist** — the connector exposes ONLY the tools named
   in CONNECTOR_TOOL_ALLOWLIST. Anything else (a future tool, or the
   deliberately-excluded ghost / facts / full-thread get_conversation) is
   removed from the surface. Removal is by set difference, so a tool added to
   server.py later is invisible here until it is explicitly allowlisted.
2. **trust-tier / exfil exclusion** — ghost (authorized tier) and search_facts
   stay on stdio but never reach a public cloud endpoint; get_conversation
   (full-thread retrieval) is excluded by default as an exfiltration
   side-channel (design §5, r1-codex-4).

S1 scope: localhost, no auth. OAuth (S2) wraps this app; it does not change the
tool surface.
"""
from __future__ import annotations

import os
import sys

from .server import _gate_tools, mcp

# Fail-closed: the ONLY tools the connector serves. Read-only, personal /
# conversation / library tier. Excludes by omission:
#   - get_conversation            (full-thread read = exfil side-channel)
#   - search_ghost_memory         (authorized tier, not for public cloud)
#   - search_facts                (authorized tier)
CONNECTOR_TOOL_ALLOWLIST = frozenset({
    "search_personal_memory",
    "search_conversations",
    "search_library",
    "list_recent_conversations",
    "list_project_conversations",
    "get_conversation_summary",
    # diary layer — the agent's daily first-person reflections. Opt-in exposure
    # (operator decision 2026-07-10): the owner wants to read the diary from
    # claude.ai; it is OAuth/owner-gated. More sensitive than conversation
    # snippets, so this inclusion is deliberate, not default.
    "get_diary",
    "search_diary",
})

# Explicitly-excluded tools we assert absent in the S1 smoke. Kept as a named
# set so the test and the design contract cannot drift apart.
CONNECTOR_TOOL_DENY_ASSERT = frozenset({
    "get_conversation",
    "search_ghost_memory",
    "search_facts",
})

HOST = os.environ.get("HIPPOCAMPUS_CONNECTOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("HIPPOCAMPUS_CONNECTOR_PORT", "8092"))


def _registered_tool_names() -> set[str]:
    """Names of tools currently registered, read synchronously at boot.

    FastMCP.list_tools() is async but only reads the in-memory _tool_manager,
    whose own list_tools() is synchronous; reach it directly to avoid an event
    loop during gating.
    """
    return {t.name for t in mcp._tool_manager.list_tools()}


def _apply_connector_allowlist() -> tuple[list[str], list[str]]:
    """Remove every registered tool not in CONNECTOR_TOOL_ALLOWLIST.

    Runs AFTER _gate_tools() so capability-gated removals already happened; this
    is the second, stricter gate. Returns (kept, removed) for logging/tests.
    Fail-closed: a tool whose name is unknown to the allowlist is removed.
    """
    import contextlib

    kept, removed = [], []
    for name in sorted(_registered_tool_names()):
        if name in CONNECTOR_TOOL_ALLOWLIST:
            kept.append(name)
        else:
            with contextlib.suppress(Exception):
                mcp.remove_tool(name)
            removed.append(name)
    return kept, removed


def gate_and_allowlist() -> tuple[list[str], list[str]]:
    """Boot-time gating shared by the connector transports (S1 + S2)."""
    _gate_tools()
    kept, removed = _apply_connector_allowlist()
    print(f"[hippocampus-connector] allowlisted tools ({len(kept)}): {', '.join(kept)}",
          file=sys.stderr, flush=True)
    print(f"[hippocampus-connector] removed from connector surface ({len(removed)}): "
          f"{', '.join(removed)}", file=sys.stderr, flush=True)
    # Fail-closed assertion (bug-hunt F3): re-read the LIVE tool set after removal
    # and assert nothing outside the allowlist survived — in particular none of
    # the deny-set. Checking `kept` was tautological (kept ⊆ allowlist by
    # construction); a removal that silently failed would go undetected.
    live = _registered_tool_names()
    escaped = live - CONNECTOR_TOOL_ALLOWLIST
    if escaped:
        print(f"[hippocampus-connector] FATAL: non-allowlisted tools still live: "
              f"{sorted(escaped)}", file=sys.stderr, flush=True)
        sys.exit(2)
    # Read-audit (§5 / r1-ops-2) then chain-read budget (C2 / bug-hunt F2). Order:
    # audit inner, budget outer — an over-budget call raises before it is audited,
    # so the audit log reflects served reads. Connector-only (C5). Both fail-open
    # / no-op safe: audit skips if its table is absent.
    from . import connector_audit
    from .connector_budget import apply_budget
    from .server import get_conn
    connector_audit.audit_available(get_conn)  # probe once
    tm = mcp._tool_manager
    for name in kept:
        tool = tm._tools.get(name)
        if tool is not None:
            tool.fn = connector_audit.wrap(tool.fn, get_conn, name)
    apply_budget(mcp, kept)
    return kept, removed


def _enable_oauth() -> None:
    """Attach the single-owner OAuth AS to the shared FastMCP instance (S2).

    server.py builds `mcp` without auth (stdio needs none); we set the provider
    + AuthSettings post-construction so streamable_http_app() serves the OAuth
    routes and gates /mcp. Config comes from the environment (sops-injected):
      HIPPOCAMPUS_CONNECTOR_ISSUER_URL   = https://<host>
      HIPPOCAMPUS_CONNECTOR_RESOURCE_URL = https://<host>/mcp
      HIPPOCAMPUS_CONNECTOR_CLIENT_ID / _CLIENT_SECRET (static claude.ai client)
    Fail-closed: missing config aborts before the app is exposed (C1).
    """
    from .connector_oauth import HippocampusOAuthProvider, build_auth_settings

    issuer = os.environ.get("HIPPOCAMPUS_CONNECTOR_ISSUER_URL", "").rstrip("/")
    resource = os.environ.get("HIPPOCAMPUS_CONNECTOR_RESOURCE_URL", "")
    client_id = os.environ.get("HIPPOCAMPUS_CONNECTOR_CLIENT_ID", "")
    client_secret = os.environ.get("HIPPOCAMPUS_CONNECTOR_CLIENT_SECRET", "")
    missing = [k for k, v in {
        "HIPPOCAMPUS_CONNECTOR_ISSUER_URL": issuer,
        "HIPPOCAMPUS_CONNECTOR_RESOURCE_URL": resource,
        "HIPPOCAMPUS_CONNECTOR_CLIENT_ID": client_id,
        "HIPPOCAMPUS_CONNECTOR_CLIENT_SECRET": client_secret,
    }.items() if not v]
    if missing:
        print(f"[hippocampus-connector] FATAL: OAuth config missing: {missing}",
              file=sys.stderr, flush=True)
        sys.exit(1)

    provider = HippocampusOAuthProvider(
        issuer_url=issuer, resource_url=resource,
        client_id=client_id, client_secret=client_secret,
    )
    # DNS-rebinding protection: FastMCP rejects any Host header not in
    # allowed_hosts with 421 (default allows only the localhost bind). Behind the
    # cloudflared tunnel the forwarded Host is the public hostname, so it must be
    # allowlisted or every claude.ai /mcp call 421s ("Invalid Host header").
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings
    public_host = urlparse(issuer).netloc  # e.g. mcp.example.com
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, f"{public_host}:443",
                       f"{HOST}:{PORT}", "127.0.0.1:8092", "localhost:8092"],
        allowed_origins=[f"https://{public_host}", f"https://{public_host}:443"],
    )
    mcp.settings.auth = build_auth_settings(issuer_url=issuer, resource_url=resource)
    mcp._auth_server_provider = provider
    # CRITICAL (bug-hunt): streamable_http_app() gates RequireAuthMiddleware on
    # _token_verifier, which FastMCP sets ONLY in __init__. Since server.py builds
    # mcp without auth, we must set it here too — otherwise /mcp is served WITHOUT
    # the bearer layer (fail-OPEN: the OAuth ceremony succeeds but /mcp accepts
    # anonymous requests and the access token is never verified).
    from mcp.server.auth.provider import ProviderTokenVerifier
    mcp._token_verifier = ProviderTokenVerifier(provider)
    print(f"[hippocampus-connector] OAuth enabled: issuer={issuer} resource={resource}",
          file=sys.stderr, flush=True)


def main() -> None:
    """S1 entrypoint: streamable HTTP on localhost, no auth (local smoke only)."""
    gate_and_allowlist()
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    print(f"[hippocampus-connector] streamable HTTP (no-auth, localhost) on "
          f"{HOST}:{PORT}{mcp.settings.streamable_http_path}", file=sys.stderr, flush=True)
    mcp.run(transport="streamable-http")


def main_oauth() -> None:
    """S2+ entrypoint: streamable HTTP with the OAuth AS (public deploy path)."""
    gate_and_allowlist()
    _enable_oauth()
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    print(f"[hippocampus-connector] streamable HTTP (OAuth) on "
          f"{HOST}:{PORT}{mcp.settings.streamable_http_path}", file=sys.stderr, flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
