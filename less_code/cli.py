"""`lc` — the less-code CLI.

lc analyze <path>   map project + code-LOC baseline
lc shrink  <path>   shrink the codebase using existing static tools + rules
                    every layer gated by the frozen test suite
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
    test_command = shlex.split(args.test_command) if args.test_command else None
    stats = shrink_project(
        target,
        lang=args.lang,
        test_timeout=args.timeout,
        ml_backend=ml_backend,
        ml_attempts=args.ml_attempts,
        ml_symbols=args.ml_symbols,
        test_command=test_command,
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
                )
            },
            indent=2,
        )
    )
    print(f"report: {args.out}")
    ok = stats.tests_ok and stats.api_ok and stats.docs_ok
    return 0 if ok else 1


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
    from .ml_shrink import CliBackend

    backend = CliBackend(args.ml_cli, args.ml_timeout) if args.ml_cli else None
    result = run_corpus(
        Path(args.manifest),
        args.timeout,
        ml_backend=backend,
        ml_attempts=args.ml_attempts,
        ml_symbols=args.ml_symbols,
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

    p = sub.add_parser("report", help="markdown summary from shrink JSON")
    p.add_argument("--json", default="shrink-report.json")
    p.add_argument("--out", default="SHRINK.md")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("corpus", help="run the pinned multi-project benchmark")
    p.add_argument("--manifest", default="bench/corpus.toml")
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
        "--ml-symbols", type=int, choices=range(1, 65), default=64, metavar="1..64"
    )
    p.set_defaults(func=cmd_corpus)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
