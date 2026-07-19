[English](BGE_ONDEMAND.md) ・ **日本語**

# BGE on-demand 実装

この文書は `EMBED_PROVIDER=bge-ondemand` のコードレベル契約です。
`v2.0.0` で入った実装を記述しています。更新時は
`src/hippocampus/embed/{client,ondemand,server}.py`,
`src/hippocampus/setup_init.py`, `src/hippocampus/doctor.py`,
`compose.yaml` と同期してください。

## 選択順

`EmbedClient` は次の順で backend を選びます。

1. `BGE_EMBED_URL` が設定されていれば、その HTTP backend を直接使う。
2. そうでなく `EMBED_PROVIDER=bge-ondemand` なら、on-demand supervisor で
   endpoint を解決し、HTTP `/embed` または `/embed_batch` を呼ぶ。
3. そうでなく `EMBED_PROVIDER=bge-inprocess` なら、現在の Python process 内で
   BGE-M3 を load する。
4. それ以外は `EmbedClientError`。暗黙の model download はしない。

on-demand 分岐は `BGE_EMBED_URL`、`BGE_EMBED_TOKEN`、process-wide な
`EmbedClient` singleton を変更しません。各 `encode()` / `encode_batch()` 呼び出しが
supervisor へ現在の endpoint を問い合わせ、その URL/token をその HTTP request
だけに渡します。

## Init

`hippocampus init --embed bge-ondemand` は次を書きます。

- `EMBED_PROVIDER=bge-ondemand`
- `BGE_EMBED_TOKEN=<token>`
- `BGE_ONDEMAND_IDLE_SECONDS=<seconds>`。デフォルト `300`、最小 `30`
- `BGE_RESTART_POLICY=no`
- `HIPPOCAMPUS_COMPOSE_DIR=<init cwd>`

`--bge-token-env` が渡された場合、init はその env var から token を読みます。
渡されない場合は `read_or_create_token()` を使い、on-demand state directory 配下に
machine-local token を保存します。token file は mode `0600`、state directory は
mode `0700` です。

`HIPPOCAMPUS_COMPOSE_DIR` は重要です。後続の semantic work は cron、MCP、
別 cwd から起動されることがあるためです。supervisor はこの directory で
Docker Compose を起動し、そこに `compose.yaml` が存在することを要求します。

## State と Lock

デフォルトの state directory は次です。

```text
${XDG_STATE_HOME:-$HOME/.local/state}/hippocampus/embed-ondemand/
```

`HIPPOCAMPUS_BGE_ONDEMAND_STATE_DIR` で override できます。
`HOME` も `XDG_STATE_HOME` もなく、override もない場合、supervisor は
`OnDemandError` を送出します。

state directory 内の file:

- `token`: `read_or_create_token()` が作る bearer token
- `state.json`: supervisor の最後の状態
- `lock`: supervisor start を serialize する `fcntl.flock()` lock

`state.json` は同一 directory の temporary file 経由で atomic に書きます。
payload には `updated_at` と、状態に応じた `state`, `url`, `idle_seconds`,
`compose_dir`, `config_hash`, `started_at`, `last_health_at`, `last_error`
などが入ります。

`config_hash` は次を SHA-256 化した値です。

- endpoint URL
- idle seconds
- compose directory
- bearer token の SHA-256

これは診断用に記録されます。現実装は Compose reconcile 前に config-hash mismatch
を拒否しません。

## Startup Algorithm

`ensure_endpoint()` は supervisor lock の下で次を実行します。

1. `endpoint_url()` で URL を解決する。
2. `BGE_EMBED_TOKEN` または `read_or_create_token()` で token を解決する。
3. `BGE_ONDEMAND_IDLE_SECONDS` または default `300` で idle seconds を解決する。
4. `HIPPOCAMPUS_COMPOSE_DIR` または current working directory で compose
   directory を解決する。
