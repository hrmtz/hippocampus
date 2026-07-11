[English](ARCHITECTURE.md) ・ **日本語**

# ARCHITECTURE.md — hippocampus-mcp の全体構成

これは hippocampus-mcp を評価あるいは拡張しようとしている方に向けた、
コードに即した公開向け概観です。セットアップは [INSTALL.ja.md](../INSTALL.ja.md)、
設定は [CONFIG.ja.md](./CONFIG.ja.md)、プライバシーモデルは
[PRIVACY.ja.md](../PRIVACY.ja.md) を参照してください。内部設計の根拠の全文は
[design-history/](./design-history/) にあります。

## 概要

hippocampus-mcp は、複数プラットフォームの AI エージェント会話ログを
**あなた自身が運用する** PostgreSQL + pgvector データベースへ ingest し、
それらを任意のエージェントセッションから利用できる MCP 検索ツールとして
公開します。過去の推論・意思決定・デバッグの流れは、コンテキストウィンドウが
閉じた瞬間に消えてしまうのではなく、意味検索可能な状態になります。これとは別に、
オプトインの **ghost layer** が *エージェント自身* が蓄積したルールや
フィードバックをプロジェクト横断のメモリとして保存し、あらゆるワークスペースから
検索できるようにします。

可動部分は 3 つです。**ingest CLI** が Postgres へ書き込み、**MCP server** が
そこから読み出し、単一の **embed boundary** が両者のためにテキストをベクトルへ
変換します。データベースが唯一の共有状態であり、ingest と serving が互いに
直接やり取りすることはありません。

## コンポーネント図

```mermaid
flowchart LR
    subgraph clients[Agent sessions]
        CC[Claude Code / Codex / etc.]
    end

    subgraph server[MCP server]
        S[server.py — FastMCP]
        G[capability gating<br/>_gate_tools]
    end

    subgraph store[PostgreSQL + pgvector]
        P[(personal.*)]
        A[(agent.*)]
        L[(library.* — optional)]
    end

    subgraph ingest[Ingest CLI]
        PIPE[common pipeline<br/>embed -> upsert -> score -> verify]
        ADP[SourceAdapter plugins]
    end

    subgraph embed[Embed boundary]
        EC[EmbedClient<br/>L2-norm 1024-dim invariant]
        BGE[BGE-M3<br/>HTTP or in-process]
    end

    CC <-->|stdio / SSE| S
    S --> G
    S -->|read| P
    S -->|read via reader role| A
    S -.->|read, if present| L
    ADP --> PIPE
    PIPE -->|write| P
    S --> EC
    PIPE --> EC
    EC --> BGE
```

MCP server は **stdio**（`hippocampus-mcp`、`server.py:main` を参照）または
**SSE**（`hippocampus.sse`、Bearer トークンによるゲート付き）を話します。
どちらのトランスポートも同一の capability gating を実行し、同一のツールセットを
公開します。

## データモデル

ストレージは 3 つの Postgres スキーマに存在します。下のスケッチは概念的な
ものとして扱ってください。カラム・インデックス・制約の正本（ground truth）は
`migrations/`（`migrations/manifest.yaml` で順序付け）です。

