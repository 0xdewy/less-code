"""In-package fallback removal (aggressive profile, Python only).

Libraries that want each module to work when copied out of the package on
its own guard sibling imports with a fallback:

    try:
        from .typeutils import make_sentinel
        _MISSING = make_sentinel(var_name="_MISSING")
    except ImportError:
        _MISSING = object()

or vendor a whole copy that a later sibling import overrides:

    class OrderedMultiDict(dict): ...      # vendored copy
    try:
        from .dictutils import OrderedMultiDict
    except ImportError:
        pass

Inside the package the relative import of an in-repo sibling cannot fail (the
sibling exists and defines the name), so the `except ImportError` arm and the
overridden copy never run. Removing them keeps in-package behavior identical
and drops the copy-the-file-out use case, which is why this is opt-in.

What is proven before an edit is proposed, and nothing else is assumed:

  * every import in the `try` body is relative and resolves to a module file
    in this tree that binds each imported name at its top level;
  * that sibling does not import the current module (a cycle could make the
    import fail half-way and the fallback real);
  * the handler catches exactly `ImportError` (or `ModuleNotFoundError`), with
    no `else`/`finally` arms;
  * a deleted definition is a module-level `def`/`class` whose name the
    unwrapped import rebinds, with no module-level statement in between that
    could read it at import time.

  * the sibling's public API for a deleted definition is a superset of the
    copy's (same signatures), so the module still exposes what it did.

Documentation inside deleted code, and the API entries now supplied by the
import, are reported so the gate can allow exactly those and no others.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter
from pathlib import Path

from .api_check import python_api

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def _module_file(path: Path, node: ast.ImportFrom, root: Path) -> Path | None:
    """The in-tree file a relative `from .x import y` resolves to, if any."""
    if node.level == 0:
        return None
    base = path.parent
    for _ in range(node.level - 1):
        base = base.parent
    if not base.is_relative_to(root):
        return None
    parts = node.module.split(".") if node.module else []
    target = base.joinpath(*parts)
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {
                (alias.asname or alias.name).split(".")[0] for alias in stmt.names
            }
    return names


def _imports_module(tree: ast.Module, sibling: Path, current: Path) -> bool:
    """Does `sibling` import `current` anywhere (a potential cycle)?"""
    stem = current.stem
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == stem:
                return True
            if any(alias.name == stem for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == stem for alias in node.names):
                return True
    return False


def _in_package_try(
    stmt: ast.stmt, path: Path, root: Path, parsed: dict[Path, ast.Module]
) -> set[str] | None:
    """Names a provably-succeeding guarded sibling import binds, or None."""
    if not (
        isinstance(stmt, ast.Try)
        and len(stmt.handlers) == 1
        and not stmt.orelse
        and not stmt.finalbody
    ):
        return None
    handler = stmt.handlers[0]
    caught = handler.type
    if not (
        isinstance(caught, ast.Name)
        and caught.id in _IMPORT_ERRORS
        and not handler.name
    ):
        return None
    imports = [s for s in stmt.body if isinstance(s, (ast.Import, ast.ImportFrom))]
    if not imports or any(not isinstance(s, ast.ImportFrom) for s in imports):
        return None
    # only the imports may raise ImportError: other statements must be plain
    # assignments/expressions that do not import anything themselves
    for s in stmt.body:
        if isinstance(s, ast.ImportFrom):
            continue
        if not isinstance(s, (ast.Assign, ast.Expr)) or any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"__import__", "import_module"}
            for n in ast.walk(s)
        ):
            return None
    bound: set[str] = set()
    for node in imports:
        target = _module_file(path, node, root)
        if target is None or target == path:
            return None
        if target not in parsed:
            try:
                parsed[target] = ast.parse(target.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                return None
        sibling = parsed[target]
        if any(alias.name == "*" for alias in node.names):
            return None
        if not {alias.name for alias in node.names} <= _top_level_names(sibling):
            return None
        if _imports_module(sibling, target, path):
            return None
        bound |= {alias.asname or alias.name for alias in node.names}
    return bound


def _reads(node: ast.AST, name: str) -> bool:
    """Could executing `node` at module level read `name` right away?

    Function bodies are skipped (they run later), but their decorators,
    defaults and annotations run now; class bodies run now.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        eager = [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]
        return any(_reads(n, name) for n in eager if n is not None)
    if isinstance(node, ast.Lambda):
        return any(_reads(n, name) for n in node.args.defaults)
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, ast.Load)
    return any(_reads(child, name) for child in ast.iter_child_nodes(node))


