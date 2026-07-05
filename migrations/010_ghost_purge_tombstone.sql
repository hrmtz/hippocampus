-- migrations/010_ghost_purge_tombstone.sql
--
-- ⚠️ round 3 codex-r3-2 + plan-pipeline-1 + plan-exec-1 反映:
--   _ghost_common.py の add_to_purge_tombstone() / is_in_tombstone() が
--   参照する table。 design §16 emergency purge と §5.1 dub script の
--   `skipped_purged` action の load-bearing storage。
--
-- 設計 doc 上は migration 009 内で 言及されているが、 実 SQL artifact が
-- 抜けていた。 ここで補完。
--
-- 同 (source_project, slug) tuple 単位で記録 (= codex-r3-2: blast radius
-- 制御。 slug 単独だと cross-project 巻添え発生)。

BEGIN;

DO $$
BEGIN
  IF to_regclass('agent.ghost_purge_tombstone') IS NOT NULL THEN
    RAISE NOTICE 'migration 010 already applied, IF NOT EXISTS で safe';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent.ghost_purge_tombstone (
    source_project  TEXT NOT NULL,
    memory_slug     TEXT NOT NULL,
    reason          TEXT NOT NULL,
        -- ⚠️ reason は §16.0 で credential sanitize 通過後のみ (= scan_blocked
        -- hit 時は '[credential-shaped reason redacted]' 固定値)
    purged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    purged_by       TEXT,
        -- 操作 user / role (= forensic、 NULL 許容で後付け可能)
    PRIMARY KEY (source_project, memory_slug)
);

CREATE INDEX IF NOT EXISTS idx_tombstone_purged_at
    ON agent.ghost_purge_tombstone(purged_at DESC);

-- GRANT (= agent_dub は SELECT のみ、 INSERT は agent_purge_admin 経由のみ)
GRANT SELECT ON agent.ghost_purge_tombstone TO agent_dub;
GRANT SELECT, INSERT ON agent.ghost_purge_tombstone TO agent_purge_admin;
-- ⚠️ agent_dub に INSERT 与えない (= dub script は tombstone を読むのみ、
-- 書き込みは purge 経路だけ。 round 3 codex-r3-5 audit integrity 原則)

-- ⚠️ UPDATE/DELETE は誰にも GRANT しない (= 削除は手動 superuser のみ、
-- tombstone は append-only forensic record)

COMMIT;
