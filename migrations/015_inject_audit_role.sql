-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/015_inject_audit_role.sql
--    Do NOT use `-1` / `--single-transaction`.
-- Requires: PostgreSQL >= 11 (= partition feature); migrations 013 + 014 applied.
--          ghost layer migration 009 (= agent_read_mcp role) must exist.
--
-- Phase 4/6: inject audit + role separation.
--
-- Mirrors the ghost layer's role+audit asymmetry (= ghost_read_log + 4-role
-- separation in migration 009) onto the personal corpus side:
--   1. personal.conversation_read_log — partitioned by month, session_id NOT NULL
--   2. personal.v_conversations_inject_safe — view exposing only redacted
--      brief_120char (= NOT raw summary_text). Excludes sentinel rows.
--   3. agent_read_mcp role:
--        REVOKE direct SELECT on personal.conversations (= no raw summary_text)
--        GRANT SELECT on v_conversations_inject_safe
--        GRANT INSERT on conversation_read_log
--   4. canonical_project_slug() REVOKE EXECUTE FROM PUBLIC + targeted GRANT
--      (= Phase 1 round 3 r3-security-2 close-out)
--
-- Scope OUT:
--   Phase 5 (#18): allowlist + purge + slug_history
--   Phase 6 (#19): the hook itself (= consumes v_conversations_inject_safe)
--
-- Rollback: psql -f migrations/015_inject_audit_role_down.sql
-- Dependencies: 009 (= agent_read_mcp role), 013 + 014 (= column + sentinels)

SET lock_timeout = '5s';

-- (1) Audit log table — partitioned by month, ghost_read_log と対称.
-- session_id NOT NULL (= ghost layer's known weakness 'session_id can be NULL'
-- is intentionally rejected here per round 3 r3-security-14).
CREATE TABLE IF NOT EXISTS personal.conversation_read_log (
    id              BIGSERIAL,
    session_id      TEXT        NOT NULL,
    current_project TEXT,
    chassis_id      TEXT,
    project_slug    TEXT,
    retrieved_n     INT         NOT NULL DEFAULT 0,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ts, id)
) PARTITION BY RANGE (ts);

-- Monthly partitions: 2026-05 through 2027-12 (= covers 1.5 years).
-- Operator can ADD PARTITION before the range runs out; default catches stragglers.
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_05 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_06 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_07 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_08 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_09 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_10 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_11 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2026_12 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2027_q1 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2027-01-01') TO ('2027-04-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2027_q2 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2027-04-01') TO ('2027-07-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2027_q3 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2027-07-01') TO ('2027-10-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_2027_q4 PARTITION OF personal.conversation_read_log
    FOR VALUES FROM ('2027-10-01') TO ('2028-01-01');
CREATE TABLE IF NOT EXISTS personal.conversation_read_log_default PARTITION OF personal.conversation_read_log
    DEFAULT;

CREATE INDEX IF NOT EXISTS idx_conv_read_log_session
    ON personal.conversation_read_log (session_id);
CREATE INDEX IF NOT EXISTS idx_conv_read_log_project
    ON personal.conversation_read_log (current_project, ts DESC);

-- (2) inject-safe view: redacted brief, sentinel exclusion enforced.
-- brief_120char regexes token-like substrings to [REDACTED] (= rough heuristic;
-- Phase 5 may refine). NEVER exposes raw summary_text or title beyond 120 char.
CREATE OR REPLACE VIEW personal.v_conversations_inject_safe AS
SELECT
    conv_id,
    project_slug,
    started_at,
    title,
    regexp_replace(
        substring(summary_text from 1 for 120),
        '[A-Za-z0-9_+/=]{30,}',
        '[REDACTED]',
        'g'
    ) AS brief_120char,
    dominant_topic
FROM personal.conversations
WHERE project_slug IS NOT NULL
  AND project_slug NOT IN ('__no_project__', '__unresolved__', '__excluded__')
  AND summary_text IS NOT NULL
  AND started_at IS NOT NULL;

-- (3) Role permission adjustments for agent_read_mcp.
-- Phase 1 left direct grants intact; Phase 4 tightens.
-- agent_read_mcp can SELECT only via the view (= no raw summary_text access)
-- and MUST log every read via INSERT into conversation_read_log.

-- Revoke direct SELECT (= migration 009 may or may not have granted broadly;
-- this is idempotent — REVOKE on absent grant is a no-op).
REVOKE ALL ON personal.conversations FROM agent_read_mcp;

-- Grant view + audit insert.
GRANT USAGE ON SCHEMA personal TO agent_read_mcp;
GRANT SELECT ON personal.v_conversations_inject_safe TO agent_read_mcp;
GRANT INSERT ON personal.conversation_read_log TO agent_read_mcp;
GRANT USAGE, SELECT ON SEQUENCE personal.conversation_read_log_id_seq TO agent_read_mcp;

-- (4) canonical_project_slug() — restrict EXECUTE (= Phase 1 round 3 r3-security-2).
-- Default CREATE FUNCTION grants EXECUTE to PUBLIC. Tighten:
REVOKE EXECUTE ON FUNCTION personal.canonical_project_slug(TEXT, TEXT) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION personal.canonical_project_slug(TEXT, TEXT) TO agent_read_mcp;
GRANT  EXECUTE ON FUNCTION personal.canonical_project_slug(TEXT, TEXT) TO agent_dub;
-- ingest scripts run as PG_URL owner; ownership keeps EXECUTE implicitly.

RESET lock_timeout;
