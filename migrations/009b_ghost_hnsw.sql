-- migrations/009b_ghost_hnsw.sql
-- 適用条件: SELECT count(*) FROM agent.ghost_memories
--           WHERE deleted_at IS NULL AND embed_model='bge-m3'
--           が 1000 行を超えてから (= model ごとに別途判定)
--
-- ⚠️ CONCURRENTLY のため transaction で wrap しない (= psql で直接 1 文ずつ)

-- BGE-M3 (= 1024 dim、 Phase 0 default、 唯一現用 model)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ghost_dense_bge_m3
    ON agent.ghost_memories USING hnsw (dense vector_cosine_ops)
    WHERE deleted_at IS NULL
      AND dense IS NOT NULL
      AND embed_model = 'bge-m3'
      AND embed_dim = 1024;

-- 将来 model 追加時の参考:
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ghost_dense_qwen3
--     ON agent.ghost_memories USING hnsw (dense vector_cosine_ops)
--     WHERE deleted_at IS NULL
--       AND dense IS NOT NULL
--       AND embed_model = 'qwen-3-embed'
--       AND embed_dim = 2048;

-- ⚠️ pgvector version 確認 (= 0.7.0+ で vector_cosine_ops on HNSW 必須)
-- ⚠️ smoke test: EXPLAIN SELECT で index usage 確認後 cron 投入
