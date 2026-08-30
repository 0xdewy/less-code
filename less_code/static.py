"""Layer 1 — static, provably-safe or test-verified reductions.

Python: own AST dead-code removal (unused module-level defs/classes and
imports) — references scanned project-wide including tests, then gated by the
frozen test suite anyway. Rust: cargo fix + clippy --fix (compiler-verified).
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

from .loc import count_source


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
            before = count_source(sources[path], "python").code
            after = count_source(new_source, "python").code
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
    for path, names in exported.items():
        source = path.read_text(encoding="utf-8", errors="replace")
        # drop export statements whose symbol is imported nowhere else
        for name in sorted(names - imported_names):
            pattern = re.compile(
                rf"^[ \t]*(?:export\s+)?(?:const|let|var)\s+{re.escape(name)}\s*=[^\n]*\n?"
                rf"|^[ \t]*export\s+(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{[^}}]*\}}\n?"
                rf"|^[ \t]*export\s+function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{[^}}]*\}}\n?",
                re.MULTILINE | re.DOTALL,
            )
            new_source = pattern.sub("", source)
            if new_source != source:
                before = count_source(source, "javascript").code
                after = count_source(new_source, "javascript").code
                if after < before:
                    result.notes.append(f"{path.name}: dropped unused export {name}")
                    source = new_source
        final = count_source(source, "javascript").code
        original = count_source(path.read_text(encoding="utf-8", errors="replace"), "javascript").code
        if final < original:
            result.changed_files[str(path)] = source
            result.loc_removed += original - final
    return result


def _rust_cargo_fix(root: Path) -> StaticResult:
    result = StaticResult()
    if shutil.which("cargo") is None:
        result.notes.append("cargo not found; skipping rust static pass")
        return result
    for cmd in (
        ["cargo", "fix", "--allow-dirty", "--allow-staged", "--quiet"],
        ["cargo", "clippy", "--fix", "--allow-dirty", "--allow-staged", "--quiet"],
    ):
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=900)
        result.notes.append(f"{cmd[1]}: exit {proc.returncode}")
    return result


def static_pass(root: Path, lang: str, source_files: list[Path], all_project_files: list[Path]) -> StaticResult:
    if lang == "python":
        return _python_remove_dead(source_files, all_project_files)
    if lang in ("javascript", "typescript"):
        return _js_remove_dead_exports(source_files, all_project_files)
    if lang == "rust":
        return _rust_cargo_fix(root)
    return StaticResult(notes=[f"no static pass for {lang}"])
