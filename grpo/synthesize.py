"""Synthetic SFT pairs from rule-library inverses (roadmap F1, no frontier models).

The mining chicken-and-egg, measured: the base 7B produces ~5-15% acceptable
proposals, and ~40% of its attempts are doc-eating — you cannot mine a
training set from a model that mostly fails the gate. But the deterministic
rule library is a set of PROVEN verbose->compact transformations; their
inverses turn real, compact, shipped code into synthetic verbose code. Each
(real symbol, verbosified symbol) pair is a training example whose target
is correct by construction — it is the real code, docstrings and all, and
the gate already certifies it.

    uv run python grpo/synthesize.py --roots fixtures/py tenacity/... \
        --out grpo/synthetic.jsonl --pairs 300
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.llm_reduce import SYMBOL_SYSTEM_PROMPT, build_symbol_prompt  # noqa: E402


# ---- inverse rules: compact -> verbose (each the inverse of a proven rule) --


def _inv_bool_return(fn: ast.FunctionDef) -> ast.FunctionDef | None:
    """`return <boolish>` -> `if c: return True\\nreturn False` (inv bool-return)."""
    for i, stmt in enumerate(fn.body):
        if not isinstance(stmt, ast.Return) or not isinstance(stmt.value, ast.Compare):
            continue
        clone = ast.parse(ast.unparse(fn))
        target = clone.body[0]
        cond = stmt.value
        target.body[i:i + 1] = [
            ast.If(
                test=cond,
                body=[ast.Return(value=ast.Constant(True))],
                orelse=[ast.Return(value=ast.Constant(False))],
            )
        ]
        return clone.body[0]
    return None


def _inv_ternary(fn: ast.FunctionDef) -> ast.FunctionDef | None:
    """`x = a if c else b` -> if/else assignment (inverse of SIM108-style)."""
    for i, stmt in enumerate(fn.body):
        if (not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1
                or not isinstance(stmt.targets[0], ast.Name)
                or not isinstance(stmt.value, ast.IfExp)):
            continue
        clone = ast.parse(ast.unparse(fn))
        target = clone.body[0]
        node = stmt.value
        name = stmt.targets[0].id
        target.body[i:i + 1] = [
            ast.If(
                test=node.test,
                body=[ast.Assign(
                    targets=[ast.Name(id=name, ctx=ast.Store())], value=node.body)],
                orelse=[ast.Assign(
                    targets=[ast.Name(id=name, ctx=ast.Store())], value=node.orelse)],
            )
        ]
        return clone.body[0]
    return None


def _inv_update_loop(fn: ast.FunctionDef) -> ast.FunctionDef | None:
    """`target.update(d)` -> explicit loop (inverse of loop-dict-to-update)."""
    for i, stmt in enumerate(fn.body):
        if (not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call)
                or not isinstance(stmt.value.func, ast.Attribute)
                or stmt.value.func.attr != "update"):
            continue
        target_name = stmt.value.func.value
        src = stmt.value.args[0] if stmt.value.args else None
        if src is None or not isinstance(target_name, ast.Name):
            continue
        clone = ast.parse(ast.unparse(fn))
        t = clone.body[0]
        k, v = ast.Name(id="key", ctx=ast.Store()), ast.Name(id="value", ctx=ast.Store())
        t.body[i:i + 1] = [
            ast.For(
                target=ast.Tuple(elts=[k, v], ctx=ast.Store()),
                iter=ast.Call(
                    func=ast.Attribute(value=src, attr="items", ctx=ast.Load()),
                    args=[], keywords=[]),
                body=[ast.Assign(
                    targets=[ast.Subscript(
                        value=ast.Name(id=target_name.id, ctx=ast.Load()),
                        slice=ast.Name(id="key", ctx=ast.Load()), ctx=ast.Store())],
                    value=ast.Name(id="value", ctx=ast.Load()))],
                orelse=[],
            )
        ]
        return clone.body[0]
    return None


def _inv_sum_loop(fn: ast.FunctionDef) -> ast.FunctionDef | None:
    """`x = sum(items)` -> accumulate loop (inverse of accumulate-to-sum)."""
    for i, stmt in enumerate(fn.body):
        if (not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1
                or not isinstance(stmt.targets[0], ast.Name)):
            continue
        call = stmt.value
        if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                or call.func.id != "sum" or not call.args):
            continue
        clone = ast.parse(ast.unparse(fn))
        t = clone.body[0]
        name = stmt.targets[0].id
        t.body[i:i + 1] = [
            ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())],
                       value=ast.Constant(0)),
            ast.For(
                target=ast.Name(id="item", ctx=ast.Store()),
                iter=call.args[0],
                body=[ast.AugAssign(
                    target=ast.Name(id=name, ctx=ast.Store()), op=ast.Add(),
                    value=ast.Name(id="item", ctx=ast.Load()))],
                orelse=[],
            ),
        ]
        return clone.body[0]
    return None


def _inv_else_collapse(fn: ast.FunctionDef) -> ast.FunctionDef | None:
    """`if c: <terminator>` + following stmts -> `if c: T else: rest` (inv
    else-after-terminator)."""
    TERM = (ast.Return, ast.Raise, ast.Break, ast.Continue)
    body = fn.body
    for i in range(len(body) - 1):
        stmt = body[i]
        if not isinstance(stmt, ast.If) or stmt.orelse:
            continue
        if not (stmt.body and isinstance(stmt.body[-1], TERM)):
            continue
        rest = body[i + 1:]
        clone = ast.parse(ast.unparse(fn))
        t = clone.body[0]
        t.body[i + 1:i + 1] = []
        t.body[i] = ast.If(
            test=stmt.test, body=stmt.body,
            orelse=rest,
        )
        t.body[i + 1:] = []
        return clone.body[0]
    return None


INVERSES = (
    ("bool-return", _inv_bool_return),
    ("ternary", _inv_ternary),
    ("update-loop", _inv_update_loop),
    ("sum-loop", _inv_sum_loop),
    ("else-collapse", _inv_else_collapse),
)


def verbosify(source: str, rng: random.Random, max_passes: int = 3) -> str | None:
    """Apply up to `max_passes` random inverse rules to one function."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    fn = tree.body[0]
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    doc = ast.get_docstring(fn, clean=False)
    current = source
    applied = 0
    order = list(INVERSES)
    rng.shuffle(order)
    for _name, inv in order:
        if applied >= max_passes:
            break
        try:
            tree_now = ast.parse(current)
            fn_now = tree_now.body[0]
            grown = inv(fn_now)
            if grown is None:
                continue
            ast.fix_missing_locations(grown)
            text = ast.unparse(grown)
            ast.parse(text)  # the inverse must produce valid code
        except (SyntaxError, RecursionError, ValueError, AttributeError, TypeError):
            continue
        if doc and '"""' not in text.split("\n", 1)[1 if text.startswith(("async ", "def ")) else 0][:200]:
            # reinsert the original docstring under the def line: the training
            # TARGET keeps its docs, so the PROMPT must show them too
            quote = '"""'
            head, _, tail = text.split("\n", 1)
            text = f'{head}\n    {quote}{doc}{quote}\n{tail}'
        current = text
        applied += 1
    if applied == 0 or ast.dump(ast.parse(current)) == ast.dump(tree):
        return None
    return current


