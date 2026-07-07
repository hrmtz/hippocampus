"""Shared transcript-prose extraction (epic #43 code-review #9).

Both the scoring stage (pipeline.py) and the rollup summarizer
(summarize.py) strip code/tool noise from message bodies before sending
them to Haiku. They previously kept two copies of `extract_prose` that had
already diverged (400 vs 300 char caps, diff-line skipping in one only).

The per-call-site differences are real and intentional — scoring trims more
aggressively (skips unified-diff lines, longer cap) than summarization — so
they stay as explicit arguments rather than two functions.
"""
from __future__ import annotations


def extract_prose(content: str, *, max_chars: int, skip_diff: bool) -> str:
    """Drop fenced code blocks and tool-result lines; return leading prose.

    skip_diff: also drop `diff ` lines (scoring wants this; summaries don't).
    """
    lines: list[str] = []
    in_code = False
    for line in content.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith("[tool_result"):
            continue
        if skip_diff and line.startswith("diff "):
            continue
        lines.append(line)
    return "\n".join(lines).strip()[:max_chars]
