**English** ・ [日本語](INSTALL.ja.md)

# INSTALL.md — detailed setup

The [README quick start](README.md#quick-start) is the short path. This
document covers the decisions and the failure modes.

## Prerequisites

- **Python 3.11+**
- **`psql` on PATH** — a hard runtime prerequisite, not just for setup:
  `hippocampus migrate` delegates every migration file to a
  `psql -v ON_ERROR_STOP=1` subprocess (several migrations contain
  `CREATE INDEX CONCURRENTLY` / `ALTER TYPE .. ADD VALUE`, which cannot
  run inside a driver-managed transaction). Debian/Ubuntu:
  `apt-get install postgresql-client`. macOS: `brew install libpq` (and
  put it on PATH).
- **PostgreSQL with pgvector** — either via the bundled compose file or
  an existing server (see below).
- Docker is optional; it is only used for the compose path and the
  optional local embed server.

Install the package from a checkout:

```bash
pip install .                              # base: MCP server + CLI + ingest
pip install '.[bge-local]'                 # + in-process BGE-M3 embedding
pip install '.[scoring]'                   # + Anthropic client for the optional scoring stage
```

The base install is deliberately light (no torch).

## Database: compose path vs existing PostgreSQL

### Path A — local compose (the default; `hippocampus init` drives it)

You normally don't run compose by hand: choose `local` at init's database
prompt (the default, also `--db local`) and init will

1. generate a database password and write it to `.env` as `PG_PASSWORD`
   (compose reads the same file — one secret, one place),
2. construct `PG_URL` for user/database `hippocampus` on `localhost`,
3. offer to run `docker compose up -d postgres` and wait for readiness,
4. run the core migrations.

The container is `pgvector/pgvector:pg16` bound to `127.0.0.1` only, data
in the named volume `pg_data`. Host port defaults to 5432; if that's
taken (host postgres, or a Windows-side listener under WSL2), use
`--pg-port <port>` — init records `HIPPOCAMPUS_PG_PORT` in `.env` so
compose follows automatically.

**DDL never runs through compose** — the container has no init-script
mount; schema creation is exclusively `hippocampus migrate` (run for you
by `hippocampus init`).

### Path B — existing PostgreSQL (local or remote server)

Choose `existing` at init's database prompt (or `--db existing`, or
`--pg-url-env VAR` for scripted installs) and provide the URL. For a
remote server, see PRIVACY.md: your conversation text transits the
network — keep it on a private network or behind TLS.

Requirements on the target database/cluster:

- pgvector available (`CREATE EXTENSION vector` must succeed — migration
  001 creates extensions `vector` and `pg_trgm`, which typically requires
  a superuser or a cluster where the extensions are allowed).
- The migration role needs **CREATEROLE**: migration 009 creates
  cluster-global `agent_*` roles (guarded with `IF NOT EXISTS`, so a
  cluster that already has them is fine — two hippocampus databases on
  one cluster share the roles).
- Network reachability from wherever the MCP server and ingest runs.

Then either run `hippocampus init` with your DSN, or skip init's
DB step (`--skip-migrations`) and run `hippocampus migrate` yourself.

## Embedding backend decision

Semantic search is **off by default** — `hippocampus init` forces an
explicit choice and there is no silent fallback (an unconfigured install
will never surprise-download a ~6 GB model).

