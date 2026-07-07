"""`hippocampus init` — interactive first-run setup (epic #43 Phase 3).

Responsibilities (plan §3.3 / Phase 3; binding findings r1-privacy-8/9):
- Collect PG URL (hidden input, never echoed), embed provider (explicit
  3-way choice, NO default — privacy stance: nothing egresses or downloads
  6GB without an explicit decision), optional ghost reader provisioning,
  optional sensitive-path denylist entries.
- Write `.env` atomically with mode 0600, via a STRICT dotenv serializer
  (double-quoted, backslash/quote/newline escaped, control chars refused,
  round-trip verified against python-dotenv before anything touches disk).
- Confirm the target database (redacted host:port/dbname — never userinfo)
  before running migrations; migrations run via `hippocampus.migrate`
  (imported lazily; degrades to "run `hippocampus migrate` next" if the
  module is not present yet).
- Print a secret-free `~/.claude/settings.json` snippet at the end.

Database is LOCAL-FIRST (2026-06-12 user direction): the default mode
constructs the DSN for the bundled docker-compose postgres (one generated
PG_PASSWORD shared between compose and PG_URL via the same .env) and offers
to start it; `existing` mode covers a self-managed server, local or remote.

Every interactive prompt is also answerable via a flag so the whole flow
can run non-interactively (CI / scripted installs):

    hippocampus init --yes --embed none              # local-first default
    hippocampus init --db existing --pg-url-env MY_PG_URL --embed none --yes
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import io
import os
import re
import secrets as _secrets
import shutil
import sys
import tempfile
import urllib.parse

from .config import redact_dsn

ENV_FILE = ".env"

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Escaped inside double quotes; everything else in C0/C1 + DEL is refused.
_ESCAPABLE = {"\\": "\\\\", '"': '\\"', "\n": "\\n"}

EMBED_CHOICES = ("bge-http", "bge-inprocess", "none")

GHOST_ROLE = "agent_read_mcp"
DENYLIST_TABLE = "personal.conversation_inject_excluded_paths"
_PATH_PREFIX_RE = re.compile(r"^/.*/$")


class InitError(RuntimeError):
    """Fatal init failure (bad input, refused value, write conflict)."""


# ── strict dotenv serializer (r1-privacy-9) ─────────────────────────────

def _check_no_control_chars(text: str, what: str) -> None:
    for ch in text:
        if ch == "\n":
            continue  # newline in values is escaped, not refused
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise InitError(
                f"{what} contains a control character (U+{code:04X}); refusing"
            )


def serialize_env(values: dict[str, str]) -> str:
    """Serialize key/value pairs to dotenv text. Strict: no f-string assembly.

    Values are always double-quoted; backslash, double-quote and newline are
    escaped. Any other control character (in key or value) is refused —
    there is no way to round-trip them safely through every dotenv parser.
    """
    lines = ["# hippocampus-mcp configuration — written by `hippocampus init`",
             "# Keep mode 0600. Process env always wins over this file."]
    for key, value in values.items():
        if not _KEY_RE.match(key):
            # also rejects any control char (incl. newline) in the key
            raise InitError(f"invalid env key name: {key!r}")
        _check_no_control_chars(value, f"value of {key}")
        if "${" in value:
            # python-dotenv interpolates ${VAR} even inside double quotes;
            # such a value cannot round-trip, so refuse instead of mangling.
            raise InitError(
                f"value of {key} contains '${{' (dotenv interpolation "
                "syntax) — cannot be stored faithfully in .env; refusing"
            )
        escaped = "".join(_ESCAPABLE.get(ch, ch) for ch in value)
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + "\n"


def _roundtrip_check(values: dict[str, str], text: str) -> None:
    """Self-check: python-dotenv must parse back exactly what we wrote."""
    from dotenv import dotenv_values  # stdlib-adjacent: already a core dep

    parsed = dict(dotenv_values(stream=io.StringIO(text)))
    if parsed != values:
        diff = sorted(set(values) ^ set(parsed)) or [
            k for k in values if parsed.get(k) != values[k]
        ]
        raise InitError(
            "dotenv round-trip check failed for key(s): "
            + ", ".join(diff)
            + " — refusing to write .env (values not shown)"
        )


def write_env_file(path: str, values: dict[str, str], *,
                   overwrite_ok: bool = False, assume_yes: bool = False) -> None:
    """Write `path` atomically with mode 0600.

    First attempt is O_CREAT|O_EXCL on the final path (fresh install fast
    path, also our existence check). If the file already exists, ask before
    overwriting, then go through a same-directory 0600 temp file +
    os.replace so a crash never leaves a partial .env.
    """
    text = serialize_env(values)
    _roundtrip_check(values, text)
    data = text.encode("utf-8")

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not overwrite_ok and not _confirm(
                f"{path} already exists — overwrite?", assume_yes=assume_yes):
            raise InitError(f"refusing to overwrite existing {path}")
    else:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return

    # Overwrite path: atomic replace via 0600 temp file in the same dir.
    dirname = os.path.dirname(os.path.abspath(path)) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".env.", dir=dirname)
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── prompting helpers (every prompt has a flag twin) ─────────────────────

def _confirm(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _require_tty(what: str) -> None:
    if not sys.stdin.isatty():
        raise InitError(
            f"{what} required but stdin is not a TTY — "
            "pass the corresponding flag (see `hippocampus init --help`)"
        )


LOCAL_DB_USER = "hippocampus"
LOCAL_DB_NAME = "hippocampus"
LOCAL_DB_PORT_DEFAULT = 5432


def _local_db_password(env_file: str) -> str:
    """Reuse PG_PASSWORD from process env or an existing .env, else generate.

    The same .env drives BOTH the compose postgres service (POSTGRES_PASSWORD:
    ${PG_PASSWORD}) and the PG_URL we construct — one secret, one file.
    """
    if os.environ.get("PG_PASSWORD"):
        return os.environ["PG_PASSWORD"]
    if os.path.exists(env_file):
        from dotenv import dotenv_values  # noqa: PLC0415

        existing = dotenv_values(env_file).get("PG_PASSWORD")
        if existing:
            return existing
    return _secrets.token_urlsafe(18)


def _gather_db(args: argparse.Namespace) -> tuple[str, dict[str, str]]:
    """Return (pg_url, extra_env_values).

    Local-first (2026-06-12 user direction): the default is a docker-compose
    PostgreSQL on this machine — the DSN is constructed, never typed. The
    `existing` mode covers a self-managed server, local or remote.
    """
    if args.pg_url_env:
        dsn = os.environ.get(args.pg_url_env, "")
        if not dsn:
            raise InitError(f"env var {args.pg_url_env} is empty or unset")
        return _validate_dsn(dsn), {}

    mode = args.db
    if not mode:
        if args.yes or not sys.stdin.isatty():
            mode = "local"  # non-interactive default = the recommended path
        else:
            print("Where should the database live?")
            print("  1) local     docker compose postgres on this machine (recommended)")
            print("  2) existing  I already run PostgreSQL (local or remote)")
            raw = input("database [1]: ").strip().lower()
            mode = {"": "local", "1": "local", "local": "local",
                    "2": "existing", "existing": "existing"}.get(raw, "")
            if not mode:
                raise InitError("please answer 1/2 (or local/existing)")

    if mode == "existing":
        _require_tty("PG URL prompt")
        dsn = getpass.getpass(
            "PostgreSQL URL (input hidden, e.g. "
            "postgresql://user:pass@localhost:5432/hippocampus): ").strip()
        if not dsn:
            raise InitError("empty PG URL")
        return _validate_dsn(dsn), {}

    password = _local_db_password(args.env_file)
    port = args.pg_port or int(os.environ.get(
        "HIPPOCAMPUS_PG_PORT", LOCAL_DB_PORT_DEFAULT))
    dsn = (f"postgresql://{LOCAL_DB_USER}:{urllib.parse.quote(password, safe='')}"
           f"@localhost:{port}/{LOCAL_DB_NAME}")
    # PG_PASSWORD (+ non-default port) land in .env so `docker compose up`
    # reads the same values.
    extra = {"PG_PASSWORD": password}
    if port != LOCAL_DB_PORT_DEFAULT:
        extra["HIPPOCAMPUS_PG_PORT"] = str(port)
    return dsn, extra


def _validate_dsn(dsn: str) -> str:
    parts = urllib.parse.urlsplit(dsn)
    if not parts.scheme.startswith("postgres"):
        raise InitError(
            "PG URL must be a postgresql:// URL (value not shown)")
    return dsn


def _ensure_local_pg(pg_url: str, *, assume_yes: bool,
                     compose_env: dict[str, str] | None = None) -> bool:
    """Local mode: make sure the compose postgres is reachable before migrate.

    Returns True when a connection succeeds. Offers `docker compose up -d
    postgres` when docker + compose.yaml are present, then polls readiness.
    """
    import time  # noqa: PLC0415

    import psycopg2  # noqa: PLC0415

    def _reachable() -> bool:
        try:
            psycopg2.connect(pg_url, connect_timeout=3).close()
            return True
        except Exception:
            return False

    if _reachable():
        return True
    if shutil.which("docker") and os.path.exists("compose.yaml"):
        if _confirm("Local postgres is not reachable — run "
                    "`docker compose up -d postgres` now?", assume_yes=assume_yes):
            import subprocess  # noqa: PLC0415

            # PG_PASSWORD travels via the subprocess env, not argv — and not
            # via the .env file, which may live elsewhere (--env-file).
            rc = subprocess.run(
                ["docker", "compose", "up", "-d", "postgres"],
                env={**os.environ, **(compose_env or {})}).returncode
            if rc != 0:
                print("docker compose failed — if the error says the port is "
                      "already in use, re-run init with --pg-port <free-port>; "
                      "otherwise start postgres manually and run "
                      "`hippocampus migrate`", file=sys.stderr)
                return False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if _reachable():
                    print("local postgres is up")
                    return True
                time.sleep(2)
            print("postgres did not become reachable within 60s — "
                  "check `docker compose logs postgres`", file=sys.stderr)
            return False
    print("local postgres is not reachable. Start it (docker compose up -d "
          "postgres) and run `hippocampus migrate` next", file=sys.stderr)
    return False


def _gather_embed(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (choice, bge_url, bge_token). No default — explicit choice only."""
    choice = args.embed
    if not choice:
        _require_tty("embed provider choice")
        print("Semantic embedding backend — choose explicitly (no default):")
        print("  1) bge-http       remote BGE-M3 HTTP server (BGE_EMBED_URL)")
        print("  2) bge-inprocess  local in-process model (~6GB download/RAM,")
        print("                    requires `pip install hippocampus-mcp[bge-local]`)")
        print("  3) none           semantic tools stay off (keyword search only)")
        mapping = {"1": "bge-http", "2": "bge-inprocess", "3": "none"}
        while True:
            raw = input("embed backend [1/2/3]: ").strip().lower()
            choice = mapping.get(raw, raw if raw in EMBED_CHOICES else "")
            if choice:
                break
            print("please answer 1, 2 or 3 (or bge-http / bge-inprocess / none)")
    if choice not in EMBED_CHOICES:
        raise InitError(f"--embed must be one of {'/'.join(EMBED_CHOICES)}")

    bge_url, bge_token = "", ""
    if choice == "bge-http":
        bge_url = args.bge_url or ""
        if not bge_url:
            _require_tty("BGE URL prompt")
            bge_url = input("BGE embed server URL (e.g. http://localhost:8086): ").strip()
        if not bge_url:
            raise InitError("bge-http selected but no BGE URL given (--bge-url)")
        if args.bge_token_env:
            bge_token = os.environ.get(args.bge_token_env, "")
        elif sys.stdin.isatty():
            bge_token = getpass.getpass(
                "BGE embed token (input hidden, empty if none): ").strip()
    return choice, bge_url.rstrip("/"), bge_token


