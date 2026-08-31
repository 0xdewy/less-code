"""Layer 2 — LLM semantic reduction with a hard verify gate.

Every candidate is written to disk, the frozen suite runs, the public API must
be unchanged, and code-LOC must shrink. Anything else reverts. Acceptance is
binary and evidence-based; the model never sees the tests' internals.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import re
from dataclasses import dataclass, field
from pathlib import Path

from .api_check import EXTRACTORS, api_feedback, api_violations
from .backends import Backend
from .hunks import decompose, search, symbol_spans
from .loc import measure

PROGRESS = None  # set by CLI to a printer for live per-attempt output


def _report(record):
    """Emit one AttemptRecord live (the CLI/pipeline sets `PROGRESS`).

    Records used to be printed only after a whole file finished, so a
    multi-hour GPU run showed nothing until it was over.
    """
    if PROGRESS:
        try:
            PROGRESS(record)
        except Exception:
            pass
    return record


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


def _feedback_for(outcome: str) -> str:
    """The rejection turned into instructions for the next attempt.

    Test failures were always fed back verbatim; an `api-changed` rejection
    used to feed back the bare word, which told the model nothing. Now the
    concrete missing/changed/added symbol lists go into the prompt.
    """
    if outcome.startswith("api-changed:"):
        return api_feedback([v.strip() for v in outcome[len("api-changed:"):].split(" | ") if v.strip()])
    return outcome


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


def syntax_check(path: Path, lang: str, root: Path) -> str:
    """Cheap parse gate (B3). Returns '' when the file on disk parses."""
    if lang == "python":
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            return f"{exc.msg} (line {exc.lineno})"
        return ""
    if lang in ("javascript", "typescript"):
        cmd = ["node", "--check", str(path)]
    elif lang == "rust":
        cmd = ["cargo", "check", "--quiet", "--tests"]
    else:
        return ""
    if shutil.which(cmd[0]) is None:
        return ""
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=600)
    if proc.returncode == 0:
        return ""
    return first_failure(proc.stdout + proc.stderr)


# rejection stages, cheapest first: most candidates die in milliseconds
STAGES = ("not-smaller", "syntax-error", "api-changed", "tests-failed")


def verify_candidate(
    path: Path,
    lang: str,
    candidate: str,
    api_before: dict[str, str],
    loc_before: int,
    run_tests_fn,
    root: Path,
) -> tuple[str, int]:
    """Apply-write-test-revert with cheap pre-gates. Returns (outcome, loc_after).

    Order is not-smaller -> parse -> API -> tests (roadmap B3): the size check
    needs no disk write at all and the parse gate costs milliseconds, so a full
    suite run is only ever spent on a candidate that could plausibly pass.
    """
    loc_after = measure(candidate, lang).code
    if loc_after >= loc_before:
        return "not-smaller", loc_after
    original = path.read_text(encoding="utf-8", errors="replace")
    try:
        path.write_text(candidate + "\n", encoding="utf-8")
        syntax = syntax_check(path, lang, root)
        if syntax:
            return f"syntax-error: {syntax[:120]}", loc_after
        api_after = EXTRACTORS[lang](candidate)
        violations = api_violations({str(path): api_before}, {str(path): api_after})
        if violations:
            # the whole list, not a stub: `_feedback_for` turns it into the
            # next prompt's instructions (gap item 2)
            return "api-changed: " + " | ".join(violations), loc_after
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
    decompose_rejected: bool = True,
) -> tuple[str, int, list[AttemptRecord]]:
    """Greedy multi-attempt reduction of one file. Returns (best_source, loc, records).

    A rejected whole-file rewrite is not thrown away: it is decomposed into
    symbol-aligned hunks and the largest passing subset is accepted (C1).
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    loc_before = measure(source, lang).code
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
            records.append(_report(AttemptRecord(str(path), attempt, "backend-error", best_loc, best_loc, str(exc)[:120])))
            break
        candidate = extract_code(response)
        if candidate is None:
            feedback = "Your reply contained no fenced code block. Return ONLY the complete file in one code block."
            records.append(_report(AttemptRecord(str(path), attempt, "no-code-block", best_loc, best_loc)))
            continue
        outcome, loc_after = verify_candidate(path, lang, candidate, api_before, best_loc, run_tests_fn, root)
        records.append(_report(AttemptRecord(str(path), attempt, outcome.split(":")[0], best_loc, loc_after, outcome)))
        if outcome == "accepted":
            best, best_loc = candidate, loc_after
            path.write_text(candidate + "\n", encoding="utf-8")
            feedback = ""
            continue
        feedback = _feedback_for(outcome)
        if not decompose_rejected or not outcome.startswith(("tests-failed", "api-changed")):
            continue
        # C1: the rewrite is usually right about most symbols — keep those.
        hunks = decompose(best, candidate, lang)
        if len(hunks) < 2:
            continue
        found = search(
            path, lang, best, hunks, api_before, best_loc, run_tests_fn, root,
        )
        records.append(
            AttemptRecord(
                str(path), attempt,
                "hunks-accepted" if found.loc < best_loc else "hunks-none",
                best_loc, found.loc, found.summary(),
            )
        )
        if found.loc < best_loc:
            best, best_loc = found.source, found.loc
            path.write_text(best + "\n", encoding="utf-8")
            feedback = ""
    return best, best_loc, records


