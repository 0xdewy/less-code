"""C4 seed — the deterministic rule library ("peephole optimizer for LOC").

Each rule is an AST pattern match that produces a *semantics-preserving*
rewrite of a contiguous run of statements. Rules are cheap, repeatable and
reviewable; they run before the LLM so the model never spends tokens on the
transformations a machine can prove.

Two design choices matter:

* **Splice, don't unparse the world.** A rewrite is emitted as a line-range
  replacement into the original text, so comments, docstrings and formatting
  outside the rewritten statements survive verbatim. (`ast.unparse` of a whole
  module would silently delete every comment in the file.)
* **The gate is still the truth.** Rules are conservative by construction, but
  `static_pass` applies them under the frozen test suite: all rules at once,
  and — if that goes red — one rule at a time, keeping only the ones that stay
  green. A misfire costs a reverted rule, never a wrong acceptance.

Public API:

    apply_rules(source, only=None) -> (new_source, [rule names applied])
    RULES -> tuple of rule names
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

RULES = (
    "bool-return",
    "append-loop-to-comprehension",
    "accumulate-to-sum",
    "sort-to-sorted",
    "drop-bare-reraise",
    "if-ladder-to-dict",
    "threshold-ladder-to-scan",
    "max-loop-to-max",
)

MAX_PASSES = 6


@dataclass
class Rewrite:
    """Replace source lines [start, end] (1-based, inclusive) with `stmts`."""

    rule: str
    start: int
    end: int
    stmts: list[ast.stmt]
    col: int
    end_col: int = 0


# ---- helpers ---------------------------------------------------------------


def _is_name(node: ast.AST, name: str | None = None) -> bool:
    return isinstance(node, ast.Name) and (name is None or node.id == name)


def _is_const(node: ast.AST, value) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _boolish(node: ast.expr) -> bool:
    """True when the expression provably already evaluates to a bool."""
    if isinstance(node, ast.Compare):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    if isinstance(node, ast.BoolOp):
        return all(_boolish(v) for v in node.values)
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return True
    return bool(isinstance(node, ast.Call) and _is_name(node.func) and (node.func.id in ('bool', 'isinstance', 'issubclass', 'hasattr', 'callable', 'any', 'all')))


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _target_names(target: ast.expr) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def scope_name_lines(scope: ast.AST) -> dict[str, list[int]]:
    """Every `Name` occurrence in a scope, as name -> line numbers."""
    out: dict[str, list[int]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Name):
            out.setdefault(node.id, []).append(node.lineno)
    return out


def _leaks(scope_lines: dict[str, list[int]], names: set[str], start: int, end: int) -> bool:
    """Does a loop target escape the loop it is about to be absorbed into?

    A `for` target leaks into the enclosing *function* scope in Python, so a
    comprehension rewrite is only safe when every mention of that name in the
    scope lies inside the loop's own line span. Checked scope-wide rather than
    block-wide: the loop may sit inside an `if` whose sibling code reads it.
    """
    for name in names:
        for line in scope_lines.get(name, ()):
            if not (start <= line <= end):
                return True
    return False


def _call(func: str, args: list[ast.expr], keywords: list[ast.keyword] | None = None) -> ast.Call:
    return ast.Call(func=ast.Name(id=func, ctx=ast.Load()), args=args, keywords=keywords or [])


def _comprehension(
    target: ast.expr, iter_: ast.expr, ifs: list[ast.expr] | None = None
) -> ast.comprehension:
    stripped = ast.parse(ast.unparse(target)).body[0]
    tgt = stripped.value if isinstance(stripped, ast.Expr) else target
    return ast.comprehension(target=tgt, iter=iter_, ifs=list(ifs or []), is_async=0)


def _span(nodes: list[ast.stmt]) -> tuple[int, int]:
    start = min(n.lineno for n in nodes)
    for node in nodes:
        for dec in getattr(node, "decorator_list", []):
            start = min(start, dec.lineno)
    return start, max(n.end_lineno for n in nodes)


def _end_col(nodes: list[ast.stmt], end: int) -> int:
    return max(
        (n.end_col_offset or 0) for n in nodes if n.end_lineno == end
    )



class _Substitute(ast.NodeTransformer):
    def __init__(self, name: str, value: ast.expr) -> None:
        self.name, self.value = name, value

    def visit_Name(self, node: ast.Name):  # noqa: N802
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            return self.value
        return node


def _flatten_body(body: list[ast.stmt]) -> tuple[ast.stmt, set[str]] | None:
    """Collapse `t1 = e1; t2 = e2; <final>` into one statement, or None.

    Only fires when every prefix statement binds a plain name to an expression
    that the remainder reads *exactly once* — so inlining can neither duplicate
    an evaluation nor reorder side effects. The bound names are returned so the
    caller can check they do not leak out of the loop.
    """
    if not body:
        return None
    prefix, final = body[:-1], body[-1]
    names: set[str] = set()
    for stmt in prefix:
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and _is_name(stmt.targets[0])
        ):
            return None
        names.add(stmt.targets[0].id)
    if len(names) != len(prefix):
        return None  # a name rebound twice: not a straight-line temp chain
    for index, stmt in enumerate(prefix):
        name = stmt.targets[0].id
        rest = prefix[index + 1 :] + [final]
        uses = sum(
            1
            for node in rest
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
        )
        if uses != 1:
            return None
    merged = final
    for stmt in reversed(prefix):
        sub = _Substitute(stmt.targets[0].id, stmt.value)
        merged = sub.visit(merged)
    return merged, names


# ---- guarded loop bodies: the `if` clause and the walrus binding ------------
#
# Iteration 07 declined `low_stock` (and friends) for two reasons: the append
# was wrapped in an `if`, and the loop body read a temp twice. Both are
# expressible in a comprehension without weakening anything — an `if` clause,
# and a walrus in that clause — provided the *evaluation order* is preserved
# exactly. That is what `_first_evaluated_name` proves.


def _pure(node: ast.AST) -> bool:
    """No calls, lambdas, awaits, yields or existing walruses anywhere.

    A guard condition is evaluated once per iteration at the same point in
    both the loop and the comprehension, so ordering alone would allow calls;
    this is the deliberately conservative line (a misfiring guard is the one
    kind of rewrite the frozen suite might not catch).
    """
    return not any(
        isinstance(n, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda))
        for n in ast.walk(node)
    )


def _first_evaluated_name(node: ast.expr) -> str | None:
    """The name whose *load* happens first when `node` is evaluated.

    Walking the leftmost evaluation path (BoolOp -> values[0], Compare ->
    left, Attribute/Subscript -> value, ...). When that name is the temp we
    want to walrus-bind, replacing it with `(temp := value)` puts `value`
    exactly where the original assignment stood: no reordering at all, so the
    rewrite is order-preserving even for impure sub-expressions after it.
    """
    while True:
        if isinstance(node, ast.Name):
            return node.id if isinstance(node.ctx, ast.Load) else None
        if isinstance(node, ast.BoolOp):
            node = node.values[0]
        elif isinstance(node, (ast.Compare, ast.BinOp)):
            node = node.left
        elif isinstance(node, ast.UnaryOp):
            node = node.operand
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.IfExp):
            node = node.test
        else:
            return None


def _load_count(nodes: list[ast.AST | None], name: str) -> int:
    return sum(
        1
        for node in nodes
        if node is not None
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
    )


class _SubstituteFirst(ast.NodeTransformer):
    """Replace only the first `Load` of `name` (used for the walrus)."""

    def __init__(self, name: str, value: ast.expr) -> None:
        self.name, self.value, self.done = name, value, False

    def visit_Name(self, node: ast.Name):  # noqa: N802
        if not self.done and node.id == self.name and isinstance(node.ctx, ast.Load):
            self.done = True
            return self.value
        return node


def _collapse_loop_body(
    body: list[ast.stmt], class_body: bool = False,
) -> tuple[ast.stmt, ast.expr | None, set[str]] | None:
    """`(final_stmt, condition_or_None, bound_names)` for a loop body.

    Handles three shapes, in increasing order of ambition:

      for v in xs: acc.append(e)                     -> no condition
      for v in xs:
          if c: acc.append(e)                        -> an `if` clause
      for v in xs:
          t = <expr>
          if <c reading t twice>: acc.append(<e reading t>)
                                                     -> `if (t := <expr>) ...`

    A temp read exactly once is inlined (as before). A temp read more than
    once is bound with a walrus in the condition, but only when it is the
    condition's first-evaluated name — otherwise the binding would move
    across another sub-expression and that is a reordering.
    """
    if not body:
        return None
    if isinstance(body[-1], ast.If) and not body[-1].orelse and body[-1].body:
        prefix, cond, tail = body[:-1], body[-1].test, body[-1].body
        if not _pure(cond):
            return None
    else:
        prefix, cond, tail = [], None, body
    flat = _flatten_body(tail)
    if flat is None:
        return None
    final, names = flat
    bound = set(names)
    walruses = 0
    for stmt in reversed(prefix):
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and _is_name(stmt.targets[0])
        ):
            return None
        name = stmt.targets[0].id
        if name in bound:
            return None  # rebound twice: not a straight-line temp chain
        bound.add(name)
        uses = _load_count([cond, final], name)
        if uses == 1:
            sub = _Substitute(name, stmt.value)
            cond = sub.visit(cond) if cond is not None else None
            final = sub.visit(final)
        elif (
            uses > 1 and cond is not None and not class_body
            and _first_evaluated_name(cond) == name
        ):
            if walruses:
                return None  # one walrus is all the order proof covers
            walruses += 1
            target = ast.Name(id=name, ctx=ast.Store())
            cond = _SubstituteFirst(name, ast.NamedExpr(target=target, value=stmt.value)).visit(cond)
        else:
            return None
    return final, cond, bound


def _fresh_list(value: ast.expr) -> ast.expr | None:
    """The iterable behind a provably-fresh list expression, or None.

    `[..]`, `[e for ..]` and `list(x)` all produce a list nobody else aliases,
    so re-ordering it in place is unobservable.
    """
    if isinstance(value, (ast.List, ast.ListComp)):
        return value
    if (
        isinstance(value, ast.Call)
        and _is_name(value.func, "list")
        and len(value.args) == 1
        and not value.keywords
    ):
        return value.args[0]  # sorted(list(x)) == sorted(x)
    return None


# ---- rules -----------------------------------------------------------------


def _rule_bool_return(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`if c: return True` + `return False` -> `return c` / `return bool(c)`.

    Both the fall-through form and the explicit `else:` form; the inverted
    True/False pair becomes `return not c` (always a bool, so always safe).
    """
    stmt = body[i]
    if not isinstance(stmt, ast.If) or len(stmt.body) != 1:
        return None
    first = stmt.body[0]
    if not isinstance(first, ast.Return) or first.value is None:
        return None
    if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.Return):
        second, covered = stmt.orelse[0], [stmt]
    elif not stmt.orelse and i + 1 < len(body) and isinstance(body[i + 1], ast.Return):
        second, covered = body[i + 1], [stmt, body[i + 1]]
    else:
        return None
    if second.value is None:
        return None
    a, b = first.value, second.value
    if _is_const(a, True) and _is_const(b, False):
        value = stmt.test if _boolish(stmt.test) else _call("bool", [stmt.test])
    elif _is_const(a, False) and _is_const(b, True):
        value = ast.UnaryOp(op=ast.Not(), operand=stmt.test)
    else:
        return None
    start, end = _span(covered)
    return Rewrite("bool-return", start, end, [ast.Return(value=value)], stmt.col_offset,
                   _end_col(covered, end))


