**English** ・ [日本語](GHOST_LAYER_USER.ja.md)

# Ghost layer — user guide

How to use the cross-project agent memory vault. (Design rationale and
schema internals live in [GHOST_LAYER_DESIGN.md](design-history/GHOST_LAYER_DESIGN.md);
you don't need them to use it.)

## What it is

Coding agents accumulate memory files per project
(`~/.claude/projects/<hash>/memory/*.md`): rules learned from incidents,
user preferences, recurring-mistake notes. By default each project's
memory is invisible to every other project — the agent re-learns the same
lesson in every repo.

The ghost layer fixes that for the memories **you choose**: a nightly
"dub" job copies explicitly promoted memory files into a dedicated
PostgreSQL schema (`agent.ghost_memories`), embeds them, and makes them
searchable from *any* project's session via the `search_ghost_memory` MCP
tool. The original files are never modified — the vault is a read-only
mirror, and every entry keeps a `source_project` tag so you always know
where a rule came from.

This is a different corpus from your conversation archive with a
different trust posture: personal memory is *recall* ("what did I think
about X"), ghost memory is *standing instructions the agent gave itself*.
That is why promotion is deliberately high-friction.

## Promotion is opt-in, dual-signal, default-deny

A memory file is dubbed **only when both signals are present**:

**Signal 1 — frontmatter** in the memory file itself:

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

**Signal 2 — a line in the human-edited allowlist file**
`~/.claude/ghost_promote_allowlist.txt` (format
`<source_project>/<memory_slug>`, default deny):

```
# one promoted memory per line
my-webapp/feedback_always_pin_versions
dotfiles/user_prefers_tabs
```

Either signal alone does nothing (it is logged, not dubbed). The split
exists because agents write their own frontmatter — if `scope: shared`
alone could publish a memory, the agent could promote content without you.
The allowlist file is the human-in-the-loop.

**Third wall:** a content scanner runs over the body right before dub and
rejects obviously credential-shaped or otherwise blocklisted content even
when both signals pass. Scanner rejections are audit-logged
(`rejected_content_scan`); false positives can be overridden via an
explicit per-slug override file.

There is also a `scope: shared-restricted` tier (memory visible only to
an enumerated list of projects rather than all of them); restricted dub
support is still gated — check the dub run output before relying on it.

## Setup

1. **Schema**: the core migration tier already includes the ghost layer
   (`hippocampus migrate` — nothing extra to apply).
2. **Reader role**: the MCP server reads the vault through a dedicated
   read-only PG role. Provision it with:

   ```bash
   hippocampus init --ghost
   ```

   This sets a generated password on the `agent_read_mcp` role (created
   by migration 009) and writes `PG_URL_AGENT_READ_MCP` into your `.env`.
   The password is never printed. `hippocampus doctor` verifies the role
   connects and can resolve the ranked-search function.
3. **Dub job**: run the dub script manually or from nightly cron:

   ```bash
   # the dub writer role needs its own DSN; the host gate must name your machine
   export GHOST_ALLOWED_HOSTS="$(hostname)"
   export PG_URL_AGENT_DUB='postgresql://agent_dub:...@localhost:5432/hippocampus'
   python3 scripts/dub_agent_memories.py --dry-run --verbose   # preview
   python3 scripts/dub_agent_memories.py                       # real run
   ```

   `--dry-run` shows exactly which files would be dubbed, skipped, or
   rejected and why. The dub requires a working embed backend.

## Searching from any project

Once dubbed, every agent session with the hippocampus MCP server gets:

```
search_ghost_memory(query="postgres migration locking", current_project="my-webapp")
search_ghost_memory(current_project="my-webapp")          # empty query = vault overview
```

- Empty query lists the top-ranked memories — "what's in my ghost vault".
- Ranking blends semantic similarity, full-text match, recency, and a
  usefulness score that self-tunes: memories that keep surfacing in
  searches rise; ones that get corrected sink.
- `current_project` is **caller-attested, not verified** — it scopes
  `shared-restricted` visibility, so don't treat it as a security
  boundary against a malicious caller on your own machine.
- If the embed backend is down, the tool degrades to text-only ranking
  and says so in a warning header rather than failing.

## Removing a memory (purge story)

Three levels, depending on what you want:

1. **Stop future updates**: remove the line from
   `~/.claude/ghost_promote_allowlist.txt` (or drop `scope: shared` from
   the file). The vault copy stays but is never refreshed.
2. **Delete from the vault**: remove it from the allowlist **and** delete
   the row — otherwise the next nightly dub resurrects it:

   ```sql
   DELETE FROM agent.ghost_memories
    WHERE source_project = 'my-webapp' AND memory_slug = 'feedback_old_rule';
   ```

3. **Delete permanently (tombstone)**: also insert a tombstone row — the
   dub job checks it and will never re-dub that `(project, slug)` pair,
   even if the signals reappear:

   ```sql
   INSERT INTO agent.ghost_purge_tombstone (source_project, memory_slug, reason, purged_by)
   VALUES ('my-webapp', 'feedback_old_rule', 'no longer true', current_user);
   ```

   The tombstone table is append-only by design (forensic record);
   tombstone inserts require the purge-admin role.

All dub actions (dubbed / skipped / rejected / purged-skip) are recorded
in `agent.ghost_dub_log`, so "why is/isn't this memory in the vault?" is
always answerable from the audit trail.

## Optional: SessionStart injection

Beyond on-demand search, a SessionStart hook can inject a handful of
relevant ghost memories into each new session automatically
(`scripts/ghost_context_inject.py`; wiring example in
[GHOST_LAYER_DESIGN.md](design-history/GHOST_LAYER_DESIGN.md) §6). Kill switch:
`HIPPOCAMPUS_GHOST_DISABLE=1` disables injection for that session. The
hook is fail-open-to-empty: it never blocks session startup.
