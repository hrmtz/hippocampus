-- migrations/012_heartbeat_include_partial.sql
--
-- ghost_dub_heartbeat view を 'ok' OR 'partial' を success 扱いに修正。
-- 既存の別 project 由来 memory file の 8 件は YAML mapping ambiguity で
-- 永続的に parse_error になる (= user content 触らず受容)、 dub script
-- 自体は機能してる。 status='partial' を「機能してる」 と扱わないと
-- heartbeat alert が永久 fire する。
--
-- 関連 finding: phase 3 health check で発覚 (= 2026-05-21)。

CREATE OR REPLACE VIEW agent.ghost_dub_heartbeat AS
SELECT
    chassis_id,
    host,
    MAX(finished_at) FILTER (WHERE status IN ('ok', 'partial')) AS last_successful_run,
    NOW() - MAX(finished_at) FILTER (WHERE status IN ('ok', 'partial')) AS staleness
FROM agent.ghost_dub_run
GROUP BY chassis_id, host;
