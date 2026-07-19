-- library.books + library.chunks: long-form literary / prose texts
-- Separate from library.conversations (dialogue-type) — this is for long-form prose.

CREATE TABLE IF NOT EXISTS library.books (
    book_id     TEXT PRIMARY KEY,   -- work ID from the source metadata
    source      TEXT NOT NULL DEFAULT 'text',
    title       TEXT NOT NULL,
    author      TEXT,
    orthography TEXT,               -- 文字遣い種別 (新字新仮名 / 旧字旧仮名 etc.)
    published   TEXT,               -- 公開日 (string, not always parseable as date)
    source_url  TEXT,               -- 図書カードURL
    meta        JSONB,              -- full original meta blob
    chunk_count INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lib_books_source     ON library.books(source);
CREATE INDEX IF NOT EXISTS idx_lib_books_author     ON library.books(author);
CREATE INDEX IF NOT EXISTS idx_lib_books_orthography ON library.books(orthography);

CREATE TABLE IF NOT EXISTS library.chunks (
    id       BIGSERIAL PRIMARY KEY,
    book_id  TEXT NOT NULL REFERENCES library.books(book_id) ON DELETE CASCADE,
    seq      INT  NOT NULL,
    content  TEXT NOT NULL,
    dense    halfvec(1024),
    fts      TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED,
    UNIQUE (book_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_lib_chunks_book_id ON library.chunks(book_id);
CREATE INDEX IF NOT EXISTS idx_lib_chunks_fts     ON library.chunks USING GIN(fts);

-- HNSW index built after bulk load for speed (run after ingest completes)
-- CREATE INDEX idx_lib_chunks_dense ON library.chunks USING hnsw (dense halfvec_ip_ops) WITH (m=16, ef_construction=64);
