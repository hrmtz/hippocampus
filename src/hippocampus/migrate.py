"""Manifest-driven migration runner (epic #43 Phase 3, plan §3.6).

Usage (module-callable; CLI wiring into `hippocampus migrate` lands
separately):

    python3 -m hippocampus.migrate [--core-only] [--with-library]
                                   [--with-multiuser]
                                   [--include-optional] [--dry-run]
                                   [--status] [--baseline [--yes]]
                                   [--db-url DSN]

Design decisions (binding, from plan §3.6 + dual-magi round 3):

- ``migrations/manifest.yaml`` is the single source of truth for order,
  tier (core / library / optional / multiuser), gating attributes, and the
  ``no_tx`` flag. ``*_down.sql``
  files are never in the manifest. The manifest is plain YAML restricted
  to a line-based subset parsed here WITHOUT pyyaml (grammar documented in
  the manifest header).

- **Uniform psql execution** (the documented "simpler alternative" of the
  spec): every file is delegated to a ``psql -v ON_ERROR_STOP=1``
  subprocess — psql natively parses DO-$$ bodies, mixed BEGIN sections and
  out-of-tx CONCURRENTLY; a hand-rolled split-on-semicolon would corrupt
  013/014. Tx-safe files (``no_tx: false``) additionally get ``-1``
  (``--single-transaction``) so files that do not self-wrap are still
  atomic; files that DO self-wrap (e.g. 009) merely emit harmless "already
  a transaction in progress" warnings under ``-1``. ``no_tx: true`` files
  run in plain autocommit. Each file gets a **fresh psql session** so
  ``lock_timeout`` GUCs set by 013/014 never leak across files
  (r3-schema-3).

- **Connection hygiene**: the DSN is never placed in psql argv (process
  list leak) and never printed. It is decomposed via urllib.parse into
  PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD environment variables for the
  subprocess. Any human-facing display goes through
  ``hippocampus.config.redact_dsn``.

- **Ledger**: ``public.hippocampus_schema_migrations(filename text primary
  key, applied_at timestamptz default now(), sha256 text)``. It lives in
  schema ``public`` deliberately — migration 001 is what creates schema
  ``personal``, so the ledger must exist before 001 runs. Already-applied
  files are skipped; if the on-disk sha256 of an applied file differs from
  the recorded one, a warning is printed but the run continues (migration
  files are immutable by convention; a diff means a comment-level edit,
  not a re-apply).

- **Belt-and-suspenders no-tx scanner** (r3-schema-1/2): before running,
  each file's SQL is scanned — with ``--`` line comments and ``/* */``
  block comments stripped and dollar-quoted bodies masked — for
  ``CREATE/DROP INDEX CONCURRENTLY`` and ``ALTER TYPE .. ADD VALUE``. If a
  construct is found but the manifest says ``no_tx: false``, the runner
  REFUSES (running it under ``-1`` would fail or, worse, half-apply).
  The reverse mismatch (``no_tx: true`` but nothing found) only warns —
  it merely loses per-file atomicity. 009 mentions "ALTER TYPE ADD VALUE"
  inside a comment while being correctly tx-wrapped; it is the regression
  fixture proving the scanner is comment-aware.

- **Resume semantics**: a failed ``CREATE INDEX CONCURRENTLY`` leaves an
  INVALID index and the file is NOT recorded in the ledger. On rerun the
  file simply runs again: 013/014 carry their own DO-block INVALID-index
  checks which RAISE with explicit remediation ("DROP INDEX CONCURRENTLY
  ...; then re-apply"). This runner surfaces those errors verbatim and
  never swallows them; remediation is the operator's single manual step,
  after which rerun proceeds.

- **Baseline mode** (``--baseline``): records the selected-tier *pending*
  migrations in the ledger (filename + on-disk sha256 + applied_at=now())
  WITHOUT executing any SQL. Use case: a production DB that predates the
  ledger — its schema already holds 001..N, so a bare run would re-apply
  001 and fail on the first unguarded CREATE TABLE. Baseline lets such
  pre-ledger installs adopt the runner: stamp what is already in the
  schema, then future runs apply only genuinely new files. Combine with
  the tier flags (``--core-only`` / ``--with-library`` /
  ``--include-optional`` / ``--with-multiuser``) to control which entries
  are stamped. Because
  it rewrites history without verification, it asks for an interactive
  confirmation ("baseline") unless ``--yes`` is passed; ``--dry-run``
  previews the stamp list. The operator is responsible for the claim
  that the schema actually matches the stamped files.

- **DB targeting**: ``--db-url`` overrides everything (avoid in shared
  transcripts — argv is visible to the caller's shell history). The env
  var ``HIPPOCAMPUS_MIGRATE_DB`` may hold a bare DATABASE NAME; the runner
  then swaps the database name inside ``Settings.load().pg_url`` (used by
  the scratch-DB e2e test so no full DSN ever appears in argv). Otherwise
  ``Settings.load().pg_url`` is used as-is.

Prerequisite: ``psql`` on PATH (checked upfront). Migration role needs
CREATEROLE for 009 (cluster-global agent_* roles; guarded with
IF NOT EXISTS so a cluster that already has them is fine).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hippocampus.config import Settings, format_pg_error, redact_dsn
from hippocampus.multiuser_backfill import (
    INITIAL_CHECKPOINT,
    BackfillArtifacts,
    DatabaseIdentity,
    run_initial_backfills,
)

MANIFEST_NAME = "manifest.yaml"


def _find_migrations_dir() -> Path:
    """Locate migrations/ for both install shapes.

    Installed (non-editable) package: the files are package data at
    ``hippocampus/migrations/`` (symlinked from repo root in the source
    tree, materialized by the wheel build). Repo checkout / editable
    install: fall back to ``<repo>/migrations``.
    """
    pkg = Path(__file__).resolve().parent / "migrations"
    if (pkg / MANIFEST_NAME).is_file():
        return pkg
    return Path(__file__).resolve().parents[2] / "migrations"


MIGRATIONS_DIR = _find_migrations_dir()
LEDGER_TABLE = "public.hippocampus_schema_migrations"

VALID_TIERS = ("core", "library", "optional", "multiuser", "code")
VALID_PREFLIGHTS = ("company_multiuser",)

# These attributes are security properties of the migrations, not policy that
# an alternate manifest may weaken.  The manifest still declares them so that
# status/reporting remains data-driven, but selected entries must match this
# code-owned contract exactly.
REQUIRED_GATE_ATTRIBUTES = {
    "031_org_multiuser.sql": ("company_multiuser", ""),
    "031b_multiuser_backfill_ddl.sql": (
        "company_multiuser",
        "multiuser_backfill_complete",
    ),
    "032_multiuser_source_identity_enforce.sql": (
        "company_multiuser",
        "multiuser_gap_window_backfill_complete",
    ),
}

# Session-level lock: one company-multiuser runner may own the backfill/freeze
# protocol for a database at a time.  The value is fixed and feature-specific.
COMPANY_MULTIUSER_ADVISORY_LOCK_KEY = 0x484950504F4D55

# The design says 1 GB (not 1 GiB), so keep the operator threshold decimal.
SMALL_INSTALL_BYTES = 1_000_000_000
AFFECTED_TABLES = (
    "personal.conversations",
    "personal.messages",
    "personal.conversation_segments",
    "personal.extracted_facts",
)

# Constructs that forbid running inside a transaction block.
_NO_TX_RE = re.compile(
    r"\b(?:CREATE|DROP)\s+INDEX\s+CONCURRENTLY\b"
    r"|\bALTER\s+TYPE\s+\S+\s+ADD\s+VALUE\b",
    re.IGNORECASE,
)


class ManifestError(ValueError):
    """Raised when manifest.yaml violates the documented line grammar."""


class PreflightError(ValueError):
    """Raised before any write when the company-multiuser gate is invalid."""


@dataclass
class Entry:
    file: str
    tier: str = ""
    no_tx: Optional[bool] = None
    note: str = ""
    preflight: str = ""
    requires_checkpoint: str = ""


@dataclass(frozen=True)
class PreflightValidation:
    record: dict[str, object]
    small_install: bool
    affected_total_bytes: int


# ---------------------------------------------------------------------------
# Manifest parsing (line-based YAML subset; grammar in manifest header)
# ---------------------------------------------------------------------------

def parse_manifest(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    cur: Optional[Entry] = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "migrations:":
            continue
        if line.startswith("- file:"):
            cur = Entry(file=line[len("- file:"):].strip())
            entries.append(cur)
            continue
        if cur is None:
            raise ManifestError(f"{path.name}:{lineno}: field before any '- file:' entry")
        key, sep, value = line.partition(":")
        if not sep:
            raise ManifestError(f"{path.name}:{lineno}: unparseable line {line!r}")
        key, value = key.strip(), value.strip()
        if key == "tier":
            if value not in VALID_TIERS:
                raise ManifestError(f"{path.name}:{lineno}: bad tier {value!r}")
            cur.tier = value
        elif key == "no_tx":
            if value not in ("true", "false"):
                raise ManifestError(f"{path.name}:{lineno}: no_tx must be true|false, got {value!r}")
            cur.no_tx = value == "true"
        elif key == "note":
            cur.note = value
        elif key == "preflight":
            if value not in VALID_PREFLIGHTS:
                raise ManifestError(f"{path.name}:{lineno}: bad preflight {value!r}")
            cur.preflight = value
        elif key == "requires_checkpoint":
            if not value:
                raise ManifestError(
                    f"{path.name}:{lineno}: requires_checkpoint must name a stage"
                )
            cur.requires_checkpoint = value
        else:
            raise ManifestError(f"{path.name}:{lineno}: unknown field {key!r}")
    seen: set[str] = set()
    for e in entries:
        if "/" in e.file or ".." in e.file:
            raise ManifestError(
                f"manifest entry file must be a bare migrations filename: {e.file!r}"
            )
        if not e.tier or e.no_tx is None:
            raise ManifestError(f"manifest entry {e.file}: missing tier or no_tx")
        if e.file in seen:
            raise ManifestError(f"manifest lists {e.file} more than once")
        if e.file.endswith("_down.sql"):
            raise ManifestError(f"manifest must not list down-migrations: {e.file}")
        seen.add(e.file)
    if not entries:
        raise ManifestError(f"{path.name}: no entries")
    return entries


def validate_required_gate_attributes(entries: list[Entry]) -> None:
    """Refuse manifests that weaken code-owned migration gate attributes."""
    for entry in entries:
        required = REQUIRED_GATE_ATTRIBUTES.get(entry.file)
        if required is None:
            continue
        actual = (entry.preflight, entry.requires_checkpoint)
        if actual != required:
            preflight, checkpoint = required
            checkpoint_text = checkpoint or "<none>"
            raise ManifestError(
                f"manifest entry {entry.file} must declare exactly "
                f"preflight={preflight!r}, "
                f"requires_checkpoint={checkpoint_text!r}"
            )


# ---------------------------------------------------------------------------
# No-tx construct scanner (comment-stripping + dollar-quote masking)
# ---------------------------------------------------------------------------

_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def mask_sql(sql: str) -> str:
    """Return *sql* with comments removed and quoted bodies masked.

    Handles: ``--`` line comments, nested ``/* */`` block comments,
    dollar-quoted strings (``$$..$$``, ``$tag$..$tag$``), single-quoted
    strings (with ``''`` escapes), and double-quoted identifiers. The
    contents of strings / dollar bodies are replaced by spaces so that
    constructs mentioned inside DO-blocks or RAISE messages never
    false-positive (r3-schema-2; 009 is the regression fixture).
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":  # line comment
            j = sql.find("\n", i)
            i = n if j == -1 else j  # keep the newline
            continue
        if ch == "/" and nxt == "*":  # block comment (PG allows nesting)
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        if ch == "'":  # single-quoted string, '' escape
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("' '")
            i = min(j + 1, n)
            continue
        if ch == '"':  # quoted identifier
            j = sql.find('"', i + 1)
            j = n - 1 if j == -1 else j
            out.append('" "')
            i = j + 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j == -1:  # unterminated — mask to EOF
                    out.append(" ")
                    i = n
                    continue
                out.append(f"{tag} {tag}")
                i = j + len(tag)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def scan_no_tx_constructs(sql: str) -> list[str]:
    """Return the no-tx-forcing constructs present in *sql* (masked scan)."""
    return [m.group(0) for m in _NO_TX_RE.finditer(mask_sql(sql))]


