-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/021_ghost_rrf_ranking_down.sql
-- Reverts gh #34: restores the migration 020 FTS-hard-prefilter version of
-- agent.search_ghost_ranked. bump_activation is untouched by 021, so it is
-- left as-is. Signature/columns are identical, so server.py needs no revert.

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
          AND (
              p_query_text IS NULL
              OR p_query_text = ''
              OR m.fts @@ websearch_to_tsquery('simple', p_query_text)
          )
    ),
    scored AS (
        SELECT
            c.id,
            c.chassis_id,
            c.source_project,
            c.memory_slug,
            c.memory_type,
            c.title,
            c.body,
            c.scope,
            c.activation_count,
            c.last_activated_at,
            c.first_dubbed_at,
            c.last_dubbed_at,
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
            CASE
                WHEN p_query_vec IS NOT NULL AND c.dense IS NOT NULL
                THEN GREATEST(1.0 - (c.dense <=> p_query_vec), 0.0)
                ELSE 0.0
            END AS semantic_sim
        FROM candidates c
    )
    SELECT
        s.id,
        s.chassis_id,
        s.source_project,
        s.memory_slug,
        s.memory_type,
        s.title,
        s.body,
        s.scope,
        s.activation_count,
        s.last_activated_at,
        s.first_dubbed_at,
        s.last_dubbed_at,
        s.base_score,
        s.recency_factor,
        s.semantic_sim,
        (s.base_score * s.recency_factor + s.semantic_sim * 0.5) AS rank_score
    FROM scored s
    ORDER BY rank_score DESC, s.last_dubbed_at DESC
    LIMIT GREATEST(1, LEAST(p_n_results, 50));
$$;

COMMENT ON FUNCTION agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT) IS
    'Hybrid FTS + vector ranking for ghost_memories. SECURITY DEFINER (search_path pinned) lets agent_read_mcp rank without raw dense column access. rank_score = base_score * recency_factor + GREATEST(semantic_sim, 0) * 0.5; semantic_sim = 1 - cosine_distance when query_vec NOT NULL else 0 (clamped at 0).';

COMMIT;