| Backend | When to pick it | Setup | Trade-offs |
|---|---|---|---|
| **none** | Trying out the install before committing RAM/disk to a model | nothing | semantic tools (`search_personal_memory` etc.) are hidden, and **ingest requires an embed backend** (vectors are written together with the text — by design there are no vector-less rows), so configure a backend before your first real ingest |
| **bge HTTP** (`bge-http`) | You want semantic search and are fine running one more container, or you have a GPU box elsewhere | `docker compose --profile bge up -d` (serves on `127.0.0.1:8086`; set `BGE_EMBED_TOKEN` in `.env`), then `BGE_EMBED_URL=http://localhost:8086` | model download (~6 GB) on first container start; ~6 GB RAM steady state; server process stays light |
| **bge in-process** (`bge-inprocess`) | Single machine, no extra container | `pip install 'hippocampus-mcp[bge-local]'`, `EMBED_PROVIDER=bge-inprocess` | ~6 GB RAM **inside the MCP/ingest process**; first call downloads the model; slower cold start |
| future providers | hosted embedding APIs | not implemented yet | will require a re-embed of the corpus on switch — vectors from different models are not comparable |

All backends pass through one client boundary that asserts L2-normalized
1024-dim output; a backend returning anything else fails loudly instead
of silently corrupting ranking.

To change later: edit `.env` (`BGE_EMBED_URL`/`BGE_EMBED_TOKEN` vs
`EMBED_PROVIDER=bge-inprocess`) and restart the server.

## Migrations: `hippocampus migrate`

Migrations are ordered by `migrations/manifest.yaml` (the single source
of truth — filenames have historical duplicate prefixes, so a glob is
wrong), applied via psql with a ledger table
(`public.hippocampus_schema_migrations`) so re-runs only apply what is
pending.

```bash
hippocampus migrate                      # core tier (default): personal + agent schemas
hippocampus migrate --with-library       # + optional library schema (external reference media)
hippocampus migrate --include-optional   # + deferred extras (ghost HNSW index — only worth it at 1000+ rows)
hippocampus migrate --status             # applied/pending table
hippocampus migrate --dry-run            # show what would run
```

Tiers:

- **core** — everything the personal-memory and ghost tools need. This is
  what `hippocampus init` applies.
- **library** — a second corpus schema for external reference media
  (books, subtitles, transcripts you ingest yourself). Library-less
  installs are fully supported; library tools simply don't register.
- **optional** — currently one deferred HNSW index for the ghost layer;
  apply once `agent.ghost_memories` has 1000+ rows.

### Pre-existing databases: `--baseline`

If your database already holds the schema (e.g. it was built by hand
before the manifest runner existed), a bare `migrate` would try to
re-apply migration 001 and fail. Baseline mode stamps the selected
pending entries into the ledger **without executing any SQL**:

```bash
hippocampus migrate --baseline --dry-run     # preview the stamp list
hippocampus migrate --baseline               # asks you to type 'baseline' to confirm
hippocampus migrate --baseline --yes         # non-interactive
```

You are asserting that the schema actually matches the stamped files —
the runner does not verify that claim. Combine with `--with-library` /
`--include-optional` to control which tiers get stamped. After
baselining, future runs apply only genuinely new files.

### Targeting a different database

Prefer the env var over argv (argv leaks into shell history and process
lists):

```bash
HIPPOCAMPUS_MIGRATE_DB=scratchdb hippocampus migrate   # bare DB name; host/user from PG_URL
hippocampus migrate --db-url postgresql://...          # full override; avoid in shared transcripts
```

### Failure semantics

A failed file is **not** recorded in the ledger — fix the cause and
re-run; already-applied files are skipped. If a `CREATE INDEX
CONCURRENTLY` build dies it can leave an `INVALID` index; the affected
migration files carry their own check that prints the exact remediation
(`DROP INDEX CONCURRENTLY ...; re-apply`), which the runner surfaces
verbatim.

## Troubleshooting: start with `hippocampus doctor`

`doctor` is the diagnostic entrypoint. Every check prints one line —
`✓` pass, `✗` failure (exit code 1), `–` informational/feature-off —
and the output is deliberately **safe to paste into a bug report**: DSN
userinfo, passwords, and tokens are scrubbed before printing.

Checks: `.env` permissions, PostgreSQL connectivity + server version,
schema presence (personal / agent required, library optional), migration
ledger vs manifest, embed backend reachability, ghost reader role,
dense-NULL count, summary coverage, scoring-key presence.

