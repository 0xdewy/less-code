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
import json
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


def _js_remove_dead_exports(
    files: list[Path],
    all_project_files: list[Path],
    knip_names: dict[str, set[str]] | None = None,
    root: Path | None = None,
) -> StaticResult:
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

    knip_extra: dict[Path, set[str]] = {}
    for rel, names in (knip_names or {}).items():
        for path in exported:
            if root is not None and path == (root / rel).resolve():
                knip_extra[path] = names
            elif str(path).endswith(rel):
                knip_extra.setdefault(path, set()).update(names)
    for path, names in exported.items():
        source = path.read_text(encoding="utf-8", errors="replace")
        # drop export statements whose symbol is imported nowhere else. Spans
        # come from the brace matcher, not a regex: a `[^}]*` body pattern
        # truncates at the first nested block and leaves the file unparsable.
        for name in sorted((names | knip_extra.get(path, set())) - imported_names):
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


# ---- D2: JS knip + dead internal (non-exported) symbols ---------------------

JS_TOP_VAR = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=", re.MULTILINE)


def _knip_unused_exports(root: Path, timeout: int = 240) -> tuple[dict[str, set[str]], str]:
    """`npx knip --reporter json` -> {relative file: {unused export names}}.

    Best effort by design: knip needs a package.json and, the first time, a
    network fetch, so an offline machine simply gets `({}, note)` and the
    hand-rolled scan below carries the pass. Only the `exports` issue class is
    consumed — knip also reports unused *files*, and acting on that would
    delete each fixture's `tests_hidden/` suite, which is the one thing the
    bench must never touch.
    """
    if shutil.which("npx") is None or not (root / "package.json").is_file():
        return {}, "knip: skipped (no npx or no package.json)"
    try:
        proc = subprocess.run(
            ["npx", "--yes", "knip", "--reporter", "json"],
            cwd=root, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return {}, "knip: unavailable (offline?)"
    payload = proc.stdout.strip()
    if not payload.startswith("{"):
        return {}, f"knip: no usable output (exit {proc.returncode})"
    try:
        data = json.loads(payload)
    except ValueError:
        return {}, "knip: output was not JSON"
    found: dict[str, set[str]] = {}
    for issue in data.get("issues", []):
        names = {e["name"] for e in issue.get("exports", []) if e.get("name")}
        if names:
            found.setdefault(issue.get("file", ""), set()).update(names)
    return found, f"knip: {sum(len(v) for v in found.values())} unused exports"


def _js_statement_end(source: str, from_index: int) -> int:
    """End offset (exclusive) of a top-level `const x = ...` statement."""
    depth, i = 0, from_index
    while i < len(source):
        ch = source[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == ";":
            return i + 1
        elif depth == 0 and ch == "\n":
            return i + 1
        i += 1
    return len(source)


def _js_internal_spans(source: str) -> list[tuple[str, int, int]]:
    """(name, start_line, end_line_exclusive) for NON-exported top-level
    functions, classes and const/let/var bindings."""
    from .hunks import symbol_spans

    lines = source.splitlines()
    spans: list[tuple[str, int, int]] = []
    for start, end, name in symbol_spans(source, "javascript"):
        if not name or lines[start].lstrip().startswith("export"):
            continue
        spans.append((name, start, end))
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def line_of(offset: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= offset:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    for m in JS_TOP_VAR.finditer(source):
        if m.group(0).startswith("export"):
            continue
        end = _js_statement_end(source, m.end())
        spans.append((m.group(1), line_of(m.start()), line_of(max(end - 1, m.start())) + 1))
    return spans


def _js_remove_dead_internal(
    sources: dict[Path, str], all_texts: dict[Path, str]
) -> StaticResult:
    """Delete non-exported top-level symbols nothing in the project mentions.

    Same reference-scan shape as the rust dead-`pub` pass: one `\bname\b` hit
    anywhere outside the symbol's own span — any file, comments and strings
    included — keeps it. Whatever survives still faces the frozen suite.
    """
    result = StaticResult()
    for path, text in sources.items():
        spans = _js_internal_spans(text)
        if not spans:
            continue
        lines = text.splitlines()
        dead: list[tuple[str, int, int]] = []
        for name, start, end in spans:
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            own = "\n".join(lines[:start] + lines[end:])
            elsewhere = pattern.search(own) or any(
                pattern.search(other) for q, other in all_texts.items() if q != path
            )
            if not elsewhere:
                dead.append((name, start, end))
        if not dead:
            continue
        kept = list(lines)
        for name, start, end in sorted(dead, key=lambda t: -t[1]):
            del kept[start:end]
            result.notes.append(f"{path.name}: dropped unreferenced internal {name}")
        new_text = "\n".join(kept) + "\n"
        before = measure(text, "javascript").code
        after = measure(new_text, "javascript").code
        if after < before:
            result.changed_files[str(path)] = new_text
            result.loc_removed += before - after
    return result


def _js_static(
    root: Path,
    source_files: list[Path],
    all_project_files: list[Path],
    runner=None,
) -> StaticResult:
    """Dead exports (own scan + knip when it runs) then dead internals.

    Narrowed like the python pass when a runner is available: if the combined
    edit set goes red, the export-only subset is tried on its own.
    """
    knip_names, knip_note = _knip_unused_exports(root)
    exports = _js_remove_dead_exports(source_files, all_project_files, knip_names, root)
    exports.notes.insert(0, knip_note)

    originals = {p: p.read_text(encoding="utf-8", errors="replace") for p in source_files}
    after_exports = {
        p: exports.changed_files.get(str(p), text) for p, text in originals.items()
    }
    all_texts = {
        p: after_exports.get(p, p.read_text(encoding="utf-8", errors="replace"))
        for p in all_project_files
    }
    internal = _js_remove_dead_internal(after_exports, all_texts)
    combined = StaticResult(
        changed_files={**exports.changed_files, **internal.changed_files},
        loc_removed=exports.loc_removed + internal.loc_removed,
        notes=exports.notes + internal.notes,
    )
    if runner is None or not combined.changed_files or not internal.changed_files:
        return combined

    def _write(changed: dict[str, str]) -> None:
        for path, text in originals.items():
            path.write_text(changed.get(str(path), text), encoding="utf-8")

    def _green(changed: dict[str, str]) -> bool:
        _write(changed)
        ok = runner(root, "javascript").ok
        _write({})
        return ok

    if _green(combined.changed_files):
        return combined
    combined.notes.append("js dead-internal removal reverted by the gate")
    return StaticResult(
        changed_files=dict(exports.changed_files),
        loc_removed=exports.loc_removed,
        notes=combined.notes,
    )



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


# Iteration 09: clippy's *default* set is a correctness set, the same way ruff's
# is (iteration 08 found ruff's default removed 0 lines and its opt-in families
# removed 10). `pedantic` and `complexity` are the two groups whose fixes
# actually collapse lines — `needless_range_loop`, `manual_let_else`,
# `redundant_closure_for_method_calls`, `explicit_iter_loop`. They are OFF by
# default because they are opinionated, which is exactly why this is its own
# gated step rather than part of `_rust_cargo_fix`.
CLIPPY_PEDANTIC = [
    "cargo", "clippy", "--fix", "--allow-dirty", "--allow-staged",
    "--allow-no-vcs", "--quiet", "--",
    "-W", "clippy::pedantic", "-W", "clippy::complexity",
]


def _rust_clippy_pedantic(root: Path, source_files: list[Path], runner=None) -> StaticResult:
    """`cargo clippy --fix` at the pedantic/complexity tier, gated on its own.

    `cargo clippy --fix` mutates the tree, unlike `ruff --fix` over stdin, so
    this snapshots every source file, runs it, reads the result back and
    **restores the tree** — the edits leave here as `changed_files`, inside the
    pipeline's revert set, exactly like `_rust_remove_dead_pub`. When a runner
    is available the tier is additionally tested on its own and dropped whole
    if it goes red, so an opinionated fix cannot take the safe tier down with
    it.
    """
    result = StaticResult()
    if shutil.which("cargo") is None:
        result.notes.append("cargo not found; skipping clippy pedantic tier")
        return result
    originals = {p: p.read_text(encoding="utf-8", errors="replace") for p in source_files}

    def _restore() -> None:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    try:
        proc = subprocess.run(CLIPPY_PEDANTIC, cwd=root, capture_output=True, text=True, timeout=1200)
    except (OSError, subprocess.SubprocessError) as exc:
        result.notes.append(f"clippy pedantic tier failed to run: {exc}")
        _restore()
        return result
    result.notes.append(f"clippy pedantic --fix: exit {proc.returncode}")
    changed = {
        str(path): path.read_text(encoding="utf-8", errors="replace")
        for path in source_files
        if path.read_text(encoding="utf-8", errors="replace") != originals[path]
    }
    if not changed:
        _restore()
        return result
    removed = sum(
        measure(originals[Path(p)], "rust").code - measure(t, "rust").code
        for p, t in changed.items()
    )
    if removed <= 0:
        # measured on the rs fixture: the pedantic tier is a net **+19 lines**
        # there, because a good part of what it fixes is adding `#[must_use]`
        # and `# Panics` doc sections. Correct code, more lines — and this pass
        # only ever accepts states where code-LOC goes down.
        _restore()
        result.notes.append(
            f"clippy pedantic tier dropped: it ADDS {-removed} code lines"
        )
        return result
    green = runner(root, "rust").ok if runner is not None else True
    _restore()
    if not green:
        result.notes.append("clippy pedantic tier reverted by the gate")
        return result
    result.changed_files = changed
    result.loc_removed = max(0, removed)
    result.notes.append(
        f"clippy pedantic tier kept: {len(changed)} file(s), {removed} code lines"
    )
    return result


# ---- D1: ruff --fix tiers and unreachable code ------------------------------


# ruff's default set (F, E4/E7/E9) is a *correctness* set and removes almost
# no lines. These families are the LOC-reducing ones — superfluous else after
# return, redundant comprehension wrappers, `class C(object)`, `if x: return
# True` — and every one of them still goes through the frozen suite.
RUFF_SELECT = "F,E4,E7,E9,RET,SIM,C4,PIE,UP,PLR1,PLW,PERF,FURB"


def _ruff_fix(text: str, filename: str, unsafe: bool = False) -> str:
    """`ruff check --fix` over stdin. Returns the input unchanged on any doubt.

    stdin mode keeps the pass *pure*: the fixed source comes back on stdout, so
    nothing is written to disk outside the pipeline's revert set (`cargo fix`,
    by contrast, mutates the tree behind the gate's back). The safe tier is
    ruff's own definition of a fix that cannot change behaviour; the unsafe
    tier (dropping an unused local binding, for instance) is a separate,
    separately-gated layer — see `_python_static`.
    """
    if shutil.which("ruff") is None:
        return text
    cmd = ["ruff", "check", "--select", RUFF_SELECT, "--fix", "--quiet"]
    if unsafe:
        cmd.append("--unsafe-fixes")
    cmd += ["--stdin-filename", filename, "-"]
    try:
        proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return text
    out = proc.stdout
    if not out.strip():
        return text  # ruff errored out; stdout is not a source file
    try:
        ast.parse(out)
    except SyntaxError:
        return text
    return out


_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _unreachable_line_spans(source: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of statements that can never run.

    Anything after a `return`/`raise` (and after `continue`/`break`) inside a
    function body is dead. Only whole, line-aligned statements are taken; a
    statement sharing a line with the terminator (`return 1; x = 2`) is left
    alone, the same discipline the rule library uses.

    Caveat, recorded rather than hidden: deleting `return; x = 1` also deletes
    the binding that made `x` a *local* name, so a function that reads `x`
    before the return would change from `UnboundLocalError` to a global read.
    That is why this layer is separately test-gated.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list) or len(block) < 2:
                    continue
                if not all(isinstance(st, ast.stmt) for st in block):
                    continue
                for index, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, _TERMINATORS):
                        dead = block[index + 1 :]
                        start = min(st.lineno for st in dead)
                        if start <= stmt.end_lineno:
                            break  # shares a line with the terminator
                        spans.append((start, max(st.end_lineno for st in dead)))
                        break
    return spans


def _python_drop_unreachable(source: str) -> str:
    spans = _unreachable_line_spans(source)
    if not spans:
        return source
    lines = source.splitlines(keepends=True)
    drop: set[int] = set()
    for start, end in spans:
        drop.update(range(start - 1, min(end, len(lines))))
    new_source = "".join(line for i, line in enumerate(lines) if i not in drop)
    try:
        ast.parse(new_source)
    except SyntaxError:  # pragma: no cover - defensive
        return source
    return new_source


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


def _python_layers(
    dead: StaticResult, source_files: list[Path]
) -> list[tuple[str, object]]:
    """The ordered, independently gateable static layers for Python.

    Each layer is `(name, transform)` where `transform(sources) -> (sources,
    notes)` maps the *current* proposed text of every file to the next. They
    compose left to right; on a red suite `_python_static` re-applies them one
    at a time and keeps only the ones that stay green, so a misfiring layer
    costs its own yield and nothing else.
    """
    from .rules import RULES

    def dead_layer(sources: dict[Path, str]):
        out = dict(sources)
        for path in sources:
            if str(path) in dead.changed_files:
                out[path] = dead.changed_files[str(path)]
        return out, list(dead.notes)

    def rule_layer(rule: str):
        def run(sources: dict[Path, str]):
            changed, notes = _python_rules(sources, {rule})
            out = {p: changed.get(str(p), t) for p, t in sources.items()}
            return out, notes
        return run

    def unreachable_layer(sources: dict[Path, str]):
        out, notes = {}, []
        for path, text in sources.items():
            new_text = _python_drop_unreachable(text)
            out[path] = new_text
            if new_text != text:
                notes.append(f"{path.name}: removed unreachable code after return/raise")
        return out, notes

    def ruff_layer(unsafe: bool):
        tier = "unsafe" if unsafe else "safe"

        def run(sources: dict[Path, str]):
            out, notes = {}, []
            for path, text in sources.items():
                new_text = _ruff_fix(text, path.name, unsafe=unsafe)
                out[path] = new_text
                if new_text != text:
                    notes.append(f"{path.name}: ruff --fix ({tier} tier)")
            return out, notes
        return run

    layers: list[tuple[str, object]] = [("dead-code", dead_layer)]
    layers += [(f"rule {rule}", rule_layer(rule)) for rule in RULES]
    layers.append(("unreachable-code", unreachable_layer))
    # ruff last: the layers above create new unused imports and locals for it
    layers.append(("ruff-safe", ruff_layer(False)))
    layers.append(("ruff-unsafe", ruff_layer(True)))
    return layers


def _python_static(
    root: Path,
    source_files: list[Path],
    all_project_files: list[Path],
    runner=None,
) -> StaticResult:
    """Dead code, the C4 rule library, unreachable-code removal and both
    `ruff --fix` tiers (roadmap D1), narrowed layer by layer under the gate.

    Without a runner this returns one combined edit set and the pipeline's
    own gate decides all-or-nothing. With a runner, a red suite is narrowed:
    each layer is re-applied on top of the last green state and kept only if
    the suite stays green. A reverted layer is a result, not a failure — it is
    reported in the notes.
    """
    originals = {p: p.read_text(encoding="utf-8", errors="replace") for p in source_files}
    dead = _python_remove_dead(source_files, all_project_files)
    layers = _python_layers(dead, source_files)

    current = dict(originals)
    notes: list[str] = []
    for _name, transform in layers:
        current, layer_notes = transform(current)
        notes += layer_notes
    combined_changes = {
        str(p): text for p, text in current.items() if text != originals[p]
    }
    combined = StaticResult(
        changed_files=combined_changes, loc_removed=dead.loc_removed, notes=notes,
    )
    if runner is None or not combined.changed_files:
        return combined

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
    accepted_text = dict(originals)
    for name, transform in layers:
        trial_text, _layer_notes = transform(dict(accepted_text))
        if trial_text == accepted_text:
            continue
        trial = {str(p): t for p, t in trial_text.items() if t != originals[p]}
        if _green(trial):
            accepted_text = trial_text
        elif name == "dead-code":
            combined.notes.append("dead-code removal reverted by the gate")
        else:
            combined.notes.append(f"{name} reverted by the gate")
    accepted = {str(p): t for p, t in accepted_text.items() if t != originals[p]}
    return StaticResult(changed_files=accepted, notes=combined.notes)


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
        return _js_static(root, source_files, all_project_files, runner)
    if lang == "rust":
        result = _rust_cargo_fix(root)
        pedantic = _rust_clippy_pedantic(root, source_files, runner)
        # compose: the dead-`pub` scan must read the pedantic text, or the two
        # edit sets would be alternative versions of the same file and the
        # later `update()` would silently drop one of them.
        pre_pedantic = {
            path_str: Path(path_str).read_text(encoding="utf-8", errors="replace")
            for path_str in pedantic.changed_files
        }
        for path_str, text in pedantic.changed_files.items():
            Path(path_str).write_text(text, encoding="utf-8")
        dead = _rust_remove_dead_pub(source_files, all_project_files)
        # …and put the tree back: this pass hands its edits to the pipeline as
        # `changed_files` rather than leaving them on disk behind the gate.
        for path_str, text in pre_pedantic.items():
            Path(path_str).write_text(text, encoding="utf-8")
        result.changed_files.update(pedantic.changed_files)
        result.changed_files.update(dead.changed_files)
        result.loc_removed += pedantic.loc_removed + dead.loc_removed
        result.notes += pedantic.notes + dead.notes
        return result
    return StaticResult(notes=[f"no static pass for {lang}"])
