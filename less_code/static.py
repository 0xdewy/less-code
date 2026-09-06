"""Layer 1 — static, provably-safe or test-verified reductions.

Python: own AST dead-code removal (unused module-level defs/classes and
imports) plus the deterministic rewrite rules of `less_code.rules` (roadmap
C4) — references scanned project-wide including tests, then gated by the
frozen test suite anyway. Rust: deterministic rule library (`less_code.rust_rules`).
Clippy/dead-pub passes are out of scope — no Rust corpus project
warrants them; revisit when one does.
JS: dead internal symbols via project-wide reference scan. External tools are optional
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


def _names_referenced_outside(
    root_files: list[Path], definitions: dict[Path, set[str]]
) -> dict[str, bool]:
    referenced: dict[str, bool] = {
        name: False for names in definitions.values() for name in names
    }
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
                if re.match(r"^\s*(def|class)\s+$", prefix) or prefix.rstrip().endswith(
                    ("def", "class")
                ):
                    continue  # the definition site itself
                referenced[name] = True
                break
    return referenced


#: top-level functions here are invoked BY NAME from CI configs and tooling,
#: not imported — "unreferenced by the tests" does not make them dead. Found
#: the hard way: the layer deleted noxfile sessions (lint, release_build)
#: from pypa/packaging. These files are still scanned for references.
_ENTRY_POINT_FILES = frozenset(
    {
        "noxfile.py",
        "setup.py",
        "asv.conf.py",
        "tasks.py",
        "manage.py",
    }
)


def _python_remove_dead(
    files: list[Path], all_project_files: list[Path]
) -> StaticResult:
    result = StaticResult()
    files = [
        p
        for p in files
        if p.name not in _ENTRY_POINT_FILES and (p.parent / "__init__.py").is_file()
    ]
    definitions: dict[Path, set[str]] = {}
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    for path, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        defs = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            annotated = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            if args.vararg:
                annotated.append(args.vararg)
            if args.kwarg:
                annotated.append(args.kwarg)
            definition_can_execute = (
                node.decorator_list
                or args.defaults
                or any(default is not None for default in args.kw_defaults)
                or node.returns is not None
                or any(arg.annotation is not None for arg in annotated)
                or getattr(node, "type_params", ())
            )
            # A repository scan cannot prove external callers or safely erase
            # decorators, defaults, annotations, class bodies, or type bounds:
            # all can execute at import time. Only plain private functions are
            # deletion candidates; dunders remain protocol surface.
            if (
                not definition_can_execute
                and node.name.startswith("_")
                and not node.name.startswith("__")
            ):
                defs.add(node.name)
        if defs:
            definitions[path] = defs
    if not definitions:
        return result
    referenced = _names_referenced_outside(all_project_files, definitions)
    for path, defs in definitions.items():
        tree = ast.parse(sources[path])
        dead = [
            node
            for node in tree.body
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in defs
                and not referenced[node.name]
            )
        ]
        if not dead:
            continue
        text = sources[path]
        lines = text.splitlines()
        kill: set[int] = set()  # 0-based indices
        for node in dead:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            kill.update(range(start, node.end_lineno))
            # the blank lines directly above the item go with it, so removal
            # never leaves a doubled blank line
            i = start - 1
            while i >= 0 and not lines[i].strip():
                kill.add(i)
                i -= 1
            # a comment block tightly attached above those blanks is the dead
            # item's own header and goes too — but only when a blank line (or
            # the file top) bounds it from above, else it is a trailing
            # comment of the previous statement and stays
            j = i
            while j >= 0 and lines[j].lstrip().startswith("#"):
                j -= 1
            if j < i and (j < 0 or not lines[j].strip()):
                kill.update(range(j + 1, i + 1))
        kept_lines = [l for n, l in enumerate(lines) if n not in kill]
        while kept_lines and not kept_lines[-1].strip():
            kept_lines.pop()
        new_source = "\n".join(kept_lines)
        if text.endswith("\n"):
            new_source += "\n"
        before = measure(sources[path], "python").code
        after = measure(new_source, "python").code
        if after < before:
            result.changed_files[str(path)] = new_source
            result.loc_removed += before - after
            dropped = sorted(n for n in defs if not referenced[n])
            result.notes.append(f"{path.name}: removed {dropped}")
    return result


# ---- D2: JS dead internal (non-exported) symbols ----------------------------


def _project_js_lint_fix(source: str, path: Path, root: Path) -> str:
    """Apply an installed project's own XO/ESLint fixes without leaving writes."""
    commands = [
        (root / "node_modules/.bin/xo", ["--fix"]),
        (root / "node_modules/.bin/eslint", ["--fix"]),
    ]
    tool = next(((exe, args) for exe, args in commands if exe.is_file()), None)
    if tool is None:
        return source
    original = path.read_text(encoding="utf-8", errors="replace")
    try:
        path.write_text(source, encoding="utf-8")
        proc = subprocess.run(
            [str(tool[0]), *tool[1], str(path.relative_to(root))],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        fixed = path.read_text(encoding="utf-8", errors="replace")
        return fixed if proc.returncode == 0 and fixed.strip() else source
    except (OSError, subprocess.SubprocessError, ValueError):
        return source
    finally:
        path.write_text(original, encoding="utf-8")


def _js_internal_spans(source: str) -> list[tuple[str, int, int]]:
    """(name, start_line, end_line_exclusive) for NON-exported top-level
    functions. Class definitions and variable initializers can execute code at
    module load, so a reference scan alone cannot prove that deleting them is safe."""
    from .hunks import symbol_spans

    lines = source.splitlines()
    spans: list[tuple[str, int, int]] = []
    for start, end, name in symbol_spans(source, "javascript"):
        declaration = lines[start].lstrip()
        if (
            not name
            or declaration.startswith("export")
            or not declaration.startswith(("function ", "function*", "async function "))
        ):
            continue
        spans.append((name, start, end))
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
    """Dead internal functions plus explicit, reviewable rewrites.

    Export deletion is intentionally absent: repository scans cannot prove an
    exported library symbol has no external callers, and the original-API gate
    would reject it anyway. The pipeline isolates failing file candidates.
    """
    originals = {
        p: p.read_text(encoding="utf-8", errors="replace") for p in source_files
    }
    all_texts = {
        p: originals.get(p, p.read_text(encoding="utf-8", errors="replace"))
        for p in all_project_files
    }
    internal = _js_remove_dead_internal(originals, all_texts)

    # ---- JS rule library (if-ladder -> array, accumulator -> .map/.every,
    # else-after-terminator, boolean-chain-collapse). Pure functions over text;
    # applied per source file, gated by the runner just like the dead-code pass.
    from .js_rules import apply_rules as _js_apply_rules

    js_rule_changes: dict[str, str] = {}
    js_rule_notes: list[str] = []
    for path, text in originals.items():
        new_text, names = _js_apply_rules(text)
        if names and new_text != text:
            new_text = _project_js_lint_fix(new_text, path, root)
            js_rule_changes[str(path)] = new_text
            js_rule_notes.append(f"{path.name}: js-rules {names}")
    js_rules = StaticResult(
        changed_files=js_rule_changes,
        loc_removed=sum(
            measure(orig, "javascript").code - measure(t, "javascript").code
            for p, t in js_rule_changes.items()
            for orig in [originals[Path(p)]]
        ),
        notes=js_rule_notes,
    )

    combined = StaticResult(
        changed_files={
            **internal.changed_files,
            **js_rules.changed_files,
        },
        loc_removed=internal.loc_removed + js_rules.loc_removed,
        notes=internal.notes + js_rules.notes,
    )
    return combined


# ---- D1: ruff --fix tiers and unreachable code ------------------------------


# ruff's default set (F, E4/E7/E9) is a *correctness* set and removes almost
# no lines. These families are the LOC-reducing ones — superfluous else after
# return, redundant comprehension wrappers, `class C(object)`, `if x: return
# True` — and every one of them still goes through the frozen suite.
# Import removal is not generally semantic-preserving (`try: import readline`
# is a feature probe), and `UP` may raise a project's minimum Python version.
# Keep only syntax-local families that do not delete arbitrary expressions or
# substitute newer runtime APIs.
RUFF_SELECT = "RET,SIM,C4,PIE,PLR1,PLR5501,PLW0120,PERF,F"


def _ruff_fix(text: str, filename: str, unsafe: bool = False) -> str:
    """`ruff check --fix` over stdin. Returns the input unchanged on any doubt.

    stdin mode keeps the pass *pure*: the fixed source comes back on stdout, so
    nothing is written to disk outside the pipeline's revert set. The safe tier is
    ruff's own definition of a fix that cannot change behaviour; the unsafe
    tier (dropping an unused local binding, for instance) is a separate,
    separately-gated layer — see `_python_static`.
    """
    if shutil.which("ruff") is None:
        return text
    cmd = [
        "ruff",
        "check",
        "--select",
        RUFF_SELECT,
        "--ignore",
        "F401,SIM103",
        "--fix",
        "--quiet",
    ]
    # F401 can erase import side effects/module attributes. SIM103 can leak
    # a user-defined rich comparison result instead of returning a bool.
    if unsafe:
        cmd.append("--unsafe-fixes")
    cmd += ["--stdin-filename", filename, "-"]
    try:
        proc = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
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
            for field_name in ("body", "orelse", "finalbody"):
                block = getattr(node, field_name, None)
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


def _python_layers(dead: StaticResult) -> list[tuple[str, object]]:
    """The ordered, independently gateable static layers for Python.

    Each layer is `(name, transform)` where `transform(sources) -> (sources,
    notes)` maps the *current* proposed text of every file to the next. They
    compose left to right; on a red suite `_python_static` re-applies them one
    at a time and keeps only the ones that stay green, so a misfiring layer
    costs its own yield and nothing else.
    """

    def dead_layer(sources: dict[Path, str]):
        out = dict(sources)
        for path in sources:
            if str(path) in dead.changed_files:
                out[path] = dead.changed_files[str(path)]
        return out, list(dead.notes)

    def unreachable_layer(sources: dict[Path, str]):
        out, notes = {}, []
        for path, text in sources.items():
            new_text = _python_drop_unreachable(text)
            out[path] = new_text
            if new_text != text:
                notes.append(
                    f"{path.name}: removed unreachable code after return/raise"
                )
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
    layers.append(("unreachable-code", unreachable_layer))
    # ruff last: the layers above create new unused imports and locals for it.
    # Pass --unsafe-fixes: rules like RET504 (unnecessary assignment before
    # return) are reported-but-not-fixed by the safe tier. The unsafe tier is
    # a strict superset of the safe tier's fix set, so one subprocess is
    # enough; the gate stack rejects anything that breaks tests or docs.
    layers.append(("ruff-unsafe", ruff_layer(True)))
    return layers


def _python_static(
    root: Path,
    source_files: list[Path],
    all_project_files: list[Path],
    runner=None,
) -> StaticResult:
    """Dead code, the C4 rule library, unreachable-code removal and safe
    `ruff --fix` rules (roadmap D1), narrowed layer by layer under the gate.

    Without a runner this returns one combined edit set and the pipeline's
    own gate decides all-or-nothing. With a runner, a red suite is narrowed:
    each layer is re-applied on top of the last green state and kept only if
    the suite stays green. A reverted layer is a result, not a failure — it is
    reported in the notes.
    """
    originals = {
        p: p.read_text(encoding="utf-8", errors="replace") for p in source_files
    }
    dead = _python_remove_dead(source_files, all_project_files)
    layers = _python_layers(dead)

    current = dict(originals)
    notes: list[str] = []
    for _name, transform in layers:
        current, layer_notes = transform(current)
        notes += layer_notes
    combined_changes = {
        str(p): text for p, text in current.items() if text != originals[p]
    }
    combined = StaticResult(
        changed_files=combined_changes,
        loc_removed=dead.loc_removed,
        notes=notes,
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
        from .rust_rules import apply_rules as _rust_apply_rules

        changes: dict[str, str] = {}
        notes: list[str] = []
        removed = 0
        for path in source_files:
            text = path.read_text(encoding="utf-8", errors="replace")
            new_text, names = _rust_apply_rules(text)
            if names and new_text != text:
                changes[str(path)] = new_text
                notes.append(f"{path.name}: rust-rules {names}")
                removed += measure(text, "rust").code - measure(new_text, "rust").code
        return StaticResult(changes, removed, notes)
    return StaticResult(notes=[f"no static pass for {lang}"])
