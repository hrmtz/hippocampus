ALTER TABLE personal.diary
    DROP COLUMN IF EXISTS source_provenance,
    DROP COLUMN IF EXISTS memory_mode,
    DROP COLUMN IF EXISTS writer_runtime,
    DROP COLUMN IF EXISTS writer_host;

ALTER TABLE personal.conversations DROP COLUMN IF EXISTS source_host;
