"""Test-runner adapters: the verify gate's source of truth is exit code 0."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResult:
    ok: bool
    exit_code: int
    duration_s: float
    output_tail: str


def _run(cmd: list[str], cwd: Path, timeout: int) -> TestResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        tail = (proc.stdout + proc.stderr)[-4000:]
        return TestResult(proc.returncode == 0, proc.returncode, time.monotonic() - start, tail)
    except subprocess.TimeoutExpired as exc:
        tail = ((exc.stdout or b"") + (exc.stderr or b""))[-4000:]
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", errors="replace")
        return TestResult(False, -1, time.monotonic() - start, f"TIMEOUT after {timeout}s\n{tail}")


def run_tests(root: Path, lang: str, timeout: int = 600, quiet: bool = False) -> TestResult:
    if lang == "python":
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--no-header"]
    elif lang in ("javascript", "typescript"):
        cmd = ["node", "--test"]
    elif lang == "rust":
        cmd = ["cargo", "test", "--quiet"]
    else:
        raise ValueError(f"unsupported language {lang}")
    if quiet:
        pass
    return _run(cmd, root, timeout)
