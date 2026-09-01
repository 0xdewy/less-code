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


def test_skip_files_keeps_the_llm_off_low_trust_files(tmp_path):
    """Trust scaling: `--skip-files` (per `lc audit`, files whose behavior
    the suite cannot see) removes a file from the LLM layer's targets while
    the static layers still run on it."""
    root = _project(tmp_path)
    (root / "untrusted.py").write_text(
        'def legacy(x):\n    if x is None:\n        return None\n    return x * 2\n'
    )
    (root / "test_mod.py").write_text(
        TESTS + "\n\nfrom untrusted import legacy\n\n\ndef test_legacy():\n    assert legacy(3) == 6\n"
    )
    before = (root / "untrusted.py").read_text()
    stats = reduce_project(
        root, backend=FakeBackend(), formatter=False,
        skip_files={"untrusted.py"},
    )
    assert stats.tests_ok and stats.api_ok
    assert (root / "untrusted.py").read_text() == before  # untouched by L2
    assert stats.loc_final < stats.loc_start              # mod.py still reduced


def test_pipeline_static_only(tmp_path):
    root = _project(tmp_path)
    stats = reduce_project(root, backend=None, formatter=False)
    assert stats.tests_ok and stats.api_ok
    assert "dead_never_used" not in (root / "mod.py").read_text()
    assert stats.loc_final < stats.loc_start


def test_pipeline_refuses_when_imports_resolve_outside_the_tree(tmp_path, monkeypatch):
    """Host-venv shadowing (pytest depends on `packaging`, which shadowed an
    editable install of pypa/packaging): the gate would test another copy of
    the code, so nothing is measured, reduced or touched."""
    from less_code import pipeline

    root = _project(tmp_path)
    monkeypatch.setattr(
        pipeline, "shadowed_imports",
        lambda _root: ["mod -> /venv/site-packages/mod/__init__.py"],
    )
    stats = reduce_project(root, backend=None, formatter=False)
    assert not stats.tests_ok
    assert stats.loc_start == 0 and stats.loc_final == 0
    assert "import-origin check failed" in stats.static_notes[0]
    assert "dead_never_used" in (root / "mod.py").read_text()  # tree untouched


def test_whole_file_strategy_skips_symbol_loops(tmp_path):
    """--strategy whole-file: repeated whole-file rewrites through the gate,
    no dedup/per-symbol records, acceptance still verify-gated."""
    (tmp_path / "mod.py").write_text(VERBOSE_MODULE)
    (tmp_path / "test_mod.py").write_text(TESTS)

    class WholeFileBackend(Backend):
        def __init__(self):
            super().__init__("fake")
            self.calls = 0

        def complete(self, system, prompt, temperature=0.2):
            self.calls += 1
            return f"```python\n{SMALLER_MODULE}```"

    backend = WholeFileBackend()
    stats = reduce_project(tmp_path, backend=backend, attempts_per_file=2, strategy="whole-file")
    j = stats.to_json()
    assert j["tests_ok"] and j["api_ok"]
    assert j["loc_final"] < j["loc_start"]
    outcomes = [a["outcome"] for a in j["attempts"]]
    assert "accepted" in outcomes
    assert not any(a["file"].endswith("(dedup)") or ":" in pathlib.Path(a["file"]).name.replace(".py", "")
                    for a in j["attempts"] if a["outcome"] == "accepted"), "no symbol/dedup records expected"


