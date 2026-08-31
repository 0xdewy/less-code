"""lc — the less-code CLI.

  lc analyze <path>          # language, files, code-LOC baseline
  lc audit <path>            # mutation score of the test suite (trust oracle)
  lc reduce <path>           # static + verify-gated LLM reduction
  lc report <path>           # render markdown summary from reduce JSON
  lc bench [dir]             # reduce every fixture, score it, append a row
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .audit import audit, audit_to_json
from .langdetect import map_project
from .loc import count_tree, formatter_available
from .pipeline import ReduceStats, reduce_project, write_report


def cmd_analyze(args: argparse.Namespace) -> int:
    project = map_project(Path(args.path), args.lang)
    loc = count_tree(project.source_files, project.lang, format_first=True)
    print(json.dumps({
        "root": str(project.root),
        "lang": project.lang,
        "source_files": [str(p) for p in project.source_files],
        "test_files": [str(p) for p in project.test_files],
        "loc": {
            "code": loc.code, "comment": loc.comment, "blank": loc.blank,
            "tokens": loc.tokens, "ast_nodes": loc.ast_nodes,
            # canonical-format counting (B1); raw counts when the tool is absent
            "formatted": loc.formatted and formatter_available(project.lang),
        },
    }, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    project = map_project(Path(args.path), args.lang)
    result = audit(Path(args.path), project.lang, project.source_files,
                   max_mutants=args.max_mutants)
    audit_to_json(result, Path(args.out))
    print(json.dumps({"total": result.total, "killed": result.killed,
                      "score": round(result.score, 4),
                      "skipped_no_baseline": result.skipped_no_baseline}, indent=2))
    if result.skipped_no_baseline:
        print("baseline tests are not green — fix before reducing", file=sys.stderr)
        return 2
    return 0 if result.score >= args.min_score else 1


def cmd_reduce(args: argparse.Namespace) -> int:
    backend = None
    if not args.static_only:
        from .backends import BudgetBackend, make_backend
        backend = make_backend(args.backend, args.model, num_ctx=args.num_ctx,
                               timeout=args.llm_timeout)
        if args.max_llm_calls:
            backend = BudgetBackend(backend, args.max_llm_calls)
    stats = reduce_project(
        Path(args.path), lang=args.lang, backend=backend,
        attempts_per_file=args.attempts, max_files=args.max_files,
        formatter=not args.no_format, test_timeout=args.timeout,
    )
    out = Path(args.out)
    write_report(stats, out)
    payload = stats.to_json()
    print(json.dumps({k: payload[k] for k in
                      ("loc_start", "loc_after_static", "loc_final",
                       "static_pct", "hybrid_pct", "tests_ok", "api_ok")}, indent=2))
    print(f"report: {out}")
    ok = stats.tests_ok and stats.api_ok and stats.loc_final < stats.loc_start
    return 0 if ok else 1


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import markdown_table, run_bench

    rows, out = run_bench(
        Path(args.path),
        Path(args.out_dir),
        config="static-only" if args.static_only else f"hybrid:{args.model or args.backend}",
        repo=Path(__file__).resolve().parent.parent,
        backend_spec=args.backend,
        model=args.model,
        attempts=args.attempts,
        max_llm_calls=args.max_llm_calls,
        timeout=args.timeout,
        num_ctx=args.num_ctx,
        llm_timeout=args.llm_timeout,
    )
    table = markdown_table(rows)
    print(table)
    print(f"\nrows: {out}")
    if args.markdown:
        Path(args.markdown).write_text(table + "\n", encoding="utf-8")
    # bench measures; it only fails on a broken run (red tests, changed API,
    # or a hidden-test regression the visible gate let through)
    ok = all(
        r.tests_ok and r.api_ok and r.hidden_ok is not False for r in rows
    )
    return 0 if rows and ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.json).read_text())
    r = data["reduce"]
    lines = [
        "# less-code report",
        "",
        f"- language: `{r['lang']}`",
        f"- LOC: {r['loc_start']} → {r['loc_final']} "
        f"(static {r['loc_after_static']})",
        f"- reduction: static **{r['static_pct']}%** + LLM **{r['llm_extra_pct']}%**"
        f" = hybrid **{r['hybrid_pct']}%**",
        f"- tests green: {r['tests_ok']}  |  API preserved: {r['api_ok']}",
        f"- LOC counted after canonical formatting: {r.get('formatted_loc', False)}",
        f"- attempts: {len(r['attempts'])} "
        f"(accepted: {sum(1 for a in r['attempts'] if a['outcome'] == 'accepted')})",
    ]
    if "audit" in data:
        a = data["audit"]
        lines.append(f"- mutation score: {a['score'] * 100:.1f}% "
                     f"({a['killed']}/{a['total']} mutants killed)")
    Path(args.out).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lc", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"lc {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="map project + LOC baseline")
    p.add_argument("path")
    p.add_argument("--lang")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("audit", help="mutation-score the test suite")
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument("--max-mutants", type=int, default=40)
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--out", default="audit.json")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("reduce", help="static + verify-gated LLM reduction")
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument("--backend", default="ollama", choices=["none", "ollama", "openai"])
    p.add_argument("--model", default=None)
    p.add_argument("--static-only", action="store_true")
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--max-files", type=int, default=None)
    p.add_argument("--no-format", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--max-llm-calls", type=int, default=8,
                   help="hard cap on LLM calls (GPU budget on shared machines)")
    p.add_argument("--num-ctx", type=int, default=16384, help="context window for local backend")
    p.add_argument("--llm-timeout", type=int, default=600,
                   help="seconds to wait for one LLM response before recording "
                        "backend-error (a slow GPU needs more than the 600s default)")
    p.add_argument("--out", default="reduce-report.json")
    p.set_defaults(func=cmd_reduce)

    p = sub.add_parser("bench", help="reduce every fixture, score it, append a bench row")
    p.add_argument("path", nargs="?", default="fixtures")
    p.add_argument("--backend", default="ollama", choices=["none", "ollama", "openai"])
    p.add_argument("--model", default=None)
    p.add_argument("--static-only", action="store_true")
    p.add_argument("--attempts", type=int, default=2)
    p.add_argument("--max-llm-calls", type=int, default=6)
    p.add_argument("--num-ctx", type=int, default=16384)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--llm-timeout", type=int, default=600,
                   help="seconds to wait for one LLM response before recording "
                        "backend-error (a slow GPU needs more than the 600s default)")
    p.add_argument("--out-dir", default="bench/results")
    p.add_argument("--markdown", default=None, help="also write the table here")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("report", help="markdown summary from reduce JSON")
    p.add_argument("--json", default="reduce-report.json")
    p.add_argument("--out", default="REDUCTION.md")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
