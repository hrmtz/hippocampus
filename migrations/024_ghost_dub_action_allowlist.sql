-- 024_ghost_dub_action_allowlist.sql
-- Add skipped_not_allowlisted to ghost_dub_action ENUM.
-- ALTER TYPE ADD VALUE cannot run inside a transaction (no_tx: true).
ALTER TYPE agent.dub_action ADD VALUE IF NOT EXISTS 'skipped_not_allowlisted';
