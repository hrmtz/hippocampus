-- 027_wiki.sql
-- LLM-wiki layer (= editable subject-knowledge pages, design LLM_WIKI_LAYER.md
-- plateau v4). Durable, re-derivable wiki built from conversation evidence:
--   - personal.wiki_pages        : body_md is the durable SoT (NO FK to
--                                   conversations — durability inversion: the
--                                   page outlives the source transcript)
--   - personal.wiki_claims       : a re-derived projection, fully REPLACED on
--                                   each apply (no zombie lineage); evidence
--                                   span (source_conv_id, source_msg_id) is
--                                   best-effort grounding, NOT the trust control
--   - personal.wiki_merge_log    : append-only audit (INSERT/SELECT only by
--                                   privilege), prior_body = rollback snapshot,
--                                   merge_id UNIQUE = idempotency anchor
--   - personal.wiki_merge_staging: propose→apply handoff, base_plateau_rev =
--                                   optimistic staleness check at apply
--
-- Schema = personal (NOT agent). Subject knowledge NEVER enters the ghost
-- cross-project vault: agent_dub is never granted on any wiki_* table.
--
-- Transaction-safe (no_tx=false). The runner applies this with psql -1, so the
-- whole file is ONE transaction — required for the SET LOCAL ROLE invariant
-- DO-block at the tail. Do NOT self-wrap BEGIN/COMMIT (matches 026). No
-- CONCURRENTLY / no ALTER TYPE ADD VALUE, so the no-tx scanner stays clean.
-- All CREATEs use IF NOT EXISTS for re-run safety (009/026 idiom).
--
-- Requires: 009 (= agent_read_mcp role + CREATEROLE), 014 (= feature_flags),
--           001 (= personal.conversations / personal.messages for evidence).
--
-- Ships the feature flag OFF, so `hippocampus migrate` is inert until the
-- operator flips personal.feature_flags.wiki_layer = TRUE after smoke.
--
-- Rollback: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/027_wiki_down.sql
--   ⚠️ down drops the durable body_md (irreversible data loss) — take a
--      sanada backup / pg_dump of personal.wiki_* before running it.

