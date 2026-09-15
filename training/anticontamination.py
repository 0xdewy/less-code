"""Exam/homework separation, enforced by test - not promised (PLAN.md §6.1).

The four benchmark crates NEVER appear in training data, prompts, few-shot
examples, or SFT pairs. Given any mined record, prompt payload, or dataset
file, this module asserts that none of the protected names, repo URLs, or
pinned commit hashes appear anywhere. A violation fails the build.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

BENCHMARK_CRATES: tuple[dict[str, str], ...] = tuple(
    {
        "name": item["name"],
        "repo": item["repo"],
        "commit": item["commit"],
    }
    for item in tomllib.loads((REPO / "bench" / "corpus.toml").read_text())["project"]
)

_ALIASES: dict[str, str] = {
    "strsim-rs": "strsim",
    "itoa": "itoa",
    "humantime": "humantime",
    "shell-words": "shell-words",
}


def protected_tokens() -> set[str]:
    """Every string whose appearance in training data is a violation."""
    tokens: set[str] = set()
    for crate in BENCHMARK_CRATES:
        tokens.add(crate["name"])
        alias = _ALIASES.get(crate["name"])
        if alias:
            tokens.add(alias)
        tokens.add(crate["repo"])
        tokens.add(crate["repo"].removesuffix(".git"))
        tokens.add(crate["commit"])
    return tokens


def find_contamination(payload: object, tokens: set[str] | None = None) -> list[str]:
    """Return every protected token found anywhere inside `payload` - dicts,
    lists, strings, arbitrarily deep. Empty list means clean."""
    tokens = protected_tokens() if tokens is None else tokens
    found: list[str] = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            for token in tokens:
                if token in item:
                    found.append(token)
    return sorted(set(found))


def scan_file(path: Path, tokens: set[str] | None = None) -> list[str]:
    """Scan one dataset file (JSONL: one JSON object per line, or any text)."""
    tokens = protected_tokens() if tokens is None else tokens
    found: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            payload = line
        found.extend(find_contamination(payload, tokens))
    return sorted(set(found))