def _gather_exclude_paths(args: argparse.Namespace) -> list[str]:
    prefixes: list[str] = list(args.exclude_path or [])
    if not prefixes and not args.yes and sys.stdin.isatty():
        print("Sensitive-path denylist: conversations whose cwd starts with a")
        print("listed prefix are never summarized into SessionStart injects.")
        while True:
            raw = input("  add absolute path prefix (empty line to finish): ").strip()
            if not raw:
                break
            prefixes.append(raw)
    normalized = []
    for p in prefixes:
        if not p.startswith("/"):
            raise InitError(f"denylist prefix must be absolute: {p!r}")
        if not p.endswith("/"):
            p += "/"
        if not _PATH_PREFIX_RE.match(p):
            raise InitError(f"invalid denylist prefix: {p!r}")
        normalized.append(p)
    return normalized


# ── DB-side steps ────────────────────────────────────────────────────────

def _run_migrations(pg_url: str, *, assume_yes: bool) -> bool:
    """Confirm target, then run hippocampus.migrate lazily. Returns success."""
    print(f"\nTarget database: {redact_dsn(pg_url)}")
    if not _confirm("Run migrations against this database?", assume_yes=assume_yes):
        print("migrations skipped — run `hippocampus migrate` when ready")
        return False
    if importlib.util.find_spec("hippocampus.migrate") is None:
        print("migration runner not installed yet — run `hippocampus migrate` next")
        return False
    from . import migrate  # noqa: PLC0415 — lazy: module may be absent

    # Pass the target explicitly: relying on Settings/cwd-.env here would
    # break --env-file installs and, worse, let a process-env PG_URL (e.g.
    # an operator secrets wrapper) silently win over the DB init just set up.
    # In-process argv is a Python list — never visible in any process table.
    rc = migrate.main(["--db-url", pg_url])
    if rc not in (0, None):
        print(f"migrations exited with status {rc} — fix and re-run "
              "`hippocampus migrate`", file=sys.stderr)
        return False
    print("migrations applied")
    return True


