-- 034b_code_hnsw.sql — HNSW ANN index for deja-code chunks (no_tx)
-- REQUIRED for Phase 1 (Stop-hook advisor top-k has a hard latency budget;
-- seq scan over tens of thousands of halfvec rows is not acceptable and
-- fail-open would hide the regression). Design §5, dual-magi R1/R2.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_code_chunks_dense
  ON code.chunks USING hnsw (dense halfvec_ip_ops) WITH (m = 16, ef_construction = 64);

-- R2 CRITICAL guard (014_inject_governance idiom): a failed CONCURRENTLY
-- build leaves an INVALID index, and a rerun's IF NOT EXISTS silently skips
-- it — the migration would enter the ledger while the index is unusable.
-- Loud-fail instead. Remediation on failure:
--   DROP INDEX CONCURRENTLY code.idx_code_chunks_dense;  -- then re-run 034b
-- The check is schema-qualified by OID (a bare relname lookup could be
-- satisfied by a valid same-named index in another schema).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_index i
    WHERE i.indexrelid = to_regclass('code.idx_code_chunks_dense')
      AND i.indrelid   = 'code.chunks'::regclass
      AND i.indisvalid
      AND i.indisready
  ) THEN
    RAISE EXCEPTION 'idx_code_chunks_dense is missing or INVALID after '
      'CONCURRENTLY build. Remediate with DROP INDEX CONCURRENTLY '
      'code.idx_code_chunks_dense and re-run 034b.';
  END IF;
END $$;
