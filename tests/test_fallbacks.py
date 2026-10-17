"""In-package fallback removal (aggressive profile)."""

from __future__ import annotations

import textwrap

from less_code.fallbacks import strip_in_package_fallbacks


def _package(tmp_path, sibling: str, module: str):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "typeutils.py").write_text(textwrap.dedent(sibling))
    target = pkg / "mod.py"
    target.write_text(textwrap.dedent(module))
    return target


SIBLING = """\
    def make_sentinel(name="_MISSING"):
        return object()


    class Thing(dict):
        \"\"\"The real Thing.\"\"\"

        def size(self):
            return len(self)
    """


def test_unwraps_sentinel_fallback_and_drops_overridden_copy(tmp_path):
    target = _package(
        tmp_path,
        SIBLING,
        """\
        try:
            from .typeutils import make_sentinel
            _MISSING = make_sentinel(var_name="_MISSING")
        except ImportError:
            _MISSING = object()  # standalone copy


        class Thing(dict):
            \"\"\"Vendored copy of Thing.\"\"\"

            def size(self):
                return len(self)


        try:
            # prefer the package's own Thing
            from .typeutils import Thing
        except ImportError:
            pass


        def use():
            return Thing()
        """,
    )
    source = target.read_text()
    new, notes, removed, waived = strip_in_package_fallbacks(source, target, tmp_path)
    assert waived == {"Thing", "Thing.size"}
    assert "except ImportError" not in new
    assert "Vendored copy" not in new and "def size" not in new
    assert "from .typeutils import make_sentinel" in new
    assert "# prefer the package's own Thing" in new
    assert removed == {
        ("comment", "# standalone copy"): 1,
        ("docstring", "Vendored copy of Thing."): 1,
    }
    assert any("removed Thing" in note for note in notes)
    compile(new, "mod.py", "exec")


def test_declines_when_the_sibling_imports_this_module(tmp_path):
    target = _package(
        tmp_path,
        "from . import mod\n\n\ndef make_sentinel():\n    return object()\n",
        """\
        try:
            from .typeutils import make_sentinel
        except ImportError:
            make_sentinel = object
        """,
    )
    source = target.read_text()
    assert strip_in_package_fallbacks(source, target, tmp_path)[0] == source


def test_declines_names_the_sibling_does_not_define(tmp_path):
    target = _package(
        tmp_path,
        SIBLING,
        """\
        try:
            from .typeutils import missing_helper
        except ImportError:
            missing_helper = None
        """,
    )
    source = target.read_text()
    assert strip_in_package_fallbacks(source, target, tmp_path)[0] == source


def test_absolute_and_platform_imports_are_left_alone(tmp_path):
    target = _package(
        tmp_path,
        SIBLING,
        """\
        try:
            import fcntl
        except ImportError:
            fcntl = None

        try:
            from socket import inet_pton
        except ImportError:
            inet_pton = None
        """,
    )
    source = target.read_text()
    assert strip_in_package_fallbacks(source, target, tmp_path)[0] == source


def test_definition_read_before_the_override_is_kept(tmp_path):
    target = _package(
        tmp_path,
        SIBLING,
        """\
        class Thing(dict):
            pass


        DEFAULT = Thing()

        try:
            from .typeutils import Thing
        except ImportError:
            pass
        """,
    )
    new, *_ = strip_in_package_fallbacks(target.read_text(), target, tmp_path)
    assert "class Thing(dict):" in new  # DEFAULT is built from the local copy
    assert "except ImportError" not in new


def test_copy_with_public_api_the_sibling_lacks_is_kept(tmp_path):
    target = _package(
        tmp_path,
        SIBLING,
        """\
        class Thing(dict):
            def size(self):
                return len(self)

            def extra(self):
                return 1


        try:
            from .typeutils import Thing
        except ImportError:
            pass
        """,
    )
    new, _, _, waived = strip_in_package_fallbacks(target.read_text(), target, tmp_path)
    assert "def extra" in new and not waived


def test_notes_become_comments_but_docstrings_and_attribute_docs_stay():
    from less_code.notes import notes_to_comments

    source = (
        '"""Module doc."""\n'
        "import os\n"
        '"""free-floating note"""\n'
        "LIMIT = 3\n"
        '"""Sphinx documents LIMIT with this."""\n'
        "\n\n"
        "def f(x):\n"
        '    """Doc."""\n'
        "    y = x + 1\n"
        "    print(y)\n"
        '    "returns y"\n'
        "    return y\n"
    )
    new, count, added = notes_to_comments(source)
    assert count == 2
    assert "# free-floating note" in new and "    # returns y" in new
    assert '"""Sphinx documents LIMIT with this."""' in new
    assert '"""Doc."""' in new and '"""Module doc."""' in new
    assert added == {
        ("comment", "# free-floating note"): 1,
        ("comment", "# returns y"): 1,
    }
