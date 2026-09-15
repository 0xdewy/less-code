"""The differential oracle: original bodies shadow rewrites during the suite."""

import sys
from pathlib import Path

from less_code.shadow import changed_functions, run_shadow

LIB = """
class Box:
    def __init__(self, value):
        self.value = value

    @property
    def double(self):
        return self.value * 2

    def bump(self, items):
        items.append(1)
        return len(items)


def add(a, b):
    return a + b


def evens(n):
    for i in range(n):
        yield i * 2


def first_over(values, limit):
    for v in values:
        if v > limit:
            return v
    return None
"""

# Weak tests: they call everything but assert almost nothing, so a wrong
# rewrite stays green and only the oracle can catch it.
TESTS = """
from lib import Box, add, evens, first_over

def test_calls():
    assert add(1, 2)
    assert list(evens(3))
    assert Box(2).double
    assert Box(2).bump([])
    assert first_over(iter([1, 5, 9]), 4)
    assert first_over(iter([1]), 4) is None
"""

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


# ---- Rust oracle (bounded v1) ----

import shutil

from less_code.rust_shadow import changed_functions as rust_changed_functions
from less_code.rust_shadow import run_rust_shadow

RS_COUNT_SPACES = """pub fn count_spaces(text: &str) -> usize {
    let mut count = 0usize;
    for c in text.chars() {
        if c == ' ' {
            count = count + 1;
        } else {
            break;
        }
    }
    count
}
"""

RS_COUNT_SPACES_EQUAL = """pub fn count_spaces(text: &str) -> usize {
    text.chars().take_while(|&c| c == ' ').count()
}
"""

RS_COUNT_SPACES_CAPPED = """pub fn count_spaces(text: &str) -> usize {
    text.chars().take_while(|&c| c == ' ').take(10).count()
}
"""


def _rs_project(tmp_path: Path, fn_text: str) -> tuple[Path, Path, str]:
    """Copy the pinned fixture (cargo keeps its incremental target/) and
    append the function under test to lib.rs, keeping the crate's own tests
    compiling."""
    root = tmp_path / "rs-shadow"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "rs", root)
    lib = root / "src" / "lib.rs"
    base = lib.read_text()
    lib.write_text(base + fn_text)
    return root, lib, base + fn_text


def test_rust_equal_rewrite_is_verified(tmp_path):
    root, lib, pristine_text = _rs_project(tmp_path, RS_COUNT_SPACES)
    lib.write_text(pristine_text.replace(RS_COUNT_SPACES, RS_COUNT_SPACES_EQUAL))
    spec = rust_changed_functions({lib: pristine_text}, {lib: lib.read_text()})
    assert set(spec[str(lib)]) == {"count_spaces"}
    report = run_rust_shadow(root, spec, ["cargo", "test", "--quiet"], 600)
    assert report.tests_ok
    assert not report.mismatches
    assert report.exercised == 1
    assert report.verified_calls > 0
    # the appended shadow mod is gone either way
    assert "__lc_shadow" not in lib.read_text()


def test_rust_wrong_result_is_reported_as_mismatch(tmp_path):
    """Passes the crate's own tests (the 20-space input is uncovered) - only
    the oracle can catch the cap."""
    root, lib, pristine_text = _rs_project(tmp_path, RS_COUNT_SPACES)
    rewritten = pristine_text.replace(RS_COUNT_SPACES, RS_COUNT_SPACES_CAPPED)
    lib.write_text(rewritten)
    spec = rust_changed_functions({lib: pristine_text}, {lib: rewritten})
    report = run_rust_shadow(root, spec, ["cargo", "test", "--quiet"], 600)
    assert report.mismatches
    assert report.mismatches[0]["function"] == "count_spaces"
    assert report.mismatches[0]["file"] == "lib.rs"
    assert "__lc_shadow" not in lib.read_text()


