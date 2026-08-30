"""Mutation-score audit: quantify how much behavior the test suite pins."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .mutator import generate_mutations
from .testrunners import run_tests


@dataclass
class AuditResult:
    total: int = 0
    killed: int = 0
    skipped_no_baseline: bool = False
    survivors: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 0.0


def audit(
    root: Path,
    lang: str,
    source_files: list[Path],
    max_mutants: int = 40,
    timeout: int = 600,
) -> AuditResult:
    result = AuditResult()
    baseline = run_tests(root, lang, timeout=timeout)
    if not baseline.ok:
        result.skipped_no_baseline = True
        return result
    for path in source_files:
        mutants = generate_mutations(path, lang, max_mutants=max_mutants)
        original = path.read_text(encoding="utf-8", errors="replace")
        try:
            for mutant in mutants:
                result.total += 1
                path.write_text(mutant.mutated_source, encoding="utf-8")
                outcome = run_tests(root, lang, timeout=timeout)
                if not outcome.ok:
                    result.killed += 1
                else:
                    result.survivors.append(f"{path.name}:{mutant.description}")
        finally:
            path.write_text(original, encoding="utf-8")
    return result


def audit_to_json(result: AuditResult, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "total": result.total,
                "killed": result.killed,
                "score": round(result.score, 4),
                "survivors": result.survivors[:50],
                "skipped_no_baseline": result.skipped_no_baseline,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
