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
from .llm_reduce import reduce_file
from .loc import count_source
from .static import static_pass
from .testrunners import run_tests


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

    def to_json(self) -> dict:
        d = {
            "lang": self.lang,
            "files_considered": self.files_considered,
            "loc_start": self.loc_start,
            "loc_after_static": self.loc_after_static,
            "loc_final": self.loc_final,
            "static_only_loc_final": self.loc_after_static,
            "static_pct": pct(self.loc_start, self.loc_after_static),
            "hybrid_pct": pct(self.loc_start, self.loc_final),
            "llm_extra_pct": pct(self.loc_after_static, self.loc_final),
            "tests_ok": self.tests_ok,
            "api_ok": self.api_ok,
            "static_notes": self.static_notes,
            "attempts": self.attempt_records,
        }
        return d


def pct(before: int, after: int) -> float:
    return round(100.0 * (before - after) / before, 2) if before else 0.0


def tree_loc(files: list[Path], lang: str) -> int:
    return sum(count_source(f.read_text(encoding="utf-8", errors="replace"), lang).code for f in files)


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
) -> ReduceStats:
    project = map_project(root, lang)
    stats = ReduceStats(lang=project.lang, files_considered=len(project.source_files))
    all_files = project.source_files + project.test_files

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
        try:
            old_handlers[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass

    if formatter:
        run_formatter(root, project.lang)

    stats.loc_start = tree_loc(project.source_files, project.lang)

    # ---- L1 static ----
    static = static_pass(root, project.lang, project.source_files, all_files)
    stats.static_notes = static.notes
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

    # ---- L2 LLM ----
    if backend is not None and backend.name != "none":
        from .llm_reduce import focused_passes, reduce_file, reduce_file_chunked

        spec = "\n\n".join(
            f.read_text(encoding="utf-8", errors="replace")[:40000]
            for f in project.test_files
        )[:40000]
        runner = lambda r, l: run_tests(r, l, timeout=test_timeout)  # noqa: E731
        ordered = sorted(project.source_files, key=lambda f: -f.stat().st_size)
        if max_files:
            ordered = ordered[:max_files]
        for path in ordered:
            source_before = path.read_text(encoding="utf-8", errors="replace")
            loc_before = count_source(source_before, project.lang).code
            if loc_before < 8:
                continue
            if project.lang == "python":
                best, best_loc, records = reduce_file(
                    backend, root, path, project.lang, runner,
                    attempts=max(1, attempts_per_file - 1), spec=spec,
                )
                if best_loc < loc_before:
                    path.write_text(best + "\n", encoding="utf-8")
                records += focused_passes(backend, root, path, project.lang, runner, spec=spec)
                now = count_source(
                    path.read_text(encoding="utf-8", errors="replace"), project.lang
                ).code
                if now > 1000:  # very large file: per-symbol fallback
                    records += reduce_file_chunked(backend, root, path, runner, spec=spec)
            else:
                best, best_loc, records = reduce_file(
                    backend, root, path, project.lang, runner,
                    attempts=attempts_per_file, spec=spec,
                )
                if best_loc < loc_before:
                    path.write_text(best + "\n", encoding="utf-8")
            for rec in records:
                print(
                    f"  [L2] {rec.file.split('/')[-1]} attempt={rec.attempt} "
                    f"{rec.outcome} {rec.loc_before}->{rec.loc_after}",
                    flush=True,
                )
                stats.attempt_records.append(
                    {
                        "file": rec.file, "attempt": rec.attempt, "outcome": rec.outcome,
                        "loc_before": rec.loc_before, "loc_after": rec.loc_after, "detail": rec.detail[:200],
                    }
                )
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
