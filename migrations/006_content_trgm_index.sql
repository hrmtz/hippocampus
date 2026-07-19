-- Trigram index on messages.content for substring search (ILIKE / ~*).
-- pg_trgm is already enabled. This replaces the 'simple' FTS leg in RRF hybrid
-- search with an ILIKE-based leg that correctly handles CJK compound words
-- (which 'simple' tokenizer treats as single indivisible tokens).
-- CONCURRENTLY avoids lock; takes a few minutes on large tables.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_content_trgm
    ON personal.messages USING gin (content gin_trgm_ops);
