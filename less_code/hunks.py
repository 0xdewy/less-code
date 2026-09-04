"""Symbol-span extractor used by the dead-code passes.

`symbol_spans(source, lang)` returns `(start_line, end_line_exclusive, name)`
for every top-level symbol in the file: Python defs/classes via AST and
JavaScript declarations via a brace-matching scan.

Originally this file also held hunk decomposition + delta-debugging for the
salvage loop on rejected LLM rewrites. The free-form LLM pass is gone; the
file keeps the symbol-boundary utility because the static pass needs it.
"""

from __future__ import annotations

import ast
import re

# ---- top-level symbol spans -------------------------------------------------


def _python_spans(source: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            for dec in getattr(node, "decorator_list", []):
                start = min(start, dec.lineno)
            spans.append((start - 1, node.end_lineno, node.name))
    return spans


JS_DECL = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function\s*\*?\s+(?P<fn>\w+)|class\s+(?P<cls>\w+))",
    re.MULTILINE,
)
RUST_DECL = re.compile(
    r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(?:fn\s+(?P<fn>\w+)|struct\s+(?P<st>\w+)|enum\s+(?P<en>\w+)|trait\s+(?P<tr>\w+)|"
    r"mod\s+(?P<md>\w+)|impl(?:<[^>]*>)?\s+(?P<im>[\w:<>, ]+))",
    re.MULTILINE,
)


def _brace_spans(source: str, pattern: re.Pattern) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for m in pattern.finditer(source):
        if m.start() < pos:
            continue
        name = next((v for v in m.groupdict().values() if v), "").strip()
        brace = source.find("{", m.start())
        semi = source.find(";", m.start())
        if brace == -1 or (semi != -1 and semi < brace):
            end = semi if semi != -1 else m.end()
            spans.append((m.start(), end + 1, name))
            pos = end + 1
            continue
        depth, i = 0, brace
        while i < len(source):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    spans.append((m.start(), i + 1, name))
                    pos = i + 1
                    break
            i += 1
    starts = [0]
    for ch in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(ch))

    def line_of(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= offset:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    out = []
    for start, end, name in spans:
        out.append((line_of(start), line_of(max(end - 1, start)) + 1, name))
    return out


def symbol_spans(source: str, lang: str) -> list[tuple[int, int, str]]:
    if lang == "python":
        return _python_spans(source)
    if lang in ("javascript", "typescript"):
        return _brace_spans(source, JS_DECL)
    if lang == "rust":
        return _brace_spans(source, RUST_DECL)
    return []
