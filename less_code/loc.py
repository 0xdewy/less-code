"""Code-LOC metric: non-blank, non-comment, non-docstring lines.

This is the authoritative reduction metric. Excluding comments/docstrings
prevents gaming the number by stripping docs instead of shrinking code.

Counting is *canonical* wherever a reduction is measured (roadmap B1): the
source is piped through `ruff format` / `prettier` / `rustfmt` at a fixed
width first, so joining statements onto one line or minifying cannot buy a
smaller number. When the formatter is missing the raw count is used and the
`Loc.formatted` flag says so. `tokens` and `ast_nodes` are secondary metrics
that stay honest even if a rewrite only merges lines.
"""

from __future__ import annotations

import ast
import io
import re
import shutil
import subprocess
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PRINT_WIDTH = 88


@dataclass(frozen=True)
class Loc:
    code: int
    comment: int
    blank: int
    formatted: bool = False
    tokens: int = 0
    ast_nodes: int = 0

    @property
    def total(self) -> int:
        return self.code + self.comment + self.blank


TOKEN_RE = re.compile(r"\w+|[^\s\w]")


def count_tokens(source: str) -> int:
    """Rough language-agnostic token count (identifiers + punctuation)."""
    return len(TOKEN_RE.findall(source))


def count_ast_nodes(source: str, lang: str) -> int:
    """Python: real AST node count. Other languages: token count as a proxy."""
    if lang == "python":
        try:
            return sum(1 for _ in ast.walk(ast.parse(source)))
        except SyntaxError:
            return 0
    return count_tokens(source)


FORMAT_CMDS = {
    "python": [
        "ruff",
        "format",
        "--line-length",
        str(PRINT_WIDTH),
        "--stdin-filename",
        "x.py",
        "-",
    ],
    "javascript": [
        "prettier",
        "--stdin-filepath",
        "x.js",
        "--print-width",
        str(PRINT_WIDTH),
    ],
    "typescript": [
        "prettier",
        "--stdin-filepath",
        "x.ts",
        "--print-width",
        str(PRINT_WIDTH),
    ],
    "rust": [
        "rustfmt",
        "--emit",
        "stdout",
        "--edition",
        "2024",
        "--config",
        f"max_width={PRINT_WIDTH}",
    ],
}
PRETTIER_NPX = ["npx", "--yes", "prettier@3.9.6"]


def format_command(lang: str) -> list[str] | None:
    cmd = FORMAT_CMDS.get(lang)
    if not cmd:
        return None
    if shutil.which(cmd[0]) is not None:
        return cmd
    if lang in ("javascript", "typescript") and shutil.which("npx") is not None:
        filename = "x.ts" if lang == "typescript" else "x.js"
        return PRETTIER_NPX + [
            "--stdin-filepath",
            filename,
            "--print-width",
            str(PRINT_WIDTH),
        ]
    return None


def formatter_available(lang: str) -> bool:
    return format_command(lang) is not None


@lru_cache(maxsize=512)
def canonical_format(source: str, lang: str) -> tuple[str, bool]:
    """(text, formatted). Falls back to the raw text when the tool is absent
    or rejects the input (a syntactically broken candidate, typically)."""
    cmd = format_command(lang)
    if not cmd:
        return source, False
    try:
        proc = subprocess.run(
            cmd, input=source, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return source, False
    if proc.returncode != 0:
        return source, False
    return proc.stdout, True


def count_python(source: str) -> Loc:
    """Count via tokenize: docstring = triple-quoted STRING starting a statement."""
    doc_lines: set[int] = set()
    code_lines: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        tokens = []
    boundary = {
        None,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NL,
        tokenize.SEMI,
    }
    prev_significant = None
    skipped_types = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
    for tok in tokens:
        if tok.type in skipped_types:
            continue
        if tok.type == tokenize.NEWLINE:
            prev_significant = tok.type
            continue
        if (
            tok.type == tokenize.STRING
            and ('"""' in tok.string or "'''" in tok.string)
            and prev_significant in boundary
        ):
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


def count_source(source: str, lang: str, format_first: bool = False) -> Loc:
    """Count code-LOC. `format_first` canonicalises the text first (B1)."""
    text, formatted = (
        canonical_format(source, lang) if format_first else (source, False)
    )
    base = LANG_COUNTERS[lang](text)
    return Loc(
        base.code,
        base.comment,
        base.blank,
        formatted=formatted,
        tokens=count_tokens(text),
        ast_nodes=count_ast_nodes(text, lang),
    )


def measure(source: str, lang: str) -> Loc:
    """The reduction metric: canonical-format count wherever it is available."""
    return count_source(source, lang, format_first=True)


def count_file(path: Path, lang: str, format_first: bool = False) -> Loc:
    return count_source(
        path.read_text(encoding="utf-8", errors="replace"), lang, format_first
    )


def count_tree(files: list[Path], lang: str, format_first: bool = False) -> Loc:
    code = comment = blank = tokens = nodes = 0
    formatted = True
    for f in files:
        loc = count_file(f, lang, format_first)
        code += loc.code
        comment += loc.comment
        blank += loc.blank
        tokens += loc.tokens
        nodes += loc.ast_nodes
        formatted = formatted and loc.formatted
    return Loc(
        code,
        comment,
        blank,
        formatted=formatted and format_first,
        tokens=tokens,
        ast_nodes=nodes,
    )
