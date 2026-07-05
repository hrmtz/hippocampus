-- Conversation-level scoring metadata used by ingest scripts and MCP display tools.
-- Idempotent migration for fresh installs that only applied 001/002.

ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS intensity SMALLINT,
    ADD COLUMN IF NOT EXISTS ai_engagement SMALLINT,
    ADD COLUMN IF NOT EXISTS dominant_topic TEXT,
    ADD COLUMN IF NOT EXISTS scored_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_conv_scored_at
    ON personal.conversations(scored_at);
