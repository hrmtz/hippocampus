-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/025_memory_link_graph.sql
-- Requires: migration 009 (roles + agent.ghost_memories) + 020/021 (ranking fn).
-- no_tx=false (no CONCURRENTLY); runner wraps in a single transaction.
--
-- Memory link-graph layer (design: docs/designs/MEMORY_LINK_GRAPH.md, ultramagi).
-- Adds:
--   1. agent.ghost_memories.source_stem  — generated filestem key (links resolve
--      by FILESTEM, not the name: slug; measured 79% vs 42% over 540 real files).
--   2. agent.memory_edges                — [[wikilink]] edges between memories.
--   3. agent.ghost_is_visible()          — shared visibility predicate helper.
--   4. agent.expand_ghost_neighbors()    — privacy-safe 1-hop spreading activation.
--
-- Privacy: neighbor expansion re-applies the EXACT ghost visibility rule via the
-- helper; agent_read_mcp gets EXECUTE only (NO raw table SELECT — mirrors 009).

-- ---------------------------------------------------------------------------
-- 1. Resolvable filestem key (generated STORED → backfills existing rows).
--    NOTE: ADD COLUMN ... GENERATED ... STORED takes ACCESS EXCLUSIVE + rewrites
--    the table. At ~505 rows this is sub-second but NOT lock-free — apply off-dub.
-- ---------------------------------------------------------------------------
ALTER TABLE agent.ghost_memories
  ADD COLUMN IF NOT EXISTS source_stem TEXT
  GENERATED ALWAYS AS (
    regexp_replace(regexp_replace(source_file, '^.*/', ''), '\.md$', '')
  ) STORED;

CREATE INDEX IF NOT EXISTS ghost_memories_resolve
  ON agent.ghost_memories (chassis_id, source_project, lower(source_stem))
  WHERE deleted_at IS NULL AND embed_model = 'bge-m3';

-- ---------------------------------------------------------------------------
-- 2. Edge table. agent_read_mcp gets NO table SELECT (least-privilege, 009).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent.memory_edges (
    id                BIGSERIAL PRIMARY KEY,
    source_memory_pk  BIGINT NOT NULL
                      REFERENCES agent.ghost_memories(id) ON DELETE CASCADE,
    source_chassis_id agent.chassis_id NOT NULL,  -- copied from parent (resolve scope)
    source_project    TEXT   NOT NULL,            -- copied from parent (resolve scope)
    target_slug       TEXT   NOT NULL,            -- normalized [[filestem]] target
    target_memory_pk  BIGINT                      -- NULL = unresolved/dangling
                      REFERENCES agent.ghost_memories(id) ON DELETE SET NULL,
    link_text         TEXT,                        -- alias if [[slug|alias]]
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_memory_pk, target_slug)
);
CREATE INDEX IF NOT EXISTS memory_edges_source    ON agent.memory_edges(source_memory_pk);
CREATE INDEX IF NOT EXISTS memory_edges_target_pk ON agent.memory_edges(target_memory_pk);
CREATE INDEX IF NOT EXISTS memory_edges_resolve
  ON agent.memory_edges (source_chassis_id, source_project, lower(target_slug))
  WHERE target_memory_pk IS NULL;   -- back-resolution of danglers

REVOKE ALL ON agent.memory_edges FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.memory_edges TO agent_dub;
GRANT USAGE, SELECT ON SEQUENCE agent.memory_edges_id_seq TO agent_dub;

-- ---------------------------------------------------------------------------
-- 3. Shared visibility helper (single source of the predicate for NEW code).
--    Mirrors agent.search_ghost_ranked's scope clause (021:87-99). pg_catalog
--    FIRST (CVE-2018-1058). A subset regression test asserts non-divergence.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION agent.ghost_is_visible(
    p_memory_pk BIGINT, p_current_project TEXT, p_include_restricted BOOLEAN
) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, agent, public AS $$
    SELECT EXISTS (
        SELECT 1 FROM agent.ghost_memories m
        WHERE m.id = p_memory_pk
          AND m.deleted_at IS NULL
          AND ( m.scope = 'shared'
                OR ( p_include_restricted AND m.scope = 'shared-restricted'
                     AND m.id IN (SELECT memory_pk FROM agent.ghost_restricted_allowlist
                                  WHERE allowed_project = p_current_project) ) )
    );
$$;

-- ---------------------------------------------------------------------------
-- 4. Neighbor expansion (1-hop spreading activation, privacy-safe).
--    Source-side liveness join (codex-r3-1) + target visibility via helper.
--    p_source_ids is passed in RANK order; array_position ranks neighbors by
--    their best source hit (codex-8). memory_type/scope as TEXT (matches 021).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION agent.expand_ghost_neighbors(
    p_source_ids BIGINT[], p_current_project TEXT,
    p_include_restricted BOOLEAN DEFAULT FALSE, p_max INT DEFAULT 5
) RETURNS TABLE (
    id BIGINT, source_project TEXT, memory_slug TEXT, memory_type TEXT,
    title TEXT, body TEXT, scope TEXT, via_source_pk BIGINT
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, agent, public AS $$
    WITH best AS (
        SELECT DISTINCT ON (m.id)
            m.id, m.source_project, m.memory_slug, m.memory_type::text AS memory_type,
            m.title, m.body, m.scope::text AS scope,
            e.source_memory_pk AS via_source_pk,
            array_position(p_source_ids, e.source_memory_pk) AS src_rank
        FROM agent.memory_edges e
        JOIN agent.ghost_memories m ON m.id = e.target_memory_pk
        -- source-side liveness (codex-r3-1): a stale/deleted/non-bge-m3 source
        -- id passed by any caller yields nothing.
        JOIN agent.ghost_memories s ON s.id = e.source_memory_pk
             AND s.deleted_at IS NULL AND s.embed_model = 'bge-m3'
        WHERE e.source_memory_pk = ANY(p_source_ids)
          AND COALESCE(array_length(p_source_ids, 1), 0) > 0
          AND e.target_memory_pk IS NOT NULL
          AND m.embed_model = 'bge-m3'
          AND m.id <> ALL(p_source_ids)
          AND agent.ghost_is_visible(m.id, p_current_project, p_include_restricted)
        ORDER BY m.id, array_position(p_source_ids, e.source_memory_pk)
    )
    SELECT id, source_project, memory_slug, memory_type, title, body, scope, via_source_pk
    FROM best
    ORDER BY src_rank, id
    LIMIT GREATEST(0, LEAST(p_max, 10));
$$;

GRANT EXECUTE ON FUNCTION agent.ghost_is_visible(BIGINT, TEXT, BOOLEAN) TO agent_read_mcp;
GRANT EXECUTE ON FUNCTION agent.expand_ghost_neighbors(BIGINT[], TEXT, BOOLEAN, INT) TO agent_read_mcp;

COMMENT ON TABLE agent.memory_edges IS
  'Wikilink edges between ghost memories. Built by hippocampus sync-edges from '
  'ghost_memories.body (decoupled from dub). target resolved by filestem within '
  '(chassis_id, source_project), bge-m3 only. See docs/designs/MEMORY_LINK_GRAPH.md.';
