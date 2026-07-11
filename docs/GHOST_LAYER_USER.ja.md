[English](GHOST_LAYER_USER.md) ・ **日本語**

# ghost layer — ユーザーガイド

プロジェクト横断のエージェントメモリ vault の使い方です。(設計の背景や
スキーマの内部仕様は [GHOST_LAYER_DESIGN.md](design-history/GHOST_LAYER_DESIGN.md)
にあります。利用するだけなら読む必要はありません。)

## これは何か

コーディングエージェントは、プロジェクトごとにメモリファイルを蓄積していきます
(`~/.claude/projects/<hash>/memory/*.md`)。インシデントから学んだルール、
ユーザーの好み、繰り返すミスのメモなどです。デフォルトでは、各プロジェクトの
メモリは他のすべてのプロジェクトからは見えません。エージェントはどのリポジトリ
でも同じ教訓を学び直すことになります。

ghost layer は、**あなたが選んだ** メモリについてこれを解決します。毎晩実行
される「dub」ジョブが、明示的に昇格させたメモリファイルを専用の PostgreSQL
スキーマ (`agent.ghost_memories`) にコピーして embed し、`search_ghost_memory`
MCP ツール経由で *どの* プロジェクトのセッションからも検索可能にします。元の
ファイルは一切変更されません。vault は読み取り専用のミラーであり、各エントリ
には `source_project` タグが付くため、ルールがどこ由来なのかが常にわかります。

これは、あなたの会話アーカイブとは別のコーパスであり、トラストの姿勢も異なり
ます。personal memory は *recall* (「X についてどう考えていたか」) であり、
ghost memory は *エージェントが自分自身に与えた常設の指示* です。だからこそ、
昇格は意図的に高い摩擦を持たせてあります。

## 昇格は opt-in、デュアルシグナル、デフォルト拒否

メモリファイルは、**両方のシグナルが揃って初めて** dub されます。

**シグナル 1 — メモリファイル自体の frontmatter**:

```markdown
---
name: feedback-always-pin-versions
description: lockfiles saved us twice, never install unpinned
metadata:
  type: feedback
  scope: shared
---
(body)
```

**シグナル 2 — 人間が編集する allowlist ファイル**
`~/.claude/ghost_promote_allowlist.txt` の 1 行 (フォーマットは
`<source_project>/<memory_slug>`、デフォルト拒否):

```
# one promoted memory per line
my-webapp/feedback_always_pin_versions
dotfiles/user_prefers_tabs
```

どちらか一方のシグナルだけでは何も起きません (ログには残りますが dub は
されません)。この分割が存在するのは、エージェントが自分で frontmatter を
書くからです。もし `scope: shared` だけでメモリを公開できてしまうと、
エージェントがあなたを介さずにコンテンツを昇格できてしまいます。allowlist
ファイルが human-in-the-loop の役割を果たします。

**第三の壁**: dub の直前に本文をスキャンするコンテンツスキャナーが走り、
両方のシグナルが揃っていても、明らかに credential らしい形のものや、
その他ブロックリストに該当するコンテンツは拒否します。スキャナーによる拒否
は監査ログに記録されます (`rejected_content_scan`)。誤検知は、slug ごとの
明示的なオーバーライドファイルで上書きできます。

`scope: shared-restricted` というティアもあります (すべてのプロジェクト
ではなく、列挙された特定のプロジェクトのリストにのみ可視となるメモリ)。
ただし restricted dub のサポートはまだゲートされています。依存する前に
dub 実行の出力を確認してください。

## セットアップ

1. **スキーマ**: コアの migration ティアにはすでに ghost layer が含まれて
   います (`hippocampus migrate` — 追加で適用するものはありません)。
2. **reader role**: MCP サーバーは専用の読み取り専用 PG role 経由で vault
   を読みます。次のコマンドでプロビジョニングします:

   ```bash
   hippocampus init --ghost
   ```

   これにより `agent_read_mcp` role (migration 009 で作成) に生成された
   パスワードが設定され、`PG_URL_AGENT_READ_MCP` が `.env` に書き込まれ
   ます。パスワードが出力されることはありません。`hippocampus doctor` は、
   この role が接続でき、ランク付け検索関数を解決できることを検証します。
