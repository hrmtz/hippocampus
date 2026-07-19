-- Preserve the chassis/environment provenance of shared diary memories.
-- source_host belongs to each ingested conversation; diary provenance is a
-- snapshot so later conversation edits cannot rewrite the memory's lineage.

ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS source_host TEXT;

ALTER TABLE personal.diary
    ADD COLUMN IF NOT EXISTS writer_host TEXT,
    ADD COLUMN IF NOT EXISTS writer_runtime TEXT,
    ADD COLUMN IF NOT EXISTS memory_mode TEXT,
    ADD COLUMN IF NOT EXISTS source_provenance JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN personal.conversations.source_host IS
    'Host/container where the source session was ingested; HIPPOCAMPUS_SOURCE_HOST overrides hostname.';
COMMENT ON COLUMN personal.diary.writer_host IS
    'Host/container that executed the diary writer.';
COMMENT ON COLUMN personal.diary.writer_runtime IS
    'Runtime that generated the memory, normally hippocampus-diary.';
COMMENT ON COLUMN personal.diary.memory_mode IS
    'How this memory arose; generated_from_transcripts for diary rows.';
COMMENT ON COLUMN personal.diary.source_provenance IS
    'Immutable-at-write snapshot of source conv_id/runtime/model/host records.';
