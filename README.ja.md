[English](README.md) ・ **日本語**

# hippocampus-mcp

AI エージェントを毎日使う人のためのパーソナルメモリ基盤です。

hippocampus-mcp は、複数プラットフォーム（Claude Code、ChatGPT、claude.ai、
Codex、Grok、Kimi、Antigravity）の会話ログを、**あなた自身が運用する**
PostgreSQL + pgvector
データベースに取り込み、それらを任意のエージェントセッションから MCP 検索
ツールとして利用できるようにします。過去の推論・意思決定・デバッグの記録が、
ウィンドウを閉じた瞬間に消え去ることがなくなります。

このシステムを特徴づけるのは **ghost layer** です。これは別建ての opt-in な
保管庫で、*エージェント自身*が蓄積したルールやフィードバック（「前回これが
失敗したのは…が原因だった」）が毎晩同期され、すべてのプロジェクトから検索
できるようになります。人間の会話の想起だけでなく、プロジェクトを横断する
エージェントメモリです。

検索可能なコーパスの上には、それぞれ専用のドキュメントを持つ 3 つの追加の
opt-in レイヤーが乗ります。蒸留された **facts** レイヤー（`search_facts`）、
エージェントが 1 日 1 回書く一人称の **diary**（加えて、各エントリの自己批判を
トランスクリプトと照合する読み取り専用の grounding 監査役）、そして実際に
あなたが学ぶ主題知識のための、編集可能で人間がゲートする **wiki** です。
これらがどう組み合わさるかは
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。

コーパスはターミナルのエージェントからだけでなく、opt-in の OAuth ゲート付き
**リモート MCP コネクタ**経由で **claude.ai の web とモバイル**からも到達できます。
スマホの claude.ai に「X について何を決めたっけ？」と聞けば、あなたのデータベースを
検索します。[docs/CONNECTOR.md](docs/CONNECTOR.ja.md) を参照。

> 名前について: 海馬（hippocampus）は、睡眠中に短期的な経験を長期記憶へと
> 定着させる脳の構造です。このシステムはそのループを模倣しています。日中の
> セッションが JSONL として蓄積され、夜間の ingest がそれらを埋め込んで
> 永続化し、次のセッションで想起できるようになります。

```
INGEST                          STORE                      RETRIEVE (MCP)
Claude Code sessions  ─┐
ChatGPT export ZIP    ─┤  parse → scrub → embed   personal.*  ──┐  search_personal_memory
claude.ai export ZIP  ─┼─────────────────────────▶ (your        ├─ search_conversations
Codex CLI history     ─┘                           PostgreSQL)  ├─ list_recent_conversations
                                                                ┘  get_conversation ...
agent memory files    ───  nightly dub (opt-in) ─▶ agent.*    ──── search_ghost_memory
```

## クイックスタート

前提条件: Python 3.11+、PATH 上の `psql` クライアント（Debian/Ubuntu:
`apt-get install postgresql-client`）、そして Docker または pgvector 拡張を
備えた既存の PostgreSQL のいずれか。

デフォルトではすべてがあなたのマシン上で動作します。データベースは同梱の
docker-compose postgres であり、`hippocampus init` がそのセットアップを
行います。

```bash
git clone <this-repo> hippocampus-mcp && cd hippocampus-mcp

# 1. パッケージをインストール
pip install .

# 2. 初回セットアップ。データベースは "local"（デフォルト）を選び、embed
#    バックエンドを選択し、必要なら ghost layer を構成する。init は DB
#    パスワードを生成し、.env（mode 0600）を書き込み、compose postgres を
#    起動し、migration を実行し、MCP 登録用スニペットを出力する。
hippocampus init

# 3. 同梱のローカル BGE-M3 サーバー経由のセマンティック検索（推奨）:
#    init で "bge-http" + http://localhost:8086 を選び、起動する。
#    compose は init が .env に書き込んだトークンを読む（初回起動時に約 6 GB のモデル）
docker compose --profile bge up -d

# 4. 検証してから Claude Code セッションを ingest
hippocampus doctor
hippocampus ingest claude-code
```