def _append_target(stmt: ast.stmt, name: str) -> ast.expr | None:
    """The appended expression when `stmt` is exactly `name.append(<expr>)`."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "append":
        return None
    if not _is_name(func.value, name) or len(call.args) != 1 or call.keywords:
        return None
    return call.args[0]


def _rule_append_loop(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`x = []` + `for t in it: x.append(e)` -> `x = [e for t in it]`."""
    assign = body[i]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and _is_name(assign.targets[0])
        and isinstance(assign.value, ast.List)
        and not assign.value.elts
    ):
        return None
    if i + 1 >= len(body):
        return None
    loop = body[i + 1]
    if not isinstance(loop, ast.For) or loop.orelse or not loop.body:
        return None
    name = assign.targets[0].id
    flat = _collapse_loop_body(loop.body, class_body)
    if flat is None:
        return None
    final, cond, temps = flat
    element = _append_target(final, name)
    if element is None:
        return None
    leaked = _target_names(loop.target) | temps
    # the accumulator must not appear in the element expression (that would be
    # a self-referential build) and the loop variable must be dead afterwards
    if name in _names(element) or name in _names(loop.iter):
        return None
    if cond is not None and name in _names(cond):
        return None
    if leaked & _names(loop.iter) or _leaks(scope_lines, leaked, loop.lineno, loop.end_lineno):
        return None
    generator = _comprehension(loop.target, loop.iter, [cond] if cond is not None else [])
    if not cond and _is_name(element) and _is_name(generator.target, element.id):
        value = _call("list", [loop.iter])  # `[t for t in it]` is just `list(it)`
    else:
        value = ast.ListComp(elt=element, generators=[generator])
    new = ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=value)
    start, end = _span([assign, loop])
    return Rewrite("append-loop-to-comprehension", start, end, [new], assign.col_offset,
                   _end_col([assign, loop], end))


