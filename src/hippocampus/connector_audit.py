"""Read-audit for the connector surface (design §5 / r1-ops-2).

Today server.py's read tools write no audit rows — only the ghost tools do
(agent.ghost_read_log). Exposing reads on a public cloud endpoint without an
audit trail is the honest gap r1-ops-2 flagged. This writes one row per
connector tool call, reusing the ghost fail-open discipline: an audit failure
(table absent, DB down) must NEVER break the read.

The audit table (personal.connector_read_log) lands as a migration coordinated
with the in-flight multiuser slice — until it exists, writes fail-open and the
connector still serves. `audit_available()` reports whether the sink is live so
deploy can verify before the S4 cutover.

Privacy: the query text is NOT stored — only the tool name, an argument digest
(sha256 prefix over the repr) and arg count. The owner is the sole principal
(C3), so the load-bearing signal is the access *pattern* (which tool, when, how
often — anomaly/burst detection), not the content.
"""
from __future__ import annotations

import hashlib
import sys
import time

_TABLE = "personal.connector_read_log"
# DDL for the coordinated migration (recorded here so the writer and the schema
# cannot drift). Applied via migrations/manifest.yaml, not ad-hoc DDL.
AUDIT_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool_name   TEXT NOT NULL,
    arg_digest  TEXT,
    arg_count   INT,
    session_hint TEXT
);
"""

_available: bool | None = None  # None = unprobed


def _digest(args: tuple, kwargs: dict) -> str:
    payload = repr(args) + repr(sorted(kwargs.items()))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def audit_available(get_conn) -> bool:
    """Probe once whether the audit sink exists. Cached. Never raises."""
    global _available
    if _available is not None:
        return _available
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (_TABLE,))
                _available = cur.fetchone()[0] is not None
        finally:
            conn.close()
    except Exception:
        _available = False
    if not _available:
        print(f"[hippocampus-connector] read-audit sink {_TABLE} absent — "
              "audit fail-open (reads still served)", file=sys.stderr, flush=True)
    return _available


def record(get_conn, *, tool_name: str, args: tuple, kwargs: dict,
           session_hint: str | None = None) -> None:
    """Write one audit row. Fail-open: any error is swallowed."""
    if _available is False:  # known-absent → skip cheaply
        return
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {_TABLE} (tool_name, arg_digest, arg_count, session_hint) "
                    "VALUES (%s, %s, %s, %s)",
                    (tool_name, _digest(args, kwargs), len(args) + len(kwargs), session_hint),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Ghost discipline: audit must never break a read. Note once, keep going.
        pass


def wrap(fn, get_conn, tool_name: str):
    """Wrap a tool callable to emit an audit row per call (fail-open)."""
    import functools

    if getattr(fn, "_hippocampus_audited", False):
        return fn

    @functools.wraps(fn)
    def audited(*args, **kwargs):
        record(get_conn, tool_name=tool_name, args=args, kwargs=kwargs)
        return fn(*args, **kwargs)

    audited._hippocampus_audited = True
    return audited
