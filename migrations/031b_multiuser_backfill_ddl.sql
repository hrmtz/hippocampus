-- 031b_multiuser_backfill_ddl.sql
-- Post-backfill DDL for company multi-user support (Slice 1, Milestone 4).
--
-- Apply only after the multiuser_backfill_complete checkpoint. This file is
-- intentionally no_tx=true: every statement runs in its own autocommit
-- transaction, and the seven index builds use CONCURRENTLY.
--
-- Requires: 031_org_multiuser.sql and completed batched backfills.
-- Rollback: psql -v ON_ERROR_STOP=1 -f migrations/031b_multiuser_backfill_ddl_down.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_tenant_owner_started
    ON personal.conversations (tenant_id, owner_user_id, started_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_tenant_visibility_started
    ON personal.conversations (tenant_id, visibility, started_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_tenant_team_started
    ON personal.conversations (tenant_id, team_id, started_at DESC)
    WHERE visibility = 'team';

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_source_identity_hash
    ON personal.conversations (source_identity_hash);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_tenant_source_lookup
    ON personal.conversations (tenant_id, owner_user_id, source_platform);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_tenant_owner_ts
    ON personal.messages (tenant_id, owner_user_id, ts DESC)
    WHERE tenant_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_tenant_conv
    ON personal.messages (tenant_id, conv_id)
    WHERE tenant_id IS NOT NULL;

-- Post-apply verification (loud-fail on a leftover INVALID index), mirroring the
-- 013/014 idiom the migrate.py runner's resume contract relies on. Because this
-- file is no_tx and uses `CREATE INDEX CONCURRENTLY IF NOT EXISTS`, a build that
-- fails mid-way leaves an INVALID index of the same name; a plain rerun would
-- then see it, skip the CREATE, and ledger 031b as "applied" while the index
-- (notably the UNIQUE source-identity index) is silently unenforced. This block
-- runs in the same autocommit context and RAISEs with explicit remediation.
DO $$
DECLARE
    v_name   TEXT;
    v_oid    OID;
    v_valid  BOOLEAN;
    v_unique BOOLEAN;
    v_cols   TEXT;
BEGIN
    FOREACH v_name IN ARRAY ARRAY[
        'personal.idx_conv_tenant_owner_started',
        'personal.idx_conv_tenant_visibility_started',
        'personal.idx_conv_tenant_team_started',
        'personal.idx_conv_source_identity_hash',
        'personal.idx_conv_tenant_source_lookup',
        'personal.idx_messages_tenant_owner_ts',
        'personal.idx_messages_tenant_conv'
    ]
    LOOP
        v_oid := to_regclass(v_name);
        IF v_oid IS NULL THEN
            RAISE EXCEPTION '% not found after CREATE — apply failed', v_name;
        END IF;
        SELECT indisvalid, indisunique INTO v_valid, v_unique
        FROM pg_index WHERE indexrelid = v_oid;
        IF NOT v_valid THEN
            RAISE EXCEPTION '% is INVALID (CONCURRENTLY build failed mid-way). Run: DROP INDEX CONCURRENTLY %; then re-apply this migration.', v_name, v_name;
        END IF;
        -- IF NOT EXISTS can silently adopt a WRONG pre-existing index of the same
        -- name. For the source-identity uniqueness guard that would leave
        -- duplicate source identities unconstrained, so assert it is actually a
        -- UNIQUE index on exactly (source_identity_hash).
        IF v_name = 'personal.idx_conv_source_identity_hash' THEN
            IF NOT v_unique THEN
                RAISE EXCEPTION '% exists but is NOT UNIQUE (a wrong pre-existing index was adopted). Run: DROP INDEX CONCURRENTLY %; then re-apply.', v_name, v_name;
            END IF;
            SELECT pg_get_indexdef(v_oid) INTO v_cols;
            IF v_cols !~ '\(source_identity_hash\)' THEN
                RAISE EXCEPTION '% is not on (source_identity_hash): %. Run: DROP INDEX CONCURRENTLY %; then re-apply.', v_name, v_cols, v_name;
            END IF;
        END IF;
    END LOOP;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'conversations_updated_at_not_null'
          AND conrelid = 'personal.conversations'::pg_catalog.regclass
    ) THEN
        ALTER TABLE personal.conversations
            ADD CONSTRAINT conversations_updated_at_not_null
            CHECK (updated_at IS NOT NULL) NOT VALID;
    END IF;
END $$;

ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conversations_updated_at_not_null;

ALTER TABLE personal.conversations
    ALTER COLUMN updated_at SET NOT NULL;

-- PostgreSQL can prove SET NOT NULL from the validated CHECK, so the CHECK is
-- redundant after the column metadata has been updated.
ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conversations_updated_at_not_null;

ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conversations_visibility_check;

ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conversations_team_visibility_check;
