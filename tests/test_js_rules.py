"""Tests for the JavaScript rule library."""

from __future__ import annotations

from less_code.js_rules import apply_rules


def test_if_ladder_to_array_lookup_simple():
    src = (
        "function quarterLabel(q) {\n"
        "  if (q === 1) { return 'Q1'; }\n"
        "  else if (q === 2) { return 'Q2'; }\n"
        "  else if (q === 3) { return 'Q3'; }\n"
        "  else if (q === 4) { return 'Q4'; }\n"
        "  throw new RangeError('bad quarter');\n"
        "}\n"
    )
    new, applied = apply_rules(src)
    assert applied == ["if-ladder-to-array-lookup"]
    assert "return ['Q1', 'Q2', 'Q3', 'Q4'][q - 1]" in new
    assert "if (q < 1 || q > 4)" in new
    # the throw is inlined into the range check
    assert "throw new RangeError" in new  # reused in the range check
    assert new.count("throw new RangeError") == 1


def test_if_ladder_skips_non_contiguous():
    """Non-contiguous integer keys must be left alone (semantics change)."""
    src = (
        "function f(x) {\n"
        "  if (x === 1) return 'a';\n"
        "  else if (x === 3) return 'c';\n"
        "  return 'b';\n"
        "}\n"
    )
    _new, applied = apply_rules(src)
    assert applied == []


def test_if_ladder_skips_non_throw_terminal():
    """A terminal `return -1` (sentinel) must NOT be rewritten."""
    src = (
        "function f(x) {\n"
        "  if (x === 1) return 'a';\n"
        "  else if (x === 2) return 'b';\n"
        "  return -1;\n"
        "}\n"
    )
    _new, applied = apply_rules(src)
    assert applied == []


def test_accumulator_to_direct():
    src = (
        "function padRight(s, width) {\n"
        "  let out = s;\n"
        "  while (out.length < width) {\n"
        "    out = out + ' ';\n"
        "  }\n"
        "  return out;\n"
        "}\n"
    )
    new, applied = apply_rules(src)
    assert applied == ["accumulator-to-direct"]
    assert "let out" not in new
    assert "while (s.length < width)" in new
    assert "return s;" in new


def test_accumulator_skips_when_init_is_expression():
    """Only plain identifier initializers qualify for direct substitution."""
    src = (
        "function f(x) {\n"
        "  let out = x + 1;\n"
        "  while (out < 10) out = out + 1;\n"
        "  return out;\n"
        "}\n"
    )
    _new, applied = apply_rules(src)
    assert applied == []


def test_boolean_chain_collapse():
    src = (
        "function isPositive(n) {\n"
        "  if (typeof n !== 'number') return false;\n"
        "  if (n < 0) return false;\n"
        "  if (!isFinite(n)) return false;\n"
        "  return true;\n"
        "}\n"
    )
    new, applied = apply_rules(src)
    assert applied == ["boolean-chain-collapse"]
    assert "return !" in new
    assert "return false" not in new


def test_boolean_chain_with_combined_condition():
    """An if-condition that is itself an `||` chain still works."""
    src = "function f(x) {\n  if (x < 0 || x > 100) return false;\n  return true;\n}\n"
    new, applied = apply_rules(src)
    assert applied == ["boolean-chain-collapse"]
    assert "return !(x < 0 || x > 100)" in new


def test_rules_idempotent_on_already_reduced():
    """Re-applying on already-reduced source is a no-op."""
    src = (
        "function monthName(month) {\n"
        "  if (month < 1 || month > 12) throw new RangeError('bad');\n"
        "  return ['January', 'February', 'December'][month - 1];\n"
        "}\n"
    )
    _new, applied = apply_rules(src)
    assert applied == []


def test_rule_does_not_break_already_compact_source():
    src = "function add(a, b) { return a + b; }\n"
    new, applied = apply_rules(src)
    assert applied == []
    assert new == src


def test_boolean_chain_with_inline_const():
    """`if (...) return false; const X = EXPR; if (X > N) return false; ...` inlines X."""
    src = (
        "function isValidDate(s) {\n"
        "  if (typeof s !== 'string') return false;\n"
        "  const month = Number(s.substring(5, 7));\n"
        "  if (month < 1 || month > 12) return false;\n"
        "  return true;\n"
        "}\n"
    )
    new, applied = apply_rules(src)
    assert applied == ["boolean-chain-collapse"]
    assert "(Number(s.substring(5, 7)))" in new
    assert "month" not in new.split("return")[1]  # not in the collapsed form


def test_for_loop_with_early_return_to_every():
    """for-loop that returns false on bad values -> .every()."""
    src = (
        "function columnIsNumeric(values) {\n"
        "  for (let i = 0; i < values.length; i++) {\n"
        "    if (typeof values[i] !== 'number' || !isFinite(values[i])) {\n"
        "      return false;\n"
        "    }\n"
        "  }\n"
        "  return true;\n"
        "}\n"
    )
    new, applied = apply_rules(src)
    assert applied == ["for-loop-with-early-return-to-every"]
    assert ".every(function (v)" in new
    assert "for (" not in new