def _provision_ghost_reader(pg_url: str) -> str:
    """ALTER ROLE agent_read_mcp with a fresh random password.

    Returns the derived PG_URL_AGENT_READ_MCP DSN. The password itself is
    never printed; it only lands inside the 0600 .env.
    """
    import psycopg2  # noqa: PLC0415
    from psycopg2 import sql  # noqa: PLC0415

    password = _secrets.token_urlsafe(24)
    conn = psycopg2.connect(pg_url, connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(GHOST_ROLE), sql.Literal(password)))
    finally:
        conn.close()

    parts = urllib.parse.urlsplit(pg_url)
    host = parts.hostname or "localhost"
    netloc = f"{GHOST_ROLE}:{urllib.parse.quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    derived = urllib.parse.urlunsplit(
        ("postgresql", netloc, parts.path or "", parts.query, ""))
    print(f"ghost reader role '{GHOST_ROLE}' provisioned "
          "(password generated, stored only in .env)")
    return derived


def _seed_denylist(pg_url: str, prefixes: list[str]) -> None:
    import psycopg2  # noqa: PLC0415

    conn = psycopg2.connect(pg_url, connect_timeout=10)
    try:
        with conn, conn.cursor() as cur:
            for prefix in prefixes:
                cur.execute(
                    f"INSERT INTO {DENYLIST_TABLE} (path_prefix, reason) "
                    "VALUES (%s, %s) ON CONFLICT (path_prefix) DO NOTHING",
                    (prefix, "operator denylist (hippocampus init)"))
        print(f"denylist seeded: {len(prefixes)} prefix(es)")
    finally:
        conn.close()


