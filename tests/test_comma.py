"""Magic trailing comma collapse: edge cases and the pipeline layer."""

from __future__ import annotations

import ast

from less_code.comma import collapse_magic_commas
from less_code.loc import canonical_format
from less_code.pipeline import shrink_project


def test_trailing_comma_pinning_a_call_is_removed():
    new, count = collapse_magic_commas("foo(\n    a,\n    b,\n)\n")
    assert new == "foo(\n    a,\n    b\n)\n"
    assert count == 1


def test_trailing_comment_pins_the_comma():
    source = "foo(\n    a,\n    b,  # keeps this\n)\n"
    assert collapse_magic_commas(source) == (source, 0)


def test_dict_and_subscript_literals_collapse():
    assert collapse_magic_commas('d = {\n    "k": v,\n}\n') == (
        'd = {\n    "k": v\n}\n',
        1,
    )
    assert collapse_magic_commas("row = [\n    1,\n    2,\n]\n") == (
        "row = [\n    1,\n    2\n]\n",
        1,
    )


def test_one_line_call_is_not_touched():
    source = "f(a, b,)\n"
    assert collapse_magic_commas(source) == (source, 0)


def test_nested_blocks_collapse_independently():
    new, count = collapse_magic_commas("x = [\n    foo(\n        a,\n    ),\n]\n")
    assert count == 2
    assert new == "x = [\n    foo(\n        a\n    )\n]\n"


def test_comma_followed_by_code_on_the_same_line_stays():
    new, count = collapse_magic_commas("foo(\n    a, b,\n)\n")
    assert count == 1
    assert new == "foo(\n    a, b\n)\n"
    assert collapse_magic_commas("foo(\n    a\n)\n") == ("foo(\n    a\n)\n", 0)


def test_syntax_invalid_input_is_returned_unchanged():
    for broken in ("foo(\n    a,\n", "def f(:\n    pass\n", "x = (1,\n"):
        assert collapse_magic_commas(broken) == (broken, 0)


def test_collapse_then_canonical_format_parses_cleanly():
    source = "def pair(values):\n    return sorted(\n        values,\n        reverse=True,\n    )\n"
    new, count = collapse_magic_commas(source)
    assert count == 1
    assert (
        new
        == "def pair(values):\n    return sorted(\n        values,\n        reverse=True\n    )\n"
    )
    canonical, ok = canonical_format(new, "python")
    assert ok
    ast.parse(canonical)


def test_pipeline_reports_the_comma_layer_and_updates_the_stat(tmp_path):
    source = "def pair(a, b):\n    return sorted(\n        [a, b],\n    )\n"
    root = tmp_path / "comma-project"
    root.mkdir()
    (root / "library.py").write_text(source)
    (root / "test_library.py").write_text(
        "from library import pair\n\ndef test_pair():\n    assert pair(2, 1) == [1, 2]\n"
    )

    stats = shrink_project(root)

    assert stats.tests_ok and stats.api_ok and stats.docs_ok
    assert (
        root / "library.py"
    ).read_text() == "def pair(a, b):\n    return sorted([a, b])\n"
    assert stats.comma_collapse_loc > 0
    assert stats.to_json()["comma_collapse_loc"] == stats.comma_collapse_loc
    record = next(r for r in stats.layer_records if r["layer"] == "comma-collapse")
    assert record["committed"]
    assert record["loc_after"] < record["loc_before"]
    assert any("magic trailing comma" in note for note in stats.static_notes)


def test_gate_rejects_joins_the_project_formatter_would_resplit(tmp_path):
    """A ruff.toml project at width 60: the canonical-88 render of the
    collapsed block joins, but the project's own formatter re-splits it.
    The gate must evaluate under the project config and reject the join,
    not count it."""
    root = tmp_path / "narrow-project"
    root.mkdir()
    (root / "ruff.toml").write_text("line-length = 60\n")
    (root / "library.py").write_text(
        "def combine(*args):\n    return args\n\n\n"
        "def pair(a, b):\n"
        "    return combine(\n"
        '        "alpha_parameter_value_here",\n'
        '        "beta_parameters_value_here",\n'
        "    )\n"
    )
    (root / "test_library.py").write_text(
        "from library import pair\n\n\ndef test_pair():\n    assert pair(1, 2)\n"
    )

    stats = shrink_project(root)

    assert stats.tests_ok and stats.api_ok and stats.docs_ok
    record = next(
        (r for r in stats.layer_records if r["layer"] == "comma-collapse"), None
    )
    assert record is not None
    assert not record["committed"]
    assert stats.comma_collapse_loc == 0
    assert '"beta_parameters_value_here",\n' in (root / "library.py").read_text()
