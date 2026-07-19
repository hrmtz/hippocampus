-- APPLY: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/020_ghost_hybrid_ranking_down.sql
--
-- Drop the SECURITY DEFINER ranking + activation functions.
--
-- ⚠️ COORDINATED REVERT REQUIRED:
-- server.py search_ghost_memory unconditionally calls
-- agent.search_ghost_ranked (= no inline-SQL fallback). Dropping the
-- function without also reverting server.py to its pre-020 form breaks
-- every search_ghost_memory invocation with `function ... does not exist`.
--
-- Recovery procedure:
--   1. git revert <020 commit> (= reverts server.py too)
--   2. psql -f migrations/020_ghost_hybrid_ranking_down.sql
--   3. restart MCP server processes
-- Or, if reverting server is not desired:
--   1. Manually restore the inline SQL in server.py:search_ghost_memory
--      to query agent.ghost_unified_no_vector directly
--   2. Apply this down migration

BEGIN;

REVOKE EXECUTE ON FUNCTION agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT) FROM agent_read_mcp;
REVOKE EXECUTE ON FUNCTION agent.bump_activation(BIGINT[]) FROM agent_read_mcp;

DROP FUNCTION IF EXISTS agent.search_ghost_ranked(TEXT, vector, TEXT, BOOLEAN, INT);
DROP FUNCTION IF EXISTS agent.bump_activation(BIGINT[]);

COMMIT;
