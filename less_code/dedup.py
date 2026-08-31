"""C2b — near-duplicate symbol groups, the missing proposal granularity.

Iterations 07/08 measured two granularities and found both wrong for the
biggest documented opportunity in the fixtures:

* a **whole file** is one all-or-nothing bet a small model cannot land;
* a **single symbol** is a bet the model *can* land, but it structurally
  cannot see that `csv_escape_row` and `csv_escape_row_owned` are the same
  function twice, or that six methods repeat the same SKU validation.

Both fixtures' FIXTURE.md list cross-symbol copy-paste as the largest embedded
opportunity. This module finds those groups so `llm_reduce` can propose *one*
shared `_helper` plus minimal rewrites of every member, as a single multi-file
candidate through the same verify gate.

Detection is deliberately dumb and language-agnostic: strip comments, fold
string literals, replace every non-keyword identifier with `ID`, and compare
the token sequences with `difflib.SequenceMatcher`. Names are the thing that
differs between copy-pasted blocks, so erasing them is the whole trick.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .hunks import symbol_spans
from .loc import measure

# Keywords stay verbatim; everything else identifier-shaped becomes `ID`.
KEYWORDS = {
    "python": {
        "def", "class", "return", "if", "elif", "else", "for", "while", "in",
        "not", "and", "or", "is", "None", "True", "False", "import", "from",
        "as", "with", "try", "except", "finally", "raise", "yield", "lambda",
        "pass", "break", "continue", "global", "nonlocal", "assert", "del",
        "async", "await", "self",
    },
    "javascript": {
        "function", "return", "if", "else", "for", "while", "of", "in", "const",
        "let", "var", "class", "new", "this", "typeof", "instanceof", "null",
        "undefined", "true", "false", "throw", "try", "catch", "finally",
        "switch", "case", "default", "break", "continue", "export", "import",
        "async", "await", "yield", "delete", "void",
    },
    "rust": {
        "fn", "let", "mut", "return", "if", "else", "for", "in", "while",
        "loop", "match", "struct", "enum", "impl", "trait", "pub", "use",
        "mod", "self", "Self", "crate", "super", "as", "where", "move", "ref",
        "const", "static", "type", "dyn", "unsafe", "async", "await", "true",
        "false", "Some", "None", "Ok", "Err",
    },
}
KEYWORDS["typescript"] = KEYWORDS["javascript"]

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d[\d_.]*|[^\sA-Za-z0-9_]")
_PY_COMMENT = re.compile(r"#[^\n]*")
_C_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def normalize_tokens(text: str, lang: str) -> list[str]:
    """Token sequence with names, literals and comments erased."""
    text = (_PY_COMMENT if lang == "python" else _C_COMMENT).sub(" ", _STRING.sub(' "S" ', text))
    keywords = KEYWORDS.get(lang, set())
    return [
        t if (not t[:1].isalpha() and t[:1] != "_") or t in keywords else "ID"
        for t in _TOKEN.findall(text)
    ]


def similarity(a: list[str], b: list[str]) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


@dataclass
class Unit:
    """One dedup candidate: a top-level symbol, or a python method."""

    path: Path
    key: str  # `name` or `Class.method`
    name: str
    start: int  # 0-based line, inclusive
    end: int  # 0-based line, exclusive
    text: str
    lang: str
    indent: int = 0

    @property
    def loc(self) -> int:
        return measure(self.text, self.lang).code

    @property
    def label(self) -> str:
        return f"{self.path.name}::{self.key}"


@dataclass
class DupGroup:
    members: list[Unit] = field(default_factory=list)
    similarity: float = 0.0

    @property
    def loc(self) -> int:
        return sum(m.loc for m in self.members)

    @property
    def files(self) -> list[Path]:
        out: list[Path] = []
        for m in self.members:
            if m.path not in out:
                out.append(m.path)
        return out

    def label(self) -> str:
        return f"[{', '.join(m.label for m in self.members)}] sim={self.similarity:.2f}"


def dedup_units(path: Path, source: str, lang: str) -> list[Unit]:
    """Every unit worth comparing: top-level symbols, plus python methods.

    Python classes are decomposed because the py fixture's copy-paste lives
    *inside* 186-line classes — comparing whole classes would find nothing.
    """
    lines = source.splitlines()
    units: list[Unit] = []
    if lang == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = _decorated_start(node)
                units.append(Unit(path, node.name, node.name, start, node.end_lineno,
                                  "\n".join(lines[start:node.end_lineno]), lang, 0))
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        start = _decorated_start(child)
                        units.append(Unit(
                            path, f"{node.name}.{child.name}", child.name, start,
                            child.end_lineno, "\n".join(lines[start:child.end_lineno]),
                            lang, child.col_offset,
                        ))
        return units
    seen: dict[str, int] = {}
    for start, end, name in sorted(symbol_spans(source, lang)):
        if not name or end <= start:
            continue
        seen[name] = seen.get(name, 0) + 1
        key = name if seen[name] == 1 else f"{name}#{seen[name]}"
        units.append(Unit(path, key, name, start, end, "\n".join(lines[start:end]), lang, 0))
    return units


def _decorated_start(node) -> int:
    start = node.lineno
    for dec in getattr(node, "decorator_list", []):
        start = min(start, dec.lineno)
    return start - 1


def find_duplicate_groups(
    sources: dict[Path, str],
    lang: str,
    threshold: float = 0.6,
    min_loc: int = 4,
    min_tokens: int = 20,
    max_members: int = 5,
) -> list[DupGroup]:
    """Group near-duplicate units project-wide, biggest total LOC first.

    Members may live in different files. `threshold` is a `SequenceMatcher`
    ratio over the normalized token sequences (0.6 ≈ "same shape, different
    names"); the gate, not this number, decides whether a merge is correct.
    """
    units: list[Unit] = []
    tokens: list[list[str]] = []
    for path, text in sorted(sources.items()):
        for unit in dedup_units(path, text, lang):
            toks = normalize_tokens(unit.text, lang)
            if unit.loc < min_loc or len(toks) < min_tokens:
                continue
            units.append(unit)
            tokens.append(toks)

    pairs: dict[tuple[int, int], float] = {}
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            # cheap length prefilter: a ratio below the threshold is impossible
            # when the two sequences differ that much in length
            li, lj = len(tokens[i]), len(tokens[j])
            if 2 * min(li, lj) < threshold * (li + lj):
                continue
            ratio = similarity(tokens[i], tokens[j])
            if ratio >= threshold:
                pairs[(i, j)] = ratio

    def ratio_of(a: int, b: int) -> float:
        return pairs.get((a, b), pairs.get((b, a), 0.0))

    # Greedy tight groups, NOT a transitive closure. Union-find chains
    # everything vaguely loop-shaped into one blob and then the size cap throws
    # away the real twins: on the rust fixture that hid `csv_escape_row` /
    # `_owned` (ratio 0.99) behind five merely-similar neighbours. So: seed on
    # the highest-ratio pair still unused, then admit a unit only if it is
    # above threshold against *every* member already in the group.
    used: set[int] = set()
    groups: list[DupGroup] = []
    for (i, j), _r in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if i in used or j in used:
            continue
        members = [i, j]
        for k in range(len(units)):
            if len(members) >= max_members:
                break
            if k in used or k in members:
                continue
            if all(ratio_of(k, m) >= threshold for m in members):
                members.append(k)
        used.update(members)
        ratios = [
            ratio_of(a, b)
            for x, a in enumerate(members) for b in members[x + 1:]
        ]
        groups.append(DupGroup(
            members=[units[m] for m in sorted(members)],
            similarity=round(sum(ratios) / len(ratios), 3) if ratios else 0.0,
        ))
    groups.sort(key=lambda g: -g.loc)
    return groups
