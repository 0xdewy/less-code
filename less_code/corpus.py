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
            rows.append(
                {
                    "name": item.name,
                    "repo": item.repo,
                    "commit": item.commit,
                    "lang": item.lang,
                    "valid": valid,
                    "loc_start": baseline_loc.code,
                    "loc_final": result["loc_final"] if valid else baseline_loc.code,
                    "reduction_pct": pct(result["loc_start"], result["loc_final"])
                    if valid
                    else 0.0,
                    "tests_ok": result["tests_ok"],
                    "api_ok": result["api_ok"],
                    "docs_ok": result["docs_ok"],
                    "formatted_loc": result["formatted_loc"],
                    "notes": result["static_notes"],
                }
            )
    before = sum(row["loc_start"] for row in rows)
    after = sum(row["loc_final"] for row in rows)
    return {
        "metric": "weighted canonical code LOC; invalid attempts score zero reduction",
        "projects": rows,
        "aggregate": {
            "projects": len(rows),
            "valid": sum(row["valid"] for row in rows),
            "loc_start": before,
            "loc_final": after,
            "reduction_pct": pct(before, after),
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
            f"Weighted post-formatter code LOC: **{aggregate['loc_start']} → "
            f"{aggregate['loc_final']} ({aggregate['reduction_pct']}%)**"
        ),
        "",
        "| Project | Language | LOC | Reduction | Tests/API/docs |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in result["projects"]:
        gates = "pass" if row["valid"] else "fail (scored as 0%)"
        lines.append(
            f"| {row['name']} | {row['lang']} | "
            f"{row['loc_start']} → {row['loc_final']} | "
            f"{row.get('reduction_pct', 0.0)}% | {gates} |"
        )
    lines += [
        "",
        (
            "Every revision is pinned in `corpus.toml`; percentages are weighted "
            "by code LOC."
        ),
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
