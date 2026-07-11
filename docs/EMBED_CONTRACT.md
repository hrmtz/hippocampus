**English** ・ [日本語](EMBED_CONTRACT.ja.md)

# Embedding contract

All dense vectors in hippocampus-mcp must be **L2-normalized 1024-dim
floats** before they touch PostgreSQL. The boundary is the
`embed_client` module (= unified producer/consumer entry point); it
asserts the invariant on every `encode()` / `encode_batch()` return.

## Storage schema (= which tables hold what)

Two storage families, both depending on the L2-unit invariant:

| table.column | type | index opclass | dim |
|---|---|---|---|
| `personal.messages.dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `personal.conversations.conv_dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `personal.conversation_segments.seg_dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `library.messages.dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `library.chunks.dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW (deferred) | 1024 |
| `agent.ghost_memories.dense` | `vector` (variable) | `vector_cosine_ops` HNSW (009b, deferred ≥1000 rows) | enforced by `CHECK (vector_dims(dense) = embed_dim)`, default 1024 |

Both families need normalized input:

- **halfvec_ip_ops** (personal/library): inner product = cosine **iff
  unit norm**. Magnitude-driven ranking corruption is the trap.
- **vector_cosine_ops** (agent.ghost_memories): explicit cosine distance,
  which pgvector implements as `1 - (a · b) / (|a| · |b|)`. Cosine is
  scale-invariant by construction, so non-unit input is *not* a ranking
  bug for this opclass — but the helper still asserts unit norm because
  (a) consistency across writers simplifies the contract, (b) future
  migration to `halfvec_ip_ops` for ghost would silently regress without
  it, and (c) BGE-M3 already produces unit output by design.

Dim is enforced at the DB layer: `halfvec(1024)` rejects wrong dims at
INSERT; `agent.ghost_memories.dense` has a `CHECK` constraint against
the per-row `embed_dim` column. The helper's dim check is redundant but
provides earlier failure + a clearer error message.

## Why the assertion exists

For `halfvec_ip_ops` (the dominant opclass): un-normalized input
silently corrupts ranking — results get pulled by vector magnitude
instead of semantic similarity, with no error or warning at the DB
layer. BGE-M3 produces L2-normalized output by default, so today the
system works. The assertion catches a future provider swap (Voyage /
OpenAI / finetuned local model) that ships un-normalized vectors before
it reaches storage.

## The boundary

Consumers MUST route embed calls through `embed_client`:

```python
from embed_client import encode, encode_batch

vec = encode("query text", where="myscript.search")
vecs = encode_batch(["a", "b", "c"], where="myscript.ingest")
```

Under the hood `embed_client` selects between three backends:

1. remote HTTP `/embed`, set via `BGE_EMBED_URL`;
2. on-demand local compose BGE-M3, set via `EMBED_PROVIDER=bge-ondemand`;
3. in-process BGE-M3, set via `EMBED_PROVIDER=bge-inprocess`.

It asserts `embed_norm.assert_normalized` / `assert_batch_normalized` on
every return path. The helper:

- verifies `len(vec) == 1024`
- verifies `abs(||vec||₂ - 1.0) ≤ 1e-3`
- raises `EmbeddingNotNormalizedError` (subclass of `ValueError`) with
  the call-site label on fail

The 1e-3 tolerance is comfortably above BGE-M3 fp16 numeric drift
(~1e-4) and well below any realistic provider drift. Legitimate BGE-M3
output passes; a provider swap that ships un-normalized vectors fails
loudly on the first request.

### Coverage gate

`scripts/check_embed_coverage.sh` enforces that any *.py making a direct
embed call (`model.encode(...)` or `POST /embed*`) must either:

- import `embed_client` / `embed_norm`, or
- be listed in `.embed_coverage_allowlist` (= legacy, scheduled for
  migration).

New scripts that bypass both fail the gate.

### Atomicity contract

The assertion raises **before any DB write** in every patched ingest
path. Consumers MUST call `encode_batch()` before opening a transaction
or before any partial-insert side effect — the helper is "all-or-nothing
per batch boundary" and surfaces the first bad row with its index in
the `where=...[i]` label.

## Swapping embed providers

If you ever swap BGE-M3 for Voyage / OpenAI / a finetuned local model:

1. Confirm the provider returns L2-unit vectors. If not, normalize at
   the adapter (`vec / np.linalg.norm(vec)`) inside `embed_client`
   before returning. **Do not** add the divide elsewhere — the
   adapter must own the contract.
2. Confirm output dim is 1024. If not, the schema migration is a
   separate, irreversible decision: you must rebuild every `dense`
   column + HNSW index, and update `EXPECTED_DIM` in `embed_norm`.
3. Run the smoke (`scripts/smoke_embed_norm.py` under
   `sops exec-env secrets.enc.yaml`)
   to confirm the assertion passes against the new provider.

## References

- helper: [`src/hippocampus/embed/client.py`](../src/hippocampus/embed/client.py)
- assertion: [`src/hippocampus/embed/norm.py`](../src/hippocampus/embed/norm.py)
- coverage gate: [`scripts/check_embed_coverage.sh`](../scripts/check_embed_coverage.sh)
- finding: dual-magi-review Round 1 of `design-history/EMBED_BOUNDARY_REVIEW.md`,
  cluster `coverage_drift` (REJECT) + `ghost_schema_doc_drift` (HIGH)
- issue: [#29](https://github.com/anthropics/hippocampus-mcp/issues/29)
- on-demand backend contract: [`docs/BGE_ONDEMAND.md`](BGE_ONDEMAND.md)
