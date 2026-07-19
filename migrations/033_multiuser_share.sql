-- 033_multiuser_share.sql
-- Slice 4: owner-driven share / unshare of conversations (design §13.2).
-- Depends on 031 (org schema, personal.memory_share_audit, write-identity
-- trigger, source-identity columns).
--
-- Both functions are SECURITY DEFINER and derive the actor by mapping
-- session_user through org.users.db_role — exactly like the write-identity
-- trigger — so identity is never caller-supplied. The owner check, the
-- visibility mutation, the shared_by/shared_at stamp and the audit insert all
-- happen in one transaction. These functions are the sole writers of
-- shared_by_user_id / shared_at.
--
-- Rollback: psql -v ON_ERROR_STOP=1 -f migrations/033_multiuser_share_down.sql

-- ---------------------------------------------------------------------------
-- share_conversation: private -> team|org
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION personal.share_conversation(
    p_conv_id    TEXT,
    p_visibility TEXT,
    p_team_id    TEXT DEFAULT NULL,
    p_reason     TEXT DEFAULT NULL
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, personal, pg_temp
AS $$
DECLARE
    v_tenant_id   TEXT;
    v_user_id     TEXT;
    v_disabled_at TIMESTAMPTZ;
    v_old_vis     TEXT;
    v_old_team    TEXT;
    v_new_team    TEXT;
BEGIN
    IF p_visibility NOT IN ('team', 'org') THEN
        RAISE EXCEPTION 'visibility must be ''team'' or ''org'', got %', p_visibility
            USING ERRCODE = '22023';
    END IF;
    IF p_visibility = 'team' AND (p_team_id IS NULL OR p_team_id = '') THEN
        RAISE EXCEPTION 'team visibility requires a team_id'
            USING ERRCODE = '22023';
    END IF;
    -- org visibility is tenant-wide: team_id is cleared.
    v_new_team := CASE WHEN p_visibility = 'team' THEN p_team_id ELSE NULL END;

    SELECT u.tenant_id, u.user_id, u.disabled_at
      INTO v_tenant_id, v_user_id, v_disabled_at
      FROM org.users u
     WHERE u.db_role = session_user::TEXT;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session role % has no org.users mapping', session_user
            USING ERRCODE = '42501';
    END IF;
    IF v_disabled_at IS NOT NULL THEN
        RAISE EXCEPTION 'session role % is disabled', session_user
            USING ERRCODE = '42501';
    END IF;

    -- team shares require the actor to actually be in that team.
    IF p_visibility = 'team' AND NOT EXISTS (
        SELECT 1 FROM org.team_memberships tm
         WHERE tm.tenant_id = v_tenant_id
           AND tm.user_id   = v_user_id
           AND tm.team_id   = p_team_id
    ) THEN
        RAISE EXCEPTION 'actor % is not a member of team %', v_user_id, p_team_id
            USING ERRCODE = '42501';
    END IF;

    -- owner check + lock. Only the owner may share.
    SELECT c.visibility, c.team_id
      INTO v_old_vis, v_old_team
      FROM personal.conversations c
     WHERE c.conv_id = p_conv_id
       AND c.tenant_id = v_tenant_id
       AND c.owner_user_id = v_user_id
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'conversation % not found or not owned by %',
            p_conv_id, v_user_id
            USING ERRCODE = '42501';
    END IF;

    UPDATE personal.conversations
       SET visibility        = p_visibility,
           team_id           = v_new_team,
           shared_by_user_id = v_user_id,
           shared_at         = pg_catalog.now(),
           updated_at        = pg_catalog.now()
     WHERE conv_id = p_conv_id;

    INSERT INTO personal.memory_share_audit
        (tenant_id, conv_id, actor_user_id, old_visibility, new_visibility,
         old_team_id, new_team_id, reason)
    VALUES (v_tenant_id, p_conv_id, v_user_id, v_old_vis, p_visibility,
            v_old_team, v_new_team, p_reason);

    RETURN p_conv_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- unshare_conversation: -> private, clears team_id + shared_* fields
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION personal.unshare_conversation(
    p_conv_id TEXT,
    p_reason  TEXT DEFAULT NULL
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, personal, pg_temp
AS $$
DECLARE
    v_tenant_id   TEXT;
    v_user_id     TEXT;
    v_disabled_at TIMESTAMPTZ;
    v_old_vis     TEXT;
    v_old_team    TEXT;
BEGIN
    SELECT u.tenant_id, u.user_id, u.disabled_at
      INTO v_tenant_id, v_user_id, v_disabled_at
      FROM org.users u
     WHERE u.db_role = session_user::TEXT;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session role % has no org.users mapping', session_user
            USING ERRCODE = '42501';
    END IF;
    IF v_disabled_at IS NOT NULL THEN
        RAISE EXCEPTION 'session role % is disabled', session_user
            USING ERRCODE = '42501';
    END IF;

    SELECT c.visibility, c.team_id
      INTO v_old_vis, v_old_team
      FROM personal.conversations c
     WHERE c.conv_id = p_conv_id
       AND c.tenant_id = v_tenant_id
       AND c.owner_user_id = v_user_id
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'conversation % not found or not owned by %',
            p_conv_id, v_user_id
            USING ERRCODE = '42501';
    END IF;

    UPDATE personal.conversations
       SET visibility        = 'private',
           team_id           = NULL,
           shared_by_user_id = NULL,
           shared_at         = NULL,
           updated_at        = pg_catalog.now()
     WHERE conv_id = p_conv_id;

    INSERT INTO personal.memory_share_audit
        (tenant_id, conv_id, actor_user_id, old_visibility, new_visibility,
         old_team_id, new_team_id, reason)
    VALUES (v_tenant_id, p_conv_id, v_user_id, v_old_vis, 'private',
            v_old_team, NULL, p_reason);

    RETURN p_conv_id;
END;
$$;

ALTER FUNCTION personal.share_conversation(TEXT, TEXT, TEXT, TEXT)
    OWNER TO hippocampus_definer;
ALTER FUNCTION personal.unshare_conversation(TEXT, TEXT)
    OWNER TO hippocampus_definer;
REVOKE ALL ON FUNCTION personal.share_conversation(TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION personal.unshare_conversation(TEXT, TEXT) FROM PUBLIC;

-- Privileges the definer owner needs to perform the mutation + audit. SELECT on
-- org.team_memberships is for the team-membership check.
GRANT UPDATE ON personal.conversations TO hippocampus_definer;
GRANT INSERT ON personal.memory_share_audit TO hippocampus_definer;
GRANT USAGE ON SEQUENCE personal.memory_share_audit_id_seq TO hippocampus_definer;
GRANT SELECT ON org.team_memberships TO hippocampus_definer;

-- EXECUTE is granted to each per-user employee login role at deploy time
-- (see the per-user roles recipe), never to PUBLIC.
