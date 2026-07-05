"""Manifest-driven migration runner (epic #43 Phase 3, plan §3.6).

Usage (module-callable; CLI wiring into `hippocampus migrate` lands
separately):

    python3 -m hippocampus.migrate [--core-only] [--with-library]
                                   [--include-optional] [--dry-run]
                                   [--status] [--baseline [--yes]]
                                   [--db-url DSN]

Design decisions (binding, from plan §3.6 + dual-magi round 3):

- ``migrations/manifest.yaml`` is the single source of truth for order,
  tier (core / library / optional) and the ``no_tx`` flag. ``*_down.sql``
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
  ``--include-optional``) to control which entries are stamped. Because
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
import hashlib
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

VALID_TIERS = ("core", "library", "optional")

# Constructs that forbid running inside a transaction block.
_NO_TX_RE = re.compile(
    r"\b(?:CREATE|DROP)\s+INDEX\s+CONCURRENTLY\b"
    r"|\bALTER\s+TYPE\s+\S+\s+ADD\s+VALUE\b",
    re.IGNORECASE,
)


class ManifestError(ValueError):
    """Raised when manifest.yaml violates the documented line grammar."""


@dataclass
class Entry:
    file: str
    tier: str = ""
    no_tx: Optional[bool] = None
    note: str = ""


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
        else:
            raise ManifestError(f"{path.name}:{lineno}: unknown field {key!r}")
    seen: set[str] = set()
    for e in entries:
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
                   include_optional: bool) -> list[Entry]:
    tiers = {"core"}
    if with_library:
        tiers.add("library")
    if include_optional:
        tiers.add("optional")
    return [e for e in entries if e.tier in tiers]


def print_status(entries: list[Entry], applied: dict[str, str]) -> None:
    width = max(len(e.file) for e in entries)
    print(f"{'migration':<{width}}  {'tier':<8}  {'no_tx':<5}  state")
    print("-" * (width + 35))
    for e in entries:
        state = "applied" if e.file in applied else "pending"
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
    if args.baseline and args.status:
        ap.error("--baseline and --status are mutually exclusive")

    manifest_path = Path(args.manifest) if args.manifest else MIGRATIONS_DIR / MANIFEST_NAME
    migrations_dir = manifest_path.parent
    try:
        entries = parse_manifest(manifest_path)
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

    conn = _connect(dsn)
    try:
        ensure_ledger(conn)
        applied = fetch_applied(conn)

        if args.status:
            print_status(entries, applied)
            return 0

        if args.baseline:
            # No SQL is executed in baseline mode, so psql is not required.
            selected = select_entries(entries, args.with_library,
                                      args.include_optional)
            return run_baseline(conn, selected, applied, len(entries),
                                migrations_dir, args.dry_run, args.yes)

        if shutil.which("psql") is None:
            print("error: `psql` not found on PATH — it is a hard prerequisite "
                  "(no-tx migrations are delegated to a psql subprocess). "
                  "Install postgresql-client and retry.", file=sys.stderr)
            return 1

        selected = select_entries(entries, args.with_library, args.include_optional)

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
            n_run += 1

        # Operator-local overlay (migrations.local/) — restores seed data a
        # public install never has; no-op when the dir is absent (r1-privacy-5).
        if not args.no_local:
            rc = apply_local_overlay(migrations_dir.parent, pg_env,
                                     dry_run=args.dry_run)
            if rc != 0:
                return 1

        verb = "would apply" if args.dry_run else "applied"
        print(f"done: {verb} {n_run}, skipped {n_skip}, "
              f"selected {len(selected)}/{len(entries)} manifest entries")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
