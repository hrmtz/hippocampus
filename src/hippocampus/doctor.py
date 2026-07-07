"""`hippocampus doctor` — install / connectivity / coverage diagnostics.

Epic #43 Phase 3 (plan §3.3 / §3.9; binding finding r3-privacy-5).

Output contract: every check prints one line — `✓` (pass), `✗` (failure,
process exits 1), `–` (informational / optional capability off) — plus a
short explanation. The output is designed to be SAFE TO PASTE into a bug
report: no DSN userinfo, no passwords, no tokens ever appear. psycopg2
error text reproduces the full DSN including the password, so every error
string is scrubbed through `sanitize_error_text()` before printing.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

# Redaction helpers live in config (shared with migrate; r3-privacy-5),
# re-exported here so existing importers (incl. the CI redaction test) and
# the doctor checks below keep using the same names.
from .config import (  # noqa: E402
    Settings,
    format_pg_error,
    redact_dsn,
    sanitize_error_text,
    secret_substrings as _secret_substrings,
)

OK, FAIL, INFO = "✓", "✗", "–"

GHOST_FUNC_SIGNATURE = "agent.search_ghost_ranked(text, vector, text, boolean, int)"


# ── check framework ──────────────────────────────────────────────────────

class Report:
    def __init__(self) -> None:
        self.failures = 0

    def line(self, symbol: str, name: str, detail: str) -> None:
        if symbol == FAIL:
            self.failures += 1
        print(f"{symbol}  {name}: {detail}")


def _connect(dsn: str):
    import psycopg2  # noqa: PLC0415

    return psycopg2.connect(dsn, connect_timeout=5)


# ── individual checks ────────────────────────────────────────────────────

def check_env_file(rep: Report) -> None:
    path = ".env"
    if not os.path.exists(path):
        rep.line(INFO, ".env", "not found in cwd (process env only — fine "
                               "under a secrets wrapper)")
        return
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        rep.line(FAIL, ".env", f"permissions {mode:04o} are group/world "
                               "readable — run: chmod 600 .env")
    else:
        rep.line(OK, ".env", f"present, mode {mode:04o}")


def check_pg(rep: Report, settings: Settings):
    """Returns an open connection on success, else None."""
    if not settings.pg_url:
        rep.line(FAIL, "postgres", "PG_URL not set (.env or process env)")
        return None
    try:
        conn = _connect(settings.pg_url)
    except Exception as e:
        rep.line(FAIL, "postgres", format_pg_error(settings.pg_url, e))
        return None
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        version = cur.fetchone()[0]
    rep.line(OK, "postgres", f"{redact_dsn(settings.pg_url)} "
                             f"(server {version})")
    return conn


def check_schemas(rep: Report, conn) -> None:
    probes = [  # (schema label, sentinel relation, required?)
        ("personal", "personal.conversations", True),
        ("agent", "agent.ghost_memories", True),
        ("library", "library.conversations", False),
    ]
    with conn.cursor() as cur:
        for label, rel, required in probes:
            cur.execute("SELECT to_regclass(%s)", (rel,))
            present = cur.fetchone()[0] is not None
            if present:
                rep.line(OK, label, f"schema present ({rel})")
            elif required:
                rep.line(FAIL, label, f"{rel} missing — run `hippocampus migrate`")
            else:
                rep.line(INFO, label, "not installed (optional)")


def check_migrations(rep: Report, conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.hippocampus_schema_migrations')")
        if cur.fetchone()[0] is None:
            rep.line(INFO, "migrations", "no ledger table (pre-ledger install "
                                         "or migrations not yet run)")
            return
        cur.execute("SELECT filename FROM public.hippocampus_schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    manifest = _load_manifest_entries()
    if manifest is None:
        rep.line(INFO, "migrations", f"{len(applied)} applied "
                                     "(manifest unavailable for comparison)")
        return
    core = [e for e in manifest if e.tier == "core"]
    core_missing = [e.file for e in core if e.file not in applied]
    if core_missing:
        rep.line(FAIL, "migrations",
                 f"{len(applied)}/{len(manifest)} applied; core pending: "
                 f"{', '.join(core_missing[:3])}"
                 f"{'…' if len(core_missing) > 3 else ''} — "
                 "run `hippocampus migrate`")
    else:
        rep.line(OK, "migrations",
                 f"{len(applied)}/{len(manifest)} applied "
                 f"(all {len(core)} core migrations present)")


def _load_manifest_entries():
    """Manifest entries via hippocampus.migrate (lazy import — degrade to
    None if the module/manifest is absent in this install)."""
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("hippocampus.migrate") is None:
        return None
    try:
        from . import migrate  # noqa: PLC0415

        return migrate.parse_manifest(
            migrate.MIGRATIONS_DIR / migrate.MANIFEST_NAME)
    except Exception:
        return None


def check_embed(rep: Report, settings: Settings) -> None:
    if not settings.embed_configured:
        rep.line(INFO, "embed", "not configured (semantic tools off) — set "
                                "BGE_EMBED_URL or EMBED_PROVIDER=bge-inprocess")
        return
    if settings.embed_provider == "bge-inprocess" and not settings.bge_embed_url:
        import importlib.util  # noqa: PLC0415

        if importlib.util.find_spec("FlagEmbedding") is None:
            rep.line(FAIL, "embed", "EMBED_PROVIDER=bge-inprocess but "
                                    "FlagEmbedding not installed — "
                                    "pip install 'hippocampus-mcp[bge-local]'")
        else:
            rep.line(OK, "embed", "in-process BGE-M3 available "
                                  "(model not loaded by doctor — heavy)")
        return
    # HTTP backend: probe with a 1-token /embed POST (no GET endpoint).
    url = settings.bge_embed_url
    payload = json.dumps({"query": "ping", "max_length": 16}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/embed", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {settings.bge_embed_token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            dim = len(body.get("dense", []))
            rep.line(OK, "embed", f"{redact_dsn(url)} HTTP {resp.status}, "
                                  f"dense dim={dim}")
    except urllib.error.HTTPError as e:
        rep.line(FAIL, "embed", f"{redact_dsn(url)} HTTP {e.code} "
                                f"({'auth?' if e.code in (401, 403) else 'server error'})")
    except Exception as e:
        detail = sanitize_error_text(
            str(e), dsns=[url], extra_secrets=[settings.bge_embed_token])
        rep.line(FAIL, "embed", f"{redact_dsn(url)} unreachable "
                                f"({type(e).__name__}: {detail})")


def check_ghost_reader(rep: Report, settings: Settings) -> None:
    dsn = settings.pg_url_agent_read_mcp
    if not dsn:
        rep.line(INFO, "ghost reader", "PG_URL_AGENT_READ_MCP not set "
                                       "(ghost tools off) — `hippocampus init "
                                       "--ghost` provisions it")
        return
    try:
        conn = _connect(dsn)
    except Exception as e:
        rep.line(FAIL, "ghost reader", format_pg_error(dsn, e))
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regprocedure(%s)", (GHOST_FUNC_SIGNATURE,))
            resolvable = cur.fetchone()[0] is not None
        if resolvable:
            rep.line(OK, "ghost reader", f"{redact_dsn(dsn)} connects; "
                                         "agent.search_ghost_ranked resolvable")
        else:
            rep.line(FAIL, "ghost reader", "connected but "
                                           "agent.search_ghost_ranked not found "
                                           "— is migration 020 applied?")
    except Exception as e:
        rep.line(FAIL, "ghost reader", format_pg_error(dsn, e))
    finally:
        conn.close()


def check_dense_null(rep: Report, conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('personal.messages')")
        if cur.fetchone()[0] is None:
            rep.line(INFO, "dense-NULL", "personal.messages missing — "
                                         "skipped (see schema check)")
            return
        cur.execute("SELECT count(*) FROM personal.messages WHERE dense IS NULL")
        nulls = cur.fetchone()[0]
    if nulls:
        rep.line(FAIL, "dense-NULL", f"{nulls} message(s) with dense IS NULL — "
                                     "ingest ran without a working embed "
                                     "backend; re-embed or re-ingest")
    else:
        rep.line(OK, "dense-NULL", "0 messages with NULL dense")


def check_conv_coverage(rep: Report, conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('personal.conversations')")
        if cur.fetchone()[0] is None:
            return  # already reported by the schema check
        cur.execute(
            "SELECT count(*),"
            "       count(*) FILTER (WHERE summary_text IS NOT NULL),"
            "       count(*) FILTER (WHERE conv_dense IS NOT NULL)"
            "  FROM personal.conversations")
        total, summarized, embedded = cur.fetchone()
    rep.line(INFO, "conv coverage", f"{summarized}/{total} summarized, "
                                    f"{embedded}/{total} conv_dense embedded "
                                    "(informational)")


def check_scoring_key(rep: Report) -> None:
    present = bool(os.environ.get("CF_ANTHROPIC_API_KEY")
                   or os.environ.get("ANTHROPIC_API_KEY"))
    if present:
        rep.line(INFO, "scoring key", "present (conversation scoring enabled; "
                                      "sends text to Anthropic)")
    else:
        rep.line(INFO, "scoring key", "not set (scoring stage off — optional)")


# ── entrypoint ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="hippocampus doctor",
        description="Diagnose the hippocampus install. Exit 0 when no ✗. "
                    "Output is safe to paste (no secrets).",
    ).parse_args(argv)

    rep = Report()
    settings = Settings.load(require_pg=False)

    check_env_file(rep)
    conn = check_pg(rep, settings)
    if conn is not None:
        try:
            check_schemas(rep, conn)
            check_migrations(rep, conn)
        except Exception as e:
            conn.rollback()
            rep.line(FAIL, "schema checks", format_pg_error(settings.pg_url, e))
    check_embed(rep, settings)
    check_ghost_reader(rep, settings)
    if conn is not None:
        try:
            check_dense_null(rep, conn)
            check_conv_coverage(rep, conn)
        except Exception as e:
            conn.rollback()
            rep.line(FAIL, "coverage checks", format_pg_error(settings.pg_url, e))
        finally:
            conn.close()
    check_scoring_key(rep)

    if rep.failures:
        print(f"\n{rep.failures} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