-- ===========================================================================
-- (1) wiki_pages — durable SoT (body_md). No FK to conversations.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS personal.wiki_pages (
    slug         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    domain       TEXT,
    body_md      TEXT NOT NULL DEFAULT '',
    body_sha     TEXT,
    plateau_rev  INT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- (2) wiki_claims — re-derived projection, fully replaced each apply.
--     superseded_by kept as a nullable column only (no zombie lineage in v0,
--     so the r3-schema-1 terminal-status concern is moot here).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS personal.wiki_claims (
    id             BIGSERIAL PRIMARY KEY,
    page_slug      TEXT NOT NULL
                   REFERENCES personal.wiki_pages(slug) ON DELETE CASCADE,
    section        TEXT,
    claim_text     TEXT NOT NULL,
    claim_hash     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'live'
                   CHECK (status IN ('live', 'struck', 'open-question')),
    -- evidence span (pairs with personal.messages UNIQUE(conv_id, msg_id));
    -- both nullable, but both-or-neither (a half-span is not a span).
    source_conv_id TEXT
                   REFERENCES personal.conversations(conv_id) ON DELETE SET NULL,
    source_msg_id  TEXT,
    superseded_by  BIGINT
                   REFERENCES personal.wiki_claims(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT wiki_claims_evidence_pair_chk
        CHECK ((source_conv_id IS NULL) = (source_msg_id IS NULL))
);

-- dedupe only over the live projection (struck / open-question may repeat text)
CREATE UNIQUE INDEX IF NOT EXISTS wiki_claims_dedupe
    ON personal.wiki_claims (page_slug, claim_hash)
    WHERE status = 'live';
CREATE INDEX IF NOT EXISTS wiki_claims_page
    ON personal.wiki_claims (page_slug);

-- ===========================================================================
-- (3) wiki_merge_log — append-only audit. INSERT/SELECT only by privilege
--     (append-only is real, not convention — see invariant DO-block below).
--     merge_id UNIQUE = idempotency anchor (a double apply hits this, no-ops).
--     prior_body = rollback snapshot (representable rollback).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS personal.wiki_merge_log (
    id           BIGSERIAL PRIMARY KEY,
    merge_id     UUID NOT NULL,
    page_slug    TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    op_summary   JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    prior_body   TEXT,             -- body snapshot before this merge (rollback)
    prior_claims JSONB,            -- live-claim snapshot before this merge so
                                   -- rollback restores claims deterministically
                                   -- (no LLM re-derivation needed)
    CONSTRAINT wiki_merge_log_merge_uniq UNIQUE (merge_id)
);

CREATE INDEX IF NOT EXISTS wiki_merge_log_page
    ON personal.wiki_merge_log (page_slug, created_at DESC);

-- ===========================================================================
-- (4) wiki_merge_staging — propose→apply handoff.
--     base_plateau_rev = optimistic staleness check (reject if page moved
--     since propose).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS personal.wiki_merge_staging (
    merge_id         UUID PRIMARY KEY,
    page_slug        TEXT NOT NULL,
    proposed_body    TEXT NOT NULL,
    derived_claims   JSONB NOT NULL,
    base_plateau_rev INT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'applied', 'expired')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wiki_merge_staging_pending
    ON personal.wiki_merge_staging (page_slug)
    WHERE status = 'pending';

-- ===========================================================================
-- (5) agent_wiki_writer role (009 idiom: DO-block IF NOT EXISTS gate).
--     NOLOGIN — used via SET LOCAL ROLE inside the owner's apply tx, so the
--     INSERT-only-on-log boundary is genuinely enforced without a new login
--     secret. Operator may later ALTER ROLE ... LOGIN PASSWORD for a distinct
--     login boundary (+ sops PG_URL_AGENT_WIKI_WRITER).
-- ===========================================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_wiki_writer') THEN
    CREATE ROLE agent_wiki_writer NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA personal TO agent_wiki_writer;

-- the writer connects in the opt-in login-boundary mode (PG_URL_AGENT_WIKI_WRITER)
-- with NO owner connection, so it must read the feature flag itself. Schema USAGE
-- does NOT confer table SELECT; grant it explicitly (mirrors 017's grant to
-- agent_read_mcp). Without this, apply/rollback die at _require_flag in login mode.
GRANT SELECT ON personal.feature_flags TO agent_wiki_writer;

GRANT SELECT, INSERT, UPDATE ON personal.wiki_pages         TO agent_wiki_writer;
-- wiki_claims is a re-derived projection, fully REPLACED each apply (DELETE +
-- re-INSERT). DELETE is by-design here (NOT the append-only surface — that is
-- wiki_merge_log only, which keeps its INSERT/SELECT-only grant below).
GRANT SELECT, INSERT, UPDATE, DELETE ON personal.wiki_claims TO agent_wiki_writer;
GRANT SELECT, INSERT, UPDATE ON personal.wiki_merge_staging TO agent_wiki_writer;

-- append-only: INSERT + SELECT, never UPDATE/DELETE.
GRANT SELECT, INSERT ON personal.wiki_merge_log TO agent_wiki_writer;
-- defensive: ensure UPDATE/DELETE are not present from any prior re-run.
REVOKE UPDATE, DELETE ON personal.wiki_merge_log FROM agent_wiki_writer;

GRANT USAGE, SELECT ON SEQUENCE personal.wiki_claims_id_seq    TO agent_wiki_writer;
GRANT USAGE, SELECT ON SEQUENCE personal.wiki_merge_log_id_seq TO agent_wiki_writer;

-- NB: agent_dub is NEVER granted on any wiki_* table (never-granted, not a
-- REVOKE). Subject knowledge cannot reach the ghost cross-project vault.

-- ===========================================================================
-- (6) redacted read view (015 parity): body only, NO provenance/claim columns.
-- ===========================================================================
CREATE OR REPLACE VIEW personal.v_wiki_inject_safe AS
SELECT
    slug,
    title,
    domain,
    body_md,
    plateau_rev,
    updated_at
FROM personal.wiki_pages;

GRANT SELECT ON personal.v_wiki_inject_safe TO agent_read_mcp;

-- Defensive REVOKE (idempotent no-op — never granted; mirrors 015's
-- REVOKE-then-grant-view discipline). agent_read_mcp reads only via the view.
REVOKE ALL ON personal.wiki_pages         FROM agent_read_mcp;
REVOKE ALL ON personal.wiki_claims        FROM agent_read_mcp;
REVOKE ALL ON personal.wiki_merge_log     FROM agent_read_mcp;
REVOKE ALL ON personal.wiki_merge_staging FROM agent_read_mcp;

-- ===========================================================================
-- (7) feature flag (014 table) — ships OFF; migrate is inert until flipped.
-- ===========================================================================
INSERT INTO personal.feature_flags (flag_name, enabled, disabled_reason)
VALUES ('wiki_layer', FALSE, 'v0 manual enable after smoke')
ON CONFLICT (flag_name) DO NOTHING;

-- ===========================================================================
-- (8) invariant verification (009 SET LOCAL ROLE + sub-block savepoint +
--     RAISE force_rollback pattern). Asserts append-only is REAL:
--       negative: agent_wiki_writer gets insufficient_privilege on UPDATE and
--                 on DELETE of personal.wiki_merge_log
--       positive: agent_wiki_writer CAN INSERT into personal.wiki_merge_log
--                 (then force_rollback so no test row persists)
--     RAISE EXCEPTION aborts the whole migration if any test disagrees.
-- ⚠️ PL/pgSQL has no explicit SAVEPOINT; the BEGIN/EXCEPTION/END sub-block is
--    an implicit savepoint, and RAISE EXCEPTION forces its rollback.
-- ===========================================================================
DO $$
DECLARE
    neg_update_denied BOOLEAN := FALSE;  -- TRUE = privilege correctly denied
    neg_delete_denied BOOLEAN := FALSE;
    pos_insert_ok     BOOLEAN := FALSE;
BEGIN
    -- negative test 1: UPDATE must be denied (permission checked even on 0 rows)
    BEGIN
        SET LOCAL ROLE agent_wiki_writer;
        BEGIN
            EXECUTE 'UPDATE personal.wiki_merge_log SET session_id = session_id';
            neg_update_denied := FALSE;  -- reached = privilege exists = violation
        EXCEPTION
            WHEN insufficient_privilege THEN
                neg_update_denied := TRUE;
        END;
        RESET ROLE;
    EXCEPTION WHEN OTHERS THEN
        RESET ROLE;
        RAISE EXCEPTION 'wiki invariant infra failed (update test): %', SQLERRM;
    END;

    -- negative test 2: DELETE must be denied
    BEGIN
        SET LOCAL ROLE agent_wiki_writer;
        BEGIN
            EXECUTE 'DELETE FROM personal.wiki_merge_log';
            neg_delete_denied := FALSE;
        EXCEPTION
            WHEN insufficient_privilege THEN
                neg_delete_denied := TRUE;
        END;
        RESET ROLE;
    EXCEPTION WHEN OTHERS THEN
        RESET ROLE;
        RAISE EXCEPTION 'wiki invariant infra failed (delete test): %', SQLERRM;
    END;

    -- positive test: INSERT must succeed, then force-rollback the test row
    BEGIN
        SET LOCAL ROLE agent_wiki_writer;
        BEGIN
            EXECUTE 'INSERT INTO personal.wiki_merge_log '
                    '(merge_id, page_slug, session_id, op_summary) '
                    'VALUES (gen_random_uuid(), ''_invariant_test'', '
                    '''_invariant_test'', ''{}''::jsonb)';
            pos_insert_ok := TRUE;
            RAISE EXCEPTION 'force_rollback' USING ERRCODE = 'P0001';
        EXCEPTION
            WHEN SQLSTATE 'P0001' THEN
                NULL;  -- expected: test row rolled back, pos_insert_ok stays TRUE
            WHEN insufficient_privilege THEN
                pos_insert_ok := FALSE;
        END;
        RESET ROLE;
    EXCEPTION WHEN OTHERS THEN
        RESET ROLE;
        RAISE EXCEPTION 'wiki invariant positive test infra failed: %', SQLERRM;
    END;

    IF NOT neg_update_denied THEN
        RAISE EXCEPTION 'INVARIANT VIOLATION: agent_wiki_writer can UPDATE '
                        'personal.wiki_merge_log (append-only broken)';
    END IF;
    IF NOT neg_delete_denied THEN
        RAISE EXCEPTION 'INVARIANT VIOLATION: agent_wiki_writer can DELETE '
                        'personal.wiki_merge_log (append-only broken)';
    END IF;
    IF NOT pos_insert_ok THEN
        RAISE EXCEPTION 'POSITIVE TEST FAILED: agent_wiki_writer cannot INSERT '
                        'into personal.wiki_merge_log (GRANT too restrictive)';
    END IF;
    RAISE NOTICE 'wiki invariant verified: agent_wiki_writer is INSERT/SELECT-only '
                 'on personal.wiki_merge_log (append-only enforced by privilege)';
END $$;
