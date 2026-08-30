"""Layer 2 — LLM semantic reduction with a hard verify gate.

Every candidate is written to disk, the frozen suite runs, the public API must
be unchanged, and code-LOC must shrink. Anything else reverts. Acceptance is
binary and evidence-based; the model never sees the tests' internals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .api_check import EXTRACTORS, api_violations
from .backends import Backend
from .loc import count_source

SYSTEM_PROMPT = """You are an expert code minimizer. Rewrite code to use fewer lines while:
1. Preserving behavior EXACTLY — the existing test suite must still pass.
2. Preserving the public API exactly: same exported/public symbols, same names, same signatures.
3. Keeping the code readable and idiomatic — NO minification tricks (no semicolon-chained lines, no one-letter names, no removing types).
4. Removing: redundant guards, dead branches, over-abstraction, duplicate logic (merge with helper functions), verbose constructs replaceable by standard-library/idiomatic equivalents, unnecessary comments/docstrings ONLY IF the logic itself is also simplified.
5. Consolidating copy-pasted blocks into parameterized helpers when it genuinely reduces total lines.
Output ONLY the complete rewritten file inside a single fenced code block. No explanations."""

FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)


@dataclass
class AttemptRecord:
    file: str
    attempt: int
    outcome: str
    loc_before: int
    loc_after: int
    detail: str = ""


@dataclass
class LLMResult:
    accepted: dict[str, str] = field(default_factory=dict)
    records: list[AttemptRecord] = field(default_factory=list)


def extract_code(response: str) -> str | None:
    blocks = FENCE.findall(response)
    if not blocks:
        return None
    return blocks[-1].strip("\n")


def build_prompt(path: Path, source: str, lang: str, loc: int, feedback: str) -> str:
    tag = {"python": "python", "javascript": "javascript", "typescript": "typescript", "rust": "rust"}[lang]
    fb = f"\nPrevious attempt feedback (do better this time):\n{feedback}\n" if feedback else ""
    return (
        f"File: {path.name} ({loc} code lines)\n"
        f"Language: {tag}\n{fb}"
        f"Rewrite this file with significantly fewer lines (aim for 20-60% fewer), "
        f"following the system rules. Return the complete file only.\n\n"
        f"```{tag}\n{source}\n```"
    )


def verify_candidate(
    path: Path,
    lang: str,
    candidate: str,
    api_before: dict[str, str],
    loc_before: int,
    run_tests_fn,
    root: Path,
) -> tuple[str, int]:
    """Apply-write-test-revert. Returns (outcome, loc_after)."""
    original = path.read_text(encoding="utf-8", errors="replace")
    try:
        path.write_text(candidate + "\n", encoding="utf-8")
        api_after = EXTRACTORS[lang](candidate)
        violations = api_violations({str(path): api_before}, {str(path): api_after})
        if violations:
            return f"api-changed: {violations[0][:120]}", loc_before
        loc_after = count_source(candidate, lang).code
        if loc_after >= loc_before:
            return "not-smaller", loc_after
        result = run_tests_fn(root, lang)
        if not result.ok:
            tail = result.output_tail[-300:].replace("\n", " | ")
            return f"tests-failed: {tail[:200]}", loc_after
        return "accepted", loc_after
    finally:
        path.write_text(original, encoding="utf-8")


def reduce_file(
    backend: Backend,
    root: Path,
    path: Path,
    lang: str,
    run_tests_fn,
    attempts: int = 3,
) -> tuple[str, int, list[AttemptRecord]]:
    """Greedy multi-attempt reduction of one file. Returns (best_source, loc, records)."""
    source = path.read_text(encoding="utf-8", errors="replace")
    loc_before = count_source(source, lang).code
    api_before = EXTRACTORS[lang](source)
    records: list[AttemptRecord] = []
    best = source
    best_loc = loc_before
    feedback = ""
    for attempt in range(1, attempts + 1):
        if best_loc <= max(3, loc_before // 5):
            break
        try:
            response = backend.complete(
                SYSTEM_PROMPT,
                build_prompt(path, best, lang, best_loc, feedback),
                temperature=0.1 if attempt == 1 else 0.4 + 0.15 * attempt,
            )
        except Exception as exc:  # backend outage ends the pass, not the run
            records.append(AttemptRecord(str(path), attempt, "backend-error", best_loc, best_loc, str(exc)[:120]))
            break
        candidate = extract_code(response)
        if candidate is None:
            feedback = "Your reply contained no fenced code block. Return ONLY the complete file in one code block."
            records.append(AttemptRecord(str(path), attempt, "no-code-block", best_loc, best_loc))
            continue
        outcome, loc_after = verify_candidate(path, lang, candidate, api_before, best_loc, run_tests_fn, root)
        records.append(AttemptRecord(str(path), attempt, outcome.split(":")[0], best_loc, loc_after, outcome))
        if outcome == "accepted":
            best, best_loc = candidate, loc_after
            path.write_text(candidate + "\n", encoding="utf-8")
            feedback = ""
        else:
            feedback = outcome
    return best, best_loc, records
