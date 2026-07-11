**English** ・ [日本語](BGE_ONDEMAND.ja.md)

# BGE on-demand implementation

This document is the code-level contract for `EMBED_PROVIDER=bge-ondemand`.
It describes the implementation shipped in `v2.0.0`; keep it aligned with
`src/hippocampus/embed/{client,ondemand,server}.py`, `src/hippocampus/setup_init.py`,
`src/hippocampus/doctor.py`, and `compose.yaml`.

## Selection

`EmbedClient` selects a backend in this order:

1. If `BGE_EMBED_URL` is set, use the configured HTTP backend directly.
2. Else if `EMBED_PROVIDER=bge-ondemand`, call the on-demand supervisor and
   then use HTTP `/embed` or `/embed_batch`.
3. Else if `EMBED_PROVIDER=bge-inprocess`, load BGE-M3 in the current Python
   process.
4. Else raise `EmbedClientError`; there is no implicit model download.

The on-demand branch does not mutate `BGE_EMBED_URL`, `BGE_EMBED_TOKEN`, or the
process-wide `EmbedClient` singleton. Each `encode()` / `encode_batch()` call
asks the supervisor for a current endpoint and passes that URL/token only to
that HTTP request.

## Init

`hippocampus init --embed bge-ondemand` writes:

- `EMBED_PROVIDER=bge-ondemand`
- `BGE_EMBED_TOKEN=<token>`
- `BGE_ONDEMAND_IDLE_SECONDS=<seconds>`; default `300`, minimum `30`
- `BGE_RESTART_POLICY=no`
- `HIPPOCAMPUS_COMPOSE_DIR=<init cwd>`

If `--bge-token-env` is provided, init reads the token from that env var. If
not, init uses `read_or_create_token()` and stores a machine-local token under
the on-demand state directory. That token file is mode `0600`; the state
directory is mode `0700`.

`HIPPOCAMPUS_COMPOSE_DIR` is important because later semantic work may be
launched from cron, MCP, or another current working directory. The supervisor
starts Docker Compose from this directory and requires `compose.yaml` to exist
there.

## State And Locking

The default state directory is:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/hippocampus/embed-ondemand/
```

It can be overridden with `HIPPOCAMPUS_BGE_ONDEMAND_STATE_DIR`. If neither
`HOME` nor `XDG_STATE_HOME` is set and no override is provided, the supervisor
raises `OnDemandError`.

Files in the state directory:

- `token`: bearer token created by `read_or_create_token()`
- `state.json`: last supervisor state
- `lock`: `fcntl.flock()` lock used to serialize supervisor starts

`state.json` is written atomically through a same-directory temporary file. The
payload includes `updated_at` plus the state-specific fields such as `state`,
`url`, `idle_seconds`, `compose_dir`, `config_hash`, `started_at`,
`last_health_at`, or `last_error`.

The `config_hash` is a SHA-256 over:

- endpoint URL
- idle seconds
- compose directory
- SHA-256 of the bearer token

It is recorded for diagnostics; the current implementation does not reject a
config-hash mismatch before reconciling Compose.

## Startup Algorithm

`ensure_endpoint()` performs this sequence under the supervisor lock:

1. Resolve URL with `endpoint_url()`.
2. Resolve token from `BGE_EMBED_TOKEN` or `read_or_create_token()`.
3. Resolve idle seconds with `BGE_ONDEMAND_IDLE_SECONDS` or default `300`.
4. Resolve compose directory with `HIPPOCAMPUS_COMPOSE_DIR` or current working
   directory.
5. Write `state=starting`.
6. Run:

   ```bash
   docker compose --profile bge up -d bge
   ```

   The subprocess env always includes:

   - `BGE_EMBED_TOKEN`
   - `BGE_ONDEMAND_IDLE_SECONDS`
   - `BGE_RESTART_POLICY=no`
   - `BGE_ONDEMAND_PORT=<port parsed from endpoint URL>`
   - `PG_PASSWORD=unused-for-bge-ondemand` only if `PG_PASSWORD` is otherwise
     unset, so the `bge` profile can be started without requiring a Postgres
     password.

7. Poll authenticated `GET /ready` until it returns `200` with `ok=true` and
   `model_loaded=true`, or until startup timeout.
8. While `/ready` is not ready, unauthenticated `GET /health` distinguishes
   `loading` from `starting`.
9. On success, write `state=hot` and return `Endpoint(url, token)`.
10. On timeout, write `state=failed` with `last_error="startup timed out"` and
    raise `OnDemandError`.

`BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS` controls the timeout. The default is
`900`; the minimum accepted value is `10`.

## Endpoint Resolution And Port Fallback

`endpoint_url()` resolves the supervisor endpoint in this order:

1. `BGE_ONDEMAND_URL`, with trailing slash stripped
2. `BGE_ONDEMAND_PORT`, validated as an integer in `1..65535`, rendered as
   `http://127.0.0.1:<port>`
3. default `http://127.0.0.1:8086`

If `docker compose up` fails with an error string containing
`address already in use`, and neither `BGE_ONDEMAND_URL` nor
`BGE_ONDEMAND_PORT` is set, the supervisor retries once on a free loopback
port. The retry URL is written to `state.json`; subsequent passive doctor
status will prefer that recorded URL while auto-port mode remains enabled.

If `BGE_ONDEMAND_URL` or `BGE_ONDEMAND_PORT` is explicitly set, no automatic
port retry is attempted and passive status uses the explicitly configured URL,
not a stale URL from `state.json`.

