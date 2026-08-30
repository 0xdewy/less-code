"""Language detection and source-file discovery for a target project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".rs": "rust",
}

SKIP_DIRS = {
    "node_modules", ".git", "target", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", "coverage",
}

TEST_HINTS = ("test", "spec")


@dataclass
class ProjectMap:
    root: Path
    lang: str
    source_files: list[Path]
    test_files: list[Path]

    @property
    def target_files(self) -> list[Path]:
        return self.source_files


def is_test_file(path: Path) -> bool:
    name = path.name.lower()
    return any(h in name for h in TEST_HINTS) or "tests" in path.parts


def detect_language(root: Path) -> str | None:
    counts: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file() and not any(p in SKIP_DIRS for p in path.parts) and path.suffix in EXT_LANG:
            lang = EXT_LANG[path.suffix]
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    priority = {"rust": 0.5, "python": 0.5, "javascript": 0.5, "typescript": 0.5}
    if (root / "Cargo.toml").exists():
        return "rust"
    if (root / "package.json").exists() and (counts.get("javascript") or counts.get("typescript")):
        return "typescript" if counts.get("typescript", 0) > counts.get("javascript", 0) else "javascript"
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        return "python"
    return max(counts, key=lambda k: counts[k] * priority.get(k, 1.0))


def map_project(root: Path, lang: str | None = None) -> ProjectMap:
    lang = lang or detect_language(root)
    if lang is None:
        raise SystemExit(f"cannot detect language of {root}")
    exts = [ext for ext, l in EXT_LANG.items() if l == lang]
    source: list[Path] = []
    tests: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in exts:
            continue
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        (tests if is_test_file(path) else source).append(path)
    return ProjectMap(root=root, lang=lang, source_files=source, test_files=tests)
