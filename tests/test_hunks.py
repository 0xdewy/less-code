"""C1 hunk decomposition + delta debugging.

The headline case: a rewrite that is correct in 2 of 3 functions and wrong in
the third must contribute the 2 good functions instead of being discarded.
"""

import pathlib
import textwrap

import pytest

from less_code.backends import Backend
from less_code.hunks import apply, decompose, symbol_spans
from less_code.pipeline import reduce_project

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


    def tally(items):
        total = 0
        for item in items:
            if item is None:
                continue
            total = total + item
        return total
''').strip() + "\n"

TESTS = textwrap.dedent('''
    from mod import clamp, sign, tally

    def test_sign():
        assert sign(5) == "positive"
        assert sign(-3) == "negative"
        assert sign(0) == "zero"

    def test_clamp():
        assert clamp(1, 2, 8) == 2
        assert clamp(5, 2, 8) == 5
        assert clamp(9, 2, 8) == 8

    def test_tally():
        assert tally([1, 2, None, 3]) == 6
        assert tally([]) == 0
''').strip() + "\n"

# sign + clamp are correct and shorter; tally silently drops the None guard
TWO_OF_THREE = textwrap.dedent('''
    def sign(n):
        return "positive" if n > 0 else "negative" if n < 0 else "zero"


    def clamp(v, lo, hi):
        return max(lo, min(v, hi))


    def tally(items):
        return sum(items)
