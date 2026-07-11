"""Single-owner OAuth authorization server for the claude.ai connector (S2).

Design: docs/CONNECTOR.md.

The MCP SDK (mcp>=1.27) ships the OAuth *routing*, client authentication and
PKCE-S256 verification, but `OAuthAuthorizationServerProvider` is a Protocol —
token issuance, storage, refresh and revocation are ours to implement. This
module is that provider, specialised for a single owner (C3):

- **static confidential client, DCR disabled** (C1/§4, r1-codex-1): exactly one
  client, whose redirect_uri allowlist is the documented claude.ai callback and
  nothing else. `register_client` refuses everything.
- **opaque, hashed-at-rest tokens** (C6, r1-codex-2): tokens are
  `secrets.token_urlsafe` values; only their SHA-256 is stored, so a store dump
  does not yield live tokens. No hand-rolled crypto — PKCE stays with the SDK.
- **audience binding** (C7, r1-codex-5/r3-codex-4): every issued token carries
  `resource`; `load_access_token` rejects (returns None → 401) any token whose
  resource ≠ the canonical resource_server_url. `RefreshToken` has no `resource`
  field in the SDK, so we subclass it to carry the binding across rotation.
- **refresh rotation + reuse detection**: each refresh mints a new refresh token
  and retires the old one; presenting a retired token revokes the whole family.

Stores are in-memory (C4: `systemctl stop` == flush; a single owner re-auths on
restart, which is acceptable). Nothing here is a general multi-tenant AS.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# claude.ai's documented OAuth callback (design §4, r3-codex-1). Only this exact
# URI is an allowed redirect. A future claude.com callback is a reviewed config
# change, not a pre-authorised URI.
CLAUDE_AI_CALLBACK = "https://claude.ai/api/mcp/auth_callback"

ACCESS_TTL_S = int(os.environ.get("HIPPOCAMPUS_CONNECTOR_ACCESS_TTL", "3600"))
REFRESH_TTL_S = int(os.environ.get("HIPPOCAMPUS_CONNECTOR_REFRESH_TTL", str(30 * 24 * 3600)))
CODE_TTL_S = 300
OWNER_SUBJECT = "owner"
DEFAULT_SCOPES = ["hippocampus.read"]


def _hash(token: str) -> str:
    """SHA-256 hex of a token — the only form kept at rest (C6)."""
    return hashlib.sha256(token.encode()).hexdigest()


class ResourceRefreshToken(RefreshToken):
    """RefreshToken + the bound resource (C7).

    The SDK's base RefreshToken drops `resource`; without carrying it, audience
    binding would be lost across a refresh (r3-codex-4). The provider is generic
    over RefreshTokenT, so this subclass flows through unchanged.
    """

    resource: str | None = None
    family_id: str = ""


@dataclass
class _Stores:
    """In-memory token/code state. One owner; cleared on process exit (C4)."""

    codes: dict[str, AuthorizationCode] = field(default_factory=dict)          # hash -> code
    access: dict[str, AccessToken] = field(default_factory=dict)               # hash -> token
    refresh: dict[str, ResourceRefreshToken] = field(default_factory=dict)     # hash -> token
    # refresh-token family -> the single currently-live refresh hash.
    live_refresh_by_family: dict[str, str] = field(default_factory=dict)
    # hash of a rotated-away (retired) refresh token -> its family_id. Presenting
    # one is a replay: the SDK calls load_refresh_token first, so detection MUST
    # live there (not in exchange_refresh_token, which a retired token never
    # reaches — bug-hunt F1). Seeing a retired hash revokes the whole family.
    retired_refresh: dict[str, str] = field(default_factory=dict)


class HippocampusOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, ResourceRefreshToken, AccessToken]
):
    """Single-owner provider. See module docstring for the invariants."""

    def __init__(self, *, issuer_url: str, resource_url: str,
                 client_id: str, client_secret: str) -> None:
        self._issuer = issuer_url.rstrip("/")
        # Canonical resource the tokens must be bound to (C7). Compared exactly.
        self._resource = resource_url
        self._client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[CLAUDE_AI_CALLBACK],  # allowlist, exactly one
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
            scope=" ".join(DEFAULT_SCOPES),
        )
        self._s = _Stores()

    # ── client registry (static; DCR disabled) ────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._client if client_id == self._client.client_id else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # DCR is disabled at the settings level; defend in depth here too.
        raise NotImplementedError("dynamic client registration is disabled")

    # ── authorize: single-owner auto-consent behind CF Access ──────────────
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        # redirect_uri is validated against the client's allowlist by the SDK
        # route before we get here; re-assert to be safe.
        if str(params.redirect_uri) not in {str(u) for u in client.redirect_uris}:
            raise ValueError("redirect_uri not in client allowlist")
        code = secrets.token_urlsafe(32)
        now = time.time()
        self._s.codes[_hash(code)] = AuthorizationCode(
            code=code,
            scopes=params.scopes or DEFAULT_SCOPES,
            expires_at=now + CODE_TTL_S,
            client_id=client.client_id,
            code_challenge=params.code_challenge,          # PKCE verified by SDK
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,                      # bind audience (C7)
            subject=OWNER_SUBJECT,
        )
        return construct_redirect_uri(str(params.redirect_uri),
                                      code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        code = self._s.codes.get(_hash(authorization_code))
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            self._s.codes.pop(_hash(authorization_code), None)
            return None
        return code

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                         authorization_code: AuthorizationCode) -> OAuthToken:
        # One-time use: consume the code first.
        self._s.codes.pop(_hash(authorization_code.code), None)
        return self._issue_pair(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            family_id=secrets.token_urlsafe(16),
        )

    # ── refresh: rotation + reuse detection, resource preserved ────────────
    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                refresh_token: str) -> ResourceRefreshToken | None:
        h = _hash(refresh_token)
        # Replay of a rotated-away token → revoke the whole family (this is the
        # reachable detection point; exchange_refresh_token is only called for a
        # token that loads, and a retired token no longer lives in the store).
        fam = self._s.retired_refresh.get(h)
        if fam is not None:
            self._revoke_family(fam)
            return None
        tok = self._s.refresh.get(h)
        if tok is None or tok.client_id != client.client_id:
            return None
        if tok.expires_at is not None and tok.expires_at < time.time():
            return None
        return tok

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                    refresh_token: ResourceRefreshToken,
                                    scopes: list[str]) -> OAuthToken:
        presented = _hash(refresh_token.token)
        live = self._s.live_refresh_by_family.get(refresh_token.family_id)
        # Defense in depth: a token that loaded but is not the family's live one.
        if live is not None and live != presented:
            self._revoke_family(refresh_token.family_id)
            raise TokenError("invalid_grant", "refresh token reuse detected")
        # Requested scopes must be a subset of the grant (SDK pre-validates, but
        # re-check). TokenError, not ValueError, so the SDK emits RFC-6749 JSON.
        granted = set(refresh_token.scopes)
        req = set(scopes) if scopes else granted
        if not req.issubset(granted):
            raise TokenError("invalid_scope", "requested scopes exceed grant")
        # Retire the presented token (record its hash so a replay is detected),
        # mint a fresh pair in the same family.
        self._s.refresh.pop(presented, None)
        self._s.retired_refresh[presented] = refresh_token.family_id
        return self._issue_pair(
            client_id=client.client_id,
            scopes=sorted(req),
            resource=refresh_token.resource,
            family_id=refresh_token.family_id,
        )

    # ── access-token verification: the audience gate (C7) ──────────────────
    async def load_access_token(self, token: str) -> AccessToken | None:
        tok = self._s.access.get(_hash(token))
        if tok is None:
            return None
        if tok.expires_at is not None and tok.expires_at < time.time():
            self._s.access.pop(_hash(token), None)
            return None
        # Audience binding: a token minted for another resource is not valid here.
        # Exact match (C7). If claude.ai omits/canonicalizes the RFC 8707 resource
        # differently, every call 401s — log the mismatch (not the token) so the
        # S4 interop failure is diagnosable rather than a silent blanket 401.
        if tok.resource != self._resource:
            import sys
            print(f"[hippocampus-connector] audience mismatch: token.resource="
                  f"{tok.resource!r} expected={self._resource!r} → 401",
                  file=sys.stderr, flush=True)
            return None
        return tok

    async def revoke_token(self, token: AccessToken | ResourceRefreshToken) -> None:
        h = _hash(token.token)
        self._s.access.pop(h, None)
        fam = getattr(token, "family_id", "")
        if fam:
            self._revoke_family(fam)
        else:
            self._s.refresh.pop(h, None)

    # ── helpers ────────────────────────────────────────────────────────────
    def _issue_pair(self, *, client_id: str, scopes: list[str],
                    resource: str | None, family_id: str) -> OAuthToken:
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._s.access[_hash(access)] = AccessToken(
            token=access, client_id=client_id, scopes=scopes,
            expires_at=now + ACCESS_TTL_S, resource=resource, subject=OWNER_SUBJECT,
        )
        rtok = ResourceRefreshToken(
            token=refresh, client_id=client_id, scopes=scopes,
            expires_at=now + REFRESH_TTL_S, subject=OWNER_SUBJECT,
            resource=resource, family_id=family_id,
        )
        self._s.refresh[_hash(refresh)] = rtok
        self._s.live_refresh_by_family[family_id] = _hash(refresh)
        return OAuthToken(
            access_token=access, token_type="Bearer",
            expires_in=ACCESS_TTL_S, refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _revoke_family(self, family_id: str) -> None:
        self._s.live_refresh_by_family.pop(family_id, None)
        for h, tok in list(self._s.refresh.items()):
            if tok.family_id == family_id:
                self._s.refresh.pop(h, None)
        # Also revoke access tokens minted for this family's owner is out of
        # scope (single owner); at minimum drop the retired markers so the set
        # doesn't grow unbounded after a family dies.
        for h, fam in list(self._s.retired_refresh.items()):
            if fam == family_id:
                self._s.retired_refresh.pop(h, None)


def build_auth_settings(*, issuer_url: str, resource_url: str) -> AuthSettings:
    """AuthSettings pinned to the canonical public origins (C7).

    issuer_url = the AS origin (https://host); resource_server_url = the
    protected resource (https://host/mcp), which drives the path-suffixed
    /.well-known/oauth-protected-resource/mcp metadata (r3-codex-4).
    """
    return AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_url,
        required_scopes=DEFAULT_SCOPES,
        client_registration_options=ClientRegistrationOptions(enabled=False),  # DCR off
        revocation_options=RevocationOptions(enabled=True),
    )
