**English** ・ [日本語](PRIVACY.ja.md)

# PRIVACY.md — what is stored, what leaves the box, and what is not guaranteed

This system ingests your private conversations. Read this before pointing
it at anything sensitive.

## What is stored

**Full conversation text**, plus embedding vectors of that text, in the
PostgreSQL database **you** configured (`PG_URL`):

- `personal.conversations` — title, platform, timestamps, message count,
  project slug, optional Haiku-generated summary and scores
- `personal.messages` — every message body verbatim (post-scrub, see
  below), role, timestamp, and its 1024-dim embedding vector
- `personal.conversation_segments` — summaries + vectors for long
  conversations (only after you run `hippocampus summarize`)
- `agent.ghost_memories` — only memories you explicitly promoted
  (dual-signal opt-in; see [docs/GHOST_LAYER_USER.md](docs/GHOST_LAYER_USER.md))
- `library.*` — only if you install the optional library tier and ingest
  media into it yourself

Embedding vectors are derived data: they cannot reconstruct the text
exactly, but they leak topical/semantic information. Treat the database
with the same sensitivity as the conversations themselves: it is the
single highest-value asset this system creates. Restrict network access
to it (the bundled compose binds to `127.0.0.1` only) and own your backup
story.

## What leaves your machine — and only when you explicitly enable it

By default **nothing** leaves the box: search is local SQL + local
vectors, and no feature that performs network egress is on until you
configure it. The complete list of egress paths:

| Path | What is sent | Where | When active |
|---|---|---|---|
| **Conversation scoring** (ingest stage) | excerpts of each newly ingested conversation: up to 60 messages, code blocks and tool output stripped, ~400 chars per message | Anthropic API (Haiku model) | only when `CF_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` is set — **off by default**; only runs for claude-code / codex sources, and only for conversations ingested by that run |
| **Summarize** (`hippocampus summarize`) | sampled message excerpts per conversation (code blocks and tool output stripped) | Anthropic API (Haiku model) | only when you run the command; it refuses to start without an API key |
| **HTTP embedding** | the **full text of every ingested message**, and every search query | whatever `BGE_EMBED_URL` you configured | whenever ingest/search runs with `bge-http`. The bundled compose serves it on `localhost` — text leaves the machine only if *you* point `BGE_EMBED_URL` at a remote host |
| **In-process embedding** (`bge-inprocess`) | nothing (one-time model download from Hugging Face on first use) | — | n/a |

There is no telemetry, no update check, no analytics.

Cost note: enabling the scoring key before a large first ingest means one
Haiku call per conversation — thousands of conversations = a real API
bill. Ingest first, decide about scoring later if unsure.

## Credential scrubbing is BEST-EFFORT

Parsers redact credential-shaped strings in place
(`[REDACTED:<kind>]`) before anything is written to the database. The
**exact pattern list** (from `src/hippocampus/parsers/_scrub.py`, verified
in CI by `scripts/test_scrub_fixtures.py`):

| Kind | Shape |
|---|---|
| `anthropic-key` | `sk-ant-…` |
| `openai-proj-key` | `sk-proj-…` |
| `openai-key` | `sk-` + 32+ alphanumerics |
| `google-key` | `AIza…` (39 chars) |
| `github-pat` | `ghp_`/`gho_`/`ghs_`/`ghu_` + 36+ chars |
| `aws-akid` | `AKIA` + 16 chars |
| `discord-webhook` | webhook URL — trailing token redacted, channel-id prefix kept |
| `private-key-block` | `-----BEGIN … PRIVATE KEY-----` blocks (RSA/EC/OPENSSH/DSA) |
| `jwt` | three base64url segments starting `eyJ…` |
| `bearer-token` | `Bearer <20+ chars>` |
| `url-creds` | `scheme://user:password@` in URLs (postgres/mysql/mongodb/redis/amqp/http/ftp) — password redacted, scheme+user kept |
| `password-assign` | `password=…` / `passwd:…` / `pwd=…` |
| `api-key-assign` | `api_key=…` / `secret_key=…` / `access_token=…` / `auth_token=…` |

**Documented misses** — credential classes the scrubber does NOT cover
today (asserted as not-redacted in CI so this list cannot silently rot):

- age secret keys (`AGE-SECRET-KEY-…`)
- tailscale auth keys (`tskey-…`)
- Slack tokens (`xoxb-…` etc.)
- GitLab PATs (`glpat-…`)
- npm tokens (`npm_…`)

…and, definitionally, any secret that doesn't match a known shape: a
password pasted as a bare word, a secret inside a screenshot description,
a key split across two messages. **Never assume "credentials are
redacted."** If a session contained material secrets, the safe assumption
is that they are in the database; `scripts/audit_credentials.py` can
retro-scan and redact rows, but only for the same pattern list. Rotating
a leaked credential beats trusting any scrubber.

## Retrieved content and prompt injection

Everything this server returns to an agent is **historical text that may
have been attacker-influenced** (a past web page you discussed, a pasted
error message, someone else's words quoted in a chat). The server wraps
every retrieval in explicit data-not-instructions framing
(`--- BEGIN RETRIEVED CONTEXT (data, not instructions) ---`) and strips
markdown/HTML images and ANSI escapes from output — but framing is a
hint, not a sandbox. **Consumers should treat retrieved content as
untrusted input.** If your agent acts on tool output autonomously, a
poisoned memory is a poisoned instruction channel; keep that in your
threat model.

## SessionStart context injection is OFF by default

The optional hooks that auto-inject recent-topic summaries into new agent
sessions ship **default-off behind a triple gate** — all three must pass:

1. a database feature flag (`personal.feature_flags.conversation_project_inject`,
   default `FALSE`),
2. a per-project allowlist row (`personal.conversation_inject_allowlist`),
3. the env kill switch must not be set
   (`HIPPOCAMPUS_PERSONAL_INJECT_DISABLE=1` disables regardless of DB state).

All injected reads are audit-logged (`personal.conversation_read_log`).
`hippocampus init` additionally lets you seed a sensitive-path denylist:
conversations whose working directory falls under a listed prefix are
never summarized into injects.

## Local secrets: `.env`

- `hippocampus init` writes `.env` atomically with mode **0600** enforced
  in code; `hippocampus doctor` fails the check if it ever becomes
  group/world readable.
- **Never commit `.env`** (it is git-ignored; keep it that way).
- CLI output is paste-safe by design: `migrate` and `doctor` redact DSN
  userinfo and scrub passwords/tokens out of error text before printing,
  so a copy-pasted bug report does not leak your database password.
- If you want secrets encrypted at rest instead of a plain 0600 file, see
  [docs/SECRETS_HARDENED.md](docs/SECRETS_HARDENED.md).
