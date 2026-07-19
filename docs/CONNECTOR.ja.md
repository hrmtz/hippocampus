# claude.ai コネクタ（OAuth 越しのリモート MCP）

個人メモリのコーパスを、ターミナルのエージェントからだけでなく **claude.ai の
web・モバイル**からも引けるようにします。コネクタは同じ検索ツール群を、claude.ai が
HTTPS で対話するリモート MCP サーバーとして公開し、単一オーナー用の OAuth 認可
サーバーでゲートします。

一度登録すれば、web でもスマホ（iOS/Android アプリ）でも「先月 X について何を
決めたっけ？」と聞くだけで、どこにいても `search_personal_memory` があなたの
データベースを検索します。

> 設計と背景: [designs/claude-ai-connector-oauth.md](designs/claude-ai-connector-oauth.md)。
> オペレータ向けデプロイ手順（デプロイ固有、gitignore 済）:
> `docs/operator/CONNECTOR_DEPLOY_RUNBOOK.md`。

## stdio MCP サーバーとの違い

通常の `hippocampus-mcp` サーバーは **stdio** を話します — ローカルのエージェント
（Claude Code、Codex）がサブプロセスとして起動します。claude.ai の web/モバイル
アプリはローカルプロセスを起動できず、HTTPS で届く**リモート MCP** コネクタしか
サポートしません。そこでコネクタは、*同じ*ツールとデータベースの周りにある第 2 の
エントリポイントになります:

| | stdio サーバー | コネクタ |
|---|---|---|
| エントリポイント | `hippocampus-mcp` | `hippocampus-mcp-connector-oauth` |
| トランスポート | stdio | streamable HTTP |
| 認証 | なし（ローカル） | OAuth（単一オーナー） |
| 到達元 | Claude Code / Codex / Desktop | claude.ai web + モバイル |
| ツール面 | ゲート済み全ツール | **読み取り専用 allowlist の部分集合** |

コネクタは**別プロセス**で動くため、stdio サーバーの挙動を変えることはありません。

## 何が公開され、何が公開されないか

コネクタは **fail-closed な allowlist** を提供します — 次の読み取りツールのみ:

- `search_personal_memory`、`search_conversations`、`search_library`
- `list_recent_conversations`、`list_project_conversations`
- `get_conversation_summary`
- `get_diary`、`search_diary` — エージェントの日次一人称日記。opt-in 公開:
  日記は会話スニペットより sensitive(セキュリティ濃い日の内省を含む)なので、
  公開面への露出はデフォルトでなく**オペレータの意図的な選択**です。日記を
  stdio 限定に戻すには `CONNECTOR_TOOL_ALLOWLIST` から外します。

意図的に**除外**（stdio 側には残る）:

- `get_conversation` — スレッド全文取得。情報流出のサイドチャネル
- `search_ghost_memory`、`search_facts` — 認可（ghost/facts）ティア

プロセス単位の **chain-read バジェット**（デフォルト 40 回 / 300 秒）がスイープを
抑え、提供された各読み取りは fail-open な**監査**行（ツール名 + 引数ダイジェスト。
クエリ本文は記録しない）を書きます。

## セキュリティ姿勢

- **単一オーナー。** OAuth クライアントは 1 つの static クライアントで、動的
  クライアント登録（DCR）は無効。許可されるリダイレクトは claude.ai の公式
  コールバックのみ。
- **audience バインドされたトークン。** すべてのトークンはこのサーバーの `/mcp`
  リソースに固定され、別リソース向けに発行されたトークンは拒否されます。
- **`/mcp` は fail-closed。** 有効・未失効・audience の正しいトークンがなければ
  `401`。OAuth の儀式用エンドポイント（metadata、`/authorize`、`/token`）はフローの
  都合上、意図的に無認証で到達可能です。
- **トークンは不透明・保存時ハッシュ化**。リフレッシュトークンは再利用検知付きで
  ローテーションします。
- 公開は **cloudflared トンネル**経由のみ（オリジンのポートは非公開）。任意で
  `/authorize` の前に **Cloudflare Access**（path 完全一致）を置き、人間の SSO
  壁とする（多層防御）。

単一オーナーの注意点: 現状コネクタは個人コーパス*全体*をフラットに公開します。
ペルソナ単位・テナント単位のスコープ分離は将来の課題です（federation 設計、gh #70）。

## セットアップ（概要）

完全な、デプロイ固有の手順はオペレータ runbook にあります。形はこうです:

1. 動かすホストで**エクストラをインストール**:
   ```bash
   pip install -e '.[connector]'          # mcp[cli]>=1.27 を pin
   ```
2. **static クライアント認証情報**をシークレットへ（claude.ai にも入力する値）:
   `HIPPOCAMPUS_CONNECTOR_CLIENT_ID` / `_CLIENT_SECRET`。
   生成例: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`。
3. systemd ユニットで**起動**:
   `systemd/hippocampus-mcp-connector.service` → `run_connector_oauth.sh`。
   `HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST=<公開ホスト>.example.com`（スキームなし）を
   drop-in で設定。ラッパーが `issuer=https://<host>` と
   `resource=https://<host>/mcp` を導出します。
4. **公開**: cloudflared ルート `<公開ホスト>` → `http://127.0.0.1:8092` と DNS
   レコード。（専用トンネルにすると他のトンネルから分離できます。）
5. **claude.ai に登録**（web）: 設定 → コネクタ → カスタムコネクタを追加 → URL
   `https://<公開ホスト>/mcp` → 詳細設定 → 手順 2 のクライアント id/secret →
   OAuth 完了。以後 web・モバイルから使えます。

## 環境変数

| 変数 | 用途 | デフォルト |
|---|---|---|
| `HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST` | 公開ホスト名（スキームなし）。issuer/resource を導出 | （OAuth に必須） |
| `HIPPOCAMPUS_CONNECTOR_CLIENT_ID` / `_CLIENT_SECRET` | static claude.ai クライアント | （OAuth に必須） |
| `HIPPOCAMPUS_CONNECTOR_PORT` | ローカルバインドポート | `8092` |
| `HIPPOCAMPUS_CONNECTOR_ACCESS_TTL` / `_REFRESH_TTL` | トークン寿命（秒） | `3600` / `2592000` |
| `HIPPOCAMPUS_CONNECTOR_BUDGET_MAX_CALLS` / `_WINDOW_S` | chain-read バジェット | `40` / `300` |

## トラブルシューティング

- **claude.ai:「トークン交換に失敗 / 連携が利用できません」。** たいていは OAuth
  後の `/mcp` 呼び出しが拒否されています。コネクタのジャーナルを確認:
  `421 "Invalid Host header"` は、公開ホスト名がアプリの DNS リバインディング
  allowlist にないという意味です。コネクタは issuer からこれを設定するので、
  `HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST` が登録した URL と一致しているか確認します。
- **ブラウザで「サイトに到達できない」が `curl` は通る。** 端末側の DNS ネガティブ
  キャッシュです。この URL は API エンドポイント（claude.ai のサーバーが取りに行く）で
  あってページではありません — `/mcp` をブラウザで開いても `401` が返るだけです。
- **`hippocampus-mcp-connector-oauth` がすぐ終了する。** OAuth 設定の欠落です —
  4 つの `HIPPOCAMPUS_CONNECTOR_*` OAuth 変数のいずれかが未設定だと fail-close します。