# ---------------------------------------------------------------------------
# DSN handling
# ---------------------------------------------------------------------------

def resolve_db_url(db_url_arg: Optional[str]) -> str:
    if db_url_arg:
        return db_url_arg
    pg_url = Settings.load().pg_url
    override_db = os.environ.get("HIPPOCAMPUS_MIGRATE_DB", "")
    if override_db:
        if "/" in override_db or "@" in override_db or ":" in override_db:
            raise SystemExit(
                "HIPPOCAMPUS_MIGRATE_DB must be a bare database name, not a DSN"
            )
        parts = urllib.parse.urlsplit(pg_url)
        parts = parts._replace(path="/" + override_db)
        pg_url = urllib.parse.urlunsplit(parts)
    return pg_url


def dsn_to_pg_env(dsn: str) -> dict[str, str]:
    """Decompose a postgresql:// DSN into PG* env vars for psql.

    The DSN never enters argv and is never printed (output hygiene rule).
    """
    parts = urllib.parse.urlsplit(dsn)
    if parts.scheme not in ("postgresql", "postgres"):
        raise SystemExit(f"unsupported DSN scheme: {parts.scheme!r}")
    env = dict(os.environ)
    if parts.hostname:
        env["PGHOST"] = parts.hostname
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = urllib.parse.unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = urllib.parse.unquote(parts.password)
    dbname = parts.path.lstrip("/")
    if dbname:
        env["PGDATABASE"] = urllib.parse.unquote(dbname)
    # query params like ?sslmode=require
    for key, vals in urllib.parse.parse_qs(parts.query).items():
        mapped = {"sslmode": "PGSSLMODE", "options": "PGOPTIONS",
                  "application_name": "PGAPPNAME",
                  "connect_timeout": "PGCONNECT_TIMEOUT"}.get(key)
        if mapped and vals:
            env[mapped] = vals[-1]
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    return env


