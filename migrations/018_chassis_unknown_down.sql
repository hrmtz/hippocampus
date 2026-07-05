-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/018_chassis_unknown_down.sql
--
-- Phase 7 rollback: removes 'unknown' from agent.chassis_id ENUM.
--
-- ⚠️ PG has no DROP VALUE for ENUM types. The standard pattern is:
--   1. Verify no row uses 'unknown' (= loud-fail if any do)
--   2. Create a new ENUM without 'unknown'
--   3. ALTER COLUMN to the new type
--   4. DROP old type
-- This file does (1) only; (2)-(4) require coordinated dump/restore and are
-- intentionally manual (= avoid silent type swap on a live DB).
--
-- Operator must rewrite any 'unknown' rows BEFORE running this:
--   UPDATE agent.ghost_read_log SET chassis_id='claude-code'
--     WHERE chassis_id='unknown';   -- or whatever attribution is appropriate

DO $$
DECLARE
    v_count INT;
BEGIN
    SELECT count(*) INTO v_count
    FROM agent.ghost_read_log
    WHERE chassis_id = 'unknown';
    IF v_count > 0 THEN
        RAISE EXCEPTION 'agent.ghost_read_log has % rows with chassis_id=unknown; rewrite before rollback', v_count;
    END IF;
END
$$;

-- Manual steps to actually drop the ENUM value (NOT executed here):
--   CREATE TYPE agent.chassis_id_v2 AS ENUM ('claude-code', 'codex');
--   ALTER TABLE agent.ghost_memories ALTER COLUMN chassis_id TYPE agent.chassis_id_v2 USING chassis_id::text::agent.chassis_id_v2;
--   ALTER TABLE agent.ghost_read_log ALTER COLUMN chassis_id TYPE agent.chassis_id_v2 USING chassis_id::text::agent.chassis_id_v2;
--   DROP TYPE agent.chassis_id;
--   ALTER TYPE agent.chassis_id_v2 RENAME TO chassis_id;