3. **dub ジョブ**: dub スクリプトを手動で、または毎晩の cron から実行します:

   ```bash
   # the dub writer role needs its own DSN; the host gate must name your machine
   export GHOST_ALLOWED_HOSTS="$(hostname)"
   export PG_URL_AGENT_DUB='postgresql://agent_dub:...@localhost:5432/hippocampus'
   python3 scripts/dub_agent_memories.py --dry-run --verbose   # preview
   python3 scripts/dub_agent_memories.py                       # real run
   ```

   `--dry-run` は、どのファイルが dub される/スキップされる/拒否されるか、
   そしてその理由を正確に表示します。dub には動作している embed バック
   エンドが必要です。

## どのプロジェクトからでも検索する

一度 dub されると、hippocampus MCP サーバーを持つすべてのエージェント
セッションで次が使えます:

```
search_ghost_memory(query="postgres migration locking", current_project="my-webapp")
search_ghost_memory(current_project="my-webapp")          # empty query = vault overview
```

- 空クエリは、最上位ランクのメモリを一覧します ——「自分の ghost vault に
  何があるか」です。
- ランク付けは、意味的類似度、全文一致、新しさ、そして自己調整する有用度
  スコアをブレンドします。検索で繰り返し浮上するメモリは上がり、訂正された
  ものは沈みます。
- `current_project` は **呼び出し側の自己申告であり、検証されません**。これは
  `shared-restricted` の可視性をスコープするためのもので、自分のマシン上の
  悪意ある呼び出し側に対するセキュリティ境界として扱わないでください。
- embed バックエンドがダウンしている場合、ツールはテキストのみのランク付け
  に degrade し、失敗する代わりに警告ヘッダーでその旨を伝えます。

## メモリの削除 (purge のやり方)

何をしたいかに応じて 3 段階あります。

1. **今後の更新を止める**: `~/.claude/ghost_promote_allowlist.txt` から該当
   行を削除します (または、ファイルから `scope: shared` を外します)。vault
   のコピーは残りますが、二度とリフレッシュされません。
2. **vault から削除する**: allowlist から削除し、**かつ** 行を削除します。
   さもないと次の毎晩の dub が復活させてしまいます:

   ```sql
   DELETE FROM agent.ghost_memories
    WHERE source_project = 'my-webapp' AND memory_slug = 'feedback_old_rule';
   ```

3. **恒久的に削除する (tombstone)**: さらに tombstone 行を挿入します。dub
   ジョブはこれをチェックし、たとえシグナルが再び現れても、その
   `(project, slug)` ペアを二度と再 dub しません:

   ```sql
   INSERT INTO agent.ghost_purge_tombstone (source_project, memory_slug, reason, purged_by)
   VALUES ('my-webapp', 'feedback_old_rule', 'no longer true', current_user);
   ```

   tombstone テーブルは設計上 append-only です (フォレンジック記録)。
   tombstone の挿入には purge-admin role が必要です。

すべての dub アクション (dubbed / skipped / rejected / purged-skip) は
`agent.ghost_dub_log` に記録されるため、「なぜこのメモリが vault にある/ない
のか」は監査証跡から常に回答可能です。

## オプション: SessionStart インジェクション

オンデマンド検索に加えて、SessionStart フックが、関連する ghost メモリを
いくつか各新規セッションに自動でインジェクトできます
(`scripts/ghost_context_inject.py`; 結線の例は
[GHOST_LAYER_DESIGN.md](design-history/GHOST_LAYER_DESIGN.md) §6)。kill switch:
`HIPPOCAMPUS_GHOST_DISABLE=1` でそのセッションのインジェクションを無効化
します。このフックは fail-open-to-empty です。セッション起動をブロックする
ことは決してありません。
