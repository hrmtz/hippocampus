-- ⚠️ APPLY: psql -f migrations/013_conversation_project.sql
--    Do NOT use `-1` / `--single-transaction` flag.
--    This file contains CREATE INDEX CONCURRENTLY which forbids tx blocks.
-- Requires: PostgreSQL >= 9.5 (= hippocampus canonical is PG 16).
--
-- Phase 1/6: schema foundation only.
--
-- Adds personal.conversations.project_slug TEXT (nullable) with sentinel CHECK
-- + canonical_project_slug() SQL function (SoT for slug normalization) +
-- partial index for current_project-scoped recall.
--
-- Scope OUT (= subsequent phases、 epic #13):
--   Phase 2  (#14): ingest patch (cwd → slug) + backfill ~190 jsonl
--   Phase 2b (#15): codex ingest extension (~/.codex/sessions/)
--   Phase 3  (#16): inject governance (exclusion paths + feature_flags)
--   Phase 4  (#17): inject audit + role (read_log + view + role REVOKE/GRANT)
--   Phase 5  (#18): inject lifecycle (allowlist + purge + slug_history)
--   Phase 6  (#19): SessionStart inject hook
--
-- Negative declaration: this migration does NOT spec any retrieval pattern.
-- Sample SELECT examples are excluded by design (= round 1 REJECT 1 mitigation).
-- Access control stays as-is in Phase 1; view + role separation lands in Phase 4.
-- Phase 6 hook implementation is BLOCKED on Phase 4 completion.
--
-- Sentinel namespace invariant: ALL sentinels MUST follow `__<name>__` shape
-- (double underscore prefix + suffix). Phase 3 will extend with `__excluded__`,
-- Phase 4+ may add more. Function and CHECK regex both depend on this.
--
-- ⚠️ Phase 1 access control gap: CHECK accepts sentinel writes from any role
-- that can UPDATE personal.conversations. An attacker (or buggy direct-SQL
-- session) can overwrite a valid claude_code slug with '__no_project__' to
-- silently break Phase 6 inject filter (= conv no longer matches current_project).
-- Operational rule until Phase 4 (#17) lands role REVOKE/GRANT:
-- NEVER touch project_slug via direct SQL — go through ingest pipelines only.
--
-- Phase 2 (#14) responsibilities (= NOT enforced by this schema):
--   - call personal.canonical_project_slug() from ingest patches (no Python re-impl)
--   - audit ghost vault: SELECT DISTINCT source_project FROM agent.ghost_memories
--     and verify personal.canonical_project_slug(NULL, src) bit-equal MATCHES
--     each existing slug (else join key drift will break Phase 6 inject)
--   - INSERT ... ON CONFLICT (conv_id) DO UPDATE SET
--       project_slug = COALESCE(personal.conversations.project_slug,
--                               EXCLUDED.project_slug)
--     (= initial-set-then-immutable; Phase 2 may add BEFORE UPDATE trigger)
--   - non-claude_code/codex platforms: explicit SET project_slug='__no_project__'
--     (Phase 2 may add BEFORE INSERT trigger; Phase 1 leaves enforcement to ingest)
--   - empirical smoke for canonical_project_slug() false-negative rate on real cwd
--     distribution before Phase 6 deploy
--
-- Phase 4 (#17) responsibilities:
--   - REVOKE EXECUTE ON FUNCTION canonical_project_slug() FROM PUBLIC; GRANT to ingest role
--   - role REVOKE/GRANT for personal.conversations + view-based read path
--
-- Phase 6 (#19) responsibilities:
--   - hook filter MUST use strict `project_slug = current_project` (NOT include
--     '__no_project__' OR-clause) to prevent platform-less rows leaking into
--     every project session
--
-- Rollback: psql -f migrations/013_conversation_project_down.sql
-- Dependencies: 001 (personal schema + conversations table)

SET lock_timeout = '3s';

-- canonical_project_slug() = SoT for git remote URL → canonical slug.
-- Replaces Python _canonical_project_name() in scripts/ghost_context_inject.py
-- (Phase 2+ ingest patches MUST call this function instead of re-implementing).
--
-- Contract:
--   input:  p_remote_url     = git remote.origin.url (e.g. https://github.com/user/repo.git)
--                              or NULL / empty / garbage
--           p_cwd_basename   = fallback when remote_url unavailable
--                              (e.g. "/home/user/projects/hippocampus-mcp")
--   output: valid slug matching [A-Za-z0-9][A-Za-z0-9_-]{0,62}
--           OR '__unresolved__' sentinel (= unparseable input / empty / non-ASCII)
--   NEVER returns NULL or '__no_project__' (= caller's responsibility for explicit set).
--
-- Properties: IMMUTABLE / PARALLEL SAFE / SECURITY INVOKER
-- NOT STRICT (default; NULL handled via sentinel cascade inside function).
-- LEAKPROOF intentionally OMITTED (= reserved for Phase 4 RLS context, where
-- regexp_replace memory-exhaustion edge case will be re-evaluated under
-- explicit input-size cap; superuser-only attribute creates rollout friction).
--
-- Case is PRESERVED (= matches Python _canonical_project_name() in
-- scripts/ghost_context_inject.py:25-46 for join key compat with
-- agent.ghost_memories.source_project).
-- Trailing-slash strip MUST match Python rstrip('/') exactly.
--
-- KNOWN drift from Python _canonical_project_name() (= edge cases only):
--   (a) scp-style root remotes (e.g. `git@host:repo.git`):
--       Python → `git@host:repo`, SQL → `repo`. SQL is cleaner.
--   (b) URL-host fallback (e.g. `https://github.com//`, no repo path):
--       Python → `github.com`, SQL → `__unresolved__` (= '.' fails ASCII regex).
--   (c) Non-ASCII basenames (e.g. cwd `/path/プロジェクト`):
--       Python → 'プロジェクト' (no validation), SQL → '__unresolved__' (regex strict).
--   (d) Python lacks `__unresolved__` sentinel (= returns cwd.name on failure),
--       SQL returns sentinel.
-- Phase 2 ingest MUST call personal.canonical_project_slug() from SQL (= NOT
-- patch Python piecemeal); Python ghost_context_inject.py should be migrated
-- to a thin wrapper calling the SQL function for join-key consistency.
-- A Phase 2 deliverable is to audit agent.ghost_memories.source_project against
-- SQL function output and remediate divergences before Phase 6 hook deploy.
--
-- Phase 2 ingest discipline (= NOT enforced by this function):
--   - strip NUL bytes (0x00) from cwd/remote_url before passing in.
--     PostgreSQL TEXT type rejects 0x00 at INSERT, so a NUL-containing arg
--     would error before this function's CASE can fall through to sentinel.

CREATE OR REPLACE FUNCTION personal.canonical_project_slug(
    p_remote_url   TEXT,
    p_cwd_basename TEXT
) RETURNS TEXT
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    SECURITY INVOKER
AS $$
    WITH
        capped AS (
            -- input size guard: reject pathological inputs early (= ReDoS / OOM)
            SELECT
                CASE WHEN length(coalesce(p_remote_url, ''))   > 4096 THEN NULL ELSE p_remote_url   END AS url,
                CASE WHEN length(coalesce(p_cwd_basename, '')) > 4096 THEN NULL ELSE p_cwd_basename END AS cwd
        ),
        candidate AS (
            -- prefer remote_url, fall back to cwd_basename; trim + rstrip('/') + NULLIF
            SELECT COALESCE(
                NULLIF(regexp_replace(trim(url), '/+$', ''), ''),
                NULLIF(regexp_replace(trim(cwd), '/+$', ''), '')
            ) AS raw
            FROM capped
        ),
        normalized AS (
            SELECT
                regexp_replace(
                    regexp_replace(
                        regexp_replace(raw, '\.git$', ''),
                        '^(git@[^:]+:|https?://[^/]+/)', ''
                    ),
                    '.*/', ''
                ) AS slug
            FROM candidate
        )
    SELECT CASE
        WHEN slug IS NULL OR slug = '' THEN '__unresolved__'
        WHEN slug ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$' THEN slug
        ELSE '__unresolved__'
    END
    FROM normalized;
$$;

-- project_slug column (nullable until Phase 2 backfill completes)
ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS project_slug TEXT;

-- sentinel CHECK constraint:
--   NULL              = not yet backfilled (Phase 2 will resolve or set sentinel)
--   __no_project__    = platform has no project concept (bulk imports etc; set by Phase 2 ingest)
--   __unresolved__    = claude_code/codex but resolve failed (set by canonical_project_slug())
--   valid slug        = [A-Za-z0-9][A-Za-z0-9_-]{0,62} (= canonical_project_slug() output)
-- __excluded__ is intentionally NOT included; Phase 3 (#16) will ALTER CHECK to add it
-- alongside the conversation_inject_excluded_paths table.

ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conv_project_slug_valid;

ALTER TABLE personal.conversations
    ADD CONSTRAINT conv_project_slug_valid
    CHECK (
        project_slug IS NULL OR
        project_slug ~ '^(__(no_project|unresolved)__|[A-Za-z0-9][A-Za-z0-9_-]{0,62})$'
    ) NOT VALID;

-- VALIDATE with ShareUpdateExclusive (not AccessExclusive) — concurrent with reads.
-- All existing 56k rows are NULL, so this is effectively a no-op but cleans the
-- "NOT VALID" state from pg_constraint so future schema introspection is clean.
ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conv_project_slug_valid;

RESET lock_timeout;

-- partial index for current_project-scoped recall.
-- Excludes NULL (= not backfilled) AND sentinels (= '__no_project__' would
-- otherwise bloat index with 50k+ rows from bulk imports etc).
-- started_at IS NOT NULL excludes legacy rows with missing timestamp.
--
-- ⚠️ MUST run in autocommit mode. Do NOT use `psql -1` / `--single-transaction`.
-- This statement is intentionally OUTSIDE any BEGIN/COMMIT block.

-- Bounded lock_timeout for the index build phase. CREATE INDEX CONCURRENTLY
-- can wait indefinitely on long ingest INSERTs without this; 60s is generous
-- but bounded (operator can retry).
SET lock_timeout = '60s';

-- Use IN-list (NOT LIKE pattern) for sentinel exclusion to avoid silently
-- dropping valid slugs whose names happen to start with __ (e.g. `__init__`).
-- IN-list must be updated when Phase 3+ adds new sentinels — note PG has no
-- ALTER INDEX...SET WHERE; Phase 3 MUST `DROP INDEX CONCURRENTLY
-- personal.idx_conv_project_slug_started; CREATE INDEX CONCURRENTLY ... NOT IN
-- (..., '__excluded__', ...);` as 2 separate statements (= second 60s lock_timeout
-- window + same INVALID-detection DO block).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_project_slug_started
    ON personal.conversations (project_slug, started_at DESC)
    WHERE project_slug IS NOT NULL
      AND project_slug NOT IN ('__no_project__', '__unresolved__')
      AND started_at IS NOT NULL;

-- Post-apply verification (= automated, loud-fail on INVALID index).
-- DO block runs in autocommit (= same context as CREATE INDEX CONCURRENTLY).
DO $$
DECLARE
    v_oid   OID;
    v_valid BOOLEAN;
BEGIN
    -- to_regclass() returns NULL on missing object (vs ::regclass which raises);
    -- this lets the "not found" branch actually fire with the curated message.
    v_oid := to_regclass('personal.idx_conv_project_slug_started');

    IF v_oid IS NULL THEN
        RAISE EXCEPTION 'idx_conv_project_slug_started not found after CREATE — apply failed';
    END IF;

    SELECT indisvalid INTO v_valid FROM pg_index WHERE indexrelid = v_oid;

    IF NOT v_valid THEN
        RAISE EXCEPTION 'idx_conv_project_slug_started is INVALID (CONCURRENTLY build failed mid-way). Run: DROP INDEX CONCURRENTLY personal.idx_conv_project_slug_started; then re-apply this migration.';
    END IF;
END
$$;

RESET lock_timeout;
