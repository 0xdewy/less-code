"""C4 rule library: before/after per rule, the guards that must *not* fire,
and proof that a misfiring rule is caught by the pipeline's test gate."""

import pathlib
import textwrap

import pytest

from less_code.rules import RULES, apply_rules


def norm(text: str) -> str:
    return textwrap.dedent(text).strip() + "\n"


# ---- bool-return -----------------------------------------------------------


@pytest.mark.parametrize(
    "before,after",
    [
        (
            """
            def f(a, b):
                if a > b:
                    return True
                return False
            """,
            """
            def f(a, b):
                return a > b
            """,
        ),
        (
            """
            def f(a, b):
                if a > b:
                    return True
                else:
                    return False
            """,
            """
            def f(a, b):
                return a > b
            """,
        ),
        (
            """
            def f(a, b):
                if a > b:
                    return False
                return True
            """,
            """
            def f(a, b):
                return not a > b
            """,
        ),
        (  # a non-boolean test must be wrapped: `return x` would leak the value
            """
            def f(x):
                if x:
                    return True
                return False
            """,
            """
            def f(x):
                return bool(x)
            """,
        ),
        (
            """
            def f(x, y):
                if x > 0 and y > 0:
                    return True
                return False
            """,
            """
            def f(x, y):
                return x > 0 and y > 0
            """,
        ),
    ],
)
def test_bool_return(before, after):
    out, applied = apply_rules(norm(before))
    assert out == norm(after)
    # an explicit `else:` first meets else-after-terminator (which dedents the
    # arm), then bool-return collapses the shape: same output, two rule names
    assert applied in (["bool-return"], ["else-after-terminator", "bool-return"])


def test_bool_return_leaves_other_constants_alone():
    src = norm(
        """
        def f(x):
            if x:
                return 1
            return 0
        """
    )
    assert apply_rules(src) == (src, [])


# ---- append-loop-to-comprehension -----------------------------------------


def test_append_loop_to_comprehension():
    out, applied = apply_rules(
        norm(
            """
            def f(rows):
                out = []
                for row in rows:
                    out.append(row.name)
                return out
            """
        )
    )
    assert out == norm(
        """
        def f(rows):
            out = [row.name for row in rows]
            return out
        """
    )
    assert applied == ["append-loop-to-comprehension"]


def test_append_identity_loop_becomes_list_call():
    out, _ = apply_rules(
        norm(
            """
            def f(d):
                keys = []
                for k in d.keys():
                    keys.append(k)
                return keys
            """
        )
    )
    assert "keys = list(d.keys())" in out


def test_append_loop_with_single_use_temp_prefix():
    out, applied = apply_rules(
        norm(
            """
            def f(d, order):
                out = []
                for k in order:
                    item = d[k]
                    out.append(item.qty)
                return out
            """
        )
    )
    assert out == norm(
        """
        def f(d, order):
            out = [d[k].qty for k in order]
            return out
        """
    )
    assert applied == ["append-loop-to-comprehension"]


def test_append_loop_not_rewritten_when_temp_used_twice():
    """Inlining a twice-read temp would evaluate `d[k]` twice — refuse."""
    src = norm(
        """
        def f(d, order):
            out = []
            for k in order:
                item = d[k]
                out.append(item.qty + item.cost)
            return out
        """
    )
    assert apply_rules(src) == (src, [])


def test_append_loop_not_rewritten_when_loop_var_leaks():
    """`for` targets survive the loop in Python; a later read forbids the rewrite."""
    src = norm(
        """
        def f(rows):
            out = []
            for row in rows:
                out.append(row.name)
            return out, row
        """
    )
    assert apply_rules(src, {"append-loop-to-comprehension"}) == (src, [])


def test_append_loop_with_guard_becomes_a_comprehension_if_clause():
    """Iteration 07 declined this shape; iteration 08 takes it. The guard is
    evaluated once per iteration at the same point in both forms."""
    src = norm(
        """
        def f(rows):
            out = []
            for row in rows:
                if row.ok:
                    out.append(row.name)
            return out
        """
    )
    new_src, applied = apply_rules(src, {"append-loop-to-comprehension"})
    assert applied == ["append-loop-to-comprehension"]
    assert "out = [row.name for row in rows if row.ok]" in new_src
    assert _behaves_the_same(src, new_src, "f", [_Rows()])


