"""Runner-owned, resumable company-multiuser conversation backfills.

The database stage table is the checkpoint source of truth.  Filesystem
JSON/JSONL artifacts are deliberately mirrors for operators and the watcher;
they never release a held migration.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


INITIAL_CHECKPOINT = "multiuser_backfill_complete"
GAP_WINDOW_CHECKPOINT = "multiuser_gap_window_backfill_complete"
DEFAULT_NO_PROGRESS_ALERT_S = 180
DEFAULT_STAGE_EXPECTED_S = 3_600


@dataclass(frozen=True)
class DatabaseIdentity:
    db_host: str
    dbname: str
    port: int = 5432

    def as_dict(self) -> dict[str, object]:
        return {
            "db_host": self.db_host,
            "dbname": self.dbname,
            "port": self.port,
        }


@dataclass(frozen=True)
class BackfillArtifacts:
    heartbeat: Optional[Path] = None
    checkpoints: Optional[Path] = None
    no_progress_alert_s: float = DEFAULT_NO_PROGRESS_ALERT_S
    stage_expected_s: float = DEFAULT_STAGE_EXPECTED_S


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_heartbeat(path: Optional[Path], record: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_checkpoint(path: Optional[Path], record: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def _mirror_record(
    stage: str,
    detail: dict[str, object],
    artifacts: BackfillArtifacts,
) -> dict[str, object]:
    return {
        "stage": stage,
        "tenant": "all",
        **detail,
        "no_progress_alert_s": artifacts.no_progress_alert_s,
        "stage_expected_s": artifacts.stage_expected_s,
    }


def _record_stage(conn, stage: str, detail: dict[str, object], *, complete: bool) -> None:
    payload = json.dumps(detail, sort_keys=True)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.hippocampus_migration_stages
                       (stage, completed_at, detail)
                 VALUES (%s, CASE WHEN %s THEN pg_catalog.now() ELSE NULL END,
                         %s::jsonb)
                 ON CONFLICT (stage) DO UPDATE
                     SET completed_at = EXCLUDED.completed_at,
                         detail = EXCLUDED.detail""",
            (stage, complete, payload),
        )


def _residual_count(conn, where_sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT pg_catalog.count(*) FROM personal.conversations WHERE {where_sql}"
        )
        return int(cur.fetchone()[0])


def _next_range(conn, where_sql: str, batch_size: int) -> tuple[str, str, int] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT pg_catalog.min(conv_id), pg_catalog.max(conv_id),
                       pg_catalog.count(*)
                  FROM (
                        SELECT conv_id
                          FROM personal.conversations
                         WHERE {where_sql}
                         ORDER BY conv_id
                         LIMIT %s
                       ) AS batch""",
            (batch_size,),
        )
        first, last, count = cur.fetchone()
    if not count:
        return None
    return str(first), str(last), int(count)


def _updated_at_batch(conn, first: str, last: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE personal.conversations
                  SET updated_at = pg_catalog.now()
                WHERE conv_id >= %s AND conv_id <= %s
                  AND updated_at IS NULL""",
            (first, last),
        )
        return max(int(cur.rowcount), 0)


_SOURCE_NULL_PREDICATE = """(
    source_conv_id IS NULL OR source_platform IS NULL
    OR source_adapter IS NULL OR source_identity_hash IS NULL
)"""