def _added_expr(stmt: ast.stmt, name: str) -> ast.expr | None:
    """The addend when `stmt` is `name += e` or the legacy `name = name + e`."""
    if isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
        return stmt.value if _is_name(stmt.target, name) else None
    if (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and _is_name(stmt.targets[0], name)
        and isinstance(stmt.value, ast.BinOp)
        and isinstance(stmt.value.op, ast.Add)
        and _is_name(stmt.value.left, name)
    ):
        return stmt.value.right
    return None


def _rule_accumulate_sum(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`t = 0` + `for v in xs: t += f(v)` -> `t = sum(f(v) for v in xs)`."""
    assign = body[i]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and _is_name(assign.targets[0])
        and isinstance(assign.value, ast.Constant)
        and assign.value.value == 0
        and not isinstance(assign.value.value, bool)
    ):
        return None
    if i + 1 >= len(body):
        return None
    loop = body[i + 1]
    if not isinstance(loop, ast.For) or loop.orelse or not loop.body:
        return None
    name = assign.targets[0].id
    flat = _collapse_loop_body(loop.body, class_body)
    if flat is None:
        return None
    final, cond, temps = flat
    added = _added_expr(final, name)
    if added is None:
        return None
    leaked = _target_names(loop.target) | temps
    if name in _names(added) or name in _names(loop.iter):
        return None
    if cond is not None and name in _names(cond):
        return None
    if leaked & _names(loop.iter) or _leaks(scope_lines, leaked, loop.lineno, loop.end_lineno):
        return None
    gen = ast.GeneratorExp(
        elt=added,
        generators=[_comprehension(loop.target, loop.iter, [cond] if cond is not None else [])],
    )
    args = [gen]
    if type(assign.value.value) is not int:
        # `total = 0.0` must stay a float even for an empty iterable, so the
        # original initial value becomes sum()'s explicit start.
        args.append(assign.value)
    new = ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=_call("sum", args))
    start, end = _span([assign, loop])
    return Rewrite("accumulate-to-sum", start, end, [new], assign.col_offset,
                   _end_col([assign, loop], end))


def _rule_sort_to_sorted(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`x = <list expr>` + `x.sort(...)` -> `x = sorted(<list expr>, ...)`.

    Only fires when the value is *provably* a fresh list (a literal or a
    comprehension), so no aliased list is silently re-ordered. Composes with
    `append-loop-to-comprehension`, which produces exactly that shape.
    """
    assign = body[i]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and _is_name(assign.targets[0])
    ):
        return None
    inner = _fresh_list(assign.value)
    if inner is None:
        return None
    if i + 1 >= len(body):
        return None
    call_stmt = body[i + 1]
    if not isinstance(call_stmt, ast.Expr) or not isinstance(call_stmt.value, ast.Call):
        return None
    call = call_stmt.value
    name = assign.targets[0].id
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "sort":
        return None
    if not _is_name(call.func.value, name) or call.args:
        return None
    if any(kw.arg not in ("key", "reverse") for kw in call.keywords):
        return None
    new = ast.Assign(
        targets=[ast.Name(id=name, ctx=ast.Store())],
        value=_call("sorted", [inner], list(call.keywords)),
    )
    start, end = _span([assign, call_stmt])
    return Rewrite("sort-to-sorted", start, end, [new], assign.col_offset,
                   _end_col([assign, call_stmt], end))