def test_guard_with_a_call_is_refused():
    """A call in the guard is the conservative line: refuse rather than
    reason about what the call does."""
    src = norm(
        """
        def f(rows):
            out = []
            for row in rows:
                if row.name.startswith("a"):
                    out.append(row.name)
            return out
        """
    )
    assert apply_rules(src, {"append-loop-to-comprehension"}) == (src, [])


def test_guard_with_an_else_branch_is_refused():
    src = norm(
        """
        def f(rows):
            out = []
            for row in rows:
                if row.ok:
                    out.append(1)
                else:
                    out.append(2)
            return out
        """
    )
    assert apply_rules(src, {"append-loop-to-comprehension"}) == (src, [])


def test_twice_read_temp_is_walrus_bound_in_the_guard():
    """The `low_stock` shape from the py fixture: `product` is read twice, so
    it cannot be inlined — but it is the guard's first-evaluated name, so the
    walrus lands exactly where the assignment stood."""
    src = norm(
        """
        def f(d, order):
            out = []
            for k in order:
                item = d[k]
                if item.qty <= item.floor:
                    out.append(k)
            return out
        """
    )
    new_src, applied = apply_rules(src, {"append-loop-to-comprehension"})
    assert applied == ["append-loop-to-comprehension"]
    assert "(item := d[k]).qty <= item.floor" in new_src
    assert _behaves_the_same(src, new_src, "f", [{"a": _Item(1, 5), "b": _Item(9, 5)}, ["a", "b"]])


def test_walrus_is_refused_when_the_temp_is_not_evaluated_first():
    """`limit()` would run before `d[k]` in the comprehension but after it in
    the loop — a reordering, so the rule declines."""
    src = norm(
        """
        def f(d, order, limit):
            out = []
            for k in order:
                item = d[k]
                if limit > 0 and item.qty <= item.floor:
                    out.append(k)
            return out
        """
    )
    assert apply_rules(src, {"append-loop-to-comprehension"}) == (src, [])


def test_two_twice_read_temps_are_refused():
    src = norm(
        """
        def f(d, order):
            out = []
            for k in order:
                item = d[k]
                cap = item.floor
                if item.qty <= cap and cap > 0 and item.qty > 0:
                    out.append(k)
            return out
        """
    )
    assert apply_rules(src, {"append-loop-to-comprehension"}) == (src, [])


def test_guarded_accumulate_becomes_a_filtered_sum():
    src = norm(
        """
        def f(rows):
            total = 0
            for row in rows:
                if row.ok:
                    total += row.n
            return total
        """
    )
    new_src, applied = apply_rules(src, {"accumulate-to-sum"})
    assert applied == ["accumulate-to-sum"]
    assert "total = sum((row.n for row in rows if row.ok))" in new_src


def test_walrus_in_a_class_body_never_ships():
    """A walrus inside a class-body comprehension is a SyntaxError. The rule
    would emit one here; `apply_rules` re-parses and returns the input."""
    src = norm(
        """
        class C:
            data = {"a": 1}
            keys = ["a"]
            out = []
            for k in keys:
                item = data[k]
                if item > 0 and item < 9:
                    out.append(k)
        """
    )
    new_src, applied = apply_rules(src, {"append-loop-to-comprehension"})
    compile(new_src, "<test>", "exec")  # would raise before the class-body guard
    assert applied == [] and new_src == src


# ---- accumulate-to-sum -----------------------------------------------------


def test_accumulate_augassign_to_sum():
    out, applied = apply_rules(
        norm(
            """
            def f(rows):
                total = 0
                for row in rows:
                    total += row.qty
                return total
            """
        )
    )
    assert "total = sum((row.qty for row in rows))" in out
    assert applied == ["accumulate-to-sum"]


