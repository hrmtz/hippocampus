-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/015_inject_audit_role_down.sql
--
-- Phase 4 rollback: undoes 015_inject_audit_role.sql.
--
-- ⚠️ WARNING: Phase 4 solo rollback only. If Phase 5+ migrations / Phase 6
-- hook depend on v_conversations_inject_safe or conversation_read_log, this
-- will FAIL with loud dependency errors (intentional).
--
-- ⚠️ DATA LOSS: dropping conversation_read_log destroys all audit history.
-- Take a backup if forensics retention matters:
--   \copy personal.conversation_read_log TO '~/.local/share/hippocampus/backups/read_log_$(date +%Y%m%d).csv' CSV HEADER
--
-- Order: restore GRANTs → drop view → drop tables (default first, then named partitions, then parent).

SET lock_timeout = '10s';

-- (4) Restore canonical_project_slug EXECUTE TO PUBLIC.
GRANT EXECUTE ON FUNCTION personal.canonical_project_slug(TEXT, TEXT) TO PUBLIC;
REVOKE EXECUTE ON FUNCTION personal.canonical_project_slug(TEXT, TEXT) FROM agent_read_mcp;
REVOKE EXECUTE ON FUNCTION personal.canonical_project_slug(TEXT, TEXT) FROM agent_dub;

-- (3) Revoke audit-only grants. agent_read_mcp permission state returns to
-- whatever migration 009 / Phase 1 left it (= may have had no direct grant).
REVOKE USAGE, SELECT ON SEQUENCE personal.conversation_read_log_id_seq FROM agent_read_mcp;
REVOKE INSERT ON personal.conversation_read_log FROM agent_read_mcp;
REVOKE SELECT ON personal.v_conversations_inject_safe FROM agent_read_mcp;
-- USAGE on schema personal stays (= other Phase 4+ grants may need it).

-- (2) Drop view.
DROP VIEW IF EXISTS personal.v_conversations_inject_safe;

-- (1) Drop audit log (cascade through partitions).
DROP TABLE IF EXISTS personal.conversation_read_log CASCADE;

RESET lock_timeout;
