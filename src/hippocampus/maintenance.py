"""Shared write barrier used while a database migration is in progress."""
from __future__ import annotations

import psycopg2


class MigrationInProgressError(RuntimeError):
    """Raised when the durable maintenance barrier is holding writes."""


_MISSING_BARRIER_ERRORS = (
    psycopg2.errors.UndefinedFunction,
    psycopg2.errors.UndefinedTable,
    psycopg2.errors.UndefinedColumn,
)
_SAVEPOINT = "hippocampus_maintenance_guard"


def assert_not_frozen(conn) -> bool:
    """Refuse writes only when the optional maintenance helper returns true.

    Migration 031 installs ``personal.is_maintenance_frozen()``. Older
    single-user databases intentionally do not have that function (and may not
    have its backing table/columns), so those three undefined-object failures
    are a not-frozen result. A savepoint keeps that compatibility probe from
    leaving the caller's transaction aborted.
    """
    cur = conn.cursor()
    use_savepoint = not getattr(conn, "autocommit", False)
    try:
        if use_savepoint:
            cur.execute(f"SAVEPOINT {_SAVEPOINT}")
        try:
            cur.execute("SELECT personal.is_maintenance_frozen()")
            row = cur.fetchone()
        except _MISSING_BARRIER_ERRORS:
            if use_savepoint:
                cur.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
                cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
            return False
        except Exception:
            if use_savepoint:
                cur.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
                cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
            raise
        if use_savepoint:
            cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    finally:
        cur.close()

    if row and row[0] is True:
        raise MigrationInProgressError(
            "migration in progress — writes are frozen"
        )
    return False
