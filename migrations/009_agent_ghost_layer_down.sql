-- migrations/009_agent_ghost_layer_down.sql
-- ⚠️ destructive、 agent.* 全消去、 user 確認必須
BEGIN;
DROP VIEW IF EXISTS agent.ghost_dub_heartbeat;
DROP VIEW IF EXISTS agent.ghost_unified_no_vector;
DROP VIEW IF EXISTS agent.ghost_unified;
DROP TABLE IF EXISTS agent.ghost_read_log CASCADE;
DROP TABLE IF EXISTS agent.ghost_dub_run;
DROP TABLE IF EXISTS agent.ghost_dub_log CASCADE;
DROP TABLE IF EXISTS agent.ghost_restricted_allowlist;
DROP TABLE IF EXISTS agent.ghost_evidence;
DROP TABLE IF EXISTS agent.ghost_telemetry;
DROP TABLE IF EXISTS agent.ghost_memories;
DROP TYPE IF EXISTS agent.dub_run_status;
DROP TYPE IF EXISTS agent.dub_action;
DROP TYPE IF EXISTS agent.memory_type;
DROP TYPE IF EXISTS agent.memory_scope;
DROP TYPE IF EXISTS agent.chassis_id;
DROP SCHEMA IF EXISTS agent;
-- role は残す (= 別 schema 再構築時に再利用、 明示削除は user 判断)
-- DROP ROLE agent_dub;
-- DROP ROLE agent_acl_admin;
-- DROP ROLE agent_purge_admin;
-- DROP ROLE agent_read_mcp;
COMMIT;
