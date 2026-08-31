"""Deterministic guard-block outlining (`less_code.outline`).

The positives prove the merge happens; the negatives prove the four escape
routes named in the module docstring are closed. `exec`-and-compare tests do
the real work: they run the original and the outlined module side by side and
require identical exception types and identical `str(exc)` — the property the
7B model could not hold in iteration 09.
"""

import textwrap

import pytest

from less_code.langdetect import map_project
from less_code.outline import outline_guards
from less_code.static import static_pass


def norm(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def one(src: str, **kw):
    changed, notes = outline_guards({"m.py": norm(src)}, **kw)
    return changed.get("m.py"), notes


def run(src: str):
    ns: dict = {}
    exec(compile(src, "m.py", "exec"), ns)
    return ns


def behaves_the_same(before: str, after: str, calls):
    """`calls(ns)` must produce the same value or the same exception."""
    a, b = run(before), run(after)

    def outcome(ns):
        try:
            return ("ok", calls(ns))
        except BaseException as exc:  # noqa: BLE001 - that is the comparison
            return ("raise", type(exc).__name__, str(exc))

    return outcome(a) == outcome(b)


# ---- positives -------------------------------------------------------------

THREE_GUARDS = """
    def a(x):
        if x is None:
            raise ValueError('x required')
        return 1

    def b(x):
        if x is None:
            raise ValueError('x required')
        return 2

    def c(x):
        if x is None:
            raise ValueError('x required')
        return 3

    def d(x):
        if x is None:
            raise ValueError('x required')
        return 4
    """


def test_extraction_fires_on_repeated_identical_guards():
    out, notes = one(THREE_GUARDS)
    assert out is not None
    assert out.count("    _check_x(x)") == 4
    assert "def _check_x(x):" in out
    assert notes and "4 sites" in notes[0]
    assert behaves_the_same(norm(THREE_GUARDS), out, lambda ns: ns["b"](None))
    assert behaves_the_same(norm(THREE_GUARDS), out, lambda ns: ns["b"](7))


def test_two_occurrences_are_below_the_floor():
    src = THREE_GUARDS.rsplit("def c(", 1)[0]
    assert one(src) == (None, [])


def test_the_occurrence_floor_is_the_floor_that_declines():
    """Three sites of this shape pay for the helper exactly, so raise the
    floor and the same input is refused for a different reason."""
    assert one(THREE_GUARDS, min_occurrences=5) == (None, [])


def test_a_differing_message_becomes_a_parameter_and_is_preserved_exactly():
    src = """
        def a(x):
            if x is None:
                raise ValueError('x required')

        def b(y):
            if y is None:
                raise ValueError('y is missing, since 2016')

        def c(z):
            if z is None:
                raise ValueError('z!')

        def d(w):
            if w is None:
                raise ValueError('w gone')
        """
    out, _notes = one(src)
    assert out is not None
    # each site passes its own message, byte for byte
    assert "_check_x(x, 'x required')" in out
    assert "_check_x(y, 'y is missing, since 2016')" in out
    assert "_check_x(z, 'z!')" in out
    assert "raise ValueError(message)" in out
    for fn in "abcd":
        assert behaves_the_same(norm(src), out, lambda ns, fn=fn: ns[fn](None))


def test_a_bound_name_used_afterwards_is_returned_and_unpacked():
    src = """
        def a(s):
            if not isinstance(s, str):
                raise TypeError('not a string')
            text = s.strip().upper()
            if len(text) != 3:
                raise ValueError('need 3: %r' % (s,))
            return text + '!'

        def b(s):
            if not isinstance(s, str):
                raise TypeError('not a string')
            text = s.strip().upper()
            if len(text) != 3:
                raise ValueError('need 3: %r' % (s,))
            return [text]

        def c(s):
            if not isinstance(s, str):
                raise TypeError('not a string')
            text = s.strip().upper()
            if len(text) != 3:
                raise ValueError('need 3: %r' % (s,))
            return {'v': text}
        """
    out, _notes = one(src)
    assert out is not None
    assert "text = _check_s(s)" in out
    assert "return text" in out
    for arg in (" abc ", "ab", 5):
        for fn in "abc":
            assert behaves_the_same(norm(src), out, lambda ns, fn=fn, arg=arg: ns[fn](arg))


def test_the_argument_is_evaluated_before_the_guards_so_only_names_are_lifted():
    """A `%`-formatted message stays *inside* the helper. If it were lifted to
    the call site it would be evaluated before the isinstance guard and raise
    TypeError instead of the pinned ValueError."""
    src = """
        def a(q):
            if not isinstance(q, int):
                raise ValueError('q must be an integer')
            if q <= 0:
                raise ValueError('q must be positive, got %d' % q)

        def b(q):
            if not isinstance(q, int):
                raise ValueError('q must be an integer')
            if q <= 0:
                raise ValueError('q must be positive, got %d' % q)

        def c(q):
            if not isinstance(q, int):
                raise ValueError('q must be an integer')
            if q <= 0:
                raise ValueError('q must be positive, got %d' % q)
        """
    out, _notes = one(src)
    assert out is not None
    assert "'q must be positive, got %d' % q" in out
    assert behaves_the_same(norm(src), out, lambda ns: ns["a"]("nope"))


# ---- negatives: the four escape routes -------------------------------------


def test_a_window_containing_a_return_is_declined():
    src = """
        def a(x):
            if x is None:
                return 0

        def b(x):
            if x is None:
                return 0

        def c(x):
            if x is None:
                return 0
        """
    assert one(src) == (None, [])


def test_a_name_the_helper_could_not_resolve_is_declined():
    """`seen` is bound only inside a branch, so it is not provably bound at the
    window; lifting it to an argument could raise UnboundLocalError early."""
    src = """
        def a(x):
            if x:
                seen = 1
            if seen is None:
                raise ValueError('bad')

        def b(x):
            if x:
                seen = 1
            if seen is None:
                raise ValueError('bad')

        def c(x):
            if x:
                seen = 1
            if seen is None:
                raise ValueError('bad')
        """
    assert one(src) == (None, [])


def test_a_lambda_in_the_window_is_declined():
    src = """
        def a(x):
            if (lambda v: v is None)(x):
                raise ValueError('bad')

        def b(x):
            if (lambda v: v is None)(x):
                raise ValueError('bad')

        def c(x):
            if (lambda v: v is None)(x):
                raise ValueError('bad')
        """
    assert one(src) == (None, [])


def test_a_global_declared_name_is_declined():
    src = """
        COUNT = 0

        def a(x):
            global COUNT
            COUNT = x
            if COUNT is None:
                raise ValueError('bad')

        def b(x):
            global COUNT
            COUNT = x
            if COUNT is None:
                raise ValueError('bad')

        def c(x):
            global COUNT
            COUNT = x
            if COUNT is None:
                raise ValueError('bad')
        """
    assert one(src) == (None, [])


def test_more_than_two_varying_constant_classes_is_declined():
    """Three independently-varying literals would need three parameters; the
    cap keeps the helper a shared guard rather than a configurable mini-DSL.
    The two statements have different shapes, so no shorter window rescues it."""
    src = """
        def a(x):
            if x != 1 or x > 2:
                raise ValueError('a')

        def b(x):
            if x != 3 or x > 4:
                raise ValueError('b')

        def c(x):
            if x != 5 or x > 6:
                raise ValueError('c')

        def d(x):
            if x != 7 or x > 8:
                raise ValueError('d')
        """
    out, _notes = one(src)
    assert out is None
    # …and it is the cap, not the shape: pin one literal and it goes through
    assert one(src.replace("x > 4", "x > 2").replace("x > 6", "x > 2")
               .replace("x > 8", "x > 2"))[0] is not None


def test_a_group_that_cannot_pay_for_its_helper_is_declined():
    """Three one-line-saving sites against a three-line helper is a wash, and
    a wash is a decline: the metric is code lines, not cleverness."""
    src = """
        def a(x):
            if x is None:
                raise ValueError('m')

        def b(x):
            if x is None:
                raise ValueError('m')

        def c(x):
            if x is None:
                raise ValueError('m')
        """
    # 3 sites x 1 line saved - 3 helper lines = 0
    assert one(src) == (None, [])


def test_a_statement_sharing_a_line_is_not_clobbered():
    src = """
        def a(x):
            if x is None: raise ValueError('x required')
            return 1

        def b(x):
            if x is None: raise ValueError('x required')
            return 2

        def c(x):
            if x is None: raise ValueError('x required')
            return 3
        """
    out, _notes = one(src)
    # one-line `if ... : raise` spans a single line, so the saving is zero and
    # the group is declined rather than mangled
    assert out is None


# ---- multi-file ------------------------------------------------------------

MULTI_A = """
    def a(x):
        if x is None:
            raise ValueError('x required')
        text = str(x).strip()
        if not text:
            raise ValueError('x empty')
        return text

    def b(x):
        if x is None:
            raise ValueError('x required')
        text = str(x).strip()
        if not text:
            raise ValueError('x empty')
        return text * 2
    """

MULTI_B = """
    def c(x):
        if x is None:
            raise ValueError('x required')
        text = str(x).strip()
        if not text:
            raise ValueError('x empty')
        return text * 3
    """


def test_a_group_spanning_two_files_hosts_the_helper_where_it_is_busiest():
    changed, notes = outline_guards({"a.py": norm(MULTI_A), "b.py": norm(MULTI_B)})
    assert set(changed) == {"a.py", "b.py"}
    assert "def _check_x(x):" in changed["a.py"]
    assert "def _check_x" not in changed["b.py"]
    assert changed["b.py"].splitlines()[0] == "from a import _check_x"
    assert "3 sites" in notes[0]


# ---- the gate --------------------------------------------------------------


GUARD_MODULE = norm(
    """
    def a(x):
        if x is None:
            raise ValueError('x required')
        text = str(x).strip()
        if not text:
            raise ValueError('x empty')
        return text

    def b(x):
        if x is None:
            raise ValueError('x required')
        text = str(x).strip()
        if not text:
            raise ValueError('x empty')
        return text * 2

    def c(x):
        if x is None:
            raise ValueError('x required')
        text = str(x).strip()
        if not text:
            raise ValueError('x empty')
        return text * 3
    """
)

GUARD_TESTS = norm(
    """
    import pytest

    from mod import a, b, c

    def test_all():
        assert (a(' q '), b(' q '), c(' q ')) == ('q', 'qq', 'qqq')

    def test_messages():
        for fn in (a, b, c):
            with pytest.raises(ValueError) as e:
                fn(None)
            assert str(e.value) == 'x required'
    """
)


def _project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(GUARD_MODULE)
    (root / "test_mod.py").write_text(GUARD_TESTS)
    return root


def test_the_static_pass_outlines_under_the_gate(tmp_path):
    from less_code.testrunners import run_tests

    root = _project(tmp_path)
    project = map_project(root)
    result = static_pass(root, "python", project.source_files,
                         project.source_files + project.test_files, runner=run_tests)
    assert any("outlined" in n for n in result.notes)
    assert "_check_x" in result.changed_files[str(root / "mod.py")]


def test_a_scripted_misfire_is_reverted_by_the_gate(tmp_path, monkeypatch):
    """The layer is only ever as trusted as the suite. Script it to reword an
    error message and the narrowing pass must drop it and say so."""
    import less_code.outline as outline
    from less_code.testrunners import run_tests

    def broken(sources, min_occurrences=3):
        out = {p: t.replace("'x required'", "'x is required'") for p, t in sources.items()}
        return {p: t for p, t in out.items() if t != sources[p]}, ["scripted misfire"]

    monkeypatch.setattr(outline, "outline_guards", broken)
    root = _project(tmp_path)
    project = map_project(root)
    result = static_pass(root, "python", project.source_files,
                         project.source_files + project.test_files, runner=run_tests)
    assert any("guard-outlining reverted by the gate" in n for n in result.notes)
    for text in result.changed_files.values():
        assert "x is required" not in text


@pytest.mark.parametrize("value", [None, "", " ok ", 3])
def test_the_real_fixture_block_keeps_every_message(value):
    """The shape the py fixture actually pastes six times, end to end."""
    src = """
        SKU_AREAS = ('AA', 'BB')

        class E(Exception):
            pass

        def one(raw):
            if raw is None:
                raise E('missing SKU')
            if not isinstance(raw, str):
                raise E('SKU must be a string')
            text = raw.strip().upper()
            if len(text) != 5:
                raise E('SKU must be 5 characters: %r' % (raw,))
            return text

        def two(sku):
            if sku is None:
                raise E('missing SKU')
            if not isinstance(sku, str):
                raise E('SKU must be a string')
            text = sku.strip().upper()
            if len(text) != 5:
                raise E('SKU must be 5 characters: %r' % (sku,))
            return text.lower()

        def three(s):
            if s is None:
                raise E('missing SKU')
            if not isinstance(s, str):
                raise E('SKU must be a string')
            text = s.strip().upper()
            if len(text) != 5:
                raise E('SKU must be 5 characters: %r' % (s,))
            return [text]
        """
    out, _notes = one(src)
    assert out is not None
    for fn in ("one", "two", "three"):
        assert behaves_the_same(norm(src), out, lambda ns, fn=fn: ns[fn](value))
