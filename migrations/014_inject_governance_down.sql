-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/014_inject_governance_down.sql
--    Do NOT use `-1` / `--single-transaction` (= contains DROP INDEX CONCURRENTLY).
--
-- Phase 3 rollback: undoes 014_inject_governance.sql.
--
-- ⚠️ WARNING: Phase 3 solo rollback only. If Phase 4+ migrations reference
-- conversation_inject_excluded_paths / feature_flags tables, this will FAIL
-- with cannot drop because other objects depend on it (intentional).
--
-- Restores the migration 013 state:
--   - CHECK constraint shrinks back to (__no_project__|__unresolved__)
--   - partial index rebuilt without __excluded__ in NOT IN list
--   - tables conversation_inject_excluded_paths + feature_flags dropped
--
-- ⚠️ DATA LOSS: any row with project_slug='__excluded__' will violate the
-- restored CHECK. Before running this rollback, either:
--   (a) UPDATE personal.conversations SET project_slug='__unresolved__'
--         WHERE project_slug='__excluded__';
--   (b) accept that VALIDATE CONSTRAINT will fail loud and the rollback aborts.

SET lock_timeout = '10s';

-- Rebuild index without __excluded__ in NOT IN list.
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_project_slug_started;

DO $$
BEGIN
    IF to_regclass('personal.idx_conv_project_slug_started') IS NOT NULL THEN
        RAISE EXCEPTION 'DROP INDEX CONCURRENTLY did not complete (likely lock_timeout). Retry then re-run.';
    END IF;
END
$$;

SET lock_timeout = '60s';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_project_slug_started
    ON personal.conversations (project_slug, started_at DESC)
    WHERE project_slug IS NOT NULL
      AND project_slug NOT IN ('__no_project__', '__unresolved__')
      AND started_at IS NOT NULL;

-- Shrink CHECK constraint.
SET lock_timeout = '5s';

ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conv_project_slug_valid;

ALTER TABLE personal.conversations
    ADD CONSTRAINT conv_project_slug_valid
    CHECK (
        project_slug IS NULL OR
        project_slug ~ '^(__(no_project|unresolved)__|[A-Za-z0-9][A-Za-z0-9_-]{0,62})$'
    ) NOT VALID;

ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conv_project_slug_valid;

-- Drop governance tables.
DROP TABLE IF EXISTS personal.feature_flags;
DROP TABLE IF EXISTS personal.conversation_inject_excluded_paths;

RESET lock_timeout;
