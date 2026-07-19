-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/021_ghost_rrf_ranking.sql
-- Requires: migration 020 applied.
--
-- gh #34: replace the FTS-hard-prefilter in agent.search_ghost_ranked with
-- Reciprocal Rank Fusion (RRF) so a query with no FTS token overlap still
-- surfaces semantic near-matches.
--
-- Problem (migration 020): the candidate WHERE clause applied
--   m.fts @@ websearch_to_tsquery('simple', p_query_text)
-- as a HARD pre-filter. When a query tokenizes to terms absent from the
-- corpus (e.g. 'acme-proj EBC epic' — 'acme-proj' is one token, strict AND
-- never matches), the candidate set is empty and semantic ranking never runs.
-- Observed: such a query returned 0 rows even though a
-- project-embed-boundary-epic memory was a clear semantic match.
--
-- Fix: rank relevance by RRF over two independent legs — dense (cosine) and
-- FTS (ts_rank) — OR'd together (1/(60+rnk) per leg). A memory matching EITHER
-- signal surfaces; matching BOTH ranks highest. Importance
-- (base_score * recency_factor) drives LIST mode (empty query) and breaks
-- query-mode ties, so relevance leads when a query is present but a fresh,
-- zero-telemetry memory can still win on a strong semantic match.
--
-- Signature + RETURNS columns are UNCHANGED, so server.py:search_ghost_memory
-- needs no coordinated change (it reads the same columns). bump_activation is
-- left as migration 020 defined it.
--
-- Rollback: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/021_ghost_rrf_ranking_down.sql

BEGIN;

DROP FUNCTION IF EXISTS agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT);

