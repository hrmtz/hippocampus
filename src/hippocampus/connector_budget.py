"""Chain-read budget for the connector surface (design C2 / bug-hunt F2).

The connector is read-only, but read-only does not bound exfiltration: indirect
prompt injection can drive a sweep — enumerate conv_ids via search/list, then
pull each — paginating the private corpus out to the claude.ai session (design
§5, r1-codex-4). A per-request cap plus a rolling per-window call budget bound
that sweep without blocking normal use.

Single owner (C3), so a per-process rolling window is a faithful proxy for
per-session. Enforced by wrapping each allowlisted tool's callable at connector
boot — the stdio server.py path never sees this wrapper (C5).
"""
from __future__ import annotations

import functools
import os
import sys
import time
from collections import deque

# Defaults tuned for a human's interactive rate, not a sweep. Overridable.
WINDOW_S = int(os.environ.get("HIPPOCAMPUS_CONNECTOR_BUDGET_WINDOW_S", "300"))
MAX_CALLS = int(os.environ.get("HIPPOCAMPUS_CONNECTOR_BUDGET_MAX_CALLS", "40"))


class ReadBudgetExceeded(RuntimeError):
    """Raised when connector reads exceed the rolling-window budget."""


class _RollingBudget:
    """Fixed-count sliding window over call timestamps (monotonic clock)."""

    def __init__(self, max_calls: int, window_s: int) -> None:
        self._max = max_calls
        self._window = window_s
        self._calls: deque[float] = deque()

    def check_and_record(self) -> None:
        now = time.monotonic()
        cutoff = now - self._window
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            raise ReadBudgetExceeded(
                f"connector read budget exceeded ({self._max} reads / "
                f"{self._window}s); slow down or narrow the query")
        self._calls.append(now)


_BUDGET = _RollingBudget(MAX_CALLS, WINDOW_S)


def _wrap(fn):
    """Wrap a tool callable so each invocation consults the shared budget.

    Preserves the original signature (functools.wraps) so FastMCP's already-
    computed fn_metadata still matches the wrapped callable.
    """
    if getattr(fn, "_hippocampus_budgeted", False):
        return fn

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        _BUDGET.check_and_record()
        return fn(*args, **kwargs)

    guarded._hippocampus_budgeted = True  # idempotent re-wrap guard
    return guarded


def apply_budget(mcp, tool_names) -> list[str]:
    """Wrap the .fn of each named tool in place. Returns the names wrapped.

    Connector-only: called from connector.gate_and_allowlist, never from the
    stdio entrypoint, so server.py's tools keep their bare callables.
    """
    tm = mcp._tool_manager
    wrapped = []
    for name in tool_names:
        tool = tm._tools.get(name) if hasattr(tm, "_tools") else None
        if tool is None:
            continue
        tool.fn = _wrap(tool.fn)
        wrapped.append(name)
    print(f"[hippocampus-connector] read budget: {MAX_CALLS} reads / {WINDOW_S}s "
          f"on {len(wrapped)} tools", file=sys.stderr, flush=True)
    return wrapped
