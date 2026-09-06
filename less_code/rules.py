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
import builtins
import io
import re
import tokenize
from dataclasses import dataclass

RULES = (
    "bool-return",
    "merge-same-branch",
    "flatten-nested-if",
    "guard-call",
    "conditional-return",
    "conditional-assignment",
    "self-default-assignment",
    "inline-return-temp",
    "inline-single-use-temp",
    "merge-imports",
    "merge-from-imports",
    "hoist-common-tail",
    "dict-build-to-literal",
    "boolean-loop-to-any-all",
    "append-loop-to-comprehension",
    "accumulate-to-sum",
    "sort-to-sorted",
    "drop-bare-reraise",
    "threshold-ladder-to-scan",
    "max-loop-to-max",
    "merge-del",
    "pack-assignments",
    "else-after-terminator",
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
    text: list[str] | None = None  # pre-rendered lines (unindented) if set


# ---- helpers ---------------------------------------------------------------


def _is_name(node: ast.AST, name: str | None = None) -> bool:
    return isinstance(node, ast.Name) and (name is None or node.id == name)


def _is_const(node: ast.AST, value) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _boolish(node: ast.expr) -> bool:
    """True when the expression provably already evaluates to a bool."""
    if isinstance(node, ast.Compare):
        return all(
            isinstance(op, (ast.Is, ast.IsNot, ast.In, ast.NotIn)) for op in node.ops
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    if isinstance(node, ast.BoolOp):
        return all(_boolish(v) for v in node.values)
    # Calls with builtin-looking names can be shadowed in the target.
    return isinstance(node, ast.Constant) and isinstance(node.value, bool)


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _target_names(target: ast.expr) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


_BUILTIN_NAMES = frozenset(dir(builtins))


def _binding_names(stmt: ast.stmt) -> set[str]:
    """Names a *direct* statement binds when it completes normally.

    Only unconditional bindings count: an assignment, an import, a def or a
    class, a `with ... as name`. Loop targets and branch-local writes may
    leave the name unbound, so they are not included.
    """
    names: set[str] = set()
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            names |= _target_names(target)
    elif (isinstance(stmt, ast.AnnAssign) and stmt.value is not None) or isinstance(
        stmt, ast.AugAssign
    ):
        names |= _target_names(stmt.target)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        names |= {
            (alias.asname or alias.name.split(".")[0])
            for alias in stmt.names
            if alias.name != "*"
        }
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                names |= _target_names(item.optional_vars)
    return names


def scope_name_lines(
    scope: ast.AST, bound: set[str] | None = None
) -> dict[str, list[int]]:
    """Every `Name` occurrence in a scope, as name -> line numbers.

    Bookkeeping keys (never valid identifiers) carry facts the rules need:

      !module           the scope is the module body
      !declared:<n>     `global`/`nonlocal` declaration of <n>
      !bound:<n>        <n> is bound before the scope's body runs: a module
                        top-level binding, a parameter, or an inherited one
      !stored:<n>       <n> is (re)bound somewhere inside this scope
      !deleted:<n>      <n> is deleted somewhere inside this scope
      !closure:<n>      <n> is read from a nested function or lambda
      !guarded          lines inside a `try` or `with` (exceptions there can
                        be caught, so exception *order* is observable)
    """
    out: dict[str, list[int]] = {}
    if isinstance(scope, ast.Module):
        out["!module"] = []
        for stmt in scope.body:
            for name in _binding_names(stmt):
                out.setdefault(f"!bound:{name}", [])
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for name in _binding_names(stmt):
                    out.setdefault(f"!import:{name}", [])
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        params += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
        for arg in params:
            out.setdefault(f"!bound:{arg.arg}", [])
    for name in bound or ():
        if name.startswith("!import:"):
            out.setdefault(name, [])
        else:
            out.setdefault(f"!bound:{name}", [])
    guarded: set[int] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Name):
            out.setdefault(node.id, []).append(node.lineno)
            if isinstance(node.ctx, ast.Store):
                out.setdefault(f"!stored:{node.id}", []).append(node.lineno)
            elif isinstance(node.ctx, ast.Del):
                out.setdefault(f"!deleted:{node.id}", []).append(node.lineno)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                out.setdefault(f"!declared:{name}", []).append(node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                out.setdefault(f"!stored:{name}", []).append(node.lineno)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.setdefault(f"!stored:{node.name}", []).append(node.lineno)
        elif isinstance(node, (ast.Try, ast.TryStar, ast.With, ast.AsyncWith)):
            guarded.update(range(node.lineno, node.end_lineno + 1))
        if node is scope:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(f"!stored:{node.name}", []).append(node.lineno)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                    out.setdefault(f"!closure:{inner.id}", []).append(inner.lineno)
    out["!guarded"] = sorted(guarded)
    return out


def _bound_at(body: list[ast.stmt], i: int, scope_lines: dict) -> set[str]:
    """Names that are certainly bound when `body[i]` starts executing."""
    earlier: set[str] = set()
    for stmt in body[:i]:
        earlier |= _binding_names(stmt)
    return earlier


def _guarded(scope_lines: dict, *nodes: ast.stmt) -> bool:
    """Do the statements sit inside a `try` or `with` of this scope?

    A rewrite that folds `x = <init>` + <loop writing x> into one statement
    leaves `x` unbound instead of partially built when the loop raises. That
    is observable only where the exception can be caught before the scope
    ends, i.e. inside a `try` or a `with` (context managers may suppress).
    """
    guarded = scope_lines.get("!guarded", ())
    if not guarded:
        return False
    lines = set(guarded)
    return any(
        line in lines
        for node in nodes
        for line in range(node.lineno, node.end_lineno + 1)
    )


def _provably_bound(name: str, earlier: set[str], scope_lines: dict) -> bool:
    """A plain load of `name` cannot raise: it is bound by an earlier
    statement of this block, or it is a parameter / module-level name /
    builtin that nothing in this scope rebinds or deletes."""
    if f"!deleted:{name}" in scope_lines or f"!declared:{name}" in scope_lines:
        return False
    if name in earlier:
        return True
    if f"!stored:{name}" in scope_lines:
        return False
    return f"!bound:{name}" in scope_lines or name in _BUILTIN_NAMES


def _leaks(
    scope_lines: dict[str, list[int]], names: set[str], start: int, end: int
) -> bool:
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


def _call(
    func: str, args: list[ast.expr], keywords: list[ast.keyword] | None = None
) -> ast.Call:
    return ast.Call(
        func=ast.Name(id=func, ctx=ast.Load()), args=args, keywords=keywords or []
    )


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
    return max((n.end_col_offset or 0) for n in nodes if n.end_lineno == end)


class _Substitute(ast.NodeTransformer):
    def __init__(self, name: str, value: ast.expr) -> None:
        self.name, self.value = name, value

    def visit_Name(self, node: ast.Name):
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
        isinstance(
            n,
            (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda),
        )
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

    def visit_Name(self, node: ast.Name):
        if not self.done and node.id == self.name and isinstance(node.ctx, ast.Load):
            self.done = True
            return self.value
        return node


def _collapse_loop_body(
    body: list[ast.stmt],
    class_body: bool = False,
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
            uses > 1
            and cond is not None
            and not class_body
            and _first_evaluated_name(cond) == name
        ):
            if walruses:
                return None  # one walrus is all the order proof covers
            walruses += 1
            target = ast.Name(id=name, ctx=ast.Store())
            cond = _SubstituteFirst(
                name, ast.NamedExpr(target=target, value=stmt.value)
            ).visit(cond)
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


def _rule_bool_return(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
        value = (
            stmt.test
            if _boolish(stmt.test)
            else ast.IfExp(
                test=stmt.test,
                body=ast.Constant(value=True),
                orelse=ast.Constant(value=False),
            )
        )
    elif _is_const(a, False) and _is_const(b, True):
        value = ast.UnaryOp(op=ast.Not(), operand=stmt.test)
    else:
        return None
    start, end = _span(covered)
    return Rewrite(
        "bool-return",
        start,
        end,
        [ast.Return(value=value)],
        stmt.col_offset,
        _end_col(covered, end),
    )


def _rule_merge_same_branch(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """Combine conditions whose branches contain exactly the same statements.

    ``if a: return x; if b: return x`` and ``if a: x; elif b: x`` both become
    one ``if a or b``.  ``or`` retains left-to-right short-circuit evaluation.
    Separate adjacent ``if`` statements are only equivalent when the shared
    body terminates; otherwise both bodies could run when both tests are true.
    """
    stmt = body[i]
    if not isinstance(stmt, ast.If) or not stmt.body:
        return None

    other: ast.If | None = None
    covered: list[ast.stmt] = [stmt]
    adjacent = False
    if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If):
        other = stmt.orelse[0]
    elif (
        not stmt.orelse
        and i + 1 < len(body)
        and isinstance(body[i + 1], ast.If)
        and not body[i + 1].orelse
        and isinstance(stmt.body[-1], (ast.Return, ast.Raise, ast.Break, ast.Continue))
    ):
        other = body[i + 1]
        covered.append(other)
        adjacent = True
    if other is None or len(stmt.body) != len(other.body):
        return None
    if any(
        ast.dump(left, include_attributes=False)
        != ast.dump(right, include_attributes=False)
        for left, right in zip(stmt.body, other.body)
    ):
        return None

    combined = ast.If(
        test=ast.BoolOp(op=ast.Or(), values=[stmt.test, other.test]),
        body=stmt.body,
        orelse=[] if adjacent else other.orelse,
    )
    start, end = _span(covered)
    return Rewrite(
        "merge-same-branch",
        start,
        end,
        [combined],
        stmt.col_offset,
        _end_col(covered, end),
    )


def _rule_flatten_nested_if(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """``if a: if b: body`` -> ``if a and b: body``.

    Both forms evaluate ``a`` first and evaluate ``b`` only when ``a`` is
    truthy. Else branches are deliberately excluded because their ownership
    would change when the nesting is flattened.
    """
    outer = body[i]
    if not (
        isinstance(outer, ast.If)
        and not outer.orelse
        and len(outer.body) == 1
        and isinstance(outer.body[0], ast.If)
        and not outer.body[0].orelse
    ):
        return None
    inner = outer.body[0]
    combined = ast.If(
        test=ast.BoolOp(op=ast.And(), values=[outer.test, inner.test]),
        body=inner.body,
        orelse=[],
    )
    start, end = _span([outer])
    return Rewrite(
        "flatten-nested-if",
        start,
        end,
        [combined],
        outer.col_offset,
        _end_col([outer], end),
    )


def _rule_guard_call(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """``if condition: call()`` -> ``condition and call()``.

    A boolean ``and`` statement has precisely the control flow of a one-arm
    call guard: it truth-tests the condition once and evaluates the call only
    when that test succeeds. The expression's value is discarded in both
    forms. Limiting the body to a call keeps this as a familiar guard idiom
    rather than turning arbitrary side effects into boolean expressions.
    """
    stmt = body[i]
    if not (
        isinstance(stmt, ast.If)
        and not stmt.orelse
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Expr)
        and isinstance(stmt.body[0].value, ast.Call)
    ):
        return None
    expression = ast.Expr(
        value=ast.BoolOp(op=ast.And(), values=[stmt.test, stmt.body[0].value])
    )
    start, end = _span([stmt])
    return Rewrite(
        "guard-call",
        start,
        end,
        [expression],
        stmt.col_offset,
        _end_col([stmt], end),
    )


def _rule_conditional_return(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`if c: return a; return b` -> `return a if c else b`.

    The explicit `else: return b` spelling is accepted too. Conditional
    expressions retain the branch's lazy evaluation and evaluate the test
    once, exactly like the statement form.
    """
    stmt = body[i]
    if not (
        isinstance(stmt, ast.If)
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Return)
        and stmt.body[0].value is not None
        and "!module" not in scope_lines
    ):
        return None
    if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.Return):
        second, covered = stmt.orelse[0], [stmt]
    elif not stmt.orelse and i + 1 < len(body) and isinstance(body[i + 1], ast.Return):
        second, covered = body[i + 1], [stmt, body[i + 1]]
    else:
        return None
    # a bare `return` is `return None`
    fallback = second.value or ast.Constant(value=None)
    value = ast.IfExp(test=stmt.test, body=stmt.body[0].value, orelse=fallback)
    start, end = _span(covered)
    return Rewrite(
        "conditional-return",
        start,
        end,
        [ast.Return(value=value)],
        stmt.col_offset,
        _end_col(covered, end),
    )


def _rule_inline_return_temp(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`result = expression; return result` -> `return expression`."""
    assign = body[i]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and _is_name(assign.targets[0])
        and i + 1 < len(body)
        and isinstance(body[i + 1], ast.Return)
        and _is_name(body[i + 1].value, assign.targets[0].id)
        and f"!declared:{assign.targets[0].id}" not in scope_lines
    ):
        return None
    covered = [assign, body[i + 1]]
    start, end = _span(covered)
    return Rewrite(
        "inline-return-temp",
        start,
        end,
        [ast.Return(value=assign.value)],
        assign.col_offset,
        _end_col(covered, end),
    )


def _rule_conditional_assignment(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`if c: x = a; else: x = b` -> `x = a if c else b`."""
    stmt = body[i]
    if not (
        isinstance(stmt, ast.If)
        and len(stmt.body) == 1
        and len(stmt.orelse) == 1
        and isinstance(stmt.body[0], ast.Assign)
        and isinstance(stmt.orelse[0], ast.Assign)
    ):
        return None
    first, second = stmt.body[0], stmt.orelse[0]
    if not (
        len(first.targets) == len(second.targets)
        and all(
            ast.dump(a) == ast.dump(b) for a, b in zip(first.targets, second.targets)
        )
        and first.type_comment is None
        and second.type_comment is None
        and not isinstance(first.value, ast.Lambda)
        and not isinstance(second.value, ast.Lambda)
    ):
        return None
    value = ast.IfExp(test=stmt.test, body=first.value, orelse=second.value)
    start, end = _span([stmt])
    return Rewrite(
        "conditional-assignment",
        start,
        end,
        [ast.Assign(targets=first.targets, value=value)],
        stmt.col_offset,
        _end_col([stmt], end),
    )


def _rule_self_default_assignment(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """``if test(x): x = value`` -> ``x = value if test(x) else x``.

    This is restricted to ordinary function locals whose first evaluated
    name in the test is the target. That proves the old value exists before
    the branch and preserves test/RHS order. Module, class, global, and
    nonlocal assignments are excluded because an otherwise redundant write
    can be observable through a custom namespace or another thread.
    """
    stmt = body[i]
    if not (
        not class_body
        and "!module" not in scope_lines
        and isinstance(stmt, ast.If)
        and not stmt.orelse
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Assign)
        and len(stmt.body[0].targets) == 1
        and _is_name(stmt.body[0].targets[0])
    ):
        return None
    assignment = stmt.body[0]
    name = assignment.targets[0].id
    if _first_evaluated_name(stmt.test) != name or f"!declared:{name}" in scope_lines:
        return None
    value = ast.IfExp(
        test=stmt.test,
        body=assignment.value,
        orelse=ast.Name(id=name, ctx=ast.Load()),
    )
    start, end = _span([stmt])
    return Rewrite(
        "self-default-assignment",
        start,
        end,
        [ast.Assign(targets=assignment.targets, value=value)],
        stmt.col_offset,
        _end_col([stmt], end),
    )


def _rule_dict_build_literal(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """Fold consecutive writes to a fresh empty dict into its literal."""
    assign = body[i]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and _is_name(assign.targets[0])
        and isinstance(assign.value, ast.Dict)
        and not assign.value.keys
    ):
        return None
    name = assign.targets[0].id
    if _guarded(scope_lines, assign):
        return None
    keys: list[ast.expr] = []
    values: list[ast.expr] = []
    end = i + 1
    while end < len(body):
        item = body[end]
        if not (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Subscript)
            and _is_name(item.targets[0].value, name)
            and name not in _names(item.targets[0].slice)
            and name not in _names(item.value)
        ):
            break
        keys.append(item.targets[0].slice)
        values.append(item.value)
        end += 1
    if len(keys) < 2:
        return None
    covered = body[i:end]
    start_line, end_line = _span(covered)
    return Rewrite(
        "dict-build-to-literal",
        start_line,
        end_line,
        [
            ast.Assign(
                targets=assign.targets,
                value=ast.Dict(keys=keys, values=values),
            )
        ],
        assign.col_offset,
        _end_col(covered, end_line),
    )


def _rule_boolean_loop(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """Collapse a short-circuiting boolean search loop to `any` or `all`."""
    loop = body[i]
    if not (
        isinstance(loop, ast.For)
        and not loop.orelse
        and len(loop.body) == 1
        and isinstance(loop.body[0], ast.If)
        and not loop.body[0].orelse
        and len(loop.body[0].body) == 1
        and isinstance(loop.body[0].body[0], ast.Return)
        and i + 1 < len(body)
        and isinstance(body[i + 1], ast.Return)
    ):
        return None
    branch = loop.body[0].body[0].value
    fallback = body[i + 1].value
    if not (
        isinstance(branch, ast.Constant)
        and isinstance(branch.value, bool)
        and isinstance(fallback, ast.Constant)
        and isinstance(fallback.value, bool)
        and branch.value is not fallback.value
    ):
        return None
    test = loop.body[0].test
    if branch.value:
        func, element = "any", test
    else:
        func, element = "all", ast.UnaryOp(op=ast.Not(), operand=test)
    generator = ast.GeneratorExp(
        elt=element,
        generators=[_comprehension(loop.target, loop.iter)],
    )
    covered = [loop, body[i + 1]]
    start, end = _span(covered)
    return Rewrite(
        "boolean-loop-to-any-all",
        start,
        end,
        [ast.Return(value=_call(func, [generator]))],
        loop.col_offset,
        _end_col(covered, end),
    )


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


def _rule_append_loop(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
    if _guarded(scope_lines, assign, loop):
        return None
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
    if leaked & _names(loop.iter) or _leaks(
        scope_lines, leaked, loop.lineno, loop.end_lineno
    ):
        return None
    generator = _comprehension(
        loop.target, loop.iter, [cond] if cond is not None else []
    )
    if not cond and _is_name(element) and _is_name(generator.target, element.id):
        value = _call("list", [loop.iter])  # `[t for t in it]` is just `list(it)`
    else:
        value = ast.ListComp(elt=element, generators=[generator])
    new = ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=value)
    start, end = _span([assign, loop])
    return Rewrite(
        "append-loop-to-comprehension",
        start,
        end,
        [new],
        assign.col_offset,
        _end_col([assign, loop], end),
    )


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


def _rule_accumulate_sum(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
    if _guarded(scope_lines, assign, loop):
        return None
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
    if leaked & _names(loop.iter) or _leaks(
        scope_lines, leaked, loop.lineno, loop.end_lineno
    ):
        return None
    gen = ast.GeneratorExp(
        elt=added,
        generators=[
            _comprehension(loop.target, loop.iter, [cond] if cond is not None else [])
        ],
    )
    args = [gen]
    if type(assign.value.value) is not int:
        # `total = 0.0` must stay a float even for an empty iterable, so the
        # original initial value becomes sum()'s explicit start.
        args.append(assign.value)
    new = ast.Assign(
        targets=[ast.Name(id=name, ctx=ast.Store())], value=_call("sum", args)
    )
    start, end = _span([assign, loop])
    return Rewrite(
        "accumulate-to-sum",
        start,
        end,
        [new],
        assign.col_offset,
        _end_col([assign, loop], end),
    )


def _rule_sort_to_sorted(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
    if _guarded(scope_lines, assign, call_stmt):
        return None
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
    return Rewrite(
        "sort-to-sorted",
        start,
        end,
        [new],
        assign.col_offset,
        _end_col([assign, call_stmt], end),
    )


def _rule_bare_reraise(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
    return Rewrite(
        "drop-bare-reraise",
        start,
        end,
        list(stmt.body),
        stmt.col_offset,
        _end_col([stmt], end),
    )


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


_ORDER_OPS = {ast.Gt: ast.Gt, ast.GtE: ast.GtE, ast.Lt: ast.Lt, ast.LtE: ast.LtE}


def _rule_threshold_ladder(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
    op_type = (
        type(tests[0].ops[0])
        if isinstance(tests[0], ast.Compare) and tests[0].ops
        else None
    )
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
                    elts=[
                        ast.Name(id=key, ctx=ast.Store()),
                        ast.Name(id=val, ctx=ast.Store()),
                    ],
                    ctx=ast.Store(),
                ),
                iter=pairs,
                ifs=[guard],
                is_async=0,
            )
        ],
    )
    start, end = _span(covered)
    return Rewrite(
        "threshold-ladder-to-scan",
        start,
        end,
        [ast.Return(value=_call("next", [gen, default]))],
        covered[0].col_offset,
        _end_col(covered, end),
    )


def _fresh(base: str, taken: set[str]) -> str:
    name = base
    n = 2
    while name in taken:
        name = f"{base}{n}"
        n += 1
    return name


def _rule_max_loop(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
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
    if not (
        isinstance(first, ast.Assign)
        and len(first.targets) == 1
        and _is_name(first.targets[0])
    ) or _guarded(scope_lines, first, loop):
        return None
    if not (
        isinstance(second, ast.Assign)
        and len(second.targets) == 1
        and _is_name(second.targets[0])
    ):
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
    if not (
        isinstance(value_stmt, ast.Assign)
        and len(value_stmt.targets) == 1
        and _is_name(value_stmt.targets[0])
    ):
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
    if not (
        isinstance(test, ast.BoolOp)
        and isinstance(test.op, ast.Or)
        and len(test.values) == 2
    ):
        return None
    sentinel, compare = test.values
    if not (
        isinstance(sentinel, ast.Compare)
        and _is_name(sentinel.left, best_v)
        and len(sentinel.ops) == 1
        and isinstance(sentinel.ops[0], ast.Is)
        and _is_const(sentinel.comparators[0], None)
    ):
        return None
    if not (
        isinstance(compare, ast.Compare)
        and _is_name(compare.left, temp)
        and len(compare.ops) == 1
        and isinstance(compare.ops[0], ast.Gt)
        and _is_name(compare.comparators[0], best_v)
    ):
        return None
    assigns = {}
    for stmt in guard.body:
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and _is_name(stmt.targets[0])
        ):
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
            posonlyargs=[],
            args=[ast.arg(arg=item)],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=value_stmt.value,
    )
    call = _call(
        "max",
        [loop.iter],
        [
            ast.keyword(arg="key", value=key),
            ast.keyword(arg="default", value=ast.Constant(value=None)),
        ],
    )
    return Rewrite(
        "max-loop-to-max",
        start,
        end,
        [ast.Assign(targets=[ast.Name(id=best, ctx=ast.Store())], value=call)],
        first.col_offset,
        _end_col(covered, end),
    )


# ---- inline a single-use temporary into the statement that reads it ---------


_BARRIER = object()


def _static_attribute(node: ast.AST, bases: frozenset[str]) -> bool:
    """`name.attr(.attr...)` on a provably bound name, or `"literal".method`.

    Attribute *reads* are treated as effect-free, the same stance `_pure`
    takes for comprehension guards: a property or `__getattr__` whose side
    effects interact with the inlined expression is not a realistic concern,
    and the frozen suite still gates the result. The base name must be
    provably bound so the lookup itself cannot fail before the inlined
    expression runs.
    """
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Constant):
        return True
    return isinstance(node, ast.Name) and node.id in bases


def _eval_order(node: ast.AST, imports: frozenset[str] = frozenset()):
    """Yield leaves in Python evaluation order; `_BARRIER` marks a point after
    which evaluation is conditional, deferred, or interleaved with side effects
    of an operation (attribute/subscript/call/operator), so an inlined
    expression placed later could observe or miss those effects."""
    if isinstance(node, (ast.Name, ast.Constant)):
        yield node
    elif isinstance(node, ast.Attribute) and _static_attribute(node, imports):
        yield from _eval_order(node.value, imports)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for elt in node.elts:
            yield from _eval_order(elt, imports)
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is not None:
                yield from _eval_order(key, imports)
            yield from _eval_order(value, imports)
    elif isinstance(node, (ast.Starred, ast.keyword)):
        yield from _eval_order(node.value, imports)
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            yield from _eval_order(value, imports)
    elif isinstance(node, ast.FormattedValue):
        yield from _eval_order(node.value, imports)
        yield _BARRIER  # __format__ runs
    elif isinstance(node, ast.UnaryOp):
        yield from _eval_order(node.operand, imports)
        yield _BARRIER
    elif isinstance(node, ast.BinOp):
        yield from _eval_order(node.left, imports)
        yield from _eval_order(node.right, imports)
        yield _BARRIER
    elif isinstance(node, ast.Compare):
        yield from _eval_order(node.left, imports)
        yield from _eval_order(node.comparators[0], imports)
        yield _BARRIER
    elif isinstance(node, ast.BoolOp):
        yield from _eval_order(node.values[0], imports)
        yield _BARRIER
    elif isinstance(node, ast.IfExp):
        yield from _eval_order(node.test, imports)
        yield _BARRIER
    elif isinstance(node, ast.Call):
        yield from _eval_order(node.func, imports)
        for arg in node.args:
            yield from _eval_order(arg, imports)
        for kw in node.keywords:
            yield from _eval_order(kw, imports)
        yield _BARRIER
    elif isinstance(node, ast.Attribute):
        yield from _eval_order(node.value, imports)
        yield _BARRIER
    elif isinstance(node, ast.Subscript):
        yield from _eval_order(node.value, imports)
        yield from _eval_order(node.slice, imports)
        yield _BARRIER
    elif isinstance(node, ast.Slice):
        for part in (node.lower, node.upper, node.step):
            if part is not None:
                yield from _eval_order(part, imports)
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        yield from _eval_order(node.generators[0].iter, imports)
        yield _BARRIER
    elif isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)):
        if node.value is not None:
            yield from _eval_order(node.value, imports)
        yield _BARRIER
    else:
        yield _BARRIER  # lambda, walrus, anything unknown


def _statement_eval_order(stmt: ast.stmt, imports: frozenset[str] = frozenset()):
    """Evaluation order of a statement's expressions (header only for
    compound statements: the body runs after the header, so it is a barrier)."""
    if isinstance(stmt, (ast.Expr, ast.Return)):
        if stmt.value is not None:
            yield from _eval_order(stmt.value, imports)
    elif isinstance(stmt, ast.Assign):
        yield from _eval_order(stmt.value, imports)
        for target in stmt.targets:
            yield from _eval_order(target, imports)
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        yield from _eval_order(stmt.value, imports)
        yield from _eval_order(stmt.target, imports)
    elif isinstance(stmt, ast.AugAssign):
        yield from _eval_order(stmt.target, imports)
        yield _BARRIER
    elif isinstance(stmt, ast.Raise):
        if stmt.exc is not None:
            yield from _eval_order(stmt.exc, imports)
        yield _BARRIER
    elif isinstance(stmt, ast.If):
        yield from _eval_order(stmt.test, imports)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        yield from _eval_order(stmt.iter, imports)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        yield from _eval_order(stmt.items[0].context_expr, imports)
    yield _BARRIER


_HEADER_STMTS = (ast.If, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)
_SIMPLE_USE_STMTS = (
    ast.Expr,
    ast.Return,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Raise,
)


def _rule_inline_single_use_temp(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`t = <expr>` + `<stmt reading t once>` -> `<stmt reading <expr>>`.

    The temp must be a function local mentioned exactly twice in its whole
    scope (the binding and this one read), so nothing else can observe it.
    Order is preserved by construction: every leaf evaluated before the read
    is a constant, and no name lookup, short-circuit, deferred scope, await or
    operator side effect sits between the start of the statement and the read
    (`_eval_order`). Only the header of a compound statement is touched, so
    comments and code inside its body survive verbatim; `while` is refused
    because its test is re-evaluated.
    """
    if class_body or "!module" in scope_lines or i + 1 >= len(body):
        return None
    assign, use = body[i], body[i + 1]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and _is_name(assign.targets[0])
        and assign.type_comment is None
    ):
        return None
    name = assign.targets[0].id
    if f"!declared:{name}" in scope_lines:
        return None
    # `t = A` + `t = B(t)`: the intermediate value is never observed by any
    # other statement, so the mention count does not matter - unless a
    # closure or an exception handler (`_guarded`) could see it.
    rebinding = (
        isinstance(use, ast.Assign)
        and len(use.targets) == 1
        and _is_name(use.targets[0], name)
        and use.type_comment is None
        and f"!closure:{name}" not in scope_lines
        and not _guarded(scope_lines, assign, use)
    )
    if not rebinding and len(scope_lines.get(name, ())) != 2:
        return None
    if any(f in scope_lines for f in ("locals", "vars", "eval", "exec", "globals")):
        return None
    if isinstance(use, _HEADER_STMTS):
        header_end = use.body[0].lineno - 1
        if header_end < use.lineno:
            return None  # body starts on the header line
    elif isinstance(use, _SIMPLE_USE_STMTS):
        header_end = use.end_lineno
    else:
        return None
    if isinstance(use, ast.Return) and _is_name(use.value, name):
        return None  # inline-return-temp owns this shape
    found = False
    earlier = _bound_at(body, i, scope_lines)
    imports = frozenset(
        key[len("!import:") :]
        for key in scope_lines
        if key.startswith("!import:")
        and not any(
            f"!{fact}:{key[len('!import:') :]}" in scope_lines
            for fact in ("stored", "deleted", "declared")
        )
    )
    bases = imports | {
        n.id
        for n in ast.walk(use)
        if isinstance(n, ast.Name) and _provably_bound(n.id, earlier, scope_lines)
    }
    for leaf in _statement_eval_order(use, bases):
        if leaf is _BARRIER:
            break
        if isinstance(leaf, ast.Name) and leaf.id == name:
            found = isinstance(leaf.ctx, ast.Load)
            break
        if isinstance(leaf, ast.Name):
            # The assignment RHS originally runs before the whole following
            # statement. A plain name lookup has no effect unless it fails,
            # so crossing one is order-preserving exactly when the name is
            # provably bound: a parameter, a module-level binding or builtin
            # nothing in this scope rebinds, or an earlier statement's target.
            if _provably_bound(leaf.id, earlier, scope_lines):
                continue
            break
    if not found:
        return None
    if _load_count([use], name) != 1:
        return None
    import copy

    replaced = _Substitute(name, assign.value).visit(copy.deepcopy(use))
    if isinstance(replaced, _HEADER_STMTS):
        header = copy.copy(replaced)
        header.body = [ast.Pass()]
        header.orelse = []
        module = ast.Module(body=[header], type_ignores=[])
        ast.fix_missing_locations(module)
        lines = ast.unparse(module).splitlines()
        text = lines[:-1]  # drop the `pass`
        if not text or not text[-1].rstrip().endswith(":"):
            return None
        return Rewrite(
            "inline-single-use-temp",
            assign.lineno,
            header_end,
            [],
            assign.col_offset,
            -1,  # the header owns its whole last line
            text=text,
        )
    covered = [assign, use]
    start, end = _span(covered)
    return Rewrite(
        "inline-single-use-temp",
        start,
        end,
        [replaced],
        assign.col_offset,
        _end_col(covered, end),
    )


# ---- merge consecutive imports --------------------------------------------


def _rule_merge_imports(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """Pack adjacent plain imports while retaining their exact order.

    ``import a`` followed by ``import b as c`` executes the same IMPORT_NAME
    operations, in the same order, as ``import a, b as c``. Greedy chunks
    stop at the canonical width so formatting cannot erase the saving.
    """
    first = body[i]
    if not isinstance(first, ast.Import):
        return None
    run = [first]
    j = i + 1
    while j < len(body) and isinstance(body[j], ast.Import):
        run.append(body[j])
        j += 1
    if len(run) < 2:
        return None
    aliases = [alias for stmt in run for alias in stmt.names]
    width = 88 - first.col_offset
    chunks: list[list[ast.alias]] = []
    for alias in aliases:
        if chunks and len(ast.unparse(ast.Import(names=chunks[-1] + [alias]))) <= width:
            chunks[-1].append(alias)
        else:
            chunks.append([alias])
    if len(chunks) >= len(run):
        return None
    statements = [ast.Import(names=chunk) for chunk in chunks]
    start, end = _span(run)
    return Rewrite(
        "merge-imports",
        start,
        end,
        statements,
        first.col_offset,
        _end_col(run, end),
    )


# ---- merge consecutive `from M import a` / `from M import b` ---------------


def _rule_merge_from_imports(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """Consecutive imports from one module become one statement per 88 columns.

    The module is imported (executed) once either way and its attributes are
    looked up in the same order, so the semantics are identical. Names are
    packed greedily so every emitted line fits the canonical width; a merge
    the formatter would explode again is never proposed.
    """
    first = body[i]
    if not isinstance(first, ast.ImportFrom) or any(a.name == "*" for a in first.names):
        return None
    run = [first]
    j = i + 1
    while j < len(body):
        nxt = body[j]
        if not (
            isinstance(nxt, ast.ImportFrom)
            and nxt.module == first.module
            and nxt.level == first.level
            and not any(a.name == "*" for a in nxt.names)
        ):
            break
        run.append(nxt)
        j += 1
    if len(run) < 2:
        return None
    aliases = [alias for stmt in run for alias in stmt.names]
    width = 88 - first.col_offset
    chunks: list[list[ast.alias]] = []
    for alias in aliases:
        if chunks:
            trial = ast.ImportFrom(
                module=first.module, names=chunks[-1] + [alias], level=first.level
            )
            if len(ast.unparse(trial)) <= width:
                chunks[-1].append(alias)
                continue
        chunks.append([alias])
    if len(chunks) >= len(run):
        return None
    stmts = [
        ast.ImportFrom(module=first.module, names=chunk, level=first.level)
        for chunk in chunks
    ]
    start, end = _span(run)
    return Rewrite(
        "merge-from-imports",
        start,
        end,
        stmts,
        first.col_offset,
        _end_col(run, end),
    )


# ---- hoist a statement shared by every branch of an if/else ---------------


def _branch_bodies(stmt: ast.If) -> list[list[ast.stmt]] | None:
    """All leaf branch bodies of an if/elif/else chain, or None without else."""
    bodies = [stmt.body]
    node = stmt
    while True:
        if not node.orelse:
            return None
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            node = node.orelse[0]
            bodies.append(node.body)
            continue
        bodies.append(node.orelse)
        return bodies


def _rule_hoist_common_tail(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`if c: A; T else: B; T` -> `if c: A else: B` + `T`.

    The last statement of every branch is the same statement, so it runs
    exactly once after whichever branch was taken - which is what placing it
    after the whole `if` does. Every branch keeps at least one statement.
    """
    stmt = body[i]
    if not isinstance(stmt, ast.If):
        return None
    bodies = _branch_bodies(stmt)
    if bodies is None or any(len(b) < 2 for b in bodies):
        return None
    tails = [b[-1] for b in bodies]
    dump = ast.dump(tails[0], include_attributes=False)
    if any(ast.dump(t, include_attributes=False) != dump for t in tails[1:]):
        return None
    import copy

    new_if = copy.deepcopy(stmt)
    for b in _branch_bodies(new_if):
        b.pop()
    start, end = _span([stmt])
    return Rewrite(
        "hoist-common-tail",
        start,
        end,
        [new_if, tails[0]],
        stmt.col_offset,
        _end_col([stmt], end),
    )


def _unparse_stmt(stmt: ast.stmt) -> str:
    module = ast.Module(body=[stmt], type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module)


# ---- merge consecutive `del` statements ------------------------------------


def _rule_merge_del(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`del a` + `del b` -> `del a, b`: the deletions run in the same order."""
    first = body[i]
    if not isinstance(first, ast.Delete):
        return None
    run = [first]
    j = i + 1
    while j < len(body) and isinstance(body[j], ast.Delete):
        run.append(body[j])
        j += 1
    if len(run) < 2:
        return None
    targets = [target for stmt in run for target in stmt.targets]
    merged = ast.Delete(targets=targets)
    if first.col_offset + len(_unparse_stmt(merged)) > 88:
        return None
    start, end = _span(run)
    return Rewrite(
        "merge-del", start, end, [merged], first.col_offset, _end_col(run, end)
    )


# ---- pack consecutive assignments into one tuple assignment ---------------


def _simple_value(node: ast.expr) -> bool:
    """Names, constants and containers/arithmetic of those: evaluating one
    cannot run user code, so nothing an earlier store does can change it."""
    return all(
        isinstance(
            n,
            (
                ast.Name,
                ast.Constant,
                ast.Tuple,
                ast.List,
                ast.Set,
                ast.UnaryOp,
                ast.BinOp,
                ast.operator,
                ast.unaryop,
                ast.expr_context,
            ),
        )
        for n in ast.walk(node)
    )


def _pack_target(target: ast.expr) -> tuple[str, bool] | None:
    """`(key, simple_only)` for a packable target, or None.

    A plain name packs with any right-hand side. `obj.attr` and `obj[key]`
    stores may run user code (a property setter, `__setitem__`), so they
    pack only when every later right-hand side is `_simple_value`.
    """
    if isinstance(target, ast.Name):
        return target.id, False
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}", True
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and isinstance(target.slice, (ast.Name, ast.Constant))
    ):
        return f"{target.value.id}[{ast.unparse(target.slice)}]", True
    return None


_PACK_UNSAFE = (ast.Yield, ast.YieldFrom, ast.Await, ast.NamedExpr, ast.Starred)


def _pack_pairs(stmt: ast.stmt) -> list[tuple[str, bool, ast.expr, ast.expr]] | None:
    """`[(key, simple_only, target, value)]` for a packable assignment.

    A plain `a = x` is one pair; an existing `a, b = x, y` contributes its
    pairs as a group (they must stay in one statement, e.g. a swap).
    """
    if not (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and stmt.type_comment is None
        and not any(isinstance(n, _PACK_UNSAFE) for n in ast.walk(stmt))
    ):
        return None
    target, value = stmt.targets[0], stmt.value
    if isinstance(target, ast.Tuple):
        if not (
            isinstance(value, ast.Tuple)
            and len(target.elts) == len(value.elts)
            and len(target.elts) > 1
        ):
            return None
        targets, values = target.elts, value.elts
    else:
        targets, values = [target], [value]
    pairs = []
    for t, v in zip(targets, values, strict=True):
        packable = _pack_target(t)
        if packable is None:
            return None
        pairs.append((packable[0], packable[1], t, v))
    return pairs


def _rule_pack_assignments(
    body: list[ast.stmt], i: int, scope_lines: dict, class_body: bool = False
) -> Rewrite | None:
    """`a = x` + `b = y` -> `a, b = x, y` when the order of effects is kept.

    Python evaluates every right-hand side of a tuple assignment before the
    first store, so the rewrite reorders only "store a" against "evaluate
    y". That is unobservable when `y` does not read `a` (checked on every
    later right-hand side against every earlier target), `a` is not shared
    with a closure, global or nonlocal that a call inside `y` could reach,
    and no exception raised by `y` can be caught while `a` is still expected
    to be bound (the statements are outside any `try`/`with` in the scope,
    or every right-hand side is a name or constant). Attribute and subscript
    targets additionally require every later right-hand side to be free of
    calls and lookups, since their stores can run user code.

    Statements are packed greedily into the longest prefix that fits the
    canonical width (a wider line would be exploded by the formatter into
    more lines than it replaces); the chunk starting at `body[i]` is emitted
    and later chunks are picked up by the next pass.
    """
    guarded = set(scope_lines.get("!guarded", ()))
    chunk: list[ast.Assign] = []
    chunk_pairs: list[tuple[str, bool, ast.expr, ast.expr]] = []
    j = i
    while j < len(body):
        stmt = body[j]
        pairs = _pack_pairs(stmt)
        if pairs is None:
            break
        values = [v for _, _, _, v in pairs]
        bases = [key.split(".")[0].split("[")[0] for key, _, _, _ in pairs]
        read = {n.id for v in values for n in ast.walk(v) if isinstance(n, ast.Name)}
        simple = all(_simple_value(v) for v in values)
        cannot_join = (
            any(f"!declared:{base}" in scope_lines for base in bases)
            or any(key in {k for k, _, _, _ in chunk_pairs} for key, _, _, _ in pairs)
            or any(
                key.split(".")[0].split("[")[0] in read for key, _, _, _ in chunk_pairs
            )
            or (not simple and any(only for _, only, _, _ in chunk_pairs))
            or (
                not simple
                and any(
                    f"!closure:{key.split('.')[0].split('[')[0]}" in scope_lines
                    for key, _, _, _ in chunk_pairs
                )
            )
        )
        if chunk and not cannot_join:
            in_try = any(
                line in guarded
                for s in chunk + [stmt]
                for line in range(s.lineno, s.end_lineno + 1)
            )
            if in_try and not all(
                isinstance(v, (ast.Name, ast.Constant))
                for _, _, _, v in chunk_pairs + pairs
            ):
                cannot_join = True
        if chunk and not cannot_join:
            text = _pack_text(chunk_pairs + pairs)
            if body[i].col_offset + len(text) > 88:
                cannot_join = True
        if chunk and cannot_join:
            break
        if (
            not chunk
            and cannot_join
            and any(f"!declared:{base}" in scope_lines for base in bases)
        ):
            return None
        chunk.append(stmt)
        chunk_pairs += pairs
        j += 1
    if len(chunk) < 2:
        return None
    # Leave the last assignment to any rule that consumes `x = ...` plus the
    # statement after it (loop folds, `return x`, single-use temps): those
    # save at least as much, and packing first would hide the shape.
    if j < len(body) and isinstance(chunk[-1].targets[0], ast.Name):
        for rule, fn in _RULE_FNS.items():
            if rule != "pack-assignments" and fn(body, j - 1, scope_lines, class_body):
                chunk.pop()
                break
        if len(chunk) < 2:
            return None
        chunk_pairs = [pair for stmt in chunk for pair in _pack_pairs(stmt)]
    text = _pack_text(chunk_pairs)
    start, end = _span(chunk)
    return Rewrite(
        "pack-assignments",
        start,
        end,
        [],
        chunk[0].col_offset,
        _end_col(chunk, end),
        text=[text],
    )


def _pack_text(pairs: list[tuple[str, bool, ast.expr, ast.expr]]) -> str:
    values = [
        f"({ast.unparse(v)})" if isinstance(v, ast.Lambda) else ast.unparse(v)
        for _, _, _, v in pairs
    ]
    return ", ".join(ast.unparse(t) for _, _, t, _ in pairs) + " = " + ", ".join(values)


# ---- else-after-terminator (textual: the else body must survive verbatim) --

_TERM_STMTS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
_ELSE_HEADER = re.compile(r"^(\s*)else\s*:\s*(#.*)?$")


def _collapse_else(source: str) -> tuple[str, list[str]]:
    """`if c: <terminator> else: BODY` -> `if c: <terminator>` + dedented BODY.

    The `else:` arm is unreachable-except-when-not-c, and code after the `if`
    runs exactly then too, so the arm can dedent out of the `if`. This is the
    pattern a 7B model kept 'discovering' on click (ruff's RET505 flags it
    but has no autofix); 15 sites in click alone.

    Rendered TEXTUALLY, not via ast.unparse: the else body keeps its
    comments, docstrings and formatting verbatim — only the `else:` line is
    dropped and the body dedents by one level. Declines anything that is not
    a clean whole-line `else:` header (elif chains, shared lines, odd
    continuation indents) and recompiles before accepting.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, []
    # parent map: an `if` that is itself an elif arm (directly inside another
    # If's orelse) must not collapse. Its dedented else would sit after the
    # WHOLE enclosing chain, reachable from earlier branches that do not
    # terminate — click's version_option tripped exactly this: the successful
    # `len == 1` branch fell through into the dedented "not installed" raise.
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    lines = source.splitlines()
    string_continuations = {
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        or isinstance(node, ast.Constant)
        and isinstance(node.value, (str, bytes))
        for line in range(node.lineno, node.end_lineno)
    }
    cuts: list[tuple[int, int, int]] = []  # (else_line_idx, span_end_idx, unit)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not node.orelse:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.If) and node in parent.orelse:
            continue  # an elif arm: the else cannot be dedented safely
        if not (node.body and isinstance(node.body[-1], _TERM_STMTS)):
            continue
        first = node.orelse[0]
        # the `else:` header sits above the first arm statement, possibly
        # behind blank lines or comments
        idx = first.lineno - 2
        while idx >= node.lineno and not _ELSE_HEADER.match(lines[idx]):
            if lines[idx].strip() and not lines[idx].lstrip().startswith("#"):
                break  # real code without a header: not a clean else arm
            idx -= 1
        match = _ELSE_HEADER.match(lines[idx]) if idx >= node.lineno else None
        if match is None or len(match.group(1)) != node.col_offset:
            continue
        unit = first.col_offset - node.col_offset
        if unit <= 0:
            continue
        span_end = node.end_lineno - 1
        ok = True
        for j in range(idx + 1, span_end + 1):
            if j in string_continuations:
                continue  # these leading spaces belong to the literal value
            stripped = lines[j]
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip())
            if indent < unit or indent <= node.col_offset:
                ok = False  # continuation at an odd column, or code outside the arm
                break
        if ok:
            cuts.append((idx, span_end, unit))
    if not cuts:
        return source, []
    # non-overlapping only: an inner cut inside an outer cut's span would
    # shift its line numbers; MAX_PASSES picks those up on the next sweep
    accepted: list[tuple[int, int, int]] = []
    covered: list[tuple[int, int]] = []
    for idx, span_end, unit in sorted(cuts):
        if any(lo <= idx <= hi for lo, hi in covered):
            continue
        accepted.append((idx, span_end, unit))
        covered.append((idx, span_end))
    for idx, span_end, unit in sorted(accepted, reverse=True):
        body = [
            line[unit:] if line.strip() and number not in string_continuations else line
            for number, line in enumerate(lines[idx + 1 : span_end + 1], idx + 1)
        ]
        header = _ELSE_HEADER.match(lines[idx])
        if header.group(2):
            body.insert(0, header.group(1) + header.group(2))
        lines[idx : span_end + 1] = body  # drops the `else:` header line
    new_source = "\n".join(lines)
    if source.endswith("\n"):
        new_source += "\n"
    try:
        compile(new_source, "<else-collapse>", "exec")
    except SyntaxError:
        return source, []
    return new_source, ["else-after-terminator"] * len(accepted)


_RULE_FNS = {
    "bool-return": _rule_bool_return,
    "merge-same-branch": _rule_merge_same_branch,
    "flatten-nested-if": _rule_flatten_nested_if,
    "guard-call": _rule_guard_call,
    "conditional-return": _rule_conditional_return,
    "conditional-assignment": _rule_conditional_assignment,
    "self-default-assignment": _rule_self_default_assignment,
    "inline-return-temp": _rule_inline_return_temp,
    "inline-single-use-temp": _rule_inline_single_use_temp,
    "merge-imports": _rule_merge_imports,
    "merge-from-imports": _rule_merge_from_imports,
    "hoist-common-tail": _rule_hoist_common_tail,
    "dict-build-to-literal": _rule_dict_build_literal,
    "boolean-loop-to-any-all": _rule_boolean_loop,
    "append-loop-to-comprehension": _rule_append_loop,
    "accumulate-to-sum": _rule_accumulate_sum,
    "sort-to-sorted": _rule_sort_to_sorted,
    "drop-bare-reraise": _rule_bare_reraise,
    "threshold-ladder-to-scan": _rule_threshold_ladder,
    "max-loop-to-max": _rule_max_loop,
    "merge-del": _rule_merge_del,
    "pack-assignments": _rule_pack_assignments,
}


_BLOCK_FIELDS = ("body", "orelse", "finalbody")


SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _collect(
    node: ast.AST,
    only: set[str],
    out: list[Rewrite],
    scope_lines: dict,
    class_body: bool = False,
) -> None:
    """Depth-first scan for non-overlapping rewrites over every statement list.

    `class_body` tracks whether the statements being scanned sit directly in
    a `class` block: a walrus inside a comprehension is a SyntaxError there,
    and `ast.parse` does not catch it (it is raised at compile time), so the
    rule has to decline up front.
    """
    if isinstance(node, SCOPES):
        # Module-level stores *are* the module's bindings; inside a function a
        # store makes the name a local that may still be unbound when a nested
        # scope runs, so such names are not passed down.
        inherited = {
            key[len("!bound:") :]
            for key in scope_lines
            if key.startswith("!bound:")
            and f"!deleted:{key[len('!bound:') :]}" not in scope_lines
            and (
                "!module" in scope_lines
                or f"!stored:{key[len('!bound:') :]}" not in scope_lines
            )
        }
        inherited |= {
            key
            for key in scope_lines
            if key.startswith("!import:") and key[len("!import:") :] in inherited
        }
        scope_lines = scope_name_lines(node, inherited)
        class_body = False
    if isinstance(node, ast.ClassDef):
        class_body = True
    for field in _BLOCK_FIELDS:
        block = getattr(node, field, None)
        if not isinstance(block, list) or not all(
            isinstance(s, ast.stmt) for s in block
        ):
            continue
        i = 0
        while i < len(block):
            hit = None
            for name in RULES:
                if name not in only or name not in _RULE_FNS:
                    continue  # textual rules (else-after-terminator) run outside _collect
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
    if rw.text is not None:
        return [(pad + line) if line.strip() else "" for line in rw.text]
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
    trailer = "" if rw.end_col < 0 else lines[rw.end - 1][rw.end_col :].strip()
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
    comment_tokens = {
        token.start[0]: token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    comment_lines = set(comment_tokens)
    lines = source.splitlines()
    applied = []
    for rw in sorted(rewrites, key=lambda r: r.start, reverse=True):
        comments = sorted(comment_lines.intersection(range(rw.start, rw.end + 1)))
        movable_comments = rw.rule in {
            "bool-return",
            "conditional-return",
            "conditional-assignment",
            "merge-same-branch",
            "flatten-nested-if",
            "guard-call",
            "self-default-assignment",
            "inline-single-use-temp",
            "merge-imports",
            "merge-from-imports",
            "hoist-common-tail",
            "merge-del",
            "pack-assignments",
        } and all(
            not re.match(
                r"#\s*(?:noqa\b|type:\s*ignore\b|pyright:|mypy:|ruff:|"
                r"fmt:|isort:|pragma:|coverage:)",
                comment_tokens[number],
                re.IGNORECASE,
            )
            for number in comments
        )
        if not _line_aligned(lines, rw) or comments and not movable_comments:
            continue
        rendered = _render(rw)
        if comments:
            rendered = [
                " " * rw.col + comment_tokens[number] for number in comments
            ] + rendered
        if re.match(r"elif\b", lines[rw.start - 1].lstrip()):
            # An AST If in orelse can be a textual elif. Its replacement
            # must remain conditional on all earlier branches failing.
            rendered = [" " * rw.col + "else:"] + [
                "    " + line if line else line for line in rendered
            ]
        candidate_lines = lines[: rw.start - 1] + rendered + lines[rw.end :]
        if rw.rule in {
            "conditional-return",
            "conditional-assignment",
            "merge-same-branch",
            "flatten-nested-if",
            "guard-call",
            "self-default-assignment",
            "inline-single-use-temp",
            "merge-imports",
            "merge-from-imports",
            "hoist-common-tail",
            "pack-assignments",
        }:
            from .loc import measure

            # Wide ternaries can expand under the unchanged canonical formatter.
            # Do not let one such expansion consume unrelated savings in a file.
            before = measure("\n".join(lines) + "\n", "python")
            after = measure("\n".join(candidate_lines) + "\n", "python")
            if not after.formatted or after.code > before.code:
                continue
        lines = candidate_lines
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
        if "else-after-terminator" in selected:
            # textual pass (comments in the dedented arm survive verbatim);
            # runs before the AST rules so they see the collapsed shape
            current, names = _collapse_else(current)
            if names:
                applied += names
        current, names = _one_pass(current, selected)
        if names:
            applied += names
        if not names and not (
            "else-after-terminator" in selected and _collapse_else(current)[1]
        ):
            break
    return current, applied
