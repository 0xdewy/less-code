import textwrap
from pathlib import Path

import pytest

from less_code.api_check import api_violations, js_api, python_api, rust_api
from less_code.langdetect import map_project
from less_code.loc import count_source
from less_code.testrunners import _probe_import, shadowed_imports


class TestEntryPointTreesAreNotLibraries:
    """docs/, examples/ and benchmarks/ files are invoked by name (sphinx
    conf.py, nox sessions, demo scripts) — test references say nothing about
    them, so they are outside the reduction scope entirely."""

    def test_map_project_skips_docs_examples_benchmarks(self, tmp_path):
        root = tmp_path / "proj"
        pkg = root / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        for rel in ("examples/demo.py", "benchmarks/bench.py", "docs/conf.py"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x = 1\n")
        project = map_project(root)
        assert [p.name for p in project.source_files] == ["__init__.py"]


class TestIsTestFile:
    """Convention-based classification. click's `testing.py` (public API,
    CliRunner) was classified as a test by the old `"test" in name` substring
    rule — excluded from reduction targets AND from the gate's API surface."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            # python conventions
            ("test_inventory.py", True),
            ("inventory_test.py", True),
            ("conftest.py", True),
            ("testing.py", False),  # click's CliRunner module: SOURCE
            ("latest.py", False),  # substring 'test' is not a convention
            ("_compat.py", False),
            # js/ts conventions
            ("reporting.test.js", True),
            ("reporting.spec.ts", True),
            ("contest.js", False),  # substring, not a suffix convention
            ("latest.js", False),
            ("test-utils.ts", True),  # '-test'/-spec kebab IS mocha style
            ("utils_test.mjs", True),
            # rust: unit tests are #[cfg(test)] mods; file names carry no signal
            ("integration.rs", False),
        ],
    )
    def test_name_conventions(self, name, expected):
        from less_code.langdetect import is_test_file

        assert is_test_file(Path("src/pkg") / name) is expected

    @pytest.mark.parametrize(
        "rel",
        [
            "tests/test_basic.py",  # pytest / cargo integration tests
            "test/test_x.py",  # mocha's default dir
            "src/__tests__/a.test.js",  # jest
            "tests/utils/helper.py",  # everything under a test dir is test
        ],
    )
    def test_test_directories(self, rel):
        from less_code.langdetect import is_test_file

        assert is_test_file(Path("proj") / rel) is True

    def test_testing_directory_is_not_a_test_directory(self, tmp_path):
        """numpy.testing / click.testing ship under `testing/` — that dir name
        says nothing about the file being a test."""
        from less_code.langdetect import is_test_file

        assert is_test_file(Path("proj/testing/utils.py")) is False

    def test_click_testing_module_maps_to_source(self, tmp_path):
        """The realized bug: a shipped public-API module excluded from both the
        reduction targets and the enforced API surface."""
        pkg = tmp_path / "src" / "click"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "testing.py").write_text("class CliRunner:\n    pass\n")
        project = map_project(tmp_path)
        assert [p.name for p in project.source_files] == ["__init__.py", "testing.py"]
        assert project.test_files == []


class TestShadowedImports:
    def _pkg(self, tmp_path):
        pkg = tmp_path / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        return tmp_path

    def test_flags_a_unit_resolved_outside_the_tree(self, tmp_path):
        root = self._pkg(tmp_path)
        probe = lambda _root, name: f"/venv/site-packages/{name}/__init__.py"
        assert shadowed_imports(root, probe) == [
            "pkg -> /venv/site-packages/pkg/__init__.py"
        ]

    def test_src_layout_resolving_inside_the_tree_is_clean(self, tmp_path):
        root = self._pkg(tmp_path)

        def probe(_root, name):
            assert name == "pkg"
            return str(root / "src" / "pkg" / "__init__.py")

        assert shadowed_imports(root, probe) == []

    def test_a_failed_or_namespace_import_is_not_flagged(self, tmp_path):
        root = self._pkg(tmp_path)
        assert shadowed_imports(root, lambda _r, _n: None) == []

    def test_real_probe_resolves_a_flat_module_from_cwd(self, tmp_path):
        (tmp_path / "mod.py").write_text("X = 1\n")
        assert _probe_import(tmp_path, "mod").endswith("mod.py")
        assert shadowed_imports(tmp_path) == []


class TestLoc:
    def test_python_counts_code_not_comments_or_docstrings(self):
        src = textwrap.dedent('''
            """Module docstring."""
            # a comment
            def f(x):
                """Doc."""
                # inline comment
                y = x + 1  # trailing comment
                return y
        ''').strip()
        loc = count_source(src, "python")
        assert loc.code == 3  # def, y=, return
        assert loc.comment >= 4
        assert loc.blank == 0

    def test_python_multiline_string_in_expr_is_code(self):
        src = "x = ('a'\n     'b')\n"
        assert count_source(src, "python").code == 2

    def test_js_comments(self):
        src = "// hi\nconst a = 1;\n/* block\nstill block */\nlet b = 2;\n"
        loc = count_source(src, "javascript")
        assert loc.code == 2
        assert loc.comment == 3

    def test_rust_comments(self):
        src = "/// doc\nfn a() {}\n// c\n/* b */\nfn b() {}\n"
        loc = count_source(src, "rust")
        assert loc.code == 2

    def test_stripping_only_comments_yields_zero_reduction(self):
        before = "def f():\n    # c\n    return 1\n"
        after = "def f():\n    return 1\n"
        assert count_source(before, "python").code == count_source(after, "python").code


class TestApiCheck:
    def test_python_api_extraction(self):
        src = "def f(a, b=1):\n    pass\n\nclass C:\n    pass\n\n_helper = 1\n"
        api = python_api(src)
        assert api == {"f": "def(a,b=1)", "C": "class"}

    def test_python_api_includes_methods_with_defaults(self):
        src = textwrap.dedent("""
            class Product(object):
                def __init__(self, sku, cost=0.0, *, unit="ea", **extra):
                    pass

                @property
                def available(self):
                    pass

                @classmethod
                def from_row(cls, row):
                    pass
        """).strip()
        api = python_api(src)
        assert api["Product"] == "class(object)"
        assert api["Product.__init__"] == "def(self,sku,cost=0.0,*,unit='ea',**extra)"
        assert api["Product.available"] == "@property def(self)"
        assert api["Product.from_row"] == "@classmethod def(cls,row)"

    def test_python_api_notices_changed_default_and_dropped_method(self):
        before = {"a.py": python_api("class C:\n    def m(self, x=1):\n        pass\n")}
        after_default = {
            "a.py": python_api("class C:\n    def m(self, x=2):\n        pass\n")
        }
        after_gone = {"a.py": python_api("class C:\n    pass\n")}
        assert any("C.m" in v for v in api_violations(before, after_default))
        assert any("missing" in v for v in api_violations(before, after_gone))

    def test_python_api_allows_new_underscore_helper_method(self):
        before = {"a.py": python_api("class C:\n    def m(self):\n        pass\n")}
        after = {
            "a.py": python_api(
                "class C:\n    def m(self):\n        pass\n\n    def _h(self):\n        pass\n"
            )
        }
        assert api_violations(before, after) == []

    def test_python_api_varargs_and_posonly(self):
        api = python_api("def f(a, /, b, *args, c=3, **kw):\n    pass\n")
        assert api["f"] == "def(a,/,b,*args,c=3,**kw)"

    def test_python_api_preserves_annotations(self):
        before = python_api("def f(value: int) -> str:\n    return str(value)\n")
        after = python_api("def f(value: float) -> str:\n    return str(value)\n")
        assert api_violations({"a.py": before}, {"a.py": after})

    def test_violations_detect_removal_rename_signature(self):
        before = {"a.py": {"f": "def(x)", "g": "def()"}}
        after_removed = {"a.py": {"g": "def()"}}
        after_changed = {"a.py": {"f": "def(x,y)", "g": "def()"}}
        assert any("missing" in v for v in api_violations(before, after_removed))
        assert any("changed" in v for v in api_violations(before, after_changed))
        assert api_violations(before, {"a.py": {"f": "def(x)", "g": "def()"}}) == []

    def test_js_api(self):
        src = "export function f() {}\nexport const g = 1;\nconst h = 2;\n"
        assert set(js_api(src)) == {"f", "g"}

    def test_js_api_preserves_exported_function_signature(self):
        before = js_api("export function f(value, radix = 10) {}\n")
        after = js_api("export function f(value) {}\n")
        assert api_violations({"a.js": before}, {"a.js": after})

    def test_js_api_export_list_and_default(self):
        src = (
            "function a() {}\nfunction b() {}\nconst c = 3;\n"
            "export { a, b as bee };\nexport default c;\n"
        )
        assert set(js_api(src)) == {"a", "bee", "default"}

    def test_js_api_export_default_function(self):
        assert "default" in js_api("export default function run() {}\n")

    def test_js_api_commonjs_forms(self):
        obj = "module.exports = { alpha, beta: inner };\n"
        assert set(js_api(obj)) == {"alpha", "beta"}
        assert set(js_api("module.exports.gamma = 1;\nexports.delta = 2;\n")) == {
            "gamma",
            "delta",
        }
        assert set(js_api("function only() {}\nmodule.exports = only;\n")) == {"only"}

    def test_rust_api(self):
        src = "pub fn f() {}\npub struct S;\nfn private() {}\npub(crate) fn g() {}\n"
        assert set(rust_api(src)) == {"f", "S", "g"}

    def test_rust_api_preserves_top_level_function_signature(self):
        before = rust_api("pub fn f(value: usize) -> bool { true }\n")
        after = rust_api("pub fn f(value: u64) -> bool { true }\n")
        assert api_violations({"a.rs": before}, {"a.rs": after})

    def test_rust_api_impl_methods_and_pub_fields(self):
        src = textwrap.dedent("""
            pub struct Stats {
                pub words: usize,
                hidden: usize,
            }

            impl Stats {
                pub fn new(words: usize) -> Self { Stats { words, hidden: 0 } }
                fn secret(&self) -> usize { self.hidden }
            }

            impl fmt::Display for Stats {
                fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { Ok(()) }
            }
        """).strip()
        api = rust_api(src)
        assert api["Stats"] == "pub"
        assert api["Stats.words"] == "pub usize"
        assert "Stats.hidden" not in api
        assert api["Stats::new"] == "pub fn(words: usize)"
        assert "Stats::secret" not in api

    def test_rust_api_flags_changed_method_signature(self):
        before = {"l.rs": rust_api("impl S {\n    pub fn m(&self, a: i32) {}\n}\n")}
        after = {"l.rs": rust_api("impl S {\n    pub fn m(&self, a: i64) {}\n}\n")}
        assert any("S::m" in v for v in api_violations(before, after))


class TestStaticDeadCode:
    def test_removes_unused_toplevel_def(self):
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "__init__.py").write_text("")
            src = root / "mod.py"
            src.write_text(
                "def used(x):\n    return helper(x)\n\n"
                "def helper(v):\n    return v * 2\n\n"
                "def _dead_one():\n    return 1\n\ndef _dead_two():\n    return 2\n"
            )
            test = root / "test_mod.py"
            test.write_text(
                "from mod import used\n\ndef test():\n    assert used(2) == 4\n"
            )
            result = _python_remove_dead([src], [src, test])
            assert str(src) in result.changed_files
            new_source = result.changed_files[str(src)]
            assert "_dead_one" not in new_source
            assert "used" in new_source and "helper" in new_source

    def test_keeps_unreferenced_public_symbol(self):
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "mod.py"
            source = "def public_hook():\n    return 1\n"
            src.write_text(source)
            result = _python_remove_dead([src], [src])
            assert result.changed_files == {}

    def test_keeps_private_definitions_with_import_time_effects(self):
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "__init__.py").write_text("")
            src = root / "mod.py"
            src.write_text(
                "def _default(value=effect()):\n    return value\n\n"
                "@decorate\ndef _decorated():\n    return 1\n\n"
                "class _Class:\n    effect()\n"
            )
            assert _python_remove_dead([src], [src]).changed_files == {}

    def test_keeps_referenced_symbols(self):
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "mod.py"
            src.write_text("def f():\n    return 1\n")
            test = root / "test_mod.py"
            test.write_text("from mod import f\n\ndef test_f():\n    assert f() == 1\n")
            result = _python_remove_dead([src], [src, test])
            assert result.changed_files == {}

    def test_entry_point_files_are_never_dead_stripped(self):
        """noxfile/setup/tasks functions are invoked BY NAME from CI configs,
        never imported — 'unreferenced' must not delete them (found on
        pypa/packaging: lint and release_build sessions were removed)."""
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "__init__.py").write_text("")
            noxfile = root / "noxfile.py"
            noxfile.write_text(
                "def lint(session):\n    session.install('ruff')\n\n"
                "def release_build(session):\n    session.install('build')\n"
            )
            mod = root / "mod.py"
            mod.write_text("def used():\n    return 1\n\ndef _dead():\n    return 2\n")
            test = root / "test_mod.py"
            test.write_text("from mod import used\n\ndef test():\n    assert used()\n")
            result = _python_remove_dead([noxfile, mod], [noxfile, mod, test])
            assert str(noxfile) not in result.changed_files
            assert "release_build" in noxfile.read_text()
            assert str(mod) in result.changed_files  # package modules still work

    def test_standalone_scripts_are_never_dead_stripped(self):
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            script = pathlib.Path(td) / "bench.py"
            script.write_text("def _main():\n    return 1\n")
            assert _python_remove_dead([script], [script]).changed_files == {}

    def test_removal_is_text_surgical_not_ast_unparse(self):
        """unparse rewrote the whole file: every comment died and every string
        flipped to single quotes (found by dogfooding the tool on its own
        repo — llm_reduce.py lost all 52 of its comments)."""
        import pathlib
        import tempfile

        from less_code.static import _python_remove_dead

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "__init__.py").write_text("")
            src = root / "mod.py"
            src.write_text(
                '"""Module doc."""\n'
                "# keep me: comment on a kept def\n"
                "def used(x):\n"
                '    return "double" + helper(x)  # trailing comment\n'
                "\n"
                "# header of the dead one, attached above a blank\n"
                "\n"
                "def _dead_one():\n"
                "    return 1\n"
            )
            test = root / "test_mod.py"
            test.write_text("from mod import used\n\ndef test():\n    assert used(1)\n")
            result = _python_remove_dead([src], [src, test])
            new_source = result.changed_files[str(src)]
            assert "_dead_one" not in new_source
            assert "# keep me: comment on a kept def" in new_source
            assert "# trailing comment" in new_source
            assert 'return "double"' in new_source  # quotes untouched
            assert "header of the dead one" not in new_source  # attached header goes
            assert new_source.endswith(
                '    return "double" + helper(x)  # trailing comment\n'
            )


def test_ml_extract_symbols_private_only_python():
    from less_code.ml_shrink import extract_symbols

    source = (
        "def public_a(x):\n    return x\n"
        "\n"
        "def _private_b(x):\n    return x\n"
        "\n"
        "class PublicC:\n    def method(self):\n        return 1\n"
        "\n"
        "class _PrivateD:\n    def method(self):\n        return 1\n"
    )
    symbols = extract_symbols(source, "python")
    names = [s["name"] for s in symbols]
    assert names == ["_private_b", "_PrivateD.method"]


def test_ml_extract_symbols_private_only_javascript():
    from less_code.ml_shrink import extract_symbols

    source = (
        "export function publicFn() { return 1; }\n"
        "function _privateFn() { return 1; }\n"
    )
    symbols = extract_symbols(source, "javascript")
    names = [s["name"] for s in symbols]
    assert names == ["_privateFn"]


def test_ml_extract_symbols_private_only_rust():
    from less_code.ml_shrink import extract_symbols

    source = "pub fn public_fn() -> i32 { 1 }\nfn _private_fn() -> i32 { 1 }\n"
    symbols = extract_symbols(source, "rust")
    names = [s["name"] for s in symbols]
    assert names == ["_private_fn"]


def test_ml_extract_symbols_include_public_for_diagnostics():
    from less_code.ml_shrink import extract_symbols

    source = "def public_fn(x):\n    return x\n\ndef _private_fn(x):\n    return x\n"
    public = extract_symbols(source, "python", private_only=False)
    names = [s["name"] for s in public]
    assert names == ["public_fn", "_private_fn"]


def test_ml_extract_symbols_max_per_file_caps():
    from less_code.ml_shrink import extract_symbols

    source = "\n".join(f"def _f{i}(x):\n    return x\n" for i in range(50))
    capped = extract_symbols(source, "python", max_per_file=5)
    assert len(capped) == 5
    assert [s["name"] for s in capped] == [f"_f{i}" for i in range(5)]


def test_model_ranking_context_and_declaration_contract(tmp_path):
    from less_code.ml_shrink import (
        declaration_preserved,
        extract_symbols,
        ranked_targets,
        symbol_context,
    )

    small = tmp_path / "a.py"
    large = tmp_path / "z.py"
    sources = {
        small: "def _tiny():\n    return 1\n",
        large: "import math\ndef _large(value):\n    a = math.sqrt(value)\n    b = a + 1\n    return b\n",
    }
    assert ranked_targets(sources, "python")[0] == (large, "_large")
    symbol = extract_symbols(sources[large], "python")[0]
    context = symbol_context(
        tmp_path,
        large,
        symbol,
        sources,
        {tmp_path / "test_a.py": "assert _large(4) == 3\n" * 3000},
    )
    assert sum(len(item["text"]) for item in context) <= 12000
    assert any(item["kind"] == "imports" for item in context)
    assert any(item["kind"] == "test reference" for item in context)
    assert declaration_preserved(
        "def _f(x=1):\n    return x\n", "def _f(x=1):\n    return 2\n", "python"
    )
    assert not declaration_preserved(
        "@decorator\ndef _f(x=1):\n    return x\n",
        "def _f(x=1):\n    return x\n",
        "python",
    )
    assert not declaration_preserved(
        "def _f(x=1):\n    return x\n", "def _f(x=2):\n    return x\n", "python"
    )
    assert not declaration_preserved(
        "def _f(x):\n    return x\n",
        "def _f(x):\n    return x\ndef added():\n    pass\n",
        "python",
    )


def test_model_edit_rejects_stale_source():
    import pytest

    from less_code.ml_shrink import ProposedEdit, apply_edit, extract_symbols

    source = "def _f():\n    return 1\n"
    symbol = extract_symbols(source, "python")[0]
    with pytest.raises(ValueError, match="source has changed"):
        apply_edit(source.replace("1", "2"), symbol, ProposedEdit(symbol["id"], source))


@pytest.mark.parametrize(
    "before,after,lang,allowed",
    [
        (
            "function f({x = 1}) { return x; }",
            "function f({x = 2}) { return x; }",
            "javascript",
            False,
        ),
        (
            "method(x) { const y = x; return y; }",
            "method(x) { return x; }",
            "javascript",
            True,
        ),
        (
            "fn f(x: i32) -> i32 { let y = x; y }",
            "fn f(x: i32) -> i32 { x }",
            "rust",
            True,
        ),
        ("fn f(x: i32) -> i32 { x }", "fn f(x: i64) -> i64 { x }", "rust", False),
    ],
)
def test_model_nonpython_declaration_contract(before, after, lang, allowed):
    from less_code.ml_shrink import declaration_preserved

    assert declaration_preserved(before, after, lang) is allowed


@pytest.mark.parametrize(
    "lang,before,after",
    [
        (
            "javascript",
            'function f(x = "a b") { return x; }',
            'function f ( x = "a b" ){return x;}',
        ),
        ("rust", "fn f(x: i32) -> i32 { x }", "fn f ( x:i32 )->i32{x}"),
    ],
)
def test_model_declaration_ignores_only_intertoken_whitespace(lang, before, after):
    from less_code.ml_shrink import declaration_preserved

    assert declaration_preserved(before, after, lang)
    if lang == "javascript":
        assert not declaration_preserved(before, after.replace("a b", "ab"), lang)


def test_model_ranking_does_not_reward_long_docstrings(tmp_path):
    from less_code.ml_shrink import ranked_targets

    path = tmp_path / "module.py"
    source = (
        'def _documented():\n    """'
        + "documentation\n" * 100
        + '    """\n    return 1\n\n'
        + "def _work(x):\n    a = x + 1\n    b = a * 2\n    return b\n"
    )
    assert ranked_targets({path: source}, "python")[0] == (path, "_work")


