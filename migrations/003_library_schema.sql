-- library schema: external reference memory (media scripts, books, etc.)
-- Mirrors personal.* structure but without topic_cluster FK (library data is
-- objective reference material, not personal episodic memory).

CREATE SCHEMA IF NOT EXISTS library;

CREATE TABLE IF NOT EXISTS library.conversations (
    conv_id     TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    title       TEXT,
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    msg_count   INT DEFAULT 0,
    model       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS library.messages (
    id          BIGSERIAL PRIMARY KEY,
    conv_id     TEXT NOT NULL REFERENCES library.conversations(conv_id),
    msg_id      TEXT,
    role        TEXT NOT NULL,
    content     TEXT,
    content_type TEXT DEFAULT 'text',
    ts          TIMESTAMPTZ,
    seq         INT,
    dense       halfvec(1024),
    fts         TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED,
    UNIQUE (conv_id, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_lib_messages_conv_id ON library.messages(conv_id);
CREATE INDEX IF NOT EXISTS idx_lib_messages_ts ON library.messages(ts);
CREATE INDEX IF NOT EXISTS idx_lib_messages_fts ON library.messages USING GIN(fts);
CREATE INDEX IF NOT EXISTS idx_lib_conv_platform ON library.conversations(platform);


-- HNSW vector index (build after data load for speed)
CREATE INDEX IF NOT EXISTS idx_lib_messages_dense
    ON library.messages USING hnsw (dense halfvec_ip_ops) WITH (m=16, ef_construction=64);