def _rule_bare_reraise(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`try: B except X: raise` -> `B` (every handler a bare re-raise)."""
    stmt = body[i]
    if not isinstance(stmt, ast.Try) or stmt.orelse or stmt.finalbody:
        return None
    if not stmt.handlers:
        return None
    for handler in stmt.handlers:
        if handler.name is not None or len(handler.body) != 1:
            return None
        raise_stmt = handler.body[0]
        if not isinstance(raise_stmt, ast.Raise) or raise_stmt.exc is not None:
            return None
        if handler.type is None:
            return None  # bare `except:` also swallows BaseException control flow
    # a `try` body containing `continue`/`break`/`return` is still equivalent:
    # the handlers re-raise unchanged, so the try adds no observable behaviour.
    start, end = _span([stmt])
    return Rewrite("drop-bare-reraise", start, end, list(stmt.body), stmt.col_offset,
                   _end_col([stmt], end))


# ---- if/elif ladders and the manual max loop (iteration 09) -----------------
#
# FIXTURE.md names three ladder shapes as unclaimed opportunities on the py
# fixture (`priority_for_status`, `classify_stock`, `discount_tier`) and one
# manual max-over-a-dict loop (`busiest_area`). Two of the four are provably
# rewritable and are taken here; the rest are declined on purpose and the
# reasons are recorded rather than papered over.


def _return_ladder(body: list[ast.stmt], i: int):
    """`(tests, values, default, covered)` for a constant-returning ladder.

    Accepts both spellings the fixture uses: an `if/elif/.../else` chain, and
    a run of consecutive `if c: return v` statements ending in a bare
    `return d` (semantically the same thing, because every arm returns).
    """
    stmt = body[i]
    if not isinstance(stmt, ast.If):
        return None
    tests: list[ast.expr] = []
    values: list[ast.expr] = []
    covered: list[ast.stmt] = [stmt]
    node = stmt
    while True:
        if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
            return None
        if node.body[0].value is None:
            return None
        tests.append(node.test)
        values.append(node.body[0].value)
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            node = node.orelse[0]
            continue
        break
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.Return):
        default = node.orelse[0].value
        return (tests, values, default, covered) if default is not None else None
    if node.orelse:
        return None
    j = i + 1
    while j < len(body) and isinstance(body[j], ast.If) and not body[j].orelse:
        inner = body[j]
        if len(inner.body) != 1 or not isinstance(inner.body[0], ast.Return):
            break
        if inner.body[0].value is None:
            break
        tests.append(inner.test)
        values.append(inner.body[0].value)
        covered.append(inner)
        j += 1
    if j < len(body) and isinstance(body[j], ast.Return) and body[j].value is not None:
        covered.append(body[j])
        return tests, values, body[j].value, covered
    return None


def _same_subject(tests: list[ast.expr], ops: tuple) -> ast.expr | None:
    """The shared left operand when every test is `<subject> <op> <const>`."""
    subject = None
    for test in tests:
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return None
        if not isinstance(test.ops[0], ops):
            return None
        if not isinstance(test.comparators[0], ast.Constant):
            return None
        if subject is None:
            subject = test.left
        elif ast.dump(subject) != ast.dump(test.left):
            return None
    # the subject is evaluated ONCE in the dict form and N times in the scan
    # form; either way it must be side-effect free to be interchangeable
    return subject if subject is not None and _pure(subject) else None


def _all_constants(nodes: list[ast.expr]) -> bool:
    return all(isinstance(n, ast.Constant) for n in nodes)


def _rule_if_ladder_dict(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`if x == 'A': return 1` ... `return 9` -> `return {...}.get(x, 9)`.

    Only for `==` against constants of one hashable, non-bool type, all
    distinct, with constant results and a constant fallback.

    Recorded caveat, deliberately not hidden: if `x` is *unhashable* at
    runtime, `{...}.get(x, d)` raises `TypeError` where the `==` ladder
    returned `d`. Nothing in the AST can rule that out, so this rule — like
    `_python_drop_unreachable` — is correct-by-construction only up to that
    case and relies on the frozen suite, which is exactly what the layer gate
    exists for. A ladder whose subject can be an arbitrary object is the one
    shape to watch in review.
    """
    ladder = _return_ladder(body, i)
    if ladder is None:
        return None
    tests, values, default, covered = ladder
    if len(tests) < 3:
        return None  # two branches do not pay for a dict literal
    subject = _same_subject(tests, (ast.Eq,))
    if subject is None or not _all_constants(values + [default]):
        return None
    keys = [t.comparators[0].value for t in tests]
    if any(isinstance(k, bool) for k in keys) or not all(
        isinstance(k, (str, int)) for k in keys
    ):
        return None  # bools alias ints as dict keys; floats invite 1 == 1.0
    if len({type(k) for k in keys}) != 1 or len(set(keys)) != len(keys):
        return None
    table = ast.Dict(
        keys=[t.comparators[0] for t in tests],
        values=list(values),
    )
    call = _call("", [])
    call.func = ast.Attribute(value=table, attr="get", ctx=ast.Load())
    call.args = [subject, default]
    start, end = _span(covered)
    return Rewrite(
        "if-ladder-to-dict", start, end, [ast.Return(value=call)],
        covered[0].col_offset, _end_col(covered, end),
    )


_ORDER_OPS = {ast.Gt: ast.Gt, ast.GtE: ast.GtE, ast.Lt: ast.Lt, ast.LtE: ast.LtE}


def _rule_threshold_ladder(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """`if q >= 100: return .15` ... `return 0` -> an ordered tuple scan.

    `next((v for t, v in ((100, .15), ...) if q >= t), 0.0)` evaluates the
    same comparisons, in the same order, lazily — so unlike the dict rewrite
    this one is exactly equivalent, including for a subject that is not
    hashable or not orderable (the same `TypeError` is raised at the same
    comparison). A `next` scan is the honest shape for a threshold ladder;
    forcing it into a dict would not be.
    """
    ladder = _return_ladder(body, i)
    if ladder is None:
        return None
    tests, values, default, covered = ladder
    if len(tests) < 3:
        return None
    op_type = type(tests[0].ops[0]) if isinstance(tests[0], ast.Compare) and tests[0].ops else None
    if op_type not in _ORDER_OPS:
        return None
    subject = _same_subject(tests, (op_type,))
    if subject is None or not _all_constants(values + [default]):
        return None
    if not all(isinstance(t.comparators[0].value, (int, float)) for t in tests):
        return None
    if any(isinstance(t.comparators[0].value, bool) for t in tests):
        return None
    taken = _names(ast.Module(body=list(covered), type_ignores=[]))
    key, val = _fresh("_t", taken), _fresh("_v", taken)
    pairs = ast.Tuple(
        elts=[
            ast.Tuple(elts=[t.comparators[0], v], ctx=ast.Load())
            for t, v in zip(tests, values)
        ],
        ctx=ast.Load(),
    )
    guard = ast.Compare(
        left=subject, ops=[op_type()], comparators=[ast.Name(id=key, ctx=ast.Load())]
    )
    gen = ast.GeneratorExp(
        elt=ast.Name(id=val, ctx=ast.Load()),
        generators=[
            ast.comprehension(
                target=ast.Tuple(
                    elts=[ast.Name(id=key, ctx=ast.Store()), ast.Name(id=val, ctx=ast.Store())],
                    ctx=ast.Store(),
                ),
                iter=pairs, ifs=[guard], is_async=0,
            )
        ],
    )
    start, end = _span(covered)
    return Rewrite(
        "threshold-ladder-to-scan", start, end,
        [ast.Return(value=_call("next", [gen, default]))],
        covered[0].col_offset, _end_col(covered, end),
    )


def _fresh(base: str, taken: set[str]) -> str:
    name = base
    n = 2
    while name in taken:
        name = f"{base}{n}"
        n += 1
    return name


def _rule_max_loop(body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False) -> Rewrite | None:
    """A `None`-sentinel manual max loop -> `max(it, key=..., default=None)`.

    Exactly this shape, and no other::

        best = None
        best_v = None
        for k in it:
            v = <pure expr>
            if best_v is None or v > best_v:
                best = k
                best_v = v

    The `None` sentinel is what makes the rewrite provable: `max(..., key=...)`
    also returns the FIRST maximal element, and `default=None` reproduces the
    empty-iterable case exactly.

    `busiest_area` in the py fixture is deliberately NOT taken: its sentinel is
    `best_units = -1`, and `max(counts, key=counts.get, default=None)` differs
    from it whenever every value is <= -1. That is not provable from the AST,
    so it is left for the LLM layer rather than forced.
    """
    if i + 2 >= len(body):
        return None
    first, second, loop = body[i], body[i + 1], body[i + 2]
    if not (isinstance(first, ast.Assign) and len(first.targets) == 1 and _is_name(first.targets[0])):
        return None
    if not (isinstance(second, ast.Assign) and len(second.targets) == 1 and _is_name(second.targets[0])):
        return None
    if not (_is_const(first.value, None) and _is_const(second.value, None)):
        return None
    best, best_v = first.targets[0].id, second.targets[0].id
    if best == best_v or not isinstance(loop, ast.For) or loop.orelse:
        return None
    if not _is_name(loop.target):
        return None
    item = loop.target.id
    if len(loop.body) != 2:
        return None
    value_stmt, guard = loop.body
    if not (isinstance(value_stmt, ast.Assign) and len(value_stmt.targets) == 1 and _is_name(value_stmt.targets[0])):
        return None
    temp = value_stmt.targets[0].id
    # the iterable is evaluated once, at the same point, in both forms, so it
    # need not be pure; the per-item value expression becomes the `key` lambda
    # and is called once per element in the same order — kept pure anyway as
    # the conservative line the rest of this library holds.
    if not _pure(value_stmt.value):
        return None
    if not isinstance(guard, ast.If) or guard.orelse or len(guard.body) != 2:
        return None
    test = guard.test
    if not (isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) and len(test.values) == 2):
        return None
    sentinel, compare = test.values
    if not (
        isinstance(sentinel, ast.Compare) and _is_name(sentinel.left, best_v)
        and len(sentinel.ops) == 1 and isinstance(sentinel.ops[0], ast.Is)
        and _is_const(sentinel.comparators[0], None)
    ):
        return None
    if not (
        isinstance(compare, ast.Compare) and _is_name(compare.left, temp)
        and len(compare.ops) == 1 and isinstance(compare.ops[0], ast.Gt)
        and _is_name(compare.comparators[0], best_v)
    ):
        return None
    assigns = {}
    for stmt in guard.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and _is_name(stmt.targets[0])):
            return None
        assigns[stmt.targets[0].id] = stmt.value
    if set(assigns) != {best, best_v}:
        return None
    if not _is_name(assigns[best], item) or not _is_name(assigns[best_v], temp):
        return None
    # `best_v` must be dead after the loop: the rewrite stops maintaining it
    covered = [first, second, loop]
    start, end = _span(covered)
    if _leaks(scope_lines, {best_v, temp}, start, end):
        return None
    key = ast.Lambda(
        args=ast.arguments(
            posonlyargs=[], args=[ast.arg(arg=item)], kwonlyargs=[],
            kw_defaults=[], defaults=[],
        ),
        body=value_stmt.value,
    )
    call = _call(
        "max", [loop.iter],
        [ast.keyword(arg="key", value=key), ast.keyword(arg="default", value=ast.Constant(value=None))],
    )
    return Rewrite(
        "max-loop-to-max", start, end,
        [ast.Assign(targets=[ast.Name(id=best, ctx=ast.Store())], value=call)],
        first.col_offset, _end_col(covered, end),
    )


