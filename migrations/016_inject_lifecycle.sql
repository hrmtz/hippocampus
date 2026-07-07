-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/016_inject_lifecycle.sql
-- Requires: migrations 013-015 applied. agent_purge_admin role (= migration 009).
--
-- Phase 5/6: inject lifecycle — opt-in allowlist + purge function + slug history.
--
-- Components:
--   1. personal.conversation_inject_allowlist — project_slug PK, opt-in gate
--      for Phase 6 hook (= even if feature_flag enabled, only allowlisted
--      projects' conversations get injected).
--   2. personal.conversations.slug_history JSONB — append-only audit of slug
--      changes (= rename support per Phase 1 round 1 security-7). Phase 5
--      provides the column only; population trigger lands in Phase 5+ or 6.
--   3. personal.purge_log — audit table for right-to-delete operations.
--   4. personal.purge_project(p_slug) — SECURITY DEFINER function that
--      cascades DELETE across personal.conversations / messages / read_log
--      + agent.ghost_memories. Executable only by agent_purge_admin.
--
-- Scope OUT: Phase 6 (#19) hook itself.
--
-- Rollback: psql -f migrations/016_inject_lifecycle_down.sql
-- Dependencies: 013, 014, 015, ghost layer 009 (= agent_purge_admin)

SET lock_timeout = '5s';

-- (1) Opt-in allowlist. Default empty (= no projects injectable).
-- Phase 6 hook MUST filter WHERE project_slug IN (SELECT project_slug FROM allowlist).
CREATE TABLE IF NOT EXISTS personal.conversation_inject_allowlist (
    project_slug TEXT PRIMARY KEY
        CHECK (project_slug ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$'),  -- no sentinels
    scope        TEXT NOT NULL DEFAULT 'brief'
        CHECK (scope IN ('brief', 'title_only')),
    enabled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note         TEXT
);

-- (2) slug_history column for rename tracking.
-- Default '[]'. Each entry: {ts: ..., from: "<old>", to: "<new>"}.
-- Phase 5+ trigger may populate; Phase 5 only reserves the column.
ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS slug_history JSONB NOT NULL DEFAULT '[]'::jsonb;

-- (3) purge audit log. Not partitioned (= low volume).
CREATE TABLE IF NOT EXISTS personal.purge_log (
    id           BIGSERIAL PRIMARY KEY,
    project_slug TEXT NOT NULL,
    executed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executor     TEXT NOT NULL,
    row_counts   JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purge_log_slug ON personal.purge_log (project_slug, executed_at DESC);

-- (4) purge_project() — right-to-delete cascade.
-- SECURITY DEFINER + ownership guards (= function runs with creator's privileges
-- which has the cross-schema DELETE rights). EXECUTE granted only to
-- agent_purge_admin.
CREATE OR REPLACE FUNCTION personal.purge_project(p_slug TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = personal, agent, public
AS $$
DECLARE
    v_conv_count    INT;
    v_msg_count     INT;
    v_seg_count     INT;
    v_read_count    INT;
    v_ghost_count   INT;
    v_counts        JSONB;
BEGIN
    -- Sanity: never purge sentinel slugs (would nuke unrelated rows).
    IF p_slug IS NULL OR p_slug LIKE '\_\_%' ESCAPE '\' THEN
        RAISE EXCEPTION 'refusing to purge sentinel or NULL slug: %', p_slug;
    END IF;

    -- ghost layer cascade (= agent.ghost_memories.source_project).
    DELETE FROM agent.ghost_memories WHERE source_project = p_slug;
    GET DIAGNOSTICS v_ghost_count = ROW_COUNT;

    -- personal.conversation_read_log (= audit; preserve as separate purge target).
    DELETE FROM personal.conversation_read_log WHERE current_project = p_slug;
    GET DIAGNOSTICS v_read_count = ROW_COUNT;

    -- personal.conversation_segments (= ON DELETE CASCADE via conv_id FK from migration 008).
    -- Counted indirectly via parent delete.
    SELECT count(*) INTO v_seg_count
    FROM personal.conversation_segments seg
    JOIN personal.conversations c ON c.conv_id = seg.conv_id
    WHERE c.project_slug = p_slug;

    -- personal.messages (= no FK ON DELETE CASCADE in baseline; manual delete).
    DELETE FROM personal.messages
    WHERE conv_id IN (
        SELECT conv_id FROM personal.conversations WHERE project_slug = p_slug
    );
    GET DIAGNOSTICS v_msg_count = ROW_COUNT;

    -- personal.conversations (parent).
    DELETE FROM personal.conversations WHERE project_slug = p_slug;
    GET DIAGNOSTICS v_conv_count = ROW_COUNT;

    v_counts := jsonb_build_object(
        'conversations', v_conv_count,
        'messages',      v_msg_count,
        'segments',      v_seg_count,
        'read_log',      v_read_count,
        'ghost_memories',v_ghost_count
    );

    INSERT INTO personal.purge_log (project_slug, executor, row_counts)
    VALUES (p_slug, current_user, v_counts);

    RETURN v_counts;
END
$$;

-- Restrict purge function execution.
REVOKE EXECUTE ON FUNCTION personal.purge_project(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION personal.purge_project(TEXT) TO agent_purge_admin;

-- agent_purge_admin needs basic SELECT to compute v_seg_count + UPDATE for cascade.
GRANT USAGE ON SCHEMA personal TO agent_purge_admin;
GRANT SELECT, DELETE ON personal.conversations TO agent_purge_admin;
GRANT SELECT, DELETE ON personal.messages TO agent_purge_admin;
GRANT SELECT ON personal.conversation_segments TO agent_purge_admin;
GRANT SELECT, DELETE ON personal.conversation_read_log TO agent_purge_admin;
GRANT SELECT, INSERT ON personal.purge_log TO agent_purge_admin;
GRANT USAGE, SELECT ON SEQUENCE personal.purge_log_id_seq TO agent_purge_admin;
-- agent schema cascade (= ghost layer 009 may already grant; safe to repeat).
GRANT USAGE ON SCHEMA agent TO agent_purge_admin;
GRANT SELECT, DELETE ON agent.ghost_memories TO agent_purge_admin;

RESET lock_timeout;