def test_decorated_method_ranking_and_declaration(tmp_path):
    from less_code.ml_shrink import (
        declaration_preserved,
        extract_symbols,
        ranked_targets,
    )

    source = "class A:\n    @property\n    def _value(self):\n        return 1\n"
    symbol = extract_symbols(source, "python")[0]
    replacement = "@property\ndef _value(self):\n    return 2"
    assert declaration_preserved(symbol["text"], replacement, "python")
    assert ranked_targets({tmp_path / "a.py": source}, "python")
    assert not declaration_preserved(
        symbol["text"], replacement.replace("@property\n", ""), "python"
    )


@pytest.mark.parametrize(
    "model,reasoning", [("qwen2.5-coder:7b", False), ("qwen3:8b", True)]
)
def test_ollama_adapter_has_explicit_reasoning_budget(
    monkeypatch, capsys, model, reasoning
):
    import io
    import json

    from bench import ollama

    monkeypatch.setattr(ollama.sys, "argv", ["ollama.py", model])
    monkeypatch.setattr(
        ollama.sys,
        "stdin",
        io.StringIO(json.dumps({"system": "contract", "source": "code"})),
    )

    def respond(request, timeout):
        payload = json.loads(request.data)
        assert payload.get("think", False) == reasoning
        assert payload["options"]["num_predict"] == (4096 if reasoning else 2048)
        assert timeout == (270 if reasoning else 90)
        return io.StringIO(json.dumps({"response": "null"}))

    monkeypatch.setattr(ollama.urllib.request, "urlopen", respond)
    ollama.main()
    assert capsys.readouterr().out.strip() == "null"


