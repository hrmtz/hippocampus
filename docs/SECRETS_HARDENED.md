**English** ・ [日本語](SECRETS_HARDENED.ja.md)

# Hardened secrets: sops instead of a plain `.env`

The **default** secrets story is a plain `.env` file with mode 0600,
written by `hippocampus init` and checked by `hippocampus doctor`. That
is fine for a single-user machine with full-disk encryption.

This addendum is for setups that want secrets **encrypted at rest** —
shared machines, synced/backed-up project directories, or simply a
stricter posture. It uses [sops](https://github.com/getsops/sops) with
[age](https://github.com/FiloSottile/age) keys. Nothing in hippocampus
requires sops; the integration point is purely "inject environment
variables into the process", which `Settings` already prefers over `.env`
(process env always wins).

## One rule above all

> **Wrappers must inject env without ever decrypting to stdout.**

The only two sops invocations you should ever type or script:

```bash
sops edit secrets.enc.yaml                 # edit (decrypts into $EDITOR, re-encrypts on save)
sops exec-env secrets.enc.yaml '<command>' # use (decrypted values go into the child's env only)
```

Anything of the form `sops -d … | …` puts plaintext secrets on stdout —
into your scrollback, your shell history, your terminal logger, and (if
an AI agent is driving the terminal) into a conversation transcript that
may be retained indefinitely. Once a secret has been printed you cannot
un-print it; the fix is rotation, not deletion. So: no `sops -d`, no
piping decrypted output through `cat`/`grep`/`head`, no
`export $(sops -d …)`. If you need to check whether a key exists, list
key *names* only:

```bash
sops exec-env secrets.enc.yaml 'env | cut -d= -f1 | sort'
```

## age key setup (sketch)

```bash
# 1. generate a keypair (keep this file safe; it decrypts everything)
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
# note the printed public key: age1...

# 2. tell sops which recipients encrypt which files — .sops.yaml next to the secrets
cat > .sops.yaml <<'EOF'
creation_rules:
  - path_regex: secrets\.enc\.yaml$
    age: age1YOUR_PUBLIC_KEY_HERE
EOF

# 3. create the encrypted file
cat > /tmp/seed.yaml <<'EOF'
PG_URL: postgresql://hippocampus:CHANGE_ME@localhost:5432/hippocampus
BGE_EMBED_URL: http://localhost:8086
BGE_EMBED_TOKEN: CHANGE_ME
EOF
sops --encrypt /tmp/seed.yaml > secrets.enc.yaml && shred -u /tmp/seed.yaml
sops edit secrets.enc.yaml      # all future edits go through this
```

The encrypted `secrets.enc.yaml` is safe to store on disk, but
git-ignoring it anyway is good hygiene — committing ciphertext binds your
repository's safety to the lifetime secrecy of one age key. Keep the age
private key (`keys.txt`) out of every repo and every backup that shares
fate with the ciphertext.

## Wiring hippocampus through sops

Replace the plain `.env` with exec-env wrappers. The precedence design
does the rest: values injected into the process environment override any
stray `.env` in the working directory.

**MCP server** — register a tiny wrapper script instead of the bare
entry point:

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

**CLI / ingest / cron** — same pattern:

```bash
sops exec-env secrets.enc.yaml 'hippocampus doctor'
sops exec-env secrets.enc.yaml 'hippocampus ingest claude-code'
sops exec-env secrets.enc.yaml 'hippocampus migrate --status'
```

For cron, point sops at the key location explicitly (cron's environment
is minimal):

```cron
0 3 * * * cd /path/to/hippocampus-mcp && SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/keys.txt flock -n /tmp/hippocampus_ingest.lock -c "sops exec-env secrets.enc.yaml 'hippocampus ingest claude-code'" >> $HOME/hippocampus-ingest.log 2>&1
```

`hippocampus init` is not needed in this mode (its job is writing `.env`);
run `hippocampus migrate` / `hippocampus init --skip-migrations` pieces
under the wrapper as required. `doctor` reports `.env: not found in cwd
(process env only — fine under a secrets wrapper)` — that line is
expected, not a failure.

## Notes and sharp edges

- **Inner pipes are fine, outer pipes are not.**
  `sops exec-env f 'pg_dump … | gzip > out.gz'` keeps secrets inside the
  child environment; `sops -d f | anything` does not.
- **Don't echo values for "verification".** `echo $PG_URL` inside an
  exec-env shell prints the secret. If you must verify, verify a boolean:
  `sops exec-env f 'test -n "$PG_URL" && echo set'`.
- **Key loss = corpus config loss, not corpus loss.** The database itself
  is not encrypted by sops; losing the age key only locks you out of the
  config file. Still: back the key up separately from the ciphertext.
- **Rotation**: if a secret ever does land in a transcript or log,
  rotate it at the source (database role password, API key) — editing the
  sops file afterwards only protects the future.
- Multiple machines: add one age recipient per machine to `.sops.yaml`
  (comma-separated), re-encrypt with `sops updatekeys secrets.enc.yaml`,
  and transport the file out-of-band.
