-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/019_dub_action_embed_failure_classes_down.sql
--
-- gh #29 rollback: removes 'embed_failed_transport' / 'embed_failed_norm'
-- from agent.dub_action ENUM.
--
-- ⚠️ PG has no DROP VALUE for ENUM types. The standard pattern is:
--   1. Verify no row uses either new value (= loud-fail if any do)
--   2. Create a new ENUM without the values
--   3. ALTER COLUMN to the new type
--   4. DROP old type
-- This file does (1) only; (2)-(4) require coordinated dump/restore and are
-- intentionally manual (= avoid silent type swap on a live audit log).
--
-- Operator must rewrite affected rows BEFORE running this:
--   UPDATE agent.ghost_dub_log SET action='embed_failed'
--     WHERE action IN ('embed_failed_transport', 'embed_failed_norm');

DO $$
DECLARE
    v_count INT;
BEGIN
    SELECT count(*) INTO v_count
    FROM agent.ghost_dub_log
    WHERE action IN ('embed_failed_transport', 'embed_failed_norm');
    IF v_count > 0 THEN
        RAISE EXCEPTION 'agent.ghost_dub_log has % rows with embed_failed_{transport,norm}; rewrite before rollback', v_count;
    END IF;
END
$$;

-- Manual steps to actually drop the ENUM values (NOT executed here):
--   CREATE TYPE agent.dub_action_v2 AS ENUM (
--     'inserted', 'updated', 'unchanged',
--     'skipped_no_scope', 'skipped_not_memory', 'skipped_restricted',
--     'skipped_active_write', 'skipped_mtime_changed', 'skipped_budget_exceeded',
--     'skipped_unknown_chassis', 'skipped_purged',
--     'parse_error', 'embed_failed', 'rejected_content_scan'
--   );
--   ALTER TABLE agent.ghost_dub_log ALTER COLUMN action TYPE agent.dub_action_v2 USING action::text::agent.dub_action_v2;
--   DROP TYPE agent.dub_action;
--   ALTER TYPE agent.dub_action_v2 RENAME TO dub_action;
