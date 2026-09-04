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


def documentation_layout(source: str, lang: str) -> tuple[tuple[str, str], ...]:
    """Documentation as an order-independent multiset of (kind, text).

    Matches the contract in CRITERIA.md / README.md: the exact multiset of
    comments and docstrings must be preserved. Position and indentation are
    not part of the gate - prettier, rustfmt, or any reformatting pass can
    move them without triggering a reversion.
    """
    if lang == "python":
        items = [
            ("comment", token.string)
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
                    items.append(("docstring", value))
        return tuple(items)

    import tree_sitter as ts

    if lang in ("javascript", "typescript"):
        import tree_sitter_javascript as grammar
    elif lang == "rust":
        import tree_sitter_rust as grammar
    else:
        return ()
    data = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(data).root_node
    items: list[tuple[str, str]] = []

    def visit(node) -> None:
        if "comment" in node.type:
            text = data[node.start_byte : node.end_byte].decode()
            items.append(("comment", text))
        else:
            for child in node.children:
                visit(child)

    visit(root)
    return tuple(items)
