"""hippocampus management CLI.

Implemented: ingest (Phase 2), summarize (Phase 2).
Planned: init / migrate / doctor (Phase 3).

Locking note: the operator wrappers (cron_ingest.sh / session_end_ingest.sh)
hold /tmp/hippocampus_ingest.lock around their invocations — the CLI itself
does NOT take it (a child re-acquiring the parent's flock would deadlock).
Direct manual runs rely on per-conversation upsert idempotency.
"""
import argparse
import sys

_MODULE_COMMANDS = {
    "init": ("setup_init", "interactive setup (.env + migrations + settings.json snippet)"),
    "migrate": ("migrate", "manifest-driven migration runner (core/library tiers)"),
    "doctor": ("doctor", "connectivity / coverage / config diagnostics"),
    "ghost": ("ghost_promote", "ghost layer management (promote / status)"),
    "graph": ("graph_viz", "render local memory link-graph as self-contained HTML"),
    "sync-edges": ("sync_edges", "rebuild agent.memory_edges from ghost_memories.body"),
    "curate-memories": ("curate", "link/staleness curation report (suggest-only)"),
    "wiki": ("wiki", "learning-note wiki layer (propose/apply/rollback/status)"),
    "roster": ("roster", "company multi-user roster provisioning (tenants/users/roles/grants)"),
    "deja": ("deja", "cross-repo code index (index/search/stats) — issue #76"),
}


def cmd_ingest(argv: list[str]) -> int:
    import psycopg2
    from pgvector.psycopg2 import register_vector

    from .config import Settings
    from .ingest import get_registry
    from .ingest.base import IngestContext
    from .ingest.pipeline import run

    registry = get_registry()
    if not argv or argv[0] in ("--list", "-l"):
        print("available sources:")
        for name in sorted(registry):
            print(f"  {name}")
        return 0
    source, rest = argv[0], argv[1:]
    if source not in registry:
        print(f"unknown source: {source} (try: hippocampus ingest --list)",
              file=sys.stderr)
        return 2
    settings = Settings.load()
    conn = psycopg2.connect(settings.pg_url, connect_timeout=10)
    register_vector(conn)
    try:
        ctx = IngestContext(settings=settings, conn=conn, args=rest)
        return run(registry[source], ctx)
    finally:
        conn.close()


def cmd_summarize(argv: list[str]) -> int:
    from .ingest import summarize

    sys.argv = ["hippocampus summarize", *argv]
    summarize.main()
    return 0


def cmd_extract_facts(argv: list[str]) -> int:
    from .ingest import extract_facts

    sys.argv = ["hippocampus extract-facts", *argv]
    extract_facts.main()
    return 0


def cmd_diary(argv: list[str]) -> int:
    from .ingest import diary

    sys.argv = ["hippocampus diary", *argv]
    diary.main()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hippocampus",
        description="Personal memory MCP server management CLI",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "ingest", help="ingest a source (claude-code / chatgpt / claude-ai / codex / antigravity / kimi / grok)")
    sub.add_parser(
        "summarize", help="build conversation rollup/segment summaries + embeds")
    sub.add_parser(
        "extract-facts", help="extract distilled facts from conversations into personal.extracted_facts")
    sub.add_parser(
        "diary", help="write the day's candid first-person diary into personal.diary")
    for name, (_mod, help_text) in _MODULE_COMMANDS.items():
        sub.add_parser(name, help=help_text)

    # Source/summarize/extract-facts args pass through verbatim (incl. --flags), so only
    # parse up to the subcommand and hand the tail over.
    argv = sys.argv[1:]
    args, rest = parser.parse_known_args(argv[:1])
    rest = argv[1:]

    if not args.command:
        parser.print_help()
        sys.exit(0)
    if args.command == "ingest":
        sys.exit(cmd_ingest(rest))
    if args.command == "summarize":
        sys.exit(cmd_summarize(rest))
    if args.command == "extract-facts":
        sys.exit(cmd_extract_facts(rest))
    if args.command == "diary":
        sys.exit(cmd_diary(rest))
    import importlib

    mod_name, _ = _MODULE_COMMANDS[args.command]
    mod = importlib.import_module(f".{mod_name}", package=__package__)
    sys.exit(mod.main(rest))


if __name__ == "__main__":
    main()