# ── settings.json snippet (secret-free, r1-privacy-9) ────────────────────

def _print_settings_snippet(project_dir: str) -> None:
    command = shutil.which("hippocampus-mcp") or "/path/to/bin/hippocampus-mcp"
    print("\nAdd this to ~/.claude/settings.json (contains no secrets —")
    print("the server reads .env from its working directory):\n")
    print("  {")
    print('    "mcpServers": {')
    print('      "hippocampus": {')
    print(f'        "command": "{command}"')
    print("      }")
    print("    }")
    print("  }\n")
    print("note: the MCP client may not launch the server from this directory;")
    print(f"if so, use a one-line wrapper as the command:")
    print(f'  printf \'#!/bin/sh\\ncd "{project_dir}" && exec "{command}"\\n\' \\')
    print("    > ~/.local/bin/hippocampus-mcp-wrapper && chmod +x ~/.local/bin/hippocampus-mcp-wrapper")


# ── entrypoint ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hippocampus init",
        description="Interactive first-run setup: .env + migrations + "
                    "ghost reader + settings.json snippet.")
    parser.add_argument("--db", choices=("local", "existing"),
                        help="database mode: local = docker-compose postgres "
                             "on this machine (default, DSN auto-constructed); "
                             "existing = your own PostgreSQL (prompted URL)")
    parser.add_argument("--pg-url-env", metavar="VAR",
                        help="read the PG URL from this environment variable "
                             "(implies --db existing)")
    parser.add_argument("--pg-port", type=int, metavar="PORT",
                        help="host port for the local compose postgres "
                             "(default 5432; use when 5432 is taken)")
    parser.add_argument("--embed", choices=EMBED_CHOICES,
                        help="embed backend choice (prompted otherwise; no default)")
    parser.add_argument("--bge-url", help="BGE embed server URL (with --embed bge-http)")
    parser.add_argument("--bge-token-env", metavar="VAR",
                        help="read the BGE embed token from this environment variable")
    parser.add_argument("--ghost", dest="ghost", action="store_true", default=None,
                        help="provision the ghost reader role (agent_read_mcp)")
    parser.add_argument("--no-ghost", dest="ghost", action="store_false",
                        help="skip ghost reader provisioning")
    parser.add_argument("--exclude-path", action="append", metavar="PREFIX",
                        help="sensitive path prefix to seed into the inject "
                             "denylist (repeatable)")
    parser.add_argument("--skip-migrations", action="store_true",
                        help="write .env only; do not touch the database")
    parser.add_argument("--env-file", default=ENV_FILE,
                        help="target dotenv path (default: ./.env)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="answer yes to confirmations (non-interactive)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except InitError as e:
        print(f"init failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — nothing partially written "
              "(.env writes are atomic)", file=sys.stderr)
        return 130


def _run(args: argparse.Namespace) -> int:
    pg_url, extra_env = _gather_db(args)
    embed_choice, bge_url, bge_token = _gather_embed(args)

    want_ghost = args.ghost
    if want_ghost is None:
        want_ghost = (not args.skip_migrations and sys.stdin.isatty()
                      and _confirm("Provision ghost layer reader role "
                                   f"({GHOST_ROLE})?", assume_yes=False))
    exclude_paths = _gather_exclude_paths(args)

    values: dict[str, str] = {"PG_URL": pg_url, **extra_env}
    if embed_choice == "bge-http":
        values["BGE_EMBED_URL"] = bge_url
        if bge_token:
            values["BGE_EMBED_TOKEN"] = bge_token
    elif embed_choice == "bge-inprocess":
        values["EMBED_PROVIDER"] = "bge-inprocess"

    write_env_file(args.env_file, values, assume_yes=args.yes)
    print(f"wrote {args.env_file} (mode 0600)")

    migrations_ok = False
    if args.skip_migrations:
        print("--skip-migrations: run `hippocampus migrate` next")
    else:
        # Local mode: bring the compose postgres up (or explain) before DDL.
        if "PG_PASSWORD" in extra_env and not _ensure_local_pg(
                pg_url, assume_yes=args.yes, compose_env=extra_env):
            print("skipping migrations until postgres is reachable",
                  file=sys.stderr)
        else:
            migrations_ok = _run_migrations(pg_url, assume_yes=args.yes)

    if want_ghost:
        try:
            ghost_dsn = _provision_ghost_reader(pg_url)
        except Exception as e:  # role absent when migrations skipped, etc.
            print(f"ghost reader provisioning failed ({type(e).__name__}) — "
                  "apply migration 009 then re-run `hippocampus init --ghost`",
                  file=sys.stderr)
        else:
            values["PG_URL_AGENT_READ_MCP"] = ghost_dsn
            write_env_file(args.env_file, values,
                           overwrite_ok=True, assume_yes=True)

    if exclude_paths:
        if args.skip_migrations and not migrations_ok:
            print("denylist seeding skipped (no migrations run) — re-run "
                  "init with --exclude-path after `hippocampus migrate`")
        else:
            try:
                _seed_denylist(pg_url, exclude_paths)
            except Exception as e:
                print(f"denylist seeding failed ({type(e).__name__}) — is "
                      f"migration 014 applied? ({DENYLIST_TABLE})",
                      file=sys.stderr)

    _print_settings_snippet(os.getcwd())
    if not args.skip_migrations and not migrations_ok:
        # .env is written, but the install is NOT usable yet — be honest in
        # the exit code so --yes automation can't mistake this for success.
        print("init INCOMPLETE: migrations did not run — start postgres and "
              "run `hippocampus migrate`, then `hippocampus doctor`.",
              file=sys.stderr)
        return 1
    print("done. next: `hippocampus doctor` to verify the install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
