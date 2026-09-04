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
    "node_modules",
    ".git",
    "target",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "coverage",
    # hidden tests are the bench's safety net: never mapped, so they stay out
    # of the prompt spec, the frozen gate and every LOC measurement.
    "tests_hidden",
    # entry-point trees, not libraries: their files are invoked by name
    # (sphinx conf.py, noxfile sessions, demo scripts) or exist to be read,
    # so "unreferenced by the tests" says nothing about them being dead.
    # Found the hard way: the dead-code layer stripped noxfile sessions from
    # packaging and demo subcommands from click's examples/.
    "docs",
    "examples",
    "benchmarks",
}

#: Directory names that make everything inside a test: pytest's `tests/`,
#: cargo integration tests, jest's `__tests__`. Deliberately NOT `testing/`:
#: projects ship modules under that name (numpy.testing, click.testing).
TEST_DIRS = {"tests", "test", "__tests__"}


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
    """Convention-based test detection: name patterns per language, or any
    parent directory named as a test directory.

    The old substring rule (`"test" in name`) misclassified shipped library
    modules as tests — click's `testing.py` (the CliRunner, public API) was
    silently excluded from both the reduction targets and the API surface the
    gate enforces. False negatives (a test file read as source) would be
    worse still: the reducer may never rewrite its own oracle, so the name
    patterns err toward classifying as test (`*_test.py`, `-spec.ts`, ...).
    """
    parts = path.parts
    name = path.name.lower()
    suffix = path.suffix.lower()
    if any(part in TEST_DIRS for part in parts[:-1]):
        return True
    if suffix == ".py":
        return (
            name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        )
    if suffix in (".js", ".mjs", ".cjs", ".ts", ".mts"):
        stem = name.rsplit(".", 1)[0]
        return (
            stem in {"test", "tests", "spec"}
            or any(
                stem.endswith(sep)
                for sep in (".test", ".spec", "_test", "_spec", "-test", "-spec")
            )
            or stem.startswith(("test-", "spec-"))
        )
    # rust unit tests live in #[cfg(test)] mods inside source files; only the
    # cargo `tests/` directory rule (above) marks whole files as tests.
    return False


def detect_language(root: Path) -> str | None:
    counts: dict[str, int] = {}
    for path in root.rglob("*"):
        if (
            path.is_file()
            and not any(p in SKIP_DIRS for p in path.parts)
            and path.suffix in EXT_LANG
        ):
            lang = EXT_LANG[path.suffix]
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    priority = {"rust": 0.5, "python": 0.5, "javascript": 0.5, "typescript": 0.5}
    if (root / "Cargo.toml").exists():
        return "rust"
    if (root / "package.json").exists() and (
        counts.get("javascript") or counts.get("typescript")
    ):
        return (
            "typescript"
            if counts.get("typescript", 0) > counts.get("javascript", 0)
            else "javascript"
        )
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