# ---- pair generation ---------------------------------------------------------


def symbols_of(root: Path, lang: str = "python") -> list[tuple[Path, str, str]]:
    """(path, key, source) for every top-level function worth verbosifying."""
    out = []
    for py in sorted(root.rglob("*.py")):
        if any(part in ("tests", "tests_hidden", "docs", "examples", "__pycache__")
               for part in py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and 4 <= node.end_lineno - node.lineno + 1 <= 60:
                out.append((py, node.name, "\n".join(lines[node.lineno - 1:node.end_lineno])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=REPO / "grpo" / "synthetic.jsonl")
    ap.add_argument("--pairs", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    pool: list[tuple[Path, str, str]] = []
    for root in args.roots:
        pool.extend(symbols_of(root))
    if not pool:
        print("no eligible symbols found")
        return 1

    seen: set[str] = set()
    written = 0
    from less_code.loc import measure as _measure

    with args.out.open("w", encoding="utf-8") as fh:
        for path, key, source in pool:
            if written >= args.pairs:
                break
            if key in seen:
                continue
            seen.add(key)
            for variant in range(2):  # two verbosities per symbol
                verbose = verbosify(source, rng, max_passes=variant + 1)
                if verbose is None or verbose == source:
                    continue
                # only pairs where the verbose side is genuinely BIGGER:
                # an inverse that did not add code teaches nothing
                if _measure(verbose, "python").code <= _measure(source, "python").code:
                    continue
                try:
                    ast.parse(verbose)
                    ast.parse(source)
                except SyntaxError:
                    continue
                # prompt exactly as the pipeline builds it: same system, same
                # instruction, spec-free (the spec varies per repo and is not
                # what we are teaching)
                prompt = build_symbol_prompt(
                    path, "python", key, verbose,
                    len([l for l in verbose.splitlines() if l.strip()]),
                    fmap="", feedback="", spec="", owner="",
                )
                fh.write(json.dumps({
                    "kind": "synthetic",
                    "unit": f"{path.name}:{key}",
                    "system": SYMBOL_SYSTEM_PROMPT,
                    "prompt": prompt,
                    "response": f"```python\n{source.strip()}\n```",
                }) + "\n")
                written += 1
                break
    print(f"{written} synthetic pairs -> {args.out}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