5. `state=starting` を書く。
6. 次を実行する。

   ```bash
   docker compose --profile bge up -d bge
   ```

   subprocess env には常に次が入ります。

   - `BGE_EMBED_TOKEN`
   - `BGE_ONDEMAND_IDLE_SECONDS`
   - `BGE_RESTART_POLICY=no`
   - `BGE_ONDEMAND_PORT=<endpoint URL から parse した port>`
   - `PG_PASSWORD=unused-for-bge-ondemand`。ただし `PG_PASSWORD` が未設定の時だけ。
     これにより、Postgres password を要求せず `bge` profile だけ起動できます。

7. 認証付き `GET /ready` が `200` かつ `ok=true` / `model_loaded=true` を返すまで、
   または startup timeout まで poll する。
8. `/ready` が未 ready の間は、認証なし `GET /health` で `loading` と `starting`
   を区別する。
9. 成功時は `state=hot` を書き、`Endpoint(url, token)` を返す。
10. timeout 時は `state=failed` と `last_error="startup timed out"` を書き、
    `OnDemandError` を送出する。

`BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS` が timeout を制御します。デフォルトは `900`、
最小値は `10` です。

## Endpoint 解決と Port Fallback

`endpoint_url()` は次の順で supervisor endpoint を解決します。

1. `BGE_ONDEMAND_URL`。末尾 slash は削る。
2. `BGE_ONDEMAND_PORT`。`1..65535` の integer として検証し、
   `http://127.0.0.1:<port>` にする。
3. default `http://127.0.0.1:8086`

`docker compose up` が `address already in use` を含む error で失敗し、
かつ `BGE_ONDEMAND_URL` も `BGE_ONDEMAND_PORT` も設定されていない場合、
supervisor は空いている loopback port で一度だけ retry します。retry URL は
`state.json` に書かれます。auto-port mode のままなら、以後の passive doctor status
はその記録済み URL を優先します。

`BGE_ONDEMAND_URL` または `BGE_ONDEMAND_PORT` が明示設定されている場合、
自動 port retry はせず、passive status は `state.json` の古い URL ではなく
明示設定 URL を使います。

## Compose Service

`bge` compose service は次を publish します。

```yaml
127.0.0.1:${BGE_ONDEMAND_PORT:-8086}:8086
```

on-demand 起動では supervisor が `BGE_RESTART_POLICY=no` を設定します。
manual `bge-http` 利用では compose default は `unless-stopped` のままです。

service は container 内へ次の env var を渡します。

- `BGE_EMBED_TOKEN`
- `BGE_EMBED_PORT=8086`
- `BGE_EMBED_HOST=0.0.0.0`
- `BGE_ONDEMAND_IDLE_SECONDS`
- `HF_HOME=/hf_cache`

HuggingFace cache は Docker volume `hf_cache` で、container 内 `/hf_cache` に
mount されます。壊れた model cache を復旧する時に `pg_data` は消さないでください。

## Embed Server Contract

`src/hippocampus/embed/server.py` は次を公開します。

- `GET /health`: 認証なし。`ok`, `model_loaded`, `active_request_count`,
  `last_completed_at`, `idle_seconds` を返す。
- `GET /ready`: bearer 認証あり。`/health` と同じ fields を返す。
- `POST /embed`: bearer 認証あり。`{"query": str, "max_length": int}` を受け取る。
- `POST /embed_batch`: bearer 認証あり。
  `{"texts": list[str], "max_length": int}` を受け取る。

`/ready` は `/embed` / `/embed_batch` と同じ bearer token check を使います。
bearer credentials が missing/wrong の場合は FastAPI/HTTPBearer の auth failure path
になります。

server は FastAPI lifespan startup 中に `BAAI/bge-m3` を load します。
device selection は `cuda`、`mps`、`cpu` の順です。model load 中の stdout は
stderr に redirect します。返す dense vector はすべて normalization boundary で
検査されます。

## Idle Self-Exit

