"""End-to-end: tiny python project through `shrink_project`, with the
gate proving its three promises:

  * tests stay green;
  * code-LOC goes down;
  * the public API surface is preserved.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from less_code.ml_shrink import ProposedEdit, apply_edit, extract_symbols, syntax_ok
from less_code.pipeline import shrink_project
from less_code.static import StaticResult


def test_within_file_salvage_keeps_savings_but_not_documentation_loss(
    tmp_path, monkeypatch
):
    source = (
        "def _first(x):\n    result = x + 1\n    return result\n\n"
        "# Preserve this explanation\n\n"
        "def _second(x):\n    result = x + 2\n    return result\n"
    )
    proposal = source.replace(
        "    result = x + 1\n    return result", "    return x + 1"
    )
    proposal = proposal.replace("# Preserve this explanation\n", "")
    proposal = proposal.replace(
        "    result = x + 2\n    return result", "    return x + 2"
    )
    root = _ml_project(
        tmp_path,
        source,
        "from library import _first, _second\ndef test_values():\n"
        "    assert _first(1) == 2\n    assert _second(2) == 4\n",
    )
    monkeypatch.setattr(
        "less_code.pipeline.static_pass",
        lambda *_a, **_k: StaticResult(
            changed_files={str(root / "library.py"): proposal}
        ),
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_outline", lambda sources: (sources, [])
    )
    stats = shrink_project(root)
    assert stats.tests_ok and stats.docs_ok and stats.api_ok
    assert stats.loc_start - stats.loc_final == 2
    assert "# Preserve this explanation" in (root / "library.py").read_text()
    assert any("kept" in note and "hunk" in note for note in stats.static_notes)


@pytest.mark.parametrize(
    "failure", ["baseline", "static", "model", "exception", "none"]
)
def test_terminal_audit_restores_checkpoint_without_model_feedback(
    tmp_path, monkeypatch, failure
):
    source = "def _helper(value):\n    result = value + 0\n    return result\n"
    root = _ml_project(
        tmp_path,
        source,
        "from library import _helper\ndef test_value():\n    assert _helper(3) == 3\n",
    )
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_outline", lambda sources: (sources, [])
    )
    calls = []

    class Model:
        def propose(self, lang, symbol):
            calls.append(symbol)
            assert "feedback" not in symbol
            return ProposedEdit(symbol["id"], "def _helper(value):\n    return value\n")

    audits = []

    def audit():
        stage = ["baseline", "static", "model"][len(audits)]
        audits.append(stage)
        if failure == "exception" and stage == "model":
            raise RuntimeError("validator unavailable")
        return stage != failure

    if failure == "exception":
        with pytest.raises(RuntimeError, match="validator unavailable"):
            shrink_project(root, ml_backend=Model(), final_validator=audit)
        assert (root / "library.py").read_text() == source
        return
    stats = shrink_project(root, ml_backend=Model(), final_validator=audit)
    assert len(calls) == int(failure not in {"baseline", "static"})
    assert stats.tests_ok and stats.api_ok and stats.docs_ok
    assert (stats.loc_final < stats.loc_start) == (failure == "none")
    if failure != "none":
        assert (root / "library.py").read_text() == source
    if failure == "model":
        assert stats.ml_stats["rolled_back"] == 1
        assert stats.ml_records[0]["accepted"]  # raw gate decision retained
        assert stats.ml_records[0]["rolled_back"]
        assert not stats.layer_records[-1]["committed"]


def test_model_recovers_from_test_failure_with_context_and_fresh_spans(
    tmp_path, monkeypatch
):
    source = (
        "def _first(value):\n    result = value + 0\n    return result\n\n"
        "def _second(value):\n    result = value + 0\n    return result\n"
    )
    root = _ml_project(
        tmp_path,
        source,
        "from library import _first, _second\n\ndef test_values():\n"
        "    assert _first(3) == 3\n    assert _second(4) == 4\n",
    )
    (root / "tests_hidden").mkdir()
    (root / "tests_hidden" / "test_secret.py").write_text("HIDDEN_MARKER = '_first'\n")
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_outline", lambda sources: (sources, [])
    )

    class Model:
        name = "feedback-fixture"

        def propose(self, lang, symbol):
            assert "HIDDEN_MARKER" not in str(symbol)
            assert any(item["kind"] == "test reference" for item in symbol["context"])
            if symbol["name"] == "_first" and not symbol.get("feedback"):
                return ProposedEdit(symbol["id"], "def _first(value):\n    return 0\n")
            if symbol["name"] == "_first":
                assert "tests failed" in symbol["feedback"]["reason"]
            else:
                current = (root / "library.py").read_bytes()
                assert (
                    current[symbol["start_byte"] : symbol["end_byte"]].decode()
                    == symbol["text"]
                )
                assert b"return value\n" in current
            return ProposedEdit(
                symbol["id"], f"def {symbol['name']}(value):\n    return value\n"
            )

    stats = shrink_project(root, ml_backend=Model())
    assert stats.tests_ok and stats.ml_stats["accepted"] == 2
    assert stats.ml_stats["calls"] == 3
    assert [r["category"] for r in stats.ml_records] == [
        "tests",
        "accepted",
        "accepted",
    ]
    assert all(not r["independently_validated"] for r in stats.ml_records)
    assert stats.ml_records[1]["prompt"]["feedback"]["previous_replacement"].endswith(
        "return 0\n"
    )


def test_model_attempt_limit_and_duplicate_short_circuit(tmp_path, monkeypatch):
    source = "def _helper(value):\n    result = value + 0\n    return result\n"
    root = _ml_project(
        tmp_path,
        source,
        "from library import _helper\ndef test_value():\n    assert _helper(3) == 3\n",
    )
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    model = _Model({"_helper": "def _helper(value):\n    return 0\n"})
    single = shrink_project(root, ml_backend=model, ml_attempts=1)
    assert single.ml_stats["calls"] == 1
    retries = shrink_project(root, ml_backend=model, ml_attempts=3)
    assert retries.ml_stats["calls"] == 2
    assert retries.ml_stats["rejected_tests"] == 1
    assert retries.ml_stats["rejected_duplicate"] == 1
    assert (root / "library.py").read_text() == source


def test_candidates_use_project_format_and_read_only_lint(tmp_path, monkeypatch):
    root = _ml_project(
        tmp_path,
        "def _helper():\n    result = 'hello'\n    return result\n",
        "from library import _helper\ndef test_value():\n    assert _helper() == 'hello'\n",
    )
    (root / "pyproject.toml").write_text(
        '[tool.ruff]\nfix = true\n[tool.ruff.lint]\nselect = ["Q"]\n'
        '[tool.ruff.lint.flake8-quotes]\ninline-quotes = "single"\n'
        '[tool.ruff.format]\nquote-style = "single"\n'
    )
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    stats = shrink_project(
        root, ml_backend=_Model({"_helper": 'def _helper():\n    return "hello"\n'})
    )
    assert stats.ml_stats["accepted"] == 1
    assert "return 'hello'" in (root / "library.py").read_text()


VERBOSE_MODULE = textwrap.dedent("""
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

    def _dead_never_used(x):
        y = x * 3
        return y + 1
