"""Pinned, reproducible multi-project reduction benchmark."""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .langdetect import map_project
from .loc import count_tree
from .pipeline import pct, shrink_project


@dataclass(frozen=True)
class CorpusProject:
    name: str
    repo: str
    commit: str
    lang: str
    test: tuple[str, ...]
    prepare: tuple[str, ...] = ()


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
        )
        for item in data["project"]
    ]


def _run(
    command: tuple[str, ...], cwd: Path, timeout: int
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def run_corpus(manifest: Path, timeout: int = 900) -> dict:
    rows = []
    with tempfile.TemporaryDirectory(prefix="less-code-corpus-") as scratch:
        for item in load_corpus(manifest):
            root = Path(scratch) / item.name
            clone = subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--filter=blob:none",
                    "--no-checkout",
                    item.repo,
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
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
                        "lang": item.lang,
                        "valid": False,
                        "loc_start": 0,
                        "loc_final": 0,
                        "error": (clone.stderr or prepare.stderr)[-500:],
                    }
                )
                continue
            project = map_project(root, item.lang)
            baseline_loc = count_tree(
                project.source_files, item.lang, format_first=True
            )
            stats = shrink_project(
                root,
                item.lang,
                test_timeout=timeout,
                test_command=list(item.test) or None,
            )
            result = stats.to_json()
            valid = bool(
                result["tests_ok"]
                and result["api_ok"]
                and result["docs_ok"]
                and result["formatted_loc"]
                and result["loc_start"]
            )
            loc_start = result["loc_start"]
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
                    "repo": item.repo,
                    "commit": item.commit,
                    "lang": item.lang,
                    "valid": valid,
                    "loc_start": loc_start,
                    "loc_after_static": static_final,
                    "loc_final": hybrid_final,
                    "raw_pct": hybrid_pct,
                    "static_pct": static_pct,
                    "audited_pct": static_pct,
                    "llm_extra_pct": llm_extra_pct,
                    "tests_ok": result["tests_ok"],
                    "api_ok": result["api_ok"],
                    "docs_ok": result["docs_ok"],
                    "formatted_loc": result["formatted_loc"],
                    "ml_stats": result.get("ml_stats", {}),
                    "notes": result["static_notes"],
                }
            )
    loc_start_total = sum(row["loc_start"] for row in rows)
    loc_after_static_total = sum(row["loc_after_static"] for row in rows)
    loc_final_total = sum(row["loc_final"] for row in rows)
    return {
        "metric": "weighted canonical code LOC; invalid attempts score zero reduction",
        "projects": rows,
        "aggregate": {
            "projects": len(rows),
            "valid": sum(row["valid"] for row in rows),
            "loc_start": loc_start_total,
            "loc_after_static": loc_after_static_total,
            "loc_final": loc_final_total,
            "raw_pct": pct(loc_start_total, loc_final_total),
            "static_pct": pct(loc_start_total, loc_after_static_total),
            "llm_extra_pct": pct(loc_after_static_total, loc_final_total),
            "audited_pct": pct(
                loc_start_total, loc_after_static_total
            ),  # the deterministic reduction is what survives a re-shrink
            # on the reduced tree; LLM proposals are not re-checked here.
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
            f"Weighted post-formatter code LOC: "
            f"**{aggregate['loc_start']} → {aggregate['loc_final']}** "
            f"({aggregate['static_pct']}% reduction)"
        ),
        "",
        "| Project | Language | LOC | reduction % | Tests/API/docs |",
        "|---|---:|---:|---:|---|",
    ]
    for row in result["projects"]:
        gates = "pass" if row["valid"] else "fail (scored as 0%)"
        lines.append(
            f"| {row['name']} | {row['lang']} | "
            f"{row['loc_start']} → {row['loc_final']} | "
            f"{row.get('static_pct', 0.0)}% | {gates} |"
        )
    lines += [
        "",
        "Every revision is pinned in `corpus.toml`; "
        "percentages are weighted by code LOC.",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
