"""Test-runner adapters: the verify gate's source of truth is exit code 0.

Two extras on top of the raw runners:
- a process-lifetime cache keyed by the content hash of the whole tree, so
  identical candidates (common across temperatures/hunk subsets) never pay
  for a second suite run (roadmap B4);
- `run_hidden_tests`, the bench-only safety net that runs each fixture's
  `tests_hidden/` target. Hidden tests are excluded from the frozen gate and
  from the prompt spec on purpose — they must never steer the reducer.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HIDDEN_DIR = "tests_hidden"


@dataclass
class TestResult:
    ok: bool
    exit_code: int
    duration_s: float
    output_tail: str
    cached: bool = False


def _run(cmd: list[str], cwd: Path, timeout: int) -> TestResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        tail = (proc.stdout + proc.stderr)[-4000:]
        return TestResult(
            proc.returncode == 0, proc.returncode, time.monotonic() - start, tail
        )
    except subprocess.TimeoutExpired as exc:
        tail = ((exc.stdout or b"") + (exc.stderr or b""))[-4000:]
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", errors="replace")
        return TestResult(
            False, -1, time.monotonic() - start, f"TIMEOUT after {timeout}s\n{tail}"
        )


def gate_command(root: Path, lang: str) -> list[str]:
    """The frozen gate's command. Hidden tests are excluded explicitly."""
    if lang == "python":
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--no-header"]
        hidden = (root / HIDDEN_DIR).resolve()
        if hidden.is_dir():
            cmd.append(f"--ignore={hidden}")
        return cmd
    if lang in ("javascript", "typescript"):
        package = root / "package.json"
        if package.is_file():
            try:
                scripts = json.loads(package.read_text()).get("scripts", {})
            except (OSError, json.JSONDecodeError):
                scripts = {}
            if "test" in scripts:
                return ["npm", "test", "--silent"]
        return ["node", "--test"]
    if lang == "rust":
        cmd = ["cargo", "test", "--quiet"]
        if (root / "Cargo.lock").is_file():
            cmd.append("--locked")
        return cmd
    raise ValueError(f"unsupported language {lang}")


def _probe_import(root: Path, name: str) -> str | None:
    """Where `python -c "import name"` resolves from cwd=root, the same
    sys.path the gate's `python -m pytest` uses ('' first, then site-packages).
    None when the import fails or reports no file (namespace package)."""
    code = f"import {name}\nprint({name}.__file__)"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None


def shadowed_imports(root: Path, probe=None) -> list[str]:
    """Importable units of `root` that resolve OUTSIDE the tree.

    A hit means the gate would silently test a different copy of the code —
    e.g. a same-named dependency of the host venv (pytest depends on
    `packaging`!) shadowing the editable install of the tree under reduction.
    Found the hard way: a pypa/packaging baseline failed on behavior the
    clone never had, because the suite had imported PyPI packaging 26.3.
    """
    from .langdetect import SKIP_DIRS, is_test_file

    if probe is None:
        probe = _probe_import
    names: list[str] = []
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            if entry.is_dir() and (entry / "__init__.py").is_file():
                names.append(entry.name)
            elif (
                entry.is_file()
                and entry.suffix == ".py"
                and entry.name != "__init__.py"
                and not is_test_file(entry)
                and not entry.name.startswith("conftest")
            ):
                names.append(entry.stem)
    shadowed: list[str] = []
    for name in names:
        resolved = probe(root, name)
        if resolved is None:
            continue
        path = Path(resolved)
        try:
            path.relative_to(root)
        except ValueError:
            shadowed.append(f"{name} -> {resolved}")
    return shadowed


# ---- content-hash cache (process lifetime) ----

_CACHE: dict[str, TestResult] = {}
STATS = {"hits": 0, "misses": 0}


def tree_hash(root: Path, lang: str) -> str:
    """sha256 over the content of every source+test file the project maps to."""
    from .langdetect import map_project

    digest = hashlib.sha256()
    try:
        project = map_project(root, lang)
        files = sorted(project.source_files + project.test_files)
    except SystemExit:
        files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        digest.update(str(path.relative_to(root)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def run_tests(
    root: Path,
    lang: str,
    timeout: int = 600,
    quiet: bool = False,
    use_cache: bool = True,
    command: list[str] | None = None,
) -> TestResult:
    """Run the frozen suite. `use_cache=False` bypasses the content-hash cache."""
    cmd = command or gate_command(root, lang)
    if not use_cache:
        return _run(cmd, root, timeout)
    key = f"{lang}:{root}:{json.dumps(cmd)}:{tree_hash(root, lang)}"
    hit = _CACHE.get(key)
    if hit is not None:
        STATS["hits"] += 1
        return TestResult(
            hit.ok, hit.exit_code, hit.duration_s, hit.output_tail, cached=True
        )
    STATS["misses"] += 1
    result = _run(cmd, root, timeout)
    _CACHE[key] = result
    return result


def run_hidden_tests(root: Path, lang: str, timeout: int = 600) -> TestResult | None:
    """Run the fixture's hidden suite, or None when it has none.

    python: `pytest tests_hidden`; javascript: every file in `tests_hidden/`
    (named so plain `node --test` ignores them); rust: the `hidden` test
    target, declared with `test = false` so `cargo test` skips it.
    """
    hidden = (root / HIDDEN_DIR).resolve()
    if not hidden.is_dir():
        return None
    if lang == "python":
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            str(hidden),
        ]
    elif lang in ("javascript", "typescript"):
        files = sorted(
            str(p) for p in hidden.rglob("*") if p.suffix in (".js", ".mjs", ".cjs")
        )
        if not files:
            return None
        cmd = ["node", "--test", *files]
    elif lang == "rust":
        cmd = ["cargo", "test", "--quiet", "--test", "hidden"]
    else:
        return None
    return _run(cmd, root, timeout)