def test_multi_condition_ladder():
    """Multiple `if (X === N1 || X === N2 || ...) return V;` -> array lookup."""
    src = (
        "export function quarterOfMonth(month) {\n"
        "  if (month === 1 || month === 2 || month === 3) { return 1; }\n"
        "  else if (month === 4 || month === 5 || month === 6) { return 2; }\n"
        "  else if (month === 7 || month === 8 || month === 9) { return 3; }\n"
        "  else if (month === 10 || month === 11 || month === 12) { return 4; }\n"
        "  throw new RangeError('bad');\n"
        "}\n"
    )
    new, applied = apply_rules(src)
    assert applied == ["multi-condition-ladder"]
    assert "[1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4][month - 1]" in new


def test_push_loop_to_map():
    src = """function rows(buckets) {
  const out = [];
  for (let i = 0; i < buckets.length; i++) {
    out.push([buckets[i].name, buckets[i].count]);
  }
  return out;
}
"""
    new, applied = apply_rules(src)
    assert "push-loop-to-map" in applied
    assert "buckets.map(function (value, i)" in new
    assert "[value.name, value.count]" in new


def test_copy_loop_to_slice_preserves_fractional_loop_bound():
    src = """function take(sorted, limit) {
  const out = [];
  const count = Math.min(limit, sorted.length);
  for (let i = 0; i < count; i++) {
    out.push(sorted[i]);
  }
  return out;
}
"""
    new, applied = apply_rules(src)
    assert "copy-loop-to-slice" in applied
    assert "slice(0, Math.ceil(limit))" in new


def test_repeat_loop():
    src = """function bar(title) {
  let out = '';
  for (let i = 0; i < title.length; i++) {
    out = out + '=';
  }
  return out;
}
"""
    new, applied = apply_rules(src)
    assert "repeat-loop" in applied
    assert "let out = '='.repeat(title.length);" in new


def test_conditional_return_preserves_bare_undefined():
    src = """function peek(head) {
  if (!head) {
    return;
  }
  return head.value;
}
"""
    new, applied = apply_rules(src, {"conditional-return"})
    assert applied == ["conditional-return"]
    assert "return head ? head.value : void 0;" in new


def test_conditional_return_does_not_cross_comment():
    src = """function f(value) {
  if (value) return 1;
  // The fallback is intentionally lazy.
  return fallback();
}
"""
    assert apply_rules(src, {"conditional-return"}) == (src, [])


def test_inline_return_binding_requires_no_other_reference():
    reducible = "function f() {\n  const value = build();\n  return value;\n}\n"
    new, applied = apply_rules(reducible, {"inline-return-binding"})
    assert applied == ["inline-return-binding"]
    assert "return build();" in new

    observed = (
        "function f() {\n  const value = build();\n  log(value);\n  return value;\n}\n"
    )
    assert apply_rules(observed, {"inline-return-binding"}) == (observed, [])

    mutable = "function f() {\n  let value = build();\n  return value;\n}\n"
    assert apply_rules(mutable, {"inline-return-binding"}) == (mutable, [])


def test_inline_return_binding_can_forward_into_call():
    source = """function makeTemporary(name) {
  const prefix = path.join(tmpdir(), name);
  return fs.make(prefix);
}
"""
    new, applied = apply_rules(source, {"inline-return-binding"})
    assert applied == ["inline-return-binding"]
    assert "return fs.make(path.join(tmpdir(), name));" in new


def test_inline_return_binding_preserves_multiline_expression_indent():
    source = """function drained() {
\tconst promise = new Promise(resolve => {
\t\tcomplete = resolve;
\t});
\treturn promise;
}
"""
    new, applied = apply_rules(source, {"inline-return-binding"})
    assert applied == ["inline-return-binding"]
    assert (
        new
        == """function drained() {
\treturn new Promise(resolve => {
\t\tcomplete = resolve;
\t});
}
"""
    )


def test_concise_arrow_return():
    source = """const classify = value => {
  return value ? 'yes' : 'no';
};
"""
    new, applied = apply_rules(source, {"concise-arrow-return"})
    assert applied == ["concise-arrow-return"]
    assert new == "const classify = value => value ? 'yes' : 'no';\n"


def test_private_substring_replace_loop_collapses():
    src = """let format = (string, close, replace, index) =>
  ~index ? prefix + replaceClose(string, close, replace, index) : string
let replaceClose = (string, close, replace, index) => {
  let result = "", cursor = 0
  do {
    result += string.substring(cursor, index) + replace
    cursor = index + close.length
    index = string.indexOf(close, cursor)
  } while (~index)
  return result + string.substring(cursor)
}
"""
    new, applied = apply_rules(src, {"substring-replace-loop"})
    assert applied == ["substring-replace-loop"]
    assert ".substring(index).split(close).join(replace)" in new
    assert "do {" not in new


def test_unbrace_single_statement_preserves_else_and_asi_hazards():
    source = "if (ready) { run(); } else { wait(); }\n"
    reduced, applied = apply_rules(source, {"unbrace-single-statement"})
    assert len(applied) == 2
    assert reduced == "if (ready) run(); else wait();\n"

    hazard = "while (ready) { value() }\n[1].forEach(run)\n"
    assert apply_rules(hazard, {"unbrace-single-statement"}) == (hazard, [])


def test_js_hoist_common_tail_does_not_normalize_literal_contents():
    source = 'if (ready) { first(); emit("a b"); } else { second(); emit("a  b"); }\n'
    assert apply_rules(source, {"hoist-common-tail"}) == (source, [])

    matching = source.replace('emit("a  b")', 'emit("a b")')
    reduced, applied = apply_rules(matching, {"hoist-common-tail"})
    assert applied
    assert reduced.count('emit("a b")') == 1
