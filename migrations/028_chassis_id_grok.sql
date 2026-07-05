-- migrations/028_chassis_id_grok.sql
--
-- Grok CLI chassis support: agent.chassis_id ENUM に 'grok' 追加。
-- SessionStart inject + ghost_read_log / conversation_read_log の監査用。
--
-- ⚠️ ALTER TYPE ADD VALUE は同 tx 内で新 value を参照できない。

ALTER TYPE agent.chassis_id ADD VALUE IF NOT EXISTS 'grok';
