"""`lc` — the less-code CLI.

lc analyze <path>   map project + code-LOC baseline
lc shrink  <path>   shrink the codebase using existing static tools + rules
                    every layer gated by the frozen test suite
lc propose <path>   mine consented deletion proposals (never applies
                    without explicit group ids)
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import propose as propose_mod
from .langdetect import map_project
from .loc import count_tree, formatter_available
from .pipeline import shrink_project, write_report


def cmd_analyze(args: argparse.Namespace) -> int:
    project = map_project(Path(args.path), args.lang)
    loc = count_tree(project.source_files, project.lang, format_first=True)
    print(
        json.dumps(
            {
                "root": str(project.root),
                "lang": project.lang,
                "source_files": [str(p) for p in project.source_files],
                "test_files": [str(p) for p in project.test_files],
                "loc": {
                    "code": loc.code,
                    "comment": loc.comment,
                    "blank": loc.blank,
                    "tokens": loc.tokens,
                    "ast_nodes": loc.ast_nodes,
                    "formatted": loc.formatted and formatter_available(project.lang),
                },
            },
            indent=2,
        )
    )
    return 0


def _resolve_target(path: Path, copy_to: str | None) -> Path:
    """`shrink` writes to its target; `--copy-to DIR` makes it work on a copy."""
    if copy_to:
        dest = Path(copy_to)
        if dest.exists() and any(dest.iterdir()):
            raise SystemExit(f"--copy-to {dest} exists and is not empty")
        shutil.copytree(path, dest, dirs_exist_ok=True)
        print(
            f"shrinking a copy at {dest} (original {path} untouched)", file=sys.stderr
        )
        return dest
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", str(path)],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return path
    if proc.returncode == 0 and proc.stdout.strip():
        print(
            f"WARNING: shrinking {path} IN PLACE and it has uncommitted changes — "
            "use --copy-to DIR to shrink a copy instead.",
            file=sys.stderr,
        )
    return path


def cmd_shrink(args: argparse.Namespace) -> int:
    target = _resolve_target(Path(args.path), args.copy_to)
    # build the ML backend if --ml-cli was given
    ml_backend = None
    if getattr(args, "ml_cli", None):
        from .ml_shrink import CliBackend

        ml_backend = CliBackend(args.ml_cli, timeout=getattr(args, "ml_timeout", 60.0))
    ml_file_backend = None
    if getattr(args, "ml_file_cli", None):
        from .ml_file import FileBackend

        ml_file_backend = FileBackend(
            args.ml_file_cli, timeout=getattr(args, "ml_file_timeout", 240.0)
        )
    test_command = shlex.split(args.test_command) if args.test_command else None
    stats = shrink_project(
        target,
        lang=args.lang,
        test_timeout=args.timeout,
        ml_backend=ml_backend,
        ml_attempts=args.ml_attempts,
        ml_symbols=args.ml_symbols,
        test_command=test_command,
        ml_file_backend=ml_file_backend,
        ml_file_attempts=args.ml_attempts,
    )
    write_report(stats, Path(args.out))
    payload = stats.to_json()
    print(
        json.dumps(
            {
                k: payload[k]
                for k in (
                    "loc_start",
                    "loc_after_static",
                    "loc_final",
                    "tests_ok",
                    "api_ok",
                    "docs_ok",
                    "gate_tests_run",
                )
            },
            indent=2,
        )
    )
    if stats.gate_tests_run == 0:
        print(
            "WARNING: the test gate ran 0 tests; only the API and "
            "documentation checks back this reduction.",
            file=sys.stderr,
        )
    print(f"report: {args.out}")
    ok = stats.tests_ok and stats.api_ok and stats.docs_ok
    return 0 if ok else 1


def tests_run_text(count) -> str:
    if count is None:
        return "unknown (unrecognized runner output)"
    if count == 0:
        return "0 tests — WARNING: vacuous gate, only API/docs checks apply"
    return f"{count} tests"


def _propose_target(
    path: Path, copy_to: str | None, in_place: bool, fresh: bool
) -> Path:
    """propose always works on a copy: the auto sibling `<path>-proposals`
    unless --copy-to DIR or --in-place. Originals are sacred."""
    if in_place:
        return path
    if copy_to:
        dest = Path(copy_to)
        if dest.exists() and any(dest.iterdir()):
            if fresh:
                raise SystemExit(f"--copy-to {dest} exists and is not empty")
            return dest
        shutil.copytree(path, dest, dirs_exist_ok=True)
        return dest
    dest = path.parent / f"{path.name}-proposals"
    if fresh and dest.exists():
        shutil.rmtree(dest)
    if not dest.exists() or not any(dest.iterdir()):
        shutil.copytree(path, dest, dirs_exist_ok=True)
    return dest


def cmd_propose(args: argparse.Namespace) -> int:
    target = Path(args.path)
    applying = bool(args.apply)
    work = _propose_target(target, args.copy_to, args.in_place, fresh=not applying)
    if args.in_place and applying:
        print(
            f"WARNING: applying deletion groups IN PLACE on {target}",
            file=sys.stderr,
        )
    out = Path(args.out) if args.out else work / "proposals.json"
    test_command = shlex.split(args.test_command) if args.test_command else None
    if not applying:
        payload, code = propose_mod.propose_tree(
            work,
            lang=args.lang,
            app=args.app,
            coverage=args.coverage,
            test_command=test_command,
            timeout=args.timeout,
            out=out,
        )
        propose_mod.print_table(payload, work)
        print(f"proposals: {out}")
        if code == 2:
            print(
                "nothing is deleted until you pass --apply with explicit group ids",
                file=sys.stderr,
            )
        return code
    ids = (
        [group["id"] for group in propose_mod._load_runs(out)[0].get("groups", [])]
        if args.apply == "all"
        else [part.strip() for part in args.apply.split(",") if part.strip()]
    )
    _, code = propose_mod.apply_proposals(work, ids, test_command, args.timeout, out)
    return code


def cmd_rewrite(args: argparse.Namespace) -> int:
    from .ml_file import FileBackend, rewrite_project

    target = Path(args.path)
    if not args.in_place:
        dest = (
            Path(args.copy_to)
            if args.copy_to
            else target.parent / f"{target.name}-rewrite"
        )
        if not (dest.exists() and any(dest.iterdir())):
            shutil.copytree(target, dest, dirs_exist_ok=True)
            print(
                f"rewriting a copy at {dest} (original {target} untouched)",
                file=sys.stderr,
            )
        target = dest
    backend = FileBackend(args.ml_cli, timeout=args.ml_timeout)
    test_command = shlex.split(args.test_command) if args.test_command else None
    stats, records, tests_ok = rewrite_project(
        target,
        backend,
        attempts=args.attempts,
        test_timeout=args.timeout,
        test_command=test_command,
        lang=args.lang,
    )
    payload = {
        "rewrite": {
            "root": str(target),
            "backend": backend.name,
            "digest": getattr(backend, "digest", None),
            "stats": stats,
            "records": records,
            "tests_ok": tests_ok,
        }
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "loc_before": stats["loc_before"],
                "loc_after": stats["loc_after"],
                "accepted_files": stats.get("accepted_files", 0),
                "tests_ok": tests_ok,
            },
            indent=2,
        )
    )
    print(f"report: {out}")
    return 0 if tests_ok else 1


def cmd_dedup(args: argparse.Namespace) -> int:
    from .dedup_tx import TxBackend, dedup_tree, default_tx_gate
    from .langdetect import map_project
    from .testrunners import run_tests

    target = Path(args.path)
    if not args.in_place:
        dest = (
            Path(args.copy_to)
            if args.copy_to
            else target.parent / f"{target.name}-dedup"
        )
        if not (dest.exists() and any(dest.iterdir())):
            shutil.copytree(target, dest, dirs_exist_ok=True)
            print(f"deduplicating a copy at {dest}", file=sys.stderr)
        target = dest
    project = map_project(target, args.lang)
    if project.lang != "rust":
        raise SystemExit(f"lc dedup targets rust projects; got {project.lang}")
    if not run_tests(
        target,
        "rust",
        timeout=args.timeout,
        command=shlex.split(args.test_command) if args.test_command else None,
    ).ok:
        raise SystemExit("baseline tests failed; refusing to deduplicate")
    pristine = {p: p.read_text(encoding="utf-8") for p in project.source_files}
    backend = TxBackend(args.ml_cli, timeout=args.ml_timeout)
    gate = default_tx_gate(
        target,
        pristine,
        args.timeout,
        shlex.split(args.test_command) if args.test_command else None,
    )
    stats, records = dedup_tree(target, project.source_files, backend, gate, attempts=2)
    tests_ok = run_tests(
        target,
        "rust",
        timeout=args.timeout,
        command=shlex.split(args.test_command) if args.test_command else None,
    ).ok
    payload = {
        "dedup": {
            "root": str(target),
            "stats": stats,
            "records": records,
            "tests_ok": tests_ok,
        }
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"stats": stats, "tests_ok": tests_ok},
            indent=2,
        )
    )
    print(f"report: {out}")
    return 0 if tests_ok else 1


def cmd_compact_tests(args: argparse.Namespace) -> int:
    from .langdetect import map_project
    from .ml_file import FileBackend
    from .test_compact import (
        apply_proposal,
        load_proposals,
        propose_compaction,
        write_proposals,
    )

    target = Path(args.path)
    if not args.in_place and not args.apply:
        dest = (
            Path(args.copy_to)
            if args.copy_to
            else target.parent / f"{target.name}-compact"
        )
        if not (dest.exists() and any(dest.iterdir())):
            shutil.copytree(target, dest, dirs_exist_ok=True)
            print(f"working on a copy at {dest}", file=sys.stderr)
        target = dest
    if args.apply:
        ids = [part.strip() for part in args.apply.split(",") if part.strip()]
        proposals = load_proposals(target)
        results = [
            apply_proposal(target, p, timeout=args.timeout)
            for p in proposals
            if p["id"] in ids
        ]
        print(json.dumps(results, indent=2))
        total = sum(r["loc_delta"] for r in results if r["applied"])
        print(
            f"applied {sum(r['applied'] for r in results)} proposal(s);"
            f" test_compaction_loc: -{total} (separate stat, never in the"
            " semantic figure)"
        )
        return 0
    project = map_project(target, args.lang)
    if project.lang != "rust":
        raise SystemExit("lc compact-tests targets rust projects")
    backend = FileBackend(args.ml_cli, timeout=args.ml_timeout)
    backend.name = "compact-cli"
    proposals = propose_compaction(
        target,
        project.source_files,
        backend,
        attempts=2,
        timeout=args.timeout,
    )
    out = write_proposals(target, proposals)
    print(
        json.dumps(
            [{k: v for k, v in p.items() if k != "candidate"} for p in proposals],
            indent=2,
        )
    )
    print(f"proposals: {out}")
    print("nothing is applied until you pass --apply with explicit ids")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Render the JSON report as a small markdown summary (no separate bench)."""
    data = json.loads(Path(args.json).read_text())
    r = data["reduce"]
    lines = [
        "# less-code report",
        "",
        f"- language: `{r['lang']}`",
        (
            f"- LOC: {r['loc_start']} -> {r['loc_after_static']} (post-static) -> "
            f"{r['loc_final']} (final)"
        ),
        f"- reduction: **{r['hybrid_pct']}%**",
        (
            f"- tests green: {r['tests_ok']}  |  API preserved: {r['api_ok']} "
            f"(baseline: {r.get('api_baseline', 'original')})"
        ),
        f"- documentation preserved: {r.get('docs_ok', False)}",
        f"- LOC counted after canonical formatting: {r.get('formatted_loc', False)}",
        f"- gate strength: {tests_run_text(r.get('gate_tests_run'))}",
    ]
    if r.get("layer_records"):
        lines.append("\n## Per-layer yield\n")
        for layer in r["layer_records"]:
            note = f" ({layer.get('notes', [''])[0]})" if layer.get("notes") else ""
            committed = "kept" if layer.get("committed", True) else "reverted"
            lines.append(
                f"- **{layer['layer']}** {layer['loc_before']} -> {layer['loc_after']} "
                f"[{committed}]{note}"
            )
    Path(args.out).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def cmd_corpus(args: argparse.Namespace) -> int:
    from .corpus import run_corpus, write_corpus_report
    from .ml_file import FileBackend
    from .ml_shrink import CliBackend

    backend = CliBackend(args.ml_cli, args.ml_timeout) if args.ml_cli else None
    file_backend = (
        FileBackend(args.ml_file_cli, args.ml_file_timeout)
        if args.ml_file_cli
        else None
    )
    dedup_backend = None
    if getattr(args, "dedup_cli", None):
        from .dedup_tx import TxBackend

        dedup_backend = TxBackend(args.dedup_cli, args.ml_file_timeout)
    result = run_corpus(
        Path(args.manifest),
        args.timeout,
        ml_backend=backend,
        ml_attempts=args.ml_attempts,
        ml_symbols=args.ml_symbols,
        only=getattr(args, "only", None),
        ml_file_backend=file_backend,
        dedup_backend=dedup_backend,
    )
    write_corpus_report(result, Path(args.out), Path(args.markdown))
    print(json.dumps(result["aggregate"], indent=2))
    return 0 if result["aggregate"]["valid"] == result["aggregate"]["projects"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lc",
        description="shrink a Python / JavaScript / Rust codebase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"lc {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="map project + LOC baseline")
    p.add_argument("path")
    p.add_argument("--lang")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("shrink", help="shrink the codebase")
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument(
        "--copy-to",
        default=None,
        metavar="DIR",
        help="copy the project to DIR and shrink the COPY, leaving path untouched",
    )
    p.add_argument(
        "--test-command",
        default=None,
        metavar="CMD",
        help=(
            "override the frozen-suite command, shell-parsed (e.g. "
            "'.venv/bin/python -m pytest -x -q' for a project with its own "
            "venv, or 'npm --prefix js test --silent' for a JS package in a "
            "monorepo). Default: the language's usual command run from path."
        ),
    )
    p.add_argument("--out", default="shrink-report.json")
    p.add_argument(
        "--ml-cli",
        default=None,
        metavar="CMD",
        help=("ML backend command; receives a prompt and returns one JSON object"),
    )
    p.add_argument(
        "--ml-timeout",
        type=float,
        default=60.0,
        help="per-call timeout for the ML backend (seconds)",
    )
    p.set_defaults(func=cmd_shrink)
    p.add_argument(
        "--ml-file-cli",
        default=None,
        metavar="CMD",
        help=(
            "whole-file ML layer for Rust, run after all deterministic layers;"
            " the command receives {system, path, source, context, feedback} and"
            " returns {path, content}"
        ),
    )
    p.add_argument(
        "--ml-file-timeout",
        type=float,
        default=240.0,
        help="per-call timeout for the whole-file ML backend (seconds)",
    )
    p.add_argument(
        "--ml-symbols",
        type=int,
        choices=range(1, 65),
        default=64,
        metavar="1..64",
        help="maximum symbols sent to the model",
    )
    p.add_argument(
        "--ml-attempts",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="maximum proposals per symbol (default: 3)",
    )

    p = sub.add_parser(
        "rewrite",
        help="whole-file model rewrites for Rust (L1b layer), fully gate-verified",
    )
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument(
        "--copy-to",
        default=None,
        metavar="DIR",
        help="work on a copy at DIR instead of the sibling <path>-rewrite",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="operate on path directly (originals are otherwise sacred)",
    )
    p.add_argument(
        "--ml-cli",
        required=True,
        metavar="CMD",
        help="backend command; receives {system, path, source, context, feedback}"
        " on stdin and returns one JSON object {path, content}",
    )
    p.add_argument("--ml-timeout", type=float, default=240.0)
    p.add_argument(
        "--attempts",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="maximum proposals per file (default: 3)",
    )
    p.add_argument(
        "--test-command",
        default=None,
        metavar="CMD",
        help="frozen-suite command for verification (shell-parsed)",
    )
    p.add_argument("--out", default="rewrite-report.json")
    p.set_defaults(func=cmd_rewrite)

    p = sub.add_parser(
        "dedup",
        help="cross-function dedup transactions for Rust, all-or-nothing gated",
    )
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument(
        "--copy-to",
        default=None,
        metavar="DIR",
        help="work on a copy at DIR instead of the sibling <path>-dedup",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="operate on path directly (originals are otherwise sacred)",
    )
    p.add_argument(
        "--ml-cli",
        required=True,
        metavar="CMD",
        help="backend command; receives {system, files, clones, feedback} on"
        " stdin and returns one transaction JSON object",
    )
    p.add_argument("--ml-timeout", type=float, default=240.0)
    p.add_argument(
        "--test-command",
        default=None,
        metavar="CMD",
        help="frozen-suite command for verification (shell-parsed)",
    )
    p.add_argument("--out", default="dedup-report.json")
    p.set_defaults(func=cmd_dedup)

    p = sub.add_parser(
        "compact-tests",
        help="mutation-certified test compaction proposals (Phase 8; consent flow)",
    )
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument(
        "--copy-to",
        default=None,
        metavar="DIR",
        help="work on a copy at DIR instead of the sibling <path>-compact",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="operate on path directly",
    )
    p.add_argument(
        "--ml-cli",
        default=None,
        metavar="CMD",
        help="backend command for test rewrites (proposal mining)",
    )
    p.add_argument("--ml-timeout", type=float, default=240.0)
    p.add_argument(
        "--apply",
        default=None,
        metavar="IDS",
        help='apply consented proposals by id, e.g. "src/lib.rs:1"',
    )
    p.set_defaults(func=cmd_compact_tests)

    p = sub.add_parser("report", help="markdown summary from shrink JSON")
    p.add_argument("--json", default="shrink-report.json")
    p.add_argument("--out", default="SHRINK.md")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("corpus", help="run the pinned multi-project benchmark")
    p.add_argument("--manifest", default="bench/corpus.toml")
    p.add_argument(
        "--only", metavar="NAME", help="run a single project row for re-measurement"
    )
    p.add_argument("--out", default="bench/baseline.json")
    p.add_argument("--markdown", default="bench/BASELINE.md")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument(
        "--ml-cli",
        metavar="CMD",
        help="benchmark a model command after static reduction",
    )
    p.add_argument("--ml-timeout", type=float, default=60.0)
    p.add_argument("--ml-attempts", type=int, choices=(1, 2, 3), default=3)
    p.add_argument(
        "--ml-file-cli",
        metavar="CMD",
        help="benchmark the whole-file Rust layer after static reduction",
    )
    p.add_argument("--ml-file-timeout", type=float, default=240.0)
    p.add_argument(
        "--dedup-cli",
        metavar="CMD",
        help="run the cross-function dedup-tx arm after the file layer",
    )
    p.add_argument(
        "--ml-symbols", type=int, choices=range(1, 65), default=64, metavar="1..64"
    )
    p.set_defaults(func=cmd_corpus)

    p = sub.add_parser(
        "propose",
        help="mine consented deletion proposals; apply only with explicit group ids",
    )
    p.add_argument("path")
    p.add_argument("--lang")
    p.add_argument(
        "--app",
        action="store_true",
        help="allow non-private symbols in repos without library packaging",
    )
    p.add_argument(
        "--apply",
        default=None,
        metavar="IDS",
        help='apply consented groups by id, e.g. "1,3,7", or "all"',
    )
    p.add_argument(
        "--copy-to",
        default=None,
        metavar="DIR",
        help="work on a copy at DIR instead of the sibling <path>-proposals",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="operate on path directly (originals are otherwise sacred)",
    )
    p.add_argument("--out", default=None, metavar="FILE")
    p.add_argument(
        "--test-command",
        default=None,
        metavar="CMD",
        help="frozen-suite command for verification (shell-parsed)",
    )
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument(
        "--coverage",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the suite under coverage to rank candidates (needs the"
        " target interpreter to import coverage; degrades silently)",
    )
    p.set_defaults(func=cmd_propose)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
