-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/019_dub_action_embed_failure_classes.sql
--    Do NOT wrap in BEGIN/COMMIT (= ALTER TYPE ADD VALUE has tx restrictions
--    in some PG versions; psql autocommit is safe).
--
-- gh #29 follow-up: split agent.dub_action 'embed_failed' into per-class
-- audit codes so the L2-normalize invariant assertion is observable in
-- ghost_dub_log.
--
-- Background: gh #29 introduced EmbeddingNotNormalizedError raised at the
-- embed boundary, with dub_agent_memories logging it as either:
--   - 'embed_failed_transport'  → BGEEmbedError (= network / 5xx / dim mismatch)
--   - 'embed_failed_norm'       → EmbeddingNotNormalizedError (= norm violation)
-- Neither token existed in the ENUM, so the audit INSERT itself raised
-- InvalidTextRepresentation, the outer handler rewrote the action to
-- 'parse_error' with a meaningless "DB error: invalid input value for enum"
-- message, and the embed-failure context was lost. Adding the values here
-- restores audit fidelity.
--
-- ultrareview bug_015 (NORMAL) — caught post-merge of #29 boundary commit.
--
-- Rollback: psql -f migrations/019_dub_action_embed_failure_classes_down.sql
-- ⚠️ Down requires ZERO rows referencing either new value (= PG cannot
-- remove an in-use ENUM value without value-rewrite migration). The down
-- file fails loud if any rows reference the values.

ALTER TYPE agent.dub_action ADD VALUE IF NOT EXISTS 'embed_failed_transport';
ALTER TYPE agent.dub_action ADD VALUE IF NOT EXISTS 'embed_failed_norm';