def test_accumulate_legacy_assign_form_to_sum():
    out, applied = apply_rules(
        norm(
            """
            def f(rows):
                total = 0
                for row in rows:
                    total = total + row.qty
                return total
            """
        )
    )
    assert "total = sum((row.qty for row in rows))" in out
    assert applied == ["accumulate-to-sum"]


def test_accumulate_float_start_is_preserved():
    """`0.0` must stay a float even when the iterable is empty."""
    out, _ = apply_rules(
        norm(
            """
            def f(rows):
                total = 0.0
                for row in rows:
                    total += row.cost
                return total
            """
        )
    )
    assert "sum((row.cost for row in rows), 0.0)" in out
    ns = {}
    exec(out, ns)
    assert isinstance(ns["f"]([]), float)


def test_accumulate_not_rewritten_for_non_add():
    src = norm(
        """
        def f(rows):
            total = 0
            for row in rows:
                total -= row.qty
            return total
        """
    )
    assert apply_rules(src) == (src, [])


# ---- sort-to-sorted --------------------------------------------------------


def test_sort_to_sorted_composes_with_the_append_rule():
    out, applied = apply_rules(
        norm(
            """
            def f(d):
                keys = []
                for k in d:
                    keys.append(k)
                keys.sort()
                return keys
            """
        )
    )
    assert out == norm(
        """
        def f(d):
            keys = sorted(d)
            return keys
        """
    )
    assert set(applied) == {"append-loop-to-comprehension", "sort-to-sorted"}


def test_sort_to_sorted_keeps_keywords():
    out, _ = apply_rules(
        norm(
            """
            def f(rows):
                out = [r.name for r in rows]
                out.sort(reverse=True)
                return out
            """
        )
    )
    assert "out = sorted([r.name for r in rows], reverse=True)" in out


def test_sort_not_rewritten_on_an_aliased_list():
    """`x = other` may alias someone else's list; sorting it in place is visible."""
    src = norm(
        """
        def f(other):
            x = other
            x.sort()
            return x
        """
    )
    assert apply_rules(src) == (src, [])


# ---- drop-bare-reraise -----------------------------------------------------


def test_drop_bare_reraise():
    out, applied = apply_rules(
        norm(
            """
            def f(self, n):
                try:
                    value = self.get(n)
                    value += 1
                except KeyError:
                    raise
                return value
            """
        )
    )
    assert out == norm(
        """
        def f(self, n):
            value = self.get(n)
            value += 1
            return value
        """
    )
    assert applied == ["drop-bare-reraise"]


def test_reraise_with_handler_logic_is_kept():
    src = norm(
        """
        def f(self, n):
            try:
                return self.get(n)
            except KeyError:
                log('boom')
                raise
        """
    )
    assert apply_rules(src) == (src, [])


def test_try_with_finally_is_kept():
    src = norm(
        """
        def f(self, n):
            try:
                return self.get(n)
            except KeyError:
                raise
            finally:
                self.close()
        """
    )
    assert apply_rules(src) == (src, [])


# ---- general contracts -----------------------------------------------------


def test_comments_outside_the_rewrite_survive():
    out, _ = apply_rules(
        norm(
            """
            # module header comment
            def f(a, b):
                # explains the check
                if a > b:
                    return True
                return False

            # trailing note
            """
        )
    )
    assert "# module header comment" in out
    assert "# explains the check" in out
    assert "# trailing note" in out


def test_rules_can_be_selected_individually():
    src = norm(
        """
        def f(a, b):
            if a > b:
                return True
            return False
        """
    )
    unchanged, applied = apply_rules(src, {"accumulate-to-sum"})
    assert (unchanged, applied) == (src, [])
    changed, applied = apply_rules(src, {"bool-return"})
    assert changed != src and applied == ["bool-return"]


def test_unparsable_source_is_returned_untouched():
    src = "def f(:\n"
    assert apply_rules(src) == (src, [])


