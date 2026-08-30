"""End-to-end: tiny python project + scripted fake LLM backend through the
real pipeline (static layer + verify-gated acceptance)."""

import pathlib
import textwrap

from less_code.backends import Backend
from less_code.pipeline import reduce_project

VERBOSE_MODULE = textwrap.dedent('''
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

    def dead_never_used(x):
        y = x * 3
        return y + 1
''').strip() + "\n"

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
''').strip() + "\n"

SMALLER_MODULE = textwrap.dedent('''
    def sign(n):
        return "positive" if n > 0 else "negative" if n < 0 else "zero"

    def clamp(v, lo, hi):
        return max(lo, min(v, hi))
''').strip() + "\n"


class FakeBackend(Backend):
    def __init__(self):
        super().__init__("fake")
        self.calls = 0

    def complete(self, system, prompt, temperature=0.2):
        self.calls += 1
        return f"```python\n{SMALLER_MODULE}```"


class BrokenBackend(Backend):
    """Returns behavior-breaking 'simplifications' — must be rejected."""

    def __init__(self):
        super().__init__("broken")

    def complete(self, system, prompt, temperature=0.2):
        return "```python\ndef sign(n):\n    return 'positive'\n\n\ndef clamp(v, lo, hi):\n    return v\n```"


class NotSmallerBackend(Backend):
    def __init__(self):
        super().__init__("notsmaller")

    def complete(self, system, prompt, temperature=0.2):
        return f"```python\n{VERBOSE_MODULE}```"


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(VERBOSE_MODULE)
    (root / "test_mod.py").write_text(TESTS)
    return root


def test_pipeline_accepts_good_reduction(tmp_path):
    root = _project(tmp_path)
    stats = reduce_project(root, backend=FakeBackend(), formatter=False)
    assert stats.tests_ok and stats.api_ok
    assert stats.loc_final < stats.loc_start
    assert stats.loc_start == 18  # code lines of the three functions
    assert "dead_never_used" not in (root / "mod.py").read_text()


def test_pipeline_rejects_behavior_breaking(tmp_path):
    root = _project(tmp_path)
    stats = reduce_project(root, backend=BrokenBackend(), formatter=False)
    assert stats.tests_ok
    assert stats.loc_final == stats.loc_after_static  # LLM layer contributed 0
    assert "sign(5)" not in (root / "mod.py").read_text()  # original logic intact


def test_pipeline_rejects_not_smaller(tmp_path):
    root = _project(tmp_path)
    stats = reduce_project(root, backend=NotSmallerBackend(), formatter=False)
    assert stats.loc_final == stats.loc_after_static


def test_pipeline_static_only(tmp_path):
    root = _project(tmp_path)
    stats = reduce_project(root, backend=None, formatter=False)
    assert stats.tests_ok and stats.api_ok
    assert "dead_never_used" not in (root / "mod.py").read_text()
    assert stats.loc_final < stats.loc_start
