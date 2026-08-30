"""Layer 2 — LLM semantic reduction with a hard verify gate.

Every candidate is written to disk, the frozen suite runs, the public API must
be unchanged, and code-LOC must shrink. Anything else reverts. Acceptance is
binary and evidence-based; the model never sees the tests' internals.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .api_check import EXTRACTORS, api_violations
from .backends import Backend
from .loc import count_source

PROGRESS = None  # set by CLI to a printer for live per-attempt output


def _report(record) -> None:
    if PROGRESS:
        try:
            PROGRESS(record)
        except Exception:
            pass


SYSTEM_PROMPT = """You are an expert code minimizer. Rewrite code to use fewer lines while:
1. Preserving behavior EXACTLY — the test suite (provided) must still pass, including error paths.
2. Preserving the public API exactly: same public symbols, same names, same signatures. If you create helper functions, their names MUST start with underscore (_helper).
3. Keeping the code readable and idiomatic — NO minification tricks (no semicolon-chained lines, no one-letter names, no removing types).
4. Removing: redundant guards, dead branches, over-abstraction, duplicate logic (merge into _helpers), verbose constructs replaceable by standard-library/idiomatic equivalents.
5. Consolidating copy-pasted blocks into parameterized _helpers when it genuinely reduces total lines.
RESPONSE FORMAT: your ENTIRE response must be exactly one fenced code block containing
the complete rewritten code — first characters: ``` + the language tag, last
characters: ```. No prose before or after. Never describe or summarize the
code; OUTPUT the rewritten code itself."""


def build_prompt(path: Path, source: str, lang: str, loc: int, feedback: str, spec: str = "", focus: str = "") -> str:
    tag = {"python": "python", "javascript": "javascript", "typescript": "typescript", "rust": "rust"}[lang]
    fb = f"\nPrevious attempt feedback (fix this):\n{feedback}\n" if feedback else ""
    tests = (
        f"\nThe test suite this code must pass (behavior specification — error messages and exception types must match EXACTLY):\n```python\n{spec}\n```\n"
        if spec else ""
    )
    fo = f"\nFOCUS: {focus}\n" if focus else ""
    return (
        f"File: {path.name} ({loc} code lines)\n"
        f"Language: {tag}\n{fo}{fb}{tests}"
        f"Rewrite the code with significantly fewer lines (aim for 20-50% fewer), "
        f"following the system rules. Return the complete rewritten code only.\n\n"
        f"```{tag}\n{source}\n```"
    )


import re as _re


def first_failure(tail: str) -> str:
    """Extract the most informative failure line from a test-runner tail."""
    lines = [l.strip() for l in tail.splitlines() if l.strip()]
    best = ""
    for i, line in enumerate(lines):
        if "AssertionError" in line or "assert" in line.lower() and "error" in line.lower():
            best = line
            context = lines[i - 1] if i else ""
            return f"{context} -> {line}"[:250]
        if line.startswith(("FAILED", "FAIL ", "test result: FAILED", "panicked at")):
            best = line[:250]
    for line in lines:
        if "Error" in line or "error:" in line:
            return line[:250]
    return best or tail[-250:]

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


CODE_STARTERS = ("def ", "class ", "import ", "from ", "pub ", "fn ", "const ", "let ", "var ", "export ", "use ", "#!", "@", '"""', "/*")


def extract_code(response: str) -> str | None:
    blocks = FENCE.findall(response)
    if blocks:
        return blocks[-1].strip("\n")
    stripped = response.strip()
    if stripped.startswith("```"):
        # unterminated fence: truncation — take everything after the opening line
        first_nl = stripped.find("\n")
        if first_nl != -1:
            return stripped[first_nl + 1 :].strip("\n")
    head = stripped.lstrip()[:12]
    if any(stripped.startswith(s) for s in CODE_STARTERS):
        return stripped
    return None


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
            return f"tests-failed: {first_failure(result.output_tail)}", loc_after
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
    spec: str = "",
    focus: str = "",
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
                build_prompt(path, best, lang, best_loc, feedback, spec=spec, focus=focus),
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


