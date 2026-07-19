# claude.ai connector (remote MCP over OAuth)

Reach your personal-memory corpus from **claude.ai on the web and mobile** —
not just from a terminal agent. The connector exposes the same search tools as
a remote MCP server that claude.ai talks to over HTTPS, gated by a single-owner
OAuth authorization server.

Once registered, you can ask claude.ai (web or the iOS/Android app) things like
"what did I decide about X last month?" and it calls `search_personal_memory`
against your database, wherever you are.

> Design & rationale: [designs/claude-ai-connector-oauth.md](designs/claude-ai-connector-oauth.md).
> Operator deploy steps (deployment-specific, gitignored):
> `docs/operator/CONNECTOR_DEPLOY_RUNBOOK.md`.

## How it is different from the stdio MCP server

The normal `hippocampus-mcp` server speaks **stdio** — a local agent (Claude
Code, Codex) launches it as a subprocess. claude.ai's web/mobile apps cannot
launch a local process; they only support **remote MCP** connectors reached over
HTTPS. So the connector is a second entry point around the *same* tools and
database:

| | stdio server | connector |
|---|---|---|
| entry point | `hippocampus-mcp` | `hippocampus-mcp-connector-oauth` |
| transport | stdio | streamable HTTP |
| auth | none (local) | OAuth (single owner) |
| reachable from | Claude Code / Codex / Desktop | claude.ai web + mobile |
| tool surface | all gated tools | **read-only allowlist subset** |

The connector runs as a **separate process** so it never changes the stdio
server's behaviour.

## What is exposed (and what is not)

The connector serves a **fail-closed allowlist** — only these read tools:

- `search_personal_memory`, `search_conversations`, `search_library`
- `list_recent_conversations`, `list_project_conversations`
- `get_conversation_summary`
- `get_diary`, `search_diary` — the agent's daily first-person diary. Opt-in
  exposure: the diary is more sensitive than conversation snippets (introspection
  over security-heavy days), so its inclusion on the public surface is a
  deliberate operator choice, not a default. Drop it from
  `CONNECTOR_TOOL_ALLOWLIST` to keep the diary stdio-only.

Deliberately **excluded** from the public surface (they stay on stdio):

- `get_conversation` — full-thread retrieval, an exfiltration side-channel
- `search_ghost_memory`, `search_facts` — the authorized (ghost/facts) tier

A per-process **chain-read budget** (default 40 reads / 300 s) bounds a
sweep, and each served read writes a fail-open **audit** row (tool + argument
digest, never the query text).

## Security posture

- **Single owner.** One static OAuth client; dynamic client registration is
  disabled. The only allowed redirect is claude.ai's documented callback.
- **Audience-bound tokens.** Every token is pinned to this server's `/mcp`
  resource; a token minted for anything else is rejected.
- **`/mcp` is fail-closed.** No valid, unexpired, correctly-audienced token →
  `401`. The OAuth ceremony endpoints (metadata, `/authorize`, `/token`) are
  intentionally reachable unauthenticated because the flow requires it.
- **Tokens are opaque and hashed at rest**; refresh tokens rotate with
  reuse detection.
- Exposure is via a **cloudflared tunnel** only (the origin port is not
  public). Optionally put **Cloudflare Access** in front of `/authorize`
  (path-exact) as a human SSO wall — defense in depth.

Single-owner caveat: the connector currently exposes your *whole* personal
corpus flat. Per-persona / per-tenant scoping is future work (see the
federation design, gh #70).

## Setup (summary)

Full, deployment-specific steps live in the operator runbook; the shape is:

1. **Install the extra** on the host that will run it:
   ```bash
   pip install -e '.[connector]'          # pins mcp[cli]>=1.27
   ```
2. **Static client credentials** into your secrets (you also type these into
   claude.ai): `HIPPOCAMPUS_CONNECTOR_CLIENT_ID` / `_CLIENT_SECRET`.
   Generate with e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Run it** behind a systemd unit:
   `systemd/hippocampus-mcp-connector.service` → `run_connector_oauth.sh`.
   Set `HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST=<your-host>.example.com` (no scheme)
   via a drop-in; the wrapper derives `issuer=https://<host>` and
   `resource=https://<host>/mcp`.
4. **Expose it**: a cloudflared route `<your-host>` → `http://127.0.0.1:8092`
   plus a DNS record. (A dedicated tunnel keeps it isolated from your other
   tunnels.)
5. **Register on claude.ai** (web): Settings → Connectors → Add custom
   connector → URL `https://<your-host>/mcp` → Advanced settings → the client
   id/secret from step 2 → complete OAuth. Then use it from web and mobile.

## Environment variables

| var | purpose | default |
|---|---|---|
| `HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST` | public hostname (no scheme); drives issuer/resource | (required for OAuth) |
| `HIPPOCAMPUS_CONNECTOR_CLIENT_ID` / `_CLIENT_SECRET` | static claude.ai client | (required for OAuth) |
| `HIPPOCAMPUS_CONNECTOR_PORT` | local bind port | `8092` |
| `HIPPOCAMPUS_CONNECTOR_ACCESS_TTL` / `_REFRESH_TTL` | token lifetimes (s) | `3600` / `2592000` |
| `HIPPOCAMPUS_CONNECTOR_BUDGET_MAX_CALLS` / `_WINDOW_S` | chain-read budget | `40` / `300` |

## Troubleshooting

- **claude.ai: "Token exchange failed / integration not available."** Usually
  the `/mcp` call after OAuth is being rejected. Check the connector journal:
  a `421 "Invalid Host header"` means the public hostname isn't in the app's
  DNS-rebinding allowlist — the connector sets this from the issuer, so confirm
  `HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST` matches the URL you registered.
- **Browser shows the site can't be reached, but `curl` works.** A stale DNS
  negative cache on your device; the URL is an API endpoint (claude.ai's
  servers fetch it), not a page — visiting `/mcp` in a browser returns `401`.
- **`hippocampus-mcp-connector-oauth` exits immediately.** Missing OAuth config
  — it fail-closes if any of the four `HIPPOCAMPUS_CONNECTOR_*` OAuth vars are
  unset.
