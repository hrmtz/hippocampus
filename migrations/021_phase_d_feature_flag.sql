-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/021_phase_d_feature_flag.sql
--
-- Phase D (= task-aware SessionStart inject) の primary feature flag を
-- personal.feature_flags に row として用意。 default disabled (= rollout
-- 期は 1 host で UPDATE で enable、 24h watch、 regression なし → 全 host)。
--
-- column set per migration 014: flag_name TEXT PK / enabled BOOLEAN /
-- disabled_reason TEXT / updated_at TIMESTAMPTZ (= r2-codex-1 fix、 旧
-- v0.2 doc は存在しない description 列を参照していた)。

BEGIN;

INSERT INTO personal.feature_flags (flag_name, enabled, disabled_reason)
VALUES (
    'phase_d_task_aware_inject',
    FALSE,
    'Phase D pre-ship; UPDATE enabled=TRUE per host after smoke pass + 24h watch'
)
ON CONFLICT (flag_name) DO NOTHING;

COMMIT;
