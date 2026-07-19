-- 032_multiuser_source_identity_enforce.sql
-- Enforce complete conversation source identity after Slice 2 stamping.
--
-- Apply only after the multiuser_gap_window_backfill_complete checkpoint. This
-- file is intentionally no_tx=true so the ADD CONSTRAINT and full-table
-- VALIDATE run as individually committed statements.
--
-- Requires: 031_org_multiuser.sql and deployed Slice 2 ingest stamping.
-- Rollback: psql -v ON_ERROR_STOP=1 -f migrations/032_multiuser_source_identity_enforce_down.sql

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'conversations_source_identity_check'
          AND conrelid = 'personal.conversations'::pg_catalog.regclass
    ) THEN
        ALTER TABLE personal.conversations
            ADD CONSTRAINT conversations_source_identity_check
            CHECK (
                source_conv_id IS NOT NULL
                AND source_platform IS NOT NULL
                AND source_adapter IS NOT NULL
                AND source_identity_hash IS NOT NULL
            ) NOT VALID;
    END IF;
END $$;

ALTER TABLE personal.conversations
    VALIDATE CONSTRAINT conversations_source_identity_check;
