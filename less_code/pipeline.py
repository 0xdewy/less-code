"""Orchestrates L1 static + L2 verify-gated LLM reduction. Accepts only
states where the full suite stays green; every candidate is measured against
code-LOC. Attribution: static-only vs LLM delta for the hybrid claim."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .api_check import api_surface, api_violations
from .langdetect import map_project
from .loc import formatter_available, measure
from .static import StaticResult, static_pass
from .testrunners import run_tests, shadowed_imports
import contextlib


@dataclass
class ReduceStats:
    lang: str
    files_considered: int = 0
    loc_start: int = 0
    loc_after_static: int = 0
    loc_final: int = 0
    static_notes: list[str] = field(default_factory=list)
    attempt_records: list[dict] = field(default_factory=list)
    tests_ok: bool = False
    api_ok: bool = False
    reduction_pct: float = 0.0
    #: Which surface `api_ok` is measured against. The static layer removes
    #: provably-dead public items by design, so the baseline the LLM layer is
    #: held to is the POST-static surface, not the original one.
    api_baseline: str = "post-static"
    #: Public symbols the static layer deleted (dead code). These are gone
    #: from the delivered tree relative to the ORIGINAL sources, so
    #: `api_ok: true` must never be read as "identical to the input API".
    static_removed_symbols: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "lang": self.lang,
            "files_considered": self.files_considered,
            "loc_start": self.loc_start,
            "loc_after_static": self.loc_after_static,
            "loc_final": self.loc_final,
            "static_only_loc_final": self.loc_after_static,
            "static_pct": pct(self.loc_start, self.loc_after_static),
            "hybrid_pct": pct(self.loc_start, self.loc_final),
            "llm_extra_pct": pct(self.loc_after_static, self.loc_final),
            "formatted_loc": formatter_available(self.lang),
            "tests_ok": self.tests_ok,
            "api_ok": self.api_ok,
            "api_baseline": self.api_baseline,
            "static_removed_symbols": self.static_removed_symbols,
            "static_notes": self.static_notes,
            "attempts": self.attempt_records,
        }


def pct(before: int, after: int) -> float:
    return round(100.0 * (before - after) / before, 2) if before else 0.0


def tree_loc(files: list[Path], lang: str) -> int:
    return sum(measure(f.read_text(encoding="utf-8", errors="replace"), lang).code for f in files)


def run_formatter(root: Path, lang: str) -> None:
    """L0 normalize (not counted as reduction). Best effort, optional tools."""
    if lang == "python" and shutil.which("ruff"):
        subprocess.run(["ruff", "format", "."], cwd=root, capture_output=True, timeout=300)
    elif lang in ("javascript", "typescript") and shutil.which("dprint"):
        subprocess.run(["dprint", "fmt"], cwd=root, capture_output=True, timeout=300)
    elif lang == "rust" and shutil.which("rustfmt"):
        subprocess.run(["cargo", "fmt"], cwd=root, capture_output=True, timeout=300)


def reduce_project(
    root: Path,
    lang: str | None = None,
    backend=None,
    attempts_per_file: int = 3,
    max_files: int | None = None,
    formatter: bool = True,
    test_timeout: int = 600,
    whole_file_sweep: bool = True,
    dedup_groups: int = 3,
    strategy: str = "mixed",
    skip_files: set[str] | None = None,
    trust: dict[str, float] | None = None,
    symbols_per_sweep: int = 3,
    run_static: bool = True,
) -> ReduceStats:
    project = map_project(root, lang)
    stats = ReduceStats(lang=project.lang, files_considered=len(project.source_files))
    all_files = project.source_files + project.test_files

    # the gate must test THIS tree: a package that resolves elsewhere (host
    # venv shadowing an editable install) would green-light reductions that
    # never ran. Refuse before anything is measured or touched.
    if project.lang == "python":
        shadowed = shadowed_imports(root)
        if shadowed:
            stats.tests_ok = False
            stats.static_notes = [
                "import-origin check failed: " + "; ".join(shadowed)
                + " — the gate would test code from outside this tree. Fix the"
                " host venv (e.g. reinstall the editable for this tree, remove"
                " the shadowing package) and re-run."
            ]
            return stats

    baseline = run_tests(root, project.lang, timeout=test_timeout)
    if not baseline.ok:
        stats.tests_ok = False
        stats.static_notes = [f"baseline tests failed: {baseline.output_tail[-300:]}"]
        return stats
    stats.tests_ok = True

    # crash safety: never leave the tree mutated if we are killed mid-verify
    snapshots = {p: p.read_text(encoding="utf-8", errors="replace") for p in project.source_files}

    def _restore(*_args) -> None:
        for p, text in snapshots.items():
            p.write_text(text, encoding="utf-8")

    import signal

    old_handlers: dict[int, object] = {}

    def _handler(signum, frame):  # pragma: no cover
        _restore()
        old = old_handlers.get(signum)
        if callable(old):
            old(signum, frame)
        raise SystemExit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            old_handlers[sig] = signal.signal(sig, _handler)

    if formatter:
        run_formatter(root, project.lang)

    stats.loc_start = tree_loc(project.source_files, project.lang)

    # ---- L1 static ----
    if not run_static:
        # mining mode: the LLM layer sees PRISTINE code, where the verbose
        # patterns still exist — pairs mined on a post-static floor are all
        # rejections (measured: 25 calls on the reduced py fixture, 0 accepted)
        stats.static_notes = ["static pass skipped (--no-static)"]
        static = StaticResult()
    else:
        static = static_pass(
            root, project.lang, project.source_files, all_files,
            runner=lambda r, l: run_tests(r, l, timeout=test_timeout, use_cache=False),
        )
        stats.static_notes = static.notes
    api_pre_static = api_surface(project.source_files, project.lang)
    originals = {p: p.read_text(encoding="utf-8", errors="replace") for p in project.source_files}
    for path_str, new_source in static.changed_files.items():
        Path(path_str).write_text(new_source, encoding="utf-8")
    check = run_tests(root, project.lang, timeout=test_timeout)
    if not check.ok:
        for p, original in originals.items():
            p.write_text(original, encoding="utf-8")
        stats.static_notes.append("static pass broke tests; reverted all static edits")
    stats.loc_after_static = tree_loc(project.source_files, project.lang)
    # static-layer removals are intentional (dead code); the LLM layer must
    # preserve the post-static surface exactly.
    api_ref = api_surface(project.source_files, project.lang)
    stats.static_removed_symbols = sorted(
        name
        for file, api in api_pre_static.items()
        for name in set(api) - set(api_ref.get(file, {}))
    )

    # ---- L2 ----
    # behavior-spec material for per-unit selection (select_spec): a
    # repo-scale suite (click: ~50 files) cannot go into one prompt, and a
    # 40k-char global prefix would feed every prompt tests unrelated to the
    # unit being rewritten. Only needed when a model will be asked.
    spec_tests: list[tuple[str, str]] = []
    if backend is not None and backend.name != "none":
        spec_tests = [
            (
                str(f.relative_to(root)),
                f.read_text(encoding="utf-8", errors="replace"),
            )
            for f in project.test_files
        ]

    # C2b FIRST, and deliberately: it is project-wide, it targets the biggest
    # opportunity both FIXTURE.md files document (cross-symbol copy-paste),
    # and the greedy per-symbol loop once spent the whole budget before any
    # cross-symbol pass ran. The mechanical merge is deterministic and
    # suite-gated, so it runs even with no LLM at all (static-only trees get
    # dedup yield too); the model is asked only for what it cannot prove.
    from .llm_reduce import reduce_duplicate_groups

    dedup_runner = lambda r, l: run_tests(r, l, timeout=test_timeout)  # noqa: E731
    dedup_records = [] if strategy == "whole-file" else reduce_duplicate_groups(
        backend, root, project.source_files, project.lang, dedup_runner,
        attempts_per_group=max(1, attempts_per_file - 1), max_groups=dedup_groups,
        spec_tests=spec_tests or None,
    )
    for rec in dedup_records:
        stats.attempt_records.append(
            {"file": rec.file, "attempt": rec.attempt, "outcome": rec.outcome,
             "loc_before": rec.loc_before, "loc_after": rec.loc_after,
             "detail": rec.detail[:200]}
        )

    if backend is not None and backend.name != "none":
        from . import llm_reduce as _llm
        from .llm_reduce import reduce_file, reduce_symbols

        # live per-attempt output: a long GPU run must be readable while it
        # runs, not only once each file is finished.
        _llm.PROGRESS = lambda rec: print(
            f"  [L2] {rec.file.split('/')[-1]} attempt={rec.attempt} "
            f"{rec.outcome} {rec.loc_before}->{rec.loc_after}",
            flush=True,
        )

        runner = lambda r, l: run_tests(r, l, timeout=test_timeout)  # noqa: E731
        # C7 scheduling, with the click lesson baked in: biggest-file-first
        # spent 100% of a 40-call budget inside core.py and 13 of 17 files
        # never saw an LLM call. Rank by expected yield (size x per-file
        # mutation score when a trust map is given) and ROUND-ROBIN the
        # budget across files in bounded sweeps instead.
        def _yield_key(f: Path) -> float:
            loc = measure(
                f.read_text(encoding="utf-8", errors="replace"), project.lang
            ).code
            t = (trust or {}).get(f.name, 0.7)
            return loc * t

        ordered = sorted(project.source_files, key=lambda f: -_yield_key(f))
        if skip_files:
            # trust-scaled aggressiveness: files whose behavior the suite
            # cannot see (e.g. platform-dead code on this OS, measured by
            # `lc audit`) stay out of the LLM's reach entirely
            ordered = [f for f in ordered if f.name not in skip_files]
        if max_files:
            ordered = ordered[:max_files]

        def _budget_gone(records) -> bool:
            return any(
                (r["outcome"] if isinstance(r, dict) else r.outcome) == "budget-exhausted"
                for r in records
            )

        if strategy == "whole-file":
            for path in ordered:
                best, best_loc, sweep_records = reduce_file(
                    backend, root, path, project.lang, runner,
                    attempts=attempts_per_file, spec_tests=spec_tests,
                )
                for rec in sweep_records:
                    stats.attempt_records.append(
                        {"file": rec.file, "attempt": rec.attempt, "outcome": rec.outcome,
                         "loc_before": rec.loc_before, "loc_after": rec.loc_after,
                         "detail": rec.detail[:200]}
                    )
                current_loc = measure(
                    path.read_text(encoding="utf-8", errors="replace"), project.lang
                ).code
                if best_loc < current_loc:
                    path.write_text(best + "\n", encoding="utf-8")
                if _budget_gone(sweep_records):
                    break
        else:
            # round-robin: a few symbols per file per sweep, so one big file
            # can no longer monopolize the budget (click: core.py ate all 40
            # calls; types.py/parser.py never got one). The fixpoint is an
            # ACCEPTANCE-free sweep: a rejected symbol gets its retry inside
            # the call (attempts_per_symbol), so re-asking it next sweep with
            # the same prompt is pure waste.
            done: set[Path] = set()
            attempted: dict[Path, set[str]] = {}
            budget_gone = False
            while not budget_gone:
                accepted_any = False
                for path in ordered:
                    if path in done:
                        continue
                    if measure(
                        path.read_text(encoding="utf-8", errors="replace"), project.lang
                    ).code < 8:
                        done.add(path)
                        continue
                    sweep = reduce_symbols(
                        backend, root, path, project.lang, runner,
                        attempts_per_symbol=max(1, attempts_per_file - 1),
                        spec_tests=spec_tests, max_symbols=symbols_per_sweep,
                        exclude=attempted.get(path),
                    )
                    # records carry `path:key`; keep the per-file attempted set
                    # growing so the next sweep proposes NEW symbols, not re-rolls
                    keys = attempted.setdefault(path, set())
                    for rec in sweep:
                        stats.attempt_records.append(
                            {"file": rec.file, "attempt": rec.attempt, "outcome": rec.outcome,
                             "loc_before": rec.loc_before, "loc_after": rec.loc_after,
                             "detail": rec.detail[:200]}
                        )
                        if ":" in rec.file:
                            keys.add(rec.file.rsplit(":", 1)[1])
                    if any(r.outcome == "accepted" for r in sweep):
                        accepted_any = True
                    if not sweep:
                        done.add(path)
                    if _budget_gone(sweep):
                        budget_gone = True
                        break
                if not accepted_any:
                    break
            # whole-file sweep LAST: only a whole-file rewrite can dedup
            # across symbols, and its rejects still feed hunk salvage
            if whole_file_sweep and not budget_gone:
                for path in ordered:
                    current = path.read_text(encoding="utf-8", errors="replace")
                    loc_now = measure(current, project.lang).code
                    best, best_loc, sweep_records = reduce_file(
                        backend, root, path, project.lang, runner,
                        attempts=1, spec_tests=spec_tests,
                    )
                    for rec in sweep_records:
                        stats.attempt_records.append(
                            {"file": rec.file, "attempt": rec.attempt, "outcome": rec.outcome,
                             "loc_before": rec.loc_before, "loc_after": rec.loc_after,
                             "detail": rec.detail[:200]}
                        )
                    if best_loc < loc_now:
                        path.write_text(best + "\n", encoding="utf-8")
                    if _budget_gone(sweep_records):
                        break
        _llm.PROGRESS = None
        final = run_tests(root, project.lang, timeout=test_timeout)
        stats.tests_ok = final.ok

    stats.loc_final = tree_loc(project.source_files, project.lang)
    api_after = api_surface(project.source_files, project.lang)
    violations = api_violations(api_ref, api_after)
    stats.api_ok = not violations
    if violations:
        stats.static_notes.append(f"API violations: {violations[:3]}")
    return stats


def write_report(stats: ReduceStats, out: Path, audit_result=None) -> Path:
    payload = {"reduce": stats.to_json()}
    if audit_result is not None:
        payload["audit"] = {
            "total": audit_result.total,
            "killed": audit_result.killed,
            "score": round(audit_result.score, 4),
        }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out