def test_semicolon_packed_lines_are_not_clobbered():
    """The splice works on whole lines, so a shared line must be refused."""
    src = "def f(a, b):\n    x = 1; total = 0\n    for v in b:\n        total += v\n    return total, x\n"
    out, applied = apply_rules(src)
    assert applied == [] and out == src


def test_every_rule_name_is_reachable():
    assert set(RULES) == {
        "bool-return",
        "append-loop-to-comprehension",
        "accumulate-to-sum",
        "sort-to-sorted",
        "drop-bare-reraise",
        "if-ladder-to-dict",
        "threshold-ladder-to-scan",
        "max-loop-to-max",
        "else-after-terminator",
        "loop-dict-to-update",
    }


# ---- else-after-terminator -------------------------------------------------


def test_else_after_raise_collapses_and_keeps_comments():
    """The exact shape a 7B kept 'discovering' on click (ruff RET505 has no
    autofix): 15 sites in click alone. The arm must keep its comments and
    docstrings verbatim — the rule dedents text, it never re-renders."""
    src = norm(
        """
        def invoke(ctx):
            if ctx is None:
                raise TypeError("ctx required")
            else:
                # the context owns cleanup from here on
                with ctx:
                    return ctx.run()
        """
    )
    out, applied = apply_rules(src)
    assert applied == ["else-after-terminator"]
    assert out == norm(
        """
        def invoke(ctx):
            if ctx is None:
                raise TypeError("ctx required")
            # the context owns cleanup from here on
            with ctx:
                return ctx.run()
        """
    )


def test_else_after_return_in_a_loop():
    src = norm(
        """
        def find(rows):
            for row in rows:
                if row.bad:
                    return None
                else:
                    value = row.value
                    return value
        """
    )
    out, applied = apply_rules(src)
    assert applied == ["else-after-terminator"]
    assert "else" not in out


def test_if_without_terminator_is_untouched():
    src = norm(
        """
        def f(c):
            if c:
                log(c)
            else:
                return 1
            return 0
        """
    )
    assert apply_rules(src) == (src, [])


def test_elif_chain_keeps_its_elifs_but_drops_the_trailing_else():
    """The chain's LAST else is a plain else-arm on the innermost if (whose
    body ends in return), so it dedents legally; the elif line itself is
    never rewritten. Behavior must be identical, verified by execution."""
    src = norm(
        """
        def f(x):
            if x < 0:
                raise ValueError("neg")
            elif x == 0:
                return "zero"
            else:
                return "pos"
        """
    )
    out, applied = apply_rules(src)
    assert applied == ["else-after-terminator"]
    assert "elif x == 0:" in out and "else" not in out
    ns = {}
    exec(compile(out, "<t>", "exec"), ns)
    assert ns["f"](5) == "pos" and ns["f"](0) == "zero"
    try:
        ns["f"](-1)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_nested_collapses_apply_over_passes():
    src = norm(
        """
        def f(x):
            if x < 0:
                raise ValueError("neg")
            else:
                if x == 0:
                    raise ValueError("zero")
                else:
                    return "pos"
        """
    )
    out, applied = apply_rules(src)
    assert applied == ["else-after-terminator", "else-after-terminator"]
    assert "else" not in out
    compile(out, "<t>", "exec")


# ---- loop-dict-to-update ---------------------------------------------------


def test_plain_dict_copy_loop_becomes_update():
    src = norm(
        """
        def merge(base, extra):
            for key, value in extra.items():
                base[key] = value
            return base
        """
    )
    out, applied = apply_rules(src)
    assert applied == ["loop-dict-to-update"]
    assert "base.update(extra)" in out
    compile(out, "<t>", "exec")


def test_guarded_merge_becomes_a_comprehension_update():
    src = norm(
        """
        def merge(base, extra):
            for key, value in extra.items():
                if key not in base:
                    base[key] = value
            return base
        """
    )
    out, applied = apply_rules(src)
    assert applied == ["loop-dict-to-update"]
    assert "base.update({" in out and "if key not in base})" in out
    compile(out, "<t>", "exec")


