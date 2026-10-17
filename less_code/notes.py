"""String-statement notes become comments (aggressive profile, Python only).

A string literal used as a statement anywhere except a docstring position is
evaluated and discarded: it is a developer note that happens to be code. The
rewrite turns it into `#` comments carrying the same text, so the note
survives verbatim and nothing executes differently. Docstrings (first
statement of a module, class or function) are never touched, and a note that
is the only statement of its block is left alone (the block needs a body), and
so is a string right after an assignment: Sphinx documents the assigned
attribute with it.

The returned `added` multiset lists exactly the comments created, so the
documentation gate can require that nothing else changed.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from collections import Counter

_DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def notes_to_comments(source: str) -> tuple[str, int, Counter]:
    """Return (new_source, notes_converted, comments_added)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0, Counter()
    lines = source.splitlines(keepends=True)
    # line numbers holding more than one statement / token sharing
    statement_starts = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            statement_starts[node.lineno] += 1
    edits: list[tuple[int, int, list[str]]] = []
    added: Counter = Counter()
    for owner in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(owner, field, None)
            if not isinstance(block, list) or len(block) < 2:
                continue
            for index, stmt in enumerate(block):
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    continue
                if index == 0 and field == "body" and isinstance(owner, _DOC_OWNERS):
                    continue  # a docstring
                if index and isinstance(
                    block[index - 1], (ast.Assign, ast.AnnAssign, ast.AugAssign)
                ):
                    continue  # an attribute docstring (Sphinx autodoc reads these)
                start, end = stmt.lineno, stmt.end_lineno
                indent = lines[start - 1][: stmt.col_offset]
                if indent.strip() or statement_starts[start] > 1:
                    continue  # shares its line with other code
                tail = lines[end - 1][stmt.end_col_offset :].strip()
                if tail and not tail.startswith("#"):
                    continue
                text = inspect.cleandoc(stmt.value.value)
                if "\r" in text:
                    continue
                comments = [
                    indent + ("# " + line if line.strip() else "#")
                    for line in text.split("\n")
                ]
                if tail:
                    comments.append(indent + tail)
                for comment in comments:
                    added[("comment", comment.strip())] += 1
                if tail:
                    added[("comment", tail)] -= 1  # it was already a comment
                edits.append((start, end, [c.rstrip() + "\n" for c in comments]))
    if not edits:
        return source, 0, Counter()
    new_lines = list(lines)
    for start, end, replacement in sorted(edits, reverse=True):
        new_lines[start - 1 : end] = replacement
    new_source = "".join(new_lines)
    try:
        compile(new_source, "<notes>", "exec")
    except SyntaxError:
        return source, 0, Counter()
    # the comment tokens really are the ones we claim to add
    before = Counter(
        ("comment", t.string)
        for t in tokenize.generate_tokens(io.StringIO(source).readline)
        if t.type == tokenize.COMMENT
    )
    after = Counter(
        ("comment", t.string)
        for t in tokenize.generate_tokens(io.StringIO(new_source).readline)
        if t.type == tokenize.COMMENT
    )
    added = +added
    if after - before != added:
        return source, 0, Counter()
    return new_source, len(edits), added
