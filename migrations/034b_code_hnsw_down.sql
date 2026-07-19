-- 034b_code_hnsw_down.sql — drop the deja-code HNSW index only
DROP INDEX CONCURRENTLY IF EXISTS code.idx_code_chunks_dense;
