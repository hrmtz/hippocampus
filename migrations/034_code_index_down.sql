-- 034_code_index_down.sql — rollback deja-code index layer
-- Drops all 034/034b objects (the HNSW index lives inside the schema).

DROP SCHEMA IF EXISTS code CASCADE;

-- role は残す (cluster-level につき schema rollback の範囲外 — 009 down と同
-- idiom。再構築時に再利用、明示削除は user 判断):
-- DROP ROLE IF EXISTS code_read_hook;
