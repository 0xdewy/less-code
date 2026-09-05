"""Deterministic guard-block outlining (`less_code.outline`).

The positives prove the merge happens; the negatives prove the four escape
routes named in the module docstring are closed. `exec`-and-compare tests do
the real work: they run the original and the outlined module side by side and
require identical exception types and identical `str(exc)` — the property the
7B model could not hold in iteration 09.
"""

import textwrap
from pathlib import Path

import pytest

from less_code.outline import outline_guards


def norm(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def one(src: str, **kw):
    changed, notes = outline_guards({"m.py": norm(src)}, **kw)
    return changed.get("m.py"), notes


def run(src: str):
    ns: dict = {}
    exec(compile(src, "m.py", "exec"), ns)  # noqa: S102 - fixed test source
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


def test_a_differing_fstring_text_part_renders_as_an_interpolation():
    """A varying f-string *literal text* part becomes a parameter; splicing a
    bare Name into JoinedStr.values is invalid AST and ast.unparse raises
    (found by running the outliner on less_code's own source)."""
    src = """
        def a(feedback, tail):
            head = f"\\nPREVIOUS:\\n{feedback}\\n" if feedback else ""
            body = tail.upper()
            foot = tail.lower()
            return head + body + foot

        def b(focus, tail):
            head = f"\\nFOCUS:\\n{focus}\\n" if focus else ""
            body = tail.upper()
            foot = tail.lower()
            return head + body + foot

        def c(fmap, tail):
            head = f"\\nMAP:\\n{fmap}\\n" if fmap else ""
            body = tail.upper()
            foot = tail.lower()
            return head + body + foot
        """
    out, _notes = one(src)
    assert out is not None
    assert "f'{message}" in out.replace('"', "'")
    for arg in ("hi", None):
        for fn in "abc":
            assert behaves_the_same(
                norm(src), out, lambda ns, fn=fn, arg=arg: ns[fn](arg, "Ab")
            )


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
            assert behaves_the_same(
                norm(src), out, lambda ns, fn=fn, arg=arg: ns[fn](arg)
            )


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
    assert (
        one(
            src.replace("x > 4", "x > 2")
            .replace("x > 6", "x > 2")
            .replace("x > 8", "x > 2")
        )[0]
        is not None
    )


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
    b = "import a\n\n" + norm(MULTI_B)  # the import edge must already exist
    changed, notes = outline_guards({"a.py": norm(MULTI_A), "b.py": b})
    assert set(changed) == {"a.py", "b.py"}
    assert "def _check_x(x):" in changed["a.py"]
    assert "def _check_x" not in changed["b.py"]
    assert "from a import _check_x" in changed["b.py"].splitlines()[:2]
    assert "3 sites" in notes[0]


def test_cross_file_outlining_needs_an_existing_import_edge():
    """A new module-level dependency can violate a contract the suite pins
    (click's test_light_imports) or create a cycle: without an existing edge
    the site keeps its inline guard and the group dies at min_occurrences."""
    changed, _notes = outline_guards({"a.py": norm(MULTI_A), "b.py": norm(MULTI_B)})
    assert changed == {}


def test_a_group_spanning_two_package_modules_imports_relatively(tmp_path):
    """The click bug: under a src-layout package the flat absolute import
    (`from _textwrap import _check`) is a hard ModuleNotFoundError. Inside a
    package the emitted import must be relative."""
    pkg = tmp_path / "src" / "clickish"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    b = "from .a import a as _a\n\n" + norm(MULTI_B)  # existing package edge
    changed, _notes = outline_guards(
        {
            str(pkg / "a.py"): norm(MULTI_A),
            str(pkg / "b.py"): b,
        }
    )
    assert "from .a import _check_x" in changed[str(pkg / "b.py")].splitlines()[:3]
    # and it must actually import and run, not just look right
    for p, text in changed.items():
        Path(p).write_text(text)
    import subprocess
    import sys

    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(tmp_path / 'src')!r})\n"
        "from clickish.a import a\n"
        "from clickish.b import c\n"
        "assert a(' q ') == 'q'\n"
        "assert c(' q ') == 'qqq'\n"
        "try:\n    a(None)\nexcept ValueError as e:\n"
        "    assert 'x required' == str(e)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr


def test_a_helper_name_taken_in_the_importer_is_avoided(tmp_path):
    """The import binds the helper name in the importer's module namespace: a
    collision there would shadow existing module state."""
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    b = "from .a import a as _a\n\n" + norm(MULTI_B) + "\n\n_check_x = 1\n"
    changed, _notes = outline_guards(
        {
            str(pkg / "a.py"): norm(MULTI_A),
            str(pkg / "b.py"): b,
        }
    )
    lines = changed[str(pkg / "b.py")].splitlines()[:3]
    (import_line,) = [l for l in lines if l.startswith("from .a import _")]
    assert import_line.rsplit(" ", 1)[1] != "_check_x"  # a NEW name, not the taken one


def test_a_mixed_flat_package_layout_keeps_the_inline_guard(tmp_path):
    """Importer inside a package, host outside it (or vice versa): no import
    form can be proven to resolve, so the site keeps its inline guard rather
    than gambling."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    loose = tmp_path / "loose"
    loose.mkdir()
    changed, _notes = outline_guards(
        {
            str(pkg / "a.py"): norm(MULTI_A),
            str(loose / "b.py"): norm(MULTI_B),
        }
    )
    # a.py alone holds 2 sites (< MIN_OCCURRENCES=3), b.py's site cannot join:
    # the group dies and nothing is outlined
    assert changed == {}


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
    from less_code.pipeline import shrink_project

    root = _project(tmp_path)
    result = shrink_project(root)
    assert any("outlined" in note for note in result.static_notes)
    assert "_check_x" in (root / "mod.py").read_text()


def test_a_scripted_misfire_is_reverted_by_the_gate(tmp_path, monkeypatch):
    """The layer is only ever as trusted as the suite. Script it to reword an
    error message and the narrowing pass must drop it and say so."""
    from less_code import outline
    from less_code.pipeline import shrink_project

    def broken(sources, min_occurrences=3):
        out = {
            p: t.replace("'x required'", "'x is required'") for p, t in sources.items()
        }
        return {p: t for p, t in out.items() if t != sources[p]}, ["scripted misfire"]

    monkeypatch.setattr(outline, "outline_guards", broken)
    root = _project(tmp_path)
    result = shrink_project(root)
    assert any("outline: reverted by the gate" in note for note in result.static_notes)
    assert "x is required" not in (root / "mod.py").read_text()


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
