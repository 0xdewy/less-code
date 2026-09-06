"""D1/D2 — ruff, unreachable Python, and dead internal JavaScript."""

import shutil
import textwrap

import pytest

from less_code.langdetect import map_project
from less_code.pipeline import shrink_project
from less_code.static import (
    _js_internal_spans,
    _js_remove_dead_internal,
    _js_static,
    _python_drop_unreachable,
    _ruff_fix,
    _unreachable_line_spans,
    static_pass,
)

ruff_only = pytest.mark.skipif(
    shutil.which("ruff") is None, reason="ruff not installed"
)


def norm(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


# ---- D1: ruff tiers --------------------------------------------------------


@ruff_only
def test_ruff_preserves_unreferenced_imports():
    """A local reference scan does not prove import side effects or external
    accesses to the imported module attribute are dispensable."""
    fixed = _ruff_fix("import os, sys\n\n\ndef f(x):\n    return x\n", "m.py")
    assert "import os, sys" in fixed


@ruff_only
def test_ruff_safe_tier_preserves_feature_probe_import():
    """A try/except import probe must not be removed even with F401 active."""
    src_probe = (
        "try:\n    import readline\nexcept ImportError:\n    available = False\n"
    )
    assert _ruff_fix(src_probe, "m.py") == src_probe


@ruff_only
def test_ruff_safe_tier_keeps_unused_local():
    """F841 is unsafe (might delete attached comments); safe tier leaves it alone."""
    fixed = _ruff_fix("def f(x):\n    y = 1\n    return x\n", "m.py")
    assert "y = 1" in fixed


@ruff_only
def test_ruff_unsafe_tier_removes_unused_local():
    """The unsafe tier applies F841 fixes; F401 still works the same as safe."""
    fixed_unsafe = _ruff_fix(
        "def f(x):\n    y = 1\n    return x\n", "m.py", unsafe=True
    )
    assert "y = 1" not in fixed_unsafe


def test_ruff_safe_tier_collapses_nested_else_if():
    source = (
        "def f(a, b):\n"
        "    if a:\n        value = 1\n"
        "    else:\n        if b:\n            value = 2\n"
        "        else:\n            value = 3\n"
        "    return value\n"
    )
    fixed = _ruff_fix(source, "m.py")
    assert "elif b:" in fixed
    assert "else:\n        if b:" not in fixed


@ruff_only
def test_ruff_tier_preserves_optional_import_probe():
    src = "try:\n    import readline\nexcept ImportError:\n    available = False\n"
    assert _ruff_fix(src, "m.py") == src


@ruff_only
@ruff_only
def test_ruff_select_includes_the_loc_reducing_families():
    """`else` after `return` (RET505) is pure LOC and is why the default
    correctness-only rule set was not enough."""
    src = norm(
        """
        def f(x):
            if x:
                return 1
            else:
                return 2
        """
    )
    assert "else" not in _ruff_fix(src, "m.py")


@ruff_only
def test_ruff_tier_removes_a_loop_else_without_break():
    source = (
        "def f(items):\n"
        "    for item in items:\n"
        "        consume(item)\n"
        "    else:\n"
        "        finish()\n"
    )
    fixed = _ruff_fix(source, "m.py", unsafe=True)
    assert "    else:" not in fixed
    assert "    finish()" in fixed


def test_ruff_fix_returns_the_input_unchanged_when_ruff_is_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    src = "import os\n"
    assert _ruff_fix(src, "m.py") == src


# ---- D1: unreachable code --------------------------------------------------


def test_unreachable_after_return_is_removed():
    src = norm(
        """
        def f(x):
            return x
            print("never")
            print("also never")
        """
    )
    assert _python_drop_unreachable(src) == "def f(x):\n    return x\n"


def test_unreachable_after_raise_in_a_branch_is_removed():
    src = norm(
        """
        def f(x):
            if x:
                raise ValueError("no")
                cleanup()
            return 1
        """
    )
    out = _python_drop_unreachable(src)
    assert "cleanup()" not in out and "return 1" in out


def test_reachable_code_is_untouched():
    src = norm(
        """
        def f(x):
            if x:
                return 1
            return 2
        """
    )
    assert _python_drop_unreachable(src) == src


def test_module_level_code_after_return_is_not_considered():
    """Only function scope: `raise` at module level guards an import."""
    src = "raise SystemExit(1)\nprint('unreachable but module level')\n"
    assert _unreachable_line_spans(src) == []


def test_a_statement_sharing_the_return_line_is_left_alone():
    src = "def f():\n    return 1; print(2)\n"
    assert _python_drop_unreachable(src) == src


# ---- D2: JS ----------------------------------------------------------------


def test_js_static_does_not_format_files_without_rule_hits(tmp_path, monkeypatch):
    source = "export const answer = 42;\n"
    path = tmp_path / "index.js"
    path.write_text(source)

    def unexpected_lint(*_args, **_kwargs):
        raise AssertionError("lint fixer called for an unchanged file")

    monkeypatch.setattr("less_code.static._project_js_lint_fix", unexpected_lint)
    result = _js_static(tmp_path, [path], [path])
    assert result.changed_files == {}
    assert path.read_text() == source


def test_js_internal_spans_skips_exported_symbols():
    src = norm(
        """
        export function kept(x) {
          return x;
        }
        function helper(y) {
          return y;
        }
        const LOCAL = 3;
        export const SHARED = 4;
        """
    )
    names = {n for n, _s, _e in _js_internal_spans(src)}
    assert names == {"helper"}


def test_js_dead_internal_is_removed_but_a_referenced_one_is_kept(tmp_path):
    src = norm(
        """
        function used(x) {
          return x + 1;
        }
        function orphan(y) {
          return y - 1;
        }
        const UNUSED = 7;
        export function api(x) {
          return used(x);
        }
        """
    )
    path = tmp_path / "m.js"
    path.write_text(src)
    result = _js_remove_dead_internal({path: src}, {path: src})
    out = result.changed_files[str(path)]
    assert "orphan" not in out
    assert "UNUSED" in out  # its initializer could have import-time side effects
    assert "function used" in out and "export function api" in out


def test_js_internal_referenced_from_a_test_file_survives(tmp_path):
    src = "function helper(x) {\n  return x;\n}\nexport const A = 1;\n"
    path = tmp_path / "m.js"
    test = tmp_path / "m.test.js"
    path.write_text(src)
    test.write_text("// helper is exercised indirectly\n")
    result = _js_remove_dead_internal({path: src}, {path: src, test: test.read_text()})
    assert not result.changed_files


# ---- end to end through the pipeline ---------------------------------------


UNREACHABLE_MODULE = norm(
    """
    def area(w, h):
        if w <= 0:
            raise ValueError("bad width")
            print("cleanup")
        result = w * h
        return result
        print("dead")


    def label(flag):
        if flag:
            return "yes"
        else:
            return "no"
    """
)

UNREACHABLE_TESTS = norm(
    """
    import pytest
    from mod import area, label

    def test_area():
        assert area(2, 3) == 6

    def test_area_bad():
        with pytest.raises(ValueError):
            area(0, 1)

    def test_label():
        assert label(True) == "yes"
        assert label(False) == "no"
    """
)


@ruff_only
def test_pipeline_static_removes_unreachable_and_applies_ruff(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(UNREACHABLE_MODULE)
    (root / "test_mod.py").write_text(UNREACHABLE_TESTS)
    stats = shrink_project(root)
    source = (root / "mod.py").read_text()
    assert stats.tests_ok and stats.api_ok
    assert 'print("dead")' not in source and 'print("cleanup")' not in source
    assert "else:" not in source  # ruff RET505
    assert stats.loc_final < stats.loc_start


def test_static_pass_is_still_pure_without_a_runner(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(UNREACHABLE_MODULE)
    (root / "test_mod.py").write_text(UNREACHABLE_TESTS)
    project = map_project(root)
    result = static_pass(
        root, "python", project.source_files, project.source_files + project.test_files
    )
    assert result.changed_files
    assert 'print("dead")' in (root / "mod.py").read_text()
