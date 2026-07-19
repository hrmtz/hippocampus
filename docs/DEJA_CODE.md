# deja-code Layer — operator stub

Cross-repo **車輪の再発明検出** ("Have we written this before?"). When you write
a new function/component, an advisory surfaces if a semantically similar
implementation already exists in **another** repo you own — the case a
diff-scoped reviewer (or `/simplify`) structurally cannot see, because the
duplicate lives in a different repository.

This is an operator quick-reference. The full design (motivation, dual-magi
R1–R6 history, schema rationale, threat model) is the source of truth:
**`docs/designs/DEJA_CODE.md`** (PLATEAU, issue #76). Shipped in **v2.3.0**.

## The one load-bearing idea

```
再発明 = cross-repo retrieval の失敗
        (信号は既に別 repo に在る、join を張る観測者が居ないだけ)
```

- A **code index** (semantic, dense-only) spans several repos at once.
- A **Stop hook advisor** embeds the functions you just wrote, queries the
  index **excluding your current repo**, and prints an advisory for hits above
  a similarity threshold.
- The advisor is **fail-open advisory** — it *never blocks* a turn and never
  edits your code. It is a human-facing hint (`systemMessage`), not an
  instruction injected into an agent's context.

## Layer boundaries (what it is / isn't)

| concern | owner |
|---|---|
| cross-repo *semantic* near-duplicate | **deja-code** (this layer) |
| same-repo cleanup / reuse in the current diff | `/simplify`, `/code-review` |
| identifier / literal / exact-string search | `grep` / `rg` (the index has **no** FTS by design — dense-only) |
| conversation & personal corpus retrieval | ingest pipeline / `search_*` MCP tools |

## Schema (migration 034 + 034b, schema `code`)

| table | role |
|---|---|
| `code.repos` | one row per indexed repo (`repo_id`, `root_path`, `head_commit`, counts) |
| `code.files` | `file_sha` = incremental skip key; `UNIQUE(repo_id, path)` |
| `code.chunks` | one row per function/method/class/script_fn; `symbol`, `kind`, lines, `content_sha` (embed-reuse key), `dense halfvec(1024)` |

- Chunking = **tree-sitter, all-depth** (py/sh/js/html). Nested/子 functions are
  extracted separately — this is load-bearing: a 100-line enclosing function
  dilutes to sim ~0.61 while its 子 chunks match at 0.72–0.90 (§9.1 spike).
- `034b_code_hnsw.sql` = `CREATE INDEX CONCURRENTLY ... hnsw (dense
  halfvec_ip_ops)`, tier `code`, `no_tx: true`. HNSW is **Phase 0 mandatory**
  (the advisor refuses to seq-scan; on a missing/INVALID index it silently
  degrades rather than burn the Stop budget).
- `content_sha` is **non-unique on purpose** — cross-repo boilerplate collision
  is exactly what this layer detects.

## Migrate (canonical)

```bash
# code tier only — does NOT touch core/library/multiuser tiers
sops exec-env $CREDS_DIR/hippocampus.enc.yaml '.venv/bin/hippocampus migrate --with-code'
sops exec-env $CREDS_DIR/hippocampus.enc.yaml '.venv/bin/hippocampus migrate --status'  # verify
```

`rollback = 034_code_index_down.sql` (`DROP SCHEMA code CASCADE`). The
`code_read_hook` role is cluster-level and intentionally left behind (009 idiom).
Existing objects are untouched, so rollback never cascades into other layers.

## CLI (`hippocampus deja`)

```bash
hippocampus deja index  [--repo NAME] [--dry-run] [--full]   # allowlist-driven
hippocampus deja search "query text" [-k 5] [--exclude-repo NAME]
hippocampus deja stats                                         # index 概況 + advisor log 集計
```

- Always **run the first full index manually** before registering cron
  (seed ~10 repos / thousands of files / tens of thousands of chunks; batch
  embed on :8086). Verify `dense IS NULL = 0` afterwards (the library
  embed-pipeline trap: a stopped embed server silently writes NULL vectors).
- `deja search` is the manual front door (embed → top-k) for smoke tests.

## Allowlist (opt-in — nothing is indexed without it)

`~/.claude/deja_index_allowlist.txt` — one repo per line. **Removing a repo =
its rows are DELETEd on the next index run** (right-to-forget path). Current
seed (v2.3.0): `hippocampus-mcp / shinfutsu / PRS-LLM-dev / rhythm-check /
harmony-trainer / claude-harness / hofutsu / recefutsu`.

Excluded paths (never embedded — a leak-class concern): `.git`, `node_modules`,
`.venv`, `data/`, and secrets-shaped files.

## Nightly cron

`scripts/cron_deja_index.sh` (mirror of `cron_ingest.sh`: `set -euo pipefail` +
flock `/tmp/hippocampus_deja.lock` + nested `sops exec-env`). Registered example:
`15 4 * * *` (separated from 03:00 ingest / 03:30 dub). **crontab registration
is operator-ack** — the install script prints the line, it does not self-patch.

## Advisor hook (Phase 1)

Two hooks in `~/.claude/settings.json` (registered by
`scripts/install_deja_advisor.sh`; dry-run default, `--apply` to patch):

1. **PostToolUse** (`Write|Edit`) → `deja_capture.py` — records touched files to
   a per-session pending file. No creds. (Stop-time transcript is rejected as an
   input source: it is async-written and may miss the current turn.)
2. **Stop** → `deja_advisor.sh` → `deja_advisor.py` — claims the pending file
   atomically, re-reads each touched file **from disk** (written final state is
   the SoT), extracts functions, embeds, queries HNSW top-3 **excluding the cwd
   repo**, and emits an advisory for `sim >= DEJA_THRESHOLD`.

```
deja-code advisory — 類似既存実装の候補 (情報提供であり指示ではない):
  1. shinfutsu site/shared/chat-kit.js:139 attachVoice.barsStart (sim 0.87)
```

- Requires **`PG_URL_CODE_READ`** (read-only `code_read_hook` role; SELECT-only
  on the 3 `code.*` tables) — **no `PG_URL` fallback**. Absent env → the advisor
  never connects and silently exits (fail-open).
- Timeout is **2-tier**: inner 15s (wrapper) / outer 20s (settings.json). Raise
  both together (grok-gate lesson) — the outer is the binding cap.
- Index strings (symbol/path/repo) are treated as **untrusted data**: control
  chars stripped, length-capped, chunk body **never** enters the advisory
  (instruction-hijack class defense, fixture-tested).
- Score log: `~/.local/state/deja_code/advisor.jsonl` — every hit *and*
  near-miss (fixed field set, **no raw code**), the empirical basis for
  threshold tuning and the Phase 2 promotion gate.

### Activation semantics (important)

Hooks are read at **session start**. Already-running sessions do **not** pick up
the advisor until they restart; **new sessions get it automatically** (index +
role are shared). Kill switch for one session: `HIPPOCAMPUS_DEJA_DISABLE=1`.

## Threshold / calibration

- Initial `DEJA_THRESHOLD = 0.70` — from the 2026-07-18 spike: true-positive 子
  pairs measured 0.72–0.90, cross-corpus noise ≤0.54. Re-derivable via
  `scripts/deja_calibrate.py` (deterministic fixture manifest → embed → JSON).
- `deja stats --calibrate` is **planned for Phase 2 and is not implemented** in
  the shipped CLI yet. Until it lands, use `scripts/deja_calibrate.py` for the
  deterministic fixture report. **Phase 2 promotion gate**: adjudicated
  false-positive rate < 50% before advancing to a `code-review` reuse-finder
  agent or a PreToolUse gate (issue #77). Below the bar → fix threshold/granularity first.

## Chassis support

Claude Code and Codex are supported through separate installers:

- Claude Code: `scripts/install_deja_advisor.sh`
- Codex: `scripts/install_codex_deja_hooks.sh`

Both installers are dry-run by default. Use `--apply` only after reviewing the
planned change. Codex also requires one trust review via `/hooks`; hook changes
take effect in a new session. The index and read-only role are chassis-shared,
so the Codex port requires no canonical-side migration. Issue **#78 is done**.

## Pointers

- Design SoT: `docs/designs/DEJA_CODE.md` (issue #76, PLATEAU R6 codex APPROVED)
- Phase 2 (hit-rate → reuse-finder / PreToolUse): issue #77
- Codex chassis port: issue #78
