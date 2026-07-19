**English** ・ [日本語](DIARY.ja.md)

# Diary — the candid first-person "fast layer"

The extracted-facts and rollup-summary layers answer "what did the *user*
decide / build". The **diary layer** answers a different question: "what does
*Claude* make of the user". Once per JST calendar day it reads that day's
`claude_code` conversations and writes a single first-person, candid-observation
diary entry — not a recap of tasks, but Claude's private notes on the person
behind the requests (personality, contradictions, real motives, the things that
grated). It is the **"fast layer"** of the personality-formation DB: a quick,
daily, store-only pass, distinct from the slower distilled layer planned for
Phase 3.

It is a **write-only layer**. There is no MCP read tool: nothing injects diary
prose into a live session in this phase (`store-only` invariant below). The pass
is `hippocampus diary` and the storage is a dedicated table (migration `026`).

## Why a diary is a control problem

A naive design — feed the writer all prior diaries every day for "continuity" —
is a **pure integrator**: each day's tone is fed back as next day's input, so
flattery, self-regard, or any stylistic tic compounds without bound until the
voice drifts away from the actual conversations. The continuity feedback path
was kept (the user prioritised continuity over a stateless writer, design pivot
2026-06-25) but **regulated** with three mechanisms so it stays bounded:

1. **Windowed (leaky integrator).** The writer reads only the prior
   `PRIOR_WINDOW` (= 7) days of diary prose, never the full history. Old tone
   decays out of context instead of accumulating.
2. **"Build on ≠ be dragged by".** Prior entries are passed *for content
   continuity only*; the prompt forbids mimicking their tone or phrasing. The
   voice is rewritten every day from that day's conversation.
3. **Drift meter.** Once a feedback path exists, it must be measured. Each
   entry's day-to-day cosine distance is computed (`--drift-report`, and printed
   on write). Outlier days (> mean + `DRIFT_FLAG_SIGMA`·σ) are flagged.

Two more invariants:

- **Grounding required.** Every observation must trace to something the user
  actually said or did that day. Inventing character assessments or armchair
  psychoanalysis with no basis in the transcript is banned; uncertain readings
  must be marked as such ("憶測だが").
- **Store-only.** This layer is never injected into a live session in this
  phase. Phase 3 will gate-inject only the slower distilled layer.

## Data model — migration 026

`026_diary.sql` (tier `core`, transaction-safe) adds one table:

```text
personal.diary
  entry_date   DATE PRIMARY KEY        -- one row per JST day (= ≤365/yr)
  body         TEXT NOT NULL           -- the first-person diary prose
  dense        halfvec(1024)           -- BGE-M3 embed of body (NULL until embedded)
  fts          TSVECTOR GENERATED      -- to_tsvector('simple', body), STORED
  conv_count   INT DEFAULT 0           -- conversations the entry was written from
  model_used   TEXT                    -- writer model (claude-sonnet-4-6)
  created_at   TIMESTAMPTZ DEFAULT now()
```

Since migration `030`, shared hippocampus deployments retain which chassis
experienced a memory. Diary rows store writer `host`, `runtime`, and
`memory_mode`, plus a JSONB snapshot of every source session's `session_id`,
`runtime`, `model`, and `host`. Conversation hosts default to the ingest
machine hostname and can be labelled with `HIPPOCAMPUS_SOURCE_HOST` (useful
for containers); `HIPPOCAMPUS_WRITER_HOST` and `HIPPOCAMPUS_WRITER_RUNTIME`
override the diary writer identity.

One row per day means at most ~365 rows/year, so **no HNSW**: a seq scan over
`dense` is sufficient and a GIN over `fts` covers text recall. The embed obeys
the same cosine/inner-product invariant as every other dense column (see
[EMBED_CONTRACT.md](./EMBED_CONTRACT.md)). `entry_date` is the PK, so re-running
a day `UPSERT`s (`ON CONFLICT (entry_date) DO UPDATE`) rather than duplicating.

