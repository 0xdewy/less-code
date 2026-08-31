"""Deterministic guard-block outlining for Python.

The rule library in `rules.py` is a *peephole* optimizer: every rewrite is
local to one run of statements in one function. The single largest documented
opportunity in the python fixture is not local at all — it is the same
validate-and-raise block pasted into six different function bodies. No
peephole rule can see the twin, and iteration 09 measured that a 7B model
handed the group cannot merge it without breaking an exact pinned error
message.

This module does that merge deterministically.

## What it looks for

A *window* is a run of consecutive statements at the top level of a function
body where every statement is either

* an `if <test>: raise <exc>` (single-statement body, no `else`), or
* a plain `name = <expr>` assignment.

Windows that are identical after **alpha-renaming** (free names, bound names)
and **constant abstraction** form a group. A group with >= 3 occurrences and a
positive line saving is outlined into one module-level `_`-prefixed helper;
each occurrence becomes a single call line.

## Why it is safe

The rewrite is *not* "these statements are pure, so they can move". Purity is
never established and never needed. The claim is narrower and provable:

    Executing a statement sequence inside a function called at that point is
    the same as executing it inline, provided nothing escapes by a route the
    call cannot carry.

The routes are enumerated and each is closed by a precondition:

1. **Values computed by the block.** Every name the window binds is returned
   by the helper and unpacked at the call site — so a later reader sees the
   same object. (Names bound at only some sites are still returned; an unused
   local is harmless.)
2. **Values consumed by the block.** Every free name is passed as an argument.
   Arguments are evaluated *eagerly*, before any guard runs, so an argument
   expression that could itself raise would reorder the failure. This is why
   only `Name` loads and `Constant`s may become parameters, and why a free
   name must be provably bound at that point: a function parameter, or the
   target of an earlier unconditional top-level assignment in the same body.
   Everything else — `isinstance(...)`, `%`-formatting of the error message,
   attribute calls — stays *inside* the helper and runs in the same order.
3. **Non-local control flow.** `return`, `break`, `continue`, `yield` and
   `await` cannot cross a call boundary, so a window containing any of them is
   refused. `raise` crosses it fine, which is the whole point.
4. **Scope tricks.** A window containing a `lambda`, a nested `def`/`class`, a
   walrus, or a name declared `global`/`nonlocal` in the enclosing function is
   refused.

Error messages are preserved *exactly*: a constant that is the same at every
site is re-emitted verbatim into the helper, and a constant that differs
becomes a parameter passed literally from each call site. A message can never
be rewritten, merged or reworded, because nothing in this module ever
constructs a string.

Traceback depth changes by one frame. `str(exc)`, `type(exc)` and the raise
order do not.

Public API:

    outline_guards(sources) -> (changed_sources, notes)
"""

from __future__ import annotations

import ast
import builtins
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MIN_OCCURRENCES = 3
MAX_VARYING_CONST_CLASSES = 2
MAX_PARAMS = 4

_BUILTINS = frozenset(dir(builtins))

# statements that cannot cross a call boundary
_ESCAPES = (ast.Return, ast.Break, ast.Continue, ast.Yield, ast.YieldFrom, ast.Await)
_SCOPE_TRICKS = (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.NamedExpr)


@dataclass
class Site:
    """One occurrence of a group's window in one function body."""

    path: str
    func: str
    start: int          # index into the body list
    end: int            # inclusive
    lineno: int
    end_lineno: int
    col: int
    end_col: int
    params: list[str]   # actual names, in canonical P0..Pn order
    binds: list[str]    # actual names, in canonical B0..Bm order
    consts: list[object]
    used_after: set[int] = field(default_factory=set)
    stmts: list = field(default_factory=list)


# ---- module facts ----------------------------------------------------------


def _module_names(tree: ast.Module) -> set[str]:
    """Names bound at module level (visible to a module-level helper)."""
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _arg_names(fn) -> list[str]:
    a = fn.args
    names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return names


