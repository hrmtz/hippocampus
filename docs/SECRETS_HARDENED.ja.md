[English](SECRETS_HARDENED.md) ・ **日本語**

# secrets のハードニング: プレーンな `.env` の代わりに sops を使う

**デフォルト** の secrets の扱いは、`hippocampus init` が書き込み、
`hippocampus doctor` がチェックする、モード 0600 のプレーンな `.env`
ファイルです。フルディスク暗号化されたシングルユーザーのマシンであれば、
これで十分です。

この補遺は、secrets を **保存時に暗号化** したいセットアップ向けです ——
共有マシン、同期/バックアップされるプロジェクトディレクトリ、あるいは単に
より厳格な姿勢を取りたい場合です。[sops](https://github.com/getsops/sops) と
[age](https://github.com/FiloSottile/age) キーを使います。hippocampus の中で
sops を必須とするものは何もありません。統合ポイントは純粋に「プロセスに環境
変数を注入する」ことだけで、これは `Settings` がすでに `.env` よりも優先する
仕組みです (プロセス環境が常に勝ちます)。

## 何よりも優先する 1 つのルール

> **ラッパーは、stdout へ復号することなく env を注入しなければなりません。**

あなたがタイプしたりスクリプトに書いたりしてよい sops の呼び出しは、次の
2 つだけです:

```bash
sops edit secrets.enc.yaml                 # edit (decrypts into $EDITOR, re-encrypts on save)
sops exec-env secrets.enc.yaml '<command>' # use (decrypted values go into the child's env only)
```

`sops -d … | …` という形のものはすべて、平文の secrets を stdout に乗せます
—— あなたのスクロールバック、シェル履歴、ターミナルロガー、そして (AI エージェ
ントがターミナルを操作している場合は) 無期限に保持されうる会話トランスクリプト
へ。一度 secret が印字されてしまうと、それを取り消すことはできません。修正手段
は rotation であって削除ではありません。したがって: `sops -d` は禁止、復号した
出力を `cat`/`grep`/`head` にパイプするのも禁止、`export $(sops -d …)` も禁止
です。キーが存在するかどうかを確認したい場合は、キーの *名前* だけを列挙します:

```bash
sops exec-env secrets.enc.yaml 'env | cut -d= -f1 | sort'
```

## age キーのセットアップ (概略)

```bash
# 1. キーペアを生成 (このファイルは厳重に保管すること。すべてを復号できる)
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
# 印字された公開鍵を控えておく: age1...

# 2. どの recipient がどのファイルを暗号化するかを sops に伝える — secrets の隣に .sops.yaml を置く
cat > .sops.yaml <<'EOF'
creation_rules:
  - path_regex: secrets\.enc\.yaml$
    age: age1YOUR_PUBLIC_KEY_HERE
EOF

# 3. 暗号化ファイルを作成
cat > /tmp/seed.yaml <<'EOF'
PG_URL: postgresql://hippocampus:CHANGE_ME@localhost:5432/hippocampus
BGE_EMBED_URL: http://localhost:8086
BGE_EMBED_TOKEN: CHANGE_ME
EOF
sops --encrypt /tmp/seed.yaml > secrets.enc.yaml && shred -u /tmp/seed.yaml
sops edit secrets.enc.yaml      # 以降の編集はすべてこれを通す
```

暗号化された `secrets.enc.yaml` はディスクに保存しても安全ですが、それでも
git-ignore しておくのが良い衛生習慣です —— 暗号文をコミットすると、リポジトリの
安全性が 1 つの age キーの生涯にわたる秘匿性に縛り付けられます。age 秘密鍵
(`keys.txt`) は、暗号文と運命を共にするあらゆるリポジトリ、あらゆるバックアップ
から外しておきましょう。

## hippocampus を sops 経由で結線する

プレーンな `.env` を exec-env ラッパーに置き換えます。優先順位の設計が残りを
やってくれます。プロセス環境に注入された値は、作業ディレクトリにある迷子の
`.env` を上書きします。

**MCP サーバー** — むき出しのエントリポイントの代わりに、小さなラッパー
スクリプトを登録します:

```bash
cat > ~/.local/bin/hippocampus-mcp-sops <<'EOF'
#!/bin/sh
cd /path/to/hippocampus-mcp && exec sops exec-env secrets.enc.yaml \
  '/path/to/venv/bin/hippocampus-mcp'
EOF
chmod +x ~/.local/bin/hippocampus-mcp-sops
```

```json
{
  "mcpServers": {
    "hippocampus": { "command": "/home/YOU/.local/bin/hippocampus-mcp-sops" }
  }
}
```

**CLI / ingest / cron** — 同じパターンです:

```bash
sops exec-env secrets.enc.yaml 'hippocampus doctor'
sops exec-env secrets.enc.yaml 'hippocampus ingest claude-code'
sops exec-env secrets.enc.yaml 'hippocampus migrate --status'
```

cron では、sops にキーの場所を明示的に指し示します (cron の環境は最小限です):

```cron
0 3 * * * cd /path/to/hippocampus-mcp && SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt flock -n /tmp/hippocampus_ingest.lock -c "sops exec-env secrets.enc.yaml 'hippocampus ingest claude-code'" >> $HOME/hippocampus-ingest.log 2>&1
```

このモードでは `hippocampus init` は不要です (その仕事は `.env` を書くこと
です)。必要に応じて `hippocampus migrate` / `hippocampus init --skip-migrations`
の各処理をラッパーの下で実行します。`doctor` は `.env: not found in cwd
(process env only — fine under a secrets wrapper)` と報告します —— その行は
想定どおりであり、失敗ではありません。

## 注意点と鋭いエッジ

- **内側のパイプは問題ないが、外側のパイプはダメ。**
  `sops exec-env f 'pg_dump … | gzip > out.gz'` は secrets を子環境の内側に
  とどめます。`sops -d f | anything` はそうではありません。
- **「検証」のために値を echo しない。** exec-env シェルの中で `echo $PG_URL`
  すると secret が印字されます。どうしても検証が必要なら、boolean を検証
  します: `sops exec-env f 'test -n "$PG_URL" && echo set'`。
- **キーの喪失 = コーパス設定の喪失であって、コーパスの喪失ではない。**
  データベース自体は sops で暗号化されていません。age キーを失っても、ロック
  アウトされるのは設定ファイルからだけです。それでも: キーは暗号文とは別に
  バックアップしておきましょう。
- **rotation**: もし secret がトランスクリプトやログに着地してしまったら、
  source で rotate します (データベース role のパスワード、API キー) ——
  あとから sops ファイルを編集しても、守れるのは未来だけです。
- 複数マシン: マシンごとに age recipient を 1 つ `.sops.yaml` に追加し
  (カンマ区切り)、`sops updatekeys secrets.enc.yaml` で再暗号化し、ファイルを
  帯域外で運搬します。