def test_dict_loop_with_leaked_target_is_declined():
    src = norm(
        """
        def merge(base, extra):
            for key, value in extra.items():
                base[key] = value
            return key
        """
    )
    assert apply_rules(src) == (src, [])


def test_dict_loop_with_extra_body_is_declined():
    src = norm(
        """
        def merge(base, extra, seen):
            for key, value in extra.items():
                seen.add(key)
                base[key] = value
            return base
        """
    )
    assert apply_rules(src) == (src, [])


# ---- the gate catches a misfiring rule ------------------------------------

MISFIRE_MODULE = '''
def is_low(on_hand, reorder):
    if on_hand <= reorder:
        return True
    return False


def total(rows):
    total = 0
    for r in rows:
        total += r
    return total
'''.lstrip()

MISFIRE_TESTS = '''
from mod import is_low, total

def test_is_low():
    assert is_low(1, 5) is True
    assert is_low(9, 5) is False

def test_total():
    assert total([1, 2, 3]) == 6
'''.lstrip()


def _misfire(body, index, scope_lines, class_body=False):
    """A deliberately wrong 'bool-return': always returns False."""
    import ast

    from less_code.rules import Rewrite, _end_col, _span

    stmt = body[index]
    if not isinstance(stmt, ast.If) or len(stmt.body) != 1:
        return None
    if not isinstance(stmt.body[0], ast.Return):
        return None
    if stmt.orelse or index + 1 >= len(body) or not isinstance(body[index + 1], ast.Return):
        return None
    covered = [stmt, body[index + 1]]
    start, end = _span(covered)
    return Rewrite(
        "bool-return", start, end,
        [ast.Return(value=ast.Constant(value=False))],
        stmt.col_offset, _end_col(covered, end),
    )


def test_pipeline_gate_reverts_a_misfiring_rule(tmp_path, monkeypatch):
    """A wrong rule must cost only that rule, never a wrong acceptance.

    The narrowing path keeps `accumulate-to-sum` (correct) and drops the
    sabotaged `bool-return` because the frozen suite goes red with it.
    """
    from less_code import rules
    from less_code.pipeline import reduce_project

    monkeypatch.setitem(rules._RULE_FNS, "bool-return", _misfire)

    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(MISFIRE_MODULE)
    (root / "test_mod.py").write_text(MISFIRE_TESTS)

    stats = reduce_project(root, backend=None, formatter=False)
    source = (root / "mod.py").read_text()

    assert stats.tests_ok
    assert any("rule bool-return reverted by the gate" in n for n in stats.static_notes)
    assert "sum(" in source  # the correct rule survived the narrowing
    # behaviour, not text: a later layer (ruff's RET/SIM tier) may legitimately
    # perform the same collapse the sabotaged rule got wrong, so what must hold
    # is that `is_low` still answers correctly.
    ns: dict = {}
    exec(source, ns)
    assert ns["is_low"](1, 5) is True and ns["is_low"](9, 5) is False


def test_pipeline_gate_reverts_everything_without_a_runner(tmp_path, monkeypatch):
    """With no runner the static layer is all-or-nothing: the pipeline's own
    gate reverts the whole edit set rather than accepting a broken rule."""
    from less_code import rules
    from less_code.langdetect import map_project
    from less_code.static import static_pass

    monkeypatch.setitem(rules._RULE_FNS, "bool-return", _misfire)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(MISFIRE_MODULE)
    (root / "test_mod.py").write_text(MISFIRE_TESTS)
    project = map_project(root)
    result = static_pass(root, "python", project.source_files,
                         project.source_files + project.test_files)
    assert "return False\n" in result.changed_files[str(root / "mod.py")]
    # nothing was written: static_pass is pure without a runner
    assert "if on_hand <= reorder:" in (root / "mod.py").read_text()


def test_backend_timeout_is_configurable():
    """A 600s ceiling turns a slow-but-correct rewrite into a lost LLM call."""
    from less_code.backends import DEFAULT_TIMEOUT, OllamaBackend, OpenAICompatBackend

    assert DEFAULT_TIMEOUT == 600
    assert OllamaBackend(model="m").timeout == 600
    assert OllamaBackend(model="m", timeout=2400).timeout == 2400
    assert OpenAICompatBackend(model="m", timeout=90).timeout == 90