def test_rust_inline_test_items_are_never_model_targets():
    from less_code.ml_shrink import extract_symbols
    from less_code.rust_rules import test_spans

    source = (
        "fn helper() {}\n#[test]\nfn regression() {}\n"
        "#[cfg(test)]\nmod checks { fn helper_for_tests() {} }\n"
        "mod tests { fn another_helper() {} }\n"
    )
    assert [s["name"] for s in extract_symbols(source, "rust")] == ["helper"]
    assert len(test_spans(source)) == 3


def test_rust_rules_leave_inline_test_helpers_unchanged():
    from less_code.rust_rules import apply_rules

    helper = (
        "fn count(s: &str) -> usize {\n    let mut count = 0usize;\n"
        "    for _ in s.chars() {\n        count = count + 1;\n    }\n    count\n}\n"
    )
    reduced, applied = apply_rules(helper)
    assert applied == ["for-count-to-method"]
    assert "let count = s.chars().count();" in reduced
    assert "count\n}" in reduced
    source = "#[cfg(test)]\nmod tests {\n" + helper + "}\n"
    assert apply_rules(source) == (source, [])


def test_rust_inline_single_use_binding():
    from less_code.rust_rules import apply_rules

    source = """fn divide(n: u128, divisor: u128) -> (u128, u64) {
    let quotient = n / divisor;
    let remainder = n - quotient * divisor;
    (quotient, remainder as u64)
}
"""
    reduced, applied = apply_rules(source, {"inline-single-use-binding"})
    assert applied == ["inline-single-use-binding"]
    assert "(quotient, (n - quotient * divisor) as u64)" in reduced


def test_rust_inline_single_use_binding_keeps_types_and_later_uses():
    from less_code.rust_rules import apply_rules

    typed = "fn f() -> u64 {\n    let value: u64 = 1;\n    value\n}\n"
    assert apply_rules(typed, {"inline-single-use-binding"}) == (typed, [])

    reused = "fn f() {\n    let value = make();\n    consume(value);\n    inspect(value);\n}\n"
    assert apply_rules(reused, {"inline-single-use-binding"}) == (reused, [])


def test_cli_model_protocol_with_context_and_feedback():
    import shlex
    import sys

    from less_code.ml_shrink import CliBackend, extract_symbols

    symbol = extract_symbols("def _f(x):\n    return x\n", "python")[0]
    symbol["context"] = [{"text": "CONTEXT"}]
    symbol["feedback"] = {"reason": "tests failed"}
    program = (
        "import json,sys; p=json.load(sys.stdin); "
        "assert p['context'][0]['text']=='CONTEXT'; "
        "assert p['feedback']['reason']=='tests failed'; "
        "print(json.dumps({'symbol_id':p['symbol_id'],'replacement':p['source']}))"
    )
    backend = CliBackend(f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}")
    assert backend.propose("python", symbol).replacement == symbol["text"]
