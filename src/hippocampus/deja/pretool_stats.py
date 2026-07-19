"""Aggregation over the deja-code PreToolUse/PostToolUse advisory state logs.

Phase 2 measurement basis (epic #77, SoT `docs/designs/DEJA_CODE_HOOK_MVP.md`
§9 observability + §13 activation gate). This module is **pure + read-only**: it
never opens, writes, or locks the Stop surface's `advisor.jsonl` /
`advised.jsonl` / `capture.lock`, so Stop-hook stats can never be contaminated
by this aggregation (§10 state-isolation contract).

Three hook-local logs are aggregated, each read **across all rotated
generations** (`<base>`, `<base>.1`, `<base>.2` — the hook keeps
LOG_GENERATIONS=3):

- ``pretool_completion.jsonl`` — exactly one record per hook invocation
  (the denominator). Fields: ``source`` (pretooluse/posttooluse/unknown),
  ``tool_name``, ``timed_out``, ``elapsed_ms``, ``eligible_chunks``,
  ``fired_count``, ``outcome_reason``.
- ``pretool_advisory.jsonl`` — one record per candidate cross-repo hit
  (many per invocation). Fields include ``source``, ``fired``, ``sim``.
- ``pretool_advised.jsonl`` — dedup ledger (emitted advisories + first-write
  markers); counted only for completeness, not part of the denominator.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# Must match scripts/hooks/deja_pretool.py (LOG_GENERATIONS). Kept as a local
# constant so this reader has no import dependency on the hook script.
LOG_GENERATIONS = 3

DEFAULT_STATE_DIR = os.path.expanduser("~/.local/state/deja_code")
COMPLETION_BASE = "pretool_completion.jsonl"
ADVISORY_BASE = "pretool_advisory.jsonl"
ADVISED_BASE = "pretool_advised.jsonl"
LOCK_BASE = "pretool.lock"
READ_LOCK_TIMEOUT = 0.5

# The Stop surface's files — this module MUST NOT read them. Named here only so
# a guard test can assert none of these basenames is ever opened.
STOP_SURFACE_FILES = ("advisor.jsonl", "advised.jsonl", "capture.lock")

# Synthetic probe identification is shared with the adoption instrument.  The
# path test catches both the historical fixture and future records that retain
# either side's path.  Add content hashes here if a path-less probe is ever
# emitted; keeping the set canonical avoids measurement tools drifting apart.
KNOWN_PROBE_CONTENT_SHAS: frozenset[str] = frozenset()


def is_probe_record(record: dict) -> bool:
    """Whether an advisory record belongs to the synthetic live probe."""
    path_probe = any(
        "deja_live_probe.js" in str(record.get(field) or "")
        for field in ("new_path", "hit_path")
    )
    return path_probe or str(record.get("new_content_sha") or "") in \
        KNOWN_PROBE_CONTENT_SHAS


@contextlib.contextmanager
def _shared_read_lock(state_dir: str):
    """Best-effort shared flock on the hook's ``pretool.lock`` during a read.

    The hook takes an EXCLUSIVE lock on this file around its append+rotate, so a
    SHARED lock here serializes the reader against an in-flight rotation (which
    would otherwise let the reader re-read a just-rotated ``base`` as ``.1`` and
    miss the new ``base``). Fail-open: if the lock file is absent or the lock is
    not obtained within a short bound, the read proceeds unlocked (this is a
    diagnostic path — availability beats a hard guarantee).
    """
    lock_path = os.path.join(state_dir, LOCK_BASE)
    fd = None
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        yield  # no lock file yet → nothing to serialize against
        return
    try:
        deadline = time.monotonic() + READ_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break  # fail-open: read without the lock
                time.sleep(0.01)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def generation_paths(state_dir: str, base: str,
                     generations: int = LOG_GENERATIONS) -> list[str]:
    """Return the base log path plus its rotated generations that exist.

    Order is newest-append-target first (``base``) then ``.1`` .. ``.N-1``;
    aggregation is order-insensitive but rotation-complete.
    """
    candidates = [os.path.join(state_dir, base)]
    candidates += [os.path.join(state_dir, f"{base}.{i}")
                   for i in range(1, generations)]
    return [p for p in candidates if os.path.exists(p)]


def read_records(state_dir: str, base: str,
                 generations: int = LOG_GENERATIONS) -> list[dict]:
    """Read every JSON object line across a log's rotated generations.

    Malformed lines and non-dict rows are skipped (the writer only ever emits
    dict lines; corruption is tolerated fail-open, matching the hook).
    """
    rows: list[dict] = []
    with _shared_read_lock(state_dir):
        for path in generation_paths(state_dir, base, generations):
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict):
                            rows.append(row)
            except OSError:
                continue
    return rows


def _percentile(sorted_values: list[int], fraction: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[idx]


@dataclass
class SourceStats:
    """Per-source rollup of the completion (invocation) log."""

    invocations: int = 0
    fired_invocations: int = 0          # fired_count > 0
    zero_eligible: int = 0              # eligible_chunks == 0
    timed_out: int = 0
    outcomes: Counter = field(default_factory=Counter)   # outcome_reason -> n
    # The full source×outcome×fired joint (§9): (outcome_reason, fired_bool) -> n.
    outcome_fired: Counter = field(default_factory=Counter)
    _elapsed: list[int] = field(default_factory=list)

    def latency(self) -> dict:
        xs = sorted(self._elapsed)
        return {
            "n": len(xs),
            "min_ms": xs[0] if xs else 0,
            "p50_ms": _percentile(xs, 0.50),
            "p95_ms": _percentile(xs, 0.95),
            "max_ms": xs[-1] if xs else 0,
        }

    def to_dict(self) -> dict:
        # outcome_by_fired[outcome] = {"fired": n, "not_fired": n} — the joint
        # cross-tab; `outcomes` is retained as the outcome marginal for display.
        joint: dict[str, dict[str, int]] = {}
        for (outcome, fired), count in self.outcome_fired.items():
            slot = joint.setdefault(outcome, {"fired": 0, "not_fired": 0})
            slot["fired" if fired else "not_fired"] += count
        return {
            "invocations": self.invocations,
            "fired_invocations": self.fired_invocations,
            "zero_eligible": self.zero_eligible,
            "timed_out": self.timed_out,
            "outcomes": dict(sorted(self.outcomes.items())),
            "outcome_by_fired": dict(sorted(joint.items())),
            "latency": self.latency(),
        }


def aggregate_completions(records: list[dict]) -> dict[str, SourceStats]:
    """Group the completion log by source×outcome×fired, tallying health."""
    by_source: dict[str, SourceStats] = defaultdict(SourceStats)
    for rec in records:
        stats = by_source[str(rec.get("source", "unknown"))]
        outcome = str(rec.get("outcome_reason", "unknown"))
        fired = (rec.get("fired_count") or 0) > 0
        stats.invocations += 1
        stats.outcomes[outcome] += 1
        stats.outcome_fired[(outcome, fired)] += 1
        if fired:
            stats.fired_invocations += 1
        if rec.get("eligible_chunks") == 0:
            stats.zero_eligible += 1
        if rec.get("timed_out"):
            stats.timed_out += 1
        elapsed = rec.get("elapsed_ms")
        if isinstance(elapsed, (int, float)):
            stats._elapsed.append(int(elapsed))
    return dict(by_source)


def aggregate_advisories(records: list[dict]) -> dict:
    """Group candidate-hit records by source×fired; summarize sim + probe share.

    ``probe_fired`` counts fired candidates whose ``new_path`` is the leftover
    ``deja_live_probe.js`` fixture — surfaced separately so the operator can see
    that synthetic fires are NOT organic demand (activation-gate integrity).
    """
    by_source_fired: Counter = Counter()   # (source, fired_bool) -> n
    sims: list[float] = []
    fired_total = 0
    probe_fired = 0
    organic_fired_keys: set[tuple] = set()
    for rec in records:
        source = str(rec.get("source", "unknown"))
        fired = bool(rec.get("fired"))
        by_source_fired[(source, fired)] += 1
        sim = rec.get("sim")
        if isinstance(sim, (int, float)):
            sims.append(float(sim))
        if fired:
            fired_total += 1
            if is_probe_record(rec):
                probe_fired += 1
            else:
                organic_fired_keys.add((
                    rec.get("cwd_repo"), rec.get("new_symbol"),
                    rec.get("hit_chunk_id"),
                ))
    sims.sort()
    return {
        "by_source_fired": {f"{s}/{'fired' if f else 'near'}": n
                            for (s, f), n in sorted(by_source_fired.items())},
        "fired_total": fired_total,
        "probe_fired": probe_fired,
        "organic_fired": fired_total - probe_fired,
        "organic_fired_distinct": len(organic_fired_keys),
        "sim": {
            "n": len(sims),
            "min": round(sims[0], 4) if sims else None,
            "p50": round(sims[len(sims) // 2], 4) if sims else None,
            "max": round(sims[-1], 4) if sims else None,
        },
    }


def collect(state_dir: str = DEFAULT_STATE_DIR) -> dict:
    """Full source×outcome aggregation over all pretool logs + rotations."""
    completions = read_records(state_dir, COMPLETION_BASE)
    advisories = read_records(state_dir, ADVISORY_BASE)
    advised = read_records(state_dir, ADVISED_BASE)
    comp_stats = aggregate_completions(completions)
    return {
        "state_dir": state_dir,
        "generations_read": {
            COMPLETION_BASE: generation_paths(state_dir, COMPLETION_BASE),
            ADVISORY_BASE: generation_paths(state_dir, ADVISORY_BASE),
            ADVISED_BASE: generation_paths(state_dir, ADVISED_BASE),
        },
        "completion_total": len(completions),
        "advisory_total": len(advisories),
        "advised_total": len(advised),
        "by_source": {src: st.to_dict() for src, st in sorted(comp_stats.items())},
        "advisory": aggregate_advisories(advisories),
    }
