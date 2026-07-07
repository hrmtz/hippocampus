-- ⚠️ APPLY: psql -f migrations/013_conversation_project_down.sql
--    Do NOT use `-1` / `--single-transaction` flag.
-- Phase 1 rollback: undoes 013_conversation_project.sql.
--
-- ⚠️ WARNING: this down migration is for Phase 1 SOLO rollback only.
-- If Phase 2+ migrations (014/015/016) have been applied and reference
-- project_slug / canonical_project_slug(), this script will FAIL with
--   ERROR: cannot drop column because other objects depend on it
-- This is INTENTIONAL — Phase順序違反を loud-fail で検出する rail.
-- Rollback Phase 2+ first via their respective down migrations in reverse order.
--
-- ⚠️ DATA LOSS: DROP COLUMN destroys all project_slug values irreversibly.
-- If Phase 2+ has populated project_slug and you want to preserve it for
-- forensics / re-import, take a backup BEFORE running this script.
-- ⚠️ NOT /tmp (= world-readable on multi-user hosts; private repo names
-- like clientX-redteam leak via filesystem). Use a 0700-permission dir:
--   mkdir -p ~/.local/share/hippocampus/backups && chmod 700 ~/.local/share/hippocampus/backups
--   \copy (SELECT conv_id, project_slug FROM personal.conversations
--          WHERE project_slug IS NOT NULL)
--     TO PROGRAM 'cat > ~/.local/share/hippocampus/backups/project_slug_backup_$(date +%Y%m%d_%H%M%S).csv' CSV
--
-- ⚠️ SECURITY: pg_dump backups taken BEFORE this rollback may contain sensitive
-- slug values. To redact the column BEFORE pg_dump, uncomment + run the
-- following pre-step EXACTLY (= typo'd empty string '' or unknown sentinel
-- like '__redacted__' will violate CHECK constraint conv_project_slug_valid
-- and partial-abort):
--   -- UPDATE personal.conversations SET project_slug = NULL
--   --   WHERE project_slug IS NOT NULL
--   --     AND project_slug NOT IN ('__no_project__', '__unresolved__');
-- Then take the backup, then run the DROP statements below.

-- ⚠️ STRONGLY recommended: apply with `psql -v ON_ERROR_STOP=1 -f <this file>`
-- so a mid-file failure (= DROP INDEX timeout) aborts before continuing to
-- DROP FUNCTION / DROP COLUMN. Without ON_ERROR_STOP, psql plows on through
-- errors and leaves a partial-rollback state (= index residue + dropped column).

-- lock_timeout for safety (= 10s; longer than up.sql's 3s since DROP INDEX
-- CONCURRENTLY can wait on long ingest INSERTs).
SET lock_timeout = '10s';

-- DROP INDEX CONCURRENTLY: must be outside any transaction block.
-- ⚠️ Do NOT use `psql -1` / `--single-transaction` when applying this file.
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_project_slug_started;

-- Loud-fail if DROP INDEX timed out and left the index in place.
-- Without this gate, subsequent DROP COLUMN would proceed and create a
-- mismatched partial state (index residue + column gone).
DO $$
BEGIN
    IF to_regclass('personal.idx_conv_project_slug_started') IS NOT NULL THEN
        RAISE EXCEPTION 'DROP INDEX CONCURRENTLY did not complete (likely lock_timeout). Retry: DROP INDEX CONCURRENTLY personal.idx_conv_project_slug_started; then re-run this down file.';
    END IF;
END
$$;

-- Drop FUNCTION BEFORE COLUMN: if a Phase 2+ object depends on the function,
-- DROP FUNCTION will loud-fail here, leaving column intact (= recoverable).
-- The reverse order would destroy column data irreversibly even when the
-- function drop later fails. (codex review r3-codex-8.)
DROP FUNCTION IF EXISTS personal.canonical_project_slug(TEXT, TEXT);

ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conv_project_slug_valid;

-- No CASCADE (intentional: see warning above).
ALTER TABLE personal.conversations
    DROP COLUMN IF EXISTS project_slug;

RESET lock_timeout;
