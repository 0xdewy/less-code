"""Pinned, reproducible multi-project reduction benchmark."""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from .langdetect import map_project
from .loc import count_tree, measure
from .pipeline import ShrinkStats, _disk_loc, pct, shrink_project
from .rust_rules import test_spans


@dataclass(frozen=True)
class CorpusProject:
    name: str
    repo: str
    commit: str
    lang: str
    test: tuple[str, ...]
    prepare: tuple[str, ...] = ()
    audit: tuple[str, ...] = ()
    cohort: str = "original"


def load_corpus(path: Path) -> list[CorpusProject]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        CorpusProject(
            item["name"],
            item["repo"],
            item["commit"],
            item["lang"],
            tuple(item["test"]),
            tuple(item.get("prepare", ())),
            tuple(
                arg.replace("{manifest_dir}", str(path.parent.resolve()))
                for arg in item.get("audit", ())
            ),
            item.get("cohort", "original"),
        )
        for item in data["project"]
    ]


def rust_inline_test_loc(sources: dict[Path, str]) -> int:
    """Canonical LOC of inline `#[cfg(test)]` spans across source files.

    Every future rust yield claim carries its denominator decomposition:
    test spans stay frozen on the main track, so the reducible mass is
    `non_test_loc`, not `loc_start` (FOLLOWUP.md policy, made automatic)."""
    total = 0
    for text in sources.values():
        encoded = text.encode()
        for start, end in test_spans(text):
            total += measure(encoded[start:end].decode("utf-8"), "rust").code
    return total


