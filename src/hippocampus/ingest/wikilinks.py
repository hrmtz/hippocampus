"""Wikilink extraction for the memory link-graph layer.

Pure, no I/O. Shared primitive used by:
  - sync_edges.py (build agent.memory_edges from ghost_memories.body)
  - graph_viz.py  (build the local-vault graph)

Design: docs/designs/MEMORY_LINK_GRAPH.md §4 (ultramagi-reviewed).

Key correctness facts (measured against 540 real memory files, R1 review):
  - Real ``[[targets]]`` are written as the FILESTEM (snake_case filename minus
    ``.md``), e.g. ``[[feedback_harness_structural_primary]]`` — NOT the
    frontmatter ``name:`` value (which differs in 72% of files). Resolution keys
    on the filestem; ``own_stem`` here is the filestem for self-link suppression.
  - Code regions must be stripped first: POSIX ``[[:space:]]`` and bash
    ``[[ -n "$X" ]]`` otherwise become the most frequent bogus "links" (28
    measured fence false positives, ``[[:space:]]`` alone 17x).
"""
from __future__ import annotations

import re

# Per-line match (no cross-newline spans). Target/alias classes EXCLUDE '[' and
# are length-bounded {1,200}: this both stops a match from spanning a '[[' opener
# and defuses catastrophic backtracking (ReDoS) on a long unclosed-'[[' line
# (bug-hunt F1: an unbounded lazy class took 16s on a 32KB line). #anchor optional.
_WIKILINK_RE = re.compile(
    r"\[\[\s*([^\]\[\|#\n]{1,200}?)\s*(?:#[^\]\[\|\n]{0,200})?"
    r"(?:\|\s*([^\]\[\n]{1,200}?)\s*)?\]\]"
)
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_WS_RE = re.compile(r"\s+")
# Defensive belt-and-suspenders filter for any code that leaks past fence-strip.
_POSIX_CLASS_RE = re.compile(r"^:[a-z]+:$")
_REJECT_CHARS = set('$"=')


def _strip_code_regions(body: str) -> str:
    """Drop fenced code blocks and inline-code spans before link matching.

    CommonMark: a fence closes only on the SAME marker char as it opened. We
    track the opener char so a ``` inside a ~~~ block (or vice-versa) does not
    spuriously toggle the fence off and leak code as prose (bug-hunt F2).
    """
    out: list[str] = []
    fence_char: str | None = None
    for line in body.splitlines():
        m = _FENCE_RE.match(line)
        if m:
            ch = m.group(1)[0]
            if fence_char is None:
                fence_char = ch          # open
            elif ch == fence_char:
                fence_char = None        # close (same marker only)
            continue
        if fence_char is not None:
            continue
        out.append(_INLINE_CODE_RE.sub("", line))
    return "\n".join(out)


def _normalize_target(raw: str) -> str:
    return _WS_RE.sub(" ", raw).strip()


def _is_garbage(target: str) -> bool:
    if not target:
        return True
    if _POSIX_CLASS_RE.match(target):  # [[:space:]] etc.
        return True
    if any(c in _REJECT_CHARS for c in target):  # bash conditionals, assignments
        return True
    return False


def extract_wikilinks(
    body: str, own_stem: str | None = None
) -> list[tuple[str, str | None]]:
    """Extract ``[[target|alias]]`` links from a markdown body.

    Returns an ordered, de-duplicated list of ``(target, alias_or_None)``.
    Targets are normalized (whitespace-collapsed, stripped) but NOT lowercased
    (resolution matches case-insensitively). Self-references (target == own_stem,
    case-insensitive) are dropped when ``own_stem`` is given.
    """
    clean = _strip_code_regions(body)
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    own = own_stem.lower() if own_stem else None
    for line in clean.splitlines():
        for m in _WIKILINK_RE.finditer(line):
            target = _normalize_target(m.group(1))
            if _is_garbage(target):
                continue
            key = target.lower()
            if own is not None and key == own:  # self-link
                continue
            if key in seen:
                continue
            alias_raw = m.group(2)
            alias: str | None = None
            if alias_raw is not None:
                # multi-pipe [[a|b|c]] keeps first segment; [[a||b]] -> empty -> None
                first = alias_raw.split("|")[0].strip()
                alias = first or None
            seen.add(key)
            out.append((target, alias))
    return out
