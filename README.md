**English** ・ [日本語](README.ja.md)

# hippocampus-mcp

Personal memory infrastructure for people who use AI agents every day.

hippocampus-mcp ingests your conversation logs from multiple platforms
(Claude Code, ChatGPT, claude.ai, Codex CLI) into a PostgreSQL + pgvector
database **that you run**, and exposes them as MCP search tools to any
agent session. Your past reasoning, decisions, and debugging sessions stop
evaporating when the window closes.

The differentiator is the **ghost layer**: a separate, opt-in vault where
the *agent's own* accumulated rules and feedback ("last time this failed
because...") are synced nightly and become searchable from every project —
cross-project agent memory, not just human conversation recall.

> The name: the hippocampus is the brain structure that consolidates
> short-term experience into long-term memory during sleep. This system
> imitates that loop — daytime sessions accumulate as JSONL, a nightly
> ingest embeds and persists them, and the next session can recall them.

```
INGEST                          STORE                      RETRIEVE (MCP)
Claude Code sessions  ─┐
ChatGPT export ZIP    ─┤  parse → scrub → embed   personal.*  ──┐  search_personal_memory
claude.ai export ZIP  ─┼─────────────────────────▶ (your        ├─ search_conversations
Codex CLI history     ─┘                           PostgreSQL)  ├─ list_recent_conversations
                                                                ┘  get_conversation ...
agent memory files    ───  nightly dub (opt-in) ─▶ agent.*    ──── search_ghost_memory
```

## Quick start

Prerequisites: Python 3.11+, a `psql` client on PATH (Debian/Ubuntu:
`apt-get install postgresql-client`), and either Docker or an existing
PostgreSQL with the pgvector extension.

Everything runs on your machine by default — the database is a bundled
docker-compose postgres, and `hippocampus init` sets it up for you.

```bash
git clone <this-repo> hippocampus-mcp && cd hippocampus-mcp

# 1. Install the package
pip install .

# 2. First-run setup. Pick "local" for the database (the default), pick an
#    embed backend, optionally provision the ghost layer. init generates
#    the DB password, writes .env (mode 0600), starts the compose postgres,
#    runs migrations, and prints the MCP registration snippet.
hippocampus init

# 3. Semantic search via the bundled local BGE-M3 server (recommended):
#    choose "bge-http" + http://localhost:8086 in init, then bring it up —
#    compose reads the token init wrote into .env (~6 GB model on first start)
docker compose --profile bge up -d

# 4. Verify, then ingest your Claude Code sessions
hippocampus doctor
hippocampus ingest claude-code
```

Non-interactive minimal install (no embed model — semantic tools stay
hidden, and ingest refuses to run, until a backend is configured; vectors
are written together with the text, never backfilled silently):

```bash
hippocampus init --yes --embed none
```

If host port 5432 is taken (a host postgres, or a Windows-side listener
under WSL2), pass `--pg-port <free-port>` — compose and the generated
`PG_URL` follow it via `.env`.

**Running the database on a separate server instead?** Choose `existing`
at the database prompt (or `--db existing`) and paste your PostgreSQL
URL — see INSTALL.md Path B, and PRIVACY.md for what a remote database
implies (your conversation text transits the network; keep it on a
private network or behind TLS). Local is the recommended default.

### Register the MCP server

