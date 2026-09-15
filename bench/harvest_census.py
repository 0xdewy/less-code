"""Census of accepted model diffs (PLAN.md Phase 3.1).

Aggregates the accepted diffs recorded by the ladder cells into shape
buckets and ranks them by accepted LOC. Rules in Phase 3 are built in this
ranked order; a shape with zero model acceptances is NOT built.

  uv run python bench/harvest_census.py        # writes bench/HARVEST.md
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LADDER_DIR = REPO / "bench" / "ladder"
HARVEST_MD = REPO / "bench" / "HARVEST.md"

BUCKETS: list[tuple[str, str]] = [
    (
        "loop-to-iterator",
        r"\.(sum|product|max|min|count|position|any|all|fold|collect)\(\)",
    ),
    ("vec-push-run", r"vec!\["),
    ("let-else", r"\blet\b.*\belse\s*\{"),
    ("match-table", r"(?:&?\[|match\s).*(?:=>|;\d|\[\"*)"),
    ("string-concat-run", r"push_str\(|concat\(|format!\("),
    ("nested-if-collapse", r"&&|\|\|"),
    ("dedup-extract-helper", r"^diff --git"),
    ("guard-merge", r"\breturn\b.*;?\s*}\s*else"),
]


def classify(diff: str) -> list[str]:
    """Shape buckets this accepted diff plausibly exercised (ranking, not proof)."""
    added = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    removed = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    tags: list[str] = []
    added_text = "\n".join(added)
    removed_text = "\n".join(removed)
    for name, pattern in BUCKETS:
        if name == "dedup-extract-helper":
            if diff.count("diff --git") >= 2 or re.search(
                r"^\+fn _", added_text, re.MULTILINE
            ):
                tags.append(name)
            continue
        if name == "match-table":
            if re.search(r"(&\[|\[[^\]]*=>|const .*: \[[^\]]*\] =)", added_text):
                tags.append(name)
            continue
        if re.search(pattern, added_text) or (
            name == "loop-to-iterator" and re.search(r"\bfor\b.*\bin\b", removed_text)
        ):
            tags.append(name)
    return tags or ["other"]


def census() -> dict:
    buckets: Counter = Counter()
    loc_by_bucket: Counter = Counter()
    accepted = 0
    for path in sorted(LADDER_DIR.glob("*.json")):
        cell = json.loads(path.read_text())
        for diff in cell.get("diffs", {}).values():
            accepted += 1
            added = sum(
                1
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            removed = sum(
                1
                for line in diff.splitlines()
                if line.startswith("-") and not line.startswith("---")
            )
            net = removed - added
            for tag in classify(diff):
                buckets[tag] += 1
                loc_by_bucket[tag] += max(net, 0)
    return {
        "accepted_files": accepted,
        "buckets": dict(buckets),
        "loc_by_bucket": {k: loc_by_bucket[k] for k in buckets},
    }


def write_harvest(result: dict) -> str:
    lines = [
        "# Harvest census - what the ladder's accepted diffs did",
        "",
        (
            f"Accepted rewrites: {result['accepted_files']}. Shapes ranked by"
            " accepted LOC; Phase 3 rules are built in this order, and a shape"
            " with zero acceptances is not built (bias to deletion)."
        ),
        "",
        "| rank | shape | accepted rewrites | accepted LOC |",
        "|---:|---|---:|---:|",
    ]
    ranked = sorted(result["loc_by_bucket"].items(), key=lambda kv: -kv[1])
    for rank, (shape, loc) in enumerate(ranked, 1):
        lines.append(f"| {rank} | {shape} | {result['buckets'][shape]} | {loc} |")
    if not ranked:
        lines.append("| n/a | no accepted diffs recorded yet | 0 | 0 |")
    HARVEST_MD.write_text("\n".join(lines) + "\n")
    return "\n".join(lines)


if __name__ == "__main__":
    print(write_harvest(census()))