# ---- C2: per-symbol proposals (the DEFAULT L2 strategy) --------------------
#
# Iteration 07's finding: at 3B *and* 7B, no whole-file candidate ever passed
# outright — every accepted LOC came from hunk-salvage of a REJECTED rewrite.
# A 500-LOC rewrite is one 10-minute all-or-nothing bet; the same GPU minutes
# spent on ~20 per-symbol rewrites are ~20 independently gated bets, each with
# a short output the model can actually get right. So the primary loop now
# rewrites ONE top-level symbol at a time, biggest first, for all three
# languages (python via ast, js/rust via the brace matcher in `hunks.py`).
# The whole-file pass survives as an optional final sweep, because it is the
# only shape that can dedup ACROSS symbols.


SYMBOL_SYSTEM_PROMPT = """You are an expert code minimizer. You are given ONE symbol (function/class/impl block) from a larger file, plus a map of the rest of the file and the test suite the file must pass.
Rewrite THAT ONE SYMBOL to use fewer lines while:
1. Preserving behavior EXACTLY — the test suite must still pass, including error paths and exact error messages.
2. Preserving the symbol's public signature exactly: same name, same parameters, same defaults, same public methods/fields.
3. Keeping the code readable and idiomatic — NO minification tricks (no semicolon-chained lines, no one-letter names, no removing types).
4. Removing redundant guards, dead branches, duplicated blocks and verbose constructs replaceable by standard-library/idiomatic equivalents.
5. Calling the other symbols in the file map when that removes duplicated logic. Do NOT redefine them.
RESPONSE FORMAT: your ENTIRE response must be exactly one fenced code block containing ONLY the rewritten symbol — nothing else from the file, no other symbol, no prose. First characters: ``` + the language tag, last characters: ```."""


def _symbol_index(source: str, lang: str) -> list[tuple[int, int, str, str]]:
    """`(start, end_exclusive, name, key)` for every top-level symbol.

    Duplicate names (two `impl Foo` blocks, an overloaded `fn`) are given
    `name#2` keys so a symbol can still be re-resolved by identity after the
    file around it has shifted.
    """
    seen: dict[str, int] = {}
    out: list[tuple[int, int, str, str]] = []
    for start, end, name in sorted(symbol_spans(source, lang)):
        if not name or end <= start:
            continue
        seen[name] = seen.get(name, 0) + 1
        key = name if seen[name] == 1 else f"{name}#{seen[name]}"
        out.append((start, end, name, key))
    return out


_DECL_END = (":", "{", ";", "=")


def _declaration(lines: list[str], start: int, end: int) -> str:
    """The symbol's signature line(s), collapsed to one line."""
    parts: list[str] = []
    for line in lines[start : min(end, start + 4)]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*", "@", "#[")):
            if parts:
                break
            continue
        parts.append(stripped)
        if stripped.endswith(_DECL_END):
            break
    text = " ".join(parts)
    return text[:160]


def file_map(source: str, lang: str, exclude: str = "") -> str:
    """A compact signatures-only map of the rest of the file.

    The model needs to know what else exists (so it can call a sibling helper
    instead of duplicating it, and so it does not reinvent a name) without
    paying whole-file input tokens for it.
    """
    lines = source.splitlines()
    rows = []
    for start, end, _name, key in _symbol_index(source, lang):
        if key == exclude:
            continue
        rows.append(f"  {_declaration(lines, start, end)}   [{end - start} lines]")
    return "\n".join(rows)


def build_symbol_prompt(
    path: Path, lang: str, key: str, symbol: str, symbol_loc: int,
    fmap: str, feedback: str, spec: str = "",
) -> str:
    tag = {"python": "python", "javascript": "javascript", "typescript": "typescript", "rust": "rust"}[lang]
    fb = f"\nPrevious attempt feedback (fix this):\n{feedback}\n" if feedback else ""
    tests = (
        "\nThe test suite the whole file must pass (behavior specification — error "
        f"messages and exception types must match EXACTLY):\n```\n{spec}\n```\n"
        if spec else ""
    )
    rest = f"\nOther symbols in {path.name} (do NOT output these, they stay as they are):\n{fmap}\n" if fmap else ""
    return (
        f"File: {path.name}\nLanguage: {tag}\n"
        f"Symbol to rewrite: `{key}` ({symbol_loc} code lines)\n"
        f"{rest}{fb}{tests}"
        f"Rewrite ONLY the symbol `{key}` with fewer lines (aim for 20-50% fewer). "
        f"Output just that one symbol, complete and self-contained, in one code block.\n\n"
        f"```{tag}\n{symbol}\n```"
    )


def _is_budget_error(exc: Exception) -> bool:
    return "budget exhausted" in str(exc).lower()


