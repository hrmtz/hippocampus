[English](INSTALL.md) ・ **日本語**

# INSTALL.md — 詳細セットアップ

[README のクイックスタート](README.ja.md#クイックスタート)が最短経路です。
この文書は、その判断と失敗モードを扱います。

## 前提条件

- **Python 3.11+**
- **PATH 上の `psql`** — セットアップ時だけでなく、実行時の必須前提です。
  `hippocampus migrate` はすべての migration ファイルを
  `psql -v ON_ERROR_STOP=1` サブプロセスに委譲します（いくつかの migration は
  `CREATE INDEX CONCURRENTLY` / `ALTER TYPE .. ADD VALUE` を含み、これらは
  ドライバ管理のトランザクション内では実行できません）。Debian/Ubuntu:
  `apt-get install postgresql-client`。macOS: `brew install libpq`（そして
  PATH に通す）。
- **pgvector を備えた PostgreSQL** — 同梱の compose ファイル経由、または既存の
  サーバー経由（下記参照）。
- Docker は任意です。compose 経路と、任意のローカル embed サーバーにのみ
  使われます。

チェックアウトからパッケージをインストールします:

```bash
pip install .                              # base: MCP server + CLI + ingest
pip install '.[bge-local]'                 # + in-process BGE-M3 embedding
pip install '.[scoring]'                   # + Anthropic client for the optional scoring stage
```

base インストールは意図的に軽量です（torch を含みません）。

## データベース: compose 経路 vs 既存 PostgreSQL

### Path A — ローカル compose（デフォルト。`hippocampus init` が駆動する）

通常、compose を手動で実行することはありません。init のデータベース
プロンプトで `local` を選ぶ（デフォルト。`--db local` でも可）と、init は
次を行います:

1. データベースパスワードを生成し、`.env` に `PG_PASSWORD` として書き込む
   （compose は同じファイルを読む — シークレットは 1 つ、場所も 1 つ）、
2. ユーザー/データベース `hippocampus` の `localhost` 向け `PG_URL` を構築する、
3. `docker compose up -d postgres` の実行を提案し、準備完了まで待つ、
4. core の migration を実行する。

コンテナは `127.0.0.1` のみにバインドされた `pgvector/pgvector:pg16` で、
データは名前付きボリューム `pg_data` に入ります。ホストポートのデフォルトは
5432 です。これが使われている場合（ホストの postgres、または WSL2 下で
Windows 側のリスナー）、`--pg-port <port>` を使ってください。init は
`HIPPOCAMPUS_PG_PORT` を `.env` に記録するので、compose が自動的に従います。

**DDL は決して compose を通して実行されません** — コンテナには init スクリプトの
マウントがありません。スキーマ作成はもっぱら `hippocampus migrate`
（`hippocampus init` があなたの代わりに実行します）です。

### Path B — 既存 PostgreSQL（ローカルまたはリモートサーバー）

init のデータベースプロンプトで `existing` を選ぶ（または `--db existing`、
スクリプト化インストールなら `--pg-url-env VAR`）と、URL を指定できます。
リモートサーバーの場合は PRIVACY.md を参照してください。あなたの会話テキストが
ネットワークを通過します — プライベートネットワーク内か TLS の背後に置いて
ください。

対象のデータベース/クラスタに対する要件:

- pgvector が利用可能であること（`CREATE EXTENSION vector` が成功する必要が
  あります — migration 001 は拡張 `vector` と `pg_trgm` を作成し、これは通常
  スーパーユーザー、または拡張が許可されたクラスタを要します）。
- migration ロールに **CREATEROLE** が必要です: migration 009 はクラスタ全体の
  `agent_*` ロールを作成します（`IF NOT EXISTS` でガードされているので、既に
  それらを持つクラスタでも問題ありません — 1 つのクラスタ上の 2 つの
  hippocampus データベースはロールを共有します）。
- MCP サーバーと ingest が動く場所からのネットワーク到達性。

その後、あなたの DSN で `hippocampus init` を実行するか、init の DB ステップを
スキップ（`--skip-migrations`）して `hippocampus migrate` を自分で実行します。

## 埋め込みバックエンドの判断

セマンティック検索は**デフォルトでオフ**です。`hippocampus init` は明示的な
選択を強制し、黙ったフォールバックはありません（未構成のインストールが約 6 GB の
モデルを不意にダウンロードすることは決してありません）。

| バックエンド | 選ぶべき場面 | セットアップ | トレードオフ |
|---|---|---|---|
| **none** | モデルに RAM/ディスクを割く前にインストールを試したい | なし | セマンティックツール（`search_personal_memory` など）は隠され、**ingest は embed バックエンドを要求します**（ベクトルはテキストと同時に書き込まれます — 設計上、ベクトルのない行は存在しません）。したがって最初の本番 ingest の前にバックエンドを構成してください |
| **bge HTTP**（`bge-http`） | セマンティック検索が欲しく、もう 1 つコンテナを動かしても構わない、または別の場所に GPU マシンがある | `docker compose --profile bge up -d`（`127.0.0.1:8086` で提供。`.env` に `BGE_EMBED_TOKEN` を設定）、その後 `BGE_EMBED_URL=http://localhost:8086` | 初回コンテナ起動時にモデルのダウンロード（約 6 GB）。定常状態で約 6 GB の RAM。サーバープロセス自体は軽量なまま |
| **bge in-process**（`bge-inprocess`） | 単一マシン、追加コンテナなし | `pip install 'hippocampus-mcp[bge-local]'`、`EMBED_PROVIDER=bge-inprocess` | **MCP/ingest プロセス内で**約 6 GB の RAM。初回呼び出しでモデルをダウンロード。コールドスタートが遅い |
| 将来のプロバイダ | ホスト型の埋め込み API | まだ未実装 | 切り替え時にコーパスの再 embed が必要 — 異なるモデルのベクトルは比較できません |

すべてのバックエンドは、L2 正規化された 1024 次元の出力をアサートする 1 つの
クライアント境界を通過します。それ以外を返すバックエンドは、ランキングを黙って
破壊する代わりに明示的に失敗します。

後から変更するには: `.env` を編集し（`BGE_EMBED_URL`/`BGE_EMBED_TOKEN` か
`EMBED_PROVIDER=bge-inprocess` か）、サーバーを再起動します。

## Migration: `hippocampus migrate`

Migration は `migrations/manifest.yaml` で順序付けられ（単一の信頼できる情報源
— ファイル名には歴史的経緯で重複した prefix があるため、glob は誤りです）、
ledger テーブル（`public.hippocampus_schema_migrations`）を使って psql 経由で
適用されるので、再実行では pending のものだけが適用されます。

```bash
hippocampus migrate                      # core tier (default): personal + agent schemas
hippocampus migrate --with-library       # + optional library schema (external reference media)
hippocampus migrate --include-optional   # + deferred extras (ghost HNSW index — only worth it at 1000+ rows)
hippocampus migrate --status             # applied/pending table
hippocampus migrate --dry-run            # show what would run
```

ティア:

- **core** — personal-memory と ghost ツールが必要とするすべて。`hippocampus
  init` が適用するのはこれです。
- **library** — 外部の参照メディア（自分で ingest する書籍、字幕、文字起こし）
  のための第 2 のコーパススキーマ。library なしのインストールも完全に
  サポートされます。library ツールが単に登録されないだけです。
- **optional** — 現在は ghost layer 用の遅延 HNSW インデックスが 1 つ。
  `agent.ghost_memories` が 1000 行以上になったら適用してください。

### 既存データベース: `--baseline`

データベースが既にスキーマを保持している場合（例: manifest runner が存在する
前に手動で構築された場合）、素の `migrate` は migration 001 を再適用しようと
して失敗します。baseline モードは、選択された pending エントリを **SQL を一切
実行せずに** ledger に刻印します:

```bash
hippocampus migrate --baseline --dry-run     # preview the stamp list
hippocampus migrate --baseline               # asks you to type 'baseline' to confirm
hippocampus migrate --baseline --yes         # non-interactive
```

あなたはスキーマが刻印されるファイルと実際に一致していることをアサートして
います — runner はその主張を検証しません。`--with-library` /
`--include-optional` と組み合わせて、どのティアが刻印されるかを制御します。
baseline 後、以降の実行は本当に新しいファイルだけを適用します。

### 別のデータベースを対象にする

argv より env var を優先してください（argv はシェル履歴とプロセスリストに
漏れます）:

```bash
HIPPOCAMPUS_MIGRATE_DB=scratchdb hippocampus migrate   # bare DB name; host/user from PG_URL
hippocampus migrate --db-url postgresql://...          # full override; avoid in shared transcripts
```

### 失敗のセマンティクス

失敗したファイルは ledger に記録**されません** — 原因を直して再実行します。
すでに適用されたファイルはスキップされます。`CREATE INDEX CONCURRENTLY` の
ビルドが途中で死ぬと `INVALID` インデックスが残ることがあります。該当する
migration ファイルは、正確な修復手順（`DROP INDEX CONCURRENTLY ...; 再適用`）を
出力する独自のチェックを持っており、runner はそれをそのまま表示します。

## トラブルシューティング: まず `hippocampus doctor`

`doctor` は診断のエントリポイントです。各チェックは 1 行を出力します —
`✓` 合格、`✗` 失敗（終了コード 1）、`–` 情報/機能オフ — そして出力は意図的に
**バグレポートに貼り付けても安全**です: DSN の userinfo、パスワード、トークンは
出力前に scrub されます。

チェック項目: `.env` のパーミッション、PostgreSQL 接続性 + サーバーバージョン、
スキーマの存在（personal / agent は必須、library は任意）、migration ledger
vs manifest、embed バックエンドの到達性、ghost reader ロール、dense-NULL の
カウント、要約のカバレッジ、スコアリングキーの存在。

よくある失敗:

| 症状 | 意味 | 対処 |
|---|---|---|
| `psql not found on PATH`（`migrate` から） | postgresql クライアント未インストール | `apt-get install postgresql-client`（またはディストロ相当） |
| `✗ postgres: ... OperationalError` | PG 到達不可 / パスワード違い / ポート違い | コンテナが起動しているか（`docker compose ps`）、ポート、`.env` の `PG_URL` を確認 |
| `✗ embed: ... HTTP 401 (auth?)` | embed サーバーがトークンを拒否 | `.env` の `BGE_EMBED_TOKEN` は embed サーバー起動時のものと一致する必要がある |
| `✗ embed: ... HTTP 404` / `unreachable` | `BGE_EMBED_URL` が誤り、またはサーバーが動いていない | `docker compose --profile bge up -d`。URL にパスサフィックスがないことを確認（クライアントが `/embed` を付加する） |
| `✗ dense-NULL: N message(s)` | 過去の ingest が embed バックエンド停止中に実行された — それらの行はセマンティック検索から見えない | バックエンドを直し、該当ソースを再 ingest（upsert は冪等）。新しい ingest はこの状態で明示的に失敗し、黙った欠落を残さない |
| `✗ ghost reader: connected but agent.search_ghost_ranked not found` | ghost migration が欠落 | `hippocampus migrate`（core tier に含まれる） |
| `– ghost reader: PG_URL_AGENT_READ_MCP not set` | ghost ツールがオフ（ghost layer を使わないなら問題なし） | `hippocampus init --ghost` |
| `✗ .env: permissions 0644 ...` | シークレットファイルが他ユーザーから読める | `chmod 600 .env` |

## 自動化

### 夜間 ingest（cron）

```cron
# crontab -e  — nightly at 03:00; flock prevents overlap with a manual run
0 3 * * * flock -n /tmp/hippocampus_ingest.lock -c 'cd /path/to/hippocampus-mcp && /path/to/venv/bin/hippocampus ingest claude-code && /path/to/venv/bin/hippocampus ingest codex' >> ~/hippocampus-ingest.log 2>&1
```

注: CLI 自体はロックを取りません（会話ごとの upsert は冪等です）。したがって
`flock` ラッパーは、cron を手動実行に対して直列化するための行儀のよい方法です。
ZIP ソース（chatgpt / claude-ai）は一度きりのインポートであり、cron の候補では
ありません。

### セッション終了時の ingest（Claude Code SessionEnd フック）

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "flock -n /tmp/hippocampus_ingest.lock -c 'cd /path/to/hippocampus-mcp && /path/to/venv/bin/hippocampus ingest claude-code' >/dev/null 2>&1 &",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### 要約

`hippocampus summarize`（任意。`ANTHROPIC_API_KEY` と embed バックエンドが
必要）は、要約粒度の検索のために会話レベルの要約 + 埋め込みを埋め戻します。
便利なフラグ: `--limit N`、`--dry-run`、`--platforms claude_code,codex`、
`--segments-only`。大きな ingest の後、または cron から週次で実行してください。
まだ要約を持たない会話だけを処理します。

## 複数マシン

すべてが PostgreSQL を介して協調するので、マルチマシン構成は単なる設定です:

- PG を 1 つのホストで動かし、各マシンの `PG_URL` をそこに向ける（信頼された
  ネットワークの外では TLS / `?sslmode=require` を使う）。
- 1 つの embed サーバーがすべてのマシンに提供できる — `BGE_EMBED_URL` を
  そのアドレスに設定し、`BGE_EMBED_TOKEN` を設定し続ける。compose のデフォルトは
  localhost のみにバインドすることを忘れずに。バインドを意図的に広げ、自前の
  トランスポートセキュリティの背後に置くこと。
- ingest はソースファイルが存在する場所で動く（各マシンが自身の
  `~/.claude/projects` を ingest する）。重複排除は会話 id で行われるので、
  重なっても無害。
- ネットワーク越しに embed トラフィックを送る前に [PRIVACY.md](PRIVACY.ja.md)
  を参照: embed のペイロードはあなたのメッセージテキスト全文です。

## インストールをエンドツーエンドで検証する

`bash scripts/test_clean_container.sh` は、文書化されたフローを使い捨ての
コンテナ（新規 pgvector + python:3.12-slim、`pip install`、
`hippocampus init --embed none --yes`、doctor、ソース一覧）で再現します。
これが通るのにあなたのインストールが通らない場合、違いはあなたの環境です —
`hippocampus doctor` の出力が調べるべき場所です。
