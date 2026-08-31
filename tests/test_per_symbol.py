"""C2 — per-symbol proposals as the default L2 strategy, and the API-violation
feedback loop (iteration 07 gap items 1 and 2).

Iteration 07 measured zero outright whole-file acceptances at 3B *and* 7B:
every accepted line came from salvaging a rejected whole-file rewrite. These
tests pin the replacement contract — one symbol per proposal, spliced through
the same verify gate — with scripted backends, so it is checked without a GPU.
"""

import pathlib
import re
import textwrap

import pytest

from less_code.api_check import api_feedback, api_violations
from less_code.llm_reduce import (
    _as_whole_file,
    _duplicate_keys,
    _feedback_for,
    _symbol_index,
    file_map,
    reduce_symbols,
)
from less_code.backends import Backend
from less_code.pipeline import reduce_project
from less_code.testrunners import run_tests

MODULE = textwrap.dedent('''
    def sign(n):
        if n > 0:
            result = "positive"
        elif n < 0:
            result = "negative"
        else:
            result = "zero"
        return result


    def clamp(v, lo, hi):
        if v < lo:
            return lo
        elif v > hi:
            return hi
        else:
            return v
''').lstrip()

TESTS = textwrap.dedent('''
    from mod import sign, clamp

    def test_sign():
        assert sign(5) == "positive"
        assert sign(-3) == "negative"
        assert sign(0) == "zero"

    def test_clamp():
        assert clamp(1, 2, 8) == 2
        assert clamp(5, 2, 8) == 5
        assert clamp(9, 2, 8) == 8
''').lstrip()

SMALL_SIGN = 'def sign(n):\n    return "positive" if n > 0 else "negative" if n < 0 else "zero"\n'
SMALL_CLAMP = "def clamp(v, lo, hi):\n    return max(lo, min(v, hi))\n"


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(MODULE)
    (root / "test_mod.py").write_text(TESTS)
    return root


def _asked_for(prompt: str) -> str:
    m = re.search(r"Symbol to rewrite: `([^`]+)`", prompt)
    return m.group(1) if m else ""


class PerSymbolBackend(Backend):
    """Answers with exactly the one symbol it was asked for."""

    def __init__(self, answers: dict[str, str]):
        super().__init__("per-symbol")
        self.answers = answers
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, system, prompt, temperature=0.2):
        self.calls += 1
        self.prompts.append(prompt)
        body = self.answers.get(_asked_for(prompt), "")
        return f"```python\n{body}```"


# ---- symbol index / file map ----------------------------------------------


def test_symbol_index_python():
    assert [k for _s, _e, _n, k in _symbol_index(MODULE, "python")] == ["sign", "clamp"]


def test_symbol_index_javascript_uses_the_brace_matcher():
    src = (
        "export function a(x) {\n  if (x) {\n    return 1;\n  }\n  return 0;\n}\n"
        "class B {\n  m() { return 2; }\n}\n"
    )
    assert [k for _s, _e, _n, k in _symbol_index(src, "javascript")] == ["a", "B"]


def test_symbol_index_rust_uses_the_brace_matcher():
    src = (
        "pub fn a(x: i32) -> [u8; 2] {\n    [0, 0]\n}\n"
        "pub struct S;\n"
        "impl S {\n    pub fn m(&self) -> i32 { 1 }\n}\n"
    )
    keys = [k for _s, _e, _n, k in _symbol_index(src, "rust")]
    assert keys[:2] == ["a", "S"]


def test_duplicate_names_get_distinct_keys():
    src = "impl A {\n    fn x(&self) {}\n}\nimpl A {\n    fn y(&self) {}\n}\n"
    assert [k for _s, _e, _n, k in _symbol_index(src, "rust")] == ["A", "A#2"]


def test_file_map_is_signatures_only_and_excludes_the_focus_symbol():
    fmap = file_map(MODULE, "python", exclude="sign")
    assert "def clamp(v, lo, hi):" in fmap
    assert "def sign" not in fmap
    assert "positive" not in fmap  # bodies are not in the map
    assert "[7 lines]" in fmap or "lines]" in fmap


# ---- the per-symbol loop ---------------------------------------------------


def test_each_symbol_is_proposed_separately_and_accepted_on_its_own(tmp_path):
    root = _project(tmp_path)
    backend = PerSymbolBackend({"sign": SMALL_SIGN, "clamp": SMALL_CLAMP})
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False),
    )
    assert [_asked_for(p) for p in backend.prompts] == ["sign", "clamp"]  # biggest first
    assert [r.outcome for r in records] == ["accepted", "accepted"]
    final = (root / "mod.py").read_text()
    assert "positive" in final and "max(lo, min(v, hi))" in final
    assert "elif n < 0" not in final