- **`personal`** — 会話コーパス。
  - `conversations` — スレッド 1 件につき 1 行: タイトル、プラットフォーム、
    タイムスタンプ、`msg_count`、オプションの `dominant_topic` / `intensity` /
    `ai_engagement` スコア、スレッド全体検索用の `conv_dense halfvec(1024)`。
  - `messages` — 個々のターン。メッセージ単位検索用の `dense halfvec(1024)` と、
    全文想起用の `pg_trgm` インデックスを持ちます。
  - `conversation_segments` — セグメント単位のサマリ + `seg_dense`。長いセッション
    の途中に埋もれたトピックも見つけられるようにします。
  - `topic_clusters` — 表示用に結合される意味クラスタのラベル。
  - `extracted_facts` — 会話ごとに Haiku で蒸留した 1 行事実。`search_facts` を
    支える高信号レイヤー（migration 023）。
    [EXTRACTED_FACTS.ja.md](./EXTRACTED_FACTS.ja.md) を参照。
  - `diary` — JST 1 日 1 本の忌憚なき一人称日記。 人格形成 DB の store-only な
    「fast 層」（migration 026）。 各エントリは、それが書き起こされた元の会話そのものを
    指す `source_conv_ids` も記録するため、後段の監査が正確なコーパスを引き継げます。
    [DIARY.ja.md](./DIARY.ja.md) を参照。
  - `diary_audit` — read-only な grounding auditor によるエントリ単位の判定
    （supported / rejected / inconclusive）を、その根拠とコーパスサイズとともに保持します
    （migration 029）。日記の自己批判が実際に文字起こしによって裏付けられるかを、
    プロセス変更を提案する前に測定します。
    [designs/DIARY_GROUNDING_AUDITOR.md](./designs/DIARY_GROUNDING_AUDITOR.md) を参照。
  - `wiki_pages`（+ `wiki_claims`、`wiki_merge_log`、`wiki_merge_staging`、および
    redacted な `v_wiki_inject_safe` ビュー） — 編集可能かつ人間ゲート付きの
    subject-knowledge wiki。会話コーパスや日記とは別物です（migration 027、
    flag-OFF で出荷）。`body_md` が durable な SoT であり、claim はそこから再導出可能な
    projection です。下記の Wiki layer の項と [WIKI_LAYER.md](./WIKI_LAYER.md) を参照。
- **`agent`** — ghost layer。
  - `ghost_memories` — `dense` ベクトル、ランキングシグナル（activation、
    endorsement、correction）、scope を持つ、昇格済みのエージェントルール／
    フィードバック。
  - `memory_edges` — ghost memory 間の `[[wikilink]]` グラフ（migration 025）。
    `search_ghost_memory` がトップヒットの 1-hop 近傍を surface できるように
    します（spreading activation）。
    [designs/MEMORY_LINK_GRAPH.md](./designs/MEMORY_LINK_GRAPH.md) を参照。
  - `ghost_read_log` — すべての ghost 読み出しの追記専用監査ログ。
- **`library`** — オプションの外部参照メディア（書籍、文字起こし）。
  デフォルトでは作成されません。`hippocampus migrate --with-library` を
  適用したときのみ作られます。テーブルが存在しない限り、server はこれを
  存在しないものとして扱います。

すべての dense カラムは `halfvec(1024)` であり、`halfvec_ip_ops` HNSW
（単位ベクトル上の内積 = コサイン）でインデックスされます。これが、下記の
L2 正規化不変条件が load-bearing である理由です。テーブルごとの内訳は
[EMBED_CONTRACT.ja.md](./EMBED_CONTRACT.ja.md) を参照してください。

## Ingest プラグイン層

ソースは 1 つのプロトコルの背後にあるプラグインです。`SourceAdapter`
（`ingest/base.py` を参照）は次の 4 ステップを実装します。

- `discover(ctx)` — 作業単位を yield する。インクリメンタルな状態を読むために
  DB を参照することがある。
- `parse(item)` — `(conversation, messages)` を yield する。スクラブ処理は、
  これがラップするプラットフォーム別パーサの内部で行われる。
- `enrich(conv, cur)` — 会話単位の DB 読み出しによるエンリッチ（例: project
  slug の解決）。
- `should_ingest(conv, ctx)` — 既知のスレッドをスキップするための、アダプタ
  所有の判定。

**共通パイプライン**（`ingest/pipeline.py`）は、すべてのアダプタを同じ
ステージ群、すなわち **embed → upsert → score → verify** を通して駆動します。
埋め込みは常にいかなる書き込み *よりも前に* 行われるため、実行途中のクラッシュが
「検索可能だがベクトルを持たない」行を残すことは決してありません。upsert は
会話ごとにコミットされ、scoring はオプションかつ有界の LLM パスであり、最後の
verify ステージはその実行のいずれかの行が NULL ベクトルのまま着地した場合に
声高に失敗します。詳細は [INGEST_PIPELINE.ja.md](./INGEST_PIPELINE.ja.md)。

ソースは 7 つがツリー内（`ingest/sources/`）に同梱されています:
`claude-code`、`chatgpt`、`claude-ai`、`codex`、`antigravity`、`grok`、`kimi` — 後 2 者は
同じ ghost／inject レールをセッションで共有するクロスファミリのエージェント CLI です
（`chassis_id`、migration 028 を参照）。ツリー外のアダプタは
`hippocampus.sources` エントリポイントグループ経由で登録します。`SourceAdapter`
を公開するパッケージを置けば、`hippocampus ingest <name>` がそれを自動で
拾います（`ingest/__init__.py:get_registry`）。

