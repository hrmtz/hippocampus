"""`hippocampus deja doctor` — canary over the PreTool/PostTool completion log.

SoT `docs/designs/DEJA_CODE_HOOK_MVP.md` §9: a **zero-eligible / excessive-timeout
canary** with **no overnight paging** — a one-shot health summary (human-read or
``--json``) that makes a silently-degraded advisory surface visible before
activation. It is pure + read-only over the same rotated ``pretool_completion.jsonl``
generations as the stats aggregator; it never touches the Stop surface.

Checks (per source, ``pretooluse`` / ``posttooluse``):
- **zero_eligible** — fraction of invocations that found no eligible chunk. Near
  1.0 over a meaningful window means the surface never has anything to compare:
  either healthy-idle or a chunker/allowlist regression. Canary, not an SLO.
- **timeout** — any ``timed_out`` invocation, or a p95 latency approaching the
  inner budget, means fail-open kills are eating invocations.
- **unparseable** — a high ``unparseable`` share (esp. pretooluse apply_patch)
  flags a parser gap that silently suppresses all advisories.
- **search_unavailable** — embed/PG outcomes mean the cross-repo query never ran.

Thresholds are deliberately conservative heuristics (documented inline), tuned to
avoid paging; the window guard suppresses noise on tiny samples.
"""
from __future__ import annotations

from .pretool_stats import COMPLETION_BASE, DEFAULT_STATE_DIR, read_records

# Below this per-source invocation count, checks report `insufficient_data`
# rather than warn/crit — a canary must not page on 3 events.
MIN_WINDOW = 20

# Heuristic thresholds (fractions of a source's invocations, unless _MS).
ZERO_ELIGIBLE_WARN = 0.95        # ~all invocations find nothing eligible
TIMEOUT_WARN = 0.02              # >2% fail-open kills
LATENCY_P95_WARN_MS = 8000       # approaching the 9s inner budget
UNPARSEABLE_WARN = 0.50          # half of invocations un-parseable → parser gap

# outcome_reason values that mean the cross-repo search could not run.
_SEARCH_UNAVAILABLE = {"embed_error", "hnsw_unhealthy", "pg_error",
                       "search_unavailable"}

_ORDER = {"ok": 0, "insufficient_data": 1, "warn": 2, "crit": 3}


def _worst(*levels: str) -> str:
    return max(levels, key=lambda lv: _ORDER.get(lv, 0))


def _check(level_if_over: str, value: float, threshold: float, n: int,
           detail: str) -> dict:
    if n < MIN_WINDOW:
        status = "insufficient_data"
    elif value > threshold:
        status = level_if_over
    else:
        status = "ok"
    return {"status": status, "value": round(value, 4),
            "threshold": threshold, "n": n, "detail": detail}


def diagnose_source(stats: dict) -> dict:
    """Run the canaries for one source's SourceStats.to_dict() payload."""
    n = stats["invocations"]
    outcomes = stats["outcomes"]
    unparseable = outcomes.get("unparseable", 0)
    search_unavail = sum(outcomes.get(k, 0) for k in _SEARCH_UNAVAILABLE)
    latency = stats["latency"]

    zero_elig = _check("warn", stats["zero_eligible"] / n if n else 0.0,
                       ZERO_ELIGIBLE_WARN, n,
                       f"{stats['zero_eligible']}/{n} invocations found no "
                       "eligible chunk")
    timeout_frac = stats["timed_out"] / n if n else 0.0
    p95 = latency["p95_ms"]
    timeout_status = "crit" if (n >= MIN_WINDOW and timeout_frac > TIMEOUT_WARN) \
        else ("warn" if (n >= MIN_WINDOW and p95 > LATENCY_P95_WARN_MS)
              else ("insufficient_data" if n < MIN_WINDOW else "ok"))
    timeout = {"status": timeout_status, "timed_out": stats["timed_out"],
               "timed_out_frac": round(timeout_frac, 4), "p95_ms": p95, "n": n,
               "detail": f"{stats['timed_out']} hard-timeout kills; "
                         f"p95={p95}ms vs {LATENCY_P95_WARN_MS}ms warn"}
    unparse = _check("warn", unparseable / n if n else 0.0, UNPARSEABLE_WARN, n,
                     f"{unparseable}/{n} invocations un-parseable")
    search = _check("warn", search_unavail / n if n else 0.0, 0.0, n,
                    f"{search_unavail}/{n} invocations could not reach embed/PG")

    checks = {"zero_eligible": zero_elig, "timeout": timeout,
              "unparseable": unparse, "search_unavailable": search}
    overall = _worst(*(c["status"] for c in checks.values()))
    return {"invocations": n, "overall": overall, "checks": checks}


def run(state_dir: str = DEFAULT_STATE_DIR) -> dict:
    """Full canary report across all completion-log rotations."""
    from .pretool_stats import aggregate_completions

    records = read_records(state_dir, COMPLETION_BASE)
    by_source = aggregate_completions(records)
    per_source = {src: diagnose_source(st.to_dict())
                  for src, st in sorted(by_source.items())}
    overall = _worst("ok", *(v["overall"] for v in per_source.values())) \
        if per_source else "insufficient_data"
    return {"state_dir": state_dir, "completion_total": len(records),
            "overall": overall, "sources": per_source}
