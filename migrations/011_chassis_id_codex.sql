-- migrations/011_chassis_id_codex.sql
--
-- Phase 4 minimum: agent.chassis_id ENUM に 'codex' 追加。
-- Phase 0 では 'claude-code' のみだったが、 codex CLI からの read 動作が
-- 確認できたため (= 同一 dev host 上、 同 hippocampus MCP server に stdio 接続)、
-- ghost_read_log で正しく chassis_id='codex' と記録するための準備。
--
-- ⚠️ ALTER TYPE ADD VALUE は PG 12+ で transaction OK だが、 同 tx 内で
--   新 value を参照できない。 single statement で安全。

ALTER TYPE agent.chassis_id ADD VALUE IF NOT EXISTS 'codex';

-- 確認 (= 別 transaction で実行する想定):
-- SELECT enum_range(NULL::agent.chassis_id);
--   expected: {claude-code,codex}