''').strip() + "\n"


class TwoOfThreeBackend(Backend):
    """One whole-file rewrite: 2 good hunks, 1 behaviour-breaking hunk."""

    def __init__(self):
        super().__init__("two-of-three")
        self.calls = 0

    def complete(self, system, prompt, temperature=0.2):
        self.calls += 1
        return f"```python\n{TWO_OF_THREE}```"


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(MODULE)
    (root / "test_mod.py").write_text(TESTS)
    return root


class TestSpans:
    def test_python_spans_cover_decorators(self):
        src = "import os\n\n\n@deco\ndef f():\n    return 1\n\n\nclass C:\n    pass\n"
        assert symbol_spans(src, "python") == [(3, 6, "f"), (8, 10, "C")]

    def test_js_spans_are_brace_matched(self):
        src = (
            "const A = 1;\n"
            "export function f(x) {\n  if (x) {\n    return 1;\n  }\n  return 0;\n}\n"
            "class C {\n  m() { return 2; }\n}\n"
        )
        assert symbol_spans(src, "javascript") == [(1, 7, "f"), (7, 10, "C")]

    def test_rust_spans_include_impl_and_bodyless_items(self):
        src = (
            "use std::fmt;\n"
            "pub struct S;\n"
            "pub fn f(a: i32) -> i32 {\n    if a > 0 { 1 } else { 0 }\n}\n"
            "impl S {\n    pub fn m(&self) -> i32 { 1 }\n}\n"
        )
        names = [s[2] for s in symbol_spans(src, "rust")]
        assert names == ["S", "f", "S"]


class TestDecompose:
    def test_one_hunk_per_changed_symbol(self):
        hunks = decompose(MODULE, TWO_OF_THREE, "python")
        assert sorted(h.key for h in hunks) == ["sym:clamp", "sym:sign", "sym:tally"]
        assert all(h.kind == "replace" for h in hunks)
        assert all(h.loc_delta("python") > 0 for h in hunks)

    def test_unchanged_symbols_produce_no_hunk(self):
        candidate = MODULE.replace(
            'def clamp(v, lo, hi):\n    if v < lo:\n        return lo\n'
            '    elif v > hi:\n        return hi\n    else:\n        return v',
            'def clamp(v, lo, hi):\n    return max(lo, min(v, hi))',
        )
        hunks = decompose(MODULE, candidate, "python")
        assert [h.key for h in hunks] == ["sym:clamp"]

    def test_apply_subset_is_exact_and_reversible(self):
        hunks = decompose(MODULE, TWO_OF_THREE, "python")
        assert apply(MODULE, hunks).strip() == TWO_OF_THREE.strip()
        assert apply(MODULE, []) == MODULE.rstrip("\n")
        only_sign = [h for h in hunks if h.key == "sym:sign"]
        patched = apply(MODULE, only_sign)
        assert 'return "positive" if n > 0' in patched
        assert "elif v > hi:" in patched  # clamp untouched
        assert "if item is None:" in patched  # tally untouched

    def test_deleted_and_added_symbols(self):
        candidate = "def sign(n):\n    return _s(n)\n\n\ndef _s(n):\n    return n\n"
        hunks = decompose(MODULE, candidate, "python")
        kinds = {h.key: h.kind for h in hunks}
        assert kinds["sym:clamp"] == "delete"
        assert kinds["sym:tally"] == "delete"
        assert kinds["sym:_s"] == "insert"
        assert "def _s(n):" in apply(MODULE, hunks)

    def test_preamble_gap_is_its_own_hunk(self):
        original = "import os\nimport sys\n\n\ndef f():\n    return os\n"
        candidate = "import os\n\n\ndef f():\n    return os\n"
        hunks = decompose(original, candidate, "python")
        assert [h.key for h in hunks] == ["gap:<preamble>"]
        assert apply(original, hunks) == candidate.rstrip("\n")

    def test_javascript_roundtrip(self):
        original = (
            "export function a(x) {\n  let out = 0;\n  out = out + x;\n  return out;\n}\n"
            "export function b(x) {\n  return x;\n}\n"
        )
        candidate = (
            "export function a(x) {\n  return x;\n}\n"
            "export function b(x) {\n  return x;\n}\n"
        )
        hunks = decompose(original, candidate, "javascript")
        assert [h.key for h in hunks] == ["sym:a"]
        assert apply(original, hunks) == candidate.rstrip("\n")

    def test_rust_roundtrip(self):
        original = (
            "pub fn a(x: i32) -> i32 {\n    let mut o = 0;\n    o = o + x;\n    o\n}\n"
            "pub fn b(x: i32) -> i32 {\n    x\n}\n"
        )
        candidate = "pub fn a(x: i32) -> i32 {\n    x\n}\npub fn b(x: i32) -> i32 {\n    x\n}\n"
        hunks = decompose(original, candidate, "rust")
        assert [h.key for h in hunks] == ["sym:a"]
        assert apply(original, hunks) == candidate.rstrip("\n")


class TestSearchThroughPipeline:
    def test_two_good_hunks_are_accepted_and_the_bad_one_is_not(self, tmp_path):
        root = _project(tmp_path)
        stats = reduce_project(root, backend=TwoOfThreeBackend(), formatter=False)
        final = (root / "mod.py").read_text()
        assert stats.tests_ok and stats.api_ok
        # the two correct rewrites landed
        assert 'return "positive" if n > 0' in final
        assert "return max(lo, min(v, hi))" in final
        # the wrong one did not
        assert "return sum(items)" not in final
        assert "if item is None:" in final
        assert stats.loc_final < stats.loc_after_static
        outcomes = [a["outcome"] for a in stats.attempt_records]
        assert "hunks-accepted" in outcomes
        detail = next(a["detail"] for a in stats.attempt_records
                      if a["outcome"] == "hunks-accepted")
        assert "sym:sign" in detail and "sym:clamp" in detail
        assert "sym:tally=tests-failed" in detail

    def test_all_bad_rewrite_changes_nothing(self, tmp_path):
        root = _project(tmp_path)

        class AllBad(Backend):
            def __init__(self):
                super().__init__("all-bad")

            def complete(self, system, prompt, temperature=0.2):
                return (
                    "```python\ndef sign(n):\n    return 'positive'\n\n\n"
                    "def clamp(v, lo, hi):\n    return v\n\n\n"
                    "def tally(items):\n    return sum(items)\n```"
                )

        stats = reduce_project(root, backend=AllBad(), formatter=False)
        assert stats.tests_ok
        assert stats.loc_final == stats.loc_after_static
        assert "if item is None:" in (root / "mod.py").read_text()
