-- 029_diary_audit_down.sql — reverse 029_diary_audit.sql
DROP TABLE IF EXISTS personal.diary_audit;
ALTER TABLE personal.diary DROP COLUMN IF EXISTS source_conv_ids;
DELETE FROM personal.feature_flags WHERE flag_name = 'diary_grounding_auditor';