def _source_identity_batch(conn, first: str, last: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE personal.conversations
                   SET source_conv_id = pg_catalog.coalesce(source_conv_id, conv_id),
                       source_platform = pg_catalog.coalesce(source_platform, platform),
                       source_adapter = pg_catalog.coalesce(source_adapter, platform),
                       source_identity_hash = personal.multiuser_source_identity_hash(
                           tenant_id,
                           owner_user_id,
                           pg_catalog.coalesce(source_platform, platform),
                           pg_catalog.coalesce(source_conv_id, conv_id)
                       )
                 WHERE conv_id >= %s AND conv_id <= %s
                   AND {_SOURCE_NULL_PREDICATE}""",
            (first, last),
        )
        return max(int(cur.rowcount), 0)


def _run_stage(
    conn,
    *,
    stage: str,
    where_sql: str,
    update_batch: Callable[[object, str, str], int],
    identity: DatabaseIdentity,
    artifacts: BackfillArtifacts,
    batch_size: int,
) -> int:
    started = time.monotonic()
    rows_total = _residual_count(conn, where_sql)
    rows_done = 0
    last_range: tuple[str, str] | None = None

    while True:
        bounds = _next_range(conn, where_sql, batch_size)
        if bounds is None:
            break
        first, last, _selected = bounds
        changed = update_batch(conn, first, last)
        rows_done += changed
        last_range = (first, last)
        elapsed = int(time.monotonic() - started)
        detail: dict[str, object] = {
            **identity.as_dict(),
            "status": "running",
            "range_start": first,
            "range_end": last,
            "rows_done": rows_done,
            "rows_total": rows_total,
            "elapsed_s": elapsed,
            "heartbeat_at": _utc_now(),
        }
        _record_stage(conn, stage, detail, complete=False)
        mirror = _mirror_record(stage, detail, artifacts)
        _append_checkpoint(artifacts.checkpoints, mirror)
        _write_heartbeat(artifacts.heartbeat, mirror)

    residual = _residual_count(conn, where_sql)
    if residual:
        raise RuntimeError(f"{stage}: {residual} residual row(s) after batched backfill")

    elapsed = int(time.monotonic() - started)
    detail = {
        **identity.as_dict(),
        "status": "complete",
        "range_start": last_range[0] if last_range else None,
        "range_end": last_range[1] if last_range else None,
        "rows_done": rows_done,
        "rows_total": rows_total,
        "elapsed_s": elapsed,
        "backfill_ran": True,
        "heartbeat_at": _utc_now(),
    }
    _record_stage(conn, stage, detail, complete=True)
    mirror = _mirror_record(stage, detail, artifacts)
    _append_checkpoint(artifacts.checkpoints, mirror)
    _write_heartbeat(artifacts.heartbeat, mirror)
    return rows_done


def run_initial_backfills(
    conn,
    identity: DatabaseIdentity,
    *,
    artifacts: BackfillArtifacts = BackfillArtifacts(),
    batch_size: int = 1_000,
) -> dict[str, int]:
    """Backfill 031 columns and record ``multiuser_backfill_complete``."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    updated = _run_stage(
        conn,
        stage="multiuser_updated_at_backfill",
        where_sql="updated_at IS NULL",
        update_batch=_updated_at_batch,
        identity=identity,
        artifacts=artifacts,
        batch_size=batch_size,
    )
    sourced = _run_stage(
        conn,
        stage="multiuser_source_identity_backfill",
        where_sql=_SOURCE_NULL_PREDICATE,
        update_batch=_source_identity_batch,
        identity=identity,
        artifacts=artifacts,
        batch_size=batch_size,
    )
    detail: dict[str, object] = {
        **identity.as_dict(),
        "status": "complete",
        "backfill_ran": True,
        "updated_at_rows": updated,
        "source_identity_rows": sourced,
        "completed_at": _utc_now(),
    }
    _record_stage(conn, INITIAL_CHECKPOINT, detail, complete=True)
    mirror = _mirror_record(INITIAL_CHECKPOINT, detail, artifacts)
    _append_checkpoint(
        artifacts.checkpoints,
        mirror,
    )
    _write_heartbeat(artifacts.heartbeat, mirror)
    return {"updated_at_rows": updated, "source_identity_rows": sourced}


def run_gap_window_backfill(
    conn,
    identity: DatabaseIdentity,
    *,
    artifacts: BackfillArtifacts = BackfillArtifacts(),
    batch_size: int = 1_000,
) -> int:
    """Fill post-031 source gaps and record the checkpoint consumed by 032.

    This is intentionally a separate explicit operation.  The migration runner
    cannot infer that Slice 2 ingest stamping is deployed, so its first gated
    invocation must not manufacture this checkpoint and prematurely apply 032.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    rows = _run_stage(
        conn,
        stage="multiuser_gap_window_source_identity_backfill",
        where_sql=_SOURCE_NULL_PREDICATE,
        update_batch=_source_identity_batch,
        identity=identity,
        artifacts=artifacts,
        batch_size=batch_size,
    )
    detail: dict[str, object] = {
        **identity.as_dict(),
        "status": "complete",
        "backfill_ran": True,
        "source_identity_rows": rows,
        "completed_at": _utc_now(),
    }
    _record_stage(conn, GAP_WINDOW_CHECKPOINT, detail, complete=True)
    mirror = _mirror_record(GAP_WINDOW_CHECKPOINT, detail, artifacts)
    _append_checkpoint(
        artifacts.checkpoints,
        mirror,
    )
    _write_heartbeat(artifacts.heartbeat, mirror)
    return rows