## Writing — `hippocampus diary`

```bash
sops exec-env "$CREDS_DIR/llm.enc.yaml" \
  '.venv/bin/hippocampus diary [--date YYYY-MM-DD] [--backfill N] [--window K]
                               [--platforms a,b] [--force] [--dry-run]
                               [--drift-report]'
```

| Option | Default | Meaning |
|---|---|---|
| `--date` | yesterday (JST) | target day to write |
| `--backfill N` | — | process the last `N` days instead of one (skips existing) |
| `--window K` | `7` | prior diary days fed for continuity (`0` = stateless writer) |
| `--platforms` | `claude_code` | comma-separated source platforms |
| `--force` | off | regenerate even if an entry for the date exists |
| `--dry-run` | off | print what would be processed without calling the model |
| `--drift-report` | off | print the day-to-day drift trajectory and exit |

Needs an Anthropic key for the writer model, resolved
`ANTHROPIC_API_KEY_INGEST` → `CF_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY`. In
this deployment the key lives in `llm.enc.yaml`, **not** `hippocampus.enc.yaml`.
It also needs the BGE-M3 embed backend to vectorize the entry.

**What it does per day** (`ingest/diary.py`):

1. **Day selection** — conversations with `msg_count > 1` whose
   `coalesce(ended_at, started_at)` falls on the target JST date.
2. **Transcript build** — for each conversation, sample prose turns
   (seq-first, prose ≥ `MIN_PROSE_LEN`, diffs skipped) up to a per-conversation
   cap, then trim the assembled day stream to `DAY_MSG_BUDGET` total messages so
   one long session can't monopolise the budget.
3. **Prior window** — the last `--window` diary entries strictly before the
   target date, chronological, each capped to `PRIOR_BODY_CAP` chars.
4. **Write** — one `claude-sonnet-4-6` call produces the first-person prose.
   The prompt carries the grounding rules, the prior-window block (content-only,
   tone-mimicry banned), and the shared transcript-as-data guard.
5. **Degenerate gate** — the output is checked by `looks_degenerate`; a
   transcript echo or a too-short body is rejected and retried up to
   `WRITE_RETRIES` times. If every attempt is degenerate the day is **skipped**
   (status `skip-degenerate`) rather than persisting garbage.
6. **Embed + store** — the body is embedded and UPSERTed; the day-to-day drift
   vs. the previous entry is computed and printed.

### Status strings

`process_day` returns one of: `written`, `skip-exists` (entry already present,
no `--force`), `skip-empty` (no conversations / empty transcript),
`skip-degenerate` (all write attempts echoed the transcript — distinct from
`skip-empty` so the instruction-hijack rate stays visible), `dry`, or `fail`.

## Instruction-hijack defence

A diary writer is especially exposed to transcript instruction-hijack: the day's
conversation almost always *contains the literal request "write a diary"*, and a
naive prompt will obey it and echo the turn instead of observing it (the
2026-04-17 incident stored `"Human: 日記を書いてください。"` as a diary entry).
Two layers, shared with `summarize` and `extract-facts` via
[`ingest/llm_guard.py`](../src/hippocampus/ingest/llm_guard.py):

- **Framing.** The prompt states that requests/commands appearing inside the
  conversation are *records to observe*, not instructions to follow, and that
  turns must not be transcribed verbatim.
- **Output gate.** `looks_degenerate` rejects output shorter than
  `MIN_DIARY_LEN` or beginning with a role marker (`[USER]`, `Human:`, …); the
  writer retries, then skips.

## Refusal fallback (cross-family)

The writer asks the model for a candid character observation of a real person.
The provider's refusal classifier occasionally fires on that — even though the
subject is the operator, the data is their own authorized conversations, and the
output is private and store-only — when the day's transcript carries
security-lab vocabulary. On 2026-07-05 the refusal was robust across
`claude-sonnet-4-6`, `claude-sonnet-5`, and `claude-opus-4-8`, so retrying the
same provider is futile.

