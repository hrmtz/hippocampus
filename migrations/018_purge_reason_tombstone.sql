-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/018_purge_reason_tombstone.sql
-- Requires: migrations 010, 016, 017 applied.
--
-- gh #23: purge_project() missing reason param + ghost_purge_tombstone insert.
--
-- design GHOST_LAYER_DESIGN.md §16 specifies p_reason TEXT + audit storage +
-- redaction-safe scan + agent.ghost_purge_tombstone INSERT. migration 016 shipped
-- only purge_project(p_slug TEXT) and missed:
--   1. p_reason argument
--   2. personal.purge_log.reason column
--   3. tombstone INSERT  (= without it, dub_agent_memories.py's is_in_tombstone()
--      never sees 'skipped_purged' → may re-dub a purged slug = right-to-delete leak)
--
-- This migration adds all three. The function signature changes from 1-arg to
-- 2-arg; the old overload is DROPped first to avoid call ambiguity.

BEGIN;

-- (1) purge_log gains reason column (= why this right-to-delete was executed).
ALTER TABLE personal.purge_log
    ADD COLUMN IF NOT EXISTS reason TEXT;

-- (2) Drop the old single-arg signature. CREATE OR REPLACE cannot change the
--     argument list, and keeping both would make purge_project('slug') ambiguous.
DROP FUNCTION IF EXISTS personal.purge_project(TEXT);

-- (3) Recreate with p_reason + tombstone capture.
CREATE OR REPLACE FUNCTION personal.purge_project(p_slug TEXT, p_reason TEXT)
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
    v_reason        TEXT;
BEGIN
    -- Sanity: never purge sentinel slugs (would nuke unrelated rows).
    IF p_slug IS NULL OR p_slug LIKE '\_\_%' ESCAPE '\' THEN
        RAISE EXCEPTION 'refusing to purge sentinel or NULL slug: %', p_slug;
    END IF;

    -- §16: right-to-delete must be auditable; reason is mandatory.
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE EXCEPTION 'purge reason required (right-to-delete audit) for slug: %', p_slug;
    END IF;

    -- §16.0: tombstone/audit reason must be credential-sanitized. Reuse the
    -- bug_018 redaction charset/threshold (migration 017) for consistency so a
    -- reason that accidentally embeds a token does not become a forensic leak.
    v_reason := regexp_replace(p_reason, '[A-Za-z0-9._+/=@$-]{20,}', '[REDACTED]', 'g');

    -- Tombstone BEFORE the ghost delete: capture each purged (source_project,
    -- memory_slug) so dub_agent_memories.is_in_tombstone() blocks re-dub.
    -- DISTINCT guards against multi-chassis rows colliding on the PK within one
    -- INSERT; DO NOTHING preserves the append-only forensic record (migration 010).
    INSERT INTO agent.ghost_purge_tombstone (source_project, memory_slug, reason, purged_by)
    SELECT DISTINCT source_project, memory_slug, v_reason, current_user
    FROM agent.ghost_memories
    WHERE source_project = p_slug
    ON CONFLICT (source_project, memory_slug) DO NOTHING;

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

    INSERT INTO personal.purge_log (project_slug, executor, row_counts, reason)
    VALUES (p_slug, current_user, v_counts, v_reason);

    RETURN v_counts;
END
$$;

-- Restrict purge function execution (= mirror migration 016 grants for new sig).
REVOKE EXECUTE ON FUNCTION personal.purge_project(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION personal.purge_project(TEXT, TEXT) TO agent_purge_admin;

COMMIT;