def test_bench_row_records_attempt_outcomes(tmp_path):
    """A finished bench row must stay diagnosable: the scratch tree is deleted
    and the attempt records live only in ReduceStats, so the tally is the only
    surviving evidence of what the LLM layer actually did."""
    from less_code.bench import BenchRow

    row = BenchRow(fixture="x", lang="python", config="c", commit="d", timestamp="t")
    assert row.attempt_outcomes == {}
    row.attempt_outcomes = {"tests-failed": 2, "hunks-accepted": 1}
    import dataclasses
    assert dataclasses.asdict(row)["attempt_outcomes"]["hunks-accepted"] == 1


class _Item:
    def __init__(self, qty, floor):
        self.qty, self.floor = qty, floor


class _Rows:
    """A row-ish object list stand-in for the guard tests."""

    def __init__(self):
        self.rows = [_Row("a", True), _Row("b", False), _Row("c", True)]

    def __iter__(self):
        return iter(self.rows)


class _Row:
    def __init__(self, name, ok):
        self.name, self.ok = name, ok


def _behaves_the_same(before: str, after: str, fn: str, args: list) -> bool:
    """Run the original and the rewrite side by side on the same input."""
    ns_a: dict = {}
    ns_b: dict = {}
    exec(before, ns_a)
    exec(after, ns_b)
    return ns_a[fn](*args) == ns_b[fn](*[a for a in args])


# ---- iteration 09: if/elif ladders and the manual max loop -----------------


LADDER_EQ = textwrap.dedent('''
    def priority(status):
        if status == "STOCKOUT":
            return 1
        elif status == "LOW":
            return 2
        elif status == "BACKORDER":
            return 3
        return 9
''').lstrip()

LADDER_THRESHOLD = textwrap.dedent('''
    def tier(qty):
        if qty >= 100:
            return 0.15
        elif qty >= 50:
            return 0.1
        elif qty >= 10:
            return 0.05
        return 0.0
''').lstrip()


def test_equality_ladder_becomes_a_dict_lookup():
    out, applied = apply_rules(LADDER_EQ, {"if-ladder-to-dict"})
    assert applied == ["if-ladder-to-dict"]
    assert '.get(status, 9)' in out
    for arg in ("STOCKOUT", "LOW", "BACKORDER", "WAT", None):
        assert _behaves_the_same(LADDER_EQ, out, "priority", [arg])


def test_consecutive_ifs_are_the_same_ladder():
    src = LADDER_EQ.replace("elif", "if").replace("    if status", "    if status")
    out, applied = apply_rules(src, {"if-ladder-to-dict"})
    assert applied == ["if-ladder-to-dict"]
    assert _behaves_the_same(src, out, "priority", ["LOW"])


def test_a_two_branch_ladder_is_left_alone():
    src = 'def f(x):\n    if x == "a":\n        return 1\n    return 0\n'
    assert apply_rules(src, {"if-ladder-to-dict"})[1] == []


def test_mixed_key_types_are_refused():
    src = 'def f(x):\n    if x == "a":\n        return 1\n    elif x == 2:\n        return 2\n    elif x == "c":\n        return 3\n    return 0\n'
    assert apply_rules(src, {"if-ladder-to-dict"})[1] == []


def test_bool_keys_are_refused_because_they_alias_ints():
    src = "def f(x):\n    if x == True:\n        return 1\n    elif x == 0:\n        return 2\n    elif x == 2:\n        return 3\n    return 9\n"
    assert apply_rules(src, {"if-ladder-to-dict"})[1] == []


def test_a_ladder_with_a_call_in_the_subject_is_refused():
    src = 'def f(x):\n    if x.get() == "a":\n        return 1\n    elif x.get() == "b":\n        return 2\n    elif x.get() == "c":\n        return 3\n    return 0\n'
    assert apply_rules(src, {"if-ladder-to-dict"})[1] == []