_RULE_FNS = {
    "bool-return": _rule_bool_return,
    "append-loop-to-comprehension": _rule_append_loop,
    "accumulate-to-sum": _rule_accumulate_sum,
    "sort-to-sorted": _rule_sort_to_sorted,
    "drop-bare-reraise": _rule_bare_reraise,
    "if-ladder-to-dict": _rule_if_ladder_dict,
    "threshold-ladder-to-scan": _rule_threshold_ladder,
    "max-loop-to-max": _rule_max_loop,
}

_BLOCK_FIELDS = ("body", "orelse", "finalbody")


SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _collect(
    node: ast.AST, only: set[str], out: list[Rewrite], scope_lines: dict,
    class_body: bool = False,
) -> None:
    """Depth-first scan for non-overlapping rewrites over every statement list.

    `class_body` tracks whether the statements being scanned sit directly in
    a `class` block: a walrus inside a comprehension is a SyntaxError there,
    and `ast.parse` does not catch it (it is raised at compile time), so the
    rule has to decline up front.
    """
    if isinstance(node, SCOPES):
        scope_lines = scope_name_lines(node)
        class_body = False
    if isinstance(node, ast.ClassDef):
        class_body = True
    for field in _BLOCK_FIELDS:
        block = getattr(node, field, None)
        if not isinstance(block, list) or not all(isinstance(s, ast.stmt) for s in block):
            continue
        i = 0
        while i < len(block):
            hit = None
            for name in RULES:
                if name not in only:
                    continue
                hit = _RULE_FNS[name](block, i, scope_lines, class_body)
                if hit is not None:
                    break
            if hit is not None:
                out.append(hit)
                # skip past the consumed statements; nested rewrites inside a
                # replaced region would overlap it
                while i < len(block) and block[i].lineno <= hit.end:
                    i += 1
                continue
            _collect(block[i], only, out, scope_lines, class_body)
            i += 1
    for handler in getattr(node, "handlers", []) or []:
        _collect(handler, only, out, scope_lines, class_body)


