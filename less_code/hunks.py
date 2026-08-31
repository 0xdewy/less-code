"""C1 — hunk decomposition and delta debugging.

A 7B whole-file rewrite is usually right about most of the file and wrong
about one function. The all-or-nothing gate threw the whole thing away; this
module splits the rewrite into hunks aligned to *top-level symbol
boundaries* (python: ast; js/rust: the brace-matching scan also used by
grpo/build_dataset.py) so the verifier can accept the good ones.

  decompose(original, candidate, lang) -> [Hunk]   # aligned diff
  apply(original, hunks)               -> str      # splice a subset back in
  search(...)                          -> best passing subset (ddmin/greedy)

Symbol boundaries — not textual diff hunks — are the unit on purpose: a
subset built that way is always a syntactically whole program, so the parse
pre-gate stays meaningful and an accepted subset is reviewable.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .loc import measure

PREAMBLE = "<preamble>"


@dataclass
class Hunk:
    """One symbol-aligned edit: replace original[start:end) with new_lines."""

    key: str
    name: str
    kind: str  # replace | delete | insert
    start: int  # line index into the original, inclusive
    end: int  # line index into the original, exclusive
    new_lines: list[str] = field(default_factory=list)
    old_lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.new_lines)

    def loc_delta(self, lang: str) -> int:
        """Code lines saved by this hunk (negative when it adds lines)."""
        before = measure("\n".join(self.old_lines), lang).code if self.old_lines else 0
        after = measure(self.text, lang).code if self.new_lines else 0
        return before - after


# ---- top-level symbol spans -------------------------------------------------


def _python_spans(source: str) -> list[tuple[int, int, str]]:
    """(start, end_exclusive, name) for top-level defs/classes, 0-based."""
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
    """Brace-matched top-level blocks (the build_dataset.py approach)."""
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for m in pattern.finditer(source):
        if m.start() < pos:
            continue  # nested inside a block already taken
        name = next((v for v in m.groupdict().values() if v), "").strip()
        brace = source.find("{", m.start())
        semi = source.find(";", m.start())
        if brace == -1 or (semi != -1 and semi < brace):
            # a declaration without a body (`pub struct S;`, `pub mod x;`)
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
    # char offsets -> line indices
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


def _segments(source: str, lang: str) -> list[tuple[str, str, int, int]]:
    """[(key, name, start, end_exclusive)] covering the whole file.

    Symbols get `sym:<name>`; the text between them gets `gap:<previous>` so
    imports and module constants are their own reviewable hunk.
    """
    lines = source.splitlines()
    spans = sorted(symbol_spans(source, lang))
    segs: list[tuple[str, str, int, int]] = []
    pos = 0
    prev = PREAMBLE
    seen: dict[str, int] = {}

    def uniq(key: str) -> str:
        seen[key] = seen.get(key, 0) + 1
        return key if seen[key] == 1 else f"{key}#{seen[key]}"

    for start, end, name in spans:
        if start > pos:
            segs.append((uniq(f"gap:{prev}"), prev, pos, start))
        segs.append((uniq(f"sym:{name}"), name, start, end))
        prev = name
        pos = end
    if pos < len(lines):
        segs.append((uniq(f"gap:{prev}"), prev, pos, len(lines)))
    return segs


# ---- decomposition ----------------------------------------------------------


def decompose(original: str, candidate: str, lang: str) -> list[Hunk]:
    """Split the whole-file rewrite into independently verifiable hunks."""
    o_lines = original.splitlines()
    c_lines = candidate.splitlines()
    o_segs = _segments(original, lang)
    c_segs = _segments(candidate, lang)
    c_by_key = {key: (start, end) for key, _n, start, end in c_segs}
    o_keys = {key for key, _n, _s, _e in o_segs}

    hunks: list[Hunk] = []
    for key, name, start, end in o_segs:
        old = o_lines[start:end]
        if key in c_by_key:
            c_start, c_end = c_by_key[key]
            new = c_lines[c_start:c_end]
            if new != old:
                hunks.append(Hunk(key, name, "replace", start, end, new, old))
        else:
            hunks.append(Hunk(key, name, "delete", start, end, [], old))

    # symbols only the candidate has (new `_helpers`): insert them after the
    # last preceding segment that both files share.
    anchor = 0
    for key, name, start, end in c_segs:
        if key in o_keys:
            for o_key, _n, _s, o_end in o_segs:
                if o_key == key:
                    anchor = o_end
                    break
            continue
        if key.startswith("gap:"):
            continue
        hunks.append(Hunk(key, name, "insert", anchor, anchor, c_lines[start:end], []))
    return hunks


def apply(original: str, hunks: list[Hunk]) -> str:
    """Splice a subset of hunks back into the original source."""
    lines = original.splitlines()
    for hunk in sorted(hunks, key=lambda h: (h.start, h.end), reverse=True):
        lines[hunk.start : hunk.end] = list(hunk.new_lines)
    return "\n".join(lines)


# ---- delta debugging over hunk subsets --------------------------------------


@dataclass
class HunkSearchResult:
    source: str
    loc: int
    accepted: list[Hunk] = field(default_factory=list)
    rejected: list[Hunk] = field(default_factory=list)
    verifications: int = 0
    outcomes: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        ok = ",".join(h.key for h in self.accepted) or "-"
        bad = ",".join(f"{h.key}={self.outcomes.get(h.key, '?')}" for h in self.rejected) or "-"
        return f"hunks {len(self.accepted)}/{len(self.accepted) + len(self.rejected)} accepted=[{ok}] rejected=[{bad}] verifications={self.verifications}"


def search(
    path: Path,
    lang: str,
    original: str,
    hunks: list[Hunk],
    api_before: dict[str, str],
    loc_before: int,
    run_tests_fn,
    root: Path,
    max_verifications: int = 60,
) -> HunkSearchResult:
    """Find the largest subset of `hunks` that passes the verify gate.

    Divide and conquer (ddmin-flavoured): a failing set is split in half, each
    half solved recursively, and the two solutions re-joined when they survive
    together. Then every still-rejected hunk is retried on top of the accepted
    state — the "wrong in one function" case ends up costing ~2N verifications,
    and the test-result cache makes repeats free.
    """
    from .llm_reduce import verify_candidate

    result = HunkSearchResult(source=original, loc=loc_before)
    memo: dict[tuple[str, ...], tuple[bool, str, int]] = {}

    def try_subset(subset: list[Hunk]) -> tuple[bool, str, int]:
        if not subset:
            return False, "empty", loc_before
        key = tuple(sorted(h.key for h in subset))
        if key in memo:
            return memo[key]
        if result.verifications >= max_verifications:
            return False, "budget-exhausted", loc_before
        result.verifications += 1
        candidate = apply(original, subset)
        outcome, loc_after = verify_candidate(
            path, lang, candidate, api_before, loc_before, run_tests_fn, root
        )
        memo[key] = (outcome == "accepted", outcome, loc_after)
        return memo[key]

    def largest_passing(subset: list[Hunk]) -> list[Hunk]:
        if not subset:
            return []
        ok, outcome, _loc = try_subset(subset)
        if ok:
            return subset
        if len(subset) == 1:
            result.outcomes[subset[0].key] = outcome.split(":")[0]
            return []
        mid = len(subset) // 2
        left = largest_passing(subset[:mid])
        right = largest_passing(subset[mid:])
        if left and right:
            joined = left + right
            ok, _outcome, _loc = try_subset(joined)
            if ok:
                return joined
            return left if len(left) >= len(right) else right
        return left or right

    accepted = largest_passing(list(hunks))

    # retry every rejected hunk on top of the accepted state
    rejected = [h for h in hunks if h not in accepted]
    for hunk in list(rejected):
        ok, outcome, _loc = try_subset(accepted + [hunk])
        if ok:
            accepted = accepted + [hunk]
            rejected.remove(hunk)
            result.outcomes[hunk.key] = "accepted"
        else:
            result.outcomes[hunk.key] = outcome.split(":")[0]

    if accepted:
        source = apply(original, accepted)
        loc = measure(source, lang).code
        if loc < loc_before:
            result.source, result.loc = source, loc
    result.accepted = accepted
    result.rejected = rejected
    for hunk in accepted:
        result.outcomes[hunk.key] = "accepted"
    return result
