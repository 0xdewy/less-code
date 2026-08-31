"""Iteration 10 — compiler diagnostics as feedback, and the mechanical merge
template for duplicate groups.

Iteration 09 measured that 100% of the rust duplicate-group proposals died at
`cargo check` and that the model was never told *why*: the outcome string
carried one truncated line squeezed out of a pytest-tail extractor. These
tests pin the two halves of the fix — the compiler's own words survive, and
they reach the next prompt.
"""

import pathlib
import re
import textwrap

import pytest

from less_code.backends import Backend
from less_code.dedup import (
    DupGroup,
    Unit,
    find_duplicate_groups,
    raw_tokens,
    token_diff_slots,
)
from less_code.llm_reduce import (
    COMPILER_CHARS,
    _feedback_for,
    build_dedup_prompt,
    compiler_errors,
    reduce_symbols,
)
from less_code.testrunners import run_tests

# ---- compiler_errors -------------------------------------------------------

CARGO_TAIL = textwrap.dedent(
    """
    warning: unused variable: `n`
      --> src/lib.rs:3:9
    error[E0308]: mismatched types
      --> src/lib.rs:12:20
       |
    12 |     let out: usize = row.join(",");
       |              -----   ^^^^^^^^^^^^^ expected `usize`, found `String`
       |              |
       |              expected due to this
    error[E0425]: cannot find value `fields` in this scope
      --> src/lib.rs:14:5
       |
    14 |     fields.len()
       |     ^^^^^^ not found in this scope
    error: aborting due to 2 previous errors
    For more information about this error, try `rustc --explain E0308`.
    """
).strip()


def test_rust_diagnostics_keep_the_location_and_the_expected_found_note():
    out = compiler_errors(CARGO_TAIL, "rust")
    assert "error[E0308]: mismatched types" in out
    assert "--> src/lib.rs:12:20" in out
    assert "expected `usize`, found `String`" in out
    # the second error is kept too: fixing only the first wastes an attempt
    assert "cannot find value `fields`" in out


def test_rust_diagnostics_drop_cargo_boilerplate_that_names_no_code():
    out = compiler_errors(CARGO_TAIL, "rust")
    assert "aborting due to" not in out
    assert "rustc --explain" not in out


def test_rust_diagnostics_are_capped():
    assert len(compiler_errors(CARGO_TAIL * 20, "rust")) <= COMPILER_CHARS


def test_a_cargo_tail_with_no_error_block_still_says_something():
    assert compiler_errors("thread 'main' panicked at foo", "rust")


NODE_TAIL = textwrap.dedent(
    """
    /tmp/proj/report.js:41
      return rows.map((r) => {
                            ^^

    SyntaxError: Unexpected end of input
        at wrapSafe (node:internal/modules/cjs/loader:1029:15)
        at Module._compile (node:internal/modules/cjs/loader:1063:27)
    """
).strip()


def test_node_check_keeps_the_message_and_the_offending_line():
    out = compiler_errors(NODE_TAIL, "javascript")
    assert "SyntaxError: Unexpected end of input" in out
    assert "report.js:41" in out
    assert "node:internal" not in out, "node's own stack is not about the code"


def test_the_compiler_text_becomes_an_instruction():
    fb = _feedback_for("syntax-error: error[E0308]: mismatched types\n  --> src/lib.rs:12:20")
    assert "DID NOT COMPILE" in fb
    assert "error[E0308]: mismatched types" in fb
    assert "--> src/lib.rs:12:20" in fb
    assert "public signature" in fb


# ---- the feedback actually reaches the next attempt ------------------------

MODULE = (
    "def sign(n):\n"
    "    if n > 0:\n"
    '        result = "positive"\n'
    "    else:\n"
    '        result = "other"\n'
    "    total = 0\n"
    "    for _ in range(1):\n"
    "        total = total + 1\n"
    "    return result\n"
)
TESTS = (
    "from mod import sign\n\n"
    "def test_sign():\n    assert sign(5) == 'positive'\n    assert sign(-1) == 'other'\n"
)
FIXED = 'def sign(n):\n    return "positive" if n > 0 else "other"\n'


class FixesOnCompilerFeedbackBackend(Backend):
    """Emits code that does not parse, and repairs it ONLY once the next
    prompt quotes the parser's complaint. If the diagnostic never reaches the
    prompt this backend loops forever and the test fails."""

    def __init__(self):
        super().__init__("syntax-fixer")
        self.feedback_seen = ""

    def complete(self, system, prompt, temperature=0.2):
        if "DID NOT COMPILE" in prompt and "expected ':'" in prompt:
            self.feedback_seen = prompt
            return f"```python\n{FIXED}```"
        return '```python\ndef sign(n)\n    return "positive" if n > 0 else "other"\n```'