# ---- chunked reduction (per top-level symbol; Python only) ----


def _top_level_spans(source: str) -> list[tuple[int, int, str, str]]:
    """(start_line, end_line, name, kind) for top-level defs/classes (0-based lines)."""
    tree = ast.parse(source)
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            spans.append((node.lineno - 1, node.end_lineno - 1, node.name, kind))
    return spans


def reduce_file_chunked(
    backend: Backend,
    root: Path,
    path: Path,
    run_tests_fn,
    attempts_per_chunk: int = 2,
    min_chunk_loc: int = 12,
    spec: str = "",
) -> list[AttemptRecord]:
    """Reduce each top-level def/class independently, biggest first.

    Spans are re-resolved by name after every acceptance because line numbers
    shift as the file shrinks.
    """
    records: list[AttemptRecord] = []
    for _sweep in range(2):
        attempted: set[str] = set()
        while True:
            source = path.read_text(encoding="utf-8", errors="replace")
            spans = [s for s in _top_level_spans(source) if s[2] not in attempted]
            if not spans:
                break
            start, end, name, kind = max(spans, key=lambda s: s[1] - s[0])
            attempted.add(name)
            lines = source.splitlines()
            chunk = "\n".join(lines[start : end + 1])
            if count_source(chunk, "python").code < min_chunk_loc:
                continue
            chunk_loc = count_source(chunk, "python").code
            api_before = EXTRACTORS["python"](source)
            full_loc = count_source(source, "python").code
            feedback = ""
            for attempt in range(1, attempts_per_chunk + 1):
                try:
                    response = backend.complete(
                        SYSTEM_PROMPT,
                        build_prompt(path, chunk, "python", chunk_loc, feedback, spec=spec),
                        temperature=0.1 if attempt == 1 else 0.5,
                    )
                except Exception as exc:
                    records.append(AttemptRecord(f"{path}:{name}", attempt, "backend-error", chunk_loc, chunk_loc, str(exc)[:100]))
                    break
                candidate = extract_code(response)
                if candidate is None:
                    feedback = "Return ONLY the rewritten code in one fenced block."
                    records.append(AttemptRecord(f"{path}:{name}", attempt, "no-code-block", chunk_loc, chunk_loc))
                    continue
                new_source = "\n".join(lines[:start]) + "\n" + candidate + "\n" + "\n".join(lines[end + 1 :])
                outcome, loc_after = verify_candidate(path, "python", new_source, api_before, full_loc, run_tests_fn, root)
                records.append(AttemptRecord(f"{path}:{name}", attempt, outcome.split(":")[0], full_loc, loc_after, outcome))
                if outcome == "accepted":
                    path.write_text(new_source + "\n", encoding="utf-8")
                    break
                feedback = outcome
    return records


def focused_passes(
    backend: Backend,
    root: Path,
    path: Path,
    lang: str,
    run_tests_fn,
    spec: str = "",
    top_k: int = 4,
    attempts_per_focus: int = 2,
) -> list[AttemptRecord]:
    """Whole-file rewrite passes, each focused on one big symbol, with
    failure-feedback retries (the verifier's failure line is fed back)."""
    records: list[AttemptRecord] = []
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        spans = _top_level_spans(source)
    except SyntaxError:
        return records
    spans = sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:top_k]
    for start, end, name, kind in spans:
        current = path.read_text(encoding="utf-8", errors="replace")
        loc_now = count_source(current, "python").code
        if loc_now < 15:
            break
        focus = (
            f"reduce the {kind} `{name}` specifically — it is the biggest remaining "
            f"block; keep every public attribute/method the tests use, keep error "
            f"messages EXACTLY as tests expect, and merge its duplicated logic"
        )
        best, best_loc, recs = reduce_file(
            backend, root, path, "python", run_tests_fn,
            attempts=attempts_per_focus, spec=spec, focus=focus,
        )
        records += recs
    return records