def assert_gated_target_matches(conn, pg_env: dict[str, str]) -> None:
    """Refuse the gated path unless the control connection and the psql DDL
    target are the SAME database.

    The advisory lock, the ``maintenance_freeze`` flag, and the checkpoint
    reads/writes all run on the psycopg2 ``conn``; the migration DDL runs via a
    psql subprocess parameterized by ``pg_env``. Both are parsed independently
    from the same DSN, so a query-param override (e.g. ``?options=-c ...``) that
    psycopg2 and libpq env handle differently could split them onto different
    databases — then every gated protection guards a session that is not the
    one being migrated. Verify equality before arming anything; raise otherwise.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        ctrl_db = cur.fetchone()[0]
    info = conn.info
    ctrl_host = (info.host or "").strip()
    ctrl_port = str(info.port or "").strip()
    tgt_db = pg_env.get("PGDATABASE", "").strip()
    tgt_host = pg_env.get("PGHOST", "").strip()
    tgt_port = pg_env.get("PGPORT", "").strip()

    # dbname is the definitive "which database"; require it and match strictly.
    if not tgt_db:
        raise PreflightError(
            "gated migration target database is unspecified (no PGDATABASE); "
            "refusing rather than migrate an ambiguous target")
    if ctrl_db != tgt_db:
        raise PreflightError(
            f"gated control connection is on database {ctrl_db!r} but the psql "
            f"migration target is {tgt_db!r}; refusing (the advisory lock, "
            f"maintenance_freeze, and checkpoints would guard a different "
            f"database than the one being migrated)")

    # host/port catch cross-cluster splits; compare only when both sides carry a
    # value, and treat the loopback spellings as equivalent.
    _loopback = {"", "localhost", "127.0.0.1", "::1"}
    if tgt_host and ctrl_host and ctrl_host != tgt_host \
            and not (ctrl_host in _loopback and tgt_host in _loopback):
        raise PreflightError(
            f"gated control connection host {ctrl_host!r} does not match psql "
            f"target host {tgt_host!r}; refusing")
    if tgt_port and ctrl_port and ctrl_port != tgt_port:
        raise PreflightError(
            f"gated control connection port {ctrl_port!r} does not match psql "
            f"target port {tgt_port!r}; refusing")


# ---------------------------------------------------------------------------
# Company-multiuser preflight + checkpoint gate
# ---------------------------------------------------------------------------

def dsn_identity(dsn: str) -> DatabaseIdentity:
    """Return the stable host/port/database identity carried by gate artifacts."""
    parts = urllib.parse.urlsplit(dsn)
    if parts.scheme not in ("postgresql", "postgres"):
        raise PreflightError(f"unsupported DSN scheme: {parts.scheme!r}")
    host = parts.hostname or os.environ.get("PGHOST") or "local"
    dbname = urllib.parse.unquote(parts.path.lstrip("/"))
    if not dbname:
        dbname = os.environ.get("PGDATABASE", "")
    if not dbname:
        raise PreflightError("target DSN does not name a database")
    return DatabaseIdentity(
        db_host=host,
        dbname=dbname,
        port=parts.port or 5432,
    )


def load_preflight_record(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreflightError(f"cannot read preflight record {path}: {exc}") from None
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError(
            f"invalid preflight JSON {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from None
    if not isinstance(record, dict):
        raise PreflightError("preflight record must be a JSON object")
    return record


def _placeholder(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    return (
        not text
        or (text.startswith("<") and text.endswith(">"))
        or "pass|fail" in text
        or "<mount" in text
        or "<value" in text
    )


def validate_record_identity(
    record: dict[str, object], identity: DatabaseIdentity
) -> None:
    for field, expected in identity.as_dict().items():
        actual = record.get(field)
        if isinstance(expected, int):
            valid_type = isinstance(actual, int) and not isinstance(actual, bool)
        else:
            valid_type = isinstance(actual, str)
        if _placeholder(actual) or not valid_type:
            raise PreflightError(f"preflight {field} is missing or a placeholder")
        if actual != expected:
            raise PreflightError(
                f"preflight database identity mismatch: {field}={actual!r}, "
                f"target={expected!r}"
            )


def measure_affected_tables(conn) -> dict[str, dict[str, int]]:
    """Read live table/index sizes without creating or changing DB objects."""
    measured: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        for table in AFFECTED_TABLES:
            cur.execute(
                """SELECT
                       CASE WHEN pg_catalog.to_regclass(%s) IS NULL THEN NULL
                            ELSE pg_catalog.pg_total_relation_size(
                                     pg_catalog.to_regclass(%s)) END,
                       CASE WHEN pg_catalog.to_regclass(%s) IS NULL THEN NULL
                            ELSE pg_catalog.pg_indexes_size(
                                     pg_catalog.to_regclass(%s)) END""",
                (table, table, table, table),
            )
            total_bytes, index_bytes = cur.fetchone()
            if total_bytes is None or index_bytes is None:
                raise PreflightError(
                    f"affected table {table} does not exist on the target database"
                )
            measured[table] = {
                "total_bytes": int(total_bytes),
                "index_bytes": int(index_bytes),
            }
    return measured


def _positive_int(record: dict[str, object], path: tuple[str, ...]) -> int:
    value: object = record
    for component in path:
        if not isinstance(value, dict) or component not in value:
            raise PreflightError(f"preflight {'.'.join(path)} is missing")
        value = value[component]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PreflightError(
            f"preflight {'.'.join(path)} must be a real positive measured value"
        )
    return value


def _nonnegative_int(record: dict[str, object], path: tuple[str, ...]) -> int:
    value: object = record
    for component in path:
        if not isinstance(value, dict) or component not in value:
            raise PreflightError(f"preflight {'.'.join(path)} is missing")
        value = value[component]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PreflightError(
            f"preflight {'.'.join(path)} must be a real non-negative measured value"
        )
    return value


def _positive_number(record: dict[str, object], field: str) -> float:
    value = record.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        raise PreflightError(f"preflight {field} must be a real positive measured value")
    return float(value)


def _required_text(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or _placeholder(value):
        raise PreflightError(f"preflight {field} is missing or a placeholder")
    return value


def _require_value(record: dict[str, object], field: str, expected: str) -> None:
    value = record.get(field)
    if _placeholder(value) or value != expected:
        raise PreflightError(f"preflight {field} must be {expected!r}")


def validate_preflight_record(
    record: dict[str, object],
    identity: DatabaseIdentity,
    measured: dict[str, dict[str, int]],
) -> PreflightValidation:
    """Validate measured resources, auto-filling only small-install sizes."""
    validate_record_identity(record, identity)
    if set(measured) != set(AFFECTED_TABLES):
        raise PreflightError("live size probe did not return every affected table")
    affected_total = sum(item["total_bytes"] for item in measured.values())
    small_install = affected_total < SMALL_INSTALL_BYTES
    validated = copy.deepcopy(record)

    # The small-install path may synthesize size measurements only.  It never
    # waives the operator's pass/fail decisions or recovery rehearsal.
    _require_value(validated, "disk_preflight", "pass")
    _require_value(validated, "memory_preflight", "pass")
    _require_value(validated, "scheduler_inventory", "complete")

    for field in (
        "backup_artifact_path",
        "restore_dsn_target",
        "lock_timeout",
        "statement_timeout",
        "resume_vs_restore_threshold",
    ):
        _required_text(validated, field)
    for field in ("measured_backup_time_s", "measured_restore_time_s"):
        _positive_number(validated, field)

    archive_mode = _required_text(validated, "archive_mode")
    if archive_mode not in ("on", "off"):
        raise PreflightError("preflight archive_mode must be 'on' or 'off'")
    if archive_mode == "on":
        _require_value(validated, "archive_command_healthy", "yes")

    if small_install:
        validated["tables"] = copy.deepcopy(measured)
        validated["affected_total_bytes"] = affected_total
    else:
        _positive_int(validated, ("affected_total_bytes",))
        tables = validated.get("tables")
        if not isinstance(tables, dict):
            raise PreflightError("preflight tables is missing")
        for table in AFFECTED_TABLES:
            _positive_int(validated, ("tables", table, "total_bytes"))

    # Size auto-generation never exempts actual free-space measurements or
    # their independent headroom decisions.  Each volume has the required-byte
    # field named by the design's resource schema.
    required_field_aliases = {
        "db_data_volume": ("required_free_bytes",),
        "wal_volume": ("required_free_bytes", "wal_required_free_bytes"),
        "temp_volume": ("required_free_bytes", "expected_temp_bytes"),
        "backup_volume": ("required_free_bytes",),
    }
    volumes = validated.get("volumes")
    if not isinstance(volumes, dict):
        raise PreflightError("preflight volumes is missing")
    for volume, aliases in required_field_aliases.items():
        volume_record = volumes.get(volume)
        if not isinstance(volume_record, dict):
            raise PreflightError(f"preflight volumes.{volume} is missing")
        required_field = next(
            (field for field in aliases if field in volume_record),
            aliases[0],
        )
        actual_free = _positive_int(
            validated, ("volumes", volume, "actual_free_bytes")
        )
        required_free = _nonnegative_int(
            validated, ("volumes", volume, required_field)
        )
        _require_value(volume_record, "preflight", "pass")
        if actual_free < required_free:
            raise PreflightError(
                f"preflight volumes.{volume}.actual_free_bytes must be >= "
                f"volumes.{volume}.{required_field}"
            )

    return PreflightValidation(
        record=validated,
        small_install=small_install,
        affected_total_bytes=affected_total,
    )


def warn_optional_gate_artifacts(preflight_path: Path) -> None:
    """Hard-reject missing load-bearing kickoff artifacts.

    The historical name is retained for callers/tests, but this is no longer a
    warning-only check.
    """
    siblings = {
        "scheduler inventory": preflight_path.with_name(
            "company_multiuser_scheduler_inventory.json"
        ),
        "watcher PID": preflight_path.with_name("company_multiuser_watcher.pid"),
        "watcher script": Path(__file__).resolve().parents[2]
        / "scripts"
        / "watch_migration_heartbeat.py",
    }
    for label, path in siblings.items():
        if not path.is_file():
            raise PreflightError(
                f"company-multiuser {label} artifact not present at {path}"
            )

    scheduler = load_preflight_record(siblings["scheduler inventory"])
    _require_value(scheduler, "scheduler_inventory", "complete")


def _checkpoint_row(conn, stage: str) -> tuple[object, object] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.to_regclass("
            "'public.hippocampus_migration_stages')"
        )
        if cur.fetchone()[0] is None:
            return None
        cur.execute(
            """SELECT completed_at, detail
                 FROM public.hippocampus_migration_stages
                WHERE stage = %s""",
            (stage,),
        )
        return cur.fetchone()


def checkpoint_complete(
    conn, stage: str, identity: DatabaseIdentity
) -> bool:
    """Require a same-DB stage row and independently derivable DB facts."""
    row = _checkpoint_row(conn, stage)
    if not row:
        return False
    completed_at, detail = row
    if completed_at is None:
        return False
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return False
    if not isinstance(detail, dict):
        return False
    if any(detail.get(k) != v for k, v in identity.as_dict().items()):
        return False
    if detail.get("backfill_ran") is not True:
        return False

    if stage == INITIAL_CHECKPOINT:
        predicate = "updated_at IS NULL OR source_identity_hash IS NULL"
    elif stage == "multiuser_gap_window_backfill_complete":
        predicate = "source_identity_hash IS NULL"
    else:
        # A manifest typo or future checkpoint must gain an explicit fact probe.
        return False
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT pg_catalog.count(*) FROM personal.conversations "
            f"WHERE {predicate}"
        )
        return int(cur.fetchone()[0]) == 0


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _connect(dsn: str):
    import psycopg2  # local import: keep module importable without driver

    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.OperationalError as exc:
        # psycopg2 errors can reproduce the full DSN (incl. a re-encoded
        # password libpq may echo) — scrub via the shared hardened helper,
        # not a plain redact_dsn string-replace.
        raise SystemExit(
            f"cannot connect: {format_pg_error(dsn, exc)}"
        ) from None
    conn.autocommit = True
    return conn


def ensure_ledger(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                    filename   TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW(),
                    sha256     TEXT
                )"""
        )


