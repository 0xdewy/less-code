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
from dataclasses import dataclass
from pathlib import Path

from .api_check import EXTRACTORS, api_feedback, api_violations
from .backends import Backend
from .hunks import decompose, search, symbol_spans
from .loc import measure
import contextlib

PROGRESS = None  # set by CLI to a printer for live per-attempt output

#: chars of test-spec per prompt: enough for several focused test files
SPEC_BUDGET = 12_000

#: test material for spec selection: (identity, source) per test file
SpecTests = list[tuple[str, str]]


def select_spec(tests: SpecTests, tokens, budget: int = SPEC_BUDGET) -> str:
    """The tests that pin THIS unit — not a global prefix of the suite.

    A repo-scale suite (click: ~50 files, ~25k LOC) cannot fit in a prompt,
    and truncating it to one 40k-char prefix feeds the model tests that have
    nothing to do with the symbol being rewritten. Rank instead: test files
    mentioning more of the unit's tokens (word-boundary match) come first,
    then conftest.py (fixture context), then the rest in name order until the
    budget is full. Whole files are kept while they fit so a test is never
    silently cut mid-assertion when there is room to keep it whole.
    """
    patterns = [
        re.compile(rf"\b{re.escape(t)}\b")
        for t in sorted({t for t in tokens if t})
    ]

    def hits(text: str) -> int:
        return sum(1 for p in patterns if p.search(text))

    ranked = sorted(
        tests,
        key=lambda kv: (-hits(kv[1]), 0 if Path(kv[0]).name == "conftest.py" else 1, kv[0]),
    )
    parts: list[str] = []
    used = 0
    for _name, text in ranked:
        if used >= budget:
            break
        remaining = budget - used
        if len(text) <= remaining:
            take = text
        elif remaining >= 400:
            take = text[:remaining]
        else:
            break
        parts.append(take)
        used += len(take) + 2
    return "\n\n".join(parts)


def _check_lang(lang, feedback, spec, message):
    tag = {'python': 'python', 'javascript': 'javascript', 'typescript': 'typescript', 'rust': 'rust'}[lang]
    fb = f'\nPrevious attempt feedback (fix this):\n{feedback}\n' if feedback else ''
    tests = f'{message}{spec}\n```\n' if spec else ''
    return (tag, fb, tests)

def _report(record):
    """Emit one AttemptRecord live (the CLI/pipeline sets `PROGRESS`).

    Records used to be printed only after a whole file finished, so a
    multi-hour GPU run showed nothing until it was over.
    """
    if PROGRESS:
        with contextlib.suppress(Exception):
            PROGRESS(record)
    return record


SYSTEM_PROMPT = """You are an expert code minimizer. Rewrite code to use fewer lines while:
1. Preserving behavior EXACTLY — the test suite (provided) must still pass, including error paths.
2. Preserving the public API exactly: same public symbols, same names, same signatures. If you create helper functions, their names MUST start with underscore (_helper).
3. Keeping the code readable and idiomatic — NO minification tricks (no semicolon-chained lines, no one-letter names, no removing types).
4. Preserving every docstring and doc comment VERBATIM — they are the project's published documentation, not code to minimize.
5. Removing: redundant guards, dead branches, over-abstraction, duplicate logic (merge into _helpers), verbose constructs replaceable by standard-library/idiomatic equivalents.
6. Consolidating copy-pasted blocks into parameterized _helpers when it genuinely reduces total lines.
RESPONSE FORMAT: your ENTIRE response must be exactly one fenced code block containing
the complete rewritten code — first characters: ``` + the language tag, last
characters: ```. No prose before or after. Never describe or summarize the
code; OUTPUT the rewritten code itself."""


def build_prompt(path: Path, source: str, lang: str, loc: int, feedback: str, spec: str = "", focus: str = "") -> str:
    tag, fb, tests = _check_lang(lang, feedback, spec, '\nThe test suite this code must pass (behavior specification — error messages and exception types must match EXACTLY):\n```python\n')
    fo = f"\nFOCUS: {focus}\n" if focus else ""
    return (
        f"File: {path.name} ({loc} code lines)\n"
        f"Language: {tag}\n{fo}{fb}{tests}"
        f"Rewrite the code with significantly fewer lines (aim for 20-50% fewer), "
        f"following the system rules. Return the complete rewritten code only.\n\n"
        f"```{tag}\n{source}\n```"
    )




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