## Compose Service

The `bge` compose service publishes:

```yaml
127.0.0.1:${BGE_ONDEMAND_PORT:-8086}:8086
```

For on-demand starts, the supervisor sets `BGE_RESTART_POLICY=no`. For manual
`bge-http` usage, the compose default remains `unless-stopped`.

The service passes these env vars into the container:

- `BGE_EMBED_TOKEN`
- `BGE_EMBED_PORT=8086`
- `BGE_EMBED_HOST=0.0.0.0`
- `BGE_ONDEMAND_IDLE_SECONDS`
- `HF_HOME=/hf_cache`

The HuggingFace cache is the Docker volume `hf_cache`, mounted at `/hf_cache`.
Do not remove `pg_data` when recovering a broken model cache.

## Embed Server Contract

`src/hippocampus/embed/server.py` exposes:

- `GET /health`: unauthenticated; returns `ok`, `model_loaded`,
  `active_request_count`, `last_completed_at`, and `idle_seconds`
- `GET /ready`: bearer-authenticated; returns the same fields as `/health`
- `POST /embed`: bearer-authenticated; accepts `{"query": str, "max_length": int}`
- `POST /embed_batch`: bearer-authenticated; accepts
  `{"texts": list[str], "max_length": int}`

`/ready` uses the same bearer token check as `/embed` and `/embed_batch`.
Missing or wrong bearer credentials produce the FastAPI/HTTPBearer auth
failure path.

The server loads `BAAI/bge-m3` during FastAPI lifespan startup. Device selection
is `cuda`, then `mps`, then `cpu`. Model stdout is redirected to stderr while
loading. All dense vectors are checked by the normalization boundary before
being returned.

## Idle Self-Exit

The embed server owns idle shutdown. This matters because the caller may be a
short-lived CLI or cron process; after that process exits, no parent supervisor
remains to reap the BGE container.

When `BGE_ONDEMAND_IDLE_SECONDS` is unset or invalid, idle exit is disabled in
the server. When it is a positive integer:

- a daemon thread wakes every `min(30, max(1, idle // 3))` seconds
- active `/embed` and `/embed_batch` requests are counted under a process-local
  lock
- if `active_request_count > 0`, shutdown is skipped
- if the elapsed time since `LAST_COMPLETED_AT` is less than the idle timeout,
  shutdown is skipped
- otherwise the thread sets `uvicorn.Server.should_exit = True`

The shutdown is graceful from Uvicorn's perspective. The child does not write a
terminal state marker on exit; stale `hot`, `running`, `starting`, or `loading`
states are handled by passive status probing.

## Doctor Status

`hippocampus doctor` is passive for `bge-ondemand`: it never calls `/embed` and
never starts Docker Compose.

`doctor` calls `passive_status(token=...)`, then prints:

- `OK`: `bge-ondemand hot ... (/ready verified)` when authenticated `/ready`
  succeeds
- `INFO`: `bge-ondemand running ... (passive status; no /embed probe)` when
  unauthenticated `/health` succeeds but `/ready` is not verified
- `INFO`: `bge-ondemand cold ...` for no state or stale active states with no
  reachable server
- `FAIL`: `bge-ondemand failed ...` when state is `failed`
- `FAIL`: `bge-ondemand config error ...` for invalid local configuration such
  as a bad port or idle timeout

For stale active states, passive status returns `state=cold` and includes
`last_state`, so doctor can report that the previous state was `hot`,
`running`, `starting`, or `loading`.

## Manual Low-Memory Mode

Manual low-memory mode is still plain `bge-http`: keep `BGE_EMBED_URL` and
`BGE_EMBED_TOKEN` configured, start the local compose service before semantic
work, and stop it afterward:

```bash
docker compose --profile bge up -d
hippocampus ingest codex
docker compose stop bge
```

If `BGE_EMBED_URL` points at a stopped local server, semantic ingest/search
fails loudly. That is expected for this mode.

## Failure Modes

- Docker missing: `OnDemandError` says to install Docker or use `--embed none`
  / remote `bge-http`.
- `compose.yaml` missing: set `HIPPOCAMPUS_COMPOSE_DIR` or run from the repo.
- Invalid `BGE_ONDEMAND_IDLE_SECONDS`: must be an integer `>= 30`.
- Invalid `BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS`: must be an integer `>= 10`.
- Invalid `BGE_ONDEMAND_PORT`: must be an integer in `1..65535`.
- Startup timeout: first model download/load may still be running; check
  `docker compose logs bge`, retry, or increase
  `BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS`.
- Interrupted or corrupt model cache: stop `bge`, retry, and if necessary
  remove only the compose `hf_cache` volume.

## Tests

`scripts/test_ondemand_embed.py` covers the pure-Python contract without
starting Docker or BGE-M3:

- `Settings` accepts `bge-ondemand`
- `embed_configured` is true
- init writes the expected env keys
- `EmbedClient` routes through `ensure_endpoint()` without mutating `self.url`
- supervisor startup and compose reconciliation paths
- automatic alternate-port retry
- doctor passive cold/failed status
- stale fallback URL handling
- authenticated `/ready`

The fresh-machine smoke used for `v2.0.0` also verified a real BGE-M3 encode:
`EmbedClient().encode("ping")` returned a normalized 1024-dimensional vector,
and the container exited after the idle timeout.
