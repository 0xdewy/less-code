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
    max_members: int = 3,
    tight_threshold: float = 0.9,
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
            # a third member has to be a *tight* twin, not merely similar:
            # iteration 09's 3-member rust groups produced token diffs twice
            # the size of the pair's, and none of them compiled. A pair is the
            # smallest merge that is still a merge.
            floor = threshold if len(members) < 2 else tight_threshold
            if all(ratio_of(k, m) >= floor for m in members):
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
    # rank by *expected* yield, not raw size: a 0.99-similar pair is a
    # template to fill in, a 0.7-similar pair is a redesign, and iteration 09
    # measured that the models can only do the first
    groups.sort(key=lambda g: -(g.loc * g.similarity))
    return groups


# ---- the mechanical merge template -----------------------------------------

# Like `_TOKEN` but string literals stay whole, because a differing error
# message is exactly the kind of variation the helper has to parameterize.
_RAW_TOKEN = re.compile(
    r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\''
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r"|[A-Za-z_][A-Za-z0-9_]*|\d[\d_.]*|[^\sA-Za-z0-9_]"
)


def raw_tokens(text: str, lang: str) -> list[str]:
    """Tokens with comments stripped but names and literals kept."""
    stripped = (_PY_COMMENT if lang == "python" else _C_COMMENT).sub(" ", text)
    return _RAW_TOKEN.findall(stripped)


def token_diff_slots(group, lang: str, max_slots: int = 8) -> list[tuple[str, ...]]:
    """What actually differs between the members, computed not guessed.

    Iteration 09's dedup proposals asked the model to *design* the merge:
    invent a helper, decide what to abstract, and get every pinned error
    message right, in one reply, in Rust. 0% landed. The diff between two
    copy-pasted definitions is a mechanical fact, so it should be computed and
    handed over: each differing token run is one thing the helper has to take
    as a parameter, and the model's job shrinks to filling in a template.

    Returns one tuple per differing run, member-aligned, first member first.
    Runs that are only the definition's own name are dropped: renaming is not
    parameterization.
    """
    if len(group.members) < 2:
        return []
    seqs = [raw_tokens(m.text, lang) for m in group.members]
    names = {m.name for m in group.members}
    slots: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    base = seqs[0]
    for other, member in zip(seqs[1:], group.members[1:]):
        matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            left = " ".join(base[i1:i2]).strip()
            right = " ".join(other[j1:j2]).strip()
            if not left and not right:
                continue
            if left in names and right in names:
                continue
            slot = (left or "(nothing)", right or "(nothing)")
            if slot in seen:
                continue
            seen.add(slot)
            slots.append(slot)
            if len(slots) >= max_slots:
                return slots
    return slots


# ---- the mechanical merge ----------------------------------------------------

_HEADER_PLAIN = re.compile(r"^(\s*)def\s+([A-Za-z_]\w*)\s*\((.*)\)\s*:\s*(#.*)?$")


def _spanned_tokens(text: str) -> list[tuple[str, int, int]]:
    """Raw tokens with char spans, comment interiors skipped. A `#` outside a
    string token starts a comment; nothing to its EOL is code."""
    import bisect

    out: list[tuple[str, int, int]] = []
    line_starts = [0]
    for pos, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(pos + 1)
    skip_line = -1
    for m in _RAW_TOKEN.finditer(text):
        line = bisect.bisect_right(line_starts, m.start()) - 1
        if line == skip_line:
            continue
        if m.group() == "#":
            skip_line = line
            continue
        skip_line = -1
        out.append((m.group(), m.start(), m.end()))
    return out


def _arg_names(params_text: str) -> list[str]:
    """`a, b=1, *args, **kw` -> ['a', 'b', '*args', '**kw'] in call order."""
    names: list[str] = []
    for part in params_text.split(","):
        part = part.strip()
        if not part:
            continue
        part = part.split(":")[0].split("=")[0].strip()
        if part:
            names.append(part)
    return names


