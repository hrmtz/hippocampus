-- 進行中セッションの差分 ingest 用: JSONL ファイルサイズを追跡
ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS last_file_size BIGINT DEFAULT 0;

COMMENT ON COLUMN personal.conversations.last_file_size IS
    'Last seen JSONL file size in bytes. Used to detect grown sessions for incremental re-ingest.';
