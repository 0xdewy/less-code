"""Consented deletion proposals: mine, apply, verify — never silent.

Mining produces reference-closed groups of symbols that nothing outside the
group can reach, behind hard never-propose rails: dynamic discovery, string
dispatch, decorators, entry points, protocol surface, side-effectful
assignments, test references, and API contracts. Nothing is ever deleted
without explicit group ids on the command line; each group is applied
mechanically, verified with the same primitives as `lc shrink`'s layers,
reverted with its gate reason on any red, and recorded in proposals.json as
the audit trail. Originals are sacred: everything runs on a sibling copy
unless --in-place.
"""

from __future__ import annotations

import ast
import io
import json
import re
import shlex
import subprocess
import sys
import tokenize
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .api_check import EXTRACTORS, api_violations
from .documentation import documentation_layout
from .langdetect import SKIP_DIRS, ProjectMap, map_project
from .loc import measure
from .shadow import changed_functions, run_shadow
from .static import _ENTRY_POINT_FILES, _attached_kill_lines, _is_definition_site
from .testrunners import gate_command, run_tests, tree_hash

_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".rst",
    ".cfg",
    ".toml",
    ".ini",
    ".txt",
    ".yml",
    ".yaml",
    ".json",
}

_DYNAMIC_DISCOVERY = (
    "pkgutil",
    "import_module",
    "walk_packages",
    "iter_modules",
    "entry_points",
    "__subclasses__",
)


@dataclass
class Candidate:
    kind: str  # "function" | "method" | "class" | "const" | "module"
    name: str
    file: str  # repo-relative
    loc: int  # canonical LOC of the span (less_code.loc.measure)
    start_line: int  # 1-based, first decorator if decorated
    end_line: int
    covered_by_tests: bool | None = None  # None when coverage unavailable
    references: list[str] = field(default_factory=list)
    text: str = ""  # exact span at propose time; applied spans must match


@dataclass
class Group:
    id: str
    candidates: list[Candidate]
    loc: int = 0
    covered_by_tests: bool | None = None
    references: list[str] = field(default_factory=list)
    api_delta: list[str] = field(default_factory=list)


@dataclass
class GateReport:
    docs_ok: bool
    tests_ok: bool
    tests_run: int | None
    shadow_ok: bool | None  # None = nothing modified survived to shadow
    api_violations: list[str]
    api_clean: bool
    reason: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.docs_ok
            and self.tests_ok
            and self.shadow_ok is not False
            and self.api_clean
        )

    def to_json(self) -> dict:
        return {
            "docs_ok": self.docs_ok,
            "tests_ok": self.tests_ok,
            "tests_run": self.tests_run,
            "shadow_ok": self.shadow_ok,
            "api_violations": self.api_violations,
            "api_clean": self.api_clean,
            "reason": self.reason,
            "ok": self.ok,
        }


@dataclass
class _Sym:
    """A top-level symbol span (1-based inclusive): candidate or live node."""

    kind: str  # function | method | class | const | assign
    name: str  # qualified for display ("C._m")
    simple: str  # the name references are scanned by
    start: int
    end: int
    decorated: bool = False
    eligible: bool = False
    candidate: Candidate | None = None


def _corpus_files(root: Path, exclude: Path | None = None) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if exclude is not None and path == exclude:
            continue
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
            continue
        out.append(path)
    return out


def _packaging_marked(root: Path) -> bool:
    """A `[project]` table or a setup.py means library consumers exist
    outside the repository; the public API is the product (rail 9)."""
    if (root / "setup.py").is_file():
        return True
    config = root / "pyproject.toml"
    if not config.is_file():
        return False
    text = config.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"(?m)^\[project\]", text))


