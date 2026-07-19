"""deja-code — cross-repo 車輪再発明検出 (issue #76).

"Have we written this before?" — function-level semantic index of allowlisted
repos (schema `code`), queried by the Stop-hook advisor and the `deja search`
CLI. Design: docs/designs/DEJA_CODE.md (dual-magi plateau'd).

CLI (dispatched from hippocampus.cli._MODULE_COMMANDS):
  hippocampus deja index  [--repo NAME] [--dry-run] [--full]
  hippocampus deja search "query text" [-k 5] [--exclude-repo NAME]
  hippocampus deja stats
  hippocampus deja adoption [--json] [--tally RECORD_ID MARK]
"""
from __future__ import annotations

import argparse
import sys


def _cmd_index(args) -> int:
    from .index import run_index
    return run_index(args.repo, args.dry_run, args.full)


def _cmd_search(args) -> int:
    import psycopg2
    from pgvector.psycopg2 import register_vector

    from ..config import Settings
    from ..embed.client import EmbedClient

    settings = Settings.load()
    conn = psycopg2.connect(settings.pg_url, connect_timeout=10)
    register_vector(conn)
    try:
        vec = EmbedClient().encode(args.query, where="cli.deja-search",
                                   max_length=1024)
        cur = conn.cursor()
        sql = """
            SELECT f.repo_id, f.path, ck.symbol, ck.kind,
                   ck.start_line, -(ck.dense <#> %s::halfvec) AS sim
            FROM code.chunks ck
            JOIN code.files f ON f.file_id = ck.file_id
            WHERE ck.dense IS NOT NULL
        """
        params: list = [vec]
        if args.exclude_repo:
            sql += " AND f.repo_id <> %s"
            params.append(args.exclude_repo)
        sql += " ORDER BY ck.dense <#> %s::halfvec LIMIT %s"
        params.extend([vec, args.k])
        cur.execute(sql, params)
        rows = cur.fetchall()
        if not rows:
            print("no hits (is the index populated? try: hippocampus deja stats)")
            return 0
        for repo, path, symbol, kind, line, sim in rows:
            print(f"  {sim:.4f}  {repo} {path}:{line}  {symbol} ({kind})")
        return 0
    finally:
        conn.close()