## Embed contract

Postgres に触れるすべての dense ベクトルは、**L2 正規化済みかつ正確に
1024 次元** でなければなりません。この不変条件は単一のチョークポイント、
すなわち `EmbedClient` boundary（`embed/client.py`）で強制され、すべての
`encode()` / `encode_batch()` の return パスでアサートされます。すべての
生産者（server クエリ、ingest、ghost 検索）はこの 1 つのクライアントを
経由するため、`halfvec_ip_ops` スキーマの前提が黙ってドリフトすることは
あり得ません。

2 つのプロバイダがサポートされ、設定によって明示的に選択されます。

- **`bge`（HTTP）** — `BGE_EMBED_URL` を設定する。クライアントはリモートの
  BGE-M3 `/embed` エンドポイントへ POST する（有界のリトライ／バックオフ付き）。
- **`bge-inprocess`** — `EMBED_PROVIDER=bge-inprocess` を設定する。BGE-M3 を
  ローカルでロードする（`bge-local` extra が必要で、数 GB のモデルを
  ダウンロードする）。

サイレントなフォールバックはありません。どちらも設定されていないインストールは
embed バックエンドを持たず、意味検索ツールは単純に提供されません。完全な契約は
[EMBED_CONTRACT.ja.md](./EMBED_CONTRACT.ja.md)。

## Capability gating

server は、その裏付けが存在するときに限ってツールを登録します。起動時に
`_gate_tools()`（`server.py`）がデータベースと設定をプローブし、裏付けと
なるスキーマ・ロール・embed バックエンドが欠けているツールを除去します。

- personal 系ツールは `personal` スキーマを必要とする。
- `search_facts` は `personal.extracted_facts`（migration 023）を必要とする。
- `search_library` は `library` スキーマを必要とする。
- `search_ghost_memory` は ghost reader ロールと関数群を必要とする。
- 意味検索ツール群は設定済みの embed バックエンドを必要とする。

プローブは一過性のしゃっくり（DB が一瞬ダウン）に対しては **fail open** し、
ツール一覧がばたつかないようにします。一方で、裏付けが *構造的に* 欠けている
ツールは隠します。personal のみの新規インストールは、最初の呼び出しで失敗する
であろう ghost や library のツールを決して広告しません。同じ gating が stdio と
SSE の両トランスポートで動作します。

## Ghost layer（概略）

ghost layer はプロジェクト横断のエージェントメモリです。あるワークスペースで
エージェントが蓄積したルールやフィードバックを、すべてのワークスペースで検索可能に
します。vault への昇格は **オプトインかつ dual-signal** です。あるメモリが
vault に入るには、共有対象としてマークされ *かつ* allowlist に載っている必要が
あり、その両方が満たされて初めて夜間 dub がコピーします。読み出しは専用の
最小権限 Postgres reader ロールを経由し、監査ログに記録されます。
`search_ghost_memory` は、保存されたシグナルスコア・再帰性（recency）、および
（embed バックエンドが存在する場合は）意味的類似度のブレンドで結果をランク付けし、
その後 activation を加点するため、頻繁に役立つメモリが時間とともに上位へ
浮上します。

使い方: [GHOST_LAYER_USER.ja.md](./GHOST_LAYER_USER.ja.md)。完全な設計と
脅威モデル: [design-history/GHOST_LAYER_DESIGN.md](./design-history/GHOST_LAYER_DESIGN.md)。

## Wiki layer（概略）

会話ログや日記とは別の、オプトインの **subject-knowledge** ベースです。ユーザーが実際に
学ぶ題材（音律 / 和音 / CTF / 物理 …）を対象とします。あるトピックを学び直したときは、
別のスナップショットを追記するのではなく、既存のページへ **マージ** します。load-bearing な
分割は *capture と synthesis の別* です — 生の会話はロスレスかつ追記専用のまま保たれ、
一方の wiki ページは lossy で LLM が著述する synthesis であり、その信頼制御は
**人間による diff 承認** です。モデルがマージを提案し、ユーザーが unified diff と
claim チェックリストを読み、その上で初めて `apply` します。