class TestMineSink:
    """SFT pairs must be exactly what the gate certified: mining records the
    (system, prompt, response) of ACCEPTED proposals only — a doc-eating or
    behavior-breaking acceptance would poison the training set."""

    def test_accepted_proposals_are_mined(self, tmp_path):
        import json

        import less_code.llm_reduce as lr
        from less_code.backends import Backend

        class OneGoodRewrite(Backend):
            def __init__(self):
                super().__init__("mine")
                self.n = 0

            def complete(self, system, prompt, temperature=0.2):
                self.n += 1
                if "clamp" in prompt[:600] and "Rewrite ONLY" in prompt:
                    return "```python\ndef clamp(v, lo, hi):\n    return max(lo, min(v, hi))\n```"
                return f"```python\n{VERBOSE_MODULE}```"  # not smaller / rejected

        root = _project(tmp_path)
        mine = tmp_path / "pairs.jsonl"
        lr.MINE_SINK = lambda *a: mine.open("a").write(json.dumps(a) + "\n")
        try:
            stats = reduce_project(root, backend=OneGoodRewrite(), formatter=False)
        finally:
            lr.MINE_SINK = None
        assert stats.tests_ok
        rows = [json.loads(l) for l in mine.read_text().splitlines() if l.strip()]
        assert rows, "nothing was mined"
        for row in rows:
            assert set(row[:2]) == {"kind", "unit"} or len(row) == 5
        # at least one accepted symbol pair with a fenced response
        assert any("clamp" in r[1] for r in rows)

    def test_rejected_proposals_are_not_mined(self, tmp_path):
        import json

        import less_code.llm_reduce as lr

        root = _project(tmp_path)
        mine = tmp_path / "pairs.jsonl"
        lr.MINE_SINK = lambda *a: mine.open("a").write(json.dumps(a) + "\n")
        try:
            stats = reduce_project(root, backend=BrokenBackend(), formatter=False)
        finally:
            lr.MINE_SINK = None
        assert stats.loc_final == stats.loc_after_static  # nothing accepted
        assert mine.exists() is False or mine.read_text().strip() == ""


class TestDocsPreservationGate:
    """A 7B model 'reduces' a mature library mostly by deleting its published
    documentation (click: whole enum docstrings, `#:` Sphinx comments). Doc
    loss is invisible to the suite and free in code-LOC, so it needs its own
    pre-gate — cheaper than the parse gate, before any disk write."""

    DOC_MODULE = textwrap.dedent('''
        def sign(n):
            """Return the sign name of n."""
            if n > 0:
                result = "positive"
            elif n < 0:
                result = "negative"
            else:
                result = "zero"
            return result
        ''').strip() + "\n"

    DOC_TESTS = "from mod import sign\n\ndef test_sign():\n    assert sign(5) == 'positive'\n    assert sign(0) == 'zero'\n"

    def _verify(self, tmp_path, candidate):
        from less_code.api_check import python_api
        from less_code.llm_reduce import verify_candidate

        (tmp_path / "mod.py").write_text(self.DOC_MODULE)
        (tmp_path / "test_mod.py").write_text(self.DOC_TESTS)
        ok = type("Result", (), {"ok": True, "output_tail": ""})()
        return verify_candidate(
            tmp_path / "mod.py", "python", candidate,
            python_api(self.DOC_MODULE), 7, lambda r, l: ok, tmp_path,
        )

    def test_a_docstring_stripping_rewrite_is_rejected(self, tmp_path):
        stripped = 'def sign(n):\n    return "positive" if n > 0 else "negative" if n < 0 else "zero"\n'
        outcome, _loc = self._verify(tmp_path, stripped)
        assert outcome.startswith("docs-lost")

    def test_a_rewrite_that_keeps_the_docstring_passes_the_gate(self, tmp_path):
        kept = (
            'def sign(n):\n'
            '    """Return the sign name of n."""\n'
            '    return "positive" if n > 0 else "negative" if n < 0 else "zero"\n'
        )
        outcome, _loc = self._verify(tmp_path, kept)
        assert not outcome.startswith("docs-lost")

    def test_doc_comment_loss_is_detected_for_js_and_rust(self):
        from less_code.llm_reduce import docs_lost

        js_before = "/** Docs. */\nfunction f(x) {\n  return x + 1;\n}\n"
        js_after = "function f(x) {\n  return x + 1;\n}\n"
        assert len(docs_lost(js_before, js_after, "javascript")) == 1
        rs_before = "/// Docs.\npub fn f(x: i32) -> i32 {\n    x + 1\n}\n"
        rs_after = "pub fn f(x: i32) -> i32 {\n    x + 1\n}\n"
        assert len(docs_lost(rs_before, rs_after, "rust")) == 1

    def test_sphinx_and_plain_comment_loss_is_detected_for_python(self):
        """The docstring-only gate let an accepted click rewrite eat 10 `#:`
        Sphinx attribute docs and a multi-line `#` comment citing the issue
        a workaround exists for. All `#` comment lines are documentation."""
        from less_code.llm_reduce import docs_lost

        before = (
            "class C:\n"
            "    #: the name the command thinks it has\n"
            "    name = ''\n"
            "    # Refs: https://github.com/pallets/click/issues/3071\n"
            "    def f(self):\n"
            "        return 1\n"
        )
        after = "class C:\n    name = ''\n\n    def f(self):\n        return 1\n"
        lost = docs_lost(before, after, "python")
        assert len(lost) == 2  # the `#:` line and the issue-reference comment
        assert any(l.startswith("#:") for l in lost)
        assert any("Refs:" in l for l in lost)

    def test_multifile_candidate_losing_docs_anywhere_is_rejected(self, tmp_path):
        from less_code.llm_reduce import verify_multifile

        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text(
            'def one():\n    """Doc one."""\n    if True:\n        return 1\n    return 0\n\n\ndef two():\n    """Doc two."""\n    return 2\n'
        )
        b.write_text("x = 1\n")
        # smaller in code-LOC AND missing one()s docstring: the docs gate must
        # catch it, not let it through on the size check alone
        stripped = {
            a: 'def one():\n    return 1\n\n\ndef two():\n    """Doc two."""\n    return 2\n',
            b: "x = 1\n",
        }
        outcome, _before, _after = verify_multifile(stripped, "python", lambda r, l: None, tmp_path)
        assert outcome.startswith("docs-lost")
        assert "a.py" in outcome