Two mechanisms handle it:

- **Never crash.** A `stop_reason='refusal'` (or any response with no text block)
  yields an empty content list. `write_diary` treats that like degenerate output
  — logs the `stop_reason`, retries, and ultimately skips — instead of indexing
  `content[0]` and throwing `IndexError` (which previously escaped the retry loop
  and killed the whole day).
- **Self-heal.** When the primary writer *refuses* (as opposed to degenerating),
  the same prompt is handed to a cross-family CLI whose calibration differs
  (`FALLBACK_WRITERS` = codex/GPT, then kimi/Moonshot). These use their own CLI
  auth (no `ANTHROPIC_API_KEY` needed) and run non-interactively. The stored
  `model_used` records which writer actually produced the entry, so provenance
  stays honest and the drift meter registers the model change.

**Data-egress note.** A fallback sends that day's transcript to the fallback
provider. It is enabled by operator choice; disable with
`HIPPOCAMPUS_DIARY_FALLBACK_DISABLE=1` (the day then records `skip-degenerate`).

## Drift report

```bash
sops exec-env "$CREDS_DIR/hippocampus.enc.yaml" \
  '.venv/bin/hippocampus diary --drift-report'
```

Prints the cosine distance between each entry and the previous one
(`dense <=> lag(dense)`), the mean and standard deviation, and a flag threshold
of mean + `DRIFT_FLAG_SIGMA`·σ. Days exceeding the threshold are marked
`<-- DRIFT`. This is the gauge that keeps the continuity feedback loop honest —
a sustained climb is the signal that tone is compounding despite the window and
the tone-mimicry ban.

## Tunable constants

All in `ingest/diary.py`:

| Constant | Value | Role |
|---|---|---|
| `DIARY_MODEL` | `claude-sonnet-4-6` | writer model |
| `DIARY_MAX_TOKENS` | `1536` | writer output cap |
| `PRIOR_WINDOW` | `7` | continuity window (leaky-integrator span) |
| `PRIOR_BODY_CAP` | `700` | per prior-entry char cap in context |
| `DAY_MSG_BUDGET` | `140` | total transcript messages per day |
| `PER_CONV_CAP` | `60` | per-conversation sample cap |
| `PROSE_MAX_CHARS` | `400` | per-message prose trim |
| `MIN_DIARY_LEN` | `120` | below this = degenerate |
| `WRITE_RETRIES` | `2` | attempts before `skip-degenerate` |
| `DRIFT_FLAG_SIGMA` | `2.0` | flag threshold = mean + Nσ |
| `EMBED_MAX_LENGTH` | `512` | embed truncation length |

## Daily operation

`scripts/cron_ingest.sh` runs the pass last, after `ingest` (so the day's
sessions are present) and after `summarize` / `extract-facts`:

```bash
hippocampus ingest claude-code
hippocampus ingest codex
hippocampus summarize     --limit 200
hippocampus extract-facts --limit 200
hippocampus diary                       # default: yesterday (JST)
```

It needs both credential files chained (`hippocampus.enc.yaml` for `PG_URL` /
`BGE_EMBED_URL`, `llm.enc.yaml` for the Anthropic key). To rebuild history after
a gap, `hippocampus diary --backfill N` walks back `N` days and skips ones that
already have an entry.

## Pointers

| Topic | Document |
|---|---|
| Embedding invariant | [EMBED_CONTRACT.md](./EMBED_CONTRACT.md) |
| Distilled facts layer | [EXTRACTED_FACTS.md](./EXTRACTED_FACTS.md) |
| Ingest pipeline | [INGEST_PIPELINE.md](./INGEST_PIPELINE.md) |
| Architecture overview | [ARCHITECTURE.md](./ARCHITECTURE.md) |