def _run(
    command: tuple[str, ...], cwd: Path, timeout: int
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def run_corpus(
    manifest: Path,
    timeout: int = 900,
    ml_backend=None,
    ml_attempts: int = 3,
    ml_symbols: int = 64,
    only: str | None = None,
    ml_file_backend=None,
    ml_file_attempts: int = 3,
    dedup_backend=None,
) -> dict:
    if not 1 <= ml_attempts <= 3:
        raise ValueError("ml_attempts must be between 1 and 3")
    if not 1 <= ml_symbols <= 64:
        raise ValueError("ml_symbols must be between 1 and 64")
    rows = []
    for item in load_corpus(manifest):
        if only and item.name != only:
            continue
        with tempfile.TemporaryDirectory(prefix="less-code-corpus-") as scratch:
            print(
                f"[{item.name}] preparing pinned checkout", file=sys.stderr, flush=True
            )
            started = time.monotonic()
            root = Path(scratch) / item.name
            clone = _run(
                (
                    "git",
                    "clone",
                    "--quiet",
                    "--filter=blob:none",
                    "--no-checkout",
                    item.repo,
                    str(root),
                ),
                Path(scratch),
                timeout,
            )
            if clone.returncode == 0:
                clone = _run(("git", "checkout", "--quiet", item.commit), root, timeout)
            prepare = (
                _run(item.prepare, root, timeout)
                if clone.returncode == 0 and item.prepare
                else clone
            )
            if clone.returncode or prepare.returncode:
                rows.append(
                    {
                        "name": item.name,
                        "cohort": item.cohort,
                        "test_command": list(item.test),
                        "audit_command": list(item.audit),
                        "repo": item.repo,
                        "commit": item.commit,
                        "lang": item.lang,
                        "valid": False,
                        "loc_start": 0,
                        "loc_after_static": 0,
                        "loc_final": 0,
                        "test_loc": 0,
                        "test_share_pct": 0.0,
                        "non_test_loc": 0,
                        "baseline_measured": False,
                        "duration_s": round(time.monotonic() - started, 3),
                        "error": (clone.stderr or prepare.stderr)[-500:],
                    }
                )
                print(
                    f"[{item.name}] setup failed: {rows[-1]['error']}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            project = map_project(root, item.lang)
            original_sources = {
                path: path.read_text(encoding="utf-8") for path in project.source_files
            }
            inline_test_loc = (
                rust_inline_test_loc(original_sources) if item.lang == "rust" else 0
            )
            baseline_loc = count_tree(
                project.source_files, item.lang, format_first=True
            )
            audit_baseline = _run(item.audit, root, timeout) if item.audit else None
            if audit_baseline is not None and audit_baseline.returncode:
                stats = ShrinkStats(item.lang, static_notes=["baseline audit failed"])
            else:
                stats = shrink_project(
                    root,
                    item.lang,
                    test_timeout=timeout,
                    test_command=list(item.test) or None,
                    ml_backend=ml_backend,
                    ml_attempts=ml_attempts,
                    ml_symbols=ml_symbols,
                    ml_file_backend=ml_file_backend,
                    ml_file_attempts=ml_file_attempts,
                    final_validator=(
                        lambda item=item, root=root: (
                            _run(item.audit, root, timeout).returncode == 0
                        )
                    )
                    if item.audit
                    else None,
                )
            # Audit output is deliberately never sent to search or retry prompts.
            audit_final = (
                _run(item.audit, root, timeout)
                if item.audit and audit_baseline.returncode == 0
                else None
            )
            if stats.root is not None:
                disk, _ = _disk_loc(root, item.lang)
                if disk != stats.loc_final:
                    raise RuntimeError(
                        f"{item.name}: report-time disk truth ({disk} code LOC)"
                        f" does not match the measured loc_final"
                        f" ({stats.loc_final}); the tree changed after the"
                        " shrink - re-run"
                    )
            # the dedup-tx arm runs AFTER the file layer; its delta is a
            # separate stat column, never blended into the others (PLAN.md §7)
            dedup_stats, dedup_records, dedup_loc = {}, [], 0
            if (
                dedup_backend is not None
                and item.lang == "rust"
                and stats.tests_ok
                and stats.root is not None
            ):
                from .dedup_tx import dedup_tree, default_tx_gate

                pre = sum(
                    measure(p.read_text(), "rust").code for p in project.source_files
                )
                gate = default_tx_gate(
                    root, {p: p.read_text() for p in project.source_files}, timeout
                )
                dedup_stats, dedup_records = dedup_tree(
                    root, project.source_files, dedup_backend, gate, attempts=2
                )
                dedup_loc = pre - sum(
                    measure(p.read_text(), "rust").code for p in project.source_files
                )
            result = stats.to_json()
            valid = bool(
                result["tests_ok"]
                and result["api_ok"]
                and result["docs_ok"]
                and result["formatted_loc"]
                and result["loc_start"]
                and (
                    not item.audit
                    or (audit_final is not None and audit_final.returncode == 0)
                )
            )
            # A failed baseline test still belongs in the measured denominator.
            loc_start = (
                (result["loc_start"] or baseline_loc.code)
                if baseline_loc.formatted
                else 0
            )
            if valid:
                static_final = result["loc_after_static"]
                hybrid_final = result["loc_final"]
                static_pct = pct(loc_start, static_final)
                hybrid_pct = pct(loc_start, hybrid_final)
                llm_extra_pct = pct(static_final, hybrid_final)
            else:
                static_pct = 0.0
                hybrid_pct = 0.0
                llm_extra_pct = 0.0
                static_final = loc_start
                hybrid_final = loc_start
            rows.append(
                {
                    "name": item.name,
                    "cohort": item.cohort,
                    "test_command": list(item.test),
                    "audit_command": list(item.audit),
                    "repo": item.repo,
                    "commit": item.commit,
                    "lang": item.lang,
                    "valid": valid,
                    "loc_start": loc_start,
                    "loc_after_static": static_final,
                    "loc_final": hybrid_final,
                    "test_loc": inline_test_loc,
                    "test_share_pct": round(100.0 * inline_test_loc / loc_start, 2)
                    if loc_start
                    else 0.0,
                    "non_test_loc": max(loc_start - inline_test_loc, 0),
                    "raw_pct": hybrid_pct,
                    "static_pct": static_pct,
                    "llm_extra_pct": llm_extra_pct,
                    "tests_ok": result["tests_ok"],
                    "api_ok": result["api_ok"],
                    "docs_ok": result["docs_ok"],
                    "formatted_loc": result["formatted_loc"],
                    "ml_stats": result.get("ml_stats", {}),
                    "ml_records": result.get("ml_records", []),
                    "ml_file_loc": result.get("ml_file_loc", 0),
                    "ml_file_stats": result.get("ml_file_stats", {}),
                    "ml_file_records": result.get("ml_file_records", []),
                    "dedup_loc": dedup_loc,
                    "dedup_stats": dedup_stats,
                    "dedup_records": dedup_records,
                    "audit_records": result.get("audit_records", []),
                    "oracle": result.get("oracle", {}),
                    "source_diff": "".join(
                        "".join(
                            difflib.unified_diff(
                                original.splitlines(keepends=True),
                                path.read_text(encoding="utf-8").splitlines(
                                    keepends=True
                                ),
                                fromfile=f"a/{path.relative_to(root)}",
                                tofile=f"b/{path.relative_to(root)}",
                            )
                        )
                        for path, original in sorted(original_sources.items())
                    ),
                    "audit_ok": audit_final.returncode == 0
                    if audit_final is not None
                    else None,
                    "audit_baseline_ok": audit_baseline.returncode == 0
                    if audit_baseline is not None
                    else None,
                    "audit_error": (
                        (
                            (audit_final or audit_baseline).stdout
                            + (audit_final or audit_baseline).stderr
                        )[-1000:]
                        if item.audit
                        and (audit_final is None or audit_final.returncode)
                        else None
                    ),
                    "baseline_measured": baseline_loc.formatted,
                    "duration_s": round(time.monotonic() - started, 3),
                    "layer_records": result["layer_records"],
                    "notes": result["static_notes"],
                }
            )
            print(
                f"[{item.name}] {'pass' if valid else 'FAIL'}: {loc_start} -> {hybrid_final} ({hybrid_pct}%)",
                file=sys.stderr,
                flush=True,
            )
    return summarize(rows, ml_backend, ml_attempts, ml_symbols)


def summarize(
    rows: list[dict], ml_backend=None, ml_attempts: int = 3, ml_symbols: int = 64
) -> dict:
    """Aggregate saved rows as well as fresh runs without changing denominators."""
    loc_start_total = sum(row["loc_start"] for row in rows)
    loc_after_static_total = sum(row["loc_after_static"] for row in rows)
    loc_final_total = sum(row["loc_final"] for row in rows)
    ml_file_loc_total = sum(row.get("ml_file_loc", 0) for row in rows)
    dedup_loc_total = sum(row.get("dedup_loc", 0) for row in rows)
    return {
        "experiment": {
            "ml_backend": getattr(ml_backend, "name", None),
            "ml_attempts": ml_attempts if ml_backend else 0,
            "ml_symbols": ml_symbols if ml_backend else 0,
        },
        "cohorts": {
            cohort: {
                "projects": sum(row["cohort"] == cohort for row in rows),
                "valid": sum(row["valid"] for row in rows if row["cohort"] == cohort),
                "reduction_pct": pct(
                    sum(row["loc_start"] for row in rows if row["cohort"] == cohort),
                    sum(row["loc_final"] for row in rows if row["cohort"] == cohort),
                ),
            }
            for cohort in sorted({row["cohort"] for row in rows})
        },
        "metric": "weighted canonical code LOC; invalid attempts score zero reduction",
        "projects": rows,
        "aggregate": {
            "projects": len(rows),
            "valid": sum(row["valid"] for row in rows),
            "projects_reduced": sum(
                row["loc_final"] < row["loc_start"] for row in rows
            ),
            "median_project_pct": median(row.get("raw_pct", 0.0) for row in rows)
            if rows
            else 0.0,
            "unmeasured": sum(not row["baseline_measured"] for row in rows),
            "loc_start": loc_start_total,
            "loc_after_static": loc_after_static_total,
            "loc_final": loc_final_total,
            "raw_pct": pct(loc_start_total, loc_final_total),
            "static_pct": pct(loc_start_total, loc_after_static_total),
            "llm_extra_pct": pct(loc_after_static_total, loc_final_total),
            "ml_file_loc": ml_file_loc_total,
            "dedup_loc": dedup_loc_total,
            "audit_performed": bool(rows)
            and all(row.get("audit_ok") is not None for row in rows),
            "audited_projects": sum(row.get("audit_ok") is not None for row in rows),
        },
    }


def write_corpus_report(result: dict, json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    aggregate = result["aggregate"]
    lines = [
        "# Pinned corpus baseline",
        "",
        (
            f"Coverage: {aggregate['valid']}/{aggregate['projects']} projects passed. "
            + (
                "Complete run."
                if aggregate["valid"] == aggregate["projects"]
                else "INCOMPLETE: do not compare this aggregate with a complete-corpus result."
            )
        ),
        "",
        (
            f"Weighted post-formatter code LOC: "
            f"**{aggregate['loc_start']} → {aggregate['loc_final']}** "
            f"({aggregate['raw_pct']}% reduction)"
        ),
        "",
        "| Project | Language | LOC | static % | total % | Tests/API/docs | Shadow oracle |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in result["projects"]:
        gates = "pass" if row["valid"] else "fail (scored as 0%)"
        oracle = row.get("oracle") or {}
        shadow = (
            f"{oracle.get('exercised', 0)}/{oracle.get('functions', 0)} fns, "
            f"{oracle.get('verified_calls', 0)} calls"
            if oracle.get("functions")
            else "n/a"
        )
        lines.append(
            f"| {row['name']} | {row['lang']} | "
            f"{row['loc_start']} → {row['loc_final']} | "
            f"{row.get('static_pct', 0.0)}% | {row.get('raw_pct', 0.0)}% | {gates} | {shadow} |"
        )
    lines += [
        "",
        (
            "Every revision is pinned in `corpus.toml`; "
            "percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence."
        ),
        f"Projects with a final regression/validation audit: {aggregate.get('audited_projects', 0)}. Audit-based checkpoint selection is not independent held-out evaluation.",
        f"Unmeasured projects: {aggregate['unmeasured']}; their LOC is unknown and excluded from the denominator.",
        "Shadow oracle (Python): rewritten functions whose original body ran alongside the rewrite on every suite call, with identical results, exceptions, iterator items and argument mutation. Functions the suite never calls are counted but unverified.",
    ]
    if result.get("experiment", {}).get("kind"):
        lines[2:2] = [f"Experiment: {result['experiment']['kind']}.", ""]
    for row in result["projects"]:
        if row.get("ml_stats", {}).get("rolled_back"):
            lines.append(
                f"{row['name']}: {row['ml_stats']['rolled_back']} initially accepted model edit(s) rolled back after audit; final LOC reflects the restored checkpoint."
            )
    if "review_filtered_aggregate" in result:
        reviewed = result["review_filtered_aggregate"]
        rejected = sum(
            record.get("review_outcome") == "rejected"
            for row in result["projects"]
            for record in row.get("ml_records", [])
        )
        lines[2:2] = [
            f"**Review-filtered reduction: {reviewed['raw_pct']}%.** The table below preserves the raw test-gated results; {rejected} model edits were rejected in review.",
            "",
        ]
        lines += [
            "",
            f"Post-run review-filtered result: {reviewed['loc_start']} → {reviewed['loc_final']} ({reviewed['raw_pct']}%). Rejected model layers fall back to their previously gate-verified static stage. See MODEL_REVIEW.md; the table above retains the raw test-gated results.",
        ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
