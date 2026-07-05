-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/016_inject_lifecycle_down.sql
--
-- Phase 5 rollback: undoes 016_inject_lifecycle.sql.
-- ⚠️ Phase 5 solo rollback only. Phase 6 dependency on allowlist will loud-fail (intentional).
-- ⚠️ DATA LOSS: purge_log + allowlist + slug_history values are destroyed.

SET lock_timeout = '10s';

REVOKE ALL ON FUNCTION personal.purge_project(TEXT) FROM agent_purge_admin;
DROP FUNCTION IF EXISTS personal.purge_project(TEXT);

REVOKE ALL ON personal.purge_log FROM agent_purge_admin;
REVOKE ALL ON SEQUENCE personal.purge_log_id_seq FROM agent_purge_admin;
DROP TABLE IF EXISTS personal.purge_log;

ALTER TABLE personal.conversations DROP COLUMN IF EXISTS slug_history;

DROP TABLE IF EXISTS personal.conversation_inject_allowlist;

-- agent_purge_admin grants on personal.* — leave or revoke? Revoke to clean.
REVOKE SELECT, DELETE ON personal.conversations FROM agent_purge_admin;
REVOKE SELECT, DELETE ON personal.messages FROM agent_purge_admin;
REVOKE SELECT ON personal.conversation_segments FROM agent_purge_admin;
REVOKE SELECT, DELETE ON personal.conversation_read_log FROM agent_purge_admin;

RESET lock_timeout;
