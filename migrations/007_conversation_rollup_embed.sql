-- Conversation-level rollup vectors for 2-stage search (Phase 3).
-- summary_text: Haiku-generated ~100-word summary of the conversation.
-- conv_dense: BGE-M3 embedding of summary_text, halfvec(1024).
-- HNSW index enables fast conversation-level ANN search before drilling into messages.
ALTER TABLE personal.conversations ADD COLUMN IF NOT EXISTS summary_text TEXT;
ALTER TABLE personal.conversations ADD COLUMN IF NOT EXISTS conv_dense halfvec(1024);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversations_conv_dense
    ON personal.conversations USING hnsw (conv_dense halfvec_ip_ops);