def _entry_point_module_files(root: Path) -> set[Path]:
    """Files reachable from console/script entries (rail 5): the names they
    reference are live, and these files are never candidates."""
    modules: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        project = data.get("project", {})
        for table in ("scripts", "gui-scripts"):
            modules += [
                target.split(":")[0] for target in project.get(table, {}).values()
            ]
        poetry = data.get("tool", {}).get("poetry", {}).get("scripts", {})
        modules += [target.split(":")[0] for target in poetry.values()]
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        text = setup_cfg.read_text(encoding="utf-8", errors="replace")
        section = re.search(r"(?ms)^\[options\.entry_points\]\s*$(.*?)(?=^\[|\Z)", text)
        if section:
            for kind in ("console_scripts", "gui_scripts"):
                block = re.search(
                    rf"(?ms)^{kind}\s*=$(.*?)(?=^\w|\Z)", section.group(1)
                )
                if block:
                    modules += [
                        line.split("=", 1)[1].strip().split(":")[0]
                        for line in block.group(1).splitlines()
                        if "=" in line
                    ]
    files: set[Path] = set()
    for module in modules:
        parts = [part for part in module.split(".") if part]
        if parts:
            for base in (root, root / "src"):
                files.add(base.joinpath(*parts).with_suffix(".py"))
    return files


def _string_literals(texts: dict[Path, str]) -> list[str]:
    literals: list[str] = []
    for text in texts.values():
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
            continue
        for tok in tokens:
            if tok.type == tokenize.STRING:
                match = re.search(r"(['\"])(.*?)\1", tok.string, re.DOTALL)
                if match and match.group(2):
                    literals.append(match.group(2))
    return literals


