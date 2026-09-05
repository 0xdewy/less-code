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
from .loc import count_tree
from .pipeline import ShrinkStats, pct, shrink_project


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
) -> dict:
    if not 1 <= ml_attempts <= 3:
        raise ValueError("ml_attempts must be between 1 and 3")
    if not 1 <= ml_symbols <= 64:
        raise ValueError("ml_symbols must be between 1 and 64")
    rows = []
    for item in load_corpus(manifest):
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
                    "raw_pct": hybrid_pct,
                    "static_pct": static_pct,
                    "llm_extra_pct": llm_extra_pct,
                    "tests_ok": result["tests_ok"],
                    "api_ok": result["api_ok"],
                    "docs_ok": result["docs_ok"],
                    "formatted_loc": result["formatted_loc"],
                    "ml_stats": result.get("ml_stats", {}),
                    "ml_records": result.get("ml_records", []),
                    "audit_records": result.get("audit_records", []),
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
        "| Project | Language | LOC | static % | total % | Tests/API/docs |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in result["projects"]:
        gates = "pass" if row["valid"] else "fail (scored as 0%)"
        lines.append(
            f"| {row['name']} | {row['lang']} | "
            f"{row['loc_start']} → {row['loc_final']} | "
            f"{row.get('static_pct', 0.0)}% | {row.get('raw_pct', 0.0)}% | {gates} |"
        )
    lines += [
        "",
        (
            "Every revision is pinned in `corpus.toml`; "
            "percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence."
        ),
        f"Projects with a final regression/validation audit: {aggregate.get('audited_projects', 0)}. Audit-based checkpoint selection is not independent held-out evaluation.",
        f"Unmeasured projects: {aggregate['unmeasured']}; their LOC is unknown and excluded from the denominator.",
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