CREATE FUNCTION agent.search_ghost_ranked(
    p_query_text         TEXT,
    p_query_vec          vector(1024),
    p_current_project    TEXT,
    p_include_restricted BOOLEAN,
    p_n_results          INT
)
RETURNS TABLE (
    id                   BIGINT,
    chassis_id           agent.chassis_id,
    source_project       TEXT,
    memory_slug          TEXT,
    memory_type          TEXT,
    title                TEXT,
    body                 TEXT,
    scope                agent.memory_scope,
    activation_count     INT,
    last_activated_at    TIMESTAMPTZ,
    first_dubbed_at      TIMESTAMPTZ,
    last_dubbed_at       TIMESTAMPTZ,
    base_score           DOUBLE PRECISION,
    recency_factor       DOUBLE PRECISION,
    semantic_sim         DOUBLE PRECISION,
    rank_score           DOUBLE PRECISION
)
LANGUAGE sql
SECURITY DEFINER
STABLE
-- pg_catalog FIRST (= CVE-2018-1058 mitigation); agent for ghost_* tables;
-- public for pgvector operators (<=>).
SET search_path = pg_catalog, agent, public
AS $$
    WITH candidates AS (
        SELECT
            m.id,
            m.chassis_id,
            m.source_project,
            m.memory_slug,
            m.memory_type,
            m.title,
            m.body,
            m.scope,
            m.dense,
            m.fts,
            m.first_dubbed_at,
            m.last_dubbed_at,
            COALESCE(t.activation_count, 0)         AS activation_count,
            COALESCE(t.incident_prevention, 0)      AS incident_prevention,
            COALESCE(t.user_endorsement, 0)         AS user_endorsement,
            COALESCE(t.user_correction, 0)          AS user_correction,
            COALESCE(t.prediction_error_ewma, 0.0)  AS prediction_error_ewma,
            t.last_activated_at
        FROM agent.ghost_memories m
        LEFT JOIN agent.ghost_telemetry t ON t.memory_pk = m.id
        WHERE m.deleted_at IS NULL
          AND (
              m.scope = 'shared'
              OR (
                  p_include_restricted
                  AND m.scope = 'shared-restricted'
                  AND m.id IN (
                      SELECT memory_pk
                      FROM agent.ghost_restricted_allowlist
                      WHERE allowed_project = p_current_project
                  )
              )
          )
          -- NOTE: no FTS pre-filter here (= gh #34). FTS is one RRF leg below,
          -- not a gate, so semantic-only matches survive.
    ),
    enriched AS (
        SELECT
            c.*,
            (c.activation_count    * 0.2
             + c.incident_prevention * 0.5
             + c.user_endorsement    * 0.3
             - c.user_correction     * 0.4
             - c.prediction_error_ewma * 0.2
             + CASE WHEN c.scope IN ('shared', 'shared-restricted') THEN 0.1 ELSE 0.0 END
            ) AS base_score,
            exp(
                -EXTRACT(EPOCH FROM (NOW() - COALESCE(c.last_activated_at, c.first_dubbed_at)))
                / (30.0 * 86400)
            ) AS recency_factor,
            -- GREATEST(.., 0) clamps negative/NaN semantic_sim (zero-vec dense →
            -- NaN cosine). Surfaced for caller display; NOT the rank driver.
            CASE
                WHEN p_query_vec IS NOT NULL AND c.dense IS NOT NULL
                THEN GREATEST(1.0 - (c.dense <=> p_query_vec), 0.0)
                ELSE 0.0
            END AS semantic_sim
        FROM candidates c
    ),
    -- Relevance leg 1: dense cosine rank (only when a query vector is supplied).
    dense_ranked AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY dense <=> p_query_vec) AS rnk
        FROM enriched
        WHERE p_query_vec IS NOT NULL AND dense IS NOT NULL
        LIMIT 100
    ),
    -- Relevance leg 2: FTS ts_rank (only when query text matches any token).
    fts_ranked AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank(fts, websearch_to_tsquery('simple', p_query_text)) DESC
               ) AS rnk
        FROM enriched
        WHERE p_query_text IS NOT NULL
          AND p_query_text <> ''
          AND fts @@ websearch_to_tsquery('simple', p_query_text)
        LIMIT 100
    )
    SELECT
        e.id,
        e.chassis_id,
        e.source_project,
        e.memory_slug,
        e.memory_type,
        e.title,
        e.body,
        e.scope,
        e.activation_count,
        e.last_activated_at,
        e.first_dubbed_at,
        e.last_dubbed_at,
        e.base_score,
        e.recency_factor,
        e.semantic_sim,
        -- RRF over the two relevance legs (query mode) PLUS an importance term
        -- that is active ONLY in list mode (= no query) so the empty-query
        -- overview still ranks by base_score * recency. In query mode importance
        -- is a tiebreaker (ORDER BY), keeping relevance the lead signal.
        (
            COALESCE(1.0 / (60 + dr.rnk), 0.0)
            + COALESCE(1.0 / (60 + fr.rnk), 0.0)
            + CASE
                  WHEN p_query_vec IS NULL
                       AND (p_query_text IS NULL OR p_query_text = '')
                  THEN e.base_score * e.recency_factor
                  ELSE 0.0
              END
        ) AS rank_score
    FROM enriched e
    LEFT JOIN dense_ranked dr ON dr.id = e.id
    LEFT JOIN fts_ranked  fr ON fr.id = e.id
    WHERE
        -- list mode: every in-scope memory is a candidate (importance overview)
        (p_query_vec IS NULL AND (p_query_text IS NULL OR p_query_text = ''))
        -- query mode: only memories matching at least one relevance leg
        OR dr.id IS NOT NULL
        OR fr.id IS NOT NULL
    ORDER BY rank_score DESC,
             e.base_score * e.recency_factor DESC,
             e.last_dubbed_at DESC
    LIMIT GREATEST(1, LEAST(p_n_results, 50));
$$;

COMMENT ON FUNCTION agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT) IS
    'Hybrid ghost ranking via RRF (gh #34). Relevance = RRF over dense-cosine + FTS legs (1/(60+rnk) each, OR-combined) so semantic-only matches survive when FTS misses. Importance (base_score*recency) drives list mode (empty query) and breaks query-mode ties. Signature/columns unchanged from migration 020. SECURITY DEFINER, search_path pinned.';

COMMIT;
