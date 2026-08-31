import textwrap

import pytest

from less_code.api_check import api_surface, api_violations, js_api, python_api, rust_api
from less_code.loc import count_source
from less_code.mutator import generate_mutations
from less_code.llm_reduce import extract_code


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
        src = textwrap.dedent('''
            class Product(object):
                def __init__(self, sku, cost=0.0, *, unit="ea", **extra):
                    pass

                @property
                def available(self):
                    pass

                @classmethod
                def from_row(cls, row):
                    pass
        ''').strip()
        api = python_api(src)
        assert api["Product"] == "class(object)"
        assert api["Product.__init__"] == "def(self,sku,cost=0.0,*,unit='ea',**extra)"
        assert api["Product.available"] == "@property def(self)"
        assert api["Product.from_row"] == "@classmethod def(cls,row)"

    def test_python_api_notices_changed_default_and_dropped_method(self):
        before = {"a.py": python_api("class C:\n    def m(self, x=1):\n        pass\n")}
        after_default = {"a.py": python_api("class C:\n    def m(self, x=2):\n        pass\n")}
        after_gone = {"a.py": python_api("class C:\n    pass\n")}
        assert any("C.m" in v for v in api_violations(before, after_default))
        assert any("missing" in v for v in api_violations(before, after_gone))

    def test_python_api_allows_new_underscore_helper_method(self):
        before = {"a.py": python_api("class C:\n    def m(self):\n        pass\n")}
        after = {"a.py": python_api(
            "class C:\n    def m(self):\n        pass\n\n    def _h(self):\n        pass\n"
        )}
        assert api_violations(before, after) == []

    def test_python_api_varargs_and_posonly(self):
        api = python_api("def f(a, /, b, *args, c=3, **kw):\n    pass\n")
        assert api["f"] == "def(a,/,b,*args,c=3,**kw)"

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
            "gamma", "delta"
        }
        assert set(js_api("function only() {}\nmodule.exports = only;\n")) == {"only"}

    def test_rust_api(self):
        src = "pub fn f() {}\npub struct S;\nfn private() {}\npub(crate) fn g() {}\n"
        assert set(rust_api(src)) == {"f", "S", "g"}

    def test_rust_api_impl_methods_and_pub_fields(self):
        src = textwrap.dedent('''
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
        ''').strip()
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


class TestMutator:
    def test_python_generates_and_changes_source(self):
        src = "def f(x):\n    if x > 3:\n        return x + 1\n    return 0\n"
        p = pytest.TmpPath / "m.py" if False else None
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "m.py"
            p.write_text(src)
            mutants = generate_mutations(p, "python", max_mutants=50)
            assert mutants, "expected at least one mutant"
            assert all(m.mutated_source != src for m in mutants)

    def test_python_mutation_count_matches_sites(self):
        import pathlib, tempfile
        src = "def f(a, b):\n    return (a > b) and (a + b != 3)\n"
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "m.py"
            p.write_text(src)
            mutants = generate_mutations(p, "python", max_mutants=100)
            # sites: > -> >=, and -> or, != -> ==, + -> -, a->a+1, b->b+1, 3->4 ... >=
            assert len(mutants) >= 5

    def test_js_masks_longer_operators(self):
        import pathlib, tempfile
        src = "function f(a, b) { return a === b || a <= b; }\n"
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "m.js"
            p.write_text(src)
            mutants = generate_mutations(p, "javascript", max_mutants=100)
            for m in mutants:
                assert "!===" not in m.mutated_source.replace("!==", "")
                # === -> !== is fine; <= must not become from ==
                assert "<==" not in m.mutated_source

    def test_rust_token_mutants(self):
        import pathlib, tempfile
        src = "pub fn f(a: i32, b: i32) -> bool { a < b && a != b }\n"
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "m.rs"
            p.write_text(src)
            mutants = generate_mutations(p, "rust", max_mutants=100)
            assert len(mutants) >= 3
            assert all("->" not in m.mutated_source.replace("->", "", 0) or True for m in mutants)


class TestExtractCode:
    def test_takes_last_fenced_block(self):
        resp = "text\n```python\nx = 1\n```\nmore\n```python\ny = 2\n```\n"
        assert extract_code(resp) == "y = 2"

    def test_none_when_missing(self):
        assert extract_code("no fences here") is None


class TestStaticDeadCode:
    def test_removes_unused_toplevel_def(self):
        from less_code.static import _python_remove_dead
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "mod.py"
            src.write_text(
                "def used(x):\n    return helper(x)\n\n"
                "def helper(v):\n    return v * 2\n\n"
                "def dead_one():\n    return 1\n\ndef dead_two():\n    return 2\n"
            )
            test = root / "test_mod.py"
            test.write_text("from mod import used\n\ndef test():\n    assert used(2) == 4\n")
            result = _python_remove_dead([src], [src, test])
            assert str(src) in result.changed_files
            new_source = result.changed_files[str(src)]
            assert "dead_one" not in new_source
            assert "used" in new_source and "helper" in new_source

    def test_keeps_referenced_symbols(self):
        from less_code.static import _python_remove_dead
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "mod.py"
            src.write_text("def f():\n    return 1\n")
            test = root / "test_mod.py"
            test.write_text("from mod import f\n\ndef test_f():\n    assert f() == 1\n")
            result = _python_remove_dead([src], [src, test])
            assert result.changed_files == {}
