-- DOWN: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/025_memory_link_graph_down.sql
-- Reverses 025_memory_link_graph.sql. All additive → clean drop, no data undo.
DROP FUNCTION IF EXISTS agent.expand_ghost_neighbors(BIGINT[], TEXT, BOOLEAN, INT);
DROP FUNCTION IF EXISTS agent.ghost_is_visible(BIGINT, TEXT, BOOLEAN);
DROP TABLE IF EXISTS agent.memory_edges;
DROP INDEX IF EXISTS agent.ghost_memories_resolve;
ALTER TABLE agent.ghost_memories DROP COLUMN IF EXISTS source_stem;
