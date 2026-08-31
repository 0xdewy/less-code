"""Roadmap A2/A3 + B1/B3/B4: bench, hidden tests, formatted LOC, pre-gates, cache."""

import json
import pathlib
import textwrap

import pytest

from less_code import testrunners
from less_code.bench import discover, markdown_table, run_bench
from less_code.langdetect import map_project
from less_code.llm_reduce import verify_candidate
from less_code.loc import count_source, formatter_available, measure
from less_code.testrunners import cache_clear, gate_command, run_hidden_tests, run_tests

MODULE = textwrap.dedent('''
    def add(a, b):
        total = a + b
        return total


    def dead_never_used(x):
        return x * 3
''').strip() + "\n"

TESTS = "from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
HIDDEN = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n\n"
    "from mod import add\n\n\ndef test_add_negative():\n    assert add(-1, -2) == -3\n"
)


def _project(tmp_path: pathlib.Path, hidden: bool = True) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "tests_hidden").mkdir(parents=True)
    (root / "mod.py").write_text(MODULE)
    (root / "test_mod.py").write_text(TESTS)
    if hidden:
        (root / "tests_hidden" / "test_hidden.py").write_text(HIDDEN)
    else:
        (root / "tests_hidden").rmdir()
    return root


class TestCanonicalLoc:
    def test_line_joining_does_not_shrink_the_metric(self):
        if not formatter_available("python"):
            pytest.skip("ruff not installed")
        verbose = "def f(x):\n    y = x + 1\n    return y\n"
        joined = "def f(x):\n    y = x + 1; return y\n"
        assert count_source(joined, "python").code < count_source(verbose, "python").code
        assert measure(joined, "python").code == measure(verbose, "python").code

    def test_formatted_flag_records_whether_a_formatter_ran(self):
        loc = measure("def f(x):\n    return x\n", "python")
        assert loc.formatted is formatter_available("python")
        assert count_source("def f(x):\n    return x\n", "python").formatted is False

    def test_unparsable_source_falls_back_to_raw_count(self):
        loc = measure("def broken(:\n", "python")
        assert loc.formatted is False
        assert loc.ast_nodes == 0

    def test_secondary_metrics_are_populated(self):
        loc = measure("def f(x):\n    return x + 1\n", "python")
        assert loc.tokens > 5
        assert loc.ast_nodes > 5
        js = measure("function f(x) {\n  return x;\n}\n", "javascript")
        assert js.tokens > 5 and js.ast_nodes == js.tokens  # token proxy