Add to `~/.claude/settings.json` (or your client's MCP config). The
snippet contains no secrets — the server reads `.env` from its working
directory:

```json
{
  "mcpServers": {
    "hippocampus": {
      "command": "/path/to/your/venv/bin/hippocampus-mcp"
    }
  }
}
```

If your MCP client does not launch servers from the project directory,
use the one-line `cd && exec` wrapper that `hippocampus init` prints at
the end of its run.

Then, from a fresh agent session:

```
search_personal_memory("that postgres deadlock we debugged")
list_recent_conversations(days=2)
get_conversation("claude_code:<conv-id>")
search_ghost_memory(current_project="my-repo")   # ghost layer, if enabled
```

## Ingest sources

Four sources are built in (`hippocampus ingest --list`):

| Source | Command | Input |
|---|---|---|
| Claude Code | `hippocampus ingest claude-code` | auto-discovers `~/.claude/projects/` (override: `CLAUDE_DIR`); incremental — re-run any time |
| ChatGPT | `hippocampus ingest chatgpt /path/to/export.zip` | official data-export ZIP |
| claude.ai | `hippocampus ingest claude-ai /path/to/data-XXXX.zip` | official data-export ZIP |
| Codex CLI | `hippocampus ingest codex` | `~/.codex/history.jsonl` (override: `CODEX_HISTORY_FILE`); known limitation: lines appended to an already-ingested session are not re-read |

Every source runs the same pipeline: parse → credential scrub → embed →
upsert → verify (the run fails loudly if any ingested message ended up
without a vector). Conversations are deduplicated, so re-running an
ingest is safe.

After ingest, `hippocampus summarize` builds per-conversation rollup
summaries and segment summaries for long conversations (substrate for
summary-level search). It requires an Anthropic API key
(`ANTHROPIC_API_KEY`) and a working embed backend — see
[PRIVACY.md](PRIVACY.md) for exactly what text it sends where.

## Semantic search backends

Semantic (vector) search is **off until you explicitly choose a
backend** — there is no silent model download. Three choices at
`hippocampus init` (changeable later in `.env`):

| Choice | What it means | Cost |
|---|---|---|
| `none` | keyword/recency tools only; semantic tools are hidden | zero |
| `bge-http` | BGE-M3 over HTTP — `docker compose --profile bge up -d` runs one on `localhost:8086`, or point `BGE_EMBED_URL` at your own | ~6 GB RAM in the container |
| `bge-inprocess` | model loaded inside the server process (`pip install 'hippocampus-mcp[bge-local]'`) | ~6 GB RAM in-process, ~6 GB one-time download |

Details and a decision table: [INSTALL.md](INSTALL.md).

## Ghost layer (cross-project agent memory)

Project-local agent memory files can be promoted — via an explicit
dual-signal opt-in (frontmatter `scope: shared` **and** a line in a
human-edited allowlist file) — into a shared vault that any project's
session can search through `search_ghost_memory`. Promotion is
default-deny; a content scanner is a third wall behind the two signals.

`hippocampus init --ghost` provisions the read-only database role it
needs. Full user guide: [docs/GHOST_LAYER_USER.md](docs/GHOST_LAYER_USER.md).

## Privacy

Short version: your full conversation text and its vectors live in *your*
PostgreSQL. Nothing leaves your machine unless you explicitly enable a
feature that needs it (Anthropic-backed scoring/summaries, a remote embed
endpoint). Credential scrubbing at ingest is **best-effort, not a
guarantee**. Read [PRIVACY.md](PRIVACY.md) before ingesting anything
sensitive.

## Support model

This is published as **useful infrastructure, not a supported product**.
It is the actual daily-driver memory system of its author, extracted into
an installable shape. Issues and PRs are welcome and handled best-effort;
there is no SLA, no roadmap commitments, and APIs may change between
minor versions. If it breaks, `hippocampus doctor` output (which is
designed to be safe to paste — no secrets ever appear in it) is the most
useful thing to include in a report.

## Documentation

- [INSTALL.md](INSTALL.md) — detailed setup: compose vs existing PG, embed backends, migrations, troubleshooting, automation
- [PRIVACY.md](PRIVACY.md) — what is stored, what leaves the box and when, scrub limits, prompt-injection posture
- [docs/GHOST_LAYER_USER.md](docs/GHOST_LAYER_USER.md) — ghost layer user guide
- [docs/SECRETS_HARDENED.md](docs/SECRETS_HARDENED.md) — optional sops-encrypted secrets setup (default is a plain `.env`, mode 0600)
- [docs/CONFIG.md](docs/CONFIG.md) — full environment-variable reference
