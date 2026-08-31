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
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
    # per-attempt outcome tally (accepted / tests-failed / api-changed /
    # syntax-error / backend-error / hunks-*). Without this a finished row is
    # not diagnosable after the fact: the scratch tree is deleted and the
    # attempt records live only in ReduceStats.
    attempt_outcomes: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def git_commit(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo,
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout.strip() or "unknown"
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
) -> BenchRow:
    """Reduce one fixture in a scratch copy and score it."""
    from .loc import formatter_available

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
        row.notes += stats.static_notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return row


def discover(root: Path) -> list[Path]:
    """Every directory under `root` that maps to a supported project."""
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
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
        "hybrid % | tests | api | hidden | calls | s |"
    )
    sep = "|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|"

    def flag(value: bool | None) -> str:
        return "-" if value is None else ("ok" if value else "FAIL")

    lines = [head, sep]
    for r in rows:
        lines.append(
            f"| {r.fixture} | {r.lang} | {r.config} | {r.loc_start} | {r.loc_after_static} | "
            f"{r.loc_final} | {r.static_pct} | {r.hybrid_pct} | {flag(r.tests_ok)} | "
            f"{flag(r.api_ok)} | {flag(r.hidden_ok)} | {r.llm_calls} | {r.seconds} |"
        )
    return "\n".join(lines)


def run_bench(
    fixtures_dir: Path,
    out_dir: Path,
    config: str = "static-only",
    repo: Path | None = None,
    **kwargs,
) -> tuple[list[BenchRow], Path]:
    """Bench every fixture under `fixtures_dir`; append one JSONL row each."""
    repo = repo or Path.cwd()
    commit = git_commit(repo)
    rows = [
        bench_fixture(fixture, config, commit, **kwargs)
        for fixture in discover(fixtures_dir)
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    with out.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(asdict(row)) + "\n")
    return rows, out