def test_rust_unverified_functions_are_never_mismatches(tmp_path):
    """unsafe bodies and methods taking self sit outside the v1 whitelist:
    counted as unverified, never a failure, and cargo never runs."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    from less_code.rust_shadow import _eligible

    unsafe_fn = "unsafe fn twist(x: u8) -> u8 { x ^ 1 }\n"
    method_fn = "struct P;\nimpl P { fn poke(&mut self) { let _ = 1; } }\n"
    parser = ts.Parser(ts.Language(grammar.language()))

    def first_item(text):
        stack = [parser.parse(text.encode()).root_node]
        while stack:
            node = stack.pop()
            if node.type == "function_item":
                return node
            stack.extend(node.named_children)
        raise AssertionError("no function_item")

    for text in (unsafe_fn, method_fn):
        ok, _, _ = _eligible(first_item(text), text.encode())
        assert not ok
    # changed_functions buckets both under !unverified
    pristine = {Path("/unused/lib.rs"): unsafe_fn + method_fn}
    current = {
        Path("/unused/lib.rs"): unsafe_fn.replace("x ^ 1", "x ^ 2")
        + method_fn.replace("let _ = 1;", "let _ = 2;")
    }
    spec = rust_changed_functions(pristine, current)
    assert set(spec) == {"!unverified"}
    report = run_rust_shadow(Path("/unused"), spec, None, 60)
    assert report.functions == 2
    assert report.exercised == 0
    assert not report.mismatches


def test_rust_gate_reverts_shadow_mismatch(tmp_path, monkeypatch):
    """End to end: a wrong rewrite the frozen suite cannot see is caught by
    the rust oracle, and the layer reverts."""
    from less_code.pipeline import shrink_project
    from less_code.static import StaticResult

    root, lib, pristine_text = _rs_project(tmp_path, RS_COUNT_SPACES)
    rewritten = pristine_text.replace(RS_COUNT_SPACES, RS_COUNT_SPACES_CAPPED)
    monkeypatch.setattr(
        "less_code.pipeline.static_pass",
        lambda *_a, **_k: StaticResult(changed_files={str(lib): rewritten}),
    )

    stats = shrink_project(root, "rust", test_command=["cargo", "test", "--quiet"])

    assert lib.read_text() == pristine_text
    assert any("rust shadow mismatch" in note for note in stats.static_notes)


def test_rust_gate_accepts_equal_rewrite_with_oracle(tmp_path, monkeypatch):
    from less_code.pipeline import shrink_project
    from less_code.static import StaticResult

    root, lib, pristine_text = _rs_project(tmp_path, RS_COUNT_SPACES)
    equal = pristine_text.replace(RS_COUNT_SPACES, RS_COUNT_SPACES_EQUAL)
    monkeypatch.setattr(
        "less_code.pipeline.static_pass",
        lambda *_a, **_k: StaticResult(changed_files={str(lib): equal}),
    )

    stats = shrink_project(root, "rust", test_command=["cargo", "test", "--quiet"])

    assert stats.tests_ok and stats.api_ok and stats.docs_ok
    assert lib.read_text() == equal
    # the rewrite is kept and the final-run rust oracle verified it
    # (other fixture lines may also shrink under canonical formatting)
    assert stats.loc_final < stats.loc_start
    assert stats.oracle["exercised"] >= 1
    assert not stats.oracle["mismatches"]


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "shadowed"
    root.mkdir(parents=True)
    lib = root / "lib.py"
    lib.write_text(LIB)
    (root / "test_lib.py").write_text(TESTS)
    return root, lib


def _report(root: Path, lib: Path, rewritten: str):
    pristine = {lib: LIB}
    lib.write_text(rewritten)
    spec = changed_functions(pristine, {lib: rewritten})
    return spec, run_shadow(root, spec, PYTEST, 120)


def test_correct_rewrites_are_verified(tmp_path):
    root, lib = _project(tmp_path)
    rewritten = LIB.replace("return a + b", "return b + a").replace(
        "        yield i * 2", "        yield 2 * i"
    )
    spec, report = _report(root, lib, rewritten)
    assert set(spec[str(lib)]) == {"add", "evens"}
    assert report.ok
    assert report.functions == 2 and report.exercised == 2
    assert report.verified_calls >= 2


def test_wrong_result_is_caught_even_when_tests_stay_green(tmp_path):
    root, lib = _project(tmp_path)
    _, report = _report(root, lib, LIB.replace("return a + b", "return a + b + 1"))
    assert report.tests_ok
    assert not report.ok
    assert report.mismatches[0]["function"] == "add"
    assert report.mismatches[0]["examples"][0]["kind"] == "result"


def test_generator_items_are_compared_lazily(tmp_path):
    root, lib = _project(tmp_path)
    _, report = _report(root, lib, LIB.replace("yield i * 2", "yield i * 3"))
    assert report.tests_ok and not report.ok
    assert report.mismatches[0]["examples"][0]["kind"] == "iterator-item"


def test_argument_mutation_and_properties_are_compared(tmp_path):
    root, lib = _project(tmp_path)
    _, report = _report(
        root,
        lib,
        LIB.replace(
            "        items.append(1)\n        return len(items)",
            "        return len(items) + 1",
        ),
    )
    assert report.tests_ok and not report.ok
    assert report.mismatches[0]["examples"][0]["kind"].endswith("mutation")
    root, lib = _project(tmp_path / "again")
    _, report = _report(root, lib, LIB.replace("self.value * 2", "self.value * 3"))
    assert not report.ok and report.mismatches[0]["function"] == "Box.double"


def test_iterator_arguments_are_teed_not_copied(tmp_path):
    root, lib = _project(tmp_path)
    rewritten = LIB.replace(
        "    for v in values:\n        if v > limit:\n            return v\n    return None",
        "    return next((v for v in values if v > limit), None)",
    )
    _, report = _report(root, lib, rewritten)
    assert report.ok and report.exercised == 1 and report.skipped_calls == 0


def test_nondeterministic_results_are_not_mismatches(tmp_path):
    root, lib = _project(tmp_path)
    lib_src = LIB + (
        "\nimport itertools\n_counter = itertools.count()\n\n"
        "def stamp(x):\n    t = next(_counter)\n    return (x, t)\n"
    )
    tests = (
        TESTS + "\ndef test_stamp():\n    from lib import stamp\n    assert stamp(1)\n"
    )
    lib.write_text(lib_src)
    (root / "test_lib.py").write_text(tests)
    rewritten = lib_src.replace(
        "    t = next(_counter)\n    return (x, t)", "    return (x, next(_counter))"
    )
    lib.write_text(rewritten)
    spec = changed_functions({lib: lib_src}, {lib: rewritten})
    report = run_shadow(root, spec, PYTEST, 120)
    assert report.ok and report.nondeterministic_calls >= 1


def test_interfering_functions_are_bisected_out(tmp_path):
    root, lib = _project(tmp_path)
    lib_src = (
        LIB + "\n_LOG = []\n\ndef log(line):\n    _LOG.append(line)\n    return line\n"
    )
    tests = TESTS + (
        "\ndef test_log():\n    from lib import log, _LOG\n"
        "    log('a')\n    log('b')\n    assert len(_LOG) == 2\n"
    )
    lib.write_text(lib_src)
    (root / "test_lib.py").write_text(tests)
    rewritten = lib_src.replace("return a + b", "return b + a").replace(
        "    _LOG.append(line)\n    return line",
        "    _LOG.append(line)\n    return str(line)",
    )
    lib.write_text(rewritten)
    spec = changed_functions({lib: lib_src}, {lib: rewritten})
    report = run_shadow(root, spec, PYTEST, 120)
    assert report.ok
    assert report.interference == ["lib.py:log"]
    assert report.exercised == 1  # `add` still verified on its own


def test_effectful_functions_are_excluded_up_front(tmp_path):
    root, lib = _project(tmp_path)
    lib_src = (
        LIB
        + "\ndef save(path, line):\n    with open(path, 'a') as fh:\n        fh.write(line)\n"
    )
    lib.write_text(lib_src)
    rewritten = lib_src.replace("        fh.write(line)", "        fh.write(str(line))")
    spec = changed_functions({lib: lib_src}, {lib: rewritten})
    assert spec == {"!effectful": {"lib.py:save": {}}}
    report = run_shadow(root, spec, PYTEST, 120)
    assert report.ok and report.functions == 0 and report.effectful == ["lib.py:save"]


def test_set_argument_iteration_order_is_not_a_mismatch(tmp_path, monkeypatch):
    """A set argument is copied for the shadowed original; a deep copy
    re-inserts in iteration order and can land in a different table layout,
    so the copy iterates differently. The oracle compares sequences, so an
    order-unfaithful copy must be refused, not compared (found on
    more-itertools: gray_product over a set literal flaked per hash seed)."""
    monkeypatch.setenv("PYTHONHASHSEED", "1")
    root, lib = _project(tmp_path)
    lib_src = LIB + (
        "\ndef pair_over(keys, other):\n    values = tuple(keys)\n"
        "    for k in values:\n        yield (k, other)\n"
    )
    tests = (
        TESTS + "\ndef test_pair_over():\n    from lib import pair_over\n"
        "    keys = {'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't'}\n"
        "    assert list(pair_over(keys, 1))\n"
    )
    lib.write_text(lib_src)
    (root / "test_lib.py").write_text(tests)
    rewritten = lib_src.replace(
        "    values = tuple(keys)\n    for k in values:",
        "    for k in tuple(keys):",
    )
    lib.write_text(rewritten)
    spec = changed_functions({lib: lib_src}, {lib: rewritten})
    report = run_shadow(root, spec, PYTEST, 120)
    assert report.ok
    assert not report.mismatches
    assert report.exercised == 1 and report.verified_calls >= 1


def test_set_returned_in_different_insertion_order_compares_equal(tmp_path):
    root, lib = _project(tmp_path)
    lib_src = LIB + (
        "\ndef letters():\n    out = set()\n"
        "    for ch in 'abc':\n        out.add(ch)\n    return out\n"
    )
    tests = (
        TESTS + "\ndef test_letters():\n    from lib import letters\n"
        "    assert letters()\n"
    )
    lib.write_text(lib_src)
    (root / "test_lib.py").write_text(tests)
    rewritten = lib_src.replace(
        "    out = set()\n    for ch in 'abc':\n        out.add(ch)\n    return out",
        "    return set(reversed('abc'))",
    )
    lib.write_text(rewritten)
    spec = changed_functions({lib: lib_src}, {lib: rewritten})
    report = run_shadow(root, spec, PYTEST, 120)
    assert report.ok and not report.mismatches and report.exercised == 1


def test_nondeterministic_iterator_items_are_not_mismatches(tmp_path):
    root, lib = _project(tmp_path)
    lib_src = LIB + (
        "\nimport itertools\n_counter = itertools.count()\n\n"
        "def stamps(n):\n    for _ in range(n):\n        yield next(_counter)\n"
    )
    tests = (
        TESTS + "\ndef test_stamps():\n    from lib import stamps\n"
        "    assert list(stamps(2))\n"
    )
    lib.write_text(lib_src)
    (root / "test_lib.py").write_text(tests)
    rewritten = lib_src.replace("yield next(_counter)", "yield _counter.__next__()")
    lib.write_text(rewritten)
    spec = changed_functions({lib: lib_src}, {lib: rewritten})
    report = run_shadow(root, spec, PYTEST, 120)
    assert report.ok and not report.mismatches
    assert report.nondeterministic_calls >= 1


def test_rust_shadow_mod_is_no_std_compatible():
    """The generated module must also compile inside a `#![no_std]` crate:
    no `vec!`/`println!` (std macro prelude, absent under no_std), no
    json-style escapes Rust cannot parse, `extern crate std;` for the paths
    the harness links anyway, and valid syntax (one `let` per binding - the
    review found `let a = x, b = y;` and a lone-surrogate literal the hard
    way, on the no_std corpus crate)."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    from less_code.rust_shadow import _shadow_mod

    info = {
        "source": (
            "fn join(a: &str, b: Option<u32>) -> String {\n"
            '    format!("{}{:?}", a, b)\n'
            "}\n"
        ),
        "params": [
            ("ref", ("str",), "&str"),
            ("option", ("scalar", "u32"), "Option<u32>"),
        ],
        "return": ("string",),
    }
    mod = _shadow_mod({"join": info}, Path("/tmp/lc-shadow-marker.log"))
    assert "extern crate std;" in mod
    for banned in ("vec!", "println!", "\\u"):
        assert banned not in mod
    root = ts.Parser(ts.Language(grammar.language())).parse(mod.encode()).root_node
    assert not root.has_error


