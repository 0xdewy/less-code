"""Magic trailing comma collapse: the source edit that un-pins a block.

A trailing comma before a closing bracket is semantically null in Python
EXCEPT when it is the only thing keeping a formatter from joining the
block, which is exactly the "magic" comma ruff format (like black) treats
as an instruction to keep the block exploded. Removing it lets the
canonical formatter collapse the block, so the edit is counted honestly by
the post-format metric — as its own gated layer, never inside the semantic
reduction figure.

Tokenizer-based by necessity: brackets inside strings and f-strings must
not open or close anything, and a regex over the raw text cannot tell a
trailing comma from a comma inside a literal.
"""

from __future__ import annotations

import io
import tokenize

_OPENERS = frozenset({"(", "[", "{"})
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_NON_LOGICAL = frozenset(
    {
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.COMMENT,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
)


def collapse_magic_commas(source: str) -> tuple[str, int]:
    """Remove trailing commas that pin a multi-line block open.

    Returns (new_source, commas_removed). Removals happen ONLY when:
      - the comma is the last non-whitespace, non-comment token on its line,
      - the matching closing bracket starts a later line,
      - nothing but whitespace sits between the comma and the line end
        (a trailing comment pins the comma; leave it alone).
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source, 0
    line_starts = [0]
    for line in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def offset(row: int, col: int) -> int:
        return line_starts[row - 1] + col

    opener_rows: list[int] = []
    last_logical = None
    deletions: list[tuple[int, int]] = []
    for tok in tokens:
        if tok.type in _NON_LOGICAL:
            continue
        if tok.type == tokenize.OP and tok.string in _CLOSERS and opener_rows:
            opener_rows.pop()
            if (
                last_logical is not None
                and last_logical.type == tokenize.OP
                and last_logical.string == ","
                and last_logical.start[0] < tok.start[0]
            ):
                comma_end = offset(*last_logical.end)
                line_end = line_starts[last_logical.end[0]]
                if source[comma_end:line_end].isspace():
                    deletions.append((offset(*last_logical.start), comma_end))
        elif tok.type == tokenize.OP and tok.string in _OPENERS:
            opener_rows.append(tok.start[0])
        last_logical = tok
    result = source
    for start, end in sorted(deletions, reverse=True):
        result = result[:start] + result[end:]
    return result, len(deletions)
