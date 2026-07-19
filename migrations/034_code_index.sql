-- 034_code_index.sql — deja-code cross-repo code index (issue #76)
-- Design: docs/designs/DEJA_CODE.md §5 (dual-magi plateau'd)
-- Shape adapted from library.books/chunks (004): parent record + ordered
-- chunks with dense halfvec(1024). FTS is INTENTIONALLY omitted — this layer
-- is semantic-similarity-only; identifier lookup belongs to grep.

CREATE SCHEMA IF NOT EXISTS code;

CREATE TABLE IF NOT EXISTS code.repos (
    repo_id     TEXT PRIMARY KEY,          -- dir name under ~/projects
    root_path   TEXT NOT NULL,             -- absolute path at index time
    head_commit TEXT,                      -- HEAD sha at last index
    file_count  INT DEFAULT 0,
    chunk_count INT DEFAULT 0,
    indexed_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS code.files (
    file_id    BIGSERIAL PRIMARY KEY,
    repo_id    TEXT NOT NULL REFERENCES code.repos(repo_id) ON DELETE CASCADE,
    path       TEXT NOT NULL,              -- repo-relative
    lang       TEXT NOT NULL,
    file_sha   TEXT NOT NULL,              -- sha256(bytes) — incremental skip key
    indexed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (repo_id, path)
);

CREATE TABLE IF NOT EXISTS code.chunks (
    id          BIGSERIAL PRIMARY KEY,
    file_id     BIGINT NOT NULL REFERENCES code.files(file_id) ON DELETE CASCADE,
    seq         INT NOT NULL,
    symbol      TEXT NOT NULL,             -- qualified (attachVoice.barsStart)
    kind        TEXT NOT NULL,             -- function|method|class|script_fn
    start_line  INT NOT NULL,
    end_line    INT NOT NULL,
    content     TEXT NOT NULL,
    content_sha TEXT NOT NULL,             -- sha256 — embed-reuse key
    dense       halfvec(1024),
    UNIQUE (file_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_file ON code.chunks(file_id);
-- NON-unique by design: identical boilerplate ACROSS repos is exactly the
-- duplication this layer exists to surface.
CREATE INDEX IF NOT EXISTS idx_code_chunks_sha  ON code.chunks(content_sha);
-- HNSW index lives in 034b (CONCURRENTLY, no_tx) — required, not optional.

-- code_read_hook: Stop-hook advisor reader. SELECT-only, code schema only,
-- no cross-schema reach (design §13.2). PG_URL_CODE_READ is MANDATORY for
-- Phase 1 — the advisor has no PG_URL fallback.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'code_read_hook') THEN
    CREATE ROLE code_read_hook LOGIN PASSWORD NULL;
  END IF;
END $$;

GRANT USAGE ON SCHEMA code TO code_read_hook;
GRANT SELECT ON code.repos, code.files, code.chunks TO code_read_hook;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA code
    FROM code_read_hook;