def _render(rw: Rewrite) -> list[str]:
    pad = " " * rw.col
    module = ast.Module(body=rw.stmts, type_ignores=[])
    ast.fix_missing_locations(module)
    text = ast.unparse(module)
    return [(pad + line) if line.strip() else "" for line in text.splitlines()]


def _line_aligned(lines: list[str], rw: Rewrite) -> bool:
    """The rewrite must own whole lines: nothing shares them but whitespace
    and a trailing comment. (`a = 1; b = 2` on one line would be clobbered.)"""
    if rw.start - 1 >= len(lines) or rw.end - 1 >= len(lines):
        return False
    if lines[rw.start - 1][: rw.col].strip():
        return False
    trailer = lines[rw.end - 1][rw.end_col :].strip()
    return trailer == "" or trailer.startswith("#")


def _one_pass(source: str, only: set[str]) -> tuple[str, list[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, []
    rewrites: list[Rewrite] = []
    _collect(tree, only, rewrites, scope_name_lines(tree), False)
    if not rewrites:
        return source, []
    lines = source.splitlines()
    applied = []
    for rw in sorted(rewrites, key=lambda r: r.start, reverse=True):
        if not _line_aligned(lines, rw):
            continue
        lines[rw.start - 1 : rw.end] = _render(rw)
        applied.append(rw.rule)
    if not applied:
        return source, []
    new_source = "\n".join(lines)
    if source.endswith("\n"):
        new_source += "\n"
    try:
        # compile(), not ast.parse(): some errors (a walrus in a class-body
        # comprehension) are raised only at the compile stage.
        compile(new_source, "<rules>", "exec")
    except (SyntaxError, ValueError):  # defensive: never emit broken code
        return source, []
    return new_source, applied


def apply_rules(source: str, only: set[str] | None = None) -> tuple[str, list[str]]:
    """Apply the rule library to `source` until fixpoint.

    Returns `(new_source, rule_names_applied)`. Never returns a source that
    fails to parse; on any doubt it returns the input unchanged.
    """
    selected = set(RULES) if only is None else (set(only) & set(RULES))
    if not selected:
        return source, []
    applied: list[str] = []
    current = source
    for _ in range(MAX_PASSES):
        current, names = _one_pass(current, selected)
        if not names:
            break
        applied += names
    return current, applied
