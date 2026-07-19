-- 031_org_multiuser.sql
-- Company multi-user schema foundation (Slice 1, Milestone 3).
--
-- Adds the org identity directory, additive tenant/owner/source/share columns,
-- append-only share audit storage, checkpoint storage, and the write-side
-- identity anchor. This file performs no backfill and creates no secondary
-- indexes. The runner applies it atomically with psql -1 (no_tx=false).
--
-- Every SECURITY DEFINER function created here is owned by the single
-- hippocampus_definer NOLOGIN role, pins search_path to
-- pg_catalog, personal, pg_temp, and uses session_user (never current_user)
-- wherever caller identity or bypass membership is resolved.
--
-- Requires: 001 personal schema, 014 feature_flags, 030 diary provenance.
-- Rollback: psql -v ON_ERROR_STOP=1 -f migrations/031_org_multiuser_down.sql

-- ===========================================================================
-- (1) Feature roles. CREATE ROLE has no IF NOT EXISTS form, so use the repo's
--     guarded DO-block idiom. Existing roles must retain the least-privilege
--     NOLOGIN shape; fail closed on a conflicting cluster-global role.
-- ===========================================================================
DO $$
DECLARE
    v_role RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'hippocampus_definer'
    ) THEN
        CREATE ROLE hippocampus_definer NOLOGIN;
        -- PostgreSQL 16 grants a non-superuser role creator ADMIN OPTION on a
        -- role it creates. Remove that implicit membership; a tightly scoped
        -- temporary membership is granted below only for ownership transfer.
        REVOKE hippocampus_definer FROM SESSION_USER;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'hippocampus_write_service'
    ) THEN
        CREATE ROLE hippocampus_write_service NOLOGIN;
        -- Bypass membership is an explicit gated-runner/operator action, not
        -- an accidental side effect of which PostgreSQL role creates 031.
        REVOKE hippocampus_write_service FROM SESSION_USER;
    END IF;

    FOR v_role IN
        SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname IN ('hippocampus_definer', 'hippocampus_write_service')
    LOOP
        IF v_role.rolcanlogin
           OR v_role.rolsuper
           OR v_role.rolcreatedb
           OR v_role.rolcreaterole
           OR v_role.rolreplication
           OR v_role.rolbypassrls THEN
            RAISE EXCEPTION
                'role % exists with unsafe attributes; expected an unprivileged NOLOGIN role',
                v_role.rolname;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members am
        JOIN pg_catalog.pg_roles r ON r.oid = am.roleid
        WHERE r.rolname = 'hippocampus_definer'
    ) THEN
        RAISE EXCEPTION
            'hippocampus_definer must not have members';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members am
        JOIN pg_catalog.pg_roles r ON r.oid = am.roleid
        WHERE r.rolname = 'hippocampus_write_service'
    ) THEN
        RAISE EXCEPTION
            'hippocampus_write_service must not have members';
    END IF;
END $$;

-- ALTER FUNCTION ... OWNER TO requires membership in the target role for a
-- non-superuser migration owner. The file-level transaction guarantees this
-- temporary membership rolls back on failure; it is revoked at the file tail.
GRANT hippocampus_definer TO SESSION_USER;

-- CURRENT_USER is the migration-applying role and the single-user
-- owner/operator DSN, so write-identity triggers bypass immediately on the
-- single-user path. Employee per-user login roles are deliberately not
-- granted this membership; they must map through org.users.
GRANT hippocampus_write_service TO CURRENT_USER;

-- ===========================================================================
-- (2) Organization identity metadata.
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS org;

CREATE TABLE IF NOT EXISTS org.tenants (
    tenant_id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS org.users (
    tenant_id TEXT NOT NULL REFERENCES org.tenants(tenant_id),
    user_id TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    db_role TEXT UNIQUE,
    disabled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS org.teams (
    tenant_id TEXT NOT NULL REFERENCES org.tenants(tenant_id),
    team_id TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, team_id)
);

CREATE TABLE IF NOT EXISTS org.team_memberships (
    tenant_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, team_id, user_id),
    FOREIGN KEY (tenant_id, team_id) REFERENCES org.teams(tenant_id, team_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES org.users(tenant_id, user_id)
);

-- ===========================================================================
-- (3) Additive conversation/message identity columns. updated_at is added
--     without a volatile default, then receives its default separately.
-- ===========================================================================
ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS owner_user_id TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS source_conv_id TEXT,
    ADD COLUMN IF NOT EXISTS source_platform TEXT,
    ADD COLUMN IF NOT EXISTS source_adapter TEXT,
    ADD COLUMN IF NOT EXISTS source_identity_hash TEXT,
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private',
    ADD COLUMN IF NOT EXISTS team_id TEXT,
    ADD COLUMN IF NOT EXISTS shared_by_user_id TEXT,
    ADD COLUMN IF NOT EXISTS shared_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

ALTER TABLE personal.conversations
    ALTER COLUMN updated_at SET DEFAULT now();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'conversations_visibility_check'
          AND conrelid = 'personal.conversations'::pg_catalog.regclass
    ) THEN
        ALTER TABLE personal.conversations
            ADD CONSTRAINT conversations_visibility_check
            CHECK (visibility IN ('private', 'team', 'org')) NOT VALID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'conversations_team_visibility_check'
          AND conrelid = 'personal.conversations'::pg_catalog.regclass
    ) THEN
        ALTER TABLE personal.conversations
            ADD CONSTRAINT conversations_team_visibility_check
            CHECK (
                (visibility = 'team' AND team_id IS NOT NULL)
                OR (visibility IN ('private', 'org') AND team_id IS NULL)
            ) NOT VALID;
    END IF;
