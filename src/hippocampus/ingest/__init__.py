"""Ingest plugin layer (epic #43 Phase 2).

Built-in adapters are registered here; out-of-tree adapters can register
via the `hippocampus.sources` entry-points group (importlib.metadata).
"""
from __future__ import annotations

from importlib.metadata import entry_points


def get_registry() -> dict:
    """Name → SourceAdapter instance for all available sources."""
    from .sources.chatgpt import ChatGPTAdapter
    from .sources.claude_ai import ClaudeAiAdapter
    from .sources.claude_code import ClaudeCodeAdapter
    from .sources.codex import CodexAdapter
    from .sources.antigravity import AntigravityAdapter
    from .sources.kimi import KimiAdapter
    from .sources.grok import GrokAdapter

    registry = {
        a.name: a
        for a in (ClaudeCodeAdapter(), ChatGPTAdapter(), ClaudeAiAdapter(),
                  CodexAdapter(), AntigravityAdapter(), KimiAdapter(),
                  GrokAdapter())
    }
    for ep in entry_points(group="hippocampus.sources"):
        try:
            adapter = ep.load()()
            registry[adapter.name] = adapter
        except Exception as e:  # noqa: BLE001 — a broken plugin must not kill built-ins
            import sys
            print(f"[hippocampus] skipping source plugin {ep.name}: {e}",
                  file=sys.stderr, flush=True)
    return registry