class _Miner:
    """Phase A: candidates, rails, mentions, groups. No edits, ever."""

    def __init__(
        self,
        root: Path,
        lang: str,
        app: bool,
        exclude: Path | None = None,
    ):
        self.root = root
        self.lang = lang
        self.app = app
        self.project = map_project(root, lang)
        self.exclude = exclude
        self.corpus = _corpus_files(root, exclude)
        self.texts: dict[Path, str] = {
            path: path.read_text(encoding="utf-8", errors="replace")
            for path in self.corpus
        }
        self.blocked_dirs: set[Path] = set()
        self.entry_files: set[Path] = set()
        self.string_dispatch = False
        self.literals: list[str] = []
        self.syms: dict[Path, list[_Sym]] = {}
        self.candidates: list[Candidate] = []
        self.scan_names: set[str] = set()
        self.cand_names: set[str] = set()
        self.live_mentions: dict[str, set[str]] = {}
        self.cand_edges: dict[str, set[str]] = {}
        self.module_names: dict[Path, str] = {}

    def apply_rails(self) -> None:
        for path, text in self.texts.items():
            if path.suffix == ".py" and any(
                re.search(rf"\b{word}\b", text) for word in _DYNAMIC_DISCOVERY
            ):
                self.blocked_dirs.add(path.parent)
        for path in self.corpus:
            parts = path.relative_to(self.root).parts
            if path.name in _ENTRY_POINT_FILES or any(
                part in ("scripts", "bin") for part in parts
            ):
                self.entry_files.add(path)
        self.entry_files |= _entry_point_module_files(self.root)
        py_texts = [t for p, t in self.texts.items() if p.suffix == ".py"]
        self.string_dispatch = any(
            re.search(r"\b(?:getattr|globals)\s*\(", t) for t in py_texts
        )
        self.literals = _string_literals(
            {p: t for p, t in self.texts.items() if p.suffix == ".py"}
            if self.string_dispatch
            else {}
        )

    def _rail_file_blocked(self, path: Path) -> bool:
        if path in self.entry_files:
            return True
        rel = path.relative_to(self.root)
        for directory in self.blocked_dirs:
            try:
                rel.relative_to(directory.relative_to(self.root))
            except ValueError:
                continue
            return True
        return False

    def _rail_private(self, sym: _Sym) -> bool:
        if sym.kind == "method":
            cls, _, method = sym.name.partition(".")
            return method.startswith("_") or cls.startswith("_")
        return sym.simple.startswith("_")

    def _eligible(self, sym: _Sym) -> bool:
        if sym.kind in ("assign",) or sym.decorated:
            return False
        if self.string_dispatch:
            if sym.kind == "method":
                return False
            if sym.kind == "function" and any(
                len(sym.simple) >= 6 and len(lit) >= 6 and sym.simple[:6] == lit[:6]
                for lit in self.literals
            ):
                return False
        if sym.simple.startswith("__"):
            return False
        return self._rail_private(sym) or (
            self.app and not _packaging_marked(self.root)
        )

    def _top_level_syms(self, path: Path, text: str) -> list[_Sym]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        syms: list[_Sym] = []
        for node in tree.body:
            start = min(
                [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
            )
            end = node.end_lineno
            decorated = bool(getattr(node, "decorator_list", []))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                syms.append(
                    _Sym("function", node.name, node.name, start, end, decorated)
                )
            elif isinstance(node, ast.ClassDef):
                syms.append(_Sym("class", node.name, node.name, start, end, decorated))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        syms.append(
                            _Sym(
                                "method",
                                f"{node.name}.{child.name}",
                                child.name,
                                child.lineno,
                                child.end_lineno,
                                bool(child.decorator_list),
                            )
                        )
            elif isinstance(node, ast.Assign) and all(
                isinstance(target, ast.Name) for target in node.targets
            ):
                name = node.targets[0].id
                literal = isinstance(node.value, ast.Constant) or (
                    isinstance(node.value, ast.Tuple)
                    and all(isinstance(e, ast.Constant) for e in node.value.elts)
                )
                syms.append(
                    _Sym("const" if literal else "assign", name, name, start, end)
                )
        return syms

    def collect_candidates(self) -> None:
        for path in self.project.source_files:
            text = self.texts.get(path, "")
            rel = str(path.relative_to(self.root))
            if (
                path in self.entry_files
                or path.name == "__init__.py"
                or self._rail_file_blocked(path)
            ):
                self.syms[path] = self._top_level_syms(path, text)
                continue
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in ("__getattr__", "__dir__")
                for node in _parse_or_none(text)
            ):
                self.syms[path] = self._top_level_syms(path, text)
                continue
            syms = self._top_level_syms(path, text)
            for sym in syms:
                sym.eligible = self._eligible(sym)
                if not sym.eligible:
                    continue
                span = "\n".join(text.splitlines()[sym.start - 1 : sym.end])
                sym.candidate = Candidate(
                    kind=sym.kind,
                    name=sym.name,
                    file=rel,
                    loc=measure(span, self.lang).code,
                    start_line=sym.start,
                    end_line=sym.end,
                    text=span,
                )
                self.candidates.append(sym.candidate)
                self.scan_names.add(sym.simple)
                self.cand_names.add(sym.simple)
            self.syms[path] = syms
            if (
                syms
                and path.name != "__init__.py"
                and not self._rail_file_blocked(path)
            ):
                stem = path.stem
                self.module_names[path] = stem
                self.scan_names.add(stem)

    def scan_mentions(self) -> None:
        words = {
            name: re.compile(rf"\b{re.escape(name)}\b") for name in self.scan_names
        }
        self.live_mentions = {name: set() for name in self.scan_names}
        self.cand_edges = {name: set() for name in self.scan_names}
        for path, text in self.texts.items():
            spans = self.syms.get(path) or (
                self._top_level_syms(path, text) if path.suffix == ".py" else []
            )
            for name, pattern in words.items():
                for m in pattern.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    prefix = text[text.rfind("\n", 0, m.start()) + 1 : m.start()]
                    if _is_definition_site(prefix):
                        continue
                    owner = _innermost(spans, line)
                    if owner is not None and owner.candidate is not None:
                        self.cand_edges[owner.simple].add(name)
                    else:
                        self.live_mentions[name].add(str(path.relative_to(self.root)))
        for cand in self.candidates:
            scan = cand.name.rsplit(".", 1)[-1]
            cand.references = sorted(self.live_mentions.get(scan, ()))

    def build_groups(self) -> list[Group]:
        """Groups are reverse-closures: a candidate may only be deleted
        together with every candidate that references it, and only when no
        member is referenced from outside the candidate set at all."""
        referencers: dict[str, set[str]] = {name: set() for name in self.cand_names}
        for name, edges in self.cand_edges.items():
            if name not in referencers:
                continue
            for target in edges:
                if target in referencers and target != name:
                    referencers[target].add(name)
        closures: dict[str, frozenset[str]] = {}
        for seed in sorted(self.cand_names):
            if self.live_mentions.get(seed):
                continue
            closure: set[str] = set()
            stack = [seed]
            while stack:
                current = stack.pop()
                if current in closure:
                    continue
                closure.add(current)
                stack.extend(referencers.get(current, ()))
            if any(self.live_mentions.get(member) for member in closure):
                continue
            closures[seed] = frozenset(closure)
        maximal: list[frozenset[str]] = []
        for closure in sorted(closures.values(), key=lambda c: (-len(c), sorted(c))):
            if any(closure <= kept for kept in maximal):
                continue
            maximal.append(closure)
        by_name: dict[str, list[Candidate]] = {}
        for syms in self.syms.values():
            for sym in syms:
                if sym.candidate is not None and sym.simple in closures:
                    by_name.setdefault(sym.simple, []).append(sym.candidate)
        groups: list[Group] = []
        for members in maximal:
            cands = [c for name in sorted(members) for c in by_name.get(name, [])]
            if not cands:
                continue
            groups.append(
                Group(
                    id="",
                    candidates=cands,
                    loc=sum(c.loc for c in cands),
                    references=sorted(
                        {
                            f
                            for name in members
                            for f in self.live_mentions.get(name, set())
                        }
                    ),
                )
            )
        return self._promote_modules(groups)

    def _promote_modules(self, groups: list[Group]) -> list[Group]:
        index_of = {id(c): i for i, g in enumerate(groups) for c in g.candidates}
        for path, stem in sorted(self.module_names.items()):
            if self.live_mentions.get(stem):
                continue
            syms = self.syms.get(path, [])
            if not syms or not all(sym.eligible for sym in syms):
                continue
            member_groups = {
                index_of[id(sym.candidate)]
                for sym in syms
                if sym.candidate is not None and id(sym.candidate) in index_of
            }
            if len(member_groups) != 1:
                continue
            group = groups[member_groups.pop()]
            rel = str(path.relative_to(self.root))
            text = self.texts.get(path, "")
            module = Candidate(
                kind="module",
                name=rel,
                file=rel,
                loc=measure(text, self.lang).code,
                start_line=1,
                end_line=len(text.splitlines()) or 1,
                references=sorted(self.live_mentions.get(stem, ())),
                text=text,
            )
            if module.references:
                continue
            group.candidates = [c for c in group.candidates if c.file != rel]
            group.candidates.append(module)
            group.loc = sum(c.loc for c in group.candidates)
            group.references = sorted(
                {f for c in group.candidates for f in c.references}
            )
        return [g for g in groups if g.candidates]


