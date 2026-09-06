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