def fetch_applied(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT filename, sha256 FROM {LEDGER_TABLE}")
        return {fn: sha for fn, sha in cur.fetchall()}


def record_applied(conn, filename: str, sha: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {LEDGER_TABLE} (filename, sha256) VALUES (%s, %s) "
            f"ON CONFLICT (filename) DO NOTHING",
            (filename, sha),
        )


def set_maintenance_freeze(conn, enabled: bool) -> None:
    """Durably arm or release the gated migration write barrier."""
    with conn.cursor() as cur:
        if enabled:
            cur.execute(
                """INSERT INTO personal.feature_flags (flag_name, enabled)
                   VALUES ('maintenance_freeze', TRUE)
                   ON CONFLICT (flag_name) DO UPDATE SET enabled = TRUE"""
            )
        else:
            cur.execute(
                """UPDATE personal.feature_flags
                      SET enabled = FALSE
                    WHERE flag_name = 'maintenance_freeze'"""
            )


def acquire_company_multiuser_lock(conn) -> bool:
    """Try to serialize the session-owned company-multiuser gated runner."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.pg_try_advisory_lock(%s)",
            (COMPANY_MULTIUSER_ADVISORY_LOCK_KEY,),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_file(path: Path, no_tx: bool, pg_env: dict[str, str]) -> int:
    """Run one migration file through psql (fresh session per file)."""
    cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    if not no_tx:
        cmd.append("-1")
    cmd += ["-f", str(path)]
    proc = subprocess.run(cmd, env=pg_env, capture_output=True, text=True)
    # psql output never contains the DSN; pass stderr through (NOTICEs +
    # errors — 013/014 DO-block remediation messages must surface verbatim).
    if proc.stdout.strip():
        sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)
    return proc.returncode


def select_entries(entries: list[Entry], with_library: bool,
                   include_optional: bool,
                   with_multiuser: bool = False,
                   with_code: bool = False) -> list[Entry]:
    tiers = {"core"}
    if with_library:
        tiers.add("library")
    if include_optional:
        tiers.add("optional")
    if with_multiuser:
        tiers.add("multiuser")
    if with_code:
        tiers.add("code")
    return [e for e in entries if e.tier in tiers]


def print_status(entries: list[Entry], applied: dict[str, str]) -> None:
    width = max(len(e.file) for e in entries)
    print(f"{'migration':<{width}}  {'tier':<8}  {'no_tx':<5}  state")
    print("-" * (width + 35))
    for e in entries:
        if e.file in applied:
            state = "applied"
        elif e.requires_checkpoint:
            state = f"held (awaiting checkpoint {e.requires_checkpoint})"
        else:
            state = "pending"
        print(f"{e.file:<{width}}  {e.tier:<8}  {str(e.no_tx).lower():<5}  {state}")
    n_applied = sum(1 for e in entries if e.file in applied)
    print(f"\n{n_applied}/{len(entries)} applied "
          f"(ledger also holds {len(set(applied) - {e.file for e in entries})} "
          f"row(s) not in the manifest)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hippocampus migrate",
        description="Apply migrations/manifest.yaml in order with a ledger.",
    )
    ap.add_argument("--core-only", action="store_true",
                    help="apply core tier only (this is the default)")
    ap.add_argument("--with-library", action="store_true",
                    help="also apply library-tier migrations")
    ap.add_argument("--include-optional", action="store_true",
                    help="also apply optional-tier migrations (009b HNSW)")
    ap.add_argument("--with-multiuser", action="store_true",
                    help="also apply multiuser-tier migrations")
    ap.add_argument("--with-code", action="store_true",
                    help="also apply code-tier migrations (deja-code index)")
    ap.add_argument("--company-multiuser-preflight", metavar="RECORD",
                    help="validate RECORD and allow company-multiuser gated "
                         "migrations (requires --with-multiuser)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would run, change nothing")
    ap.add_argument("--status", action="store_true",
                    help="print applied/pending table and exit")
    ap.add_argument("--baseline", action="store_true",
                    help="record selected-tier pending migrations as applied "
                         "in the ledger WITHOUT executing any SQL (adopt the "
                         "runner on a pre-ledger install whose schema already "
                         "holds those migrations)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive --baseline confirmation prompt")
    ap.add_argument("--db-url", default=None,
                    help="override target DSN (prefer HIPPOCAMPUS_MIGRATE_DB "
                         "env var to keep DSNs out of argv)")
    ap.add_argument("--no-local", action="store_true",
                    help="skip the operator overlay (migrations.local/*.sql), "
                         "which is otherwise auto-applied after the core tier "
                         "to restore operator-local seed data on a rebuild")
    ap.add_argument("--manifest", default=None, help=argparse.SUPPRESS)
    return ap


def apply_local_overlay(repo_root: Path, pg_env: dict, *, dry_run: bool) -> int:
    """Apply operator-local overlay SQL after the tracked migrations.

    migrations.local/ is gitignored (operator-only, e.g. the sensitive-path
    inject denylist seed extracted from 014, r1-privacy-5). Files must be
    idempotent (ON CONFLICT DO NOTHING). A non-interactive `hippocampus
    migrate` on a rebuilt operator DB restores these without needing the
    interactive `hippocampus init` prompt. No-op (silent) when the dir is
    absent — public installs never have it. Not ledgered (overlays re-run
    cheaply and may change between rebuilds).
    """
    local_dir = repo_root / "migrations.local"
    if not local_dir.is_dir():
        return 0
    files = sorted(local_dir.glob("*.sql"))
    if not files:
        return 0
    for f in files:
        if dry_run:
            print(f"would apply [local] {f.name}")
            continue
        print(f"apply   [local] {f.name}")
        rc = run_file(f, False, pg_env)
        if rc != 0:
            print(f"FAILED: operator overlay {f.name} (psql exit {rc})",
                  file=sys.stderr)
            return rc
    return 0


def run_baseline(conn, selected: list[Entry], applied: dict[str, str],
                 n_total: int, migrations_dir: Path,
                 dry_run: bool, assume_yes: bool) -> int:
    """Stamp pending selected entries into the ledger without running SQL."""
    pending = [e for e in selected if e.file not in applied]
    if not pending:
        print(f"baseline: nothing to do — all {len(selected)} selected "
              f"entries already in the ledger")
        return 0
    print(f"baseline: would mark {len(pending)} pending file(s) as applied "
          f"WITHOUT executing any SQL "
          f"(selected {len(selected)}/{n_total} manifest entries):")
    for e in pending:
        print(f"  stamp [{e.tier:<8}] {e.file}")
    if dry_run:
        print("dry-run: ledger unchanged")
        return 0
    gated = [e.file for e in pending if e.preflight == "company_multiuser"]
    if gated:
        print("baseline: refusing to stamp preflight: company_multiuser "
              "migration(s) without the company-multiuser gate artifacts: "
              f"{', '.join(gated)}", file=sys.stderr)
        return 1
    if not assume_yes:
        if not sys.stdin.isatty():
            print("baseline: refusing without confirmation (stdin is not a "
                  "TTY); pass --yes to proceed non-interactively",
                  file=sys.stderr)
            return 1
        reply = input("Type 'baseline' to confirm stamping these as applied: ")
        if reply.strip() != "baseline":
            print("baseline: aborted (no confirmation)", file=sys.stderr)
            return 1
    for e in pending:
        sha = sha256_file(migrations_dir / e.file)
        record_applied(conn, e.file, sha)
        print(f"baselined {e.file} (sha256 {sha[:12]}…)")
    print(f"done: baselined {len(pending)}, "
          f"already-applied {len(selected) - len(pending)} — no SQL executed")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.core_only and args.with_library:
        ap.error("--core-only and --with-library are mutually exclusive")
    if args.core_only and args.with_multiuser:
        ap.error("--core-only and --with-multiuser are mutually exclusive")
    if args.core_only and args.with_code:
        ap.error("--core-only and --with-code are mutually exclusive")
    if args.baseline and args.status:
        ap.error("--baseline and --status are mutually exclusive")
    gated_mode = args.company_multiuser_preflight is not None
    if gated_mode and not args.with_multiuser:
        ap.error("--company-multiuser-preflight requires --with-multiuser")
    if gated_mode and args.baseline:
        ap.error("--company-multiuser-preflight is an apply gate, not a "
                 "--baseline bypass")

    manifest_path = Path(args.manifest) if args.manifest else MIGRATIONS_DIR / MANIFEST_NAME
    migrations_dir = manifest_path.parent
    try:
        entries = parse_manifest(manifest_path)
        selected = select_entries(
            entries,
            args.with_library,
            args.include_optional,
            args.with_multiuser,
            args.with_code,
        )
        validate_required_gate_attributes(selected)
    except (ManifestError, OSError) as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 1

    missing = [e.file for e in entries if not (migrations_dir / e.file).is_file()]
    if missing:
        print(f"manifest references missing file(s): {', '.join(missing)}",
              file=sys.stderr)
        return 1

    dsn = resolve_db_url(args.db_url)
    print(f"target: {redact_dsn(dsn)}")

    preflight_path: Path | None = None
    preflight_record: dict[str, object] | None = None
    identity: DatabaseIdentity | None = None
    if gated_mode:
        preflight_path = Path(args.company_multiuser_preflight).expanduser()
        try:
            identity = dsn_identity(dsn)
            preflight_record = load_preflight_record(preflight_path)
            # This check is deliberately before _connect/ensure_ledger: a
            # record for staging cannot cause a write to production.
            validate_record_identity(preflight_record, identity)
        except PreflightError as exc:
            print(f"company-multiuser preflight refused: {exc}", file=sys.stderr)
            return 1

    maintenance_freeze_armed = False
    initial_gate_complete = False
    conn = _connect(dsn)
    try:
        if gated_mode:
            assert preflight_record is not None
            assert preflight_path is not None
            assert identity is not None
            if not acquire_company_multiuser_lock(conn):
                print(
                    "company-multiuser migration refused: another gated "
                    "migration is already running",
                    file=sys.stderr,
                )
                return 1
            try:
                measured = measure_affected_tables(conn)
                validation = validate_preflight_record(
                    preflight_record, identity, measured
                )
                warn_optional_gate_artifacts(preflight_path)
            except PreflightError as exc:
                print(
                    f"company-multiuser preflight refused: {exc}",
                    file=sys.stderr,
                )
                return 1
            if validation.small_install:
                print(
                    "company-multiuser preflight: small-install auto-size "
                    f"path ({validation.affected_total_bytes} bytes live)"
                )
            else:
                print(
                    "company-multiuser preflight: captured-size path "
                    f"({validation.affected_total_bytes} bytes live)"
                )

        ensure_ledger(conn)
        applied = fetch_applied(conn)
        initial_gate_complete = (
            "031b_multiuser_backfill_ddl.sql" in applied
        )

        if args.status:
            print_status(entries, applied)
            return 0

        if args.baseline:
            # No SQL is executed in baseline mode, so psql is not required.
            return run_baseline(conn, selected, applied, len(entries),
                                migrations_dir, args.dry_run, args.yes)

        # The pre-existing plain path remains fail-closed.  Only the positive
        # gated branch above can authorize tagged entries.
        if not gated_mode:
            gated = [
                e.file for e in selected
                if e.file not in applied
                and (e.preflight == "company_multiuser" or e.requires_checkpoint)
            ]
            if gated:
                print("REFUSING to run gated migration(s) without the "
                      "company-multiuser preflight/checkpoint validator: "
                      f"{', '.join(gated)}", file=sys.stderr)
                return 1

        if shutil.which("psql") is None:
            print("error: `psql` not found on PATH — it is a hard prerequisite "
                  "(no-tx migrations are delegated to a psql subprocess). "
                  "Install postgresql-client and retry.", file=sys.stderr)
            return 1

        # Belt-and-suspenders scan (r3-schema-1/2) before touching anything.
        for e in selected:
            constructs = scan_no_tx_constructs((migrations_dir / e.file).read_text(encoding="utf-8"))
            if constructs and not e.no_tx:
                print(f"REFUSING to run: {e.file} is marked no_tx=false in the "
                      f"manifest but contains transaction-forbidden construct(s): "
                      f"{', '.join(sorted(set(constructs)))}. Fix the manifest.",
                      file=sys.stderr)
                return 1
            if e.no_tx and not constructs:
                print(f"warning: {e.file} is marked no_tx=true but the scanner "
                      f"found no transaction-forbidden construct (file will run "
                      f"without single-transaction atomicity)", file=sys.stderr)

        pg_env = dsn_to_pg_env(dsn)
        n_run = n_skip = 0
        held_stage: str | None = None
        artifacts = BackfillArtifacts()
        if gated_mode:
            assert preflight_path is not None
            # SECURITY: the freeze/lock/checkpoints run on `conn`; the DDL runs
            # via psql with `pg_env`. Refuse before arming anything if they are
            # not provably the same database (see assert_gated_target_matches).
            assert_gated_target_matches(conn, pg_env)
            artifacts = BackfillArtifacts(
                heartbeat=preflight_path.with_name(
                    "company_multiuser_migration.json"
                ),
                checkpoints=preflight_path.with_name(
                    "company_multiuser_checkpoints.jsonl"
                ),
            )
            gated_entries = [
                e for e in selected
                if e.preflight == "company_multiuser" or e.requires_checkpoint
            ]
            if (
                not args.dry_run
                and gated_entries
                and all(e.file in applied for e in gated_entries)
            ):
                # Everything gated is already applied; this is a no-op re-run. A
                # prior run may have crashed after the ledger write but before
                # releasing the freeze, so ensure it is released (idempotent:
                # setting FALSE is a no-op when already released). Do NOT arm
                # here — arming on an otherwise-idle re-run would transiently
                # reject every application write for the duration of this call.
                set_maintenance_freeze(conn, False)
        for e in selected:
            path = migrations_dir / e.file
            sha = sha256_file(path)
            if e.file in applied:
                if applied[e.file] and applied[e.file] != sha:
                    print(f"warning: {e.file} already applied but file content "
                          f"changed since (sha256 mismatch); migration files are "
                          f"immutable by convention — skipping, NOT re-applying",
                          file=sys.stderr)
                else:
                    print(f"skip    {e.file} (applied)")
                n_skip += 1
                continue

            if (
                gated_mode
                and not args.dry_run
                and not maintenance_freeze_armed
                and (e.preflight == "company_multiuser" or e.requires_checkpoint)
            ):
                set_maintenance_freeze(conn, True)
                maintenance_freeze_armed = True

            if gated_mode and e.requires_checkpoint:
                assert identity is not None
                complete = checkpoint_complete(
                    conn, e.requires_checkpoint, identity
                )
                if (
                    not complete
                    and e.requires_checkpoint == INITIAL_CHECKPOINT
                    and not args.dry_run
                ):
                    print("backfill [multiuser] conversations before "
                          f"{e.file}")
                    try:
                        run_initial_backfills(
                            conn, identity, artifacts=artifacts
                        )
                    except Exception as exc:
                        print(
                            "FAILED: company-multiuser backfill executor: "
                            f"{exc}",
                            file=sys.stderr,
                        )
                        return 1
                    complete = checkpoint_complete(
                        conn, e.requires_checkpoint, identity
                    )
                if not complete:
                    print(
                        f"hold    {e.file} (awaiting checkpoint "
                        f"{e.requires_checkpoint})"
                    )
                    held_stage = e.requires_checkpoint
                    break

            mode = "no-tx" if e.no_tx else "tx"
            if args.dry_run:
                print(f"would run [{mode:5}] {e.file}")
                n_run += 1
                continue
            print(f"apply   [{mode:5}] {e.file}")
            rc = run_file(path, e.no_tx, pg_env)
            if rc != 0:
                print(f"FAILED: {e.file} (psql exit {rc}). Nothing recorded in "
                      f"the ledger for this file; fix the cause and rerun. "
                      f"If a CONCURRENTLY index build died it may be INVALID — "
                      f"the file's own DO-block check prints the remediation.",
                      file=sys.stderr)
                return 1
            record_applied(conn, e.file, sha)
            applied[e.file] = sha
            if e.file == "031b_multiuser_backfill_ddl.sql":
                initial_gate_complete = True
            n_run += 1

        # Release as soon as the initial backfill + 031b have durably completed,
        # before any operator overlay can return early.  The outer finally is a
        # second safety net for later in-loop failures (for example 032).
        if maintenance_freeze_armed and initial_gate_complete:
            set_maintenance_freeze(conn, False)
            maintenance_freeze_armed = False

        # Operator-local overlay (migrations.local/) — restores seed data a
        # public install never has; no-op when the dir is absent (r1-privacy-5).
        if not args.no_local:
            rc = apply_local_overlay(migrations_dir.parent, pg_env,
                                     dry_run=args.dry_run)
            if rc != 0:
                return 1

        verb = "would apply" if args.dry_run else "applied"
        held = f", held at {held_stage}" if held_stage else ""
        print(f"done: {verb} {n_run}, skipped {n_skip}{held}, "
              f"selected {len(selected)}/{len(entries)} manifest entries")
        return 0
    finally:
        try:
            if maintenance_freeze_armed and initial_gate_complete:
                set_maintenance_freeze(conn, False)
                maintenance_freeze_armed = False
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