# cargo prints these after the real diagnostics; they name no code
_RUST_NOISE = (
    "error: aborting",
    "error: could not compile",
    "For more information about this error",
)

COMPILER_CHARS = 600


def compiler_errors(text: str, lang: str, max_errors: int = 3, max_chars: int = COMPILER_CHARS) -> str:
    """The compiler's own words, kept whole enough to be actionable.

    Iteration 09 measured that 100% of the rust duplicate-group proposals died
    at `cargo check` — and the model was never told why, because the outcome
    string carried one truncated line of `first_failure()`, an extractor built
    for pytest tails. A `-->` location and an `expected ... found ...` note are
    exactly what turns "it did not compile" into a fixable instruction, so a
    diagnostic is kept as a *block*: the `error[...]` line plus its location
    and notes, for the first few errors.
    """
    lines = text.splitlines()
    if lang == "rust":
        blocks: list[str] = []
        i = 0
        while i < len(lines) and len(blocks) < max_errors:
            line = lines[i]
            if line.startswith("error") and not line.startswith(_RUST_NOISE):
                block = [line.rstrip()]
                j = i + 1
                while j < len(lines) and len(block) < 6:
                    nxt = lines[j]
                    if nxt.startswith(("error", "warning")):
                        break
                    if nxt.strip():
                        block.append(nxt.rstrip())
                    j += 1
                blocks.append("\n".join(block))
                i = j
                continue
            i += 1
        joined = "\n".join(blocks)
        return joined[:max_chars] if joined else first_failure(text)[:max_chars]
    # node --check: `path:line`, the offending source, a caret run, then the
    # `SyntaxError:` line — the stack frames below it are about node, not the code
    kept = [l.rstrip() for l in lines if l.strip() and not l.lstrip().startswith("at ")]
    marker = next(
        (i for i, l in enumerate(kept) if "Error" in l or l.lstrip().startswith("error")), None
    )
    if marker is None:
        return first_failure(text)[:max_chars]
    return "\n".join(kept[max(0, marker - 3) : marker + 2])[:max_chars]


FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)


def _feedback_for(outcome: str) -> str:
    """The rejection turned into instructions for the next attempt.

    Test failures were always fed back verbatim; an `api-changed` rejection
    used to feed back the bare word, which told the model nothing. Now the
    concrete missing/changed/added symbol lists go into the prompt.
    """
    if outcome.startswith("api-changed:"):
        return api_feedback([v.strip() for v in outcome[len("api-changed:"):].split(" | ") if v.strip()])
    if outcome.startswith("docs-lost:"):
        return (
            "Your previous reply DELETED docstrings or doc comments. They are the "
            "project's published documentation and must be preserved verbatim "
            "(an enum member's docstring documents that member, a `#:` comment "
            "feeds the docs site). Rewrite the CODE under and around them; "
            "keep every docstring and doc comment exactly as it is."
        )
    if outcome.startswith("syntax-error:"):
        # the compiler's own diagnostics, framed as an instruction
        return (
            "Your previous reply DID NOT COMPILE. The compiler said:\n"
            + outcome[len("syntax-error:"):].strip()
            + "\nFix exactly these errors. Do not change any public signature."
        )
    return outcome


@dataclass
class AttemptRecord:
    file: str
    attempt: int
    outcome: str
    loc_before: int
    loc_after: int
    detail: str = ""


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
    stripped.lstrip()[:12]
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
    return compiler_errors(proc.stdout + proc.stderr, lang)


# rejection stages, cheapest first: most candidates die in milliseconds
STAGES = ("not-smaller", "syntax-error", "api-changed", "tests-failed")


def _docstrings(source: str, lang: str) -> list[str]:
    """The documentation a rewrite must preserve: python docstrings (module,
    class, function and enum-member level — click renders them on its docs
    site) and `#` comments (incl. `#:` Sphinx attribute docs and `# type:`
    directives — an accepted click rewrite once ate a multi-line comment
    citing the issue it fixed); js/ts `/** */` doc blocks; rust `///`, `//!`.
    None of it counts as code-LOC, so the size gate ignores it all — without
    this check a 7B model 'reduces' a mature library by deleting its
    published documentation wholesale."""
    if lang == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        documentable = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        docs = [
            doc
            for node in ast.walk(tree)
            if isinstance(node, documentable)
            and (doc := ast.get_docstring(node, clean=False))
        ]
        return docs + [
            line.strip()
            for line in source.splitlines()
            if line.lstrip().startswith("#")
        ]
    if lang in ("javascript", "typescript"):
        return re.findall(r"/\*\*(.*?)\*/", source, re.DOTALL)
    return re.findall(r"(?m)^\s*//[/!].*$", source)


