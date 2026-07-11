#!/bin/bash
# hippocampus claude.ai connector — streamable HTTP + OAuth authorization server.
# Listens on 127.0.0.1:$PORT; a cloudflared tunnel routes the public hostname to
# it (see docs/CONNECTOR.md). OAuth gates /mcp; the
# /authorize endpoint sits behind Cloudflare Access (owner SSO).
#
# Required in the hippocampus sops file (hippocampus.enc.yaml):
#   HIPPOCAMPUS_CONNECTOR_CLIENT_ID     — static claude.ai client id
#   HIPPOCAMPUS_CONNECTOR_CLIENT_SECRET — static claude.ai client secret
# Required in the environment / this wrapper (NOT secret):
#   HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST   — e.g. mcp.example.com (no scheme)
set -u
cd "$(dirname "$0")"
SOPS_BIN="$(command -v sops || echo "$HOME/.local/bin/sops")"
HIPPOCAMPUS_SECRETS="${CREDS_DIR:-.}/hippocampus.enc.yaml"

PUBLIC_HOST="${HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST:?set HIPPOCAMPUS_CONNECTOR_PUBLIC_HOST (public hostname, no scheme)}"
PORT="${HIPPOCAMPUS_CONNECTOR_PORT:-8092}"
# The hippocampus embed service (:8086) provides semantic search; without it the
# connector still serves FTS-only (tools auto-gate). Override for a different host.
BGE_URL="${BGE_EMBED_URL:-http://127.0.0.1:8086}"

# issuer = AS origin; resource = protected /mcp (drives the /mcp-suffixed
# protected-resource metadata). Both pinned to the public tunnel host (C7).
ISSUER="https://${PUBLIC_HOST}"
RESOURCE="https://${PUBLIC_HOST}/mcp"

exec env SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" \
  HF_HOME="${HF_HOME:-$HOME/.hf_cache}" \
  "$SOPS_BIN" exec-env "$HIPPOCAMPUS_SECRETS" \
  "env BGE_EMBED_URL=$BGE_URL \
    HIPPOCAMPUS_CONNECTOR_HOST=127.0.0.1 \
    HIPPOCAMPUS_CONNECTOR_PORT=$PORT \
    HIPPOCAMPUS_CONNECTOR_ISSUER_URL=$ISSUER \
    HIPPOCAMPUS_CONNECTOR_RESOURCE_URL=$RESOURCE \
    HIPPOCAMPUS_CONNECTOR_CLIENT_ID=\$HIPPOCAMPUS_CONNECTOR_CLIENT_ID \
    HIPPOCAMPUS_CONNECTOR_CLIENT_SECRET=\$HIPPOCAMPUS_CONNECTOR_CLIENT_SECRET \
    $(dirname "$0")/.venv/bin/hippocampus-mcp-connector-oauth"