def _declared(fn) -> set[str]:
    """Names the function declares `global` or `nonlocal` (anywhere inside)."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
    return out


# ---- window eligibility ----------------------------------------------------


def _is_guard(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.If)
        and not stmt.orelse
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Raise)
        and stmt.body[0].exc is not None
    )


def _is_simple_assign(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    )


def _eligible(stmt: ast.stmt) -> bool:
    if not (_is_guard(stmt) or _is_simple_assign(stmt)):
        return False
    for node in ast.walk(stmt):
        if isinstance(node, _ESCAPES) or isinstance(node, _SCOPE_TRICKS):
            return False
    return True


def _loads(node: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]


def _all_ids(nodes) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                out.update(n.names)
    return out


# ---- abstraction -----------------------------------------------------------


class _Abstract(ast.NodeTransformer):
    """Rename names to canonical slots and lift every constant to a slot."""

    def __init__(self, name_map: dict[str, str]) -> None:
        self.name_map = name_map
        self.consts: list[object] = []

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = self.name_map.get(node.id, node.id)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        self.consts.append(node.value)
        return ast.Constant(value=f"\x00C{len(self.consts) - 1}")


class _Concretize(ast.NodeTransformer):
    """Undo `_Abstract` for the helper body: literal back, or a parameter."""

    def __init__(self, values: list[object], params: dict[int, str], names: dict[str, str]) -> None:
        self.values = values
        self.params = params
        self.names = names

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = self.names.get(node.id, node.id)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        if isinstance(value, str) and value.startswith("\x00C"):
            index = int(value[2:])
            if index in self.params:
                return ast.Name(id=self.params[index], ctx=ast.Load())
            return ast.Constant(value=self.values[index])
        return node


def _window_site(
    path: str, func: str, body: list[ast.stmt], start: int, end: int,
    bound_before: set[str], argnames: set[str], declared: set[str],
    module_names: set[str],
) -> tuple[str, Site] | None:
    """Validate `body[start:end+1]` as a window; return `(key, site)` or None."""
    stmts = body[start : end + 1]
    params: list[str] = []
    binds: list[str] = []
    seen_params: set[str] = set()
    seen_binds: set[str] = set()
    for stmt in stmts:
        for name in _loads(stmt):
            if name in seen_binds or name in seen_params:
                continue
            if name in argnames or name in bound_before:
                # route 2: provably bound at this point, so passed by value
                if name in declared:
                    return None
                params.append(name)
                seen_params.add(name)
            elif name not in module_names and name not in _BUILTINS:
                # a local this analysis cannot prove is bound here: the helper
                # would raise NameError where the original did not
                return None
        if _is_simple_assign(stmt):
            target = stmt.targets[0].id
            if target in declared or target in seen_params:
                return None
            if target not in seen_binds:
                binds.append(target)
                seen_binds.add(target)
    if len(params) + len(binds) == 0:
        return None
    if len(binds) != len(set(binds)):
        return None
    if set(params) & set(binds):
        return None
    if len(params) > MAX_PARAMS:
        return None
    name_map = {name: f"\x00P{i}" for i, name in enumerate(params)}
    name_map.update({name: f"\x00B{i}" for i, name in enumerate(binds)})
    import copy

    abstract = _Abstract(name_map)
    copied = [abstract.visit(copy.deepcopy(s)) for s in stmts]
    key = "\n".join(ast.dump(s) for s in copied)
    site = Site(
        path=path, func=func, start=start, end=end,
        lineno=stmts[0].lineno, end_lineno=stmts[-1].end_lineno,
        col=stmts[0].col_offset, end_col=stmts[-1].end_col_offset,
        params=params, binds=binds, consts=abstract.consts, stmts=copied,
    )
    later = _all_ids(body[end + 1 :])
    site.used_after = {i for i, name in enumerate(binds) if name in later}
    return key, site


def _collect_sites(path: str, tree: ast.Module) -> dict[str, list[Site]]:
    """Every eligible window in every function body of one module."""
    groups: dict[str, list[Site]] = {}
    module_names = _module_names(tree)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        argnames = set(_arg_names(fn))
        declared = _declared(fn)
        body = fn.body
        bound_before: set[str] = set()
        runs: list[list[int]] = []
        current: list[int] = []
        for i, stmt in enumerate(body):
            if _eligible(stmt):
                current.append(i)
            else:
                if current:
                    runs.append(current)
                current = []
        if current:
            runs.append(current)
        # unconditional top-level bindings, per index
        bound_at: list[set[str]] = []
        acc: set[str] = set()
        for stmt in body:
            bound_at.append(set(acc))
            if _is_simple_assign(stmt):
                acc.add(stmt.targets[0].id)
        for run in runs:
            for a in range(len(run)):
                for b in range(a, len(run)):
                    start, end = run[a], run[b]
                    made = _window_site(
                        path, f"{fn.name}:{fn.lineno}", body, start, end,
                        bound_at[start], argnames, declared, module_names,
                    )
                    if made is None:
                        continue
                    key, site = made
                    groups.setdefault(key, []).append(site)
    return groups


# ---- grouping over constants ----------------------------------------------


def _const_classes(sites: list[Site]) -> tuple[list[list[int]], bool] | None:
    """Partition varying constant slots into parameter classes."""
    n = len(sites[0].consts)
    if any(len(s.consts) != n for s in sites):
        return None
    varying = [i for i in range(n) if len({repr(s.consts[i]) for s in sites}) > 1]
    classes: list[list[int]] = []
    for i in varying:
        for cls in classes:
            j = cls[0]
            if all(repr(s.consts[i]) == repr(s.consts[j]) for s in sites):
                cls.append(i)
                break
        else:
            classes.append([i])
    if len(classes) > MAX_VARYING_CONST_CLASSES:
        return None
    return classes, True


# ---- naming ----------------------------------------------------------------


def _pick(candidates: list[str], taken: set[str], fallback: str) -> str:
    base = Counter(candidates).most_common(1)[0][0] if candidates else fallback
    if not base.isidentifier() or base in _BUILTINS:
        base = fallback
    name = base
    i = 2
    while name in taken:
        name = f"{base}{i}"
        i += 1
    taken.add(name)
    return name


# ---- rendering -------------------------------------------------------------


def _render_helper(
    sites: list[Site], classes: list[list[int]], helper: str,
    param_names: list[str], const_names: list[str], bind_names: list[str],
    returns: list[int],
) -> str:
    import copy

    proto = sites[0]
    slot_param = {}
    for cls, name in zip(classes, const_names):
        for slot in cls:
            slot_param[slot] = name
    names = {f"\x00P{i}": param_names[i] for i in range(len(param_names))}
    names.update({f"\x00B{i}": bind_names[i] for i in range(len(bind_names))})
    body = [
        _Concretize(proto.consts, slot_param, names).visit(copy.deepcopy(s))
        for s in proto.stmts
    ]
    if returns:
        value: ast.expr
        if len(returns) == 1:
            value = ast.Name(id=bind_names[returns[0]], ctx=ast.Load())
        else:
            value = ast.Tuple(
                elts=[ast.Name(id=bind_names[i], ctx=ast.Load()) for i in returns],
                ctx=ast.Load(),
            )
        body.append(ast.Return(value=value))
    fn = ast.FunctionDef(
        name=helper,
        args=ast.arguments(
            posonlyargs=[], args=[ast.arg(arg=n) for n in param_names + const_names],
            kwonlyargs=[], kw_defaults=[], defaults=[],
        ),
        body=body, decorator_list=[], returns=None, type_params=[],
    )
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module)


def _render_call(site: Site, helper: str, classes: list[list[int]], returns: list[int]) -> str:
    args = [ast.Name(id=n, ctx=ast.Load()) for n in site.params]
    args += [ast.Constant(value=site.consts[cls[0]]) for cls in classes]
    call = ast.Call(func=ast.Name(id=helper, ctx=ast.Load()), args=args, keywords=[])
    stmt: ast.stmt
    if not returns:
        stmt = ast.Expr(value=call)
    elif len(returns) == 1:
        stmt = ast.Assign(targets=[ast.Name(id=site.binds[returns[0]], ctx=ast.Store())], value=call)
    else:
        stmt = ast.Assign(
            targets=[ast.Tuple(
                elts=[ast.Name(id=site.binds[i], ctx=ast.Store()) for i in returns],
                ctx=ast.Store(),
            )],
            value=call,
        )
    module = ast.Module(body=[stmt], type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module)


# ---- placement -------------------------------------------------------------


def _helper_anchor(tree: ast.Module) -> int | None:
    """1-based line before which a module-level helper may be inserted."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines = [node.lineno] + [d.lineno for d in node.decorator_list]
            return min(lines)
    return None


