-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/020_ghost_hybrid_ranking.sql
--
-- Hybrid ghost_memories ranking (= FTS + vector semantic) + activation
-- auto-bump on search-return, encapsulated in SECURITY DEFINER functions
-- so agent_read_mcp can rank without raw access to `dense` column or to
-- ghost_telemetry UPDATE.
--
-- Background: per server.py search_ghost_memory, scoring used base_score
-- (= activation*0.2 + incident_prevention*0.5 + endorsement*0.3
--    - correction*0.4 - pred_error*0.2 + scope_bonus 0.1) * exp(-days/30).
-- Freshly-dubbed memories (= no telemetry yet) collapsed to base_score=0.1,
-- so `search_ghost_memory(query=...)` ordered N candidates all at +0.100
-- with no relevance differentiation — see user feedback 2026-05-23 ghost
-- session.
--
-- This migration adds:
--   1. agent.search_ghost_ranked(...) — returns ranked rows with
--      base_score / recency / semantic_sim columns surfaced so callers
--      can show the score breakdown. Hybrid:
--          rank_score = base_score * recency + GREATEST(semantic_sim, 0) * 0.5
--      When query_vec IS NULL (= list mode), semantic_sim defaults to 0.
--      semantic_sim is clamped at 0 to prevent NaN propagation (= zero-vec
--      stored dense → NaN cosine → NaN rank → ORDER BY pins NaN to slot 1).
--   2. agent.bump_activation(memory_ids BIGINT[]) — UPSERT
--      ghost_telemetry to increment activation_count + refresh
--      last_activated_at. Called by the MCP tool after a search returns
--      a row (= proxy "AI considered this memory" signal). Returns the
--      actual number of rows upserted (NOT input array length).
--
-- Both functions are SECURITY DEFINER and pin `SET search_path = pg_catalog,
-- agent` (= CVE-2018-1058 mitigation; without this an attacker with CREATE
-- on any schema can shadow unqualified built-ins like exp/unnest/NOW and
-- execute as the function owner). agent_read_mcp can EXECUTE them without
-- holding direct SELECT on agent.ghost_memories or UPDATE on
-- ghost_telemetry. The function bodies do their own filtering
-- (= scope = shared OR allowlisted restricted, deleted_at IS NULL),
-- preserving the boundary the views were guarding.
--
-- Rollback: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/020_ghost_hybrid_ranking_down.sql
-- ⚠️ down requires coordinated server.py revert (= server.py:790 unconditionally
-- calls agent.search_ghost_ranked; dropping the function without reverting
-- server.py breaks every ghost_search_memory call).

BEGIN;

-- DROP first (= signature change between iterations + cleanest re-create
-- semantics for SECURITY DEFINER changes).
DROP FUNCTION IF EXISTS agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT);
DROP FUNCTION IF EXISTS agent.bump_activation(BIGINT[]);

-- ---------------------------------------------------------------------------
-- (1) ranking function
-- ---------------------------------------------------------------------------
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
-- pg_catalog FIRST (= CVE-2018-1058 mitigation: attacker cannot shadow
-- built-ins like NOW/EXTRACT/exp/unnest); agent for ghost_* tables;
-- public for pgvector operators (<=>) which default-install into public.
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
            -- GREATEST(.., 0) clamps negative semantic_sim (= when vectors point
            -- opposite directions) so rank_score breakdown stays consistent
            -- with the math the caller displays, AND it guards against NaN
            -- (= zero-vec stored dense → NaN cosine_distance → NaN rank →
            -- ORDER BY pins NaN to slot #1 in PG float ordering).
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
    -- last_dubbed_at as tiebreaker matches pre-020 server.py behavior
    -- (= most-recently re-dubbed memory surfaces first on rank_score ties).
    ORDER BY rank_score DESC, s.last_dubbed_at DESC
    LIMIT GREATEST(1, LEAST(p_n_results, 50));
$$;

COMMENT ON FUNCTION agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT) IS
    'Hybrid FTS + vector ranking for ghost_memories. SECURITY DEFINER (search_path pinned) lets agent_read_mcp rank without raw dense column access. rank_score = base_score * recency_factor + GREATEST(semantic_sim, 0) * 0.5; semantic_sim = 1 - cosine_distance when query_vec NOT NULL else 0 (clamped at 0).';

-- ---------------------------------------------------------------------------
-- (2) activation auto-bump
-- ---------------------------------------------------------------------------
-- Filters: deleted_at IS NULL + scope check (defense-in-depth — caller may
-- pass arbitrary BIGINT[] including soft-deleted or out-of-scope ids).
-- Returns the actual count of upserts (NOT input array length).
-- unnest aliased explicitly as u(mid) so the EXISTS guard works correctly
-- (= bare `id` would resolve to the inner m.id column → no-op subquery).
CREATE FUNCTION agent.bump_activation(p_memory_ids BIGINT[])
RETURNS INT
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, agent, public
AS $$
    WITH upserted AS (
        INSERT INTO agent.ghost_telemetry (memory_pk, activation_count, last_activated_at, updated_at)
        SELECT DISTINCT u.mid, 1, NOW(), NOW()
        FROM unnest(p_memory_ids) AS u(mid)
        WHERE EXISTS (
            SELECT 1
            FROM agent.ghost_memories m
            WHERE m.id = u.mid
              AND m.deleted_at IS NULL
              AND m.scope IN ('shared', 'shared-restricted')
        )
        ON CONFLICT (memory_pk) DO UPDATE
        SET activation_count  = agent.ghost_telemetry.activation_count + 1,
            last_activated_at = NOW(),
            updated_at        = NOW()
        RETURNING 1
    )
    SELECT COUNT(*)::INT FROM upserted;
$$;

COMMENT ON FUNCTION agent.bump_activation(BIGINT[]) IS
    'Increment activation_count + refresh last_activated_at on the given memory ids. Filters: deleted_at IS NULL + scope in (shared, shared-restricted). DEDUPs input via SELECT DISTINCT so duplicate ids do not raise cardinality_violation. Returns actual upsert count (NOT input length). SECURITY DEFINER with pinned search_path so agent_read_mcp can call safely.';

-- ---------------------------------------------------------------------------
-- (3) grants
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER functions run as their owner regardless of caller. We
-- only need to grant EXECUTE to agent_read_mcp (= the MCP search tool);
-- the owner already has full access to the underlying tables.
GRANT EXECUTE ON FUNCTION agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT) TO agent_read_mcp;
GRANT EXECUTE ON FUNCTION agent.bump_activation(BIGINT[]) TO agent_read_mcp;

COMMIT;
