"""Layer 1 — static, provably-safe or test-verified reductions.

Python: own AST dead-code removal (unused module-level defs/classes and
imports) plus the deterministic rewrite rules of `less_code.rules` (roadmap
C4) — references scanned project-wide including tests, then gated by the
frozen test suite anyway. Rust: cargo fix + clippy --fix (compiler-verified) plus dead `pub` item
removal (roadmap D3), references scanned project-wide including tests.
JS: dead internal exports via import-graph scan. External tools are optional
enhancements; the tool stays hermetic without them.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .loc import measure


@dataclass
class StaticResult:
    changed_files: dict[str, str] = field(default_factory=dict)  # path -> new source
    loc_removed: int = 0
    notes: list[str] = field(default_factory=list)


def _names_referenced_outside(root_files: list[Path], definitions: dict[Path, set[str]]) -> dict[str, bool]:
    referenced: dict[str, bool] = {name: False for names in definitions.values() for name in names}
    import re as _re

    word = {name: _re.compile(rf"\b{_re.escape(name)}\b") for name in referenced}
    for path in root_files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, pattern in word.items():
            if referenced[name]:
                continue
            for m in pattern.finditer(source):
                line_start = source.rfind("\n", 0, m.start()) + 1
                prefix = source[line_start : m.start()]
                if re.match(r"^\s*(def|class)\s+$", prefix) or prefix.rstrip().endswith(("def", "class")):
                    continue  # the definition site itself
                referenced[name] = True
                break
    return referenced


def _python_remove_dead(files: list[Path], all_project_files: list[Path]) -> StaticResult:
    result = StaticResult()
    definitions: dict[Path, set[str]] = {}
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    for path, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        defs = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs.add(node.name)
        if defs:
            definitions[path] = defs
    if not definitions:
        return result
    referenced = _names_referenced_outside(all_project_files, definitions)
    for path, defs in definitions.items():
        tree = ast.parse(sources[path])
        kept = [
            node
            for node in tree.body
            if not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in defs
                and not referenced[node.name]
            )
        ]
        if len(kept) != len(tree.body):
            new_tree = ast.Module(body=kept, type_ignores=[])
            new_source = ast.unparse(new_tree)
            before = measure(sources[path], "python").code
            after = measure(new_source, "python").code
            if after < before:
                result.changed_files[str(path)] = new_source
                result.loc_removed += before - after
                result.notes.append(f"{path.name}: removed {sorted(defs - set(referenced) & defs)}")
    return result


def _js_remove_dead_exports(files: list[Path], all_project_files: list[Path]) -> StaticResult:
    result = StaticResult()
    exported: dict[Path, set[str]] = {}
    for path in files:
        source = path.read_text(encoding="utf-8", errors="replace")
        names = set()
        for m in re.finditer(r"export\s+(?:const|let|var|function|class|async function)\s+(\w+)", source):
            names.add(m.group(1))
        if names:
            exported[path] = names
    if not exported:
        return result
    imported_names: set[str] = set()
    for path in all_project_files:
        if path in exported:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"import\s+(?:\{([^}]*)\}|(\w+))", source):
            if m.group(1):
                imported_names.update(n.strip().split(" as ")[0] for n in m.group(1).split(",") if n.strip())
            elif m.group(2):
                imported_names.add(m.group(2))
        for m in re.finditer(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", source):
            pass
    from .hunks import symbol_spans

    for path, names in exported.items():
        source = path.read_text(encoding="utf-8", errors="replace")
        # drop export statements whose symbol is imported nowhere else. Spans
        # come from the brace matcher, not a regex: a `[^}]*` body pattern
        # truncates at the first nested block and leaves the file unparsable.
        for name in sorted(names - imported_names):
            lines = source.splitlines()
            span = next((sp for sp in symbol_spans(source, "javascript") if sp[2] == name), None)
            if span is not None:
                del lines[span[0] : span[1]]
                new_source = "\n".join(lines) + "\n"
            else:
                pattern = re.compile(
                    rf"^[ \t]*(?:export\s+)?(?:const|let|var)\s+{re.escape(name)}\s*=[^\n]*\n?",
                    re.MULTILINE,
                )
                new_source = pattern.sub("", source)
            if new_source != source:
                before = measure(source, "javascript").code
                after = measure(new_source, "javascript").code
                if after < before:
                    result.notes.append(f"{path.name}: dropped unused export {name}")
                    source = new_source
        final = measure(source, "javascript").code
        original = measure(path.read_text(encoding="utf-8", errors="replace"), "javascript").code
        if final < original:
            result.changed_files[str(path)] = source
            result.loc_removed += original - final
    return result



# ---- rust dead `pub` items (roadmap D3) ------------------------------------

RUST_PUB_ITEM = re.compile(
    r"^pub(?:\((?:crate|super|in\s+[^)]*)\))?\s+"
    r'(?:unsafe\s+|async\s+|extern\s+"[^"]*"\s+)*'
    r"(?:fn\s+(?P<fn>\w+)|struct\s+(?P<st>\w+)|enum\s+(?P<en>\w+)|"
    r"trait\s+(?P<tr>\w+)|const\s+(?P<co>\w+)|static\s+(?P<sa>\w+)|"
    r"type\s+(?P<ty>\w+))\b",
    re.MULTILINE,
)
_ATTACHED_PREFIX = ("///", "//!", "#[", "#!", "//")


def _rust_item_end(source: str, from_index: int) -> int:
    """End offset (exclusive) of the item starting at `from_index`.

    Scans for the first `{` or `;` that is not nested inside `(`/`[`/`<`-free
    delimiters — `pub fn f() -> [u8; 4] { .. }` has a `;` inside brackets that
    a naive `find(";")` would take for the end of a declaration.
    """
    depth = 0
    i = from_index
    while i < len(source):
        ch = source[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and ch == ";":
            return i + 1
        elif depth == 0 and ch == "{":
            brace = 0
            while i < len(source):
                if source[i] == "{":
                    brace += 1
                elif source[i] == "}":
                    brace -= 1
                    if brace == 0:
                        return i + 1
                i += 1
            return -1
        i += 1
    return -1


def _rust_attached_start(source: str, item_start: int) -> int:
    """Walk back over the doc comments and attributes attached to an item."""
    start = source.rfind("\n", 0, item_start) + 1  # first column of the item's line
    while start > 0:
        prev_start = source.rfind("\n", 0, start - 1) + 1
        line = source[prev_start : start - 1].strip()
        if line.startswith(_ATTACHED_PREFIX) or line.endswith(")]"):
            start = prev_start
        else:
            break
    return start


def _rust_top_level_pub_items(source: str) -> list[tuple[str, int, int]]:
    """(name, start_offset, end_offset) for column-0 `pub` items with a body."""
    items: list[tuple[str, int, int]] = []
    pos = 0
    for m in RUST_PUB_ITEM.finditer(source):
        if m.start() < pos:
            continue  # nested inside an item already consumed
        name = next((v for v in m.groupdict().values() if v), "")
        end = _rust_item_end(source, m.end())
        if end == -1:
            continue
        pos = end
        items.append((name, _rust_attached_start(source, m.start()), end))
    return items


def _rust_remove_dead_pub(source_files: list[Path], all_project_files: list[Path]) -> StaticResult:
    """Delete `pub` items nothing in the project — tests included — mentions.

    Conservative by construction: a single `\bname\b` hit anywhere outside the
    item's own span (any file, comments included) keeps the item. Everything
    that survives the scan still has to pass the frozen suite in the pipeline,
    which reverts the whole static edit set on red.
    """
    result = StaticResult()
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in source_files}
    others = {
        p: p.read_text(encoding="utf-8", errors="replace")
        for p in all_project_files
        if p not in sources
    }
    candidates: dict[Path, list[tuple[str, int, int]]] = {
        path: _rust_top_level_pub_items(text) for path, text in sources.items()
    }
    for path, items in candidates.items():
        keep_spans = []
        for name, start, end in items:
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            elsewhere = any(
                pattern.search(text) for text in others.values()
            ) or any(
                pattern.search(other if q != path else other[:start] + other[end:])
                for q, other in sources.items()
            )
            if elsewhere or not name:
                continue
            keep_spans.append((name, start, end))
        if not keep_spans:
            continue
        text = sources[path]
        for name, start, end in sorted(keep_spans, key=lambda t: -t[1]):
            tail = end
            while tail < len(text) and text[tail] in " \t":
                tail += 1
            while tail < len(text) and text[tail] == "\n":
                tail += 1
            text = text[:start] + text[tail:]
            result.notes.append(f"{path.name}: removed dead pub item {name}")
        before = measure(sources[path], "rust").code
        after = measure(text, "rust").code
        if after < before:
            result.changed_files[str(path)] = text
            result.loc_removed += before - after
    return result


def _rust_cargo_fix(root: Path) -> StaticResult:
    result = StaticResult()
    if shutil.which("cargo") is None:
        result.notes.append("cargo not found; skipping rust static pass")
        return result
    for cmd in (
        ["cargo", "fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs", "--quiet"],
        ["cargo", "clippy", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs", "--quiet"],
    ):
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=900)
        result.notes.append(f"{cmd[1]}: exit {proc.returncode}")
    return result


def _python_rules(sources: dict[Path, str], only: set[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Run the C4 rule library over already-proposed sources. Pure function."""
    from .rules import apply_rules

    changed: dict[str, str] = {}
    notes: list[str] = []
    for path, text in sources.items():
        new_text, applied = apply_rules(text, only)
        if applied and new_text != text:
            changed[str(path)] = new_text
            counts = {name: applied.count(name) for name in sorted(set(applied))}
            notes.append(f"{path.name}: rules {counts}")
    return changed, notes


