-- 026_diary.sql
-- Daily diary layer (= "fast 層" of the personality-formation DB).
-- One first-person, candid-observation diary entry per JST calendar day,
-- written by the 03:00 cron from that day's claude_code conversations.
--
-- Design invariants (enforced in src/hippocampus/ingest/diary.py):
--   - windowed continuity: the writer reads the prior PRIOR_WINDOW (=7) days of
--     diary prose for continuity, kept bounded (leaky integrator + tone-mimicry
--     ban + day-to-day drift meter) so the tone-reinforcement loop stays
--     regulated rather than cut (design pivoted 2026-06-25, see diary.py)
--   - grounding required: observations must trace to real exchanges
--   - store-only: never injected into a live session in this phase
--
-- One row per day (= 365/yr), so no HNSW; a seq scan over the dense column
-- and a GIN over fts are sufficient. Transaction-safe (no_tx=false).

CREATE TABLE IF NOT EXISTS personal.diary (
    entry_date   DATE PRIMARY KEY,
    body         TEXT NOT NULL,
    dense        halfvec(1024),
    fts          TSVECTOR GENERATED ALWAYS AS
                 (to_tsvector('simple', coalesce(body, ''))) STORED,
    conv_count   INT DEFAULT 0,
    model_used   TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS diary_fts_gin
    ON personal.diary USING gin (fts);
