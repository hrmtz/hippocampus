[English](EMBED_CONTRACT.md) ・ **日本語**

# 埋め込み契約

hippocampus-mcp 内のすべての dense ベクトルは、PostgreSQL に触れる前に
**L2 正規化された 1024 次元の float** でなければなりません。境界は
`embed_client` モジュール（= 統一された producer/consumer エントリポイント）
です。これがすべての `encode()` / `encode_batch()` の return で不変条件を
アサートします。

## ストレージスキーマ（= どのテーブルが何を保持するか）

2 つのストレージファミリがあり、いずれも L2 単位不変条件に依存します。

| table.column | type | index opclass | dim |
|---|---|---|---|
| `personal.messages.dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `personal.conversations.conv_dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `personal.conversation_segments.seg_dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `library.messages.dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW | 1024 |
| `library.chunks.dense` | `halfvec(1024)` | `halfvec_ip_ops` HNSW (deferred) | 1024 |
| `agent.ghost_memories.dense` | `vector` (variable) | `vector_cosine_ops` HNSW (009b, deferred ≥1000 rows) | enforced by `CHECK (vector_dims(dense) = embed_dim)`, default 1024 |

どちらのファミリも正規化された入力を必要とします。

- **halfvec_ip_ops**（personal/library）: 内積 = コサインとなるのは
  **単位ノルムのときに限ります**。ランキングがマグニチュードに引きずられて
  壊れることが罠です。
- **vector_cosine_ops**（agent.ghost_memories）: 明示的なコサイン距離で、
  pgvector はこれを `1 - (a · b) / (|a| · |b|)` として実装します。コサインは
  構成上スケール不変なので、この opclass にとって非単位入力はランキング上の
  *バグではありません* — それでもヘルパは単位ノルムをアサートします。理由は
  (a) writer 間の一貫性が契約を単純にすること、(b) 将来 ghost を
  `halfvec_ip_ops` へ移行した際にこれがないと黙ってリグレッションすること、
  (c) BGE-M3 はそもそも設計上、単位出力を生成すること、です。

dim は DB 層で強制されます。`halfvec(1024)` は INSERT 時に誤った dim を
拒否し、`agent.ghost_memories.dense` は行ごとの `embed_dim` カラムに対する
`CHECK` 制約を持ちます。ヘルパの dim チェックは冗長ですが、より早い失敗と
より明確なエラーメッセージを提供します。

## なぜアサーションが存在するのか

`halfvec_ip_ops`（支配的な opclass）では、非正規化入力が黙ってランキングを
壊します — 結果が意味的類似度ではなくベクトルのマグニチュードに引っ張られ、
DB 層ではエラーも警告も出ません。BGE-M3 はデフォルトで L2 正規化された出力を
生成するため、今日のところシステムは動作します。アサーションは、非正規化
ベクトルを送り出す将来のプロバイダ切り替え（Voyage / OpenAI / ファインチューン
したローカルモデル）を、それがストレージに到達する前に捕捉します。

## 境界

消費者は embed 呼び出しを `embed_client` 経由でルーティングしなければなりません。

```python
from embed_client import encode, encode_batch

vec = encode("query text", where="myscript.search")
vecs = encode_batch(["a", "b", "c"], where="myscript.ingest")
```

内部では `embed_client` が 3 つのバックエンドから選択します。

1. `BGE_EMBED_URL` で設定する remote HTTP `/embed`
2. `EMBED_PROVIDER=bge-ondemand` で設定する local compose BGE-M3 on-demand
3. `EMBED_PROVIDER=bge-inprocess` で設定する in-process BGE-M3

すべての return パスで `embed_norm.assert_normalized` /
`assert_batch_normalized` をアサートします。ヘルパは次を行います。

- `len(vec) == 1024` を検証する
- `abs(||vec||₂ - 1.0) ≤ 1e-3` を検証する
- 失敗時に、呼び出し元ラベル付きで `EmbeddingNotNormalizedError`（`ValueError`
  のサブクラス）を送出する

1e-3 の許容値は、BGE-M3 fp16 の数値ドリフト（~1e-4）を余裕をもって上回り、
現実的なプロバイダのドリフトのいずれをも十分に下回ります。正当な BGE-M3 出力は
通過し、非正規化ベクトルを送り出すプロバイダ切り替えは最初のリクエストで
声高に失敗します。

### カバレッジゲート

`scripts/check_embed_coverage.sh` は、直接 embed を呼び出す（`model.encode(...)`
または `POST /embed*`）あらゆる *.py が、次のいずれかであることを強制します。

- `embed_client` / `embed_norm` を import している、または
- `.embed_coverage_allowlist`（= レガシー、移行予定）に列挙されている。

両方をバイパスする新しいスクリプトはゲートで失敗します。

### アトミシティ契約

アサーションは、パッチ済みのすべての ingest パスにおいて **いかなる DB 書き込み
よりも前に** 送出されます。消費者は、トランザクションを開く前、あるいは部分
INSERT の副作用が生じる前に `encode_batch()` を呼ばなければなりません — ヘルパは
「バッチ境界ごとに all-or-nothing」であり、最初の不良行をその index 付きで
`where=...[i]` ラベルに表面化させます。

## embed プロバイダの切り替え

もし BGE-M3 を Voyage / OpenAI / ファインチューンしたローカルモデルへ切り替える
場合:

1. プロバイダが L2 単位ベクトルを返すことを確認する。そうでなければ、return の
   前に `embed_client` 内のアダプタで正規化する（`vec / np.linalg.norm(vec)`）。
   割り算を **それ以外の場所に** 追加しないこと — アダプタが契約を所有しなければ
   なりません。
2. 出力 dim が 1024 であることを確認する。そうでなければ、スキーマ migration は
   別個の、不可逆な決定になります。すべての `dense` カラム + HNSW インデックスを
   再構築し、`embed_norm` の `EXPECTED_DIM` を更新しなければなりません。
3. smoke（`sops exec-env secrets.enc.yaml`
   の下で `scripts/smoke_embed_norm.py`）を実行し、新プロバイダに対して
   アサーションが通過することを確認する。

## 参照

- helper: [`src/hippocampus/embed/client.py`](../src/hippocampus/embed/client.py)
- assertion: [`src/hippocampus/embed/norm.py`](../src/hippocampus/embed/norm.py)
- coverage gate: [`scripts/check_embed_coverage.sh`](../scripts/check_embed_coverage.sh)
- finding: dual-magi-review Round 1 of `design-history/EMBED_BOUNDARY_REVIEW.md`,
  cluster `coverage_drift` (REJECT) + `ghost_schema_doc_drift` (HIGH)
- issue: [#29](https://github.com/anthropics/hippocampus-mcp/issues/29)
- on-demand backend contract: [`docs/BGE_ONDEMAND.ja.md`](BGE_ONDEMAND.ja.md)
