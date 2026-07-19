"""Multi-user access predicate for personal-memory read tools (Slice 3).

Single normative place that turns a caller Principal into a SQL WHERE fragment.
Every personal read tool appends the fragment returned here to the candidate
CTE(s) that scan `personal.conversations` / `personal.messages`, so scoping
happens BEFORE ORDER BY / LIMIT — never as result-only post-filtering.

Design: docs/designs/company-shared-hippocampus.md §12 (Access Predicate).

Trust model for THIS deployment (operator-mode, trusted internal team):
identity comes from the per-user process env (Claude Desktop stdio wrapper);
the real enforcement boundary is each user's own PG role + password (§9.3).
The heavier DB-side SECURITY DEFINER / session_user derivation (§12 employee
idiom) is deliberately deferred until the trust boundary tightens.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    team_ids: tuple[str, ...]
    role: str
    default_visibility: str
    multiuser: bool


def current_principal(settings) -> Principal:
    """Derive the caller Principal from process Settings."""
    return Principal(
        tenant_id=settings.tenant_id,
        user_id=settings.user_id,
        team_ids=tuple(settings.team_ids),
        role=settings.role,
        default_visibility=settings.default_visibility,
        multiuser=settings.multiuser,
    )


def personal_access_sql(alias: str, principal: Principal) -> tuple[str, dict]:
    """Return (sql_fragment, params) scoping `alias` to what `principal` may read.

    - Single-user mode returns ("TRUE", {}) so existing queries are byte-identical.
    - `alias` must be a conversation-shaped relation exposing tenant_id,
      owner_user_id, visibility, team_id (personal.conversations, or a message
      alias in owner-only contexts — see `personal_access_sql_owner_only`).
    - Params are NAMED (acl_*) so the fragment composes into multi-CTE queries
      without positional-order fragility; the same names may repeat across CTEs.
    """
    if not principal.multiuser:
        return "TRUE", {}
    # owner access is unconditional on visibility; org is tenant-wide;
    # team requires a non-null team_id matching one of the caller's teams.
    frag = (
        f"{alias}.tenant_id = %(acl_tenant_id)s AND ("
        f"{alias}.owner_user_id = %(acl_user_id)s"
        f" OR {alias}.visibility = 'org'"
        f" OR ({alias}.visibility = 'team' AND {alias}.team_id = ANY(%(acl_team_ids)s))"
        f")"
    )
    return frag, {
        "acl_tenant_id": principal.tenant_id,
        "acl_user_id": principal.user_id,
        "acl_team_ids": list(principal.team_ids),
    }


def personal_access_sql_owner_only(alias: str, principal: Principal) -> tuple[str, dict]:
    """Owner-only scoping usable on a MESSAGE alias (no visibility column).

    A message row carries tenant_id + owner_user_id but not visibility/team_id,
    so team/org *shared-read* cannot be expressed on it. Use this only where the
    candidate relation is `personal.messages` with no conversation join in scope
    (e.g. the trgm-only branch of the RRF search): it still guarantees the
    load-bearing isolation property — a caller never sees another user's rows —
    and shared rows that this branch drops are recovered via the dense branch,
    which does join conversations and uses the full predicate.
    """
    if not principal.multiuser:
        return "TRUE", {}
    frag = (
        f"{alias}.tenant_id = %(acl_tenant_id)s"
        f" AND {alias}.owner_user_id = %(acl_user_id)s"
    )
    return frag, {
        "acl_tenant_id": principal.tenant_id,
        "acl_user_id": principal.user_id,
    }