def _parse_or_none(text: str) -> list[ast.stmt]:
    try:
        return ast.parse(text).body
    except SyntaxError:
        return []


def _innermost(spans: list[_Sym], line: int) -> _Sym | None:
    best = None
    for span in spans:
        if span.start <= line <= span.end and (
            best is None or (span.end - span.start) < (best.end - best.start)
        ):
            best = span
    return best


def mine(root: Path, lang: str | None, app: bool = False, exclude: Path | None = None):
    """(project, groups): groups are ranked and id-assigned for consent."""
    from .langdetect import detect_language

    lang = lang or detect_language(root)
    if lang != "python":
        raise SystemExit(f"lc propose supports Python only, not {lang}")
    miner = _Miner(root, lang, app, exclude)
    miner.apply_rails()
    miner.collect_candidates()
    if not miner.candidates:
        return miner.project, []
    miner.scan_mentions()
    groups = miner.build_groups()
    groups.sort(
        key=lambda g: (
            {False: 0, None: 1, True: 2}[g.covered_by_tests],
            -g.loc,
            g.candidates[0].file,
            g.candidates[0].name,
        )
    )
    for number, group in enumerate(groups, 1):
        group.id = str(number)
    return miner.project, groups


def coverage_overlay(
    root: Path,
    project: ProjectMap,
    groups: list[Group],
    test_command: list[str] | None,
    timeout: int,
) -> None:
    """Optional accelerator: rank only. Rails + the gate still decide."""
    interpreter = sys.executable
    try:
        probe = subprocess.run(
            [interpreter, "-c", "import coverage"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if probe.returncode != 0:
        return
    base = list(test_command) if test_command else gate_command(root, project.lang)
    if len(base) >= 2 and base[1] == "-m":
        run_cmd = [interpreter, "-m", "coverage", "run", "--parallel-mode", *base[1:]]
    else:
        run_cmd = [interpreter, "-m", "coverage", "run", "--parallel-mode", *base]
    out = root / ".coverage-propose.json"
    try:
        run = subprocess.run(
            run_cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if run.returncode != 0:
            return
        subprocess.run(
            [interpreter, "-m", "coverage", "combine", "-q"],
            cwd=root,
            capture_output=True,
            timeout=120,
            check=False,
        )
        report = subprocess.run(
            [interpreter, "-m", "coverage", "json", "-o", str(out)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if report.returncode != 0:
            return
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return
    finally:
        with _suppressed():
            out.unlink(missing_ok=True)
            for stale in root.glob(".coverage*"):
                if stale.is_file():
                    stale.unlink()
    executed: dict[str, set[int]] = {}
    for path, info in data.get("files", {}).items():
        executed[str(Path(path).resolve())] = set(info.get("executed_lines", []))
    for group in groups:
        flags = []
        for cand in group.candidates:
            lines = executed.get(str((root / cand.file).resolve()), set())
            cand.covered_by_tests = bool(
                lines & set(range(cand.start_line, cand.end_line + 1))
            )
            flags.append(cand.covered_by_tests)
        group.covered_by_tests = any(flags) if flags else None


@contextmanager
def _suppressed():
    try:
        yield
    except OSError:
        pass


class StaleSpan(Exception):
    """A recorded span no longer matches the tree; the group must not apply."""


def apply_group(
    root: Path,
    project: ProjectMap,
    group: Group,
    removed: dict[Path, set[int]] | None = None,
) -> tuple[list[Path], dict[Path, set[int]]]:
    """Mechanical deletion: kill-line spans, or the whole file for modules.

    Spans are recorded in propose-time line numbers. `removed` carries the
    propose-time lines earlier consented groups in this invocation already
    deleted (file -> set of 1-based line numbers); every span is remapped
    through them and verified against the text recorded at propose time
    before anything is written, so sequential deletions in one file — at any
    apply order — can never steer a later group's lines astray (the same
    stale-span discipline as the ML layer's host-owned edits).

    Returns the touched paths and, per file, the propose-time line numbers
    this group removed; the caller records them only after the group's gate
    passes, so a reverted group leaves no offset debt behind."""
    removed = removed if removed is not None else {}
    line_spans: dict[Path, list[tuple[int, int, Candidate]]] = {}
    module_files: list[Path] = []
    for cand in group.candidates:
        path = root / cand.file
        if cand.kind == "module":
            if not path.is_file():
                raise StaleSpan(f"{cand.file}: module file is gone")
            module_files.append(path)
        else:
            line_spans.setdefault(path, []).append(
                (cand.start_line, cand.end_line, cand)
            )
    pending: list[tuple[Path, str]] = []
    killed: dict[Path, set[int]] = {}
    for path, spans in sorted(line_spans.items()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise StaleSpan(f"{path.name}: file is gone; re-run lc propose") from exc
        lines = text.splitlines()
        gone = removed.get(path, set())
        survivors: list[int] | None = None
        if gone:
            if any(start <= n <= end for start, end, _ in spans for n in gone):
                raise StaleSpan(
                    f"{path.name}: a later span overlaps a group already"
                    " applied in this invocation; re-run lc propose"
                )
            propose_len = len(lines) + len(gone)
            survivors = [n for n in range(1, propose_len + 1) if n not in gone]
        kill: set[int] = set()
        for start, end, cand in spans:
            recorded = cand.text.splitlines()
            if survivors is not None:
                start -= sum(1 for n in gone if n < start)
                end -= sum(1 for n in gone if n <= end)
            if start < 1 or end > len(lines) or lines[start - 1 : end] != recorded:
                raise StaleSpan(
                    f"{cand.file}:{cand.start_line}-{cand.end_line}"
                    f" ({cand.name}): span changed since propose;"
                    " re-run lc propose"
                )
            kill |= _attached_kill_lines(lines, start - 1, end)
        keep = [n for n in range(len(lines)) if n not in kill]
        while keep and not lines[keep[-1]].strip():
            keep.pop()
        removed_current = {n for n in range(len(lines))} - set(keep)
        if survivors is not None:
            killed[path] = {survivors[n] for n in removed_current}
        else:
            killed[path] = {n + 1 for n in removed_current}
        new_text = "\n".join(lines[n] for n in keep)
        if text.endswith("\n"):
            new_text += "\n"
        ast.parse(new_text)
        if new_text != text:
            pending.append((path, new_text))
    for path, new_text in pending:
        path.write_text(new_text, encoding="utf-8")
    touched = [path for path, _ in pending]
    for path in module_files:
        path.unlink()
        touched.append(path)
    return touched, killed


def _api_names(violations: list[str]) -> list[str]:
    names: list[str] = []
    for violation in violations:
        for body in re.findall(r"(?:missing|changed|added)=(\[[^\]]*\])", violation):
            try:
                names.extend(ast.literal_eval(body))
            except (ValueError, SyntaxError):
                continue
    return names


def _api_clean(violations: list[str], group: Group) -> bool:
    """Every violation must name a symbol in the group: deleting `_x` removes
    `_x` from the surface, which is the disclosure, not a defect. A violation
    naming anything else means live surface depends on the group."""
    group_names = {cand.name for cand in group.candidates}
    simple = {name.rsplit(".", 1)[-1] for name in group_names}
    return all(
        name in group_names or name.rsplit(".", 1)[-1] in simple
        for name in _api_names(violations)
    )


def verify_tree(
    root: Path,
    project: ProjectMap,
    before_sources: dict[Path, str],
    group: Group,
    test_command: list[str] | None,
    timeout: int,
) -> GateReport:
    """Same primitives and order as `lc shrink`'s layer gate: docs, tests,
    then the shadow oracle for Python — plus the API delta as disclosure."""
    current = {
        path: _read_or_empty(root / str(path.relative_to(root)))
        for path in before_sources
    }
    docs_ok = {
        path: documentation_layout(text, project.lang)
        for path, text in before_sources.items()
    } == {
        path: documentation_layout(text, project.lang) for path, text in current.items()
    }
    reason = "" if docs_ok else "documentation changed"
    check = run_tests(root, project.lang, timeout=timeout, command=test_command)
    tests_ok = check.ok
    if not tests_ok:
        reason = reason or f"tests failed: {check.output_tail[-200:]}"
    shadow_ok = None
    if tests_ok and project.lang == "python":
        spec = changed_functions(before_sources, current)
        if spec:
            report = run_shadow(root, spec, test_command, timeout)
            shadow_ok = report.tests_ok and not report.mismatches
            if not shadow_ok:
                reason = reason or "shadow oracle mismatch"
    extract = EXTRACTORS[project.lang]
    before_api = {str(path): extract(text) for path, text in before_sources.items()}
    after_api = {str(path): extract(text) for path, text in current.items()}
    violations = api_violations(before_api, after_api)
    api_clean = _api_clean(violations, group)
    if not api_clean:
        reason = reason or f"API changed outside the group: {violations[0]}"
    return GateReport(
        docs_ok=docs_ok,
        tests_ok=tests_ok,
        tests_run=check.tests_run,
        shadow_ok=shadow_ok,
        api_violations=violations,
        api_clean=api_clean,
        reason=reason,
    )


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _snapshot(files: list[Path]) -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in files}


def _restore(snapshot: dict[Path, str]) -> None:
    for path, text in snapshot.items():
        path.write_text(text, encoding="utf-8")


def _api_delta(root: Path, group: Group, lang: str) -> list[str]:
    extract = EXTRACTORS[lang]
    delta: set[str] = set()
    for cand in group.candidates:
        try:
            surface = extract(
                (root / cand.file).read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        if cand.kind == "module":
            delta |= set(surface)
        elif cand.name in surface:
            delta.add(cand.name)
    return sorted(delta)


def _group_json(root: Path, group: Group, lang: str) -> dict:
    return {
        "id": group.id,
        "candidates": [
            {
                "kind": c.kind,
                "name": c.name,
                "file": c.file,
                "loc": c.loc,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "covered_by_tests": c.covered_by_tests,
                "references": c.references,
                "text": c.text,
            }
            for c in group.candidates
        ],
        "loc": group.loc,
        "evidence": {
            "references": sorted({f for c in group.candidates for f in c.references}),
            "covered_by_tests": group.covered_by_tests,
        },
        "api_delta": _api_delta(root, group, lang),
        "applied": False,
    }


def _load_runs(path: Path) -> tuple[dict, list]:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return data, data.get("runs", [])
    return {}, []


def propose_tree(
    root: Path,
    lang: str | None = None,
    app: bool = False,
    coverage: bool = True,
    test_command: list[str] | None = None,
    timeout: int = 600,
    out: Path | None = None,
) -> tuple[dict, int]:
    """Mine groups, write proposals.json (appending runs), print the table.
    Verifies nothing. Returns (report, exit_code)."""
    project, groups = mine(root, lang, app, exclude=out)
    if coverage and groups:
        coverage_overlay(root, project, groups, test_command, timeout)
        groups.sort(
            key=lambda g: (
                {False: 0, None: 1, True: 2}[g.covered_by_tests],
                -g.loc,
                g.candidates[0].file,
                g.candidates[0].name,
            )
        )
        for number, group in enumerate(groups, 1):
            group.id = str(number)
    _, runs = _load_runs(out) if out is not None else ({}, [])
    consent = {
        "command": " ".join(shlex.quote(part) for part in sys.argv),
        "path": str(root),
        "lang": "python",
        "app": app,
        "test_command": list(test_command or []),
        "timeout": timeout,
        "tree_hash": tree_hash(root, "python"),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload = {
        "consent": consent,
        "groups": [_group_json(root, group, "python") for group in groups],
        "runs": runs,
    }
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload, (2 if groups else 0)


def print_table(payload: dict, target: Path) -> None:
    groups = payload["groups"]
    if not groups:
        print("no deletion candidates passed the rails — nothing to propose")
        return
    print("| id | name | kind | file | LOC | covered_by_tests | api delta |")
    print("|---|---|---|---|---:|---|---|")
    for group in groups:
        for cand in group["candidates"]:
            print(
                f"| {group['id']} | {cand['name']} | {cand['kind']} | "
                f"{cand['file']} | {cand['loc']} | {cand['covered_by_tests']} | "
                f"{', '.join(group['api_delta']) or '-'} |"
            )
    ids = ",".join(group["id"] for group in groups)
    print(f"\napply: lc propose {target} --apply {ids}")


def apply_proposals(
    root: Path,
    ids: list[str],
    test_command: list[str] | None,
    timeout: int,
    out: Path,
) -> tuple[dict, int]:
    """Apply consented groups by id: apply -> verify -> revert on any red.
    A proposals file older than the tree is rejected (exit 3)."""
    data, runs = _load_runs(out)
    if not data or not data.get("groups"):
        print(
            f"no proposals at {out}; re-run lc propose first",
            file=sys.stderr,
        )
        return data, 3
    consent = data.get("consent", {})
    lang = consent.get("lang", "python")
    if consent.get("tree_hash") != tree_hash(root, lang):
        print(
            "proposals are stale: the tree changed since lc propose — re-run lc propose",
            file=sys.stderr,
        )
        return data, 3
    command = (
        list(test_command) if test_command else consent.get("test_command") or None
    )
    wait = timeout if timeout != 600 else consent.get("timeout", timeout)
    by_id = {group["id"]: group for group in data["groups"]}
    unknown = [gid for gid in ids if gid not in by_id]
    if unknown:
        print(f"unknown group id(s): {', '.join(unknown)}", file=sys.stderr)
        return data, 3
    project = map_project(root, lang)

    # Consent order is apply order. Every kept group records the propose-time
    # lines it removed, and apply_group remaps later groups' spans through
    # those removals — so sequential deletions in one file are safe in any
    # order, and a reverted group leaves no offset debt behind.
    removed: dict[Path, set[int]] = {}
    results = []
    for gid in ids:
        group = by_id[gid]
        snapshot = _snapshot(project.source_files)
        try:
            _, killed = apply_group(root, project, _group_from_json(group), removed)
        except SyntaxError as exc:
            _restore(snapshot)
            group["applied"] = False
            group["gate"] = {"ok": False, "reason": f"syntax error: {exc}"}
            results.append((gid, False, f"syntax error: {exc}"))
            continue
        except StaleSpan as exc:
            _restore(snapshot)
            group["applied"] = False
            group["gate"] = {"ok": False, "reason": str(exc)}
            results.append((gid, False, str(exc)))
            continue
        report = verify_tree(
            root, project, snapshot, _group_from_json(group), command, wait
        )
        if report.ok:
            for path, lines in killed.items():
                removed.setdefault(path, set()).update(lines)
            group["applied"] = True
            group["gate"] = report.to_json()
            results.append((gid, True, ""))
        else:
            _restore(snapshot)
            group["applied"] = False
            group["gate"] = report.to_json()
            results.append((gid, False, report.reason))
    runs.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "command": " ".join(shlex.quote(part) for part in sys.argv),
            "tree_hash": tree_hash(root, lang),
            "requested": list(ids),
            "results": [
                {"id": gid, "applied": ok, "reason": why} for gid, ok, why in results
            ],
        }
    )
    data["runs"] = runs
    if out is not None:
        out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for gid, ok, why in results:
        print(f"group {gid}: {'applied' if ok else f'reverted ({why})'}")
    if results and all(ok for _, ok, _ in results):
        return data, 0
    if results and not any(ok for _, ok, _ in results):
        return data, 1
    return data, 0


def _group_from_json(group: dict) -> Group:
    return Group(
        id=group["id"],
        candidates=[
            Candidate(
                kind=c["kind"],
                name=c["name"],
                file=c["file"],
                loc=c["loc"],
                start_line=c["start_line"],
                end_line=c["end_line"],
                covered_by_tests=c.get("covered_by_tests"),
                references=c.get("references", []),
                text=c.get("text", ""),
            )
            for c in group["candidates"]
        ],
        loc=group.get("loc", 0),
    )