def reduce_symbols(
    backend: Backend,
    root: Path,
    path: Path,
    lang: str,
    run_tests_fn,
    attempts_per_symbol: int = 2,
    min_symbol_loc: int = 6,
    spec: str = "",
    sweeps: int = 1,
    max_symbols: int | None = None,
) -> list[AttemptRecord]:
    """Rewrite one top-level symbol at a time, biggest first (roadmap C2).

    Each proposal is spliced into the file and put through the *same* verify
    gate as a whole-file rewrite — pre-gates first, so a bad bet costs
    milliseconds, and the test-result cache makes repeats free. Spans are
    re-resolved by key after every acceptance because line numbers shift as
    the file shrinks.
    """
    records: list[AttemptRecord] = []
    done: set[str] = set()
    budget_gone = False
    for _sweep in range(max(1, sweeps)):
        if budget_gone:
            break
        while not budget_gone:
            source = path.read_text(encoding="utf-8", errors="replace")
            index = [s for s in _symbol_index(source, lang) if s[3] not in done]
            if not index:
                break
            if max_symbols is not None and len(done) >= max_symbols:
                break
            start, end, name, key = max(index, key=lambda s: s[1] - s[0])
            done.add(key)
            lines = source.splitlines()
            symbol = "\n".join(lines[start:end])
            symbol_loc = measure(symbol, lang).code
            if symbol_loc < min_symbol_loc:
                continue
            api_before = EXTRACTORS[lang](source)
            full_loc = measure(source, lang).code
            fmap = file_map(source, lang, exclude=key)
            feedback = ""
            for attempt in range(1, attempts_per_symbol + 1):
                try:
                    response = backend.complete(
                        SYMBOL_SYSTEM_PROMPT,
                        build_symbol_prompt(
                            path, lang, key, symbol, symbol_loc, fmap, feedback, spec=spec
                        ),
                        temperature=0.1 if attempt == 1 else 0.5,
                    )
                except Exception as exc:
                    records.append(_report(AttemptRecord(
                        f"{path}:{key}", attempt, "backend-error", full_loc, full_loc, str(exc)[:120]
                    )))
                    budget_gone = _is_budget_error(exc)
                    break
                candidate = extract_code(response)
                if candidate is None:
                    feedback = "Return ONLY the rewritten symbol in one fenced code block."
                    records.append(_report(AttemptRecord(f"{path}:{key}", attempt, "no-code-block", full_loc, full_loc)))
                    continue
                # A small model often ignores "one symbol" and returns the
                # whole file. Splicing that in would duplicate every other
                # symbol — and the duplicate would still pass the suite,
                # because the later definition wins. So detect it and verify
                # it as the whole-file candidate it is instead of charging a
                # wasted call; and refuse any splice that duplicates a symbol.
                proposal = _as_whole_file(candidate, source, lang)
                if proposal is None:
                    proposal = "\n".join(lines[:start] + candidate.splitlines() + lines[end:])
                    dupes = _duplicate_keys(proposal, lang)
                    if dupes:
                        outcome, loc_after = f"duplicate-symbol: {sorted(dupes)}", full_loc
                        records.append(_report(AttemptRecord(
                            f"{path}:{key}", attempt, "duplicate-symbol", full_loc, full_loc, outcome
                        )))
                        feedback = (
                            f"Your reply redefined {sorted(dupes)}, which already exist "
                            f"elsewhere in the file. Output ONLY `{key}`."
                        )
                        continue
                outcome, loc_after = verify_candidate(
                    path, lang, proposal, api_before, full_loc, run_tests_fn, root
                )
                records.append(_report(AttemptRecord(
                    f"{path}:{key}", attempt, outcome.split(":")[0], full_loc, loc_after, outcome
                )))
                if outcome == "accepted":
                    path.write_text(proposal + "\n", encoding="utf-8")
                    break
                feedback = _feedback_for(outcome)
    return records


def _as_whole_file(candidate: str, source: str, lang: str) -> str | None:
    """`candidate` when it looks like the entire file rather than one symbol."""
    original_keys = {k for _s, _e, _n, k in _symbol_index(source, lang)}
    candidate_keys = {k for _s, _e, _n, k in _symbol_index(candidate, lang)}
    if len(candidate_keys) >= 2 and len(candidate_keys & original_keys) >= 2:
        return candidate
    return None


def _duplicate_keys(source: str, lang: str) -> set[str]:
    """Top-level names defined more than once (a splice gone wrong)."""
    names = [n for _s, _e, n, _k in _symbol_index(source, lang)]
    return {n for n in names if names.count(n) > 1}


# ---- legacy python-only entry points (kept: the pipeline's optional sweeps) --


def _top_level_spans(source: str) -> list[tuple[int, int, str, str]]:
    """(start_line, end_line, name, kind) for top-level defs/classes (0-based lines)."""
    tree = ast.parse(source)
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            spans.append((node.lineno - 1, node.end_lineno - 1, node.name, kind))
    return spans


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
        loc_now = measure(current, "python").code
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
