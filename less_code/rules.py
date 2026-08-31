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
    if isinstance(node, ast.Call) and _is_name(node.func) and node.func.id in (
        "bool", "isinstance", "issubclass", "hasattr", "callable", "any", "all",
    ):
        return True
    return False


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
        elif isinstance(node, ast.Compare):
            node = node.left
        elif isinstance(node, ast.BinOp):
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


_RULE_FNS = {
    "bool-return": _rule_bool_return,
    "append-loop-to-comprehension": _rule_append_loop,
    "accumulate-to-sum": _rule_accumulate_sum,
    "sort-to-sorted": _rule_sort_to_sorted,
    "drop-bare-reraise": _rule_bare_reraise,
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
