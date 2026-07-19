-- 035_multiuser_message_ownership.sql
-- Security fix (issue #85, CRITICAL): close the cross-conversation message
-- poisoning seam in the write-identity trigger.
--
-- Depends on 031 (personal.enforce_multiuser_write_identity + the messages /
-- conversations write-identity triggers). This migration re-defines that
-- function via CREATE OR REPLACE, adding a REQUIRED parent-conversation
-- ownership guard on the messages relation. It is authored as a new ledger
-- entry (not an edit of 031) because 031 may already be applied on deployed
-- company databases; the ledger only re-runs new files.
--
-- THE SEAM (before this fix):
--   * write side: the trigger validated only that NEW.tenant_id/owner_user_id
--     matched the session_user -> org.users mapping. It never checked that the
--     writer owns NEW.conv_id.
--   * read side: get_conversation / get_conversation_summary / dense search
--     scope by CONVERSATION visibility and never re-check m.owner_user_id.
--   Composed: a mapped employee could INSERT a self-stamped message onto a
--   VICTIM's (or org-shared) conv_id — trigger passes, FK satisfied by the
--   victim's row — and the poison rendered inside the victim's conversation as
--   trusted, victim-attributed memory (disinformation + prompt injection).
--
-- THE FIX: for personal.messages writes by a non-service mapped role, require
-- that NEW.conv_id's parent conversation is owned by the SAME (tenant, user)
-- the writer is stamping. This is safe against every legitimate path because
-- messages are only ever written to the writer's own conversations (ingest
-- writes your own conversations; sharing only promotes visibility on a conv
-- you already own — it never writes messages into someone else's conv). The
-- guard fires on both INSERT and UPDATE (an UPDATE could otherwise re-point
-- NEW.conv_id at a victim's conversation).
--
-- Rollback: psql -v ON_ERROR_STOP=1 -f migrations/035_multiuser_message_ownership_down.sql
--   (restores the 031 definition WITHOUT the guard — only for a clean revert;
--   re-applying 035 re-closes the hole.)

CREATE OR REPLACE FUNCTION personal.enforce_multiuser_write_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, personal, pg_temp
AS $$
DECLARE
    v_tenant_id    TEXT;
    v_user_id      TEXT;
    v_disabled_at  TIMESTAMPTZ;
BEGIN
    IF pg_catalog.pg_has_role(
        session_user, 'hippocampus_write_service', 'MEMBER'
    ) THEN
        RETURN NEW;
    END IF;

    SELECT u.tenant_id, u.user_id, u.disabled_at
    INTO v_tenant_id, v_user_id, v_disabled_at
    FROM org.users u
    WHERE u.db_role = session_user::TEXT;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'session role % has no org.users mapping', session_user
            USING ERRCODE = '42501';
    END IF;

    IF v_disabled_at IS NOT NULL THEN
        RAISE EXCEPTION
            'session role % is disabled', session_user
            USING ERRCODE = '42501';
    END IF;

    IF NEW.tenant_id IS DISTINCT FROM v_tenant_id
       OR NEW.owner_user_id IS DISTINCT FROM v_user_id THEN
        RAISE EXCEPTION
            'session role % cannot stamp tenant_id/owner_user_id as %/%',
            session_user, NEW.tenant_id, NEW.owner_user_id
            USING ERRCODE = '42501';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.tenant_id IS DISTINCT FROM v_tenant_id
           OR OLD.owner_user_id IS DISTINCT FROM v_user_id THEN
            RAISE EXCEPTION
                'session role % cannot UPDATE row owned by tenant_id/owner_user_id %/%',
                session_user, OLD.tenant_id, OLD.owner_user_id
                USING ERRCODE = '42501';
        END IF;

        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
            RAISE EXCEPTION
                'session role % cannot change tenant_id/owner_user_id on UPDATE',
                session_user
                USING ERRCODE = '42501';
        END IF;
    END IF;

    IF TG_TABLE_SCHEMA = 'personal'
       AND TG_TABLE_NAME = 'conversations' THEN
        IF TG_OP = 'INSERT' AND NEW.visibility IS DISTINCT FROM 'private' THEN
            RAISE EXCEPTION
                'non-service session role % may only INSERT private conversations',
                session_user
                USING ERRCODE = '42501';
        END IF;
    ELSIF TG_TABLE_SCHEMA = 'personal'
          AND TG_TABLE_NAME = 'messages' THEN
        -- issue #85: a message may only be attached to a conversation the
        -- writer owns. Without this, self-stamping owner_user_id passes the
        -- checks above while NEW.conv_id points at another user's conversation,
        -- and the read path (scoped by conversation, not by m.owner_user_id)
        -- serves the poison inside the victim's thread.
        IF NOT EXISTS (
            SELECT 1
            FROM personal.conversations c
            WHERE c.conv_id = NEW.conv_id
              AND c.tenant_id = v_tenant_id
              AND c.owner_user_id = v_user_id
        ) THEN
            RAISE EXCEPTION
                'session role % cannot attach a message to conversation % it does not own',
                session_user, NEW.conv_id
                USING ERRCODE = '42501';
        END IF;
    ELSE
        RAISE EXCEPTION
            'write-identity trigger attached to unexpected relation %.%',
            TG_TABLE_SCHEMA, TG_TABLE_NAME;
    END IF;

    RETURN NEW;
END;
$$;

ALTER FUNCTION personal.enforce_multiuser_write_identity()
    OWNER TO hippocampus_definer;
REVOKE ALL ON FUNCTION personal.enforce_multiuser_write_identity() FROM PUBLIC;