def docs_lost(before: str, after: str, lang: str) -> list[str]:
    """Docstrings/comments present in `before` and gone from `after`."""
    from collections import Counter

    lost = Counter(_docstrings(before, lang))
    lost.subtract(Counter(_docstrings(after, lang)))
    return sorted(doc for doc, count in lost.items() if count > 0)


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

    Order is not-smaller -> docs -> parse -> API -> tests (roadmap B3): the
    size and doc checks need no disk write at all and the parse gate costs
    milliseconds, so a full suite run is only ever spent on a candidate that
    could plausibly pass.
    """
    loc_after = measure(candidate, lang).code
    if loc_after >= loc_before:
        return "not-smaller", loc_after
    original = path.read_text(encoding="utf-8", errors="replace")
    lost = docs_lost(original, candidate, lang)
    if lost:
        return f"docs-lost: {len(lost)} docstring(s)/doc-comment(s) removed", loc_after
    try:
        path.write_text(candidate + "\n", encoding="utf-8")
        syntax = syntax_check(path, lang, root)
        if syntax:
            return f"syntax-error: {syntax[:COMPILER_CHARS]}", loc_after
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


def verify_multifile(
    candidates: dict[Path, str],
    lang: str,
    run_tests_fn,
    root: Path,
) -> tuple[str, int, int]:
    """The same gate over SEVERAL files at once. Returns (outcome, before, after).

    A duplicate-group merge touches every member's file plus wherever the new
    `_helper` lands, so the single-file `verify_candidate` cannot express it.
    The contract is identical — not-smaller -> parse -> API -> tests, and every
    touched file is snapshotted and restored no matter how the check exits.
    """
    originals = {p: p.read_text(encoding="utf-8", errors="replace") for p in candidates}
    loc_before = sum(measure(t, lang).code for t in originals.values())
    loc_after = sum(measure(t, lang).code for t in candidates.values())
    if loc_after >= loc_before:
        return "not-smaller", loc_before, loc_after
    for path, text in candidates.items():
        lost = docs_lost(originals[path], text, lang)
        if lost:
            return (
                f"docs-lost: {len(lost)} docstring(s)/doc-comment(s) removed in {path.name}",
                loc_before, loc_after,
            )
    try:
        for path, text in candidates.items():
            path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        checked: set[str] = set()
        for path in candidates:
            # rust's check is a whole-crate `cargo check`; running it per file
            # would pay the compile cost N times for the same answer
            marker = lang if lang == "rust" else str(path)
            if marker in checked:
                continue
            checked.add(marker)
            syntax = syntax_check(path, lang, root)
            if syntax:
                return f"syntax-error: {syntax[:COMPILER_CHARS]}", loc_before, loc_after
        before_api = {str(p): EXTRACTORS[lang](t) for p, t in originals.items()}
        after_api = {str(p): EXTRACTORS[lang](t) for p, t in candidates.items()}
        violations = api_violations(before_api, after_api)
        if violations:
            return "api-changed: " + " | ".join(violations), loc_before, loc_after
        result = run_tests_fn(root, lang)
        if not result.ok:
            return f"tests-failed: {first_failure(result.output_tail)}", loc_before, loc_after
        return "accepted", loc_before, loc_after
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")


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
    spec_tests: SpecTests | None = None,
) -> tuple[str, int, list[AttemptRecord]]:
    """Greedy multi-attempt reduction of one file. Returns (best_source, loc, records).

    A rejected whole-file rewrite is not thrown away: it is decomposed into
    symbol-aligned hunks and the largest passing subset is accepted (C1).

    With `spec_tests` the behavior spec is selected for this file (tests
    mentioning any of its top-level symbols or its module name).
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    if spec_tests is not None:
        spec = select_spec(spec_tests, [*EXTRACTORS[lang](source), path.stem])
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
            records.append(_report(AttemptRecord(str(path), attempt, _exc_outcome(exc), best_loc, best_loc, str(exc)[:120])))
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
        if not decompose_rejected or not outcome.startswith(
            ("tests-failed", "api-changed", "syntax-error")
        ):
            # syntax-error included deliberately: a rewrite that does not
            # compile as a whole usually contains individually valid symbol
            # rewrites, and the parse/check pre-gate rejects the bad hunks
            # in milliseconds (measured: every rust whole-file 7B reject in
            # iterations 09-10 was a syntax-error carrying big reductions).
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
4. Preserving every docstring and doc comment VERBATIM (the symbol's own, its methods' and enum members') — they are published documentation, not code to minimize.
5. Removing redundant guards, dead branches, duplicated blocks and verbose constructs replaceable by standard-library/idiomatic equivalents.
6. Calling the other symbols in the file map when that removes duplicated logic. Do NOT redefine them.
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


# Iteration 08's clearest miss: on the py fixture "one top-level symbol" is a
# 186-line class, i.e. the same all-or-nothing gamble C2 exists to abolish.
# js and rust units average 12-20 lines and moved; python did not. So a class
# over this many code lines is decomposed into its methods, and each method is
# proposed on its own — spliced over its own ast line span, through the same
# gate. The class docstring and its class-level attributes are never inside a
# method span, so they are untouched by construction.
CLASS_METHOD_THRESHOLD = 40


def proposal_units(
    source: str,
    lang: str,
    class_threshold: int = CLASS_METHOD_THRESHOLD,
    force: frozenset[str] = frozenset(),
) -> list[tuple[int, int, str, str]]:
    """`(start, end_exclusive, name, key)` for every unit worth proposing.

    Top-level symbols, except that a big python class becomes its methods
    (`Class.method` keys). `class_threshold <= 0` decomposes every class, and
    `force` names classes to decompose whatever their size — the loop uses it
    so a class does not re-consolidate into one all-or-nothing unit the moment
    an accepted method drops it under the threshold.
    """
    top = _symbol_index(source, lang)
    if lang != "python":
        return top
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return top
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    lines = source.splitlines()
    units: list[tuple[int, int, str, str]] = []
    for start, end, name, key in top:
        node = classes.get(name)
        methods = [
            c for c in (node.body if node else [])
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        cls_loc = measure("\n".join(lines[start:end]), "python").code
        if node is None or key != name or len(methods) < 2 or (
            cls_loc <= class_threshold and name not in force
        ):
            units.append((start, end, name, key))
            continue
        for method in methods:
            m_start = min([method.lineno] + [d.lineno for d in method.decorator_list]) - 1
            units.append((m_start, method.end_lineno, method.name, f"{name}.{method.name}"))
    return units


def class_context(source: str, class_name: str, exclude: str) -> str:
    """The sibling context a single method rewrite needs to be correct.

    The other methods' signatures (so it can call them instead of pasting
    their bodies) and `__init__`'s **body** (so it knows which attributes
    exist). Bodies of everything else are omitted — they are what the pass is
    trying not to pay tokens for.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    node = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None
    )
    if node is None:
        return ""
    lines = source.splitlines()
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    out = [f"class {class_name}({bases}):" if bases else f"class {class_name}:"]
    for child in node.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            out.append("    " + ast.unparse(child))
    for child in node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if child.name == exclude:
            continue
        start = min([child.lineno] + [d.lineno for d in child.decorator_list]) - 1
        if child.name == "__init__":
            out += lines[start:child.end_lineno]
            continue
        head = _declaration(lines, start, child.end_lineno)
        out.append(f"    {head} ...   [{child.end_lineno - start} lines]")
    return "\n".join(out)


def _reindent(text: str, indent: int) -> str:
    """Shift a reply so its first line sits at `indent` columns.

    A model asked for `Inventory.receive` answers with `def receive(...)` at
    column 0 about half the time and at column 4 the rest; the splice has to
    work either way.
    """
    lines = text.splitlines()
    first = next((l for l in lines if l.strip()), "")
    delta = indent - (len(first) - len(first.lstrip()))
    if delta == 0:
        return text
    if delta > 0:
        pad = " " * delta
        return "\n".join(pad + l if l.strip() else l for l in lines)
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        strip = len(line) - len(line.lstrip())
        out.append(line[min(strip, -delta):])
    return "\n".join(out)


def _method_reply_ok(candidate: str, method: str) -> str:
    """'' when the reply is a set of function defs containing `method`.

    Guards the class-shaped reply: a model handed one method often answers
    with the whole class, and splicing that inside the class body nests a
    class where a method used to be.
    """
    try:
        tree = ast.parse(_reindent(candidate, 0))
    except SyntaxError:
        return ""  # let the syntax pre-gate report it with a real message
    names = [
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(tree.body) != len(names):
        return f"reply must contain only `def` blocks, not {[type(n).__name__ for n in tree.body]}"
    if method not in names:
        return f"reply does not define `{method}` (it defines {names})"
    return ""


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
    fmap: str, feedback: str, spec: str = "", owner: str = "",
) -> str:
    tag, fb, tests = _check_lang(lang, feedback, spec, '\nThe test suite the whole file must pass (behavior specification — error messages and exception types must match EXACTLY):\n```\n')
    example = ""
    if lang == "python":
        # few-shot from an ACCEPTED click rewrite (C6 lite): the shape a
        # correct answer has — signature and docstring intact, one structural
        # win, nothing else touched. Instructions did not teach this (40% of
        # click proposals were doc-eating anyway); an example does better.
        example = (
            "\nEXAMPLE of a correct rewrite (this exact shape was accepted):\n"
            "```\n"
            "before:\n"
            "    def get_help_extra(self):\n"
            '        """Extra metrics for the help."""\n'
            "        if self.show_default is None:\n"
            "            return None\n"
            "        else:\n"
            '            return {"default": self.show_default}\n'
            "after:\n"
            "    def get_help_extra(self):\n"
            '        """Extra metrics for the help."""\n'
            "        if self.show_default is None:\n"
            "            return None\n"
            '        return {"default": self.show_default}\n'
            "```\n"
        )
    if owner:
        rest = (
            f"\nThe class `{owner}` it belongs to — other method signatures and the "
            f"full `__init__` so you know which attributes exist (do NOT output any "
            f"of this, and do NOT output the `class` line):\n```{tag}\n{fmap}\n```\n"
        ) if fmap else ""
        instruction = (
            f"Rewrite ONLY the method `{key.split('.')[-1]}` with fewer lines (aim for "
            f"20-50% fewer, so at most {max(1, symbol_loc - 1)} code lines). Output just "
            f"that one `def` block at column 0, complete, in one code block. You may "
            f"call the class's other methods instead of repeating their bodies."
        )
    else:
        rest = f"\nOther symbols in {path.name} (do NOT output these, they stay as they are):\n{fmap}\n" if fmap else ""
        instruction = (
            f"Rewrite ONLY the symbol `{key}` with fewer lines (aim for 20-50% fewer, "
            f"so at most {max(1, symbol_loc - 1)} code lines). Output just that one "
            f"symbol, complete and self-contained, in one code block."
        )
    return (
        f"File: {path.name}\nLanguage: {tag}\n"
        f"{'Method' if owner else 'Symbol'} to rewrite: `{key}` ({symbol_loc} code lines)\n"
        f"{rest}{fb}{tests}{example}{instruction}\n\n"
        f"```{tag}\n{symbol}\n```"
    )


def _is_budget_error(exc: Exception) -> bool:
    return "budget exhausted" in str(exc).lower()


def _exc_outcome(exc: Exception) -> str:
    """A budget stop is the operator's own cap, not a backend fault — report
    it as such instead of inflating backend-error counts."""
    return "budget-exhausted" if _is_budget_error(exc) else "backend-error"


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
    spec_tests: SpecTests | None = None,
    exclude: set[str] | None = None,
) -> list[AttemptRecord]:
    """Rewrite one top-level symbol at a time, biggest first (roadmap C2).

    Each proposal is spliced into the file and put through the *same* verify
    gate as a whole-file rewrite — pre-gates first, so a bad bet costs
    milliseconds, and the test-result cache makes repeats free. Spans are
    re-resolved by key after every acceptance because line numbers shift as
    the file shrinks.

    With `spec_tests` the behavior spec is selected per symbol (the tests
    that mention it), instead of one flat `spec` string for every prompt.
    """
    records: list[AttemptRecord] = []
    done: set[str] = set()
    decomposed: set[str] = set()
    budget_gone = False
    for _sweep in range(max(1, sweeps)):
        if budget_gone:
            break
        while not budget_gone:
            source = path.read_text(encoding="utf-8", errors="replace")
            index = [
                s for s in proposal_units(source, lang, force=frozenset(decomposed))
                if s[3] not in done and (not exclude or s[3] not in exclude)
            ]
            if not index:
                break
            if max_symbols is not None and len(done) >= max_symbols:
                break
            start, end, name, key = max(index, key=lambda s: s[1] - s[0])
            done.add(key)
            if "." in key:
                decomposed.add(key.split(".")[0])
            lines = source.splitlines()
            symbol = "\n".join(lines[start:end])
            symbol_loc = measure(symbol, lang).code
            if symbol_loc < min_symbol_loc:
                continue
            api_before = EXTRACTORS[lang](source)
            full_loc = measure(source, lang).code
            owner = key.rsplit(".", 1)[0] if "." in key and lang == "python" else ""
            symbol_spec = (
                select_spec(spec_tests, (key, name, owner, path.stem))
                if spec_tests is not None else spec
            )
            indent = len(lines[start]) - len(lines[start].lstrip()) if owner else 0
            fmap = (
                class_context(source, owner, name) if owner
                else file_map(source, lang, exclude=key)
            )
            feedback = ""
            for attempt in range(1, attempts_per_symbol + 1):
                try:
                    response = backend.complete(
                        SYMBOL_SYSTEM_PROMPT,
                        build_symbol_prompt(
                            path, lang, key, symbol, symbol_loc, fmap, feedback,
                            spec=symbol_spec, owner=owner,
                        ),
                        temperature=0.1 if attempt == 1 else 0.5,
                    )
                except Exception as exc:
                    records.append(_report(AttemptRecord(
                        f"{path}:{key}", attempt, _exc_outcome(exc), full_loc, full_loc, str(exc)[:120]
                    )))
                    budget_gone = _is_budget_error(exc)
                    break
                candidate = extract_code(response)
                if candidate is not None and not candidate.strip():
                    candidate = None  # an empty block would splice the symbol away
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
                if proposal is not None and owner:
                    proposal = None  # a method reply is never the whole file
                if proposal is None and owner:
                    problem = _method_reply_ok(candidate, name)
                    if problem:
                        records.append(_report(AttemptRecord(
                            f"{path}:{key}", attempt, "bad-method-reply", full_loc, full_loc, problem
                        )))
                        feedback = (
                            f"{problem}. Output ONLY the method `{name}`, starting with "
                            f"`def {name}(`, and nothing else — no class statement, no "
                            f"other methods."
                        )
                        continue
                    candidate = _reindent(candidate, indent)
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


# ---- C2b: duplicate-group proposals ---------------------------------------
#
# The granularity neither whole-file nor per-symbol can express. Both fixtures'
# FIXTURE.md name cross-symbol copy-paste as their largest embedded
# opportunity: python's SKU validation pasted 6x across methods of two classes,
# rust's csv_escape_row/_owned/_rows and count_leading/trailing_spaces, js's
# buildMonthly/Quarterly/RegionSummary. A single-symbol proposal cannot see the
# twin; a whole-file proposal can, but is one bet a small model cannot land.
# `dedup.find_duplicate_groups` finds the groups, and each group becomes ONE
# multi-file candidate: a shared `_helper` plus a minimal rewrite of every
# member, through `verify_multifile` — the same gate, several files at once.


DEDUP_SYSTEM_PROMPT = """You are an expert code minimizer. You are given a group of near-duplicate functions/methods from one project, plus the test suite they must pass.
Produce ONE shared helper and rewrite every member to call it:
1. The helper's name MUST start with an underscore (`_shared_helper`). It is new private code.
2. Every member keeps its EXACT public signature — same name, same parameters, same defaults, same return type. Only its body changes, and it should become a short call to the helper.
3. Behavior must be preserved EXACTLY, including error paths, exact error messages and exception/panic types.
4. The total number of lines must go DOWN: helper + all rewritten members must be clearly fewer lines than the originals.
5. No minification tricks, no one-letter names, no removed types.
6. Exact error/panic messages and exact public signatures are PINNED BY THE TESTS. Copy every string literal character for character; never reword, never merge two different messages into one.
7. You are given a computed token diff listing everything that differs between the members. That list is complete. Parameterize exactly those things and nothing else — the rest of the body is already identical and must be copied verbatim.
8. Every member keeps its docstring/doc comments VERBATIM — they are published documentation, not code to minimize.
RESPONSE FORMAT: your ENTIRE response must be exactly one fenced code block containing, in this order: the helper, then EVERY member rewritten, in the SAME order they were given, each as a complete top-level definition at column 0. Nothing else — no prose, no other code, no class statements."""


def build_dedup_prompt(group, lang: str, feedback: str = "", spec: str = "") -> str:
    tag, fb, tests = _check_lang(lang, feedback, spec, '\nThe test suite the project must still pass (behavior specification — error messages and exception types must match EXACTLY):\n```\n')
    from .dedup import token_diff_slots

    slots = token_diff_slots(group, lang)
    if slots and len(group.members) == 2:
        a, b = (m.key for m in group.members)
        rows = "\n".join(
            f"  {i}. in `{a}`: {left}\n     in `{b}`: {right}"
            for i, (left, right) in enumerate(slots, 1)
        )
        diff = (
            f"\nCOMPUTED TOKEN DIFF — this is the complete list of what differs "
            f"between the two definitions ({len(slots)} item(s)). Everything else is "
            f"already identical:\n{rows}\n"
            f"So the helper needs at most {len(slots)} parameter(s) (or generic "
            f"type parameter(s)), one per item above. Write the helper by copying "
            f"`{a}` verbatim and replacing only those {len(slots)} item(s); then make "
            f"both members one-line calls to it.\n"
        )
    elif slots:
        rows = "\n".join(f"  {i}. {' | '.join(vals)}" for i, vals in enumerate(slots, 1))
        diff = (
            f"\nCOMPUTED TOKEN DIFF against member 1 — the complete list of what "
            f"differs:\n{rows}\nParameterize exactly these and nothing else.\n"
        )
    else:
        diff = ""
    blocks = []
    for i, member in enumerate(group.members, 1):
        note = ""
        if lang == "python" and "." in member.key:
            note = (
                f" — a method of `{member.key.split('.')[0]}`; output it as a plain "
                f"`def` at column 0 and it will be put back in the class"
            )
        blocks.append(
            f"member {i}: `{member.key}` in {member.path.name} "
            f"({member.loc} code lines){note}\n```{tag}\n{member.text}\n```"
        )
    total = group.loc
    return (
        f"Language: {tag}\n"
        f"{len(group.members)} near-duplicate definitions (token similarity "
        f"{group.similarity:.2f}), {total} code lines in total:\n\n"
        + "\n\n".join(blocks)
        + f"\n{diff}{fb}{tests}"
        f"Write ONE private helper that captures what they share, then rewrite all "
        f"{len(group.members)} members to call it. Output the helper first, then the "
        f"{len(group.members)} members in the order above, all at column 0, in a single "
        f"code block. The total must be well under {total} code lines.\n"
    )


def _split_dedup_reply(candidate: str, group, lang: str):
    """`(helpers, [rewritten member text…])` or `(None, problem)`."""
    units = _symbol_index(candidate, lang)
    if not units:
        return None, "the reply contained no complete definitions"
    lines = candidate.splitlines()
    names = [m.name for m in group.members]
    matched = [u for u in units if u[2] in names]
    helpers = [u for u in units if u[2] not in names]
    public = [u[2] for u in helpers if not u[2].startswith("_")]
    if public:
        return None, f"the new helper(s) {public} must be private: rename with a leading underscore"
    if len(matched) != len(group.members):
        return None, (
            f"the reply must contain all {len(group.members)} members "
            f"({names}); it defined {[u[2] for u in units]}"
        )
    return (
        [(u[2], "\n".join(lines[u[0]:u[1]])) for u in helpers],
        [(u[2], "\n".join(lines[u[0]:u[1]])) for u in matched],
    )


def _dedup_candidate(candidate: str, group, lang: str, sources: dict[Path, str]):
    """Turn one reply into `{path: new_text}`, or `(None, problem)`.

    Members are spliced over their own spans (re-indented for python methods);
    the helper is appended to every file that has a member, so no cross-file
    import is invented and nothing outside the touched spans moves. A helper
    duplicated across two files costs its own lines — the size gate decides
    whether the merge still pays.
    """
    helpers, members = _split_dedup_reply(candidate, group, lang)
    if helpers is None:
        return None, members
    by_file: dict[Path, list] = {}
    for member, (_name, text) in zip(group.members, members):
        by_file.setdefault(member.path, []).append((member, text))
    out: dict[Path, str] = {}
    helper_text = "\n\n".join(_reindent(t, 0) for _n, t in helpers)
    for path, edits in by_file.items():
        file_lines = sources[path].splitlines()
        for member, text in sorted(edits, key=lambda e: -e[0].start):
            body = _reindent(text, member.indent).splitlines()
            file_lines[member.start:member.end] = body
        text = "\n".join(file_lines).rstrip("\n")
        if helper_text:
            text += "\n\n\n" + helper_text
        out[path] = text + "\n"
    return out, ""


def reduce_duplicate_groups(
    backend,
    root: Path,
    files: list[Path],
    lang: str,
    run_tests_fn,
    attempts_per_group: int = 2,
    spec: str = "",
    max_groups: int = 3,
    min_group_loc: int = 8,
    threshold: float = 0.6,
    spec_tests: SpecTests | None = None,
) -> list[AttemptRecord]:
    """Propose one shared `_helper` per near-duplicate group (C2b).

    `backend=None` is the mechanical-only mode: the deterministic merge runs
    (zero LLM cost, suite-gated), the model is never asked — static-only
    runs still get dedup yield."""
    from .dedup import find_duplicate_groups, mechanical_merge

    records: list[AttemptRecord] = []
    attempted: set[frozenset] = set()
    for _round in range(max_groups):
        sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
        groups = [
            g for g in find_duplicate_groups(sources, lang, threshold=threshold)
            if g.loc >= min_group_loc
            and frozenset(m.label for m in g.members) not in attempted
        ]
        if not groups:
            break
        group = groups[0]
        attempted.add(frozenset(m.label for m in group.members))
        tag = "dedup:" + "+".join(m.key for m in group.members)[:80]
        group_spec = (
            select_spec(
                spec_tests,
                [m.key for m in group.members] + [Path(m.path).stem for m in group.members],
            )
            if spec_tests is not None else spec
        )
        loc_before = sum(measure(sources[p], lang).code for p in group.files)
        # the merge of two copy-pasted definitions is a computed fact, not a
        # design task: try the mechanical builder FIRST (zero LLM cost, body
        # kept verbatim); the model is the fallback for what it cannot prove
        mech = mechanical_merge(group)
        if mech is not None:
            proposal_files, mech_note = mech
            outcome, _before, _after = verify_multifile(
                proposal_files, lang, run_tests_fn, root
            )
            records.append(_report(AttemptRecord(
                tag, 0, outcome.split(":")[0], loc_before, _after,
                mech_note + " -> " + outcome[:80],
            )))
            if outcome == "accepted":
                for path, text in proposal_files.items():
                    path.write_text(text, encoding="utf-8")
                continue
        feedback = ""
        if backend is None:
            continue  # mechanical-only mode: the model is never asked
        for attempt in range(1, attempts_per_group + 1):
            try:
                response = backend.complete(
                    DEDUP_SYSTEM_PROMPT,
                    build_dedup_prompt(group, lang, feedback, spec=group_spec),
                    temperature=0.1 if attempt == 1 else 0.5,
                )
            except Exception as exc:
                records.append(_report(AttemptRecord(
                    tag, attempt, _exc_outcome(exc), loc_before, loc_before, str(exc)[:120]
                )))
                if _is_budget_error(exc):
                    return records
                break
            candidate = extract_code(response)
            if candidate is None:
                feedback = "Return ONLY the helper and the rewritten members in one fenced code block."
                records.append(_report(AttemptRecord(tag, attempt, "no-code-block", loc_before, loc_before)))
                continue
            proposal, problem = _dedup_candidate(candidate, group, lang, sources)
            if proposal is None:
                records.append(_report(AttemptRecord(
                    tag, attempt, "bad-dedup-reply", loc_before, loc_before, problem
                )))
                feedback = problem
                continue
            outcome, before, after = verify_multifile(proposal, lang, run_tests_fn, root)
            records.append(_report(AttemptRecord(
                tag, attempt, outcome.split(":")[0], before, after, outcome
            )))
            if outcome == "accepted":
                for path, text in proposal.items():
                    path.write_text(text, encoding="utf-8")
                break
            feedback = _feedback_for(outcome)
    return records


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
