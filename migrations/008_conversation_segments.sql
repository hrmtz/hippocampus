-- Segmented conversation vectors for proper recall on long sessions.
-- Long sessions (>200 msgs) are split into 200-message windows, each with
-- its own Haiku summary + BGE-M3 embedding. Enables search to find the
-- specific segment of a long conversation where a topic appeared.
CREATE TABLE IF NOT EXISTS personal.conversation_segments (
    id          BIGSERIAL PRIMARY KEY,
    conv_id     TEXT    NOT NULL REFERENCES personal.conversations(conv_id) ON DELETE CASCADE,
    seg_idx     INT     NOT NULL,
    start_seq   INT     NOT NULL,
    end_seq     INT     NOT NULL,
    summary_text TEXT,
    seg_dense   halfvec(1024),
    UNIQUE (conv_id, seg_idx)
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conv_segs_dense
    ON personal.conversation_segments USING hnsw (seg_dense halfvec_ip_ops);

CREATE INDEX IF NOT EXISTS idx_conv_segs_conv_id
    ON personal.conversation_segments (conv_id);
