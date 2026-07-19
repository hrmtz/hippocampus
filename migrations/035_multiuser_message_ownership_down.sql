-- Rollback for 035_multiuser_message_ownership.sql
-- Restores the 031 definition of enforce_multiuser_write_identity WITHOUT the
-- issue-#85 parent-conversation ownership guard. Use ONLY for a clean revert;
-- reverting re-opens the cross-conversation message poisoning hole, so prefer
-- re-applying 035 over leaving this state in place.

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
    ELSIF TG_TABLE_SCHEMA <> 'personal'
          OR TG_TABLE_NAME <> 'messages' THEN
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
