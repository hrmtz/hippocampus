-- migrations/009_agent_ghost_layer.sql
-- ⚠️ 全体を transaction で wrap (= half-applied state 回避)
-- ⚠️ 冪等性は全 CREATE 文の IF NOT EXISTS で実現 (= round 2 r2-schema-1 反映、
--    DO block の RETURN は block 内 exit のみで migration 全体 abort できないため)
BEGIN;

-- pre-flight: 既適用なら advisory notice (= 実 protect は CREATE ... IF NOT EXISTS)
DO $$
BEGIN
  IF to_regclass('agent.ghost_memories') IS NOT NULL THEN
    RAISE NOTICE 'migration 009 partial or full apply detected, '
                 'all CREATE statements are IF NOT EXISTS, safe to re-run';
  END IF;
END $$;

-- 新 schema: agent (= 君の library = personal.* と完全分離)
CREATE SCHEMA IF NOT EXISTS agent;

-- ENUM types (= 値ドメイン enforce、 typo 防止、 wire-level type safety)
-- ⚠️ PG は CREATE TYPE IF NOT EXISTS 非対応、 DO block で gate

DO $$ BEGIN
  CREATE TYPE agent.memory_scope AS ENUM (
    'local',                  -- default、 dub されない
    'shared',                 -- 全 project / 全 chassis に dub
    'shared-restricted'       -- 明示 allowlist project にのみ dub (= pentest 等)
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agent.memory_type AS ENUM (
    'user', 'feedback', 'project', 'reference'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agent.dub_action AS ENUM (
    'inserted', 'updated', 'unchanged',
    'skipped_no_scope', 'skipped_not_memory', 'skipped_restricted',
    'skipped_active_write', 'skipped_mtime_changed', 'skipped_budget_exceeded',
    'skipped_unknown_chassis', 'skipped_purged',
    'parse_error', 'embed_failed', 'rejected_content_scan'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agent.dub_run_status AS ENUM (
    'running', 'ok', 'partial', 'failed'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- chassis allow-list (= 値 enum、 normalization 強制)
-- ⚠️ round 2 r2-privacy-4 反映: cursor / copilot は OUT OF SCOPE (§9)、
--    adapter 実装時に ALTER TYPE ADD VALUE で別 migration で追加。
DO $$ BEGIN
  CREATE TYPE agent.chassis_id AS ENUM (
    'claude-code'              -- Phase 0 で唯一サポート
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ===========================================================================
-- 主 table: ghost_memories (= immutable-ish canonical artifact)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_memories (
    id              BIGSERIAL PRIMARY KEY,

    chassis_id      agent.chassis_id NOT NULL,
    source_project  TEXT NOT NULL,
    memory_slug     TEXT NOT NULL,

    embed_model     TEXT NOT NULL DEFAULT 'bge-m3',
    embed_dim       INT NOT NULL DEFAULT 1024,

    memory_type     agent.memory_type NOT NULL,
    title           TEXT,
    body            TEXT NOT NULL,
    body_hash       TEXT NOT NULL
                    CHECK (body_hash ~ '^sha256:[0-9a-f]{64}$'),
    scope           agent.memory_scope NOT NULL DEFAULT 'local',

    source_file     TEXT NOT NULL,
    source_mtime    TIMESTAMPTZ NOT NULL,
    source_host     TEXT NOT NULL,

    dense           vector
                    CHECK (dense IS NULL OR vector_dims(dense) = embed_dim),

    fts             TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('simple', coalesce(body, ''))) STORED,

    first_dubbed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_dubbed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ,

    UNIQUE (chassis_id, source_project, memory_slug, embed_model)
);

CREATE INDEX IF NOT EXISTS idx_ghost_source_project ON agent.ghost_memories(source_project);
CREATE INDEX IF NOT EXISTS idx_ghost_memory_type    ON agent.ghost_memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_ghost_chassis        ON agent.ghost_memories(chassis_id);
CREATE INDEX IF NOT EXISTS idx_ghost_scope          ON agent.ghost_memories(scope);
CREATE INDEX IF NOT EXISTS idx_ghost_active         ON agent.ghost_memories(id)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ghost_fts ON agent.ghost_memories USING GIN(fts);

-- HNSW は 009b で別 migration

-- ===========================================================================
-- telemetry table (= mutable、 HNSW churn 隔離)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_telemetry (
    memory_pk                BIGINT PRIMARY KEY
                             REFERENCES agent.ghost_memories(id)
                             ON DELETE CASCADE,
    activation_count         INT NOT NULL DEFAULT 0,
    incident_prevention      INT NOT NULL DEFAULT 0,
    user_endorsement         INT NOT NULL DEFAULT 0,
    user_correction          INT NOT NULL DEFAULT 0,
    prediction_error_ewma    REAL NOT NULL DEFAULT 0.0,
    last_activated_at        TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_activation
    ON agent.ghost_telemetry(activation_count DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_last_activated
    ON agent.ghost_telemetry(last_activated_at DESC);

-- ===========================================================================
-- evidence audit (= personal → agent 教師信号 provenance)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_evidence (
    id              BIGSERIAL PRIMARY KEY,
    memory_pk       BIGINT NOT NULL REFERENCES agent.ghost_memories(id) ON DELETE CASCADE,
    conv_id         TEXT,
    msg_id          TEXT,
    signal_kind     TEXT NOT NULL CHECK (signal_kind IN ('endorsement', 'correction', 'activation')),
    signal_source   TEXT NOT NULL CHECK (signal_source IN ('explicit_command', 'mining_unused')),
    note            TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_memory ON agent.ghost_evidence(memory_pk);
CREATE INDEX IF NOT EXISTS idx_evidence_conv ON agent.ghost_evidence(conv_id);

-- ===========================================================================
-- ghost_dub_log (= per-file audit、 monthly partition)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_dub_log (
    id              BIGSERIAL,
    run_id          UUID NOT NULL,
    chassis_id      agent.chassis_id NOT NULL,
    source_project  TEXT NOT NULL,
    memory_slug     TEXT NOT NULL,
    action          agent.dub_action NOT NULL,
    body_hash_old   TEXT,
    body_hash_new   TEXT,
    error_message   TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS agent.ghost_dub_log_y2026m05 PARTITION OF agent.ghost_dub_log
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE IF NOT EXISTS agent.ghost_dub_log_y2026m06 PARTITION OF agent.ghost_dub_log
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

CREATE INDEX IF NOT EXISTS idx_dub_log_run ON agent.ghost_dub_log(run_id);
CREATE INDEX IF NOT EXISTS idx_dub_log_action ON agent.ghost_dub_log(action)
    WHERE action != 'unchanged';

-- ===========================================================================
-- ghost_dub_run (= per-run 集計)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_dub_run (
    run_id          UUID PRIMARY KEY,
    host            TEXT NOT NULL,
    chassis_id      agent.chassis_id NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          agent.dub_run_status NOT NULL DEFAULT 'running',
    n_inserted      INT NOT NULL DEFAULT 0,
    n_updated       INT NOT NULL DEFAULT 0,
    n_unchanged     INT NOT NULL DEFAULT 0,
    n_skipped       INT NOT NULL DEFAULT 0,
    n_errored       INT NOT NULL DEFAULT 0,
    embed_calls     INT NOT NULL DEFAULT 0,
    embed_seconds   REAL NOT NULL DEFAULT 0.0,
    error_summary   TEXT
);
CREATE INDEX IF NOT EXISTS idx_dub_run_started ON agent.ghost_dub_run(started_at DESC);

CREATE OR REPLACE VIEW agent.ghost_dub_heartbeat AS
SELECT
    chassis_id,
    host,
    MAX(finished_at) FILTER (WHERE status = 'ok') AS last_successful_run,
    NOW() - MAX(finished_at) FILTER (WHERE status = 'ok') AS staleness
FROM agent.ghost_dub_run
GROUP BY chassis_id, host;

-- ===========================================================================
-- ghost_read_log (= MCP search access audit)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_read_log (
    id              BIGSERIAL,
    session_id      TEXT,
    current_project TEXT,
    chassis_id      agent.chassis_id,
    query_kind      TEXT NOT NULL,
    query_text      TEXT,
    returned_ids    BIGINT[],
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS agent.ghost_read_log_y2026m05 PARTITION OF agent.ghost_read_log
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE IF NOT EXISTS agent.ghost_read_log_y2026m06 PARTITION OF agent.ghost_read_log
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

-- DEFAULT partition (= round 3 codex-r3-4: rotation 漏れ fail-safe)
CREATE TABLE IF NOT EXISTS agent.ghost_read_log_default PARTITION OF agent.ghost_read_log
    DEFAULT;
CREATE TABLE IF NOT EXISTS agent.ghost_dub_log_default PARTITION OF agent.ghost_dub_log
    DEFAULT;

CREATE INDEX IF NOT EXISTS idx_read_log_project ON agent.ghost_read_log(current_project, ts);

-- ===========================================================================
-- shared-restricted access control
-- (= round 3 codex-r3-1: PK is memory_pk to avoid cross-project confused deputy)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent.ghost_restricted_allowlist (
    memory_pk       BIGINT NOT NULL
                    REFERENCES agent.ghost_memories(id) ON DELETE CASCADE,
    allowed_project TEXT NOT NULL,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (memory_pk, allowed_project)
);

CREATE INDEX IF NOT EXISTS idx_restricted_project
    ON agent.ghost_restricted_allowlist(allowed_project);

-- ===========================================================================
-- 統合 read view (= round 2 r2-schema-7: dense exclude version 分離)
-- ===========================================================================

CREATE OR REPLACE VIEW agent.ghost_unified AS
SELECT
    m.id,
    m.chassis_id, m.source_project, m.memory_slug,
    m.memory_type, m.title, m.body, m.scope,
    m.embed_model, m.embed_dim, m.dense, m.fts,
    m.first_dubbed_at, m.last_dubbed_at,
    COALESCE(t.activation_count, 0) AS activation_count,
    COALESCE(t.incident_prevention, 0) AS incident_prevention,
    COALESCE(t.user_endorsement, 0) AS user_endorsement,
    COALESCE(t.user_correction, 0) AS user_correction,
    COALESCE(t.prediction_error_ewma, 0.0) AS prediction_error_ewma,
    t.last_activated_at
FROM agent.ghost_memories m
LEFT JOIN agent.ghost_telemetry t ON t.memory_pk = m.id
WHERE m.deleted_at IS NULL;

CREATE OR REPLACE VIEW agent.ghost_unified_no_vector AS
SELECT
    id, chassis_id, source_project, memory_slug,
    memory_type, title, body, scope,
    embed_model, embed_dim, fts,
    first_dubbed_at, last_dubbed_at,
    activation_count, incident_prevention, user_endorsement,
    user_correction, prediction_error_ewma, last_activated_at
FROM agent.ghost_unified;

-- ===========================================================================
-- PG role 分離 (= round 3 codex-r3-5: least privilege)
-- ===========================================================================

-- agent_dub: nightly cron
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_dub') THEN
    CREATE ROLE agent_dub LOGIN PASSWORD NULL;
  END IF;
END $$;

GRANT USAGE ON SCHEMA agent TO agent_dub;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA agent TO agent_dub;
GRANT SELECT, INSERT, UPDATE ON agent.ghost_memories TO agent_dub;
GRANT SELECT, INSERT, UPDATE ON agent.ghost_telemetry TO agent_dub;
GRANT SELECT, INSERT ON agent.ghost_evidence TO agent_dub;
GRANT SELECT, INSERT ON agent.ghost_dub_log TO agent_dub;
GRANT SELECT, INSERT, UPDATE ON agent.ghost_dub_run TO agent_dub;
GRANT SELECT ON agent.ghost_restricted_allowlist TO agent_dub;

REVOKE UPDATE, DELETE ON agent.ghost_dub_log FROM agent_dub;
REVOKE INSERT, UPDATE, DELETE ON agent.ghost_read_log FROM agent_dub;
REVOKE INSERT, UPDATE, DELETE ON agent.ghost_restricted_allowlist FROM agent_dub;

GRANT USAGE ON SCHEMA personal TO agent_dub;
GRANT SELECT ON ALL TABLES IN SCHEMA personal TO agent_dub;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON ALL TABLES IN SCHEMA personal FROM agent_dub;

-- agent_acl_admin: 人手 allowlist 編集
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_acl_admin') THEN
    CREATE ROLE agent_acl_admin LOGIN PASSWORD NULL;
  END IF;
END $$;
GRANT USAGE ON SCHEMA agent TO agent_acl_admin;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON agent.ghost_restricted_allowlist TO agent_acl_admin;
GRANT SELECT ON agent.ghost_memories TO agent_acl_admin;

-- agent_purge_admin: SECURITY DEFINER procedure 経由
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_purge_admin') THEN
    CREATE ROLE agent_purge_admin LOGIN PASSWORD NULL;
  END IF;
END $$;
GRANT USAGE ON SCHEMA agent TO agent_purge_admin;

-- agent_read_mcp: MCP server
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_read_mcp') THEN
    CREATE ROLE agent_read_mcp LOGIN PASSWORD NULL;
  END IF;
END $$;

GRANT USAGE ON SCHEMA agent TO agent_read_mcp;
GRANT SELECT ON agent.ghost_unified_no_vector TO agent_read_mcp;
GRANT SELECT ON agent.ghost_restricted_allowlist TO agent_read_mcp;
GRANT INSERT ON agent.ghost_read_log TO agent_read_mcp;
GRANT USAGE, SELECT ON SEQUENCE agent.ghost_read_log_id_seq TO agent_read_mcp;
REVOKE SELECT ON agent.ghost_unified FROM agent_read_mcp;
REVOKE SELECT ON agent.ghost_memories FROM agent_read_mcp;

-- ===========================================================================
-- invariant 検証 (= round 2 r2-schema-6 + r2-privacy-3)
-- ⚠️ PL/pgSQL は explicit SAVEPOINT 不可、 BEGIN/EXCEPTION/END sub-block で
--   implicit savepoint + RAISE EXCEPTION で強制 rollback する pattern
-- ===========================================================================
DO $$
DECLARE
    tbl RECORD;
    test_result TEXT;
    test_passed_count INT := 0;
    test_failed_count INT := 0;
    positive_passed BOOLEAN := FALSE;
BEGIN
    FOR tbl IN
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = 'personal'
    LOOP
        test_result := NULL;
        BEGIN
            -- sub-block 1 (= implicit savepoint、 RAISE で auto rollback)
            SET LOCAL ROLE agent_dub;
            BEGIN
                EXECUTE format(
                    'INSERT INTO %I.%I DEFAULT VALUES',
                    tbl.schemaname, tbl.tablename
                );
                -- INSERT 成功 = invariant 違反、 即 RAISE で sub-block 内 rollback
                test_result := 'failed_priv_exists';
                RAISE EXCEPTION 'force_rollback' USING ERRCODE = 'P0001';
            EXCEPTION
                WHEN insufficient_privilege THEN
                    test_result := 'passed';
                WHEN SQLSTATE 'P0001' THEN
                    -- 自分の RAISE = priv 確認済の rollback
                    NULL;  -- test_result は 'failed_priv_exists' のまま
                WHEN OTHERS THEN
                    -- priv あったが not_null_violation 等で別 error
                    -- = privilege check は failed (= INSERT 構文評価まで到達)
                    test_result := 'failed_priv_exists';
            END;
            RESET ROLE;
        EXCEPTION WHEN OTHERS THEN
            -- 想定外 (= SET LOCAL ROLE 自体失敗 等)
            RESET ROLE;
            RAISE EXCEPTION 'invariant test infrastructure failed on %.%: %',
                            tbl.schemaname, tbl.tablename, SQLERRM;
        END;

        IF test_result = 'passed' THEN
            test_passed_count := test_passed_count + 1;
        ELSE
            test_failed_count := test_failed_count + 1;
            RAISE WARNING 'INVARIANT VIOLATION: agent_dub can write to personal.%',
                          tbl.tablename;
        END IF;
    END LOOP;

    IF test_passed_count = 0 THEN
        RAISE EXCEPTION 'INVARIANT TEST INVALID: 0 personal.* tables tested '
                        '(= test target が存在しない、 false-positive verified の typical pattern)';
    END IF;
    IF test_failed_count > 0 THEN
        RAISE EXCEPTION 'INVARIANT VIOLATION: agent_dub can write to % personal.* tables',
                        test_failed_count;
    END IF;
    RAISE NOTICE 'invariant verified: agent_dub cannot write to % personal.* tables',
                 test_passed_count;

    -- positive test (= agent_dub が agent.ghost_dub_run には書ける確認)
    BEGIN
        SET LOCAL ROLE agent_dub;
        BEGIN
            EXECUTE 'INSERT INTO agent.ghost_dub_run (run_id, host, chassis_id, started_at, status) '
                    'VALUES (gen_random_uuid(), ''_invariant_test'', ''claude-code'', NOW(), ''ok'')';
            positive_passed := TRUE;
            -- 成功 = expected、 即 RAISE で test row を rollback
            RAISE EXCEPTION 'force_rollback' USING ERRCODE = 'P0002';
        EXCEPTION
            WHEN SQLSTATE 'P0002' THEN
                NULL;  -- expected rollback、 positive_passed は TRUE
            WHEN insufficient_privilege THEN
                positive_passed := FALSE;
        END;
        RESET ROLE;
    EXCEPTION WHEN OTHERS THEN
        RESET ROLE;
        RAISE EXCEPTION 'positive test infrastructure failed: %', SQLERRM;
    END;

    IF NOT positive_passed THEN
        RAISE EXCEPTION 'POSITIVE TEST FAILED: agent_dub cannot write to agent.* '
                        '(= GRANT too restrictive)';
    END IF;
    RAISE NOTICE 'positive test verified: agent_dub can write to agent.*';
END $$;

COMMIT;