def _documentation(text: str, tree: ast.AST) -> Counter:
    found: Counter = Counter(
        ("comment", tok.string)
        for tok in tokenize.generate_tokens(io.StringIO(text).readline)
        if tok.type == tokenize.COMMENT
    )
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            value = ast.get_docstring(node, clean=False)
            if value is not None:
                found[("docstring", value)] += 1
    return found


def _multiline_strings(source: str, start: int, end: int) -> bool:
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if (
            tok.type == tokenize.STRING
            and tok.start[0] != tok.end[0]
            and start <= tok.start[0] <= end
        ):
            return True
    return False


def _api_entries(api: dict[str, str], name: str) -> dict[str, str]:
    return {k: v for k, v in api.items() if k == name or k.startswith(name + ".")}


def strip_in_package_fallbacks(
    source: str, path: Path, root: Path, parsed: dict[Path, ast.Module] | None = None
) -> tuple[str, list[str], Counter, set[str]]:
    """Return (new_source, notes, documentation_removed, api_entries_waived)."""
    parsed = {} if parsed is None else parsed
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, [], Counter(), set()
    own_api = python_api(source)
    waived: set[str] = set()
    lines = source.splitlines(keepends=True)
    deletions: list[tuple[int, int]] = []  # 1-based inclusive line spans
    unwraps: list[ast.Try] = []
    notes: list[str] = []
    body = tree.body
    for index, stmt in enumerate(body):
        bound = _in_package_try(stmt, path, root, parsed)
        if bound is None:
            continue
        if lines[stmt.lineno - 1][: stmt.col_offset].strip() or any(
            s.col_offset != stmt.col_offset + 4 for s in stmt.body
        ):
            continue
        if _multiline_strings(source, stmt.lineno, stmt.body[-1].end_lineno):
            continue  # dedenting would change the string's value
        unwraps.append(stmt)
        notes.append(f"{path.name}: unwrapped in-package import fallback")
        # earlier definitions this import overrides are dead in the package
        for back in range(index - 1, -1, -1):
            earlier = body[back]
            if not isinstance(
                earlier, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if any(_reads(earlier, name) for name in bound):
                    break
                continue
            if earlier.name not in bound:
                if any(_reads(earlier, name) for name in bound):
                    break
                continue
            if any(_reads(s, earlier.name) for s in body[back + 1 : index]):
                break
            copy_api = _api_entries(own_api, earlier.name)
            if copy_api and not _sibling_covers(stmt, path, root, parsed, copy_api):
                continue  # the copy exposes something the sibling's does not
            waived |= set(copy_api)
            start = min([earlier.lineno] + [d.lineno for d in earlier.decorator_list])
            deletions.append((start, earlier.end_lineno))
            notes.append(f"{path.name}: removed {earlier.name} (overridden in-package)")
    if not unwraps:
        return source, [], Counter(), set()
    new_lines = list(lines)
    edits: list[tuple[int, int, list[str]]] = [(a, b, []) for a, b in deletions]
    for stmt in unwraps:
        # keep the try body (dedented one level), drop `try:` and the handler
        indent = lines[stmt.lineno - 1][: stmt.col_offset]
        last = stmt.body[-1].end_lineno
        # comment lines between `try:` and the first statement stay with the body
        kept = []
        for line in lines[stmt.lineno : last]:
            stripped = line.removeprefix(indent)
            kept.append(stripped.removeprefix("    "))
        edits.append(
            (
                stmt.lineno,
                stmt.end_lineno,
                [indent + k if k.strip() else k for k in kept],
            )
        )
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        new_lines[start - 1 : end] = replacement
    new_source = "".join(new_lines)
    try:
        new_tree = ast.parse(new_source)
        compile(new_source, str(path), "exec")
    except SyntaxError:
        return source, [], Counter(), set()
    removed = _documentation(source, tree) - _documentation(new_source, new_tree)
    return new_source, notes, removed, waived


def _sibling_covers(
    stmt: ast.Try,
    path: Path,
    root: Path,
    parsed: dict[Path, ast.Module],
    copy_api: dict[str, str],
) -> bool:
    """Every public entry of the copy exists, identically, in the sibling."""
    for node in stmt.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        target = _module_file(path, node, root)
        if target is None:
            continue
        sibling_api = python_api(target.read_text(encoding="utf-8"))
        if all(sibling_api.get(k) == v for k, v in copy_api.items()):
            return True
    return False