def test_one_bad_symbol_does_not_cost_the_good_one(tmp_path):
    """The whole point of C2: a wrong `clamp` no longer throws away `sign`."""
    root = _project(tmp_path)
    backend = PerSymbolBackend({
        "sign": SMALL_SIGN,
        "clamp": "def clamp(v, lo, hi):\n    return v\n",  # wrong
    })
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False), attempts_per_symbol=1,
    )
    outcomes = [r.outcome for r in records]
    assert outcomes[0] == "accepted"
    assert outcomes[1] == "tests-failed"
    final = (root / "mod.py").read_text()
    assert "positive" in final
    assert "elif v > hi:" in final  # the original clamp survives intact


def test_a_whole_file_reply_is_verified_as_a_whole_file_not_spliced(tmp_path):
    """A small model often ignores 'one symbol'. Splicing that reply in would
    duplicate every other symbol — and the duplicate would still pass the
    suite, because the later definition wins."""
    root = _project(tmp_path)
    whole = SMALL_SIGN + "\n\n" + SMALL_CLAMP
    backend = PerSymbolBackend({"sign": whole, "clamp": whole})
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False), attempts_per_symbol=1,
    )
    assert records[0].outcome == "accepted"
    final = (root / "mod.py").read_text()
    assert final.count("def sign") == 1 and final.count("def clamp") == 1


def test_duplicate_symbol_splice_is_refused(tmp_path):
    root = _project(tmp_path)
    # a reply that redefines a *different* symbol only (not whole-file shaped)
    backend = PerSymbolBackend({"sign": "def clamp(v, lo, hi):\n    return v\n"})
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False),
        attempts_per_symbol=1, max_symbols=1,
    )
    assert records[0].outcome == "duplicate-symbol"
    assert "def clamp" in (root / "mod.py").read_text()
    assert _duplicate_keys(MODULE + "\ndef sign(n):\n    return 1\n", "python") == {"sign"}


def test_as_whole_file_needs_two_shared_symbols():
    assert _as_whole_file(SMALL_SIGN, MODULE, "python") is None
    assert _as_whole_file(SMALL_SIGN + SMALL_CLAMP, MODULE, "python") is not None


def test_per_symbol_is_the_pipeline_default(tmp_path):
    root = _project(tmp_path)
    backend = PerSymbolBackend({"sign": SMALL_SIGN, "clamp": SMALL_CLAMP})
    stats = reduce_project(
        root, backend=backend, formatter=False, whole_file_sweep=False,
    )
    assert stats.tests_ok and stats.api_ok
    assert stats.loc_final < stats.loc_after_static
    # every attempt record is keyed to a symbol, not to the file
    assert all(":" in rec["file"] for rec in stats.attempt_records)


def test_budget_exhaustion_stops_the_symbol_loop(tmp_path):
    from less_code.backends import BudgetBackend

    root = _project(tmp_path)
    backend = BudgetBackend(PerSymbolBackend({"sign": SMALL_SIGN}), 1)
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False), attempts_per_symbol=2,
    )
    assert records[0].outcome == "accepted"
    assert records[-1].outcome == "backend-error"
    assert sum(1 for r in records if r.outcome == "backend-error") == 1


# ---- API-violation feedback (gap item 2) -----------------------------------


def test_api_feedback_names_the_missing_changed_and_added_symbols():
    violations = api_violations(
        {"m.py": {"f": "def(x)", "C.m": "def(self)"}},
        {"m.py": {"f": "def(x,y)", "g": "def()"}},
    )
    text = api_feedback(violations)
    assert "C.m" in text and "f" in text and "g" in text
    assert "missing=" in text and "changed=" in text and "added=" in text
    assert api_feedback([]) == ""


def test_feedback_for_expands_an_api_rejection():
    outcome = "api-changed: m.py: missing=['helper'] | n.py: added=['leak']"
    text = _feedback_for(outcome)
    assert "helper" in text and "leak" in text
    assert "leading underscore" in text
    # a test failure is still passed through verbatim
    assert _feedback_for("tests-failed: assert 1 == 2") == "tests-failed: assert 1 == 2"


class FixesApiOnFeedbackBackend(Backend):
    """Breaks the API first; repairs it once the feedback names the violation.

    This is the loop gap item 2 closes: before, the next prompt was told only
    the word `api-changed`, which is not actionable.
    """

    def __init__(self):
        super().__init__("api-fixer")
        self.saw_feedback = False

    def complete(self, system, prompt, temperature=0.2):
        if "missing=" in prompt and "renamed" not in prompt:
            self.saw_feedback = True
            return f"```python\n{SMALL_SIGN}```"
        # attempt 1: renames the public symbol
        return "```python\ndef signum(n):\n    return 'x'\n```"


def test_api_violation_feedback_lets_the_next_attempt_fix_it(tmp_path):
    root = _project(tmp_path)
    backend = FixesApiOnFeedbackBackend()
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False),
        attempts_per_symbol=2, max_symbols=1,
    )
    assert records[0].outcome == "api-changed"
    assert "missing=['sign']" in records[0].detail
    assert backend.saw_feedback, "the violation list never reached the model"
    assert records[1].outcome == "accepted"
    assert "positive" in (root / "mod.py").read_text()