def _import_anchor(tree: ast.Module) -> int:
    """1-based line before which an import may be inserted."""
    line = 1
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            line = node.end_lineno + 1
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            line = node.end_lineno + 1
            continue
        break
    return line


def _line_aligned(lines: list[str], site: Site) -> bool:
    if site.end_lineno > len(lines):
        return False
    if lines[site.lineno - 1][: site.col].strip():
        return False
    trailer = lines[site.end_lineno - 1][site.end_col :].strip()
    return trailer == "" or trailer.startswith("#")


# ---- the pass --------------------------------------------------------------


def _score(sites: list[Site], helper_lines: int) -> int:
    saved = sum((s.end_lineno - s.lineno + 1) - 1 for s in sites)
    return saved - helper_lines


def outline_guards(
    sources: dict[str, str], min_occurrences: int = MIN_OCCURRENCES,
) -> tuple[dict[str, str], list[str]]:
    """Outline repeated validate-and-raise blocks into shared helpers.

    `sources` maps path -> text. Returns `(changed, notes)` where `changed`
    only contains files whose text moved. Never returns a source that fails to
    compile; on any doubt the input is returned unchanged.
    """
    trees: dict[str, ast.Module] = {}
    for path, text in sources.items():
        try:
            trees[path] = ast.parse(text)
        except SyntaxError:
            return {}, []
    if not trees:
        return {}, []

    groups: dict[str, list[Site]] = {}
    for path, tree in trees.items():
        for key, sites in _collect_sites(path, tree).items():
            groups.setdefault(key, []).extend(sites)

    file_lines = {path: text.splitlines() for path, text in sources.items()}
    globals_by_file = {path: _module_names(tree) for path, tree in trees.items()}
    taken = {path: set(globals_by_file[path]) for path in trees}

    # greedy: repeatedly take the highest-scoring group whose sites are free
    used: dict[tuple[str, str], list[tuple[int, int]]] = {}
    edits: dict[str, list[tuple[int, int, str]]] = {path: [] for path in sources}
    helpers: dict[str, list[str]] = {path: [] for path in sources}
    imports: dict[str, set[tuple[str, str]]] = {path: set() for path in sources}
    notes: list[str] = []

    def free(site: Site) -> bool:
        for lo, hi in used.get((site.path, site.func), []):
            if site.start <= hi and lo <= site.end:
                return False
        return _line_aligned(file_lines[site.path], site)

    remaining = dict(groups)
    while True:
        best = None
        for key, sites in remaining.items():
            live = [s for s in sites if free(s)]
            if len(live) < min_occurrences:
                continue
            made = _const_classes(live)
            if made is None:
                continue
            classes, _ok = made
            # optimistic lower bound on the helper's cost (`def` + the body,
            # no `return`): used only to rank and to reject cheaply, the real
            # cost is measured against the rendered helper below
            approx = max(s.end_lineno - s.lineno + 1 for s in live) + 1
            score = _score(live, approx)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, key, live, classes)
        if best is None:
            break
        _score_value, key, live, classes = best
        remaining.pop(key)

        host = Counter(s.path for s in live).most_common(1)[0][0]
        # locals of the helper get their own namespace: only the helper's own
        # name has to be unique in the host module
        pool = set(globals_by_file[host])
        n_params = len(live[0].params)
        param_names = [
            _pick([s.params[i] for s in live], pool, f"arg{i}") for i in range(n_params)
        ]
        const_names = []
        for i, cls in enumerate(classes):
            strings = all(isinstance(s.consts[cls[0]], str) for s in live)
            const_names.append(_pick([], pool, "message" if strings else f"value{i}"))
        n_binds = len(live[0].binds)
        bind_names = [
            _pick([s.binds[i] for s in live], pool, f"tmp{i}") for i in range(n_binds)
        ]
        returns = sorted({i for s in live for i in s.used_after})
        helper = _pick(
            [f"_check_{param_names[0]}"] if param_names else [], taken[host], "_check"
        )
        text = _render_helper(live, classes, helper, param_names, const_names, bind_names, returns)
        if _score(live, len(text.splitlines())) <= 0:
            continue

        helpers[host].append(text)
        for site in live:
            call = _render_call(site, helper, classes, returns)
            edits[site.path].append((site.lineno, site.end_lineno, " " * site.col + call))
            used.setdefault((site.path, site.func), []).append((site.start, site.end))
            if site.path != host:
                imports[site.path].add((Path(host).stem, helper))
        notes.append(
            f"{Path(host).name}: outlined {helper}() from {len(live)} sites "
            f"({_score(live, len(text.splitlines()))} code lines saved)"
        )

    changed: dict[str, str] = {}
    for path, text in sources.items():
        if not edits[path] and not helpers[path] and not imports[path]:
            continue
        lines = list(file_lines[path])
        for lo, hi, replacement in sorted(edits[path], reverse=True):
            lines[lo - 1 : hi] = [replacement]
        if helpers[path]:
            anchor = _helper_anchor(trees[path])
            block: list[str] = []
            for helper_text in helpers[path]:
                block += helper_text.splitlines() + [""]
            if anchor is None:
                lines += [""] + block
            else:
                offset = _shift(edits[path], anchor)
                lines[offset - 1 : offset - 1] = block
        if imports[path]:
            anchor = _shift(edits[path], _import_anchor(trees[path]))
            block = [f"from {mod} import {name}" for mod, name in sorted(imports[path])]
            lines[anchor - 1 : anchor - 1] = block
        new_text = "\n".join(lines)
        if text.endswith("\n"):
            new_text += "\n"
        try:
            compile(new_text, "<outline>", "exec")
        except (SyntaxError, ValueError):  # defensive: never emit broken code
            return {}, []
        if new_text != text:
            changed[path] = new_text
    return changed, notes


def _shift(edits: list[tuple[int, int, str]], line: int) -> int:
    """Map a pre-edit line number to its post-edit position."""
    out = line
    for lo, hi, _text in edits:
        if hi < line:
            out -= (hi - lo + 1) - 1
    return out