class TestHiddenTests:
    def test_hidden_tests_are_not_part_of_the_project_map(self, tmp_path):
        root = _project(tmp_path)
        project = map_project(root)
        assert all("tests_hidden" not in str(p) for p in project.source_files)
        assert all("tests_hidden" not in str(p) for p in project.test_files)

    def test_frozen_gate_excludes_the_hidden_directory(self, tmp_path):
        root = _project(tmp_path)
        assert any("--ignore" in part for part in gate_command(root, "python"))
        assert not any("--ignore" in part for part in gate_command(_project(tmp_path / "b", hidden=False), "python"))

    def test_hidden_suite_runs_and_reports(self, tmp_path):
        root = _project(tmp_path)
        result = run_hidden_tests(root, "python")
        assert result is not None and result.ok

    def test_missing_hidden_suite_is_none(self, tmp_path):
        assert run_hidden_tests(_project(tmp_path, hidden=False), "python") is None

    def test_hidden_suite_catches_a_regression_the_gate_missed(self, tmp_path):
        root = _project(tmp_path)
        # visible suite only pins add(1, 2); this "reduction" still passes it
        (root / "mod.py").write_text("def add(a, b):\n    return abs(a) + abs(b)\n")
        assert run_tests(root, "python", use_cache=False).ok
        assert not run_hidden_tests(root, "python").ok

    def test_every_fixture_ships_hidden_tests(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        for name in ("py", "js", "rs"):
            fixture = repo / "fixtures" / name
            assert (fixture / "tests_hidden").is_dir(), name


class TestResultCache:
    def test_identical_tree_is_only_tested_once(self, tmp_path):
        root = _project(tmp_path)
        cache_clear()
        first = run_tests(root, "python")
        second = run_tests(root, "python")
        assert first.ok and second.ok
        assert first.cached is False and second.cached is True
        assert testrunners.STATS == {"hits": 1, "misses": 1}

    def test_editing_a_source_file_invalidates_the_entry(self, tmp_path):
        root = _project(tmp_path)
        cache_clear()
        run_tests(root, "python")
        (root / "mod.py").write_text(MODULE + "\n\ndef extra():\n    return 1\n")
        assert run_tests(root, "python").cached is False
        assert testrunners.STATS["misses"] == 2

    def test_bypass_flag_skips_the_cache(self, tmp_path):
        root = _project(tmp_path)
        cache_clear()
        run_tests(root, "python")
        assert run_tests(root, "python", use_cache=False).cached is False
        assert testrunners.STATS["hits"] == 0


class TestPreGates:
    """B3: not-smaller -> parse -> API -> tests, each recorded by name."""

    def setup_method(self):
        self.ran = []

    def runner(self, root, lang):
        self.ran.append(root)
        return run_tests(root, lang)

    def _args(self, tmp_path):
        root = _project(tmp_path)
        path = root / "mod.py"
        from less_code.api_check import python_api

        source = path.read_text()
        return root, path, python_api(source), measure(source, "python").code

    def test_not_smaller_short_circuits_before_any_test_run(self, tmp_path):
        root, path, api, loc = self._args(tmp_path)
        outcome, _loc = verify_candidate(
            path, "python", MODULE + "\n\ndef more():\n    return 1\n", api, loc,
            self.runner, root,
        )
        assert outcome == "not-smaller"
        assert self.ran == []

    def test_syntax_error_is_caught_before_the_suite(self, tmp_path):
        root, path, api, loc = self._args(tmp_path)
        outcome, _loc = verify_candidate(
            path, "python", "def add(a, b):\n    return (a + b\n", api, loc,
            self.runner, root,
        )
        assert outcome.startswith("syntax-error")
        assert self.ran == []
        assert path.read_text() == MODULE  # reverted

    def test_api_change_is_caught_before_the_suite(self, tmp_path):
        root, path, api, loc = self._args(tmp_path)
        outcome, _loc = verify_candidate(
            path, "python", "def add(a, b, c=1):\n    return a + b\n", api, loc,
            self.runner, root,
        )
        assert outcome.startswith("api-changed")
        assert self.ran == []

    def test_only_a_plausible_candidate_pays_for_tests(self, tmp_path):
        root, path, api, loc = self._args(tmp_path)
        outcome, loc_after = verify_candidate(
            path, "python",
            "def add(a, b):\n    return a + b\n\n\ndef dead_never_used(x):\n    return x * 3\n",
            api, loc, self.runner, root,
        )
        assert outcome == "accepted" and loc_after < loc
        assert len(self.ran) == 1
        assert path.read_text() == MODULE  # verify always reverts


class TestBench:
    def test_bench_runs_static_only_and_writes_a_row(self, tmp_path):
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        root = _project(fixtures)
        assert discover(fixtures) == [root]
        rows, out = run_bench(fixtures, tmp_path / "results", config="static-only")
        assert len(rows) == 1
        row = rows[0]
        assert row.lang == "python"
        assert row.tests_ok and row.api_ok and row.hidden_ok is True
        assert row.loc_final < row.loc_start  # dead_never_used removed
        assert row.llm_calls == 0 and row.seconds >= 0
        assert row.static_pct > 0 and row.commit
        written = [json.loads(line) for line in out.read_text().splitlines()]
        assert written[0]["fixture"] == "proj"
        assert set(written[0]) >= {
            "lang", "loc_start", "loc_after_static", "loc_final", "static_pct",
            "hybrid_pct", "tests_ok", "api_ok", "hidden_ok", "llm_calls", "seconds",
        }

    def test_bench_never_mutates_the_source_fixture(self, tmp_path):
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        root = _project(fixtures)
        before = (root / "mod.py").read_text()
        run_bench(fixtures, tmp_path / "results", config="static-only")
        assert (root / "mod.py").read_text() == before

    def test_markdown_table_has_a_row_per_fixture(self, tmp_path):
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        _project(fixtures)
        rows, _out = run_bench(fixtures, tmp_path / "results", config="static-only")
        table = markdown_table(rows)
        lines = table.splitlines()
        assert lines[0].startswith("| fixture |") and "hidden" in lines[0]
        assert len(lines) == 3
        assert "| proj | python | static-only |" in lines[2]