def test_a_non_constant_arm_is_refused():
    src = 'def f(x, y):\n    if x == "a":\n        return y\n    elif x == "b":\n        return 2\n    elif x == "c":\n        return 3\n    return 0\n'
    assert apply_rules(src, {"if-ladder-to-dict"})[1] == []


def test_threshold_ladder_becomes_an_ordered_scan_not_a_dict():
    out, applied = apply_rules(LADDER_THRESHOLD, {"if-ladder-to-dict", "threshold-ladder-to-scan"})
    assert applied == ["threshold-ladder-to-scan"]
    assert "next(" in out and ".get(" not in out
    for qty in (0, 9, 10, 49, 50, 99, 100, 1000):
        assert _behaves_the_same(LADDER_THRESHOLD, out, "tier", [qty])


def test_a_threshold_scan_keeps_the_comparison_so_a_type_error_still_raises():
    out, _applied = apply_rules(LADDER_THRESHOLD, {"threshold-ladder-to-scan"})
    ns: dict = {}
    exec(out, ns)
    with pytest.raises(TypeError):
        ns["tier"]("not a number")


def test_mixed_comparison_operators_are_refused():
    src = "def f(x):\n    if x >= 100:\n        return 1\n    elif x <= 5:\n        return 2\n    elif x >= 10:\n        return 3\n    return 0\n"
    assert apply_rules(src, {"threshold-ladder-to-scan"})[1] == []


def test_the_scan_variable_does_not_capture_the_subject_name():
    src = "def f(_t):\n    if _t >= 100:\n        return 1\n    elif _t >= 50:\n        return 2\n    elif _t >= 10:\n        return 3\n    return 0\n"
    out, applied = apply_rules(src, {"threshold-ladder-to-scan"})
    assert applied == ["threshold-ladder-to-scan"]
    for qty in (0, 10, 50, 100):
        assert _behaves_the_same(src, out, "f", [qty])


MAX_LOOP = textwrap.dedent('''
    def busiest(counts):
        best = None
        best_units = None
        for area in sorted(counts):
            units = counts[area]
            if best_units is None or units > best_units:
                best = area
                best_units = units
        return best
''').lstrip()


def test_a_none_sentinel_max_loop_becomes_max_with_a_key():
    out, applied = apply_rules(MAX_LOOP, {"max-loop-to-max"})
    assert applied == ["max-loop-to-max"]
    assert "max(sorted(counts), key=lambda area: counts[area], default=None)" in out
    for counts in ({}, {"a": 1}, {"b": 2, "a": 2}, {"a": 1, "b": 5, "c": 5}):
        assert _behaves_the_same(MAX_LOOP, out, "busiest", [counts])


def test_a_numeric_sentinel_max_loop_is_refused():
    """`busiest_area` in the py fixture: `best_units = -1` is NOT equivalent to
    `default=None` when every value is <= -1, and the AST cannot prove it is
    not. Declined on purpose, left for the LLM layer."""
    src = MAX_LOOP.replace("best_units = None", "best_units = -1").replace(
        "best_units is None or units > best_units", "units > best_units"
    )
    assert apply_rules(src, {"max-loop-to-max"})[1] == []


def test_a_max_loop_whose_sentinel_is_read_afterwards_is_refused():
    src = MAX_LOOP.replace("    return best", "    return best, best_units")
    assert apply_rules(src, {"max-loop-to-max"})[1] == []


def test_a_max_loop_with_an_impure_value_is_refused():
    src = MAX_LOOP.replace("units = counts[area]", "units = compute(area)")
    assert apply_rules(src, {"max-loop-to-max"})[1] == []


def test_the_new_rules_fire_on_the_py_fixture():
    """The two ladder rules exist because FIXTURE.md documents these shapes."""
    source = pathlib.Path("fixtures/py/inventory.py").read_text()
    _out, applied = apply_rules(source)
    assert "if-ladder-to-dict" in applied
    assert "threshold-ladder-to-scan" in applied