class TestBudgetScheduling:
    """click lesson: biggest-file-first spent 100% of a 40-call budget in
    core.py; 13 of 17 files never saw an LLM call. The budget now round-robins
    across files, ranked by size x per-file trust."""

    BIG = textwrap.dedent('''
        def one(x):
            if x is None:
                return None
            if not isinstance(x, int):
                raise ValueError("int required")
            return x + 1

        def two(x):
            if x is None:
                return None
            if not isinstance(x, int):
                raise ValueError("int required")
            return x + 2
    ''').strip() + "\n"

    SMALL = textwrap.dedent('''
        def tre(x):
            try:
                return x + 3
            except TypeError:
                return None

        def for_(x):
            try:
                return x + 4
            except TypeError:
                return None
    ''').strip() + "\n"

    TESTS = (
        "from big import one, two\nfrom small import tre, for_\n\n"
        "def test_all():\n"
        "    assert one(1) == 2 and two(1) == 3\n"
        "    assert tre(1) == 4 and for_(1) == 5\n"
    )

    def _project(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "big.py").write_text(self.BIG)
        (root / "small.py").write_text(self.SMALL)
        (root / "test_all.py").write_text(self.TESTS)
        return root

    def test_the_budget_spreads_across_files(self, tmp_path):
        from less_code.backends import BudgetBackend

        class Garbage(Backend):
            def __init__(self):
                super().__init__("garbage")

            def complete(self, system, prompt, temperature=0.2):
                return "no code block"

        root = self._project(tmp_path)
        backend = BudgetBackend(Garbage(), 6)
        stats = reduce_project(root, backend=backend, formatter=False)
        files_attempted = {a["file"].rsplit(":", 1)[0] for a in stats.attempt_records}
        assert any(f.endswith("big.py") for f in files_attempted)
        assert any(f.endswith("small.py") for f in files_attempted), (
            "the smaller file must see LLM calls too, not just the biggest"
        )

    def test_trust_ranks_a_small_verified_file_first(self, tmp_path):
        prompts = []

        class Recording(Backend):
            def __init__(self):
                super().__init__("rec")

            def complete(self, system, prompt, temperature=0.2):
                prompts.append(prompt)
                return "no code block"

        root = self._project(tmp_path)
        reduce_project(
            root, backend=Recording(), formatter=False,
            trust={"big.py": 0.1, "small.py": 0.95}, symbols_per_sweep=1,
        )
        assert prompts, "no prompt was built"
        symbol_prompts = [p for p in prompts if p.startswith("File: small.py")]
        assert symbol_prompts, "small.py must reach the per-symbol loop"
        assert "tre" in symbol_prompts[0], (
            "small.py x 0.95 must outrank big.py x 0.1 despite its size"
        )