非対話・最小インストール（embed モデルなし。バックエンドが構成されるまで
セマンティックツールは隠され、ingest も実行を拒否します。ベクトルは
テキストと同時に書き込まれ、後から黙って埋め戻されることはありません）:

```bash
hippocampus init --yes --embed none
```

ホストのポート 5432 が使われている場合（ホストの postgres、または WSL2 下で
Windows 側のリスナーがいる場合）、`--pg-port <free-port>` を渡してください。
compose と生成される `PG_URL` は `.env` 経由でそれに従います。

**データベースを別サーバーで動かしますか？** データベースのプロンプトで
`existing` を選ぶ（または `--db existing`）と、PostgreSQL の URL を貼り付け
られます。INSTALL.md の Path B を参照してください。リモートデータベースが
何を意味するか（あなたの会話テキストがネットワークを通過します。プライベート
ネットワーク内か TLS の背後に置いてください）は PRIVACY.md を参照してください。
ローカルが推奨デフォルトです。

### MCP サーバーを登録する

`~/.claude/settings.json`（またはお使いのクライアントの MCP 設定）に追加
します。このスニペットにシークレットは含まれません。サーバーは作業ディレクトリ
から `.env` を読み込みます:

```json
{
  "mcpServers": {
    "hippocampus": {
      "command": "/path/to/your/venv/bin/hippocampus-mcp"
    }
  }
}
```

お使いの MCP クライアントがプロジェクトディレクトリからサーバーを起動しない
場合は、`hippocampus init` が実行の最後に出力する 1 行の `cd && exec`
ラッパーを使ってください。

その後、新しいエージェントセッションから:

```
search_personal_memory("that postgres deadlock we debugged")
list_recent_conversations(days=2)
get_conversation("claude_code:<conv-id>")
search_ghost_memory(current_project="my-repo")   # ghost layer, if enabled
```

## Ingest ソース

4 つのソースが組み込まれています（`hippocampus ingest --list`）:

| ソース | コマンド | 入力 |
|---|---|---|
| Claude Code | `hippocampus ingest claude-code` | `~/.claude/projects/` を自動検出（上書き: `CLAUDE_DIR`）。差分対応 — いつでも再実行可 |
| ChatGPT | `hippocampus ingest chatgpt /path/to/export.zip` | 公式データエクスポート ZIP |
| claude.ai | `hippocampus ingest claude-ai /path/to/data-XXXX.zip` | 公式データエクスポート ZIP |
| Codex CLI | `hippocampus ingest codex` | `~/.codex/history.jsonl`（上書き: `CODEX_HISTORY_FILE`）。既知の制約: 既に ingest 済みのセッションに追記された行は再読み込みされない |

すべてのソースは同じパイプラインを通ります: parse → クレデンシャル scrub →
embed → upsert → verify（ingest されたメッセージがベクトルを持たないまま
終わった場合、実行は明示的に失敗します）。会話は重複排除されるため、ingest
の再実行は安全です。

ingest 後、`hippocampus summarize` は会話ごとのロールアップ要約と、長い会話の
セグメント要約（要約レベル検索の基盤）を構築します。これには Anthropic API
キー（`ANTHROPIC_API_KEY`）と動作する embed バックエンドが必要です。どの
テキストがどこに送られるかの正確な内容は [PRIVACY.md](PRIVACY.ja.md) を
参照してください。

## セマンティック検索バックエンド

セマンティック（ベクトル）検索は、**明示的にバックエンドを選ぶまでオフ**です。
モデルの黙ったダウンロードは行われません。`hippocampus init` で 3 つの選択肢が
あります（後から `.env` で変更可能）:

| 選択肢 | 意味 | コスト |
|---|---|---|
| `none` | キーワード/新着順ツールのみ。セマンティックツールは隠される | ゼロ |
| `bge-http` | HTTP 経由の BGE-M3 — `docker compose --profile bge up -d` が `localhost:8086` に 1 つ起動する。または `BGE_EMBED_URL` を自前のものに向ける | コンテナ内で約 6 GB の RAM |
| `bge-inprocess` | サーバープロセス内にモデルを読み込む（`pip install 'hippocampus-mcp[bge-local]'`） | プロセス内で約 6 GB の RAM、初回約 6 GB のダウンロード |

