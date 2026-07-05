CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS personal;

CREATE TABLE IF NOT EXISTS personal.conversations (
    conv_id     TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,          -- 'chatgpt' | 'claude_code'
    title       TEXT,
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    msg_count   INT DEFAULT 0,
    model       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS personal.messages (
    id          BIGSERIAL PRIMARY KEY,
    conv_id     TEXT NOT NULL REFERENCES personal.conversations(conv_id),
    msg_id      TEXT,
    role        TEXT NOT NULL,          -- 'user' | 'assistant' | 'system' | 'tool'
    content     TEXT,
    content_type TEXT DEFAULT 'text',
    ts          TIMESTAMPTZ,
    seq         INT,
    dense       halfvec(1024),
    fts         TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED,
    UNIQUE (conv_id, msg_id)
);

CREATE TABLE IF NOT EXISTS personal.access_log (
    id          BIGSERIAL PRIMARY KEY,
    query       TEXT,
    tool        TEXT,
    result_count INT,
    ts          TIMESTAMPTZ DEFAULT NOW()
);

-- indexes (created after bulk insert)
CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON personal.messages(conv_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON personal.messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_fts ON personal.messages USING GIN(fts);
CREATE INDEX IF NOT EXISTS idx_conv_platform ON personal.conversations(platform);
CREATE INDEX IF NOT EXISTS idx_conv_started ON personal.conversations(started_at);