Common failures:

| Symptom | Meaning | Fix |
|---|---|---|
| `psql not found on PATH` (from `migrate`) | postgresql client not installed | `apt-get install postgresql-client` (or distro equivalent) |
| `✗ postgres: ... OperationalError` | PG unreachable / wrong password / wrong port | check the container is up (`docker compose ps`), the port, and `PG_URL` in `.env` |
| `✗ embed: ... HTTP 401 (auth?)` | embed server rejected the token | `BGE_EMBED_TOKEN` in `.env` must match the one the embed server was started with |
| `✗ embed: ... HTTP 404` / `unreachable` | wrong `BGE_EMBED_URL`, or the server isn't running | `docker compose --profile bge up -d`; confirm the URL has no path suffix (the client appends `/embed`) |
| `✗ dense-NULL: N message(s)` | a past ingest ran while the embed backend was down — those rows are invisible to semantic search | fix the backend, then re-ingest the affected source (upserts are idempotent); new ingests fail loudly on this instead of leaving silent gaps |
| `✗ ghost reader: connected but agent.search_ghost_ranked not found` | ghost migrations missing | `hippocampus migrate` (core tier includes them) |
| `– ghost reader: PG_URL_AGENT_READ_MCP not set` | ghost tools off (this is fine if you don't use the ghost layer) | `hippocampus init --ghost` |
| `✗ .env: permissions 0644 ...` | secrets file readable by other users | `chmod 600 .env` |

## Automation

### Nightly ingest (cron)

```cron
# crontab -e  — nightly at 03:00; flock prevents overlap with a manual run
0 3 * * * flock -n /tmp/hippocampus_ingest.lock -c 'cd /path/to/hippocampus-mcp && /path/to/venv/bin/hippocampus ingest claude-code && /path/to/venv/bin/hippocampus ingest codex' >> ~/hippocampus-ingest.log 2>&1
```

Notes: the CLI itself takes no lock (per-conversation upserts are
idempotent), so the `flock` wrapper is the polite way to serialize cron
against manual runs. ZIP sources (chatgpt / claude-ai) are one-shot
imports, not cron candidates.

### Ingest on session end (Claude Code SessionEnd hook)

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "flock -n /tmp/hippocampus_ingest.lock -c 'cd /path/to/hippocampus-mcp && /path/to/venv/bin/hippocampus ingest claude-code' >/dev/null 2>&1 &",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### Summaries

`hippocampus summarize` (optional; requires `ANTHROPIC_API_KEY` and an
embed backend) backfills conversation-level summaries + embeddings for
summary-grain search. Useful flags: `--limit N`, `--dry-run`,
`--platforms claude_code,codex`, `--segments-only`. Run it after large
ingests, or weekly from cron. It only processes conversations that don't
have a summary yet.

## Multiple machines

Everything coordinates through PostgreSQL, so a multi-machine setup is
just configuration:

- Run PG on one host; point each machine's `PG_URL` at it (use TLS /
  `?sslmode=require` outside a trusted network).
- One embed server can serve every machine — set `BGE_EMBED_URL` to its
  address and keep `BGE_EMBED_TOKEN` set; remember the compose default
  binds to localhost only, so widen the bind deliberately and put it
  behind your own transport security.
- Ingest runs wherever the source files live (each machine ingests its
  own `~/.claude/projects`); dedup is by conversation id, so overlap is
  harmless.
- See [PRIVACY.md](PRIVACY.md) before sending embed traffic across a
  network: the embed payload is your full message text.

## Verifying an install end-to-end

`bash scripts/test_clean_container.sh` reproduces the documented flow in
disposable containers (fresh pgvector + python:3.12-slim, `pip install`,
`hippocampus init --embed none --yes`, doctor, source listing). If that
passes but your install doesn't, the difference is your environment —
`hippocampus doctor` output is the place to look.
