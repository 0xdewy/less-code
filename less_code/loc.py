"""Code-LOC metric: non-blank, non-comment, non-docstring lines.

This is the authoritative reduction metric. Excluding comments/docstrings
prevents gaming the number by stripping docs instead of shrinking code.
"""

from __future__ import annotations

import io
import tokenize
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Loc:
    code: int
    comment: int
    blank: int

    @property
    def total(self) -> int:
        return self.code + self.comment + self.blank


def count_python(source: str) -> Loc:
    """Count via tokenize: docstring = triple-quoted STRING starting a statement."""
    doc_lines: set[int] = set()
    code_lines: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        tokens = []
    boundary = {None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL, tokenize.SEMI}
    prev_significant = None
    skipped_types = {
        tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
        tokenize.ENDMARKER, tokenize.ENCODING,
    }
    for tok in tokens:
        if tok.type in skipped_types:
            continue
        if tok.type == tokenize.NEWLINE:
            prev_significant = tok.type
            continue
        if tok.type == tokenize.STRING and (
            '"""' in tok.string or "'''" in tok.string
        ) and prev_significant in boundary:
            doc_lines.update(range(tok.start[0], tok.end[0] + 1))
            prev_significant = tok.type
            continue
        code_lines.update(range(tok.start[0], tok.end[0] + 1))
        prev_significant = tok.type
    code = blank = comment = 0
    for n, line in enumerate(source.splitlines(), 1):
        s = line.strip()
        if not s:
            blank += 1
        elif n in code_lines:
            code += 1
        else:
            comment += 1
    return Loc(code, comment, blank)


def _count_c_like(source: str, line_comment: str) -> Loc:
    code = blank = comment = 0
    in_block = False
    for line in source.splitlines():
        s = line.strip()
        if not s:
            blank += 1
            continue
        if in_block:
            comment += 1
            if "*/" in s:
                in_block = False
                if s.split("*/", 1)[1].strip():
                    code += 1
                    comment -= 1
            continue
        if s.startswith(line_comment):
            comment += 1
        elif s.startswith("/*"):
            comment += 1
            if "*/" not in s[2:]:
                in_block = True
            elif s.split("*/", 1)[1].strip():
                code += 1
                comment -= 1
        else:
            code += 1
    return Loc(code, comment, blank)


def count_js(source: str) -> Loc:
    return _count_c_like(source, "//")


def count_rust(source: str) -> Loc:
    return _count_c_like(source, "//")


LANG_COUNTERS = {
    "python": count_python,
    "javascript": count_js,
    "typescript": count_js,
    "rust": count_rust,
}


def count_source(source: str, lang: str) -> Loc:
    return LANG_COUNTERS[lang](source)


def count_file(path: Path, lang: str) -> Loc:
    return count_source(path.read_text(encoding="utf-8", errors="replace"), lang)


def count_tree(files: list[Path], lang: str) -> Loc:
    code = comment = blank = 0
    for f in files:
        loc = count_file(f, lang)
        code += loc.code
        comment += loc.comment
        blank += loc.blank
    return Loc(code, comment, blank)
