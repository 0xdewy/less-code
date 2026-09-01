"""A2 — `lc bench`: the same reduction over every fixture, one row per run.

Every fixture is copied to a scratch tree first, so a bench run never mutates
the repo. After the reduction the *hidden* suite runs against the reduced copy
(the frozen gate never saw it, so `hidden_ok=False` means a real behaviour
regression slipped through the oracle). Rows are appended to
`bench/results/<timestamp>.jsonl`, keyed by (fixture, config, git commit), so
every later change carries a bench delta.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
from pathlib import Path

from .langdetect import SKIP_DIRS, map_project
from .pipeline import reduce_project
from .testrunners import cache_clear, run_hidden_tests

# the hidden suite must travel with the fixture even though the mapper skips it
COPY_IGNORE = shutil.ignore_patterns(*(SKIP_DIRS - {"tests_hidden"}), "*.pyc")


@dataclass
class BenchRow:
    fixture: str
    lang: str
    config: str
    commit: str
    timestamp: str
    loc_start: int = 0
    loc_after_static: int = 0
    loc_final: int = 0
    static_pct: float = 0.0
    hybrid_pct: float = 0.0
    tests_ok: bool = False
    api_ok: bool = False
    hidden_ok: bool | None = None
    llm_calls: int = 0
    seconds: float = 0.0
    formatted_loc: bool = False
    # "canonical" when the language's formatter ran, "raw" when it was missing.
    # Iteration 07's one promising js result was invalidated because
    # `formatted_loc: false` was a quiet field nobody read: raw physical lines
    # are exactly the metric roadmap B1 exists to abolish, because line-joining
    # games them. A raw row is not comparable with a canonical one.
    metric: str = "canonical"
    # per-attempt outcome tally (accepted / tests-failed / api-changed /
    # syntax-error / backend-error / hunks-*). Without this a finished row is
    # not diagnosable after the fact: the scratch tree is deleted and the
    # attempt records live only in ReduceStats.
    attempt_outcomes: dict[str, int] = field(default_factory=dict)
    # `api_ok` is measured against the POST-static surface: L1 removes
    # provably-dead public items by design. Without these two fields a green
    # `api_ok` reads as "identical API", which for the rs fixture is false by
    # three `pub fn`s (C7 review D4).
    api_baseline: str = "post-static"
    static_removed_symbols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def git_commit(repo: Path) -> str:
    """Provenance stamp for a bench row.

    A row records the code that produced it, so a run made from a tree with
    uncommitted changes gets a `-dirty` suffix (D5): the bare sha would claim a
    reproducibility that the recorded commit cannot deliver.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo,
            capture_output=True, text=True, timeout=30,
        )
        sha = proc.stdout.strip()
        if not sha:
            return "unknown"
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo,
            capture_output=True, text=True, timeout=30,
        )
        if status.returncode == 0 and status.stdout.strip():
            return f"{sha}-dirty"
        return sha
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def bench_fixture(
    fixture: Path,
    config: str,
    commit: str,
    backend_spec: str = "none",
    model: str | None = None,
    attempts: int = 2,
    max_llm_calls: int = 6,
    timeout: int = 600,
    num_ctx: int = 16384,
    llm_timeout: int = 600,
    strategy: str = "mixed",
    keep_tree: Path | None = None,
) -> BenchRow:
    """Reduce one fixture in a scratch copy and score it.

    `keep_tree` copies the reduced scratch tree to `keep_tree/<fixture>` before
    it is deleted. Without it a bench row is the tool's own self-report about a
    tree nobody can look at — which is exactly why C4 (js) had no artifact and
    was the one criterion the C7 reviewer could establish nothing about (D2).
    """
    from .loc import formatter_available

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    row = BenchRow(fixture=fixture.name, lang="?", config=config, commit=commit, timestamp=stamp)
    backend = None
    if config != "static-only":
        from .backends import BudgetBackend, make_backend

        backend = BudgetBackend(
            make_backend(backend_spec, model, num_ctx=num_ctx, timeout=llm_timeout),
            max_llm_calls,
        )

    tmp = Path(tempfile.mkdtemp(prefix="lc-bench-"))
    try:
        scratch = tmp / fixture.name
        shutil.copytree(fixture, scratch, ignore=COPY_IGNORE)
        cache_clear()
        start = time.monotonic()
        stats = reduce_project(
            scratch, backend=backend, attempts_per_file=attempts, test_timeout=timeout,
            strategy=strategy,
        )
        row.seconds = round(time.monotonic() - start, 2)
        payload = stats.to_json()
        row.lang = stats.lang
        row.loc_start = payload["loc_start"]
        row.loc_after_static = payload["loc_after_static"]
        row.loc_final = payload["loc_final"]
        row.static_pct = payload["static_pct"]
        row.hybrid_pct = payload["hybrid_pct"]
        row.tests_ok = payload["tests_ok"]
        row.api_ok = payload["api_ok"]
        row.formatted_loc = formatter_available(stats.lang)
        row.metric = "canonical" if row.formatted_loc else "raw"
        if not row.formatted_loc:
            row.notes.append(
                f"UNSCOREABLE METRIC: no formatter for {stats.lang}; LOC is raw "
                f"physical lines, not comparable with canonical rows"
            )
            print(
                f"WARNING: {fixture.name} ({stats.lang}) has no formatter installed — "
                f'this row is metric="raw" (physical lines) and is NOT comparable '
                f"with canonical rows. Install the formatter and re-run.",
                file=sys.stderr, flush=True,
            )
        row.llm_calls = getattr(backend, "calls", 0)
        tally: dict[str, int] = {}
        for rec in payload["attempts"]:
            tally[rec["outcome"]] = tally.get(rec["outcome"], 0) + 1
        row.attempt_outcomes = tally
        hidden = run_hidden_tests(scratch, stats.lang, timeout=timeout)
        if hidden is not None:
            row.hidden_ok = hidden.ok
            if not hidden.ok:
                row.notes.append(f"hidden: {hidden.output_tail[-300:]}")
        row.api_baseline = stats.api_baseline
        row.static_removed_symbols = stats.static_removed_symbols
        row.notes += stats.static_notes
        if keep_tree is not None:
            dest = Path(keep_tree) / fixture.name
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(scratch, dest, ignore=COPY_IGNORE)
            row.notes.append(f"reduced tree preserved at {dest}")
            print(f"  [bench] {fixture.name} tree kept at {dest}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return row


def discover(root: Path, only: str | None = None) -> list[Path]:
    """Every directory under `root` that maps to a supported project.

    `only` selects a single fixture by name — a 15-minute GPU run should not
    be the only way to re-measure one language.
    """
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
            continue
        if only and child.name != only:
            continue
        try:
            map_project(child)
        except SystemExit:
            continue
        found.append(child)
    return found


def markdown_table(rows: list[BenchRow]) -> str:
    head = (
        "| fixture | lang | config | LOC start | after static | final | static % | "
        "hybrid % | tests | api | hidden | calls | s | metric |"
    )
    sep = "|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|"

    def flag(value: bool | None) -> str:
        return "-" if value is None else ("ok" if value else "FAIL")

    lines = [head, sep]
    for r in rows:
        lines.append(
            f"| {r.fixture} | {r.lang} | {r.config} | {r.loc_start} | {r.loc_after_static} | "
            f"{r.loc_final} | {r.static_pct} | {r.hybrid_pct} | {flag(r.tests_ok)} | "
            f"{flag(r.api_ok)} | {flag(r.hidden_ok)} | {r.llm_calls} | {r.seconds} | "
            f"{'**RAW**' if r.metric == 'raw' else 'canonical'} |"
        )
    return "\n".join(lines)


def run_bench(
    fixtures_dir: Path,
    out_dir: Path,
    config: str = "static-only",
    repo: Path | None = None,
    fixture: str | None = None,
    **kwargs,
) -> tuple[list[BenchRow], Path]:
    """Bench every fixture under `fixtures_dir`; append one JSONL row each.

    Rows are appended **as each fixture finishes**, not at the end. Iteration
    07 lost a completed multi-hour run's first two fixtures to an interrupted
    third, and then spent a session guessing at a row it could not inspect.
    A checkpointed file is the difference between evidence and a story.
    """
    repo = repo or Path.cwd()
    commit = git_commit(repo)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    rows: list[BenchRow] = []
    for target in discover(fixtures_dir, fixture):
        row = bench_fixture(target, config, commit, **kwargs)
        rows.append(row)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(row)) + "\n")
            fh.flush()
        print(f"  [bench] {row.fixture} written to {out}", flush=True)
    return rows, out
