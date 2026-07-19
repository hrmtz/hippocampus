-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/028_chassis_id_grok_down.sql
--
-- Grok chassis rollback PREFLIGHT: this file does NOT remove the enum value
-- (Postgres cannot DROP an enum value in place). It is a guard that FAILS LOUD
-- if any chassis_id-typed column still holds 'grok', so a subsequent manual
-- ENUM rebuild (drop+recreate type, retype every column) is safe.
--
-- ALL agent.chassis_id-typed STORED columns are checked (table, column) —
-- note the column name is NOT uniform:
--   agent.ghost_memories.chassis_id        (migration 009)
--   agent.ghost_dub_log.chassis_id         (migration 009)
--   agent.ghost_dub_run.chassis_id         (migration 009)
--   agent.ghost_read_log.chassis_id        (migration 009)
--   agent.memory_edges.source_chassis_id   (migration 025)  ← distinct col name
-- (020/021 are functions/views, not stored data — nothing to rewrite there.)
--
-- Operator must rewrite any 'grok' rows BEFORE running this, e.g.:
--   UPDATE agent.ghost_read_log SET chassis_id='unknown' WHERE chassis_id='grok';
--   UPDATE agent.memory_edges  SET source_chassis_id='unknown' WHERE source_chassis_id='grok';

DO $$
DECLARE
    v_pair  TEXT[];
    v_count INT;
    -- (fully-qualified table, chassis_id column name)
    v_targets TEXT[][] := ARRAY[
        ARRAY['agent.ghost_memories', 'chassis_id'],
        ARRAY['agent.ghost_dub_log',  'chassis_id'],
        ARRAY['agent.ghost_dub_run',  'chassis_id'],
        ARRAY['agent.ghost_read_log', 'chassis_id'],
        ARRAY['agent.memory_edges',   'source_chassis_id']
    ];
BEGIN
    FOREACH v_pair SLICE 1 IN ARRAY v_targets LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I = %L',
            v_pair[1], v_pair[2], 'grok'
        ) INTO v_count;
        IF v_count > 0 THEN
            RAISE EXCEPTION
                '%.% has % rows with value grok; rewrite before rollback',
                v_pair[1], v_pair[2], v_count;
        END IF;
    END LOOP;
END
$$;