def _python_static(
    root: Path,
    source_files: list[Path],
    all_project_files: list[Path],
    runner=None,
) -> StaticResult:
    """Dead-code removal, then the rule library, then (if a runner is given)
    per-rule gating.

    Without a runner this returns one combined edit set and the pipeline's
    own gate decides all-or-nothing. With a runner, a red suite is narrowed:
    dead-code-only first, then each rule on its own, keeping what stays green.
    A reverted rule is a result, not a failure — it is reported in the notes.
    """
    dead = _python_remove_dead(source_files, all_project_files)
    proposed = {
        p: dead.changed_files.get(str(p), p.read_text(encoding="utf-8", errors="replace"))
        for p in source_files
    }
    rule_changes, rule_notes = _python_rules(proposed)
    combined = StaticResult(
        changed_files={**dead.changed_files, **rule_changes},
        loc_removed=dead.loc_removed,
        notes=list(dead.notes) + rule_notes,
    )
    if runner is None or not combined.changed_files:
        return combined

    originals = {p: p.read_text(encoding="utf-8", errors="replace") for p in source_files}

    def _write(changed: dict[str, str]) -> None:
        for path, text in originals.items():
            path.write_text(changed.get(str(path), text), encoding="utf-8")

    def _green(changed: dict[str, str]) -> bool:
        _write(changed)
        ok = runner(root, "python").ok
        _write({})
        return ok

    if _green(combined.changed_files):
        return combined

    combined.notes.append("static+rules broke tests; narrowing rule by rule")
    accepted: dict[str, str] = {}
    if dead.changed_files and _green(dead.changed_files):
        accepted = dict(dead.changed_files)
    elif dead.changed_files:
        combined.notes.append("dead-code removal reverted by the gate")
    from .rules import RULES

    for rule in RULES:
        base = {p: accepted.get(str(p), originals[p]) for p in source_files}
        single, _ = _python_rules(base, {rule})
        if not single:
            continue
        trial = {**accepted, **single}
        if _green(trial):
            accepted = trial
        else:
            combined.notes.append(f"rule {rule} reverted by the gate")
    kept = StaticResult(changed_files=accepted, notes=combined.notes)
    return kept


def static_pass(
    root: Path,
    lang: str,
    source_files: list[Path],
    all_project_files: list[Path],
    runner=None,
) -> StaticResult:
    """`runner(root, lang) -> TestResult` enables per-rule narrowing on red.

    It is optional: when it is absent the caller's own gate (the pipeline)
    still decides whether the whole edit set is kept.
    """
    if lang == "python":
        return _python_static(root, source_files, all_project_files, runner)
    if lang in ("javascript", "typescript"):
        return _js_remove_dead_exports(source_files, all_project_files)
    if lang == "rust":
        result = _rust_cargo_fix(root)
        dead = _rust_remove_dead_pub(source_files, all_project_files)
        result.changed_files.update(dead.changed_files)
        result.loc_removed += dead.loc_removed
        result.notes += dead.notes
        return result
    return StaticResult(notes=[f"no static pass for {lang}"])