def mechanical_merge(group) -> tuple[dict[Path, str], str] | None:
    """Build the shared-helper merge for a pair mechanically, no model.

    The LLM was asked to *design* this merge and 0 of 2 landed on click; the
    diff between two copy-pasted definitions is a computed fact. Member 1's
    body becomes the helper VERBATIM (comments survive; only the differing
    token runs become parameters), every member becomes its signature plus a
    one-line call. Declines anything it cannot prove slot-shaped: multi-line
    header, decorators, async, differing docstrings or parameter lists, runs
    that touch the header, span multiple lines, are huge, or a member name
    appearing in the other's body (recursion/aliasing).

    Returns {(file path): new text} plus a note, or None when the pair is not
    slot-mergeable. The caller still gates the result on the frozen suite.
    """
    members = group.members
    if len(members) != 2 or members[0].lang != "python":
        return None
    a, b = members
    if a.indent != b.indent:
        return None
    match_a = _HEADER_PLAIN.match(a.text.split("\n", 1)[0])
    match_b = _HEADER_PLAIN.match(b.text.split("\n", 1)[0])
    if not match_a or not match_b:
        return None
    ind_a, params_a = match_a.group(1), match_a.group(3)
    ind_b, params_b = match_b.group(1), match_b.group(3)
    if params_a.replace(" ", "") != params_b.replace(" ", ""):
        return None
    try:
        fn_a = ast.parse(a.text).body[0]
        fn_b = ast.parse(b.text).body[0]
    except (SyntaxError, IndexError):
        return None
    for fn in (fn_a, fn_b):
        if not isinstance(fn, ast.FunctionDef) or fn.decorator_list:
            return None
    if ast.get_docstring(fn_a, clean=False) != ast.get_docstring(fn_b, clean=False):
        return None

    spans_a = _spanned_tokens(a.text)
    spans_b = _spanned_tokens(b.text)
    # a member's name appearing in the other's body is recursion/aliasing;
    # token-level (a one-letter name like `a` is a substring of `total`)
    toks_a = {t for t, _s, _e in spans_a}
    toks_b = {t for t, _s, _e in spans_b}
    if b.name in toks_a or a.name in toks_b:
        return None
    matcher = difflib.SequenceMatcher(
        None, [t for t, _s, _e in spans_a], [t for t, _s, _e in spans_b], autojunk=False
    )
    header_end = a.text.index("\n")  # single-line header, verified by the regex
    slots: list[tuple[int, int, str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        a_start = spans_a[i1][1] if i2 > i1 else spans_a[i1][1]
        a_end = spans_a[i2 - 1][2] if i2 > i1 else spans_a[i1][1]
        b_start = spans_b[j1][1] if j2 > j1 else spans_b[j1][1]
        b_end = spans_b[j2 - 1][2] if j2 > j1 else spans_b[j1][1]
        a_run = a.text[a_start:a_end]
        b_run = b.text[b_start:b_end]
        if not a_run.strip() and not b_run.strip():
            continue
        names = {a.name, b.name}
        if a_run.strip() in names and b_run.strip() in names:
            continue  # the def names: renaming, not parameterization
        if a_start <= header_end or b_start <= header_end:
            return None  # run touches the header (e.g. a default value)
        if "\n" in a_run or "\n" in b_run or len(a_run) > 60 or len(b_run) > 60:
            return None
        slots.append((a_start, a_end, a_run, b_run))
    if not slots:
        return None
    used_names = {t for t, _s, _e in spans_a} | {t for t, _s, _e in spans_b}
    slot_params = []
    for i in range(len(slots)):
        name = f"_slot{i}"
        while name in used_names:
            name = "_" + name
        slot_params.append(name)

    # helper = member a's body verbatim, dedented, runs -> parameters
    body = a.text[header_end + 1:]
    for (start, end, _run, _b), param in sorted(
        zip(slots, slot_params), key=lambda x: x[0][0], reverse=True
    ):
        body = body[:start - header_end - 1] + param + body[end - header_end - 1:]
    body = "\n".join(
        line[len(ind_a):] if line.strip() else line
        for line in body.split("\n")
    )
    helper_name = f"_{a.name}_shared"
    while helper_name in used_names:
        helper_name = "_" + helper_name
    params_full = ", ".join(
        [p.strip() for p in params_a.split(",") if p.strip()] + slot_params
    )
    helper = f"def {helper_name}({params_full}):\n{body}".rstrip() + "\n"

    doc = ast.get_docstring(fn_a, clean=False)
    doc_line = f'{ind_a}    """{doc}"""\n' if doc else ""

    def member_rewrite(m: "Unit", args_text: str) -> str:
        head = f"{ind_a}def {m.name}({params_a.strip()}):"
        call = f"{ind_a}    return {helper_name}({args_text})"
        return head + "\n" + (doc_line if doc else "") + call

    arg_names = _arg_names(params_a)
    args_a = ", ".join(arg_names + [run for _s, _e, run, _b in slots])
    args_b = ", ".join(arg_names + [run for _s, _e, _a, run in slots])

    out: dict[Path, str] = {}
    # splice bottom-up: a same-file pair would otherwise see the second
    # member's stored line span shifted by the first splice
    for m, args in sorted(((a, args_a), (b, args_b)), key=lambda p: -p[0].start):
        lines = (
            m.path.read_text(encoding="utf-8", errors="replace").splitlines()
            if m.path not in out
            else out[m.path].splitlines()
        )
        lines[m.start:m.end] = member_rewrite(m, args).splitlines()
        if m is a:
            lines[m.start:m.start] = helper.splitlines()
        out[m.path] = "\n".join(lines) + "\n"
    note = (
        f"mechanical merge {a.key}+{b.key} -> {helper_name} "
        f"({len(slots)} slot(s))"
    )
    for text in out.values():
        try:
            compile(text, "<mech>", "exec")
        except SyntaxError:
            return None
    return out, note
