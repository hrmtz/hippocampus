-- DOWN: psql -v ON_ERROR_STOP=1 -f migrations/031_org_multiuser_down.sql
-- Reverses 031_org_multiuser.sql in dependency-safe order.
--
-- Run later multi-user down migrations first. This file names every org object
-- explicitly; it never uses DROP SCHEMA org CASCADE.

-- (1) Trigger dependents before the trigger function.
-- A non-superuser migration owner needs temporary membership in the function
-- owner role to drop the SECURITY DEFINER functions. The role is memberless
-- after 031; revoke the temporary membership before dropping the role below.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'hippocampus_definer'
    ) THEN
        GRANT hippocampus_definer TO SESSION_USER;
    END IF;
END $$;

DROP TRIGGER IF EXISTS conversations_write_identity
    ON personal.conversations;
DROP TRIGGER IF EXISTS messages_write_identity
    ON personal.messages;

DROP FUNCTION IF EXISTS personal.enforce_multiuser_write_identity();
DROP FUNCTION IF EXISTS personal.count_owned_dense_null_messages(TEXT[]);
DROP FUNCTION IF EXISTS personal.is_maintenance_frozen();
DROP FUNCTION IF EXISTS personal.multiuser_source_identity_hash(
    TEXT, TEXT, TEXT, TEXT
);

-- (2) Tables created by 031.
DROP TABLE IF EXISTS personal.memory_share_audit;
DROP TABLE IF EXISTS public.hippocampus_migration_stages;

-- (3) Additive columns and their constraints/defaults.
ALTER TABLE personal.messages
    DROP COLUMN IF EXISTS owner_user_id,
    DROP COLUMN IF EXISTS tenant_id;

ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conversations_team_visibility_check,
    DROP CONSTRAINT IF EXISTS conversations_visibility_check,
    DROP COLUMN IF EXISTS updated_at,
    DROP COLUMN IF EXISTS shared_at,
    DROP COLUMN IF EXISTS shared_by_user_id,
    DROP COLUMN IF EXISTS team_id,
    DROP COLUMN IF EXISTS visibility,
    DROP COLUMN IF EXISTS source_identity_hash,
    DROP COLUMN IF EXISTS source_adapter,
    DROP COLUMN IF EXISTS source_platform,
    DROP COLUMN IF EXISTS source_conv_id,
    DROP COLUMN IF EXISTS owner_user_id,
    DROP COLUMN IF EXISTS tenant_id;

-- (4) org objects in foreign-key dependency order, then the empty schema.
DROP TABLE IF EXISTS org.team_memberships;
DROP TABLE IF EXISTS org.teams;
DROP TABLE IF EXISTS org.users;
DROP TABLE IF EXISTS org.tenants;
DROP SCHEMA IF EXISTS org;

-- (5) Cluster-global feature roles. All 031-owned functions and grants above
--     are gone first. If a later migration still depends on either role,
--     DROP ROLE fails closed instead of removing unrelated objects broadly.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'hippocampus_definer'
    ) THEN
        REVOKE SELECT ON personal.feature_flags,
                         personal.conversations,
                         personal.messages
        FROM hippocampus_definer;
        REVOKE USAGE ON SCHEMA personal FROM hippocampus_definer;
        REVOKE hippocampus_definer FROM SESSION_USER;
        DROP ROLE hippocampus_definer;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'hippocampus_write_service'
    ) THEN
        DROP ROLE hippocampus_write_service;
    END IF;
END $$;
