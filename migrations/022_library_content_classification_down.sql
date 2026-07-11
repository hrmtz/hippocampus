-- Rollback migration 022

BEGIN;

ALTER TABLE library.conversations
    DROP CONSTRAINT IF EXISTS chk_classification_integrity;

DROP INDEX IF EXISTS library.idx_library_conv_content_class;

ALTER TABLE library.conversations
    DROP COLUMN IF EXISTS content_class,
    DROP COLUMN IF EXISTS quality_score,
    DROP COLUMN IF EXISTS classified_at;

COMMIT;