詳細と判断表: [INSTALL.md](INSTALL.ja.md)。

## Ghost layer（プロジェクト横断のエージェントメモリ）

プロジェクトローカルのエージェントメモリファイルは、明示的なデュアルシグナルの
opt-in（frontmatter の `scope: shared` **かつ**人間が編集する allowlist
ファイルへの 1 行）によって、任意のプロジェクトのセッションが
`search_ghost_memory` で検索できる共有保管庫へと昇格できます。昇格は
デフォルト拒否です。2 つのシグナルの背後にある第三の壁としてコンテンツ
スキャナーがあります。

`hippocampus init --ghost` は、必要な読み取り専用データベースロールを
プロビジョニングします。完全なユーザーガイド:
[docs/GHOST_LAYER_USER.md](docs/GHOST_LAYER_USER.ja.md)。

## claude.ai コネクタ（web・モバイルから使う）

stdio の MCP サーバーはターミナルのエージェントにしか届きません。**claude.ai の
web アプリやスマホアプリ**からメモリを検索するには、任意の**コネクタ**を動かします。
同じツール群を streamable HTTP 経由で、単一オーナー用の **OAuth** 認可サーバーの
背後で提供する第 2 のエントリポイント（`hippocampus-mcp-connector-oauth`）で、
cloudflared トンネル経由で公開します。

stdio の面より意図的に狭い設計です — fail-closed な**読み取り専用 allowlist**
（personal/conversation/library 検索のみ。ghost・facts・スレッド全文取得は除外）、
**audience バインド**されたトークン、chain-read バジェット、fail-open な読み取り監査。
claude.ai のコネクタ設定に一度登録すれば、すべての端末から使えます。

セットアップ・セキュリティ姿勢・トラブルシューティング:
[docs/CONNECTOR.md](docs/CONNECTOR.ja.md)。

## プライバシー

要約すると: あなたの会話テキスト全文とそのベクトルは、*あなたの* PostgreSQL に
存在します。それを必要とする機能を明示的に有効化しない限り（Anthropic による
スコアリング/要約、リモートの embed エンドポイント）、何もあなたのマシンから
外に出ません。ingest 時のクレデンシャル scrub は**ベストエフォートであり、
保証ではありません**。機微なものを ingest する前に [PRIVACY.md](PRIVACY.ja.md)
を読んでください。

## サポートモデル

これは**有用な基盤として公開されており、サポートされる製品ではありません**。
作者自身が実際に毎日使っているメモリシステムを、インストール可能な形に切り
出したものです。Issue と PR は歓迎し、ベストエフォートで対応します。SLA は
なく、ロードマップの約束もなく、API はマイナーバージョン間で変わる可能性が
あります。壊れた場合は、`hippocampus doctor` の出力（貼り付けても安全になる
よう設計されており、シークレットは一切現れません）がレポートに含めるのに最も
有用です。

## ドキュメント

- [INSTALL.md](INSTALL.ja.md) — 詳細セットアップ: compose vs 既存 PG、embed バックエンド、migration、トラブルシューティング、自動化
- [PRIVACY.md](PRIVACY.ja.md) — 何が保存されるか、何がいつ箱の外に出るか、scrub の限界、プロンプトインジェクションへの姿勢
- [docs/GHOST_LAYER_USER.md](docs/GHOST_LAYER_USER.ja.md) — ghost layer ユーザーガイド
- [docs/CONNECTOR.md](docs/CONNECTOR.ja.md) — claude.ai リモート MCP コネクタ（web・モバイルからメモリを使う）
- [docs/SECRETS_HARDENED.md](docs/SECRETS_HARDENED.ja.md) — 任意の sops 暗号化シークレット構成（デフォルトはプレーンな `.env`、mode 0600）
- [docs/CONFIG.md](docs/CONFIG.ja.md) — 環境変数の完全リファレンス
