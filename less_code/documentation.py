"""Extract source documentation for lossless reduction gates."""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter


def documentation(source: str, lang: str) -> Counter[tuple[str, str]]:
    """Return an order-independent multiset of comments and docstrings."""
    if lang == "python":
        found = Counter(
            ("comment", token.string)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        )
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return found
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                value = ast.get_docstring(node, clean=False)
                if value is not None:
                    found[("docstring", value)] += 1
        return found

    import tree_sitter as ts

    if lang in ("javascript", "typescript"):
        import tree_sitter_javascript as grammar
    elif lang == "rust":
        import tree_sitter_rust as grammar
    else:
        return Counter()
    data = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(data).root_node
    found: Counter[tuple[str, str]] = Counter()

    def visit(node) -> None:
        if "comment" in node.type:
            found[("comment", data[node.start_byte : node.end_byte].decode())] += 1
        else:
            for child in node.children:
                visit(child)

    visit(root)
    return found


def documentation_layout(source: str, lang: str) -> tuple[tuple[str, str, int], ...]:
    """Documentation in source order, including its indentation.

    Text-only multisets let a reducer detach a comment and paste it elsewhere.
    Indentation plus order is deliberately stable under formatting while
    rejecting movement across scopes in the common case.
    """
    if lang == "python":
        items = [
            (token.start[0], "comment", token.string, token.start[1])
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        ]
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return tuple(items)
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                value = ast.get_docstring(node, clean=False)
                if value is not None:
                    expr = node.body[0]
                    items.append((expr.lineno, "docstring", value, expr.col_offset))
        return tuple((kind, text, column) for _, kind, text, column in sorted(items))

    import tree_sitter as ts

    if lang in ("javascript", "typescript"):
        import tree_sitter_javascript as grammar
    elif lang == "rust":
        import tree_sitter_rust as grammar
    else:
        return ()
    data = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(data).root_node
    items: list[tuple[int, str, str, int]] = []

    def visit(node) -> None:
        if "comment" in node.type:
            text = data[node.start_byte : node.end_byte].decode()
            items.append((node.start_byte, "comment", text, node.start_point.column))
        else:
            for child in node.children:
                visit(child)

    visit(root)
    return tuple((kind, text, column) for _, kind, text, column in sorted(items))
