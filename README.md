**English** ・ [日本語](README.ja.md)

# hippocampus-mcp

Personal memory infrastructure for people who use AI agents every day.

hippocampus-mcp ingests your conversation logs from multiple platforms
(Claude Code, ChatGPT, claude.ai, Codex, Grok, Kimi, Antigravity) into a
PostgreSQL + pgvector database **that you run**, and exposes them as MCP
search tools to any agent session. Your past reasoning, decisions, and
debugging sessions stop evaporating when the window closes.

The differentiator is the **ghost layer**: a separate, opt-in vault where
the *agent's own* accumulated rules and feedback ("last time this failed
because...") are synced nightly and become searchable from every project —
cross-project agent memory, not just human conversation recall.

On top of the searchable corpus sit three further opt-in layers, each with
its own doc: a distilled **facts** layer (`search_facts`), a first-person
**diary** the agent writes once a day (plus a read-only grounding auditor that
checks each entry's self-criticism against the transcripts), and an editable,
human-gated **wiki** for the subject knowledge you actually study. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit.

The corpus is reachable not only from terminal agents but, via an opt-in
OAuth-gated **remote MCP connector**, from **claude.ai on the web and mobile**
too — ask claude.ai on your phone "what did I decide about X?" and it searches
your database. See [docs/CONNECTOR.md](docs/CONNECTOR.md).

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

# 3. For local semantic search without resident BGE RAM:
#    choose "bge-ondemand" in init. The first semantic ingest/search starts
#    the compose BGE-M3 server; it exits after the idle timeout.

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

Seven sources are built in (`hippocampus ingest --list`):

| Source | Command | Input |
|---|---|---|
| Claude Code | `hippocampus ingest claude-code` | auto-discovers `~/.claude/projects/` (override: `CLAUDE_DIR`); incremental — re-run any time |
| ChatGPT | `hippocampus ingest chatgpt /path/to/export.zip` | official data-export ZIP |
| claude.ai | `hippocampus ingest claude-ai /path/to/data-XXXX.zip` | official data-export ZIP |
| Codex CLI | `hippocampus ingest codex` | `~/.codex/history.jsonl` (override: `CODEX_HISTORY_FILE`); known limitation: lines appended to an already-ingested session are not re-read |
| Antigravity | `hippocampus ingest antigravity` | `~/.gemini/antigravity-cli/brain` (override: `ANTIGRAVITY_BRAIN_DIR`) |
| Kimi Code | `hippocampus ingest kimi` | `~/.kimi-code` (override: `KIMI_DIR`) |
| Grok CLI | `hippocampus ingest grok` | `~/.grok` (override: `GROK_DIR`) |

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
| `bge-ondemand` | local compose BGE-M3 starts on first semantic ingest/search, then exits after `BGE_ONDEMAND_IDLE_SECONDS` | ~6 GB RAM only while the container is running; first request waits for startup/download |
| `bge-http` | BGE-M3 over HTTP — `docker compose --profile bge up -d` runs one on `localhost:8086`, or point `BGE_EMBED_URL` at your own | ~6 GB RAM in the container while it is running |
| `bge-inprocess` | model loaded inside the server process (`pip install 'hippocampus-mcp[bge-local]'`) | ~6 GB RAM in-process, ~6 GB one-time download |

Recommended single-machine setup:

```bash
hippocampus init --embed bge-ondemand
hippocampus doctor          # reports cold/hot status without starting BGE
hippocampus ingest codex    # first semantic call starts compose `bge`
```

Peak memory is unchanged: BGE-M3 still needs roughly 6 GB while it is
running. On-demand only reduces how long that memory stays resident.

Manual low-memory workflow for a single local machine: keep `bge-http`
configured, start the semantic backend only when you need it, then stop it
to release the BGE-M3 container memory:

```bash
docker compose --profile bge up -d   # start semantic backend
hippocampus doctor
hippocampus ingest claude-code       # or run semantic search/summarize
docker compose stop bge              # release BGE-M3 memory
```

If `BGE_EMBED_URL` remains set while the local `bge` container is stopped,
semantic ingest/search fails loudly until you start it again. That is
expected for manual low-memory use; run `docker compose --profile bge up -d`
before semantic work.

On the first `bge` start, the model downloads into the compose `hf_cache`
volume (mounted as `/hf_cache` in the container). If the first download is
interrupted and later starts keep failing during model load, stop `bge` and
retry. If the HuggingFace cache is corrupt, remove only the compose
`hf_cache` volume and let it re-download; do not remove `pg_data`, which is
the database volume.

Details and a decision table: [INSTALL.md](INSTALL.md). Code-level
`bge-ondemand` behavior is documented in [docs/BGE_ONDEMAND.md](docs/BGE_ONDEMAND.md).

## Ghost layer (cross-project agent memory)

Project-local agent memory files can be promoted — via an explicit
dual-signal opt-in (frontmatter `scope: shared` **and** a line in a
human-edited allowlist file) — into a shared vault that any project's
session can search through `search_ghost_memory`. Promotion is
default-deny; a content scanner is a third wall behind the two signals.

`hippocampus init --ghost` provisions the read-only database role it
needs. Full user guide: [docs/GHOST_LAYER_USER.md](docs/GHOST_LAYER_USER.md).

## claude.ai connector (use it from web & mobile)

The stdio MCP server only reaches terminal agents. To search your memory from
**claude.ai's web app or phone app**, run the optional **connector**: a second
entry point (`hippocampus-mcp-connector-oauth`) that serves the same tools over
streamable HTTP behind a single-owner **OAuth** authorization server, exposed
through a cloudflared tunnel.

It is deliberately narrower than the stdio surface — a fail-closed **read-only
allowlist** (personal/conversation/library search only; ghost, facts, and
full-thread retrieval are excluded), **audience-bound** tokens, a chain-read
budget, and fail-open read auditing. Register it once in claude.ai's connector
settings and it works from every device.

Setup, security posture, and troubleshooting: [docs/CONNECTOR.md](docs/CONNECTOR.md).

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
- [docs/CONNECTOR.md](docs/CONNECTOR.md) — claude.ai remote MCP connector (use your memory from web & mobile)
- [docs/SECRETS_HARDENED.md](docs/SECRETS_HARDENED.md) — optional sops-encrypted secrets setup (default is a plain `.env`, mode 0600)
- [docs/CONFIG.md](docs/CONFIG.md) — full environment-variable reference
