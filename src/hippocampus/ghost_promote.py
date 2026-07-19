"""ghost_promote.py — ghost layer management CLI (hippocampus ghost ...).

Subcommands:
  hippocampus ghost status   — list scope:shared memories not yet in allowlist
  hippocampus ghost promote  — interactively add candidates to allowlist
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
ALLOWLIST_PATH = Path(os.environ.get(
    "GHOST_PROMOTE_ALLOWLIST",
    str(Path.home() / ".claude" / "ghost_promote_allowlist.txt"),
))
MEMORY_FILENAME_RE = re.compile(r"^(user|feedback|project|reference)_.+\.md$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


# ─────────────────────────────────────────────────────────────
# Minimal parsing helpers (mirrors _ghost_common without the sys.path dance)
# ─────────────────────────────────────────────────────────────

@dataclass
class ParsedMemory:
    frontmatter: dict
    body: str


def _parse_md(text: str) -> Optional[ParsedMemory]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return None
        return ParsedMemory(frontmatter=fm, body=m.group(2))
    except yaml.YAMLError:
        return None


def _fm_get(fm: dict, key: str, default=None):
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    return meta.get(key) or fm.get(key) or default


def _get_scope(fm: dict) -> str:
    return _fm_get(fm, "scope") or "shared"


def _get_slug(fm: dict, fallback: str) -> str:
    return fm.get("name") or fallback


def _get_title(fm: dict) -> str:
    return fm.get("description") or ""


def _get_type(fm: dict) -> str:
    return _fm_get(fm, "type") or "?"


# ─────────────────────────────────────────────────────────────
# Allowlist I/O
# ─────────────────────────────────────────────────────────────

def _load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    entries: set[str] = set()
    for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            entries.add(s)
    return entries


def _append_to_allowlist(entries: list[str]) -> None:
    with open(ALLOWLIST_PATH, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e}\n")


# ─────────────────────────────────────────────────────────────
# Project name derivation (simplified, mirrors _ghost_common)
# ─────────────────────────────────────────────────────────────

def _derive_project_name(hash_dir: Path) -> str:
    name = hash_dir.name
    parts = name.lstrip("-").split("-")
    for split_point in range(len(parts), 0, -1):
        dir_parts = parts[: split_point - 1]
        name_part = "-".join(parts[split_point - 1:])
        candidate = (
            Path("/" + "/".join(dir_parts + [name_part]))
            if dir_parts
            else Path("/" + name_part)
        )
        if candidate.exists():
            try:
                r = subprocess.run(
                    ["git", "-C", str(candidate), "config", "--get", "remote.origin.url"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    remote = r.stdout.strip().rstrip("/").rsplit("/", 1)[-1]
                    if remote.endswith(".git"):
                        remote = remote[:-4]
                    if remote:
                        return remote
            except Exception:
                pass
            return candidate.name
    return name


# ─────────────────────────────────────────────────────────────
# Candidate discovery
# ─────────────────────────────────────────────────────────────

def _find_candidates(
    allowlist: set[str],
    project_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
) -> list[tuple[str, str, ParsedMemory, Path]]:
    """Return (project, slug, parsed, file) for scope:shared not yet in allowlist."""
    out = []
    if not PROJECTS_ROOT.exists():
        return out
    for hash_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not hash_dir.is_dir():
            continue
        mem_dir = hash_dir / "memory"
        if not mem_dir.is_dir():
            continue
        try:
            project = _derive_project_name(hash_dir)
        except Exception:
            project = hash_dir.name

        if project_filter and project_filter not in project:
            continue

        for md_file in sorted(mem_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            if not MEMORY_FILENAME_RE.match(md_file.name):
                continue
            try:
                raw = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = _parse_md(raw)
            if parsed is None:
                continue
            scope = _get_scope(parsed.frontmatter)
            if scope not in ("shared",):
                continue
            slug = _get_slug(parsed.frontmatter, md_file.stem)
            if type_filter and _get_type(parsed.frontmatter) != type_filter:
                continue
            key = f"{project}/{slug}"
            if key in allowlist:
                continue
            out.append((project, slug, parsed, md_file))
    return out


# ─────────────────────────────────────────────────────────────
# Subcommands
# ─────────────────────────────────────────────────────────────

def cmd_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hippocampus ghost status",
        description="Show scope:shared memories not yet in allowlist",
    )
    parser.add_argument("--project", help="filter by project name substring")
    parser.add_argument("--type", dest="mem_type",
                        help="filter by memory type (user/feedback/project/reference)")
    args = parser.parse_args(argv)

    allowlist = _load_allowlist()
    candidates = _find_candidates(allowlist, project_filter=args.project,
                                  type_filter=args.mem_type)

    if not candidates:
        print("no pending candidates — all scope:shared memories are in allowlist")
        return 0

    by_project: dict[str, list] = {}
    for project, slug, parsed, _ in candidates:
        by_project.setdefault(project, []).append((slug, parsed))

    total = len(candidates)
    print(f"pending promotion candidates: {total}\n")
    for project, entries in sorted(by_project.items()):
        print(f"  [{project}]  ({len(entries)})")
        for slug, parsed in entries:
            title = _get_title(parsed.frontmatter)
            mem_type = _get_type(parsed.frontmatter)
            suffix = f"  — {title}" if title else ""
            print(f"    • {slug}  [{mem_type}]{suffix}")
    print(f"\nRun `hippocampus ghost promote` to approve interactively.")
    return 0


def cmd_promote(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hippocampus ghost promote",
        description="Interactively promote scope:shared memories to allowlist",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show candidates without writing to allowlist")
    parser.add_argument("--yes-all", action="store_true",
                        help="Approve all candidates without prompting")
    parser.add_argument("--project", help="filter by project name substring")
    parser.add_argument("--type", dest="mem_type",
                        help="filter by memory type (user/feedback/project/reference)")
    args = parser.parse_args(argv)

    allowlist = _load_allowlist()
    candidates = _find_candidates(allowlist, project_filter=args.project,
                                  type_filter=args.mem_type)

    if not candidates:
        print("no pending candidates — all scope:shared memories are already in allowlist")
        return 0

    print(f"Found {len(candidates)} candidates not yet in allowlist.")
    if args.dry_run:
        print("(dry-run — no writes)\n")
    else:
        print()

    approved: list[str] = []
    i = 0
    while i < len(candidates):
        project, slug, parsed, md_file = candidates[i]
        key = f"{project}/{slug}"
        title = _get_title(parsed.frontmatter) or "(no description)"
        mem_type = _get_type(parsed.frontmatter)

        print(f"[{i + 1}/{len(candidates)}] {key}")
        print(f"  type : {mem_type}")
        print(f"  desc : {title}")
        body_lines = parsed.body.strip().splitlines()[:4]
        if body_lines:
            preview = "\n  ".join(body_lines)
            print(f"  ─────\n  {preview}\n  ─────")

        if args.yes_all:
            print("  → approved (--yes-all)")
            approved.append(key)
            i += 1
            print()
            continue

        while True:
            try:
                choice = input("  promote? [y]es / [n]o / [q]uit / [a]ll : ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "q"

            if choice in ("y", "yes"):
                approved.append(key)
                break
            elif choice in ("n", "no"):
                break
            elif choice in ("q", "quit"):
                print(f"\nAborted. Approved {len(approved)} so far.")
                if approved and not args.dry_run:
                    _append_to_allowlist(approved)
                    print(f"Written {len(approved)} entries to {ALLOWLIST_PATH}")
                return 0
            elif choice in ("a", "all"):
                approved.append(key)
                approved.extend(
                    f"{p}/{s}" for p, s, _, __ in candidates[i + 1:]
                )
                print(f"  → approved (all remaining: {len(candidates) - i} total)")
                i = len(candidates)
                break
            else:
                print("  (y/n/q/a)")
        print()
        i += 1

    print(f"Approved {len(approved)} / {len(candidates)}.")
    if approved and not args.dry_run:
        _append_to_allowlist(approved)
        print(f"Written {len(approved)} entries to {ALLOWLIST_PATH}")
        print("\nNext: run `sops exec-env ... python3 scripts/dub_agent_memories.py` to sync.")
    elif not approved:
        print("Nothing approved.")
    elif args.dry_run:
        print("(dry-run — no changes written)")
    return 0


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hippocampus ghost",
        description="Ghost layer management",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("promote", help="interactively promote scope:shared memories to allowlist")
    sub.add_parser("status", help="show pending promotion candidates")

    if not argv:
        parser.print_help()
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd == "promote":
        return cmd_promote(rest)
    if cmd == "status":
        return cmd_status(rest)
    parser.print_help()
    return 1