idle shutdown は embed server 自身が所有します。caller は短命な CLI や cron process
のことがあり、その process が終了した後は BGE container を reap する parent
supervisor が残らないためです。

`BGE_ONDEMAND_IDLE_SECONDS` が未設定または invalid の場合、server 側の idle exit は
無効です。正の integer の場合:

- daemon thread が `min(30, max(1, idle // 3))` 秒ごとに wake する。
- active な `/embed` / `/embed_batch` request は process-local lock の下で数える。
- `active_request_count > 0` なら shutdown を skip する。
- `LAST_COMPLETED_AT` からの経過が idle timeout 未満なら shutdown を skip する。
- それ以外なら `uvicorn.Server.should_exit = True` を設定する。

shutdown は Uvicorn 視点では graceful です。child は終了時に terminal state marker を
書きません。古い `hot`, `running`, `starting`, `loading` state は passive status
probe 側で処理します。

## Doctor Status

`hippocampus doctor` は `bge-ondemand` では passive です。`/embed` を呼ばず、
Docker Compose も起動しません。

`doctor` は `passive_status(token=...)` を呼び、次のように表示します。

- `OK`: 認証付き `/ready` が成功した時、
  `bge-ondemand hot ... (/ready verified)`
- `INFO`: 認証なし `/health` は成功するが `/ready` が verified ではない時、
  `bge-ondemand running ... (passive status; no /embed probe)`
- `INFO`: state がない、または古い active state で reachable server がない時、
  `bge-ondemand cold ...`
- `FAIL`: state が `failed` の時、`bge-ondemand failed ...`
- `FAIL`: bad port / bad idle timeout など local configuration が invalid な時、
  `bge-ondemand config error ...`

古い active state の場合、passive status は `state=cold` と `last_state` を返します。
そのため doctor は前回 state が `hot`, `running`, `starting`, `loading` のいずれ
だったかを表示できます。

## Manual Low-Memory Mode

manual low-memory mode は通常の `bge-http` です。`BGE_EMBED_URL` と
`BGE_EMBED_TOKEN` を設定したまま、semantic work の前に local compose service を起動し、
終わったら停止します。

```bash
docker compose --profile bge up -d
hippocampus ingest codex
docker compose stop bge
```

`BGE_EMBED_URL` が停止中の local server を指している場合、semantic ingest/search は
fail-loud します。この mode ではそれが期待挙動です。

## Failure Modes

- Docker がない: `OnDemandError` は Docker install、`--embed none`、remote `bge-http`
  のいずれかを案内する。
- `compose.yaml` がない: `HIPPOCAMPUS_COMPOSE_DIR` を設定するか repo から実行する。
- invalid `BGE_ONDEMAND_IDLE_SECONDS`: integer `>= 30` が必要。
- invalid `BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS`: integer `>= 10` が必要。
- invalid `BGE_ONDEMAND_PORT`: integer `1..65535` が必要。
- startup timeout: 初回 model download/load が継続中の可能性がある。
  `docker compose logs bge` を確認し、retry するか
  `BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS` を増やす。
- model cache の中断/破損: `bge` を停止して retry する。必要なら compose
  `hf_cache` volume だけを削除する。

## Tests

`scripts/test_ondemand_embed.py` は Docker / BGE-M3 を起動せずに pure-Python contract
を検証します。

- `Settings` が `bge-ondemand` を受け入れる。
- `embed_configured` が true になる。
- init が期待 env keys を書く。
- `EmbedClient` が `self.url` を mutate せず `ensure_endpoint()` 経由で route する。
- supervisor startup と compose reconciliation path。
- automatic alternate-port retry。
- doctor passive cold/failed status。
- stale fallback URL handling。
- authenticated `/ready`。

`v2.0.0` の fresh-machine smoke では real BGE-M3 encode も確認しています。
`EmbedClient().encode("ping")` が normalized 1024-dimensional vector を返し、
container は idle timeout 後に exit しました。
