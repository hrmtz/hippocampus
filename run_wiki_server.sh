#!/bin/bash
# hippocampus LLM-wiki read server launcher (HTML over HTTP).
# Serves personal.wiki_pages bodies as HTML (pandoc when present, <pre> else).
# Default 127.0.0.1:8087 (localhost-only; public access goes through the
# cloudflared tunnel which dials 127.0.0.1). Set WIKI_SERVE_HOST=0.0.0.0
# explicitly to expose on the WSL host / tailnet. Read-only: no DB mutation,
# PG via sops.
cd "$(dirname "$0")"
# ~/.local/bin holds pandoc (used for GFM->HTML render). systemd user services
# do not inherit it by default, so without this the server silently falls back
# to the raw <pre> surface for every page. Prepend it explicitly.
export PATH="$HOME/.local/bin:$PATH"
SOPS_BIN="$(command -v sops || echo "$HOME/.local/bin/sops")"
HIPPOCAMPUS_SECRETS="${CREDS_DIR:-.}/hippocampus.enc.yaml"

exec env SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" \
  "$SOPS_BIN" exec-env "$HIPPOCAMPUS_SECRETS" \
  "$(dirname "$0")/.venv/bin/hippocampus wiki serve --host ${WIKI_SERVE_HOST:-127.0.0.1} --port ${WIKI_SERVE_PORT:-8087}"
