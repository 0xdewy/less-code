"""Differential oracle for Python rewrites: run the original body alongside.

Tests are a weak oracle because they check assertions, not functions. This
module strengthens the gate without changing what the tests exercise: the
suite runs once more with every rewritten function shadowed by its original
body (see `shadow_site/sitecustomize.py`), and any call whose outcome differs
between the two bodies rejects the layer.

    changed_functions(pristine, current) -> spec of rewritten functions
    run_shadow(root, spec, ...)          -> ShadowReport

The comparison covers return values, exceptions, elements of returned
iterators (lazily), and argument mutation. It runs only for calls the suite
makes, so `ShadowReport.exercised` says how many rewritten functions were
verified at all - the number to quote next to the reduction.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

from .testrunners import run_tests

SITE_DIR = Path(__file__).parent / "shadow_site"

_SIMPLE_DECORATORS = {"property", "classmethod", "staticmethod"}

#: Calls whose second execution is observable outside the function (I/O,
#: process state, randomness, clocks) or whose result depends on the call
#: stack, which the shadow wrapper deepens by one frame. Such functions are
#: reported as `effectful`, never shadowed; bisecting them out at run time
#: costs a suite run per step, and they would only produce false mismatches.
_EFFECT_NAMES = {"print", "input", "open", "exec", "eval", "exit", "quit"}
_EFFECT_MODULES = {
    "os",
    "sys",
    "io",
    "shutil",
    "subprocess",
    "socket",
    "select",
    "signal",
    "threading",
    "multiprocessing",
    "random",
    "secrets",
    "time",
    "datetime",
    "uuid",
    "logging",
    "warnings",
    "gc",
    "atexit",
    "tempfile",
    "traceback",
    "inspect",
    "linecache",
    "builtins",
    "importlib",
}
_EFFECT_ATTRS = {
    "write",
    "writelines",
    "flush",
    "close",
    "send",
    "sendall",
    "recv",
    "read",
    "readline",
    "readlines",
    "seek",
    "truncate",
    "now",
    "today",
    "utcnow",
    "getvalue",
    "_getframe",
    "f_back",
    "f_globals",
    "f_locals",
}


def _effectful(node: ast.FunctionDef) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            if isinstance(func, ast.Name) and func.id in _EFFECT_NAMES:
                return True
            if isinstance(func, ast.Attribute):
                if func.attr in _EFFECT_ATTRS:
                    return True
                base = func.value
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name) and base.id in _EFFECT_MODULES:
                    return True
        elif isinstance(inner, ast.Attribute) and inner.attr in _EFFECT_ATTRS:
            return True
        elif isinstance(inner, (ast.With, ast.AsyncWith)):
            return True  # context managers: files, locks, temporary state
    return False


def _shadowable(node: ast.FunctionDef) -> bool:
    """Only bodies that behave identically when compiled at module level."""
    decorators = [ast.unparse(d) for d in node.decorator_list]
    if any(d not in _SIMPLE_DECORATORS for d in decorators):
        return False
    for inner in ast.walk(node):
        if isinstance(inner, (ast.Await, ast.Global)):
            return False
        if isinstance(inner, ast.Name) and inner.id in ("super", "__class__"):
            return False
        if isinstance(inner, ast.Attribute) and (
            inner.attr.startswith("__") and not inner.attr.endswith("__")
        ):
            return False  # name mangling needs the class body
    return True


def _function_index(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    index: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            index[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef):
                    index[f"{node.name}.{child.name}"] = child
    return index


def changed_functions(
    pristine: dict[Path, str], current: dict[Path, str]
) -> dict[str, dict[str, dict]]:
    """`{module path: {qualname: {"source": original def}}}` for every
    function whose body or decorators changed and whose original can run as
    a shadow. Functions that were added or removed have nothing to compare."""
    spec: dict[str, dict[str, dict]] = {}
    for path, before_text in pristine.items():
        after_text = current.get(path)
        if after_text is None or after_text == before_text:
            continue
        try:
            before, after = ast.parse(before_text), ast.parse(after_text)
        except SyntaxError:
            continue
        old_index, new_index = _function_index(before), _function_index(after)
        lines = before_text.splitlines()
        functions: dict[str, dict] = {}
        for qualname, node in old_index.items():
            other = new_index.get(qualname)
            if other is None or ast.dump(node) == ast.dump(other):
                continue
            if not _shadowable(node) or not _shadowable(other):
                continue
            if _effectful(node) or _effectful(other):
                spec.setdefault("!effectful", {})[f"{Path(path).name}:{qualname}"] = {}
                continue
            source = textwrap.dedent(
                "\n".join(lines[node.lineno - 1 : node.end_lineno])
            )
            functions[qualname] = {"source": source}
        if functions:
            spec[os.path.realpath(path)] = functions
    return spec


@dataclass
class ShadowReport:
    tests_ok: bool
    functions: int = 0
    exercised: int = 0
    verified_calls: int = 0
    skipped_calls: int = 0
    mismatches: list[dict] = field(default_factory=list)
    install_errors: list[str] = field(default_factory=list)
    nondeterministic_calls: int = 0
    interference: list[str] = field(default_factory=list)
    effectful: list[str] = field(default_factory=list)
    output_tail: str = ""

    def to_json(self) -> dict:
        return {
            "tests_ok": self.tests_ok,
            "functions": self.functions,
            "exercised": self.exercised,
            "verified_calls": self.verified_calls,
            "skipped_calls": self.skipped_calls,
            "mismatches": self.mismatches[:20],
            "install_errors": self.install_errors[:20],
            "nondeterministic_calls": self.nondeterministic_calls,
            "interference": self.interference[:20],
            "effectful": self.effectful[:40],
        }

    @property
    def ok(self) -> bool:
        return self.tests_ok and not self.mismatches


#: Functions whose shadow execution broke the suite, keyed by (path, qualname,
#: original source). Interference is a property of the original body, so it
#: is remembered for the process lifetime and never bisected twice; the gate
#: runs dozens of times per project and would otherwise repeat the search.
_INTERFERING: dict[tuple[str, str, str], bool] = {}


def _keys(spec: dict[str, dict[str, dict]]) -> list[tuple[str, str]]:
    return [(path, name) for path, functions in spec.items() for name in functions]


def _subset(spec: dict[str, dict[str, dict]], keys) -> dict[str, dict[str, dict]]:
    wanted = set(keys)
    out: dict[str, dict[str, dict]] = {}
    for path, functions in spec.items():
        kept = {
            name: info for name, info in functions.items() if (path, name) in wanted
        }
        if kept:
            out[path] = kept
    return out


def run_shadow(
    root: Path,
    spec: dict[str, dict[str, dict]],
    test_command: list[str] | None,
    timeout: int,
) -> ShadowReport:
    """Run the suite with the shadow oracle active for `spec`.

    Shadowing runs every call twice, so a function with external effects
    (it writes a file, prints, consumes a shared stream) can make the suite
    itself fail even though both bodies agree. Such a failure is not a
    mismatch, it is the oracle interfering; the culprit functions are found
    by bisection, reported as `interference` (unverified), and the remaining
    functions are compared on their own.
    """
    started = time.monotonic()
    effectful = sorted(spec.pop("!effectful", {}))
    keys = _keys(spec)
    known = [
        key for key in keys if _INTERFERING.get((*key, spec[key[0]][key[1]]["source"]))
    ]
    report = _run_once(
        root, _subset(spec, [k for k in keys if k not in known]), test_command, timeout
    )
    report.functions = len(keys)
    report.interference = [f"{Path(p).name}:{n}" for p, n in known]
    report.effectful = effectful
    if report.tests_ok or not keys:
        _trace(report, started, bisected=False)
        return report
    interfering: list[tuple[str, str]] = list(known)

    def bisect(group: list[tuple[str, str]]) -> None:
        if len(group) == 1:
            interfering.append(group[0])
            _INTERFERING[(*group[0], spec[group[0][0]][group[0][1]]["source"])] = True
            return
        middle = len(group) // 2
        for half in (group[:middle], group[middle:]):
            if _run_once(root, _subset(spec, half), test_command, timeout).tests_ok:
                continue
            bisect(half)

    bisect([key for key in keys if key not in known])
    remaining = [key for key in keys if key not in interfering]
    if remaining:
        report = _run_once(root, _subset(spec, remaining), test_command, timeout)
        if not report.tests_ok:
            # the culprits interact; give up on the oracle for this spec
            report = ShadowReport(tests_ok=True, functions=len(keys))
            report.interference = [f"{Path(p).name}:{n}" for p, n in keys]
            return report
    else:
        report = ShadowReport(tests_ok=True)
    report.functions = len(keys)
    report.interference = [f"{Path(p).name}:{n}" for p, n in interfering]
    report.effectful = effectful
    _trace(report, started, bisected=True)
    return report


def _trace(report: ShadowReport, started: float, bisected: bool) -> None:
    if os.environ.get("LC_TRACE"):
        print(
            f"[shadow] {report.functions} fns, {report.exercised} exercised,"
            f" {report.verified_calls} calls, {len(report.mismatches)} mismatch,"
            f" {len(report.interference)} interfering, {len(report.effectful)}"
            f" effectful, bisected={bisected}, tests_ok={report.tests_ok},"
            f" {time.monotonic() - started:.0f}s"
            + "".join(
                f"\n[shadow]   {m['file']}:{m['function']} x{m['count']} {m['examples'][:1]}"
                for m in report.mismatches[:8]
            ),
            file=sys.stderr,
            flush=True,
        )


def _run_once(
    root: Path,
    spec: dict[str, dict[str, dict]],
    test_command: list[str] | None,
    timeout: int,
) -> ShadowReport:
    total = sum(len(functions) for functions in spec.values())
    if not total:
        return ShadowReport(tests_ok=True)
    with tempfile.TemporaryDirectory(prefix="lc-shadow-") as scratch:
        report_dir = Path(scratch) / "reports"
        spec_path = Path(scratch) / "spec.json"
        spec_path.write_text(
            json.dumps({"functions": spec, "report_dir": str(report_dir)}),
            encoding="utf-8",
        )
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SITE_DIR) + (os.pathsep + existing if existing else "")
        env["LC_SHADOW"] = str(spec_path)
        result = run_tests(
            root, "python", timeout=timeout, command=test_command, env=env
        )
        report = ShadowReport(
            tests_ok=result.ok, functions=total, output_tail=result.output_tail
        )
        merged: dict[str, dict] = {}
        if report_dir.is_dir():
            for path in sorted(report_dir.glob("*.json")):
                try:
                    for key, entry in json.loads(path.read_text()).items():
                        target = merged.setdefault(
                            key,
                            {
                                "verified": 0,
                                "skipped": 0,
                                "mismatches": 0,
                                "examples": [],
                            },
                        )
                        target["verified"] += entry.get("verified", 0)
                        target["skipped"] += entry.get("skipped", 0)
                        target["mismatches"] += entry.get("mismatches", 0)
                        target["nondeterministic"] = target.get(
                            "nondeterministic", 0
                        ) + entry.get("nondeterministic", 0)
                        target["examples"] += entry.get("examples", [])
                        if entry.get("install_error"):
                            target["install_error"] = entry["install_error"]
                except (OSError, ValueError):
                    continue
        for key, entry in merged.items():
            report.verified_calls += entry["verified"]
            report.skipped_calls += entry["skipped"]
            report.nondeterministic_calls += entry.get("nondeterministic", 0)
            if entry["verified"]:
                report.exercised += 1
            if entry.get("install_error"):
                report.install_errors.append(
                    f"{Path(key.split('::', 1)[0]).name}:{key.split('::', 1)[-1]}"
                    f" ({entry['install_error']})"
                )
            if entry["mismatches"]:
                report.mismatches.append(
                    {
                        "function": key.split("::", 1)[-1],
                        "file": Path(key.split("::", 1)[0]).name,
                        "count": entry["mismatches"],
                        "examples": entry["examples"][:3],
                    }
                )
    return report
