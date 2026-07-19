-- 023_extracted_facts.sql
-- Distilled facts layer: Haiku-extracted key facts from personal conversations.
-- Enables high-signal search for decisions/preferences/context without raw message noise.
-- no_tx=true because CREATE INDEX CONCURRENTLY cannot run inside a transaction.

CREATE TABLE IF NOT EXISTS personal.extracted_facts (
    id           BIGSERIAL PRIMARY KEY,
    conv_id      TEXT NOT NULL
                 REFERENCES personal.conversations(conv_id) ON DELETE CASCADE,
    fact_text    TEXT NOT NULL,
    dense        halfvec(1024),
    fts          TSVECTOR GENERATED ALWAYS AS
                 (to_tsvector('simple', coalesce(fact_text, ''))) STORED,
    extracted_at TIMESTAMPTZ DEFAULT now(),
    model_used   TEXT DEFAULT 'claude-haiku-4-5-20251001'
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS extracted_facts_dense_hnsw
    ON personal.extracted_facts USING hnsw (dense halfvec_ip_ops);

CREATE INDEX IF NOT EXISTS extracted_facts_fts_gin
    ON personal.extracted_facts USING gin (fts);

CREATE INDEX IF NOT EXISTS extracted_facts_conv_id
    ON personal.extracted_facts (conv_id);