""").lstrip()


TESTS = textwrap.dedent("""
    from mod import sign, clamp

    def test_sign():
        assert sign(5) == "positive"
        assert sign(-3) == "negative"
        assert sign(0) == "zero"

    def test_clamp():
        assert clamp(1, 2, 8) == 2
        assert clamp(5, 2, 8) == 5
        assert clamp(9, 2, 8) == 8
""").lstrip()


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "mod.py").write_text(VERBOSE_MODULE)
    (root / "test_mod.py").write_text(TESTS)
    return root


def test_shrink_runs_static_rules(tmp_path):
    root = _project(tmp_path)
    stats = shrink_project(root)
    assert stats.tests_ok
    assert stats.api_ok
    assert stats.loc_final < stats.loc_start
    src = (root / "mod.py").read_text()
    # private dead code is removed by the static layer
    assert "_dead_never_used" not in src
    # the rule library tightened the if-elif-else into a one-liner
    assert "sign" in src and "clamp" in src


def test_shrink_reverts_a_layer_that_breaks_a_test(tmp_path):
    """A layer whose edits go red is reverted; the rest of the pipeline
    still runs and the final tree is green."""
    root = _project(tmp_path)
    # Re-point one test to demand a name the dead-code pass would remove.
    (root / "test_mod.py").write_text(
        TESTS
        + "\n\nfrom mod import _dead_never_used\n\n"
        + "def test_dead():\n    assert _dead_never_used(2) == 7\n"
    )
    stats = shrink_project(root)
    # The dead-code removal would break this test, so the layer is reverted.
    src = (root / "mod.py").read_text()
    assert "_dead_never_used" in src  # preserved because the test pins it
    assert stats.tests_ok
    # Other layers (rules) still ran and reduced LOC.
    assert stats.loc_final < stats.loc_start


def test_shrink_rejects_when_baseline_tests_are_red(tmp_path):
    root = _project(tmp_path)
    (root / "test_mod.py").write_text("def test_will_fail():\n    assert False\n")
    stats = shrink_project(root)
    assert not stats.tests_ok
    assert stats.loc_start == 0
    assert (root / "mod.py").read_text() == VERBOSE_MODULE


def test_shrink_refuses_when_imports_resolve_outside_the_tree(tmp_path, monkeypatch):
    """Host-venv shadowing: the gate would test a different copy of the code,
    so the pipeline refuses before measuring anything."""
    from less_code import pipeline

    root = _project(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "shadowed_imports",
        lambda _root: ["mod -> /venv/site-packages/mod/__init__.py"],
    )
    stats = shrink_project(root)
    assert not stats.tests_ok
    assert stats.loc_start == 0
    assert "import-origin check failed" in stats.static_notes[0]
    assert (root / "mod.py").read_text() == VERBOSE_MODULE


def test_shrink_preserves_the_post_static_api_surface(tmp_path):
    """A layer that edits a public symbol (changes its signature) is
    reverted by the API check at the end."""
    root = _project(tmp_path)
    stats = shrink_project(root)
    assert stats.api_ok
    src_after = (root / "mod.py").read_text()
    assert "def sign" in src_after, "public symbol removed"
    assert "def clamp" in src_after


def test_static_gate_salvages_good_files_from_rejected_batch(tmp_path, monkeypatch):
    from less_code import pipeline

    root = tmp_path / "static-project"
    root.mkdir()
    good = root / "good.py"
    documented = root / "documented.py"
    good.write_text("def value(x):\n    result = x\n    return result\n")
    documented_source = (
        "def label(x):\n"
        '    """Return the label unchanged."""\n'
        "    result = x\n"
        "    return result\n"
    )
    documented.write_text(documented_source)
    (root / "test_project.py").write_text(
        "from good import value\n"
        "from documented import label\n\n"
        "def test_values():\n"
        "    assert value(1) == 1\n"
        '    assert label("x") == "x"\n'
    )
    monkeypatch.setattr(
        pipeline,
        "static_pass",
        lambda *_a, **_k: StaticResult(
            changed_files={
                str(good): "def value(x):\n    return x\n",
                str(documented): "def label(x):\n    return x\n",
            }
        ),
    )
    monkeypatch.setattr(pipeline, "_apply_rules", lambda sources: (sources, []))
    monkeypatch.setattr(pipeline, "_apply_outline", lambda sources: (sources, []))

    stats = shrink_project(root)

    assert good.read_text() == "def value(x):\n    return x\n"
    assert documented.read_text() == documented_source
    assert stats.loc_after_static == stats.loc_start - 1
    assert stats.tests_ok and stats.api_ok and stats.docs_ok
    assert any("static subset: kept subset" in note for note in stats.static_notes)
    assert any("documentation changed" in note for note in stats.static_notes)


def test_static_gate_preserves_the_original_public_api(tmp_path, monkeypatch):
    from less_code import pipeline

    root = tmp_path / "public-api-project"
    root.mkdir()
    module = root / "library.py"
    source = "def public_hook():\n    return 1\n\ndef used():\n    return 2\n"
    module.write_text(source)
    (root / "test_library.py").write_text(
        "from library import used\n\ndef test_used():\n    assert used() == 2\n"
    )
    monkeypatch.setattr(
        pipeline,
        "static_pass",
        lambda *_a, **_k: StaticResult(
            changed_files={str(module): "def used():\n    return 2\n"}
        ),
    )
    monkeypatch.setattr(pipeline, "_apply_rules", lambda sources: (sources, []))
    monkeypatch.setattr(pipeline, "_apply_outline", lambda sources: (sources, []))

    stats = shrink_project(root)

    assert module.read_text() == source
    assert stats.api_ok and not stats.static_removed_symbols
    assert any("API changed" in note for note in stats.static_notes)


def test_rule_gate_salvages_good_files_from_rejected_batch(tmp_path, monkeypatch):
    from less_code import pipeline, rules

    root = tmp_path / "rule-project"
    root.mkdir()
    good = root / "good.py"
    bad = root / "bad.py"
    good_source = "def value(x):\n    result = x\n    return result\n"
    bad_source = "def label(x):\n    result = x\n    return result\n"
    good.write_text(good_source)
    bad.write_text(bad_source)
    (root / "test_project.py").write_text(
        "from good import value\n"
        "from bad import label\n\n"
        "def test_values():\n"
        "    assert value(1) == 1\n"
        "    assert label(2) == 2\n"
    )

    def scripted(sources, only=None):
        if only not in (None, {"scripted"}):
            return sources, []
        return {
            path: (
                "def value(x):\n    return x\n"
                if path == good
                else "def label(x):\n    return 0\n"
            )
            for path in sources
        }, ["scripted edits"]

    monkeypatch.setattr(pipeline, "static_pass", lambda *_a, **_k: StaticResult())
    monkeypatch.setattr(pipeline, "_apply_rules", scripted)
    monkeypatch.setattr(pipeline, "_apply_outline", lambda sources: (sources, []))
    monkeypatch.setattr(pipeline, "_apply_ruff_again", lambda sources: (sources, []))
    monkeypatch.setattr(rules, "RULES", ("scripted",))

    stats = shrink_project(root)

    assert good.read_text() == "def value(x):\n    return x\n"
    assert bad.read_text() == bad_source
    assert stats.tests_ok and stats.api_ok and stats.docs_ok
    assert any("rules:scripted subset: kept subset" in n for n in stats.static_notes)
    assert any(
        "rules:scripted subset: rejected bad.py" in n for n in stats.static_notes
    )


class _Model:
    name = "scripted"

    def __init__(self, replacements):
        self.replacements = replacements

    def propose(self, _lang, symbol):
        replacement = self.replacements.get(symbol["name"])
        return ProposedEdit(symbol["id"], replacement) if replacement else None


def _ml_project(tmp_path, source, tests):
    root = tmp_path / "ml-project"
    root.mkdir()
    (root / "library.py").write_text(source)
    (root / "test_library.py").write_text(tests)
    return root


@pytest.mark.parametrize("preserve_signature", [False, True])
def test_ml_public_bodies_are_eligible_but_signatures_are_frozen(
    tmp_path, monkeypatch, preserve_signature
):
    source = "def public(value):\n    result = value + 0\n    return result\n"
    root = _ml_project(
        tmp_path,
        source,
        "from library import public\n\ndef test_public():\n    assert public(3) == 3\n",
    )
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    parameter = "value" if preserve_signature else "x"
    replacement = f"def public({parameter}):\n    return {parameter}\n"
    model = _Model({"public": replacement})

    stats = shrink_project(root, ml_backend=model, ml_attempts=1)

    assert stats.tests_ok and stats.api_ok
    assert (root / "library.py").read_text() == (
        replacement if preserve_signature else source
    )
    assert stats.ml_stats["proposed"] == 1
    assert stats.ml_stats["accepted"] == int(preserve_signature)


def test_ml_documentation_loss_is_reverted(tmp_path, monkeypatch):
    source = (
        "def _helper(value):\n"
        '    """Return the supplied value."""\n'
        "    result = value + 0\n"
        "    return result\n"
    )
    root = _ml_project(
        tmp_path,
        source,
        "from library import _helper\n\ndef test_helper():\n    assert _helper(3) == 3\n",
    )
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    model = _Model({"_helper": "def _helper(value):\n    return value\n"})

    stats = shrink_project(root, ml_backend=model)

    assert stats.docs_ok and (root / "library.py").read_text() == source
    assert stats.ml_stats["rejected_docs"] == 1


def test_ml_keeps_good_edit_when_another_fails_tests(tmp_path, monkeypatch):
    source = textwrap.dedent("""
        def _first(value):
            result = value + 0
            return result

        def _second(value):
            result = value + 0
            return result
    """).lstrip()
    tests = textwrap.dedent("""
        from library import _first, _second

        def test_values():
            assert _first(3) == 3
            assert _second(4) == 4
    """).lstrip()
    root = _ml_project(tmp_path, source, tests)
    monkeypatch.setattr(
        "less_code.pipeline.static_pass", lambda *_a, **_k: StaticResult()
    )
    monkeypatch.setattr(
        "less_code.pipeline._apply_rules", lambda sources: (sources, [])
    )
    model = _Model(
        {
            "_first": "def _first(value):\n    return value\n",
            "_second": "def _second(value):\n    return 0\n",
        }
    )

    stats = shrink_project(root, ml_backend=model)
    result = (root / "library.py").read_text()

    assert stats.tests_ok and stats.api_ok
    assert "def _first(value):\n    return value" in result
    assert "result = value + 0" in result.split("def _second", 1)[1]
    assert stats.ml_stats["accepted"] == 1
    assert stats.ml_stats["rejected_tests"] == 1


def test_ml_host_owns_symbol_spans_and_detects_parser_errors():
    symbol = extract_symbols(
        "class C:\n    def _f(self, value):\n        return value\n",
        "python",
    )[0]
    assert symbol["name"] == "C._f"
    wrong = ProposedEdit("other@1:2", "def f(self):\n    return 1\n")
    try:
        apply_edit(symbol["text"], symbol, wrong)
    except ValueError:
        pass
    else:
        raise AssertionError("a model-owned span was accepted")
    try:
        apply_edit(symbol["text"], symbol, ProposedEdit(symbol["id"], 3))
    except ValueError:
        pass
    else:
        raise AssertionError("a non-text replacement was accepted")
    assert not syntax_ok("export function f( {", "javascript")
    assert not syntax_ok("pub fn f( {", "rust")


def test_ml_byte_spans_preserve_neighbors_unicode_and_method_indentation():
    javascript = 'const label = "é"; function _f(x) { return x; } const y = 1;\n'
    js_symbol = extract_symbols(javascript, "javascript")[0]
    changed = apply_edit(
        javascript,
        js_symbol,
        ProposedEdit(js_symbol["id"], "function _f(x) { return x ?? 0; }"),
    )
    assert changed == (
        'const label = "é"; function _f(x) { return x ?? 0; } const y = 1;\n'
    )

    python = (
        "class C:\n"
        "    def _f(self, value):\n"
        "        result = value + 0\n"
        "        return result\n"
    )
    py_symbol = extract_symbols(python, "python")[0]
    changed = apply_edit(
        python,
        py_symbol,
        ProposedEdit(py_symbol["id"], "def _f(self, value):\n    return value"),
    )
    assert syntax_ok(changed, "python")
    assert "    def _f(self, value):\n        return value" in changed


def test_ml_rejects_nonshrinking_edit_before_running_candidate_tests(
    tmp_path, monkeypatch
):
    from less_code import pipeline

    source = "def _helper(value):\n    return value\n"
    root = _ml_project(
        tmp_path,
        source,
        "from library import _helper\n\ndef test_helper():\n    assert _helper(3) == 3\n",
    )
    monkeypatch.setattr(pipeline, "static_pass", lambda *_a, **_k: StaticResult())
    real_run_tests = pipeline.run_tests
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return real_run_tests(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_tests", counted)
    model = _Model(
        {"_helper": "def _helper(value):\n    extra = value\n    return extra\n"}
    )

    stats = shrink_project(root, ml_backend=model)

    assert (root / "library.py").read_text() == source
    assert stats.ml_stats["rejected_loc"] == 1
    assert len(calls) == 2  # baseline and final confirmation, not the candidate