# ---- oracle v2: methods on constructible local structs ----

_DEFAULT_METHOD = """\
use std::time::Duration;

#[derive(Debug, Default, Clone)]
struct Config {
    retries: u32,
    timeout: Duration,
}

impl Config {
    fn effective_timeout(&self) -> Duration {
        self.timeout + Duration::from_secs(self.retries as u64)
    }
}
"""


def test_v2_method_mismatch_is_caught(tmp_path):
    root, lib, pristine_text = _rs_project(tmp_path, _DEFAULT_METHOD)
    rewritten = pristine_text.replace(
        "self.timeout + Duration::from_secs(self.retries as u64)",
        "self.timeout - Duration::from_secs(self.retries as u64)",
    )
    lib.write_text(rewritten)
    spec = rust_changed_functions({lib: pristine_text}, {lib: rewritten})
    assert "Config::effective_timeout" in spec[str(lib)]
    report = run_rust_shadow(root, spec, ["cargo", "test", "--quiet"], 600)
    assert report.mismatches, "subtracting timeouts must surface as a mismatch"
    assert report.mismatches[0]["function"] == "Config::effective_timeout"
    assert "__lc_shadow" not in lib.read_text()


def test_v2_equal_method_is_verified(tmp_path):
    root, lib, pristine_text = _rs_project(tmp_path, _DEFAULT_METHOD)
    rewritten = pristine_text.replace(
        """    fn effective_timeout(&self) -> Duration {
        self.timeout + Duration::from_secs(self.retries as u64)
    }""",
        """    fn effective_timeout(&self) -> Duration {
        let extra = Duration::from_secs(self.retries as u64);
        extra + self.timeout
    }""",
    )
    lib.write_text(rewritten)
    spec = rust_changed_functions({lib: pristine_text}, {lib: rewritten})
    assert "Config::effective_timeout" in spec[str(lib)]
    report = run_rust_shadow(root, spec, ["cargo", "test", "--quiet"], 600)
    assert report.tests_ok
    assert not report.mismatches
    assert report.exercised == 1
    assert "__lc_shadow" not in lib.read_text()