def _cmd_stats(args) -> int:
    import json
    import os

    # `--source --json` is the machine-readable, file-local measurement path:
    # emit ONLY the isolated pretool aggregation as standalone JSON — no PG
    # query, no Stop-surface read — so consumers get valid JSON and the path
    # stays isolated even when PG/Stop are unavailable (§9).
    if getattr(args, "source", False) and getattr(args, "json", False):
        from . import pretool_stats
        print(json.dumps(pretool_stats.collect(), ensure_ascii=False, indent=2))
        return 0

    import psycopg2

    from ..config import Settings

    settings = Settings.load()
    conn = psycopg2.connect(settings.pg_url, connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.repo_id, r.file_count, r.chunk_count, r.indexed_at
            FROM code.repos r ORDER BY r.repo_id""")
        rows = cur.fetchall()
        if not rows:
            print("index empty — run: hippocampus deja index")
        else:
            print(f"{'repo':<28} {'files':>6} {'chunks':>7}  indexed_at")
            for repo, files, chunks, ts in rows:
                print(f"{repo:<28} {files:>6} {chunks:>7}  {ts}")
        cur.execute("SELECT COUNT(*) FROM code.chunks WHERE dense IS NULL")
        nulls = cur.fetchone()[0]
        print(f"dense NULL: {nulls}" + ("  (!! embed gap)" if nulls else ""))
    finally:
        conn.close()

    log_path = os.path.expanduser("~/.local/state/deja_code/advisor.jsonl")
    fired = near = 0
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("fired"):
                    fired += 1
                else:
                    near += 1
        print(f"advisor log (Stop surface): fired={fired} near-miss={near} ({log_path})")
    except OSError:
        print("advisor log (Stop surface): none yet (Phase 1 hook not active or no events)")

    if getattr(args, "source", False):
        _print_pretool_stats(args)
    return 0


def _print_pretool_stats(args) -> None:
    """Source×outcome breakdown of the PreTool/PostTool advisory logs (§9).

    Separate from the Stop `advisor.jsonl` totals above — the pretool logs are a
    distinct, versioned, source-tagged surface, so Stop stats are uncontaminated.
    The `--json` variant is handled earlier in `_cmd_stats` as a file-local path;
    this human view is reached only for `--source` without `--json`.
    """
    from . import pretool_stats

    report = pretool_stats.collect()
    print()
    print(f"pretool advisory logs (source×outcome, "
          f"{report['completion_total']} invocations across rotations):")
    if not report["by_source"]:
        print("  none yet (PreTool/PostTool advisory hook not active or no events)")
    for source, st in report["by_source"].items():
        lat = st["latency"]
        print(f"  [{source}] invocations={st['invocations']} "
              f"fired={st['fired_invocations']} zero-eligible={st['zero_eligible']} "
              f"timed_out={st['timed_out']} "
              f"latency(p50/p95/max ms)={lat['p50_ms']}/{lat['p95_ms']}/{lat['max_ms']}")
        for reason, count in st["outcomes"].items():
            print(f"       {reason:28} {count}")
    adv = report["advisory"]
    print(f"  candidate hits: {report['advisory_total']}  "
          f"fired={adv['fired_total']} (probe={adv['probe_fired']} "
          f"organic={adv['organic_fired']}, distinct organic={adv['organic_fired_distinct']})")
    print(f"  sim(min/p50/max)={adv['sim']['min']}/{adv['sim']['p50']}/{adv['sim']['max']}")
    if adv["organic_fired"] == 0 and adv["fired_total"] > 0:
        print("  ⚠ all fired advisories are probe-file artifacts — "
              "no organic demand signal yet (activation gate: HOLD)")


def _cmd_doctor(args) -> int:
    import json

    from . import doctor

    report = doctor.run()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    icon = {"ok": "✓", "insufficient_data": "·", "warn": "!", "crit": "✗"}
    print(f"deja doctor — overall: {report['overall']}  "
          f"({report['completion_total']} invocations across rotations)")
    if not report["sources"]:
        print("  no completion records yet (hook inactive or no events)")
        return 0
    for source, diag in report["sources"].items():
        print(f"  [{source}] {diag['overall']}  ({diag['invocations']} invocations)")
        for name, chk in diag["checks"].items():
            print(f"    {icon.get(chk['status'], '?')} {name:20} "
                  f"{chk['status']:18} {chk['detail']}")
    return 0


def _cmd_adoption(args) -> int:
    import json

    from . import adoption

    if args.tally:
        record_id, mark = args.tally
        path = adoption.record_tally(args.state_dir, record_id, mark)
        print(f"recorded {record_id}={mark} → {path}")
        return 0

    report = adoption.collect(
        state_dir=args.state_dir,
        worksheet_final=args.worksheet,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    gate = report["adoption"]
    print(f"deja adoption — classified={report['n_classified']} "
          f"excluded-probe={report['n_excluded_probe']} "
          f"organic-pretool={report['organic_pretool_n']}")
    if gate is None:
        print("  gate metric: unavailable (no adjudication worksheet)")
    else:
        print(f"  P(acted|fired,reusable): {gate['rate']} "
              f"Wilson-95={gate['wilson95']} "
              f"decided={gate['n_decided']}/{gate['n_total']} "
              f"coverage={gate['coverage']}")
    complement = report["unconditional_complement"]
    print(f"  P(acted|fired), non-gate: {complement['rate']} "
          f"decided={complement['n_decided']}/{complement['n_total']}")
    tally = report["tally"]
    if tally["decided_n"]:
        print(f"  tally: rate={tally['rate']} n={tally['decided_n']} "
              f"detector-agreement={tally['agreement']} "
              f"overlap-n={tally['agreement_n']}")
    for note in report["notes"]:
        print(f"  ! {note}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hippocampus deja",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="incremental index of allowlisted repos")
    p_index.add_argument("--repo", help="single repo (must be allowlisted)")
    p_index.add_argument("--dry-run", action="store_true")
    p_index.add_argument("--full", action="store_true",
                         help="re-chunk every file even if file_sha unchanged")
    p_index.set_defaults(fn=_cmd_index)

    p_search = sub.add_parser("search", help="semantic top-k over the index")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=5)
    p_search.add_argument("--exclude-repo")
    p_search.set_defaults(fn=_cmd_search)

    p_stats = sub.add_parser("stats", help="index + advisor-log summary")
    p_stats.add_argument("--source", action="store_true",
                         help="add PreTool/PostTool source×outcome breakdown (§9)")
    p_stats.add_argument("--json", action="store_true",
                         help="emit the --source breakdown as JSON")
    p_stats.set_defaults(fn=_cmd_stats)

    p_doctor = sub.add_parser("doctor",
                              help="zero-eligible / excessive-timeout canary (§9)")
    p_doctor.add_argument("--json", action="store_true",
                          help="emit the health summary as JSON")
    p_doctor.set_defaults(fn=_cmd_doctor)

    from .pretool_stats import DEFAULT_STATE_DIR

    p_adoption = sub.add_parser(
        "adoption", help="offline adoption detector + adjudication join (§7)")
    p_adoption.add_argument("--json", action="store_true",
                            help="emit the adoption report as JSON")
    p_adoption.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                            help="advisory/tally state directory")
    p_adoption.add_argument("--worksheet",
                            help="worksheet.final.jsonl override")
    p_adoption.add_argument(
        "--tally", nargs=2, metavar=("RECORD_ID", "MARK"),
        choices=None,
        help="append RECORD_ID mark (accepted, ignored, or na)")
    p_adoption.set_defaults(fn=_cmd_adoption)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"deja: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