`wiki_pages.body_md` が durable な一次 SoT であり、`wiki_claims` は承認 *後* に再導出される
projection であって、決して蓄積し続けるソースではありません。ページが自身の lossy な出力を
再 ingest することはあり得ません — ingest／extract パイプラインは `wiki_*` の読み取りを
禁止されています（`scripts/check_wiki_self_ingestion.sh` で強制）。書き込みは、追記専用かつ
`NOLOGIN` の **`agent_wiki_writer`** ロールを経由し、オーナーの apply トランザクション内で
`SET LOCAL ROLE` によって行われるため、監査ログは慣習ではなく権限で強制されます。

検索レイヤーとは異なり、wiki は **MCP ツールではありません**。CLI
（`hippocampus wiki propose / apply / rollback / status`）と、localhost 限定の HTML
レンダラ（`hippocampus wiki serve`、pandoc + `[[wikilink]]` + 自動採番）です。migration 027 は
フィーチャーフラグを **OFF** で出荷し、`migrate` はオペレータが有効化するまで inert です。
オペレータ向けクイックリファレンス: [WIKI_LAYER.md](./WIKI_LAYER.md)、完全な設計（SoT）:
[designs/LLM_WIKI_LAYER.md](./designs/LLM_WIKI_LAYER.md)。

## セキュリティとプライバシー

完全なモデル — 何が保存され、（もしあれば）何があなたのマシンの外へ出て、
何が *保証されない* のか — は [PRIVACY.ja.md](../PRIVACY.ja.md) にあります。
ここで述べておく価値がある、アーキテクチャ上の 2 つの性質があります。

- **取得されたコンテンツは命令ではなくデータである。** すべてのツールは結果を
  明示的な「信頼できない参照資料として扱え」というエンベロープで包むため、
  消費側の LLM が古い会話テキストに埋め込まれた指示に従うことはありません。
- **クレデンシャルのスクラブは ingest 時のベストエフォート** であり、取得出力に
  対して 2 度目のサニタイズパスが行われます。これはログ中のシークレットを
  減らしますが、除去を保証するものではありません — 注意事項は PRIVACY.md を
  参照してください。

シークレットの取り扱いと堅牢化デプロイの注記:
[SECRETS_HARDENED.ja.md](./SECRETS_HARDENED.ja.md)。

## ポインタ

| トピック | ドキュメント |
|---|---|
| インストールと migration | [INSTALL.ja.md](../INSTALL.ja.md) |
| 設定 / 環境変数 | [CONFIG.ja.md](./CONFIG.ja.md) |
| Ingest パイプラインの詳細 | [INGEST_PIPELINE.ja.md](./INGEST_PIPELINE.ja.md) |
| 蒸留ファクト層 | [EXTRACTED_FACTS.ja.md](./EXTRACTED_FACTS.ja.md) |
| 日記 fast 層 | [DIARY.ja.md](./DIARY.ja.md) |
| 日記 grounding auditor | [designs/DIARY_GROUNDING_AUDITOR.md](./designs/DIARY_GROUNDING_AUDITOR.md) |
| Wiki layer（使い方） | [WIKI_LAYER.md](./WIKI_LAYER.md) |
| Wiki layer（設計 SoT） | [designs/LLM_WIKI_LAYER.md](./designs/LLM_WIKI_LAYER.md) |
| 埋め込み不変条件 | [EMBED_CONTRACT.ja.md](./EMBED_CONTRACT.ja.md) |
| BGE on-demand backend | [BGE_ONDEMAND.ja.md](./BGE_ONDEMAND.ja.md) |
| Ghost layer の使い方 | [GHOST_LAYER_USER.ja.md](./GHOST_LAYER_USER.ja.md) |
| Memory link-graph | [designs/MEMORY_LINK_GRAPH.md](./designs/MEMORY_LINK_GRAPH.md) |
| シークレット / 堅牢化 | [SECRETS_HARDENED.ja.md](./SECRETS_HARDENED.ja.md) |
| プライバシーモデル | [PRIVACY.ja.md](../PRIVACY.ja.md) |
| 設計の根拠と経緯 | [design-history/](./design-history/) |