END $$;

ALTER TABLE personal.messages
    ADD COLUMN IF NOT EXISTS tenant_id TEXT,
    ADD COLUMN IF NOT EXISTS owner_user_id TEXT;

-- ===========================================================================
-- (4) Append-only share audit storage and database-backed stage checkpoints.
--     Append-only is enforced by the later least-privilege grants: employee
--     roles receive no direct INSERT/UPDATE/DELETE privilege on the audit.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS personal.memory_share_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    old_visibility TEXT,
    new_visibility TEXT NOT NULL,
    old_team_id TEXT,
    new_team_id TEXT,
    reason TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.hippocampus_migration_stages (
    stage TEXT PRIMARY KEY,
    completed_at TIMESTAMPTZ,
    detail JSONB
);

-- ===========================================================================
-- (5) Shared SQL/Python source-identity digest contract.
-- ===========================================================================
CREATE OR REPLACE FUNCTION personal.multiuser_source_identity_hash(
    p_tenant_id TEXT,
    p_owner_user_id TEXT,
    p_source_platform TEXT,
    p_source_conv_id TEXT
)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(p_tenant_id, 'UTF8')
            || '\x00'::bytea
            || pg_catalog.convert_to(p_owner_user_id, 'UTF8')
            || '\x00'::bytea
            || pg_catalog.convert_to(p_source_platform, 'UTF8')
            || '\x00'::bytea
            || pg_catalog.convert_to(p_source_conv_id, 'UTF8')
        ),
        'hex'
    );
$$;

-- ===========================================================================
-- (6) SECURITY DEFINER helpers. PUBLIC receives no direct EXECUTE; later
--     grants enumerate only the employee roles that need each helper.
-- ===========================================================================
GRANT USAGE ON SCHEMA personal, org TO hippocampus_definer;
GRANT SELECT ON personal.feature_flags,
                personal.conversations,
                personal.messages
TO hippocampus_definer;
GRANT SELECT ON org.users TO hippocampus_definer;

CREATE OR REPLACE FUNCTION personal.is_maintenance_frozen()
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, personal, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM personal.feature_flags
        WHERE flag_name = 'maintenance_freeze'
          AND enabled
    );
$$;

ALTER FUNCTION personal.is_maintenance_frozen()
    OWNER TO hippocampus_definer;
REVOKE ALL ON FUNCTION personal.is_maintenance_frozen() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION personal.is_maintenance_frozen() TO CURRENT_USER;

CREATE OR REPLACE FUNCTION personal.count_owned_dense_null_messages(
    p_conv_ids TEXT[]
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, personal, pg_temp
AS $$
DECLARE
    v_tenant_id    TEXT;
    v_user_id      TEXT;
    v_disabled_at  TIMESTAMPTZ;
    v_count        BIGINT;
BEGIN
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

    SELECT pg_catalog.count(*)
    INTO v_count
    FROM personal.messages m
    JOIN personal.conversations c ON c.conv_id = m.conv_id
    WHERE m.conv_id = ANY(p_conv_ids)
      AND m.dense IS NULL
      AND c.tenant_id = v_tenant_id
      AND c.owner_user_id = v_user_id;

    RETURN v_count;
END;
$$;

ALTER FUNCTION personal.count_owned_dense_null_messages(TEXT[])
    OWNER TO hippocampus_definer;
REVOKE ALL ON FUNCTION personal.count_owned_dense_null_messages(TEXT[])
    FROM PUBLIC;

-- ===========================================================================
-- (7) Write-side identity anchor. Bypass is based only on session_user group
--     membership. Non-bypass writers must map to one enabled org.users row,
--     may stamp only that tenant/user, and may INSERT only private rows.
-- ===========================================================================
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

DROP TRIGGER IF EXISTS conversations_write_identity
    ON personal.conversations;
CREATE TRIGGER conversations_write_identity
BEFORE INSERT OR UPDATE ON personal.conversations
FOR EACH ROW
EXECUTE FUNCTION personal.enforce_multiuser_write_identity();

DROP TRIGGER IF EXISTS messages_write_identity
    ON personal.messages;
CREATE TRIGGER messages_write_identity
BEFORE INSERT OR UPDATE ON personal.messages
FOR EACH ROW
EXECUTE FUNCTION personal.enforce_multiuser_write_identity();

-- Leave the dedicated definer role with no members. Employee roles receive
-- EXECUTE on selected functions later, never membership in their owner role.
REVOKE hippocampus_definer FROM SESSION_USER;
