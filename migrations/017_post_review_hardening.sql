-- ⚠️ APPLY: psql -v ON_ERROR_STOP=1 -f migrations/017_post_review_hardening.sql
-- Requires: migrations 013-016 applied.
--
-- Phase 6.5: post-ultrareview hardening (= 3 cluster of findings).
--
-- (A) bug_009: agent_read_mcp lacked SELECT on feature_flags + allowlist,
--     making Phase 6 recent_topics_inject.py fail-closed in production
--     (= test was done as owner role, masked the issue).
-- (B) bug_015: purge_project() is SECURITY DEFINER but agent_purge_admin also
--     had direct DELETE grants → could bypass audit. Revoke direct grants;
--     function still works on its creator's privileges.
-- (C) bug_018: redaction regex {30,} missed 16-29 char tokens (AWS keys 20 char,
--     GitHub tokens, JWT segments) and excluded '-' char. Tighten to {20,} with
--     wider charset.

SET lock_timeout = '5s';

-- (A) Phase 6 hook needs to read these tables to evaluate the triple-gate.
GRANT SELECT ON personal.feature_flags                 TO agent_read_mcp;
GRANT SELECT ON personal.conversation_inject_allowlist TO agent_read_mcp;
GRANT SELECT ON personal.conversation_inject_excluded_paths TO agent_read_mcp;

-- (B) Force all destructive ops through purge_project() audit path.
-- SECURITY DEFINER means the function runs with the function-owner's privileges,
-- not the caller's — so agent_purge_admin doesn't need direct DELETE grants.
REVOKE DELETE ON personal.conversations       FROM agent_purge_admin;
REVOKE DELETE ON personal.messages            FROM agent_purge_admin;
REVOKE DELETE ON personal.conversation_read_log FROM agent_purge_admin;
REVOKE DELETE ON agent.ghost_memories         FROM agent_purge_admin;
-- Keep SELECT (= for forensics + dry-run-like previews).

-- (C) Tighter brief redaction.
-- Threshold dropped 30→20 (= catches AWS 20-char access keys, GitHub PATs).
-- Charset adds '-' '.' '@' (= UUIDs, hostnames, email-shaped credentials).
-- '$' added for shell-var-like patterns. Both bounds anchored.
-- Note: this lowers the false-negative rate at modest cost of brief readability.
CREATE OR REPLACE VIEW personal.v_conversations_inject_safe AS
SELECT
    conv_id,
    project_slug,
    started_at,
    title,
    regexp_replace(
        substring(summary_text from 1 for 120),
        '[A-Za-z0-9._+/=@$-]{20,}',
        '[REDACTED]',
        'g'
    ) AS brief_120char,
    dominant_topic
FROM personal.conversations
WHERE project_slug IS NOT NULL
  AND project_slug NOT IN ('__no_project__', '__unresolved__', '__excluded__')
  AND summary_text IS NOT NULL
  AND started_at IS NOT NULL;

GRANT SELECT ON personal.v_conversations_inject_safe TO agent_read_mcp;

RESET lock_timeout;
