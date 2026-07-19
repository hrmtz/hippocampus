-- DOWN: psql -v ON_ERROR_STOP=1 "$PG_URL" -f migrations/027_wiki_down.sql
-- Reverses 027_wiki.sql in FK-safe order.
--
-- ⚠️ This DROPs personal.wiki_pages.body_md — the durable SoT of the wiki
--    layer. That is IRREVERSIBLE data loss (the body cannot be re-derived once
--    the source transcripts age out). Take a sanada backup / pg_dump of
--    personal.wiki_* before running:
--      pg_dump "$PG_URL" -n personal -t 'personal.wiki_*' \
--        > /path/to/backups/wiki_predown_$(date +%Y%m%d_%H%M%S).sql
--
-- NOT listed in migrations/manifest.yaml (the runner's parse_manifest rejects
-- *_down.sql); invoked manually only. All drops are additive-reverse — no
-- data-undo logic beyond the table drops.

-- (1) view first (depends on wiki_pages)
DROP VIEW IF EXISTS personal.v_wiki_inject_safe;

-- (2) staging (independent table)
DROP TABLE IF EXISTS personal.wiki_merge_staging;

-- (3) claims (FK child of wiki_pages — drop before pages)
DROP TABLE IF EXISTS personal.wiki_claims;

-- (4) merge_log (independent table)
DROP TABLE IF EXISTS personal.wiki_merge_log;

-- (5) pages (parent — drop last)
DROP TABLE IF EXISTS personal.wiki_pages;

-- (6) feature flag row
DELETE FROM personal.feature_flags WHERE flag_name = 'wiki_layer';

-- (7) role teardown: DROP OWNED clears all GRANTs (else DROP ROLE fails on
--     dependent privileges), then drop the role. Guarded so a re-run (or a
--     down on a DB where the role was never created) is a no-op.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_wiki_writer') THEN
    DROP OWNED BY agent_wiki_writer;
    DROP ROLE agent_wiki_writer;
  END IF;
END $$;