def test_a_syntax_error_reaches_the_next_prompt_and_is_fixed(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(MODULE)
    (root / "test_mod.py").write_text(TESTS)
    backend = FixesOnCompilerFeedbackBackend()
    records = reduce_symbols(
        backend, root, root / "mod.py", "python",
        lambda r, l: run_tests(r, l, use_cache=False),
        attempts_per_symbol=2, max_symbols=1,
    )
    assert records[0].outcome == "syntax-error"
    assert "expected ':'" in records[0].detail and "line 1" in records[0].detail
    assert backend.feedback_seen, "the parser's message never reached the model"
    assert records[1].outcome == "accepted"
    assert (root / "mod.py").read_text().strip() == FIXED.strip()


# ---- the mechanical merge template ----------------------------------------


def _group(lang: str, *texts: str) -> DupGroup:
    members = []
    for i, text in enumerate(texts):
        body = textwrap.dedent(text).strip()
        name = re.search(r"(?:fn|def)\s+(\w+)", body).group(1)
        members.append(Unit(path=pathlib.Path("m"), key=name, name=name, start=0,
                            end=1, text=body, lang=lang, indent=0))
    return DupGroup(members=members, similarity=0.99)


def test_the_token_diff_names_exactly_what_varies():
    group = _group(
        "rust",
        "pub fn csv_escape_row(fields: &[&str]) -> String { fields.join(\",\") }",
        "pub fn csv_escape_row_owned(fields: &[String]) -> String { fields.join(\",\") }",
    )
    slots = token_diff_slots(group, "rust")
    assert slots == [("& str", "String")]


def test_the_definition_name_is_not_a_parameter():
    """Renaming is not parameterization: `f0` vs `f1` must never show up as
    something the helper has to abstract."""
    group = _group(
        "rust",
        "fn f0(a: usize) -> usize { a + 1 }",
        "fn f1(a: usize) -> usize { a + 2 }",
    )
    assert token_diff_slots(group, "rust") == [("1", "2")]


def test_a_differing_error_message_shows_up_as_a_slot():
    group = _group(
        "python",
        "def f0(x):\n    if x is None:\n        raise ValueError('x required')\n    return x",
        "def f1(x):\n    if x is None:\n        raise ValueError('y required')\n    return x",
    )
    assert token_diff_slots(group, "python") == [("'x required'", "'y required'")]


def test_raw_tokens_keep_string_literals_whole():
    assert "'a, b'" in raw_tokens("x = 'a, b'  # note", "python")
    assert "note" not in raw_tokens("x = 'a, b'  # note", "python")


def test_groups_are_capped_at_three_members():
    src = {}
    body = "def f{i}(a, b):\n    total = 0\n    for x in a:\n        total = total + x * {i}\n    return total + b\n"
    src[pathlib.Path("m.py")] = "\n".join(body.replace("{i}", str(i)) for i in range(6))
    for group in find_duplicate_groups(src, "python"):
        assert len(group.members) <= 3


def test_a_merely_similar_third_member_does_not_join_a_tight_pair():
    twin = "fn count_leading(s: &str) -> usize {\n    let mut c = 0usize;\n    for ch in s.chars() {\n        if ch == ' ' { c = c + 1; } else { break; }\n    }\n    c\n}\n"
    other = "fn count_trailing(s: &str) -> usize {\n    let mut c = 0usize;\n    for ch in s.chars().rev() {\n        if ch == ' ' { c = c + 1; } else { break; }\n    }\n    c\n}\n"
    loose = "fn total_len(s: &str) -> usize {\n    let mut c = 0usize;\n    for ch in s.chars() {\n        if ch != '\\n' { c = c + ch.len_utf8(); }\n    }\n    c + 1\n}\n"
    groups = find_duplicate_groups({pathlib.Path("m.rs"): twin + other + loose}, "rust")
    top = groups[0]
    assert {m.name for m in top.members} == {"count_leading", "count_trailing"}


def test_the_prompt_hands_over_the_diff_and_pins_the_messages():
    group = _group(
        "rust",
        "pub fn csv_escape_row(fields: &[&str]) -> String { fields.join(\",\") }",
        "pub fn csv_escape_row_owned(fields: &[String]) -> String { fields.join(\",\") }",
    )
    prompt = build_dedup_prompt(group, "rust")
    assert "COMPUTED TOKEN DIFF" in prompt
    assert "& str" in prompt and "String" in prompt
    assert "at most 1 parameter" in prompt
    assert "verbatim" in prompt


def test_the_system_prompt_states_what_the_tests_pin():
    from less_code.llm_reduce import DEDUP_SYSTEM_PROMPT

    assert "PINNED BY THE TESTS" in DEDUP_SYSTEM_PROMPT
    assert "never reword" in DEDUP_SYSTEM_PROMPT
