"""deja-code index policy — repo allowlist, path denylist, secret guard.

Design: docs/designs/DEJA_CODE.md §4.3 + §13. The 4-layer containment:
  1. opt-in repo allowlist (~/.claude/deja_index_allowlist.txt) — repos not
     listed are never read (dual-magi R1 r1-security-1: the medical/client
     repo cluster under ~/projects makes a denylist unsafe by default)
  2. `git ls-files` tracked-only enumeration (done in index.py)
  3. path/name denylist applied to the FULL repo-relative path and every
     path component (r1-security-4: basename-only matching leaks
     app/secrets/x.py; `.env*` alone misses config/foo.env)
  4. content-level secret patterns (r1-security-2/-3: age1 is a PUBLIC key
     prefix — the real secret is AGE-SECRET-KEY-1; DSN-with-password is the
     most common in-corpus class)
"""
from __future__ import annotations

import fnmatch
import os
import re

ALLOWLIST_PATH = (os.environ.get("HIPPOCAMPUS_DEJA_ALLOWLIST")
                  or os.path.expanduser("~/.claude/deja_index_allowlist.txt"))

ALLOWLIST_HEADER = """\
# deja-code index allowlist — one repo dir name per line (under ~/projects).
# OPT-IN: repos not listed here are never read by the indexer.
# WARNING before adding a repo (docs/designs/DEJA_CODE.md §13):
#   - its tracked source text (not just embeddings) lands on the canonical
#     remote PG host; medical / client / engagement / forensics repos need an
#     explicit contractual-constraint check first.
# Removing a repo prunes all its data from the DB on the next index run.
"""

# worktree duplicates are refused even if listed (self-hit noise)
DENY_REPOS = {"_formation_wt", "_canon_work"}

DENY_DIR_COMPONENTS = {"data", "dist", "build", "vendor", "node_modules"}

DENY_NAME_GLOBS = [
    "*.min.js", "secrets*", "*.enc.*", ".env*", "*.env", "*.env.*",
    "credentials*", "*.pem", "*_key*",
]

MAX_FILE_BYTES = 300 * 1024

INDEX_EXTS = {"py", "js", "mjs", "cjs", "ts", "sh", "bash", "html"}

# High-confidence secret patterns (§4.3 step 5). Generic entropy scanning is
# deliberately OUT of scope for v0 (FP-prone). NOTE: `age1` is the age
# RECIPIENT (public) prefix and must NOT be matched — only AGE-SECRET-KEY-1.
SECRET_PATTERNS = [re.compile(p) for p in (
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    r"ghp_[A-Za-z0-9]{36}",
    r"sk-ant-",
    r"AGE-SECRET-KEY-1[0-9A-Z]{50,}",
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^:@/\s]+:[^@/\s]+@",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
)]


def load_allowlist(path: str = ALLOWLIST_PATH) -> list[str]:
    """Return allowlisted repo names (comment/blank lines skipped). Missing
    file => empty list (nothing is indexed until the operator opts in)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    repos = []
    for line in lines:
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name in DENY_REPOS:
            continue
        repos.append(name)
    return repos


def path_allowed(rel_path: str) -> bool:
    """Policy layer 3: repo-relative path against dir/name denylists."""
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in DENY_DIR_COMPONENTS:
            return False
    for part in parts:  # every component AND the basename against name globs
        for pattern in DENY_NAME_GLOBS:
            if fnmatch.fnmatch(part, pattern):
                return False
    ext = parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
    return ext in INDEX_EXTS


def content_is_secret(text: str) -> bool:
    """Policy layer 4: chunk-level secret guard."""
    return any(p.search(text) for p in SECRET_PATTERNS)
