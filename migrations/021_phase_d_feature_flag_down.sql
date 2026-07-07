-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/021_phase_d_feature_flag_down.sql
--
-- Remove the Phase D feature flag row. Safe to run even if Phase D code
-- is still installed — code will simply gate to legacy fallback when
-- the flag row is absent.
--
-- ⚠️ depends on no FK to personal.feature_flags(flag_name). If a future
-- migration adds an audit table FK referencing this column, DELETE will
-- fail with foreign_key_violation and the whole transaction rolls back.
-- Coordinate a CASCADE / RESTRICT decision before adding any such FK.

BEGIN;

DELETE FROM personal.feature_flags WHERE flag_name = 'phase_d_task_aware_inject';

COMMIT;
