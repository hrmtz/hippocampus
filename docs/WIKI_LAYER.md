# LLM-Wiki Layer — operator stub

A **living, editable, correctable** knowledge base for *subject knowledge* (the
音律 / 和音 / CTF / 物理 material the user actually learns) — distinct from the
user-fact memory layer and the first-person diary (mig 026). Re-learning a topic
**merges into** the existing page instead of appending yet another snapshot.

This is an operator quick-reference. The full design (motivation, the MVP cut,
schema rationale, security envelope, dual-magi history) is the source of truth:
**`docs/designs/LLM_WIKI_LAYER.md`** (plateau v4, issue #50).

## The one load-bearing idea

```
① capture   = raw conv log (lossless, append-only) — already exists
② synthesis = compact fragments into a page — LOSSY (an LLM summary), human-gated
```

- `wiki_pages.body_md` is the **durable primary SoT** (the synthesized page).
- `wiki_claims` are a **re-derivable projection of the approved body** (audit /
  dedupe), derived *after* you approve the prose — never an accumulating primary.
- **Human diff approval is THE trust control.** The model proposes; you read the
  unified diff + claim checklist and only then `apply`.
- **Never self-ground:** the ingest/extract pipeline is forbidden from reading
  `wiki_*` (enforced by `scripts/check_wiki_self_ingestion.sh`), so a page can
  never re-ingest its own lossy output as fresh evidence.

## Schema (migration 027, schema `personal`)

| table | role |
|---|---|
| `wiki_pages` | durable `body_md` SoT (no FK to conversations) |
| `wiki_claims` | derived projection, fully replaced each apply |
| `wiki_merge_log` | append-only audit + `prior_body` rollback snapshot |
| `wiki_merge_staging` | proposed merge awaiting apply (`base_plateau_rev` staleness check) |
| view `v_wiki_inject_safe` | redacted read (body only, no provenance) for `agent_read_mcp` |

The append-only writer is the **`agent_wiki_writer`** role (NOLOGIN; used via
`SET LOCAL ROLE` inside the owner's apply tx — INSERT-only on `wiki_merge_log`
is genuinely privilege-enforced, asserted by an invariant DO-block in 027).

## Enabling the layer

027 ships the feature flag **OFF** — `migrate` is inert until an operator flips it.


```bash
# 1. apply 027 (operator-gated; against canonical only after backup + sign-off)
hippocampus migrate --status            # confirm 027 pending
hippocampus migrate                     # core tier; ships flag OFF

# 2. flip the flag ON (manual enable after smoke)
sops exec-env "$CREDS_DIR/hippocampus.enc.yaml" 'psql "$PG_URL" -c "
  UPDATE personal.feature_flags SET enabled=TRUE WHERE flag_name=$$wiki_layer$$"'
```

Kill switch: `UPDATE personal.feature_flags SET enabled=FALSE WHERE flag_name='wiki_layer'`.
`status` works regardless of the flag; `propose`/`apply` `SystemExit` while it is OFF.

## Usage — propose / apply / rollback / status

LLM passes (`propose`, and `apply`'s op-summary) need the Anthropic key, which
lives in `llm.enc.yaml` (`ANTHROPIC_API_KEY_INGEST`), **not** the PG secrets:

```bash
# inspect (read-only, no flag, no LLM) — safe anytime
hippocampus wiki status
hippocampus wiki status --page music-route-a

# propose a merge from a conversation into a page (drafts body_md, derives claims,
# stages the merge, prints a unified diff + claim checklist + an apply hint)
sops exec-env "$CREDS_DIR/llm.enc.yaml" \
  '.venv/bin/hippocampus wiki propose --conv-id <CONV> --page music-route-a'
#   optional: --section <name> --title/--domain (new-page bootstrap) --dry-run

# review the diff, then apply by merge-id (one all-or-nothing tx; idempotent —
# a double-apply no-ops on UNIQUE(merge_id))
sops exec-env "$CREDS_DIR/llm.enc.yaml" \
  '.venv/bin/hippocampus wiki apply --merge-id <MERGE_ID>'

# rollback = re-apply the prior_body as a NEW append (plateau_rev++), not a
# destructive undo
sops exec-env "$CREDS_DIR/llm.enc.yaml" \
  '.venv/bin/hippocampus wiki rollback --merge-id <MERGE_ID>'
```

Notes:
- **Single-page confinement:** a proposal writes exactly one page; foreign-slug
  claims are dropped at propose and re-rejected in the applier.
- **Staleness:** apply rejects (`expired`) if the page moved since propose
  (`base_plateau_rev` mismatch) — re-propose against the new body.
- **Fatigue flags** (oversized diff / low evidence ratio) are surfaced in the
  propose review output as warnings, not hard blocks — they are a signal to you.

## Tests / guards

```bash
.venv/bin/python3 scripts/test_wiki.py        # pure-python unit + manifest static
.venv/bin/python3 scripts/test_migrate_scanner.py  # 027 manifest/no_tx cross-check
bash scripts/check_wiki_self_ingestion.sh     # self-ingestion exclusion gate
bash scripts/smoke_wiki.sh                     # CLI shape smoke (no DB/network)
```

End-to-end propose→apply→rollback and the 027 DDL apply should be exercised
**only against a throwaway PG** (e.g. `HIPPOCAMPUS_MIGRATE_DB=<scratch>` for the
migration, `PG_URL` on a disposable db for `status`/apply) — never canonical in a
test pass. The down-migration is `migrations/027_wiki_down.sql` (run manually:
`psql -v ON_ERROR_STOP=1 -f migrations/027_wiki_down.sql`; never in the manifest).
