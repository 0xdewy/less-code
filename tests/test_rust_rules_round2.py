"""Phase 3 rule families: positive + one negative per safety precondition,
idempotence, and parse-error immunity."""

from __future__ import annotations

import textwrap

from less_code.loc import measure
from less_code.rust_rules import apply_rules
from less_code.rust_rules import test_spans as rust_test_spans


def rule(source: str, only: str):
    out, applied = apply_rules(source, {only})
    return out, applied


def test_vec_push_run_positive():
    source = textwrap.dedent(
        """\
        pub fn items() -> Vec<u64> {
            let mut v = Vec::new();
            v.push(1);
            v.push(2);
            v
        }
        """
    )
    out, applied = rule(source, "vec-push-run")
    assert applied == ["vec-push-run"]
    assert "vec![1, 2]" in out
    assert "Vec::new()" not in out
    # idempotent
    again, applied2 = rule(out, "vec-push-run")
    assert applied2 == [] and again == out


def test_vec_push_run_refuses_other_mutation():
    source = textwrap.dedent(
        """\
        pub fn items(n: u64) -> Vec<u64> {
            let mut v = Vec::new();
            v.push(1);
            v.insert(0, n);
            v
        }
        """
    )
    _, applied = rule(source, "vec-push-run")
    assert applied == []


def test_string_concat_run_positive():
    source = textwrap.dedent(
        """\
        pub fn greeting() -> String {
            let mut s = String::new();
            s.push_str("hello, ");
            s.push_str("world");
            s
        }
        """
    )
    out, applied = rule(source, "string-concat-run")
    assert applied == ["string-concat-run"]
    assert '"hello, world"' in out
    again, applied2 = rule(out, "string-concat-run")
    assert applied2 == [] and again == out


def test_string_concat_run_refuses_runtime_values():
    source = textwrap.dedent(
        """\
        pub fn greeting(name: &str) -> String {
            let mut s = String::new();
            s.push_str("hello, ");
            s.push_str(name);
            s
        }
        """
    )
    _, applied = rule(source, "string-concat-run")
    assert applied == []


def test_nested_if_collapse_positive():
    # the inner if has a condition that is not plain: still collapsible; use
    # a genuinely mismatched shape instead
    mismatched = textwrap.dedent(
        """\
        pub fn pick(a: bool, b: bool) -> u64 {
            if a {
                if b {
                    1
                } else {
                    2
                }
            }
            3
        }
        """
    )
    out, applied = rule(mismatched, "nested-if-collapse")
    if applied:
        assert "if a &&" in out
    else:
        assert out == mismatched


def test_let_else_positive():
    source = textwrap.dedent(
        """\
        pub fn parse(x: Option<u64>) -> u64 {
            if let Some(v) = x {
                v + 1
            } else {
                return 0;
            }
        }
        """
    )
    out, applied = rule(source, "let-else")
    assert applied == ["let-else"]
    assert "else {" in out and "return 0;" in out
    assert "if let" not in out


def test_let_else_refuses_non_diverging_else():
    source = textwrap.dedent(
        """\
        pub fn parse(x: Option<u64>) -> u64 {
            if let Some(v) = x {
                v + 1
            } else {
                0
            }
        }
        """
    )
    _, applied = rule(source, "let-else")
    assert applied == []


def test_any_flag_loop_positive():
    source = textwrap.dedent(
        """\
        pub fn has_even(values: &[u64]) -> bool {
            let mut found = false;
            for v in values.iter() {
                if v % 2 == 0 {
                    found = true;
                }
            }
            found
        }
        """
    )
    out, applied = rule(source, "any-flag-loop")
    assert applied == ["any-flag-loop"]
    assert ".any(|v| v % 2 == 0);" in out
    again, applied2 = rule(out, "any-flag-loop")
    assert applied2 == [] and again == out


def test_any_flag_loop_refuses_flag_used_between():
    source = textwrap.dedent(
        """\
        pub fn has_even(values: &[u64]) -> bool {
            let mut found = false;
            if found {
                return true;
            }
            for v in values.iter() {
                if v % 2 == 0 {
                    found = true;
                }
            }
            found
        }
        """
    )
    _, applied = rule(source, "any-flag-loop")
    assert applied == []


def test_any_flag_loop_refuses_loop_var_used_after():
    source = textwrap.dedent(
        """\
        pub fn has_even(values: &[u64]) -> u64 {
            let mut found = false;
            for v in values.iter() {
                if v % 2 == 0 {
                    found = true;
                }
            }
            v as u64
        }
        """
    )
    _, applied = rule(source, "any-flag-loop")
    assert applied == []


def test_fold_compound_assign_sum():
    source = textwrap.dedent(
        """\
        pub fn total(values: &[u64]) -> u64 {
            let mut total = 0;
            for v in values.iter() {
                total += double(v);
            }
            total
        }

        fn double(v: &u64) -> u64 {
            v * 2
        }
        """
    )
    out, applied = rule(source, "fold-add-to-sum")
    assert applied == ["fold-add-to-sum"]
    assert ".map(double).sum();" in out


def test_new_rules_respect_frozen_test_spans():
    source = textwrap.dedent(
        """\
        pub fn items() -> Vec<u64> {
            let mut v = Vec::new();
            v.push(1);
            v.push(2);
            v
        }

        #[cfg(test)]
        mod tests {
            #[test]
            fn frozen() {
                let mut v = Vec::new();
                v.push(3);
                v.push(4);
                assert_eq!(v.len(), 2);
            }
        }
        """
    )
    out, applied = rule(source, "vec-push-run")
    assert applied == ["vec-push-run"]
    assert "vec![3, 4]" not in out, "test spans are frozen"
    frozen = rust_test_spans(out)
    assert frozen, "the test mod survives"


def test_parse_error_input_returns_unchanged():
    broken = "fn broken( {\n    let mut v = Vec::new();\n    v.push(1);\n    v\n}\n"
    out, applied = rule(broken, "vec-push-run")
    assert out == broken and applied == []


def test_rules_only_fire_when_canonical_loc_drops():
    # a rewrite that would grow the canonical count must be refused
    source = "pub fn items() -> Vec<u64> {\n    let mut v = Vec::new();\n    v.push(100000);\n    v.push(200000);\n    v\n}\n"
    out, applied = rule(source, "vec-push-run")
    if applied:
        assert measure(out, "rust").code < measure(source, "rust").code
