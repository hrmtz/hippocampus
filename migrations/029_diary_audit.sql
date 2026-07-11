-- 029_diary_audit.sql
-- Diary grounding auditor storage (gh #64, docs/designs/DIARY_GROUNDING_AUDITOR.md).
--
-- Two pieces:
--   1. personal.diary.source_conv_ids — the diary generator EMITS the exact
--      conv_id population it summarized (corpus resolution DECIDED 2026-07-08:
--      emit-from-generator; re-derivation drifts — 07-07 measured 26 at
--      generation vs 25 at audit). The auditor inherits this array instead of
--      re-deriving the population.
--   2. personal.diary_audit — one verdict row per audited diary entry.
--      Rejections are logged too (a result, not a non-event). entry_date PK =
--      idempotent per date; re-runs upsert.
--
-- Kill switch: feature flag 'diary_grounding_auditor' (created disabled) +
-- env HIPPOCAMPUS_DIARY_AUDIT_DISABLE, mirroring the inject hooks.
-- Transaction-safe (no_tx=false).

ALTER TABLE personal.diary
    ADD COLUMN IF NOT EXISTS source_conv_ids TEXT[];

COMMENT ON COLUMN personal.diary.source_conv_ids IS
    'Exact conv_ids the diary writer summarized (emit-from-generator, gh #64). '
    'NULL = legacy row; auditor falls back to deterministic re-derivation.';

CREATE TABLE IF NOT EXISTS personal.diary_audit (
    entry_date    DATE PRIMARY KEY REFERENCES personal.diary(entry_date),
    claim         TEXT,
    verdict       TEXT NOT NULL CHECK (verdict IN
                      ('supported', 'rejected', 'inconclusive',
                       'no_actionable_reflection')),
    evidence_json JSONB,
    proposal_path TEXT,
    corpus_size   INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE personal.diary_audit IS
    'Read-only diary grounding auditor verdicts (gh #64). The auditor writes '
    'exactly one row here per run (+ a Discord one-liner); nothing else.';

INSERT INTO personal.feature_flags (flag_name, enabled, disabled_reason)
VALUES
    ('diary_grounding_auditor', FALSE,
     'MVP not yet activated; enable after 07-07 oracle smoke + human go (gh #64)')
ON CONFLICT (flag_name) DO NOTHING;
