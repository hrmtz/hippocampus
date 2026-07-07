-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/014_inject_governance.sql
--    Do NOT use `-1` / `--single-transaction` (= contains CREATE INDEX CONCURRENTLY).
-- Requires: PostgreSQL >= 9.5; migration 013 (= project_slug + canonical fn) applied.
--
-- Phase 3/6: inject governance — exclusion paths + feature_flags.
--
-- Adds the governance rail that the Phase 6 SessionStart hook will consult
-- before injecting any conversation summary:
--   1. personal.conversation_inject_excluded_paths — cwd path prefixes that
--      mark a session as off-limits (= pentest, medical, client work).
--      Ships EMPTY; user-specific prefixes are seeded by `hippocampus init`
--      (= r1-privacy-5; operator seed in migrations.local/, gitignored).
--   2. personal.feature_flags — operational kill switch table.
--      Seeds conversation_project_inject = FALSE (default OFF).
--      Phase 6 hook MUST AND-gate with env var HIPPOCAMPUS_PERSONAL_INJECT_DISABLE.
--   3. Extends conv_project_slug_valid CHECK to allow '__excluded__' sentinel.
--   4. Rebuilds idx_conv_project_slug_started (= PG lacks ALTER INDEX...SET WHERE)
--      to add '__excluded__' to the NOT IN list (= avoids index bloat).
--
-- Scope OUT:
--   Phase 4 (#17): view + role separation + audit log
--   Phase 5 (#18): allowlist + purge + slug_history
--   Phase 6 (#19): SessionStart hook itself
--
-- Rollback: psql -f migrations/014_inject_governance_down.sql
-- Dependencies: 013 (= project_slug column + canonical_project_slug + index)

SET lock_timeout = '5s';

-- (1) Exclusion path table.
-- path_prefix MUST be absolute and end with '/' for prefix-match semantics
-- (= "starts-with" rather than substring, to avoid false-positive on cwd
-- containing the segment elsewhere in the path).
CREATE TABLE IF NOT EXISTS personal.conversation_inject_excluded_paths (
    path_prefix TEXT PRIMARY KEY
        CHECK (path_prefix ~ '^/.*/$'),
    reason      TEXT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Default: EMPTY table (r1-privacy-5). Sensitive path prefixes are
-- user-specific policy, not shippable SQL: they are seeded interactively by
-- `hippocampus init` (which prompts for the user's own sensitive paths) or
-- via manual INSERT. Operator-local seed rows live in
-- migrations.local/014_operator_denylist_seed.sql (gitignored).

-- (2) feature_flags table — operational kill switch.
CREATE TABLE IF NOT EXISTS personal.feature_flags (
    flag_name        TEXT PRIMARY KEY,
    enabled          BOOL NOT NULL DEFAULT FALSE,
    disabled_reason  TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO personal.feature_flags (flag_name, enabled, disabled_reason)
VALUES
    ('conversation_project_inject', FALSE,
     'pending Phase 4 (audit+role) and Phase 6 (hook implementation)')
ON CONFLICT (flag_name) DO NOTHING;

-- (3) Extend CHECK constraint to allow '__excluded__' sentinel.
-- Use NOT VALID + VALIDATE pattern (= consistent with migration 013).
ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conv_project_slug_valid;

ALTER TABLE personal.conversations
    ADD CONSTRAINT conv_project_slug_valid
    CHECK (
        project_slug IS NULL OR
        project_slug ~ '^(__(no_project|unresolved|excluded)__|[A-Za-z0-9][A-Za-z0-9_-]{0,62})$'
    ) NOT VALID;

ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conv_project_slug_valid;

RESET lock_timeout;

-- (4) Rebuild partial index to add '__excluded__' to NOT IN list.
-- Per migration 013 ritual: PG has no ALTER INDEX...SET WHERE; we must
-- DROP + CREATE CONCURRENTLY as 2 statements outside any transaction block.
-- ⚠️ Brief window where the index is absent — queries fall back to seq scan.
-- Acceptable at 56k row scale; revisit for larger tables.

SET lock_timeout = '60s';

DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_project_slug_started;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_project_slug_started
    ON personal.conversations (project_slug, started_at DESC)
    WHERE project_slug IS NOT NULL
      AND project_slug NOT IN ('__no_project__', '__unresolved__', '__excluded__')
      AND started_at IS NOT NULL;

DO $$
DECLARE
    v_oid   OID;
    v_valid BOOLEAN;
BEGIN
    v_oid := to_regclass('personal.idx_conv_project_slug_started');
    IF v_oid IS NULL THEN
        RAISE EXCEPTION 'idx_conv_project_slug_started not found after CREATE — apply failed';
    END IF;
    SELECT indisvalid INTO v_valid FROM pg_index WHERE indexrelid = v_oid;
    IF NOT v_valid THEN
        RAISE EXCEPTION 'idx_conv_project_slug_started is INVALID. Run: DROP INDEX CONCURRENTLY personal.idx_conv_project_slug_started; then re-apply migration 014.';
    END IF;
END
$$;

RESET lock_timeout;