def test_v2_refusals_are_counted_unverified():
    """Drop impls, generic structs and interior mutability are refused -
    unverified, never a mismatch, and no cargo run is needed to see it."""
    drop_case = (
        "struct D;\n"
        "impl Drop for D { fn drop(&mut self) {} }\n"
        "impl D { fn hit(&self) -> u32 { 1 } }\n"
    )
    generic_case = (
        "struct G<T> { v: T }\n"
        "impl<T> G<T> { fn first(&self) -> u32 { 1 } }\n"
        "impl G<u32> { fn hit(&self) -> u32 { 1 } }\n"
    )
    interior_case = (
        "use std::cell::Cell;\n"
        "#[derive(Debug, Default)]\n"
        "struct M { n: Cell<u32> }\n"
        "impl M { fn hit(&self) -> u32 { self.n.get() } }\n"
    )
    for bad in (drop_case, generic_case, interior_case):
        lib = Path("/unused/lib.rs")
        current = bad.replace(
            "fn hit(&self) -> u32 { 1 }", "fn hit(&self) -> u32 { 2 }"
        )
        current = current.replace("self.n.get()", "self.n.get() + 1")
        spec = rust_changed_functions({lib: bad}, {lib: current})
        assert set(spec) == {"!unverified"}, bad
        assert len(spec["!unverified"]) == 1


def test_v2_mut_self_and_trait_impls_stay_refused():
    mut_case = (
        "#[derive(Debug, Default)]\n"
        "struct C { n: u32 }\n"
        "impl C { fn bump(&mut self) { self.n += 1; } }\n"
    )
    trait_case = (
        "use std::fmt;\n"
        "#[derive(Debug, Default)]\n"
        "struct C { n: u32 }\n"
        "impl fmt::Display for C {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
        '        write!(f, "{}", self.n)\n'
        "    }\n"
        "}\n"
    )
    for bad in (mut_case, trait_case):
        current = bad.replace("self.n += 1;", "self.n += 2;").replace(
            'write!(f, "{}", self.n)', 'write!(f, "{} ", self.n)'
        )
        spec = rust_changed_functions(
            {Path("/unused/lib.rs"): bad}, {Path("/unused/lib.rs"): current}
        )
        assert set(spec) == {"!unverified"}, bad
