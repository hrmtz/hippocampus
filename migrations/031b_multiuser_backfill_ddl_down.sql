-- 031b_multiuser_backfill_ddl_down.sql
-- Roll back post-backfill indexes and the conversations.updated_at NOT NULL.
-- Run without psql -1: DROP INDEX CONCURRENTLY is forbidden in a transaction.

DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_tenant_owner_started;
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_tenant_visibility_started;
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_tenant_team_started;
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_source_identity_hash;
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_conv_tenant_source_lookup;
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_messages_tenant_owner_ts;
DROP INDEX CONCURRENTLY IF EXISTS personal.idx_messages_tenant_conv;

ALTER TABLE personal.conversations
    ALTER COLUMN updated_at DROP NOT NULL;

ALTER TABLE personal.conversations
    DROP CONSTRAINT IF EXISTS conversations_updated_at_not_null;

-- 031 owns conversations_visibility_check and
-- conversations_team_visibility_check. Their validated state is intentionally
-- left in place because constraint validation is not reversible.
