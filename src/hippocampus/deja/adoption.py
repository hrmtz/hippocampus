"""Conservative, offline deja-code adoption measurement (§7).

The detector is deliberately high-precision and low-recall: only a newly added
reference to the advised implementation is ``adopted``.  Git and filesystem
reads live behind injectable callables so classification tests need neither a
checkout nor subprocesses.

``tree_reader(repo, path)`` returns text for one relative path, or a
``{relative_path: text}`` mapping when path is ``None``.  ``git_log(repo, path,
fire_time)`` returns a mapping with ``is_git``, ``fire_snapshot_known``,
``content_at_fire``, and ``touched_after`` fields.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Mapping

from ._stats import wilson_ci
from .pretool_stats import (ADVISORY_BASE, DEFAULT_STATE_DIR, is_probe_record,
                            read_records)

STOP_ADVISORY_BASE = "advisor.jsonl"
TALLY_BASE = "adoption_tally.jsonl"
DEFAULT_WORKSHEET = (Path(__file__).resolve().parents[3] / "docs" / "designs" /
                     "deja_demand_sample_20260719" / "worksheet.final.jsonl")
LABELS = ("adopted", "not_adopted", "cant_tell")
TALLY_MARKS = frozenset({"accepted", "ignored", "na"})
HOLD_DECIDED_N = 30

TreeReader = Callable[[str, str | None], str | Mapping[str, str] | None]
GitLog = Callable[[str, str | None, str | None], Mapping[str, object]]


def join_key(record: Mapping[str, object]) -> str:
    """Return the demand-worksheet suffix for an advisory fire record."""
    return f"{str(record.get('session_id') or '')[:8]}-{record.get('hit_chunk_id')}"


def _repo_root(repo: str) -> Path | None:
    candidate = Path(repo).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    sibling = Path(__file__).resolve().parents[4] / repo
    return sibling.resolve() if sibling.is_dir() else None


def _default_tree_reader(repo: str, path: str | None):
    root = _repo_root(repo)
    if root is None:
        raise FileNotFoundError(repo)
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    if probe.returncode or probe.stdout.strip() != "true":
        raise FileNotFoundError(f"not a git tree: {root}")
    if path is not None:
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError(path) from exc
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
        timeout=5, check=False,
    )
    if tracked.returncode:
        raise FileNotFoundError(f"cannot list git tree: {root}")
    result: dict[str, str] = {}
    for raw in tracked.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        try:
            result[rel] = (root / rel).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
    return result


def _default_git_log(repo: str, path: str | None,
                     fire_time: str | None) -> Mapping[str, object]:
    root = _repo_root(repo)
    base: dict[str, object] = {
        "is_git": False, "fire_snapshot_known": False,
        "content_at_fire": None, "touched_after": None,
    }
    if root is None:
        return base
    inside = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    if inside.returncode:
        return base
    base["is_git"] = True
    git_path = path
    if path and os.path.isabs(path):
        try:
            git_path = str(Path(path).resolve().relative_to(root))
        except ValueError:
            return base
    if not fire_time:
        return base
    before = subprocess.run(
        ["git", "-C", str(root), "rev-list", "-1", f"--before={fire_time}",
         "HEAD"], capture_output=True, text=True, timeout=5, check=False,
    )
    revision = before.stdout.strip() if not before.returncode else ""
    if not revision:
        return base
    base["fire_snapshot_known"] = True
    if git_path:
        shown = subprocess.run(
            ["git", "-C", str(root), "show", f"{revision}:{git_path}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        # A missing path at a known revision is established absence, represented
        # by an empty snapshot rather than an unknown one.
        base["content_at_fire"] = shown.stdout if not shown.returncode else ""
        touched = subprocess.run(
            ["git", "-C", str(root), "log", "--format=%H",
             f"{revision}..HEAD", "--", git_path], capture_output=True, text=True,
            timeout=5, check=False,
        )
        if not touched.returncode:
            base["touched_after"] = bool(touched.stdout.strip())
    return base


def _files(tree: str | Mapping[str, str] | None,
           path: str | None) -> dict[str, str]:
    if isinstance(tree, str):
        return {path or "": tree}
    if isinstance(tree, Mapping):
        return {str(name): str(text) for name, text in tree.items()
                if isinstance(text, str)}
    return {}


def _has_reference(text: str, hit_path: object, hit_symbol: object,
                   new_symbol: object = None) -> bool:
    module = Path(str(hit_path or "")).stem
    symbol = str(hit_symbol or "").split(".")[-1]
    import_hit = False
    if module:
        quoted_module = re.escape(module)
        import_hit = bool(re.search(
            rf"(?m)^\s*(?:from\s+[\w.]*{quoted_module}[\w.]*\s+import\b|"
            rf"import\s+[^\n]*\b{quoted_module}\b|"
            rf"(?:import[^\n]*from\s*|require\s*\()['\"][^'\"]*"
            rf"{quoted_module}[^'\"]*['\"])", text,
        ))
    # Self-name guard: when the advised symbol shares the leaf name of the code
    # being written (the *common* case — deja fires precisely on similarly-named
    # cross-repo code, e.g. wilson_ci↔wilson_ci), a bare call is indistinguishable
    # from the writer calling *their own* copy.  Require an import in that case;
    # counting the self-call would inflate adoption (the §4b −0.26 lesson).
    if symbol and str(new_symbol or "").split(".")[-1] == symbol:
        return import_hit
    call_hit = False
    if symbol:
        call_re = re.compile(rf"(?<![\w$]){re.escape(symbol)}\s*\(")
        for line in text.splitlines():
            match = call_re.search(line)
            if not match:
                continue
            prefix = line[:match.start()].strip()
            # A declaration is not evidence of calling/reusing the advised
            # symbol.  Be conservative for Python/JS named functions and JS
            # method shorthand; false negatives only lower detector recall.
            if re.search(r"\b(?:def|function|class)\s*$", prefix):
                continue
            if not prefix and re.search(r"\)\s*\{\s*$", line):
                continue
            call_hit = True
            break
    return import_hit or call_hit


def _symbol_content(path: str, text: str, symbol: object) -> str | None:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    wanted = str(symbol or "")
    try:
        # The indexer's parser is an optional deployment dependency.  Adoption
        # remains usable in the light base install and falls back conservatively
        # when it is absent.
        from .chunker import chunk_source
        chunks = chunk_source(text.encode("utf-8", errors="replace"), ext)
    except Exception:
        chunks = None
    if chunks is None:
        return _fallback_symbol_content(text, wanted, ext)
    exact = [chunk for chunk in chunks if chunk.symbol == wanted]
    if exact:
        return exact[0].content
    return None


def _fallback_symbol_content(text: str, symbol: str, ext: str) -> str | None:
    """Best-effort symbol region when tree-sitter is unavailable.

    False negatives become E3/E5/E4, never E1, so this fallback cannot inflate
    adoption.  Python indentation and brace-balanced JS/TS cover the hook's
    supported languages sufficiently for the exact-unchanged E2 check.
    """
    leaf = symbol.split(".")[-1]
    lines = text.splitlines()
    if ext == "py":
        start = next((index for index, line in enumerate(lines)
                      if re.match(rf"^\s*(?:async\s+)?def\s+{re.escape(leaf)}\s*\(",
                                  line)), None)
        if start is None:
            return None
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                end = index
                break
        return "\n".join(lines[start:end])

    if ext in {"js", "mjs", "cjs", "ts"}:
        pattern = re.compile(
            rf"\b(?:function\s+)?{re.escape(leaf)}\b[^\n]*?(?:=>\s*)?\{{")
        start = next((index for index, line in enumerate(lines)
                      if pattern.search(line)), None)
        if start is None:
            return None
        depth = 0
        seen = False
        for index in range(start, len(lines)):
            depth += lines[index].count("{") - lines[index].count("}")
            seen = seen or "{" in lines[index]
            if seen and depth <= 0:
                return "\n".join(lines[start:index + 1])
        return None

    if ext in {"sh", "bash"}:
        start = next((index for index, line in enumerate(lines)
                      if re.match(rf"^\s*(?:function\s+)?{re.escape(leaf)}\s*\(?.*\{{",
                                  line)), None)
        if start is None:
            return None
        depth = 0
        for index in range(start, len(lines)):
            depth += lines[index].count("{") - lines[index].count("}")
            if depth <= 0 and index > start:
                return "\n".join(lines[start:index + 1])
    return None


def _fire_time(record: Mapping[str, object]) -> str | None:
    for field in ("fire_ts", "fire_time", "ts", "timestamp", "created_at"):
        value = record.get(field)
        if value:
            return str(value)
    return None


def classify(record: Mapping[str, object], *, tree_reader: TreeReader,
             git_log: GitLog, fire_time: str | None = None) -> tuple[str, str]:
    """Classify one fired advisory using the E0–E5 first-match cascade."""
    if is_probe_record(dict(record)):
        return "excluded", "E0"

    repo = str(record.get("cwd_repo") or "")
    path_value = record.get("new_path")
    path = str(path_value) if path_value else None
    if not repo:
        return "cant_tell", "E5"
    # Path-anchored only.  A path-less record (the entire Stop surface, 213/213)
    # cannot be located offline: a repo-wide symbol scan is O(records×files),
    # unsound (a reference in file X can't establish absence in symbol-file Y),
    # and — like every mode — blocked by the missing fire timestamp (§7.2/§7.4).
    # It yields nothing but `cant_tell`, so we return that without touching disk.
    if not path:
        return "cant_tell", "E5"
    try:
        tree = tree_reader(repo, path)
    except (OSError, subprocess.SubprocessError):
        return "cant_tell", "E5"
    files = _files(tree, path)
    if not files:
        return "cant_tell", "E5"

    new_symbol = record.get("new_symbol")
    symbol_content = next(
        (content for name, text in files.items()
         if (content := _symbol_content(name, text, new_symbol)) is not None),
        None)
    reference_now = any(_has_reference(text, record.get("hit_path"),
                                       record.get("hit_symbol"), new_symbol)
                        for text in files.values())

    effective_time = fire_time or _fire_time(record)
    if reference_now:
        # E1 (the ONLY path to `adopted`) requires proving the reference was
        # *absent at fire time*.  That needs a known pre-fire snapshot, which
        # needs a fire timestamp the current logs do not record — so E1 stays
        # dormant (→ E4) rather than ever manufacturing adoption. Adding `fire_ts`
        # to the hook writer would activate it (recommended, out-of-scope here).
        info = git_log(repo, path, effective_time)
        if info.get("is_git") and info.get("fire_snapshot_known"):
            old_text = str(info.get("content_at_fire") or "")
            if not _has_reference(old_text, record.get("hit_path"),
                                  record.get("hit_symbol"), new_symbol):
                return "adopted", "E1"
        return "cant_tell", "E4"

    if symbol_content is None:
        return "cant_tell", "E3"
    expected = str(record.get("new_content_sha") or "")
    if expected and hashlib.sha256(symbol_content.encode(
            "utf-8", errors="replace")).hexdigest() == expected:
        return "not_adopted", "E2"
    return "cant_tell", "E4"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _worksheet_key(item_id: object) -> str:
    return re.sub(r"^\d{2}-", "", str(item_id or ""), count=1)


def _rate(rows: list[dict]) -> dict:
    counts = Counter(str(row["label"]) for row in rows)
    adopted = counts["adopted"]
    decided = adopted + counts["not_adopted"]
    point, low, high = wilson_ci(adopted, decided)
    return {
        "n_total": len(rows), "n_decided": decided, "n_adopted": adopted,
        "n_not_adopted": counts["not_adopted"],
        "n_cant_tell": counts["cant_tell"],
        "rate": round(point, 4) if decided else None,
        "wilson95": [round(low, 4), round(high, 4)] if decided else None,
        "coverage": round(decided / len(rows), 4) if rows else None,
    }


def _breakdowns(rows: list[dict], field: str) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row[field])].append(row)
    return {name: _rate(group) for name, group in sorted(grouped.items())}


def collect(*, state_dir: str = DEFAULT_STATE_DIR,
            worksheet_final: str | os.PathLike[str] | None = None,
            tally_path: str | os.PathLike[str] | None = None,
            tree_reader: TreeReader | None = None,
            git_log: GitLog | None = None) -> dict:
    """Read both advisory surfaces, classify fires, and aggregate §7 metrics."""
    reader = tree_reader or _default_tree_reader
    history = git_log or _default_git_log
    raw: list[dict] = []
    for surface, base in (("pretool", ADVISORY_BASE), ("stop", STOP_ADVISORY_BASE)):
        for record in read_records(state_dir, base):
            if record.get("fired"):
                tagged = dict(record)
                tagged["surface"] = surface
                raw.append(tagged)

    records: list[dict] = []
    excluded = 0
    for record in raw:
        label, evidence = classify(record, tree_reader=reader, git_log=history)
        if label == "excluded":
            excluded += 1
            continue
        record_id = join_key(record)
        records.append({**record, "record_id": record_id, "label": label,
                        "evidence_code": evidence,
                        "anchor": "path" if record.get("new_path") else "symbol"})

    worksheet_path = Path(worksheet_final) if worksheet_final is not None \
        else DEFAULT_WORKSHEET
    worksheet_available = worksheet_path.is_file()
    verdicts = {_worksheet_key(row.get("item_id")): row.get("verdict")
                for row in _read_jsonl(worksheet_path)} if worksheet_available else {}
    for record in records:
        record["reusability_verdict"] = verdicts.get(record["record_id"])
    joined = [row for row in records if row["record_id"] in verdicts]
    reusable = [row for row in joined
                if str(row.get("reusability_verdict") or "").lower() == "reusable"]

    gate = _rate(reusable) if worksheet_available else None
    unconditional = _rate(joined if worksheet_available else records)
    actual_tally_path = (Path(tally_path) if tally_path is not None
                         else Path(state_dir) / TALLY_BASE)
    tally_rows = _read_jsonl(actual_tally_path) if str(actual_tally_path) else []
    latest = {str(row.get("record_id") or ""): str(row.get("mark") or "")
              for row in tally_rows if row.get("record_id")}
    known_ids = {row["record_id"] for row in records}
    decided_marks = [mark for key, mark in latest.items()
                     if key in known_ids and mark in {"accepted", "ignored"}]
    accepted = decided_marks.count("accepted")
    tally_point, tally_low, tally_high = wilson_ci(accepted, len(decided_marks))
    reusable_ids = {row["record_id"] for row in reusable}
    reusable_marks = [mark for key, mark in latest.items()
                      if key in reusable_ids and mark in {"accepted", "ignored"}]
    reusable_accepted = reusable_marks.count("accepted")
    rp, rl, rh = wilson_ci(reusable_accepted, len(reusable_marks))
    overlaps = [(row["label"], latest[row["record_id"]]) for row in records
                if row["record_id"] in latest
                and row["label"] in {"adopted", "not_adopted"}
                and latest[row["record_id"]] in {"accepted", "ignored"}]
    agreements = sum((label == "adopted") == (mark == "accepted")
                     for label, mark in overlaps)

    notes = []
    if not worksheet_available:
        notes.append("HOLD: worksheet.final.jsonl absent; reusable-conditioned metric skipped")
    elif gate and gate["n_decided"] < HOLD_DECIDED_N:
        notes.append(f"HOLD: reusable decided-N={gate['n_decided']} is too small")
    organic_pretool_n = sum(row["surface"] == "pretool" for row in records)
    if organic_pretool_n < HOLD_DECIDED_N:
        notes.append(f"organic pretool N={organic_pretool_n}; do not annualize")
    # Structural-blocker banners (§7.2/§7.4): make the two data-schema gaps that
    # cap the detector explicit in every report, so a 0-adopted result is read as
    # "instrument blind", not "measured zero adoption".
    n_pathless = sum(row["anchor"] == "symbol" for row in records)
    if n_pathless:
        notes.append(
            f"{n_pathless} path-less (Stop-surface) records are offline-"
            "undetectable → cant_tell; only the pretool surface carries new_path")
    n_adopted = sum(row["label"] == "adopted" for row in records)
    if n_adopted == 0 and records:
        notes.append(
            "0 `adopted`: E1 needs a fire timestamp the hook does not log — "
            "detector emits only not_adopted/cant_tell until `fire_ts` is added; "
            "use the operator tally (--tally) as the primary adoption signal")

    return {
        "state_dir": state_dir,
        "worksheet": {"path": str(worksheet_path),
                      "available": worksheet_available, "joined_n": len(joined)},
        "n_fired_read": len(raw), "n_excluded_probe": excluded,
        "n_classified": len(records), "organic_pretool_n": organic_pretool_n,
        "adoption": gate,
        "unconditional_complement": unconditional,
        "by_surface": _breakdowns(records, "surface"),
        "by_anchor": _breakdowns(records, "anchor"),
        "tally": {
            "path": str(actual_tally_path), "marks_n": len(latest),
            # The primary tally rate is conditioned on adjudicated reusable
            # fires, matching the ROI adoption term.  ``all_*`` is the non-gate
            # monitoring complement.
            "decided_n": len(reusable_marks),
            "accepted_n": reusable_accepted,
            "rate": round(rp, 4) if reusable_marks else None,
            "wilson95": ([round(rl, 4), round(rh, 4)]
                         if reusable_marks else None),
            "all_decided_n": len(decided_marks),
            "all_accepted_n": accepted,
            "all_rate": round(tally_point, 4) if decided_marks else None,
            "all_wilson95": ([round(tally_low, 4), round(tally_high, 4)]
                             if decided_marks else None),
            "agreement_n": len(overlaps),
            "agreement": round(agreements / len(overlaps), 4) if overlaps else None,
        },
        "notes": notes,
        "records": records,
    }


def record_tally(state_dir: str, record_id: str, mark: str) -> str:
    """Append one operator adoption mark and return the tally path."""
    if mark not in TALLY_MARKS:
        raise ValueError(f"mark must be one of {sorted(TALLY_MARKS)}")
    path = Path(state_dir).expanduser() / TALLY_BASE
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    row = {"record_id": record_id, "mark": mark,
           "ts": dt.datetime.now(dt.timezone.utc).isoformat()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return str(path)
